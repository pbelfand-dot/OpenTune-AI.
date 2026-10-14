"""Tuning engine: sessions, knowledge base, mistakes, benchmark and training (simulation only)."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from opentune.lab.api.common import state
from opentune.lab.api.routes.dyno import CalibrationSource, describe_source, load_calibration
from opentune.lab.calibration.tables import Calibration
from opentune.lab.physics.config import EngineConfig
from opentune.lab.physics.presets import PRESETS
from opentune.lab.tuning.actions import ACTIONS, DEFAULT_ACTIONS
from opentune.lab.tuning.benchmark import CURRICULUM, SUITES, run_benchmark, train
from opentune.lab.tuning.evaluator import (
    CONDITION_INFO,
    FIDELITIES,
    GOALS,
    BatteryConfig,
    StressLimits,
)
from opentune.lab.tuning.knowledge import OUTPUTS, ResponseModel, output_unit
from opentune.lab.tuning.session import DISCLAIMER, TUNER_VERSION, TuningRequest, TuningSession

router = APIRouter(tags=["tuning engine"])

TUNED_TABLES = ("torque_limit", "boost_max", "ign_base", "lambda_target")


def engine_key(cfg: EngineConfig) -> str:
    """Knowledge is kept per engine model: the preset id, or the id plus a hash if it was edited."""
    preset = PRESETS.get(cfg.id)
    if preset is not None and preset().to_dict() == cfg.to_dict():
        return cfg.id
    blob = json.dumps({k: v for k, v in cfg.to_dict().items() if k not in ("name", "description")},
                      sort_keys=True).encode()
    return f"{cfg.id}~p{hashlib.sha256(blob).hexdigest()[:8]}"


class SessionBody(BaseModel):
    project_id: str
    calibration: CalibrationSource = Field(default_factory=CalibrationSource)
    request: dict[str, Any] = Field(default_factory=dict)


class ApplyBody(BaseModel):
    session_id: str


class BenchmarkBody(BaseModel):
    suite: Literal["smoke", "quick", "standard"] = "quick"
    learn: bool = False


class TrainBody(BaseModel):
    rounds: int = Field(1, ge=1, le=10)


class ResetBody(BaseModel):
    confirm: bool = False


@router.get("/api/tuning/meta")
def meta(request: Request, project_id: str | None = None) -> dict:
    limits = StressLimits()
    if project_id:
        limits = StressLimits.from_config(state(request).library.get_project(project_id).engine_config())
    return {
        "tuner_version": TUNER_VERSION,
        "goals": [{"id": k, **v} for k, v in GOALS.items()],
        "actions": [a.to_dict() for a in ACTIONS.values()], "default_actions": list(DEFAULT_ACTIONS),
        "fidelities": {f: {"conditions": {c: CONDITION_INFO[c] for c in BatteryConfig.preset(f).conditions},
                           "part_load_points": BatteryConfig.preset(f).part_load_points,
                           "tip_in": BatteryConfig.preset(f).tip_in} for f in FIDELITIES},
        "defaults": TuningRequest().to_dict(), "limits": limits.to_dict(),
        "suites": {k: [t.to_dict() for t in v] for k, v in SUITES.items()},
        "curriculum": [t.to_dict() for t in CURRICULUM],
        "outputs": {k: output_unit(k) for k in OUTPUTS},
        "tables_changed": list(TUNED_TABLES),
        "disclaimer": DISCLAIMER, "ready_for_vehicle": False,
    }


@router.post("/api/tuning/sessions")
def start_session(request: Request, body: SessionBody) -> dict:
    st = state(request)
    project = st.library.get_project(body.project_id)
    cfg = project.engine_config()
    req = TuningRequest.from_dict(body.request)
    problems = req.problems()
    if problems:
        raise HTTPException(400, "; ".join(problems))
    cal = load_calibration(request, project.id, body.calibration)
    label = describe_source(request, project.id, body.calibration, cal)
    key = engine_key(cfg)

    def job(progress, cancel):
        report = TuningSession(cfg, cal, req, st.knowledge, engine_key=key, project_id=project.id,
                               progress=progress, cancel=cancel).run()
        report["calibration_source"] = label
        return {"session_id": report["id"], "summary": report["summary"], "stop_reason": report["stop_reason"],
                "ready_for_vehicle": False}

    return st.jobs.submit("tuning", job, project.id).to_dict()


@router.get("/api/tuning/sessions")
def list_sessions(request: Request, project_id: str | None = None, limit: int = 50) -> list[dict]:
    rows = state(request).knowledge.sessions(min(max(limit, 1), 500))
    return [r for r in rows if project_id is None or r["project_id"] == project_id]


@router.get("/api/tuning/sessions/{sid}")
def get_session(request: Request, sid: str, include_calibration: bool = False) -> dict:
    report = state(request).knowledge.session_report(sid)
    if report is None:
        raise HTTPException(404, "Unknown or unfinished tuning session")
    if not include_calibration:
        report.pop("calibration", None)
    return report


@router.post("/api/projects/{pid}/tuning/apply")
def apply_session(request: Request, pid: str, body: ApplyBody) -> dict:
    """Write a session's recommended tables into the working copy (undoable) — for simulation only."""
    st = state(request)
    report = st.knowledge.session_report(body.session_id)
    if report is None or report.get("project_id") != pid:
        raise HTTPException(404, "Unknown tuning session for this project")
    cal = Calibration.from_dict(report["calibration"])
    editor = st.editor(pid)
    changed = []
    for tid in TUNED_TABLES:
        new = cal.table(tid)
        cur = editor.calibration.table(tid)
        if [a.values for a in new.axes] != [a.values for a in cur.axes]:
            raise HTTPException(409, f"{tid}: axes of the working copy differ from the session's baseline")
        if (new.values != cur.values).any():
            editor.replace_values(tid, new.values, f"{tid}: tuning-engine recommendation (simulation only)")
            changed.append(tid)
    return {"changed_tables": changed, "changes": report["final"]["changes"], "ready_for_vehicle": False,
            "validated": report["summary"]["validated"],
            "note": "Applied to the working copy for further simulation. Verify on the virtual dyno and in the Test "
                    "Laboratory; undo to revert. Not evidence that it is safe for a real vehicle."}


@router.get("/api/tuning/knowledge")
def knowledge(request: Request, engine: str | None = None, project_id: str | None = None,
              fidelity: str = "quick") -> dict:
    st = state(request)
    kb = st.knowledge
    out = kb.summary()
    key = engine
    if project_id:
        key = engine_key(st.library.get_project(project_id).engine_config())
    if key:
        actions = list(DEFAULT_ACTIONS)
        prior = kb.prior(actions, key, fidelity)
        model = ResponseModel(actions, prior)
        out["engine_key"] = key
        out["sources"] = prior.source
        out["sensitivities"] = {a: {k: [round(v, 4) for v in model.predict({a: 1.0}, raw=True)[k]] for k in OUTPUTS}
                                for a in actions}
        out["sensitivity_note"] = ("Predicted change (mean, σ) for one action unit from the baseline: metrics in %, "
                                   "margins in percentage points, knock in degrees. Learned from simulated experiments.")
        out["units"] = {k: output_unit(k) for k in OUTPUTS}
        out["actions"] = {a: ACTIONS[a].to_dict() for a in actions}
    return out


@router.get("/api/tuning/mistakes")
def mistakes(request: Request, limit: int = 100) -> list[dict]:
    return state(request).knowledge.mistakes(min(max(limit, 1), 1000))


@router.post("/api/tuning/benchmark")
def benchmark(request: Request, body: BenchmarkBody) -> dict:
    st = state(request)

    def job(progress, cancel):
        return run_benchmark(st.knowledge, body.suite, body.learn, progress, cancel)

    return st.jobs.submit("tuning-benchmark", job).to_dict()


@router.get("/api/tuning/benchmarks")
def benchmarks(request: Request, limit: int = 50) -> list[dict]:
    return state(request).knowledge.benchmarks(min(max(limit, 1), 500))


@router.post("/api/tuning/train")
def train_route(request: Request, body: TrainBody) -> dict:
    st = state(request)

    def job(progress, cancel):
        return train(st.knowledge, body.rounds, progress=progress, cancel=cancel)

    return st.jobs.submit("tuning-train", job).to_dict()


@router.post("/api/tuning/knowledge/reset")
def reset(request: Request, body: ResetBody) -> dict:
    if not body.confirm:
        raise HTTPException(400, "Set confirm=true to delete all tuning sessions, experiments and benchmarks")
    st = state(request)
    st.knowledge.reset()
    return {"ok": True}
