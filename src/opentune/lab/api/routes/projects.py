"""Project library: engine configurations with their calibration lineage, runs and reports."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from opentune.lab.api.common import state
from opentune.lab.calibration.defaults import build_baseline
from opentune.lab.physics.config import EngineConfig

router = APIRouter(prefix="/api/projects", tags=["projects"])


class NewProject(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    preset_id: str = "b58-inspired"
    config: dict[str, Any] | None = None
    notes: str = ""


class PatchProject(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    notes: str | None = None
    config: dict[str, Any] | None = None


def _project_view(request: Request, pid: str) -> dict:
    st = state(request)
    p = st.library.get_project(pid)
    d = p.to_dict()
    d["engine_name"] = p.config.get("name")
    d["engine_id"] = p.config.get("id")
    d["active_version"] = st.library.calibrations.info(p.active_version_id).to_dict()
    d["flashed_version"] = (st.library.calibrations.info(p.flashed_version_id).to_dict()
                            if p.flashed_version_id else None)
    d["flashed_unknown"] = p.flashed_version_id == ""  # ECU holds content not saved in this project
    d["live"] = bool(st.live and st.live.project_id == pid)
    return d


@router.get("")
def list_projects(request: Request) -> list[dict]:
    return [_project_view(request, p.id) for p in state(request).library.list_projects()]


@router.post("")
def create_project(request: Request, body: NewProject) -> dict:
    cfg = EngineConfig.from_dict(body.config) if body.config else None
    st = state(request)
    p = st.library.create_project(body.name, body.preset_id, cfg, body.notes)
    st.device(p.id)  # factory-program the project's virtual ECU with the baseline
    return _project_view(request, p.id)


@router.get("/{pid}")
def get_project(request: Request, pid: str) -> dict:
    return _project_view(request, pid)


@router.patch("/{pid}")
def patch_project(request: Request, pid: str, body: PatchProject) -> dict:
    cfg = EngineConfig.from_dict(body.config) if body.config is not None else None
    state(request).library.update_project(pid, name=body.name, notes=body.notes, config=cfg)
    return _project_view(request, pid)


@router.delete("/{pid}")
def delete_project(request: Request, pid: str) -> dict:
    st = state(request)
    if st.live and st.live.project_id == pid:
        st.replace_live(None)
    st.drop_editor(pid)
    st.library.delete_project(pid)
    import shutil

    st.device_path(pid).unlink(missing_ok=True)
    shutil.rmtree(st.backup_dir(pid), ignore_errors=True)
    return {"deleted": pid}


@router.post("/{pid}/regenerate-baseline")
def regenerate_baseline(request: Request, pid: str) -> dict:
    """Generate a new synthetic baseline from the project's current engine configuration."""
    st = state(request)
    project = st.library.get_project(pid)
    cal = build_baseline(project.engine_config())
    info = st.library.calibrations.save(cal, note="Regenerated synthetic baseline (engine configuration changed)",
                                        lineage=project.calibration_lineage, parent_id=project.active_version_id)
    st.library.calibrations.set_baseline_for_lineage(info.id)
    st.library.update_project(pid, active_version_id=info.id)
    st.checkout(pid, info.id)
    return _project_view(request, pid)


@router.get("/{pid}/runs")
def project_runs(request: Request, pid: str) -> list[dict]:
    return state(request).library.list_runs(pid)


@router.get("/{pid}/reports")
def project_reports(request: Request, pid: str) -> list[dict]:
    return state(request).library.list_reports(pid)


@router.get("/reports/{rid}")
def get_report(request: Request, rid: str) -> dict:
    return state(request).library.get_report(rid)


@router.delete("/reports/{rid}")
def delete_report(request: Request, rid: str) -> dict:
    state(request).library.delete_report(rid)
    return {"deleted": rid}
