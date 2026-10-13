"""Calibration editing: tables, scalars, undo/redo, versions, baseline comparison, import/export."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Literal

import numpy as np
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from opentune.lab.api.common import state
from opentune.lab.calibration.defaults import TABLE_CATEGORIES
from opentune.lab.calibration.io import (
    export_json,
    export_mat,
    import_json,
    table_from_csv,
    table_to_csv,
)
from opentune.lab.calibration.tables import CalibrationError

router = APIRouter(prefix="/api/projects/{pid}/calibration", tags=["calibration"])


class EditBody(BaseModel):
    op: Literal["set_cell", "set_cells", "region", "axis", "replace"]
    index: list[int] | None = None
    value: float | None = None
    cells: list[tuple[list[int], float]] | None = None
    operation: str | None = None
    amount: float = 0.0
    region: list[tuple[int, int]] | None = None
    axis: int | None = None
    point: int | None = None
    resample: bool = True
    values: list[Any] | None = None


class ScalarBody(BaseModel):
    value: float


class SaveBody(BaseModel):
    note: str = Field("", max_length=500)


class TextBody(BaseModel):
    text: str


class CheckoutBody(BaseModel):
    discard_changes: bool = False


def _baseline(request: Request, pid: str):
    return state(request).library.load_calibration(pid, "baseline")


def _summary(request: Request, pid: str) -> dict:
    st = state(request)
    editor = st.editor(pid)
    cal = editor.calibration
    base = _baseline(request, pid)
    diff = base.diff(cal)
    project = st.library.get_project(pid)
    tables = [{"id": t.id, "name": t.name, "category": t.category, "unit": t.unit, "ndim": t.ndim,
               "shape": list(t.shape), "axes": [{"name": a.name, "label": a.label, "unit": a.unit} for a in t.axes],
               "description": t.description, "provenance": t.provenance,
               "changed_cells": diff.tables[t.id].changed_cells if t.id in diff.tables else 0}
              for t in cal.tables.values()]
    scalars = [{**s.to_dict(), "baseline": base.scalars[s.id].value if s.id in base.scalars else None}
               for s in cal.scalars.values()]
    version_id = st.editor_version(pid)
    return {"name": cal.name, "engine_id": cal.engine_id, "content_hash": cal.content_hash(),
            "schema_version": cal.schema_version, "firmware_family": cal.firmware_family,
            "metadata": cal.metadata, "categories": TABLE_CATEGORIES, "tables": tables, "scalars": scalars,
            "version_id": version_id, "active_version_id": project.active_version_id,
            "flashed_version_id": project.flashed_version_id, "dirty": st.is_dirty(pid),
            "history": editor.history(), "can_undo": editor.can_undo, "can_redo": editor.can_redo,
            "problems": cal.validate(), "changed_vs_baseline": not diff.identical}


def _table_view(request: Request, pid: str, tid: str) -> dict:
    editor = state(request).editor(pid)
    t = editor.calibration.table(tid)
    d = t.to_dict()
    base = _baseline(request, pid)
    if tid in base.tables and base.tables[tid].shape == t.shape and \
            [a.values for a in base.tables[tid].axes] == [a.values for a in t.axes]:
        d["baseline_values"] = base.tables[tid].values.tolist()
        d["delta"] = (t.values - base.tables[tid].values).round(6).tolist()
    else:
        d["baseline_values"] = None
        d["delta"] = None
    d["can_undo"], d["can_redo"] = editor.can_undo, editor.can_redo
    d["content_hash"] = editor.calibration.content_hash()
    return d


@router.get("")
def summary(request: Request, pid: str) -> dict:
    return _summary(request, pid)


@router.get("/tables/{tid}")
def get_table(request: Request, pid: str, tid: str) -> dict:
    return _table_view(request, pid, tid)


@router.post("/tables/{tid}/lookup")
def lookup(request: Request, pid: str, tid: str, coords: list[float]) -> dict:
    t = state(request).editor(pid).calibration.table(tid)
    return {"value": t.lookup(*coords)}


@router.post("/tables/{tid}/edit")
def edit_table(request: Request, pid: str, tid: str, body: EditBody) -> dict:
    editor = state(request).editor(pid)
    if body.op == "set_cell":
        if body.index is None or body.value is None:
            raise HTTPException(400, "index and value required")
        cmd = editor.set_cell(tid, tuple(body.index), body.value)
    elif body.op == "set_cells":
        if not body.cells:
            raise HTTPException(400, "cells required")
        cmd = editor.set_cells(tid, [(tuple(i), v) for i, v in body.cells])
    elif body.op == "region":
        if not body.operation:
            raise HTTPException(400, "operation required")
        region = tuple(tuple(r) for r in body.region) if body.region else None
        cmd = editor.apply_region(tid, body.operation, body.amount, region)
    elif body.op == "axis":
        if body.axis is None or body.point is None or body.value is None:
            raise HTTPException(400, "axis, point and value required")
        cmd = editor.set_axis_value(tid, body.axis, body.point, body.value, body.resample)
    else:
        if body.values is None:
            raise HTTPException(400, "values required")
        cmd = editor.replace_values(tid, np.asarray(body.values, dtype=float))
    view = _table_view(request, pid, tid)
    view["last_edit"] = cmd.description
    return view


@router.post("/scalars/{sid}")
def set_scalar(request: Request, pid: str, sid: str, body: ScalarBody) -> dict:
    cmd = state(request).editor(pid).set_scalar(sid, body.value)
    return {"last_edit": cmd.description, **_summary(request, pid)}


@router.post("/undo")
def undo(request: Request, pid: str) -> dict:
    cmd = state(request).editor(pid).undo()
    return {"undone": cmd.description, "target": cmd.target, "kind": cmd.kind, **_summary(request, pid)}


@router.post("/redo")
def redo(request: Request, pid: str) -> dict:
    cmd = state(request).editor(pid).redo()
    return {"redone": cmd.description, "target": cmd.target, "kind": cmd.kind, **_summary(request, pid)}


@router.post("/save")
def save(request: Request, pid: str, body: SaveBody) -> dict:
    st = state(request)
    editor = st.editor(pid)
    project = st.library.get_project(pid)
    info = st.library.calibrations.save(editor.calibration, note=body.note, parent_id=st.editor_version(pid),
                                        lineage=project.calibration_lineage)
    st.library.update_project(pid, active_version_id=info.id)
    st.set_editor_version(pid, info.id)
    return {"version": info.to_dict(), **_summary(request, pid)}


@router.get("/versions")
def versions(request: Request, pid: str) -> list[dict]:
    st = state(request)
    project = st.library.get_project(pid)
    out = []
    for v in st.library.calibrations.history(project.calibration_lineage):
        d = v.to_dict()
        d["active"] = v.id == project.active_version_id
        d["flashed"] = v.id == project.flashed_version_id
        d["checked_out"] = v.id == st.editor_version(pid)
        out.append(d)
    return out


@router.post("/versions/{vid}/checkout")
def checkout(request: Request, pid: str, vid: str, body: CheckoutBody) -> dict:
    st = state(request)
    if st.is_dirty(pid) and not body.discard_changes:
        raise HTTPException(409, "Unsaved changes — save them or set discard_changes")
    st.checkout(pid, vid)
    st.library.update_project(pid, active_version_id=vid)
    return _summary(request, pid)


@router.post("/versions/{vid}/baseline")
def set_baseline(request: Request, pid: str, vid: str) -> dict:
    st = state(request)
    project = st.library.get_project(pid)
    if st.library.calibrations.info(vid).lineage != project.calibration_lineage:
        raise HTTPException(404, "Unknown version for this project")
    st.library.calibrations.set_baseline_for_lineage(vid)
    return _summary(request, pid)


@router.get("/diff")
def diff(request: Request, pid: str, against: str = "baseline", version_id: str | None = None) -> dict:
    st = state(request)
    other = st.library.load_calibration(pid, "version" if against == "version" else against, version_id)
    d = other.diff(st.editor(pid).calibration).to_dict()
    d["against"] = against
    d["other_hash"] = other.content_hash()
    return d


@router.post("/validate")
def validate(request: Request, pid: str) -> dict:
    problems = state(request).editor(pid).calibration.validate()
    return {"valid": not problems, "problems": problems}


@router.get("/export.json", response_class=PlainTextResponse)
def export(request: Request, pid: str) -> PlainTextResponse:
    cal = state(request).editor(pid).calibration
    return PlainTextResponse(export_json(cal), media_type="application/json",
                             headers={"Content-Disposition": f'attachment; filename="{cal.engine_id}.otcal.json"'})


@router.get("/export.mat")
def export_matlab(request: Request, pid: str) -> FileResponse:
    cal = state(request).editor(pid).calibration
    path = Path(tempfile.mkdtemp(prefix="otlab-")) / f"{cal.engine_id}_calibration.mat"
    export_mat(cal, path)
    return FileResponse(path, media_type="application/octet-stream", filename=path.name)


@router.get("/tables/{tid}/csv", response_class=PlainTextResponse)
def table_csv(request: Request, pid: str, tid: str) -> PlainTextResponse:
    t = state(request).editor(pid).calibration.table(tid)
    return PlainTextResponse(table_to_csv(t), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{tid}.csv"'})


@router.post("/tables/{tid}/csv")
def import_table_csv(request: Request, pid: str, tid: str, body: TextBody) -> dict:
    editor = state(request).editor(pid)
    values = table_from_csv(editor.calibration.table(tid), body.text)
    editor.replace_values(tid, values, f"{tid}: imported from CSV")
    return _table_view(request, pid, tid)


@router.post("/import")
def import_calibration(request: Request, pid: str, body: TextBody) -> dict:
    """Replace the working copy with an imported OpenTune calibration (save afterwards to keep it)."""
    st = state(request)
    cal = import_json(body.text)
    current = st.editor(pid).calibration
    if cal.engine_id != current.engine_id:
        raise CalibrationError(f"Calibration is for engine {cal.engine_id!r}, project uses {current.engine_id!r}")
    if set(cal.tables) != set(current.tables) or set(cal.scalars) != set(current.scalars):
        raise CalibrationError("Calibration layout differs from this project's (tables or scalars missing/extra)")
    editor = st.editor(pid)
    for tid, table in cal.tables.items():
        if table.shape != current.tables[tid].shape or \
                [a.values for a in table.axes] != [a.values for a in current.tables[tid].axes]:
            raise CalibrationError(f"Table {tid}: axes differ from this project's layout")
    for tid, table in cal.tables.items():
        if not np.array_equal(table.values, current.tables[tid].values):
            editor.replace_values(tid, table.values, f"{tid}: imported")
    for sid, sc in cal.scalars.items():
        if sc.value != current.scalars[sid].value:
            editor.set_scalar(sid, sc.value)
    return _summary(request, pid)
