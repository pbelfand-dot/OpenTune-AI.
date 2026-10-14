"""Virtual dyno jobs, stored runs, comparisons, exports and uncertainty."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from opentune.lab.api.common import state
from opentune.lab.dyno import (
    DRIVETRAIN_EXAMPLES,
    STANDARDS,
    TEST_KINDS,
    DynoTest,
    channel_list,
    compare_runs,
    curve_to_csv,
    estimate_uncertainty,
    figure_spec,
    run_dyno,
    samples_to_csv,
)

router = APIRouter(tags=["dyno"])


class CalibrationSource(BaseModel):
    source: Literal["working", "active", "baseline", "flashed", "version"] = "working"
    version_id: str | None = None


class DynoJobBody(BaseModel):
    project_id: str
    test: dict[str, Any] = Field(default_factory=dict)
    calibration: CalibrationSource = Field(default_factory=CalibrationSource)
    uncertainty_samples: int = Field(0, ge=0, le=500)


class CompareBody(BaseModel):
    run_ids: list[str] = Field(min_length=2, max_length=8)
    baseline: int = 0


class FigureBody(BaseModel):
    run_ids: list[str] = Field(min_length=1, max_length=8)
    left: str = "torque_nm"
    right: str | None = "power_kw"
    band: bool = False


class UncertaintyBody(BaseModel):
    samples: int = Field(64, ge=8, le=500)
    seed: int = 7


def load_calibration(request: Request, project_id: str, src: CalibrationSource):
    st = state(request)
    if src.source == "working":
        return st.editor(project_id).calibration.copy()
    return st.library.load_calibration(project_id, src.source, src.version_id)


def describe_source(request: Request, project_id: str, src: CalibrationSource, cal) -> str:
    """Short human label such as 'v3 (flashed)' or 'working copy of v2 (edited)'."""
    st = state(request)
    if src.source == "working":
        base = st.library.calibrations.info(st.editor_version(project_id))
        return f"working copy of v{base.version}" + (" (edited)" if st.is_dirty(project_id) else "")
    vid = st.version_for_hash(project_id, cal.content_hash())
    info = st.library.calibrations.info(vid) if vid else None
    return (f"v{info.version}" if info else "unsaved") + f" ({src.source})"


@router.get("/api/dyno/meta")
def meta() -> dict:
    return {"kinds": list(TEST_KINDS), "channels": channel_list(),
            "corrections": [{"id": s.id, "name": s.name, "formula": s.formula, "reference": s.reference,
                             "valid_range": s.valid_range, "source_note": s.source_note} for s in STANDARDS.values()],
            "drivetrains": [d.to_dict() for d in DRIVETRAIN_EXAMPLES.values()],
            "defaults": DynoTest().to_dict()}


@router.post("/api/dyno/run")
def start_run(request: Request, body: DynoJobBody) -> dict:
    st = state(request)
    project = st.library.get_project(body.project_id)
    cfg = project.engine_config()
    cal = load_calibration(request, body.project_id, body.calibration)
    test = DynoTest.from_dict(body.test)
    problems = test.validate(cfg)
    if problems:
        raise HTTPException(400, "; ".join(problems))
    source = body.calibration.source
    if not test.label:
        test.label = f"{test.kind} — {describe_source(request, body.project_id, body.calibration, cal)}"

    def job(progress, cancel):
        result = run_dyno(cfg, cal, test, progress, cancel)
        for run in result.runs:
            run.test["calibration_source"] = source
            if body.uncertainty_samples and run.kind in ("sweep", "partial", "steady"):
                progress(0.97, "uncertainty")
                run.uncertainty = estimate_uncertainty(cfg, run, samples=body.uncertainty_samples).to_dict()
            st.library.save_run(project.id, run)
        return {"run_ids": [r.id for r in result.runs], "summaries": [r.summary for r in result.runs],
                "repeatability": result.repeatability, "metrics": [r.metrics for r in result.runs]}

    return st.jobs.submit("dyno", job, project.id).to_dict()


@router.get("/api/dyno/runs")
def list_runs(request: Request, project_id: str | None = None) -> list[dict]:
    return state(request).library.list_runs(project_id)


@router.get("/api/dyno/runs/{rid}")
def get_run(request: Request, rid: str, samples: bool = False) -> dict:
    return state(request).library.get_run(rid).to_dict(include_samples=samples)


@router.delete("/api/dyno/runs/{rid}")
def delete_run(request: Request, rid: str) -> dict:
    state(request).library.delete_run(rid)
    return {"deleted": rid}


@router.post("/api/dyno/compare")
def compare(request: Request, body: CompareBody) -> dict:
    lib = state(request).library
    return compare_runs([lib.get_run(r) for r in body.run_ids], baseline=body.baseline)


@router.post("/api/dyno/figure")
def figure(request: Request, body: FigureBody) -> dict:
    lib = state(request).library
    runs = [lib.get_run(r) for r in body.run_ids]
    band = runs[0].uncertainty if body.band else None
    return figure_spec(runs, body.left, body.right, band)


@router.get("/api/dyno/runs/{rid}/curve.csv", response_class=PlainTextResponse)
def curve_csv(request: Request, rid: str) -> PlainTextResponse:
    run = state(request).library.get_run(rid)
    return PlainTextResponse(curve_to_csv(run), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="dyno_{rid}_curve.csv"'})


@router.get("/api/dyno/runs/{rid}/samples.csv", response_class=PlainTextResponse)
def samples_csv(request: Request, rid: str) -> PlainTextResponse:
    run = state(request).library.get_run(rid)
    return PlainTextResponse(samples_to_csv(run), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="dyno_{rid}_samples.csv"'})


@router.post("/api/dyno/runs/{rid}/uncertainty")
def run_uncertainty(request: Request, rid: str, body: UncertaintyBody) -> dict:
    st = state(request)
    run = st.library.get_run(rid)
    pid = st.library.run_project(rid)
    cfg = st.library.get_project(pid).engine_config()

    def job(progress, cancel):
        progress(0.05, "Monte Carlo")
        result = estimate_uncertainty(cfg, run, samples=body.samples, seed=body.seed)
        run.uncertainty = result.to_dict()
        st.library.save_run(pid, run)
        return run.uncertainty

    return st.jobs.submit("uncertainty", job, pid).to_dict()


# ------------------------------------------------------------------ jobs
@router.get("/api/jobs")
def jobs(request: Request, project_id: str | None = None) -> list[dict]:
    return [j.to_dict(include_result=False) for j in state(request).jobs.list(project_id)]


@router.get("/api/jobs/{jid}")
def job(request: Request, jid: str) -> dict:
    return state(request).jobs.get(jid).to_dict()


@router.delete("/api/jobs/{jid}")
def cancel_job(request: Request, jid: str) -> dict:
    return state(request).jobs.cancel(jid).to_dict(include_result=False)
