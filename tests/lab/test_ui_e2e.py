"""Browser smoke test of the built UI (skipped when Playwright/Chromium or the bundle is unavailable).

Starts the real server on a free loopback port, opens every page in headless
Chromium and checks that each renders its key content without console errors.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path

import pytest

from opentune.lab.api.app import WEB_DIST, create_app

playwright = pytest.importorskip("playwright.sync_api")
if not (WEB_DIST / "index.html").exists():
    pytest.skip("UI bundle not built (cd web && npm ci && npm run build)", allow_module_level=True)


def _chromium() -> str | None:
    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers"))
    for cand in sorted(root.glob("chromium-*/chrome-linux/chrome")):
        return str(cand)
    return None


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    import uvicorn

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    app = create_app(tmp_path_factory.mktemp("ui"))
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    t.join(timeout=5)


PAGES = [("dashboard", "Workflow (10 steps)"), ("engine", "Steady-state full-load preview"),
         ("tuning", "Base ignition timing"), ("dyno", "Run test"), ("control", "PID loop lab"),
         ("diagnostics", "No live session"), ("ai", "Ask the analyst"), ("tests", "Interrupted ECU update"),
         ("tuner", "Start tuning session"), ("library", "New project")]


def test_every_page_renders_without_console_errors(server):
    exe = _chromium()
    errors: list[str] = []
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - no browser on this machine
            pytest.skip(f"Chromium not available: {exc}")
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.on("console", lambda m: m.type == "error" and errors.append(m.text))
        page.on("pageerror", lambda e: errors.append(str(e)))
        for path, text in PAGES:
            page.goto(f"{server}/#/{path}")
            page.get_by_text(text).first.wait_for(timeout=20000)
        assert "Real ECU writes" in page.content()
        # tuning engine tabs: knowledge and benchmark views render (empty knowledge base)
        page.goto(f"{server}/#/tuner")
        page.get_by_role("button", name="What it has learned").click()
        page.get_by_text("Learned sensitivities").first.wait_for(timeout=20000)
        page.get_by_role("button", name="Benchmark & training").click()
        page.get_by_text("held-out engines").first.wait_for(timeout=20000)
        # a small interaction: start the live engine and see frames arrive
        page.goto(f"{server}/#/dashboard")
        page.get_by_role("button", name="Start session").click()
        page.get_by_role("button", name="Crank / start").click()
        page.get_by_text("ECU RUNNING").first.wait_for(timeout=20000)
        browser.close()
    assert errors == []
