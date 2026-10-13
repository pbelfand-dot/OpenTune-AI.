"""Model-uncertainty estimate for dyno results (Monte Carlo over assumption parameters).

Most model coefficients are engineering assumptions. This module asks: *if
those assumptions were somewhat different, how different would the torque be
at the operating points the ECU actually ran?* For each sample it perturbs the
assumption parameters (documented parameters are never perturbed), then
re-solves the steady-state engine at every selected point of the run with the
same manifold pressure, lambda, spark and cam targets.

What the band means — and what it does not:

* It is a sensitivity band for the synthetic model, not a confidence interval
  for any real engine. The spreads below are themselves assumptions.
* The ECU's response is held fixed (same boost, spark and lambda). A real ECU
  would react, e.g. with knock retard or boost limiting.
* Points where the perturbed turbo could not reach the run's boost are counted
  and reported (``infeasible_fraction``).
* A virtual torque-transducer error (assumption: 0.5 % of reading, 1 σ) is
  combined in quadrature so the band never shrinks to zero.
"""

from __future__ import annotations

import math
import random
from dataclasses import asdict, dataclass

from opentune.lab.dyno.runs import DynoRun
from opentune.lab.physics.config import EngineConfig
from opentune.lab.physics.plant import Ambient, EnginePlant
from opentune.lab.physics.steady import steady_operating_point
from opentune.lab.provenance import Provenance


@dataclass(frozen=True)
class UncertainParameter:
    path: str
    sigma: float  # 1-σ, relative (fraction) unless ``absolute``
    absolute: bool = False
    description: str = ""


UNCERTAIN_PARAMETERS: tuple[UncertainParameter, ...] = (
    UncertainParameter("combustion.eta_ig_ch", 0.02, description="gross indicated efficiency factor"),
    UncertainParameter("combustion.spark_efficiency_coeff", 0.15, description="spark-timing loss curvature"),
    UncertainParameter("combustion.knock_base_deg", 1.5, True, "knock-limited advance (only used for limits)"),
    UncertainParameter("valvetrain.ve_peak", 0.025, description="peak volumetric efficiency"),
    UncertainParameter("valvetrain.ve_curvature", 0.15, description="VE fall-off away from the peak"),
    UncertainParameter("friction.fmep_a", 0.10, description="friction MEP, constant term"),
    UncertainParameter("friction.fmep_b", 0.10, description="friction MEP, linear term"),
    UncertainParameter("friction.fmep_c", 0.10, description="friction MEP, quadratic term"),
    UncertainParameter("turbo.compressor_eta_max", 0.03, description="peak compressor efficiency"),
    UncertainParameter("turbo.turbine_eta_max", 0.04, description="peak turbine efficiency"),
    UncertainParameter("intake.intercooler_effectiveness", 0.05, description="charge-cooler effectiveness"),
    UncertainParameter("exhaust.backpressure_coeff", 0.15, description="post-turbine back-pressure"),
    UncertainParameter("thermal.wall_heat_fraction", 0.08, description="in-cylinder wall heat loss"),
    UncertainParameter("combustion.dilution_coeff", 0.25, description="residual-gas efficiency penalty"),
)

TRANSDUCER_SIGMA = 0.005  # relative, virtual load-cell (assumption)
Z95 = 1.959964


@dataclass
class UncertaintyResult:
    samples: int
    seed: int
    rpm: list[float]
    torque_nominal: list[float]
    torque_p05: list[float]
    torque_p50: list[float]
    torque_p95: list[float]
    power_p05_kw: list[float]
    power_p95_kw: list[float]
    peak_torque_p05: float
    peak_torque_p50: float
    peak_torque_p95: float
    peak_power_p05_kw: float
    peak_power_p50_kw: float
    peak_power_p95_kw: float
    infeasible_fraction: float
    model_vs_run_pct: float  # steady model at nominal parameters vs the dynamic run (transient effects)
    parameters: list[dict]
    sensitivity: list[dict]
    notes: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


def _percentile(values: list[float], q: float) -> float:
    s = sorted(values)
    if not s:
        return float("nan")
    k = (len(s) - 1) * q
    lo = math.floor(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _perturbable(config: EngineConfig) -> list[UncertainParameter]:
    return [p for p in UNCERTAIN_PARAMETERS if config.provenance_of(p.path) != Provenance.DOCUMENTED]


def _perturb(config: EngineConfig, params: list[UncertainParameter], rng: random.Random) -> EngineConfig:
    cfg = config.copy()
    for p in params:
        base = cfg.get(p.path)
        z = max(-3.0, min(3.0, rng.gauss(0.0, 1.0)))
        value = base + z * p.sigma if p.absolute else base * (1.0 + z * p.sigma)
        cfg.set(p.path, value)
    return cfg


def _points(run: DynoRun, max_points: int) -> list[dict]:
    pts = [p for p in run.curve if p.get("map_kpa") and p.get("lambda") and p.get("torque_nm", 0) > 20]
    if len(pts) <= max_points:
        return pts
    step = (len(pts) - 1) / (max_points - 1)
    picked = [pts[round(i * step)] for i in range(max_points)]
    # always include the measured peaks
    for key in ("torque_nm", "power_kw"):
        best = max(pts, key=lambda p, key=key: p[key])
        if best not in picked:
            picked.append(best)
    return sorted(picked, key=lambda p: p["rpm"])


def _evaluate(cfg: EngineConfig, ambient: Ambient, pts: list[dict]) -> tuple[list[float], int]:
    plant = EnginePlant(cfg, ambient)
    torques, infeasible = [], 0
    for p in pts:
        sp = steady_operating_point(
            plant, p["rpm"], p["map_kpa"] * 1000.0, lam=min(max(p["lambda"], 0.7), 1.2), spark=p["spark_deg"],
            t_coolant=p["coolant_c"] + 273.15, t_oil=p["oil_c"] + 273.15,
            t_charge_loop=p["charge_loop_c"] + 273.15, intake_cam=p["intake_cam_deg"],
            exhaust_cam=p["exhaust_cam_deg"])
        if not sp.feasible:
            infeasible += 1
        torques.append(sp.brake_torque)
    return torques, infeasible


def estimate_uncertainty(config: EngineConfig, run: DynoRun, samples: int = 64, seed: int = 7,
                         max_points: int = 14, sensitivity: bool = True) -> UncertaintyResult:
    """Monte Carlo model-uncertainty band for a sweep/steady run (see module docstring)."""
    if run.kind not in ("sweep", "partial", "repeat", "steady"):
        raise ValueError("Uncertainty estimates need a sweep or steady run")
    if not 8 <= samples <= 2000:
        raise ValueError("samples must be 8–2000")
    test = run.test
    ambient = Ambient(pressure=test["ambient_pressure_kpa"] * 1000.0, temperature=test["ambient_temp_c"] + 273.15)
    pts = _points(run, max_points)
    if not pts:
        raise ValueError("Run has no loaded operating points")
    params = _perturbable(config)
    rng = random.Random(seed)
    nominal, _ = _evaluate(config, ambient, pts)
    runs: list[list[float]] = []
    infeasible = 0
    for _ in range(samples):
        tq, bad = _evaluate(_perturb(config, params, rng), ambient, pts)
        # virtual transducer error, fully correlated across one run
        tq = [t * (1.0 + rng.gauss(0.0, TRANSDUCER_SIGMA)) for t in tq]
        runs.append(tq)
        infeasible += bad
    rpm = [p["rpm"] for p in pts]
    w = [r * 2.0 * math.pi / 60.0 / 1000.0 for r in rpm]
    cols = list(zip(*runs, strict=False))
    p05 = [_percentile(list(c), 0.05) for c in cols]
    p50 = [_percentile(list(c), 0.50) for c in cols]
    p95 = [_percentile(list(c), 0.95) for c in cols]
    peak_t = [max(r) for r in runs]
    peak_p = [max(t * wi for t, wi in zip(r, w, strict=False)) for r in runs]
    run_tq = [p["torque_nm"] for p in pts]
    model_vs_run = (sum(nominal) - sum(run_tq)) / sum(run_tq) * 100.0

    sens: list[dict] = []
    if sensitivity:
        # one-at-a-time ±1σ on peak power: which assumption matters most here?
        base_peak = max(t * wi for t, wi in zip(nominal, w, strict=False))
        for p in params:
            effects = []
            for sign in (-1.0, 1.0):
                cfg = config.copy()
                v = cfg.get(p.path)
                cfg.set(p.path, v + sign * p.sigma if p.absolute else v * (1.0 + sign * p.sigma))
                tq, _ = _evaluate(cfg, ambient, pts)
                effects.append(max(t * wi for t, wi in zip(tq, w, strict=False)) - base_peak)
            sens.append({"path": p.path, "description": p.description, "sigma": p.sigma, "absolute": p.absolute,
                         "peak_power_minus_kw": round(effects[0], 3), "peak_power_plus_kw": round(effects[1], 3),
                         "swing_kw": round(abs(effects[1] - effects[0]), 3)})
        sens.sort(key=lambda s: -s["swing_kw"])

    notes = [
        "Sensitivity band of the synthetic model, not a confidence interval for a real engine.",
        "ECU response held fixed at the run's boost, lambda, spark and cam targets.",
        f"Virtual torque transducer error {TRANSDUCER_SIGMA * 100:.1f} % (1 σ, assumption) included.",
        "Parameter spreads are assumptions; documented parameters are not perturbed.",
    ]
    if abs(model_vs_run) > 3.0:
        notes.append(f"Steady model differs from the dynamic run by {model_vs_run:+.1f} % (sweep-rate / thermal "
                     "transient effects); the band is centred on the steady model.")
    return UncertaintyResult(
        samples=samples, seed=seed, rpm=rpm, torque_nominal=[round(v, 3) for v in nominal],
        torque_p05=[round(v, 3) for v in p05], torque_p50=[round(v, 3) for v in p50],
        torque_p95=[round(v, 3) for v in p95],
        power_p05_kw=[round(t * wi, 3) for t, wi in zip(p05, w, strict=False)],
        power_p95_kw=[round(t * wi, 3) for t, wi in zip(p95, w, strict=False)],
        peak_torque_p05=round(_percentile(peak_t, 0.05), 3), peak_torque_p50=round(_percentile(peak_t, 0.5), 3),
        peak_torque_p95=round(_percentile(peak_t, 0.95), 3),
        peak_power_p05_kw=round(_percentile(peak_p, 0.05), 3), peak_power_p50_kw=round(_percentile(peak_p, 0.5), 3),
        peak_power_p95_kw=round(_percentile(peak_p, 0.95), 3),
        infeasible_fraction=round(infeasible / (samples * len(pts)), 4), model_vs_run_pct=round(model_vs_run, 3),
        parameters=[asdict(p) for p in params], sensitivity=sens, notes=notes,
    )
