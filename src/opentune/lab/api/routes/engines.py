"""Engine presets, parameter schema, validation and quick steady-state previews."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from opentune.lab.api.common import state
from opentune.lab.physics.config import EngineConfig, param_schema
from opentune.lab.physics.plant import Ambient, EnginePlant
from opentune.lab.physics.presets import PRESETS, get_preset
from opentune.lab.physics.steady import steady_operating_point
from opentune.lab.provenance import Provenance

router = APIRouter(prefix="/api/engines", tags=["engines"])


class ConfigBody(BaseModel):
    config: dict[str, Any] | None = None
    project_id: str | None = None


class PreviewBody(ConfigBody):
    boost_kpa: float = Field(200.0, ge=80.0, le=320.0)
    lambda_value: float = Field(0.88, ge=0.7, le=1.1)
    rpm_step: float = Field(250.0, ge=100.0, le=1000.0)
    ambient_temp_c: float = Field(25.0, ge=-30.0, le=50.0)


def resolve_config(request: Request, body: ConfigBody) -> EngineConfig:
    if body.config is not None:
        return EngineConfig.from_dict(body.config)
    if body.project_id:
        return state(request).library.get_project(body.project_id).engine_config()
    raise HTTPException(400, "Provide either config or project_id")


@router.get("/presets")
def presets() -> list[dict]:
    out = []
    for pid, factory in PRESETS.items():
        cfg = factory()
        out.append({"id": pid, "name": cfg.name, "description": cfg.description,
                    "reference_ratings": cfg.reference_ratings,
                    "documented_parameters": sorted(k for k, v in cfg.provenance.items()
                                                    if v.get("provenance") == Provenance.DOCUMENTED.value)})
    return out


@router.get("/presets/{preset_id}")
def preset(preset_id: str) -> dict:
    return get_preset(preset_id).to_dict()


@router.get("/schema")
def schema() -> list[dict]:
    return param_schema()


@router.get("/provenance")
def provenance_kinds() -> list[dict]:
    return [{"id": p.value, "label": p.label} for p in Provenance]


@router.post("/validate")
def validate(request: Request, body: ConfigBody) -> dict:
    cfg = resolve_config(request, body)
    problems = cfg.validate()
    return {"valid": not problems, "problems": problems, "displacement_l": round(cfg.displacement * 1000.0, 4)}


@router.post("/preview")
def preview(request: Request, body: PreviewBody) -> dict:
    """Open-loop steady-state full-load curve at a fixed manifold-pressure target (no calibration)."""
    cfg = resolve_config(request, body)
    problems = cfg.validate()
    if problems:
        raise HTTPException(400, "; ".join(problems[:5]))
    plant = EnginePlant(cfg, Ambient(temperature=body.ambient_temp_c + 273.15))
    rows = []
    rpm = 1000.0
    while rpm <= cfg.limits.max_rpm + 1e-6:
        target = body.boost_kpa * 1000.0 if cfg.turbo.enabled else plant.ambient.pressure * 0.97
        sp = steady_operating_point(plant, rpm, target, lam=body.lambda_value)
        rows.append({"rpm": rpm, "torque_nm": round(sp.brake_torque, 2), "power_kw": round(sp.brake_power / 1000, 2),
                     "feasible": sp.feasible, "reason": sp.reason, "egt_c": round(sp.t_exhaust - 273.15, 1),
                     "turbo_krpm": round(sp.turbo_rpm / 1000, 1), "wastegate_pct": round(sp.wastegate_pos * 100, 1),
                     "spark_deg": round(sp.spark, 2), "mbt_deg": round(sp.mbt, 2),
                     "knock_limit_deg": round(sp.knock_limit, 2), "bsfc_g_kwh": round(sp.bsfc_g_kwh, 1),
                     "ve_pct": round(sp.volumetric_efficiency * 100, 2), "compressor_pr": round(sp.compressor_pr, 3),
                     "compressor_eta_pct": round(sp.compressor_eta * 100, 2),
                     "exhaust_pressure_kpa": round(sp.p_em / 1000, 2),
                     "peak_pressure_bar": round(sp.peak_pressure / 1e5, 1)})
        rpm += body.rpm_step
    return {"points": rows, "boost_kpa": body.boost_kpa, "lambda": body.lambda_value,
            "note": "Steady-state model at a fixed manifold-pressure target with spark at min(MBT, knock limit − "
                    "1.5°). Infeasible points: the turbo cannot reach the target. Simulated; not a prediction "
                    "for a real engine."}
