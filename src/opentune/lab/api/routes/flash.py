"""Virtual ECU communication: identify, flash (with fault injection), read-back, rollback, backups.

Every operation targets the project's simulated ECU memory over the in-process
loopback link. There is no endpoint, parameter or transport for real hardware.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from opentune.lab.api.common import state
from opentune.lab.flash import (
    DeviceFaults,
    LinkFaults,
    LoopbackLink,
    Programmer,
    build_image,
    decode_image,
    verify_image,
)
from opentune.lab.flash.image import parse_header

router = APIRouter(tags=["virtual ECU"])

FAULT_HELP = {
    "link_corrupt_rate": "Probability that a frame gets a flipped bit (CRC catches it; the programmer retries).",
    "link_drop_rate": "Probability that a response is lost (timeout; the programmer retries).",
    "disconnect_after_frames": "Cable pulled after N frames (interrupted transfer).",
    "power_loss_stage": "Power lost during 'erase', 'write' or 'activate'.",
    "power_loss_block": "Block index for a power loss during 'write'.",
    "bitflip_block": "A flash cell flips a bit after this block is written (caught by verify).",
}


class FlashBody(BaseModel):
    version_id: str | None = None  # default: the project's active saved version
    block_size: int = Field(512, ge=64, le=2048)
    retries: int = Field(3, ge=0, le=10)
    link_corrupt_rate: float = Field(0.0, ge=0.0, le=0.5)
    link_drop_rate: float = Field(0.0, ge=0.0, le=0.5)
    disconnect_after_frames: int | None = Field(None, ge=1, le=10000)
    power_loss_stage: Literal["erase", "write", "activate"] | None = None
    power_loss_block: int = Field(0, ge=0, le=1000)
    bitflip_block: int | None = Field(None, ge=0, le=1000)
    seed: int = 1


class ReadBody(BaseModel):
    load_into_working_copy: bool = False


def _engine_running(request: Request, pid: str) -> bool:
    live = state(request).live
    return bool(live and live.project_id == pid and live.sim.ecu.state in ("RUNNING", "CRANKING"))


def _ecu_view(request: Request, pid: str) -> dict:
    st = state(request)
    dev = st.device(pid)
    dev.engine_running = _engine_running(request, pid)
    project = st.library.get_project(pid)
    flashed = st.library.calibrations.info(project.flashed_version_id).to_dict() if project.flashed_version_id \
        else None
    return {"status": dev.status(), "flashed_version": flashed, "boot_log": dev.boot_log[-10:],
            "history": dev.history[-30:], "backups": _backups(request, pid),
            "label": "Simulated ECU (OTVECU-SIM). No physical hardware."}


def _backups(request: Request, pid: str) -> list[dict]:
    d = state(request).backup_dir(pid)
    if not d.exists():
        return []
    out = []
    for f in sorted(d.glob("*.otci"), reverse=True):
        data = f.read_bytes()
        try:
            h = verify_image(data)
            ok, engine, sha = True, h.engine_id, h.payload_sha256
        except ValueError:
            ok, engine, sha = False, None, None
        out.append({"name": f.name, "bytes": len(data), "valid": ok, "engine_id": engine, "payload_sha256": sha})
    return out


def _after_change(request: Request, pid: str, dev) -> None:
    """Persist the device, track which saved version is in the ECU, update a live session."""
    st = state(request)
    st.save_device(pid, dev)
    image = dev.running_image() if dev.powered else None
    if image is None:
        return
    cal = decode_image(image)
    vid = st.version_for_hash(pid, cal.content_hash())
    st.library.update_project(pid, flashed_version_id=vid or "")
    live = st.live
    if live is not None and live.project_id == pid:
        info = st.library.calibrations.info(vid) if vid else None
        live.load_calibration(cal, f"{cal.name} v{info.version} (flashed, bank {dev.running_bank})" if info
                              else f"{cal.name} (flashed, bank {dev.running_bank})")


@router.get("/api/flash/meta")
def meta() -> dict:
    return {"faults": FAULT_HELP, "block_sizes": [128, 256, 512, 1024, 2048],
            "protocol": "OTVL (fictional OpenTune Virtual Link) over an in-process loopback",
            "image_format": "OTCI (fictional): header CRC-32, image CRC-32, payload SHA-256",
            "note": "Simulated programming of the OpenTune virtual ECU only. Not a model of any production ECU, "
                    "bootloader or security-access scheme."}


@router.get("/api/projects/{pid}/ecu")
def ecu(request: Request, pid: str) -> dict:
    return _ecu_view(request, pid)


@router.post("/api/projects/{pid}/ecu/flash")
def flash(request: Request, pid: str, body: FlashBody) -> dict:
    st = state(request)
    project = st.library.get_project(pid)
    vid = body.version_id or project.active_version_id
    cal = st.library.load_calibration(pid, "version", vid)
    info = st.library.calibrations.info(vid)
    image = build_image(cal, sequence=info.version)
    running = _engine_running(request, pid)

    def job(progress, cancel):
        with st.lock:
            dev = st.device(pid)
            dev.engine_running = running
            dev.faults = DeviceFaults(body.power_loss_stage, body.power_loss_block, body.bitflip_block)
            link = LoopbackLink(dev, LinkFaults(body.link_corrupt_rate, body.link_drop_rate,
                                                body.disconnect_after_frames), seed=body.seed)
            prog = Programmer(link, block_size=body.block_size, retries=body.retries, progress=progress, cancel=cancel)
            report = prog.flash(image)
            if report.backup:
                bdir = st.backup_dir(pid)
                bdir.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
                name = f"{stamp}_bank{report.previous_bank}_{hashlib.sha256(report.backup).hexdigest()[:10]}.otci"
                (bdir / name).write_bytes(report.backup)
            dev.faults = DeviceFaults()
            _after_change(request, pid, dev)
            out = report.to_dict()
            out["calibration"] = {"version_id": vid, "version": info.version, "name": cal.name}
            st.library.save_report(pid, "flash", f"Flash v{info.version}: {'OK' if report.success else 'FAILED'}",
                                   out)
            return out

    return st.jobs.submit("flash", job, pid).to_dict()


@router.post("/api/projects/{pid}/ecu/rollback")
def rollback(request: Request, pid: str) -> dict:
    st = state(request)
    with st.lock:
        dev = st.device(pid)
        dev.engine_running = _engine_running(request, pid)
        report = Programmer(LoopbackLink(dev)).rollback()
        _after_change(request, pid, dev)
        out = report.to_dict()
        st.library.save_report(pid, "flash", f"Rollback: {'OK' if report.success else 'FAILED'}", out)
    return out


@router.post("/api/projects/{pid}/ecu/power-cycle")
def power_cycle(request: Request, pid: str) -> dict:
    st = state(request)
    with st.lock:
        dev = st.device(pid)
        if _engine_running(request, pid):
            raise HTTPException(409, "Switch the engine off before power-cycling the ECU")
        boot = dev.boot()
        _after_change(request, pid, dev)
    return {"boot": boot, **_ecu_view(request, pid)}


@router.post("/api/projects/{pid}/ecu/read")
def read_back(request: Request, pid: str, body: ReadBody) -> dict:
    st = state(request)
    with st.lock:
        dev = st.device(pid)
        prog = Programmer(LoopbackLink(dev))
        cal = prog.read_calibration()
        stats = prog.link.stats()
    working = st.editor(pid).calibration
    diff = cal.diff(working).to_dict()
    vid = st.version_for_hash(pid, cal.content_hash())
    loaded = False
    if body.load_into_working_copy:
        if vid:
            st.checkout(pid, vid)
            st.library.update_project(pid, active_version_id=vid)
        else:
            info = st.library.calibrations.save(cal, note="Read back from the virtual ECU",
                                                lineage=st.library.get_project(pid).calibration_lineage)
            st.checkout(pid, info.id)
            st.library.update_project(pid, active_version_id=info.id)
        loaded = True
    return {"name": cal.name, "content_hash": cal.content_hash(), "matches_version": vid, "link": stats,
            "differs_from_working_copy": not diff["identical"], "diff": diff, "loaded": loaded}


@router.get("/api/projects/{pid}/ecu/backups")
def backups(request: Request, pid: str) -> list[dict]:
    return _backups(request, pid)


@router.post("/api/projects/{pid}/ecu/backups/{name}/restore")
def restore(request: Request, pid: str, name: str) -> dict:
    if not re.fullmatch(r"[\w.-]+\.otci", name):
        raise HTTPException(400, "Bad backup name")
    st = state(request)
    path = st.backup_dir(pid) / name
    if not path.exists():
        raise HTTPException(404, "Unknown backup")
    image = path.read_bytes()
    parse_header(image)
    with st.lock:
        dev = st.device(pid)
        dev.engine_running = _engine_running(request, pid)
        report = Programmer(LoopbackLink(dev)).flash(image, operation="restore")
        _after_change(request, pid, dev)
        out = report.to_dict()
        st.library.save_report(pid, "flash", f"Restore {name}: {'OK' if report.success else 'FAILED'}", out)
    return out
