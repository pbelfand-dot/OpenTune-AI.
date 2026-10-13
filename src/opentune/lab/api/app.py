"""FastAPI application for the Virtual Engine Lab (REST + WebSocket + the browser UI).

The server binds to 127.0.0.1 by default: it is a local engineering tool, not
a network service. It has no endpoint that can reach automotive hardware; the
only "ECU" it can program is the virtual one.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from opentune import __version__
from opentune.lab import LAB_VERSION
from opentune.lab.api.common import SafeJSONResponse
from opentune.lab.api.live import LiveError
from opentune.lab.api.routes import calibration, dyno, engines, live, projects
from opentune.lab.api.state import LabState
from opentune.lab.calibration.tables import CalibrationError
from opentune.lab.dyno.runs import DynoError
from opentune.lab.provenance import Provenance
from opentune.lab.storage.library import LibraryError

WEB_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"

FALLBACK_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>OpenTune Virtual Engine Lab</title>
<style>body{background:#0f1117;color:#d8dee9;font-family:system-ui;margin:3rem;max-width:46rem}
code{background:#1b1f2a;padding:.1rem .3rem;border-radius:4px}a{color:#4cc9f0}</style></head><body>
<h1>OpenTune Virtual Engine Lab</h1><p>The API is running, but the browser UI has not been built.</p>
<p>Build it with <code>cd web &amp;&amp; npm ci &amp;&amp; npm run build</code>, then reload.
The REST API documentation is at <a href="/docs">/docs</a>.</p></body></html>"""


def plotly_js_path() -> Path | None:
    spec = importlib.util.find_spec("plotly")
    if spec is None or not spec.submodule_search_locations:
        return None
    path = Path(list(spec.submodule_search_locations)[0]) / "package_data" / "plotly.min.js"
    return path if path.exists() else None


def safety_status() -> dict:
    from opentune.ecu.safety import REAL_ECU_WRITE_ENABLED

    return {
        "real_ecu_write_enabled": REAL_ECU_WRITE_ENABLED,
        "physical_hardware_access": "none — the lab has no code path to serial, CAN, USB or J2534 devices",
        "flash_target": "virtual ECU only (simulated memory)",
        "statement": "Simulation and engineering-development tool. Results are simulated, not measurements; "
                     "calibrations are synthetic and must not be used on a vehicle.",
    }


def create_app(data_dir: str | Path | None = None, demo_projects: bool = True, job_workers: int = 2) -> FastAPI:
    if data_dir is None:
        from opentune.config import default_data_dir

        data_dir = default_data_dir()
    lab = LabState(Path(data_dir), job_workers=job_workers)
    if demo_projects:
        lab.ensure_demo_projects()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        lab.shutdown()

    app = FastAPI(title="OpenTune Virtual Engine Lab", version=LAB_VERSION, default_response_class=SafeJSONResponse,
                  lifespan=lifespan,
                  description="Simulation-only engine, virtual-ECU and calibration laboratory. No physical "
                              "hardware access.")
    app.state.lab = lab

    @app.exception_handler(LibraryError)
    async def _not_found(request: Request, exc: LibraryError):
        return SafeJSONResponse({"detail": str(exc).strip("'\"")}, status_code=404)

    @app.exception_handler(KeyError)
    async def _key_error(request: Request, exc: KeyError):
        return SafeJSONResponse({"detail": str(exc).strip("'\"")}, status_code=404)

    for exc_type in (ValueError, CalibrationError, DynoError, LiveError):
        @app.exception_handler(exc_type)
        async def _bad_request(request: Request, exc: Exception):
            return SafeJSONResponse({"detail": str(exc)}, status_code=400)

    @app.get("/api/system", tags=["system"])
    def system() -> dict:
        from opentune.lab.integrations import integration_status

        return {"app_version": __version__, "lab_version": LAB_VERSION,
                "python": sys.version.split()[0], "platform": sys.platform,
                "data_dir": str(lab.root), "safety": safety_status(),
                "provenance_kinds": [{"id": p.value, "label": p.label} for p in Provenance],
                "integrations": integration_status(), "ui_built": (WEB_DIST / "index.html").exists()}

    @app.get("/api/health", tags=["system"])
    def health() -> dict:
        return {"ok": True}

    for r in (engines.router, projects.router, calibration.router, live.router, dyno.router):
        app.include_router(r)
    from opentune.lab.api.routes import extra

    for r in extra.routers():
        app.include_router(r)

    plotly_path = plotly_js_path()

    @app.get("/vendor/plotly.min.js", include_in_schema=False)
    def plotly_js():
        if plotly_path is None:
            return HTMLResponse("/* plotly package not installed: pip install 'opentune-ai[lab]' */",
                                media_type="application/javascript")
        return FileResponse(plotly_path, media_type="application/javascript",
                            headers={"Cache-Control": "public, max-age=86400"})

    if (WEB_DIST / "assets").exists():
        app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith(("api/", "ws/")):
            return SafeJSONResponse({"detail": "Not found"}, status_code=404)
        candidate = (WEB_DIST / path).resolve()
        if path and candidate.is_file() and WEB_DIST.resolve() in candidate.parents:
            return FileResponse(candidate)
        index = WEB_DIST / "index.html"
        if index.exists():
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        return HTMLResponse(FALLBACK_PAGE)

    return app


def serve(host: str = "127.0.0.1", port: int = 8765, data_dir: str | None = None, open_browser: bool = False) -> None:
    import uvicorn

    if host not in ("127.0.0.1", "localhost", "::1") and not os.environ.get("OPENTUNE_LAB_ALLOW_REMOTE"):
        raise SystemExit("Refusing to listen on a non-loopback address. Set OPENTUNE_LAB_ALLOW_REMOTE=1 if you "
                         "really want the lab reachable from your network (it has no authentication).")
    app = create_app(data_dir)
    if open_browser:
        import threading
        import webbrowser

        threading.Timer(1.0, lambda: webbrowser.open(f"http://{host}:{port}/")).start()
    uvicorn.run(app, host=host, port=port, log_level="info")
