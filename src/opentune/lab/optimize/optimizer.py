"""Constrained operating-point optimisation on the steady-state engine model (SciPy).

For each engine speed the optimiser chooses manifold pressure, spark advance and
lambda to either maximise brake torque or minimise BSFC at a required torque,
subject to modelled constraints: knock margin, exhaust-gas temperature, turbo
speed, compressor choke and outlet temperature, peak cylinder pressure,
wastegate control reserve and a minimum lambda.

Validation gate — read this before using any result:

* The optimum is an optimum *of the synthetic model*. Its most important
  constraint (the knock limit) comes from an empirical assumption, not from
  measurements of any engine.
* Every result carries ``validation.status = "unvalidated"`` and
  ``ready_for_vehicle = False``. The candidate calibration is for simulation
  experiments only (load it, run the virtual dyno, compare).
* A Monte Carlo robustness check re-evaluates each optimum with perturbed
  assumption parameters and reports how often a constraint would be violated.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

import numpy as np

from opentune.lab.calibration.editor import CalibrationEditor
from opentune.lab.calibration.tables import Calibration
from opentune.lab.dyno.uncertainty import _perturb, _perturbable
from opentune.lab.physics.config import EngineConfig
from opentune.lab.physics.plant import Ambient, EnginePlant
from opentune.lab.physics.steady import SteadyPoint, steady_operating_point

OBJECTIVES = ("max_torque", "min_bsfc")


@dataclass
class OptimizationProblem:
    objective: str = "max_torque"
    rpm_points: list[float] = field(default_factory=lambda: [2000.0, 3000.0, 4000.0, 5000.0, 6000.0])
    map_bounds_kpa: tuple[float, float] = (100.0, 280.0)
    spark_bounds_deg: tuple[float, float] = (-5.0, 45.0)
    lambda_bounds: tuple[float, float] = (0.78, 1.0)
    knock_margin_deg: float = 2.0
    egt_margin_c: float = 25.0
    turbo_speed_margin: float = 0.05  # fraction below the limit
    peak_pressure_margin_bar: float = 10.0
    wastegate_reserve: float = 0.05  # keep at least this wastegate opening for boost control authority
    max_torque_nm: float | None = None  # optional cap (e.g. drivetrain)
    torque_target_fraction: float = 0.8  # min_bsfc: required torque as a fraction of the max-torque optimum
    ambient_temp_c: float = 25.0
    robustness_samples: int = 24
    seed: int = 11

    def validate(self, config: EngineConfig) -> list[str]:
        p = []
        if self.objective not in OBJECTIVES:
            p.append(f"Unknown objective {self.objective!r}")
        if not self.rpm_points or len(self.rpm_points) > 24:
            p.append("1–24 rpm points required")
        if any(not 1000.0 <= r <= config.limits.max_rpm for r in self.rpm_points):
            p.append("rpm points must lie between 1000 rpm and the engine's maximum")
        if not (0.0 <= self.knock_margin_deg <= 10.0 and 0.0 <= self.egt_margin_c <= 200.0):
            p.append("margins out of range")
        if not 0.3 <= self.torque_target_fraction <= 1.0:
            p.append("torque_target_fraction must be 0.3–1.0")
        if not 0 <= self.robustness_samples <= 500:
            p.append("robustness_samples must be 0–500")
        return p


def _constraints(cfg: EngineConfig, prob: OptimizationProblem, sp: SteadyPoint) -> dict[str, float]:
    """Constraint values g ≥ 0 means satisfied."""
    lim = cfg.limits
    g = {
        "knock_margin": sp.knock_limit - prob.knock_margin_deg - sp.spark,
        "egt": (lim.egt_limit_c - prob.egt_margin_c) - (sp.t_exhaust - 273.15),
        "turbo_speed": lim.turbo_speed_limit_rpm * (1 - prob.turbo_speed_margin) - sp.turbo_rpm,
        "peak_pressure": (lim.peak_pressure_limit_bar - prob.peak_pressure_margin_bar) - sp.peak_pressure / 1e5,
        "compressor_outlet": lim.compressor_outlet_limit_c - (sp.t_compressor_out - 273.15),
    }
    if cfg.turbo.enabled:
        g["wastegate_reserve"] = sp.wastegate_pos - prob.wastegate_reserve if sp.feasible else -1.0
        g["compressor_choke"] = 0.97 * cfg.turbo.compressor_flow_choke - sp.compressor_flow_param
    if prob.max_torque_nm is not None:
        g["torque_cap"] = prob.max_torque_nm - sp.brake_torque
    return g


# normalisation so SLSQP sees comparable magnitudes
_SCALE = {"knock_margin": 1.0, "egt": 10.0, "turbo_speed": 5000.0, "peak_pressure": 5.0, "compressor_outlet": 5.0,
          "wastegate_reserve": 0.05, "compressor_choke": 0.01, "torque_cap": 10.0, "torque_required": 10.0}


@dataclass
class PointResult:
    rpm: float
    success: bool
    message: str
    map_kpa: float
    spark_deg: float
    lambda_value: float
    torque_nm: float
    power_kw: float
    bsfc_g_kwh: float
    egt_c: float
    turbo_krpm: float
    peak_pressure_bar: float
    knock_limit_deg: float
    mbt_deg: float
    wastegate_pct: float
    constraints: dict[str, float]
    active_constraints: list[str]
    robustness: dict | None = None


def _evaluate(plant: EnginePlant, rpm: float, x: np.ndarray) -> SteadyPoint:
    return steady_operating_point(plant, rpm, float(x[0]) * 1000.0, lam=float(x[2]), spark=float(x[1]))


def _optimize_point(cfg: EngineConfig, plant: EnginePlant, prob: OptimizationProblem, rpm: float,
                    objective: str, torque_required: float | None = None) -> tuple[np.ndarray, bool, str]:
    from scipy.optimize import minimize

    lo = np.array([prob.map_bounds_kpa[0], prob.spark_bounds_deg[0], prob.lambda_bounds[0]])
    hi = np.array([prob.map_bounds_kpa[1], prob.spark_bounds_deg[1], prob.lambda_bounds[1]])
    if not cfg.turbo.enabled:
        hi[0] = min(hi[0], plant.ambient.pressure / 1000.0 * 0.98)
        lo[0] = min(lo[0], hi[0] - 5.0)
    scale = hi - lo
    cache: dict[tuple, SteadyPoint] = {}

    def point(z: np.ndarray) -> SteadyPoint:
        key = tuple(np.round(z, 9))
        if key not in cache:
            cache[key] = _evaluate(plant, rpm, lo + np.clip(z, 0, 1) * scale)
        return cache[key]

    def f(z):
        sp = point(z)
        if objective == "max_torque":
            return -sp.brake_torque / 100.0
        return sp.bsfc_g_kwh / 100.0 if sp.bsfc_g_kwh > 0 else 10.0

    cons = []
    names = list(_constraints(cfg, prob, point(np.full(3, 0.5))).keys())
    for name in names:
        cons.append({"type": "ineq", "fun": (lambda z, n=name: _constraints(cfg, prob, point(z))[n] / _SCALE[n])})
    if torque_required is not None:
        cons.append({"type": "ineq", "fun": lambda z: (point(z).brake_torque - torque_required) / 10.0})
    best = None
    starts = [np.array([0.3, 0.35, 0.6]), np.array([0.55, 0.3, 0.45]), np.array([0.15, 0.4, 0.9])]
    for z0 in starts:
        res = minimize(f, z0, method="SLSQP", bounds=[(0, 1)] * 3, constraints=cons,
                       options={"maxiter": 80, "ftol": 1e-6, "eps": 1e-4})
        sp = point(res.x)
        g = _constraints(cfg, prob, sp)
        feasible = all(v >= -1e-3 * _SCALE[k] for k, v in g.items()) and (
            torque_required is None or sp.brake_torque >= torque_required - 0.5)
        score = f(res.x)
        if feasible and (best is None or score < best[1]):
            best = (res.x, score, res.message)
    if best is None:
        return lo + np.clip(starts[0], 0, 1) * scale, False, "no feasible operating point found within bounds"
    return lo + np.clip(best[0], 0, 1) * scale, True, str(best[2])


def _point_result(cfg: EngineConfig, prob: OptimizationProblem, plant: EnginePlant, rpm: float, x: np.ndarray,
                  ok: bool, msg: str) -> PointResult:
    sp = _evaluate(plant, rpm, x)
    g = _constraints(cfg, prob, sp)
    active = [k for k, v in g.items() if abs(v) < 0.05 * _SCALE[k] * 4]
    for name, value, (lo, hi) in (("map", x[0], prob.map_bounds_kpa), ("spark", x[1], prob.spark_bounds_deg),
                                  ("lambda", x[2], prob.lambda_bounds)):
        if value <= lo + 0.005 * (hi - lo):
            active.append(f"bound:{name}_min")
        elif value >= hi - 0.005 * (hi - lo):
            active.append(f"bound:{name}_max")
    return PointResult(rpm, ok, msg, round(float(x[0]), 3), round(float(x[1]), 3), round(float(x[2]), 4),
                       round(sp.brake_torque, 2), round(sp.brake_power / 1000.0, 2), round(sp.bsfc_g_kwh, 1),
                       round(sp.t_exhaust - 273.15, 1), round(sp.turbo_rpm / 1000.0, 1),
                       round(sp.peak_pressure / 1e5, 1), round(sp.knock_limit, 2), round(sp.mbt, 2),
                       round(sp.wastegate_pos * 100.0, 1), {k: round(v, 4) for k, v in g.items()}, active)


def _robustness(config: EngineConfig, prob: OptimizationProblem, ambient: Ambient, results: list[PointResult]) -> None:
    if prob.robustness_samples <= 0:
        return
    rng = random.Random(prob.seed)
    params = _perturbable(config)
    counts = [dict.fromkeys(r.constraints, 0) for r in results]
    torque = [[] for _ in results]
    for _ in range(prob.robustness_samples):
        cfg = _perturb(config, params, rng)
        plant = EnginePlant(cfg, ambient)
        for i, r in enumerate(results):
            sp = _evaluate(plant, r.rpm, np.array([r.map_kpa, r.spark_deg, r.lambda_value]))
            for k, v in _constraints(cfg, prob, sp).items():
                if v < 0:
                    counts[i][k] += 1
            torque[i].append(sp.brake_torque)
    n = prob.robustness_samples
    for i, r in enumerate(results):
        viol = {k: round(c / n, 3) for k, c in counts[i].items() if c}
        t = np.asarray(torque[i])
        r.robustness = {"samples": n, "violation_probability": viol,
                        "any_violation_probability": round(max(viol.values()) if viol else 0.0, 3),
                        "torque_p05": round(float(np.percentile(t, 5)), 2),
                        "torque_p95": round(float(np.percentile(t, 95)), 2)}


@dataclass
class OptimizationResult:
    objective: str
    points: list[PointResult]
    validation: dict
    candidate_changes: list[dict]
    ready_for_vehicle: bool = False
    simulated: bool = True
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def optimize(config: EngineConfig, problem: OptimizationProblem, progress: Callable[[float, str], None] | None = None,
             cancel: Callable[[], bool] | None = None) -> OptimizationResult:
    problems = problem.validate(config)
    if problems:
        raise ValueError("; ".join(problems))
    ambient = Ambient(temperature=problem.ambient_temp_c + 273.15)
    plant = EnginePlant(config, ambient)
    rpms = sorted(problem.rpm_points)
    results: list[PointResult] = []
    for i, rpm in enumerate(rpms):
        if cancel is not None and cancel():
            raise RuntimeError("Optimisation cancelled")
        if progress:
            progress(0.05 + 0.75 * i / len(rpms), f"{rpm:.0f} rpm")
        if problem.objective == "max_torque":
            x, ok, msg = _optimize_point(config, plant, problem, rpm, "max_torque")
        else:
            xt, ok_t, _ = _optimize_point(config, plant, problem, rpm, "max_torque")
            required = problem.torque_target_fraction * _evaluate(plant, rpm, xt).brake_torque if ok_t else 0.0
            x, ok, msg = _optimize_point(config, plant, problem, rpm, "min_bsfc", required) if ok_t else \
                (xt, False, "no feasible max-torque point")
        results.append(_point_result(config, problem, plant, rpm, x, ok, msg))
    if progress:
        progress(0.85, "robustness")
    _robustness(config, problem, ambient, [r for r in results if r.success])
    knock_active = sum("knock_margin" in r.active_constraints for r in results)
    risky = [r.rpm for r in results if r.robustness and r.robustness["any_violation_probability"] > 0.1]
    validation = {
        "status": "unvalidated",
        "ready_for_vehicle": False,
        "summary": "Simulated optimum of a synthetic model. Not validated against any engine; not a calibration "
                   "for a vehicle.",
        "reasons": [
            "Model coefficients marked 'assumption' have no measured basis.",
            f"The knock-margin constraint (an empirical assumption) is active at {knock_active} of "
            f"{len(results)} speeds, so the result is dominated by an unvalidated sub-model."
            if knock_active else "Knock-margin constraint not active.",
            "No measured data (pressure traces, knock sensor data, dyno runs) was used.",
        ],
        "robustness_risk_rpm": risky,
        "required_before_any_real_use": [
            "Measured engine data to calibrate the knock, combustion and turbo sub-models",
            "Independent review of every limit used here",
            "Bench validation by qualified engineers with appropriate safety measures",
        ],
    }
    notes = ["Optimisation uses the steady-state solver: thermal transients, heat soak and control dynamics are "
             "not part of the objective. Check candidates on the virtual dyno."]
    bound_hits = sorted({a for r in results for a in r.active_constraints if a.startswith("bound:")})
    if bound_hits:
        notes.append("Search bounds limit the result (" + ", ".join(b[6:] for b in bound_hits) + "): the model's "
                     "optimum lies at or beyond a bound you chose, not at a physical constraint.")
    return OptimizationResult(problem.objective, results, validation, [], False, True, notes)


def candidate_calibration(calibration: Calibration, result: OptimizationResult, reserve_kpa: float | None = None
                          ) -> tuple[Calibration, list[dict]]:
    """Copy of ``calibration`` with torque-limit and boost-target changes that let the virtual ECU reach the optimum.

    For simulation only: the copy's name and metadata say so. Spark and lambda optima are reported, not applied.
    """
    cal = calibration.copy(f"{calibration.name} — optimiser candidate (SIMULATION ONLY)")
    cal.metadata = {**cal.metadata, "candidate": "optimizer", "ready_for_vehicle": False,
                    "validation": result.validation["status"]}
    ed = CalibrationEditor(cal)
    ok = [p for p in result.points if p.success]
    if not ok:
        return cal, []
    rpm = np.array([p.rpm for p in ok])
    tq = np.array([p.torque_nm for p in ok])
    mp = np.array([p.map_kpa for p in ok])
    reserve = reserve_kpa if reserve_kpa is not None else cal.scalar("boost_reserve_kpa")
    changes: list[dict] = []
    tl = cal.table("torque_limit")
    new_tl = tl.values.copy()
    for i, r in enumerate(tl.axes[0].values):
        if rpm[0] <= r <= rpm[-1]:
            v = float(np.interp(r, rpm, tq))
            v = min(max(v, tl.minimum), tl.maximum)
            if abs(v - new_tl[i]) > 0.5:
                changes.append({"table": "torque_limit", "index": [i], "rpm": r, "old": round(float(new_tl[i]), 2),
                                "new": round(v, 2)})
                new_tl[i] = v
    ed.replace_values("torque_limit", new_tl, "optimiser candidate: torque limit")
    bm = cal.table("boost_max")
    new_bm = bm.values.copy()
    last = len(bm.axes[1].values) - 1
    for i, r in enumerate(bm.axes[0].values):
        if rpm[0] <= r <= rpm[-1]:
            v = min(max(float(np.interp(r, rpm, mp)) + reserve + 5.0, bm.minimum), bm.maximum)
            if abs(v - new_bm[i, last]) > 0.5:
                changes.append({"table": "boost_max", "index": [i, last], "rpm": r, "pedal": bm.axes[1].values[last],
                                "old": round(float(new_bm[i, last]), 2), "new": round(v, 2)})
                new_bm[i, last] = v
    ed.replace_values("boost_max", new_bm, "optimiser candidate: boost target at full pedal")
    for p in ok:
        load = p.map_kpa  # informational comparison with the spark table at a comparable load
        changes.append({"table": "ign_base", "informational": True, "rpm": p.rpm,
                        "optimum_spark_deg": p.spark_deg, "optimum_lambda": p.lambda_value,
                        "note": f"not applied; compare with ign_base / lambda_target near {load:.0f} kPa"})
    result.candidate_changes = changes
    cal.metadata["candidate_changes"] = len([c for c in changes if not c.get("informational")])
    return cal, changes
