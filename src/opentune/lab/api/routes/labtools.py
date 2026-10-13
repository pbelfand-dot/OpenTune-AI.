"""Control lab, optimisation, AI analyst, Test Laboratory scenarios and sound synthesis."""

from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from opentune.analysis.local_model import LocalModelError
from opentune.lab.analysis import (
    AnalysisResult,
    analyze_calibration_diff,
    analyze_comparison,
    analyze_repeatability,
    analyze_run,
    answer_without_model,
    model_client,
    narrate,
)
from opentune.lab.api.common import state
from opentune.lab.api.routes.dyno import CalibrationSource, load_calibration
from opentune.lab.control import (
    ENGINE_EXPERIMENTS,
    GAIN_SCALARS,
    PLANT_KINDS,
    ControllerConfig,
    EngineExperiment,
    Experiment,
    PlantModel,
    fit_fopdt,
    run_engine_experiment,
    simc_tuning,
    simulate_loop,
)
from opentune.lab.optimize import OBJECTIVES, OptimizationProblem, candidate_calibration, optimize
from opentune.lab.optimize.optimizer import OptimizationResult, PointResult
from opentune.lab.scenarios import DISCLAIMER, SCENARIOS, run_scenarios
from opentune.lab.sound import presets_list, rev_trace, synthesize, to_wav

router = APIRouter(tags=["lab tools"])


# ---------------------------------------------------------------------------------------- control lab
class LoopBody(BaseModel):
    plant: dict[str, Any] = Field(default_factory=dict)
    controller: dict[str, Any] = Field(default_factory=dict)
    experiment: dict[str, Any] = Field(default_factory=dict)


class SimcBody(BaseModel):
    plant: dict[str, Any]
    tau_c: float | None = Field(None, gt=0, le=100)


class FitBody(BaseModel):
    t: list[float] = Field(min_length=10, max_length=20000)
    y: list[float] = Field(min_length=10, max_length=20000)
    u_step: float
    t_step: float


class EngineExpBody(BaseModel):
    project_id: str
    calibration: CalibrationSource = Field(default_factory=CalibrationSource)
    experiment: dict[str, Any] = Field(default_factory=dict)


def _dc(cls, data: dict):
    known = set(cls.__dataclass_fields__)
    bad = set(data) - known
    if bad:
        raise HTTPException(400, f"Unknown fields for {cls.__name__}: {sorted(bad)}")
    return cls(**data)


@router.get("/api/control/meta")
def control_meta() -> dict:
    return {"plant_kinds": list(PLANT_KINDS), "engine_experiments": list(ENGINE_EXPERIMENTS),
            "gain_scalars": list(GAIN_SCALARS), "defaults": {"plant": PlantModel().__dict__,
                                                             "controller": ControllerConfig().__dict__,
                                                             "experiment": Experiment().__dict__,
                                                             "engine": EngineExperiment().__dict__},
            "tuning_reference": "SIMC: S. Skogestad, J. Process Control 13 (2003) 291–309"}


@router.post("/api/control/loop")
def control_loop(body: LoopBody) -> dict:
    return simulate_loop(_dc(PlantModel, body.plant), _dc(ControllerConfig, body.controller),
                         _dc(Experiment, body.experiment)).to_dict()


@router.post("/api/control/simc")
def control_simc(body: SimcBody) -> dict:
    return simc_tuning(_dc(PlantModel, body.plant), body.tau_c)


@router.post("/api/control/fit")
def control_fit(body: FitBody) -> dict:
    if len(body.t) != len(body.y):
        raise HTTPException(400, "t and y must have the same length")
    return fit_fopdt(body.t, body.y, body.u_step, body.t_step)


@router.post("/api/control/engine")
def control_engine(request: Request, body: EngineExpBody) -> dict:
    st = state(request)
    project = st.library.get_project(body.project_id)
    cfg = project.engine_config()
    cal = load_calibration(request, body.project_id, body.calibration)
    exp = _dc(EngineExperiment, body.experiment)

    def job(progress, cancel):
        progress(0.1, exp.kind)
        res = run_engine_experiment(cfg, cal, exp)
        st.library.save_report(project.id, "control", f"Control: {exp.kind}", res)
        return res

    return st.jobs.submit("control", job, project.id).to_dict()


# ---------------------------------------------------------------------------------------- optimisation
class OptimizeBody(BaseModel):
    project_id: str
    problem: dict[str, Any] = Field(default_factory=dict)


class CandidateBody(BaseModel):
    report_id: str


@router.get("/api/optimize/meta")
def optimize_meta() -> dict:
    p = OptimizationProblem()
    return {"objectives": list(OBJECTIVES), "defaults": p.__dict__,
            "gate": "Results are always 'unvalidated' and never ready for a vehicle; candidates are for simulation."}


@router.post("/api/optimize")
def run_optimize(request: Request, body: OptimizeBody) -> dict:
    st = state(request)
    project = st.library.get_project(body.project_id)
    cfg = project.engine_config()
    data = dict(body.problem)
    for key in ("map_bounds_kpa", "spark_bounds_deg", "lambda_bounds"):
        if key in data:
            data[key] = tuple(data[key])
    problem = _dc(OptimizationProblem, data)
    problems = problem.validate(cfg)
    if problems:
        raise HTTPException(400, "; ".join(problems))

    def job(progress, cancel):
        res = optimize(cfg, problem, progress, cancel).to_dict()
        res["report_id"] = st.library.save_report(project.id, "optimization",
                                                  f"Optimisation ({problem.objective})", res)
        return res

    return st.jobs.submit("optimize", job, project.id).to_dict()


@router.post("/api/projects/{pid}/optimize/apply-candidate")
def apply_candidate(request: Request, pid: str, body: CandidateBody) -> dict:
    """Apply an optimiser candidate to the working copy (undoable) — for simulation experiments only."""
    st = state(request)
    rep = st.library.get_report(body.report_id)
    if rep["project_id"] != pid or rep["kind"] != "optimization":
        raise HTTPException(404, "Unknown optimisation report for this project")
    d = rep["data"]
    result = OptimizationResult(d["objective"], [PointResult(**{k: v for k, v in p.items()}) for p in d["points"]],
                                d["validation"], [], False, True, d.get("notes", []))
    editor = st.editor(pid)
    cand, changes = candidate_calibration(editor.calibration, result)
    for tid in ("torque_limit", "boost_max"):
        editor.replace_values(tid, cand.table(tid).values, f"{tid}: optimiser candidate (simulation only)")
    return {"changes": changes, "ready_for_vehicle": False,
            "note": "Applied to the working copy for simulation. Verify on the virtual dyno; undo to revert."}


# ---------------------------------------------------------------------------------------- AI analyst
class AiSettings(BaseModel):
    enabled: bool = False
    backend: Literal["ollama", "openai-compatible"] = "ollama"
    url: str = "http://127.0.0.1:11434"
    model: str = "llama3.2"
    timeout_s: float = Field(60.0, ge=1.0, le=600.0)


class AnalyzeBody(BaseModel):
    project_id: str
    run_ids: list[str] = Field(default_factory=list, max_length=8)
    include_calibration: bool = True
    use_model: bool = True


class AskBody(AnalyzeBody):
    question: str = Field(min_length=2, max_length=2000)


def _ai_path(request: Request):
    return state(request).root / "ai_settings.json"


def _ai_settings(request: Request) -> dict:
    path = _ai_path(request)
    if path.exists():
        try:
            return AiSettings(**json.loads(path.read_text())).model_dump()
        except (ValueError, TypeError):
            pass
    return AiSettings().model_dump()


@router.get("/api/ai/settings")
def get_ai_settings(request: Request) -> dict:
    return _ai_settings(request)


@router.put("/api/ai/settings")
def put_ai_settings(request: Request, body: AiSettings) -> dict:
    try:
        model_client({**body.model_dump(), "enabled": True})  # validates: local URLs only
    except LocalModelError as exc:
        raise HTTPException(400, str(exc)) from exc
    _ai_path(request).write_text(json.dumps(body.model_dump()))
    return body.model_dump()


@router.get("/api/ai/status")
def ai_status(request: Request) -> dict:
    cfg = _ai_settings(request)
    if not cfg["enabled"]:
        return {"enabled": False, "available": False, "detail": "Rule-based analysis only (no local model enabled)."}
    try:
        client = model_client({**cfg, "timeout_s": 2.0})
        models = client.list_models()
        return {"enabled": True, "available": True, "models": models,
                "detail": f"{cfg['backend']} at {cfg['url']} ({len(models)} models)"}
    except LocalModelError as exc:
        return {"enabled": True, "available": False, "detail": str(exc)}


def _findings(request: Request, body: AnalyzeBody):
    st = state(request)
    project = st.library.get_project(body.project_id)
    cfg = project.engine_config()
    runs = [st.library.get_run(r) for r in body.run_ids]
    findings = []
    for run in runs:
        findings += analyze_run(run, cfg)
        rep = run.metrics.get("repeatability") if isinstance(run.metrics, dict) else None
        if rep:
            findings += analyze_repeatability(rep)
    diff = None
    if body.include_calibration:
        base = st.library.load_calibration(project.id, "baseline")
        diff = base.diff(st.editor(project.id).calibration).to_dict()
        findings += analyze_calibration_diff(diff)
    if len(runs) >= 2:
        findings += analyze_comparison(runs[0], runs[1], diff)
    ctx = (f"Engine model: {cfg.name}. Runs: " + "; ".join(f"{r.label} ({r.kind}) peak {r.summary.get('peak_power_kw')}"
                                                            f" kW / {r.summary.get('peak_torque_nm')} N·m" for r in runs))
    return project, findings, ctx


@router.post("/api/ai/analyze")
def ai_analyze(request: Request, body: AnalyzeBody) -> dict:
    project, findings, ctx = _findings(request, body)
    client = model_client(_ai_settings(request)) if body.use_model else None
    text, status = narrate(findings, client, context=ctx)
    result = AnalysisResult(findings, text, "speculative", status).to_dict()
    result["report_id"] = state(request).library.save_report(project.id, "analysis", "AI analysis", result)
    return result


@router.post("/api/ai/ask")
def ai_ask(request: Request, body: AskBody) -> dict:
    _project, findings, ctx = _findings(request, body)
    client = model_client(_ai_settings(request)) if body.use_model else None
    text, status = narrate(findings, client, question=body.question, context=ctx)
    if text is None:
        return {"answer": answer_without_model(body.question, findings), "provenance": "rule-based",
                "model_status": status, "findings": [f.to_dict() for f in findings]}
    return {"answer": text, "provenance": "speculative", "model_status": status,
            "findings": [f.to_dict() for f in findings]}


# ---------------------------------------------------------------------------------------- scenarios
class ScenarioBody(BaseModel):
    project_id: str
    ids: list[str] = Field(default_factory=lambda: list(SCENARIOS))
    quick: bool = False
    calibration: CalibrationSource = Field(default_factory=CalibrationSource)


@router.get("/api/scenarios")
def scenarios() -> dict:
    from opentune.lab.scenarios import library

    return {"scenarios": [{"id": sid, "name": name, "doc": (fn.__doc__ or "").strip()}
                          for sid, (name, fn) in SCENARIOS.items()],
            "disclaimer": DISCLAIMER, "description": (library.__doc__ or "").strip()}


@router.post("/api/scenarios/run")
def run_scenarios_job(request: Request, body: ScenarioBody) -> dict:
    st = state(request)
    project = st.library.get_project(body.project_id)
    cfg = project.engine_config()
    cal = load_calibration(request, body.project_id, body.calibration)
    bad = [i for i in body.ids if i not in SCENARIOS]
    if bad or not body.ids:
        raise HTTPException(400, f"Unknown scenario ids {bad}")

    def job(progress, cancel):
        results = run_scenarios(cfg, cal, body.ids, body.quick, progress, cancel)
        out = []
        for r in results:
            for run in r.runs:
                run.metrics.pop("all_runs", None)
                st.library.save_run(project.id, run)
            d = r.to_dict()
            d["report_id"] = st.library.save_report(project.id, "scenario",
                                                    f"Scenario {r.id}: {r.name} — {'PASS' if r.passed else 'FAIL'}", d)
            out.append(d)
        return {"results": out, "passed": all(r["passed"] for r in out), "disclaimer": DISCLAIMER}

    return st.jobs.submit("scenarios", job, project.id).to_dict()


# ---------------------------------------------------------------------------------------- sound
@router.get("/api/sound/presets")
def sound_presets() -> dict:
    return {"presets": presets_list(), "note": "Illustrative synthesis: firing-order harmonics, load-dependent noise "
                                              "and a scaled turbo whistle. Not a recording or an acoustic model."}


def _wav(data: bytes) -> Response:
    return Response(content=data, media_type="audio/wav", headers={"Cache-Control": "no-store"})


@router.get("/api/dyno/runs/{rid}/sound.wav")
def run_sound(request: Request, rid: str, preset: str = "exhaust") -> Response:
    st = state(request)
    run = st.library.get_run(rid)
    pid = st.library.run_project(rid)
    cyl = int(st.library.get_project(pid).config["geometry"]["cylinders"])
    pts = run.samples or []
    if len(pts) < 2:
        pts = [{"t": i * 0.1, "rpm": p["rpm"], "torque_nm": p.get("torque_nm") or 0, "turbo_krpm":
                p.get("turbo_krpm") or 0} for i, p in enumerate(run.curve)]
    if len(pts) < 2:
        raise HTTPException(400, "Run has no speed trace")
    peak = max(abs(p.get("torque_nm") or 0) for p in pts) or 1.0
    t = [p["t"] - pts[0]["t"] for p in pts]
    audio = synthesize(t, [p["rpm"] for p in pts], [max(0.0, (p.get("torque_nm") or 0) / peak) for p in pts],
                       [(p.get("turbo_krpm") or 0) * 1000 for p in pts], cyl, preset)
    return _wav(to_wav(audio))


@router.get("/api/sound/rev.wav")
def rev_sound(cylinders: int = 6, preset: str = "exhaust", redline: float = 6500.0) -> Response:
    if cylinders not in (3, 4, 5, 6, 8):
        raise HTTPException(400, "cylinders must be 3, 4, 5, 6 or 8")
    t, rpm, load, turbo = rev_trace(redline=min(max(redline, 3000.0), 9000.0))
    return _wav(to_wav(synthesize(t, rpm, load, turbo, cylinders, preset)))
