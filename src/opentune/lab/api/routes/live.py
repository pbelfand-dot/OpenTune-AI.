"""Live simulation control, diagnostics (DTCs, sensors, fault injection) and the WebSocket stream."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from opentune.lab.api.common import jsonable, state
from opentune.lab.api.live import LiveSession
from opentune.lab.ecu.controller import sensor_list
from opentune.lab.ecu.sensors import FAULT_TYPES
from opentune.obd.dtc import describe

router = APIRouter(tags=["live"])


class StartBody(BaseModel):
    project_id: str
    calibration: Literal["flashed", "active"] = "flashed"
    warm: bool = True
    speed: float = Field(1.0, ge=0.1, le=4.0)
    ambient_temp_c: float = Field(25.0, ge=-30.0, le=50.0)
    ambient_pressure_kpa: float = Field(101.325, ge=60.0, le=110.0)
    rate_hz: float = Field(20.0, ge=5.0, le=50.0)


class ControlBody(BaseModel):
    action: Literal["ignition_on", "ignition_off", "crank", "stop_engine", "pedal", "load", "dyno_target", "speed",
                    "pause", "resume", "ambient"]
    value: float | None = None
    load: Literal["neutral", "dyno", "vehicle"] | None = None
    target_rpm: float | None = None
    gear_ratio: float | None = None
    temp_c: float | None = None
    pressure_kpa: float | None = None


class FaultBody(BaseModel):
    sensor: str
    kind: str
    magnitude: float = 0.0


def _live(request: Request) -> LiveSession:
    live = state(request).live
    if live is None:
        raise HTTPException(409, "No live simulation running — start one first")
    return live


@router.post("/api/live/start")
def start(request: Request, body: StartBody) -> dict:
    st = state(request)
    project = st.library.get_project(body.project_id)
    if body.calibration == "flashed":
        # Run exactly what is in the virtual ECU's memory (decoded from the running bank).
        from opentune.lab.flash import decode_image

        dev = st.device(project.id)
        image = dev.running_image() if dev.powered else None
        if image is None:
            raise HTTPException(409, "Virtual ECU is in bootloader (recovery) mode — flash a calibration first")
        cal = decode_image(image)
        vid = st.version_for_hash(project.id, cal.content_hash())
        info = st.library.calibrations.info(vid) if vid else None
        label = f"{cal.name}{f' v{info.version}' if info else ''} (flashed, bank {dev.running_bank})"
    else:
        cal = st.library.calibrations.load(project.active_version_id)
        info = st.library.calibrations.info(project.active_version_id)
        label = f"{cal.name} v{info.version} (active version, not flashed)"
    session = LiveSession(project.id, project.engine_config(), cal, label, rate_hz=body.rate_hz, speed=body.speed,
                          warm=body.warm, ambient_temp_c=body.ambient_temp_c,
                          ambient_pressure_kpa=body.ambient_pressure_kpa)
    st.replace_live(session)
    return session.status()


@router.post("/api/live/stop")
def stop(request: Request) -> dict:
    state(request).replace_live(None)
    return {"stopped": True}


@router.get("/api/live/status")
def status(request: Request) -> dict:
    live = state(request).live
    return {"running": False} if live is None else {"running": True, **live.status()}


@router.post("/api/live/control")
def control(request: Request, body: ControlBody) -> dict:
    live = _live(request)
    a = body.action
    if a == "ignition_on":
        live.ignition(True)
    elif a in ("ignition_off", "stop_engine"):
        live.stop_engine()
    elif a == "crank":
        live.crank()
    elif a == "pedal":
        live.set_pedal(body.value if body.value is not None else 0.0)
    elif a == "load":
        live.set_load(body.load or "neutral", body.target_rpm, body.gear_ratio)
    elif a == "dyno_target":
        live.set_dyno_target(body.target_rpm or body.value or 2000.0)
    elif a == "speed":
        live.set_speed(body.value or 1.0)
    elif a == "pause":
        live.paused = True
    elif a == "resume":
        live.paused = False
    elif a == "ambient":
        if body.temp_c is None or body.pressure_kpa is None:
            raise HTTPException(400, "temp_c and pressure_kpa required")
        live.set_ambient(body.temp_c, body.pressure_kpa)
    return live.status()


@router.get("/api/live/history")
def history(request: Request, seconds: float = 30.0) -> list[dict]:
    live = _live(request)
    n = int(max(1.0, min(seconds, 120.0)) * live.rate_hz)
    return list(live.history)[-n:]


# ------------------------------------------------------------------ diagnostics
@router.get("/api/diagnostics/meta")
def diagnostics_meta() -> dict:
    sensors = sensor_list()
    codes = sorted({c for s in sensors for c in (s["dtc_low"], s["dtc_high"]) if c} |
                   {"P0016", "P0171", "P0172", "P0219", "P0234", "P0299", "P0335", "P0340"})
    return {"sensors": sensors, "fault_types": list(FAULT_TYPES),
            "dtcs": [{"code": c, "description": describe(c).description} for c in codes]}


@router.get("/api/diagnostics/dtcs")
def dtcs(request: Request) -> list[dict]:
    return _live(request).dtc_report()


@router.post("/api/diagnostics/dtcs/clear")
def clear_dtcs(request: Request) -> dict:
    _live(request).clear_dtcs()
    return {"cleared": True}


@router.get("/api/diagnostics/sensors")
def sensors(request: Request) -> list[dict]:
    return _live(request).sensors()


@router.post("/api/diagnostics/faults")
def inject(request: Request, body: FaultBody) -> dict:
    live = _live(request)
    live.inject_fault(body.sensor, body.kind, body.magnitude)
    return live.status()


@router.delete("/api/diagnostics/faults")
def clear_faults(request: Request, sensor: str | None = None) -> dict:
    live = _live(request)
    live.clear_faults(sensor)
    return live.status()


# ------------------------------------------------------------------ WebSocket
@router.websocket("/ws/live")
async def ws_live(ws: WebSocket) -> None:
    await ws.accept()
    st = ws.app.state.lab
    session: LiveSession | None = None
    queue: asyncio.Queue | None = None
    loop = asyncio.get_running_loop()
    try:
        while True:
            if st.live is not session:  # session started, replaced or stopped
                if session is not None and queue is not None:
                    session.unsubscribe(queue)
                session, queue = st.live, None
                if session is not None:
                    queue = session.subscribe(loop)
                    await ws.send_json(jsonable({"type": "status", **session.status()}))
                else:
                    await ws.send_json({"type": "status", "running": False})
            if queue is None:
                await asyncio.sleep(0.25)
                continue
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            await ws.send_json(jsonable(msg))
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        if session is not None and queue is not None:
            with contextlib.suppress(Exception):
                session.unsubscribe(queue)
