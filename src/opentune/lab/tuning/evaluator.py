"""Tuning evaluator: a standard test battery that turns a calibration into a scorecard.

This is the "exam" every candidate calibration sits before the tuning engine may
accept it. The battery runs the full closed-loop simulation (engine model +
virtual ECU) under several conditions:

* ``nominal``      full-load sweep at 25 °C (performance metrics)
* ``hot``          full-load sweep at 35 °C ambient with reduced cooling airflow
* ``low_octane``   full-load sweep on fuel 2 AKI below the preset's
* corner engines   the same sweep on *perturbed engine models* (``knock_prone``,
                   ``hot_exhaust``, ``weak_turbo``): the calibration must hold up if
                   the model's assumptions are somewhat wrong
* ``part_load``    steady points at fixed torque for fuel efficiency (BSFC)
* ``tip_in``       pedal step for drivability (torque rise time, overshoot)

The scorecard reports performance, efficiency and drivability metrics from the
nominal condition, and **mechanical/thermal stress** as the worst case over all
conditions: peak cylinder pressure, crank torque (drivetrain/connecting-rod
load), turbo speed, exhaust-gas, compressor-outlet, charge-air and coolant
temperatures, knock-control activity and minimum lambda. Each stress is compared
with a limit (``StressLimits``) to give a margin; exceeding a limit is a
violation.

Everything here is simulated. A clean scorecard means "within the modelled limits
of a synthetic model", never "safe for a real engine".
"""

from __future__ import annotations

import math
import os
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import asdict, dataclass

from opentune.lab.calibration.tables import Calibration
from opentune.lab.dyno.runs import DynoTest, run_dyno
from opentune.lab.physics.config import EngineConfig

FIDELITIES = ("quick", "standard", "thorough")

# Pessimistic model-error "corners". Magnitudes are about 1–1.5 σ of the assumption spreads used by
# the dyno uncertainty estimate (dyno/uncertainty.py). They are assumptions about how wrong the model
# might be, chosen to stress knock, exhaust temperature and the turbocharger respectively.
CORNERS: dict[str, dict] = {
    "knock_prone": {"description": "knock limit 2° earlier and more sensitive to charge temperature",
                    "changes": {"combustion.knock_base_deg": ("add", -2.0),
                                "combustion.knock_iat_coeff": ("mul", 1.25)}},
    "hot_exhaust": {"description": "10 % less wall heat loss and 8 % less charge-cooler effectiveness",
                    "changes": {"thermal.wall_heat_fraction": ("mul", 0.90),
                                "intake.intercooler_effectiveness": ("mul", 0.92)}},
    "weak_turbo": {"description": "compressor efficiency −3 points, turbine efficiency −4 points",
                   "changes": {"turbo.compressor_eta_max": ("add", -0.03),
                               "turbo.turbine_eta_max": ("add", -0.04)}},
}


def corner_config(config: EngineConfig, corner: str) -> EngineConfig:
    cfg = config.copy()
    for path, (op, value) in CORNERS[corner]["changes"].items():
        cur = cfg.get(path)
        cfg.set(path, cur + value if op == "add" else cur * value)
    return cfg


@dataclass
class StressLimits:
    """Limits the tuning engine must respect, with the margin it must keep below each.

    Defaults come from the engine model's ``limits`` (assumptions, not OEM values) plus a crank-torque
    limit standing in for drivetrain and connecting-rod strength (default: 130 % of the published
    rated torque — an assumption; set it from real component ratings when known).
    """

    max_peak_pressure_bar: float = 140.0
    max_torque_nm: float = 650.0
    max_turbo_krpm: float = 230.0
    max_egt_c: float = 950.0
    max_compressor_out_c: float = 190.0
    # Charge-air temperature is an *indicator* by default (no limit): its real risk is knock, which the
    # hot / knock-prone conditions test directly; the hardware temperature limit that matters for the
    # charge-air path is the compressor outlet. Set a value to make it a hard limit.
    max_iat_c: float | None = None
    max_coolant_c: float = 115.0
    max_knock_retard_nominal_deg: float = 1.0  # knock control should be quiet in normal conditions
    max_knock_retard_adverse_deg: float = 4.0  # some knock control is expected when hot / bad fuel
    min_lambda: float = 0.76
    required_margin_pct: float = 3.0  # every stress must stay this far below its limit to be accepted
    provenance: str = "assumption"

    @classmethod
    def from_config(cls, config: EngineConfig) -> StressLimits:
        lim = config.limits
        ref = config.reference_ratings.get("torque_nm")
        return cls(max_peak_pressure_bar=lim.peak_pressure_limit_bar,
                   max_torque_nm=round(1.3 * ref, 1) if ref else 650.0,
                   max_turbo_krpm=lim.turbo_speed_limit_rpm / 1000.0, max_egt_c=lim.egt_limit_c,
                   max_compressor_out_c=lim.compressor_outlet_limit_c, max_coolant_c=lim.coolant_limit_c)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> StressLimits:
        known = set(cls.__dataclass_fields__)
        return cls(**{k: (float(v) if v is not None else None) if k != "provenance" else v
                      for k, v in d.items() if k in known})

    def active(self) -> dict[str, tuple[str, str]]:
        """Stress keys that have a limit (the others are reported as indicators)."""
        return {k: v for k, v in STRESS_KEYS.items() if getattr(self, v[0]) is not None}


# stress metric -> (limit attribute, direction): "max" means value must stay below the limit
STRESS_KEYS: dict[str, tuple[str, str]] = {
    "peak_pressure_bar": ("max_peak_pressure_bar", "max"),
    "torque_nm": ("max_torque_nm", "max"),
    "turbo_krpm": ("max_turbo_krpm", "max"),
    "egt_c": ("max_egt_c", "max"),
    "compressor_out_c": ("max_compressor_out_c", "max"),
    "iat_c": ("max_iat_c", "max"),
    "coolant_c": ("max_coolant_c", "max"),
    "min_lambda": ("min_lambda", "min"),
}


@dataclass
class BatteryConfig:
    fidelity: str = "quick"
    rpm_start: float = 2000.0
    rpm_end_offset: float = 500.0  # sweep ends at max_rpm − offset
    ramp_rpm_s: float = 600.0
    conditions: tuple[str, ...] = ("nominal", "hot_knock_prone")
    # part-load points as (rpm, fraction of the reference torque): efficiency is compared at equal
    # *torque*, not equal pedal (a higher torque limit would otherwise look like "better BSFC")
    part_load_points: tuple[tuple[float, float], ...] = ((2000.0, 0.25), (2500.0, 0.30), (3000.0, 0.40))
    tip_in: bool = False
    workers: int = 0  # 0 = automatic (up to 4 processes); 1 = sequential

    @classmethod
    def preset(cls, fidelity: str) -> BatteryConfig:
        if fidelity == "quick":
            return cls("quick")
        if fidelity == "standard":
            return cls("standard", 1500.0, 400.0, 400.0,
                       ("nominal", "hot", "low_octane", "knock_prone", "weak_turbo"),
                       ((2000.0, 0.25), (2500.0, 0.30), (3000.0, 0.40), (3500.0, 0.50)), True)
        if fidelity == "thorough":
            return cls("thorough", 1500.0, 300.0, 300.0,
                       ("nominal", "hot", "low_octane", "knock_prone", "hot_exhaust", "weak_turbo",
                        "hot_knock_prone"),
                       ((1500.0, 0.20), (2000.0, 0.25), (2500.0, 0.30), (3000.0, 0.40), (3500.0, 0.50)), True)
        raise ValueError(f"Unknown fidelity {fidelity!r}. Options: {', '.join(FIDELITIES)}")


CONDITION_INFO = {
    "nominal": "25 °C, full cooling airflow, preset fuel",
    "hot": "35 °C ambient, 60 % cooling airflow",
    "low_octane": "fuel 2 AKI below the preset's",
    "knock_prone": CORNERS["knock_prone"]["description"],
    "hot_exhaust": CORNERS["hot_exhaust"]["description"],
    "weak_turbo": CORNERS["weak_turbo"]["description"],
    "hot_knock_prone": "35 °C ambient, 60 % airflow, on the knock-prone engine variant",
}
ADVERSE = {"hot", "low_octane", "knock_prone", "hot_exhaust", "weak_turbo", "hot_knock_prone"}


def _condition_setup(config: EngineConfig, condition: str) -> tuple[EngineConfig, dict]:
    cfg = config
    test_kw: dict = {}
    if condition in ("hot", "hot_knock_prone"):
        test_kw.update(ambient_temp_c=35.0, fan_airflow=0.6)
    if condition == "low_octane":
        cfg = config.copy()
        cfg.combustion.octane_aki -= 2.0
    if condition in CORNERS:
        cfg = corner_config(config, condition)
    if condition == "hot_knock_prone":
        cfg = corner_config(config, "knock_prone")
    return cfg, test_kw


def reference_torque(config: EngineConfig) -> float:
    """Published rated torque if the preset has one, else 100 N·m per litre (assumption)."""
    ref = config.reference_ratings.get("torque_nm")
    return float(ref) if ref else 100.0 * config.displacement * 1000.0


def pedal_for_torque(calibration: Calibration, rpm: float, torque_nm: float) -> float:
    """Pedal position at which this calibration's driver demand equals ``torque_nm`` (bisection)."""
    cap = calibration.lookup("torque_limit", rpm)
    want = min(100.0, max(0.0, torque_nm / cap * 100.0)) if cap > 0 else 100.0
    lo, hi = 0.0, 100.0
    for _ in range(40):
        mid = 0.5 * (lo + hi)
        if calibration.lookup("pedal_map", rpm, mid) < want:
            lo = mid
        else:
            hi = mid
    return hi


def _run_job(job: dict) -> dict:
    """One battery run (top-level so it can execute in a worker process)."""
    config = EngineConfig.from_dict(job["config"])
    cal = Calibration.from_dict(job["calibration"])
    test = DynoTest.from_dict(job["test"])
    result = run_dyno(config, cal, test)
    return {"name": job["name"], "runs": [r.to_dict(include_samples=False) for r in result.runs]}


_POOL: ProcessPoolExecutor | None = None
_POOL_LOCK = threading.Lock()


def _pool(workers: int) -> ProcessPoolExecutor:
    """Shared worker processes ("spawn": safe in a multi-threaded server). Grows, never shrinks."""
    global _POOL
    with _POOL_LOCK:
        if _POOL is None or getattr(_POOL, "_max_workers", 0) < workers:
            if _POOL is not None:
                _POOL.shutdown(wait=False)
            import multiprocessing

            _POOL = ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn"))
        return _POOL


def _reset_pool() -> None:
    global _POOL
    with _POOL_LOCK:
        if _POOL is not None:
            _POOL.shutdown(wait=False, cancel_futures=True)
        _POOL = None


@dataclass
class Scorecard:
    calibration_hash: str
    engine_id: str
    fidelity: str
    metrics: dict[str, float]
    stress: dict[str, float]
    stress_by_condition: dict[str, dict[str, float]]
    margins_pct: dict[str, float]
    knock: dict[str, float]
    violations: list[str]
    warnings: list[str]
    stress_index: float
    curves: dict[str, list[dict]]
    events: dict[str, dict]
    limits: dict
    eval_seconds: float
    provenance: str = "simulated"
    note: str = ("Simulated on a synthetic engine model. Within modelled limits does not mean safe for a real "
                 "engine.")

    @property
    def acceptable(self) -> bool:
        return not self.violations and not self.warnings

    def to_dict(self, include_curves: bool = True) -> dict:
        d = asdict(self)
        d["acceptable"] = self.acceptable
        if not include_curves:
            d["curves"] = {}
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Scorecard:
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in known})


def _curve_points(run: dict) -> list[dict]:
    keys = ("rpm", "torque_nm", "power_kw", "boost_kpa", "map_kpa", "boost_target_kpa", "spark_deg", "mbt_deg",
            "knock_limit_deg", "knock_retard_deg", "lambda", "egt_c", "turbo_krpm", "peak_pressure_bar",
            "iat_c", "torque_limit_nm", "torque_request_nm", "compressor_out_c", "coolant_c", "bsfc_g_kwh",
            "load_pct", "wastegate_pct")
    return [{k: p.get(k) for k in keys} for p in run["curve"]]


def _stress_of(run: dict) -> dict[str, float]:
    s = run["summary"]
    curve = run["curve"]

    def mx(key):
        vals = [p[key] for p in curve if p.get(key) is not None]
        return max(vals) if vals else 0.0

    return {
        "peak_pressure_bar": s.get("max_peak_pressure_bar") or mx("peak_pressure_bar"),
        "torque_nm": s.get("peak_torque_nm") or 0.0,
        "turbo_krpm": s.get("max_turbo_krpm") or 0.0,
        "egt_c": s.get("max_egt_c") or 0.0,
        "compressor_out_c": mx("compressor_out_c"),
        "iat_c": s.get("max_iat_c") or 0.0,
        "coolant_c": s.get("max_coolant_c") or 0.0,
        "min_lambda": s.get("min_lambda") or 1.0,
        "knock_retard_deg": s.get("max_knock_retard_deg") or 0.0,
    }


def _band_mean(curve: list[dict], key: str, lo: float, hi: float) -> float:
    vals = [p[key] for p in curve if lo <= p["rpm"] <= hi and p.get(key) is not None]
    return sum(vals) / len(vals) if vals else 0.0


def evaluate(config: EngineConfig, calibration: Calibration, battery: BatteryConfig | None = None,
             limits: StressLimits | None = None, cancel=None) -> Scorecard:
    """Run the battery and build the scorecard."""
    battery = battery or BatteryConfig.preset("quick")
    limits = limits or StressLimits.from_config(config)
    t0 = time.perf_counter()
    end = config.limits.max_rpm - battery.rpm_end_offset
    sweep = {"kind": "sweep", "rpm_start": battery.rpm_start, "rpm_end": end, "ramp_rpm_s": battery.ramp_rpm_s,
             "settle_s": 1.0, "label": ""}
    cal_d = calibration.to_dict()
    jobs = []
    for cond in battery.conditions:
        cfg, kw = _condition_setup(config, cond)
        jobs.append({"name": cond, "config": cfg.to_dict(), "calibration": cal_d,
                     "test": {**sweep, **kw, "label": f"battery {cond}"}})
    if battery.part_load_points:
        ref = reference_torque(config)
        pts = [[rpm, round(pedal_for_torque(calibration, rpm, frac * ref), 3)] for rpm, frac in battery.part_load_points]
        jobs.append({"name": "part_load", "config": config.to_dict(), "calibration": cal_d,
                     "test": {"kind": "steady", "steady_points": pts,
                              "steady_settle_s": 1.5, "steady_measure_s": 0.4, "label": "battery part load"}})
    if battery.tip_in:
        jobs.append({"name": "tip_in", "config": config.to_dict(), "calibration": cal_d,
                     "test": {"kind": "transient", "transient_rpm": 2500.0, "transient_from_pedal": 15.0,
                              "transient_to_pedal": 100.0, "transient_duration_s": 2.5, "settle_s": 1.0,
                              "label": "battery tip-in"}})
    workers = battery.workers or min(4, os.cpu_count() or 1)
    results: dict[str, dict] = {}
    if workers > 1 and len(jobs) > 1:
        try:
            futures = {_pool(workers).submit(_run_job, j): j["name"] for j in jobs}
            for fut, name in futures.items():
                if cancel is not None and cancel():
                    for f in futures:
                        f.cancel()
                    raise RuntimeError("Evaluation cancelled")
                results[name] = fut.result()["runs"][0]
        except BrokenProcessPool:
            # Worker processes could not start (e.g. the caller's __main__ is not importable under the
            # "spawn" start method). Same computation, run in this process instead.
            _reset_pool()
            results = {}
    if len(results) < len(jobs):
        for j in jobs:
            if cancel is not None and cancel():
                raise RuntimeError("Evaluation cancelled")
            results[j["name"]] = _run_job(j)["runs"][0]

    nominal = results["nominal"]
    curve = nominal["curve"]
    s = nominal["summary"]
    lo, hi = battery.rpm_start, end
    metrics = {
        "peak_power_kw": s["peak_power_kw"], "peak_torque_nm": s["peak_torque_nm"],
        # mean over the last 1500 rpm: tracks peak power but does not jump when the peak moves between bins
        "top_power_kw": round(_band_mean(curve, "power_kw", hi - 1500.0, hi), 3),
        "mean_power_kw": round(_band_mean(curve, "power_kw", max(lo, 2500.0), hi), 3),
        "mean_torque_nm": round(_band_mean(curve, "torque_nm", lo, min(hi, 5000.0)), 3),
        "power_at_redline_kw": curve[-1]["power_kw"] if curve else 0.0,
    }
    if "part_load" in results:
        pts = results["part_load"]["curve"]
        bsfc = [p["bsfc_g_kwh"] for p in pts if p.get("bsfc_g_kwh")]
        metrics["part_load_bsfc_g_kwh"] = round(sum(bsfc) / len(bsfc), 3) if bsfc else 0.0
        metrics["part_load_torque_nm"] = round(sum(p["torque_nm"] for p in pts) / len(pts), 3)
    if "tip_in" in results:
        m = results["tip_in"]["metrics"]
        metrics["tip_in_t90_s"] = m.get("t90_s") or 0.0
        metrics["tip_in_overshoot_pct"] = m.get("torque_overshoot_pct") or 0.0

    by_cond = {name: _stress_of(r) for name, r in results.items() if r["kind"] in ("sweep", "partial")}
    stress = {k: (min if k == "min_lambda" else max)(v[k] for v in by_cond.values()) for k in
              next(iter(by_cond.values()))}
    margins: dict[str, float] = {}
    violations: list[str] = []
    warnings: list[str] = []
    utilisation = []
    for key, (attr, direction) in limits.active().items():
        limit = getattr(limits, attr)
        value = stress[key]
        if direction == "max":
            margin = (limit - value) / limit * 100.0
            utilisation.append(value / limit)
        else:
            margin = (value - limit) / limit * 100.0
            utilisation.append(limit / value if value > 0 else 2.0)
        margins[key] = round(margin, 3)
        worst = max(by_cond, key=lambda c, k=key, d=direction: by_cond[c][k] if d == "max" else -by_cond[c][k])
        if margin < 0:
            violations.append(f"{key} {value:.1f} beyond limit {limit:g} (worst in '{worst}')")
        elif margin < limits.required_margin_pct:
            warnings.append(f"{key} margin {margin:.1f} % < required {limits.required_margin_pct:g} % "
                            f"(worst in '{worst}')")
    knock_nominal = by_cond["nominal"]["knock_retard_deg"]
    knock_adverse = max((v["knock_retard_deg"] for c, v in by_cond.items() if c in ADVERSE), default=0.0)
    if knock_nominal > limits.max_knock_retard_nominal_deg:
        violations.append(f"knock control active in nominal conditions ({knock_nominal:.1f}° retard)")
    if knock_adverse > limits.max_knock_retard_adverse_deg:
        violations.append(f"heavy knock control in adverse conditions ({knock_adverse:.1f}° retard)")
    for name, r in results.items():
        if r["events"].get("dtcs"):
            violations.append(f"DTCs in '{name}': {', '.join(r['events']['dtcs'])}")
        for prot in ("limp_mode", "overboost_protection", "coolant_torque_limit"):
            if prot in r["events"].get("protections", []):
                violations.append(f"protection '{prot}' engaged in '{name}'")
    return Scorecard(
        calibration_hash=calibration.content_hash(), engine_id=config.id, fidelity=battery.fidelity,
        metrics={k: round(float(v), 4) for k, v in metrics.items()},
        stress={k: round(float(v), 3) for k, v in stress.items()},
        stress_by_condition={c: {k: round(float(x), 3) for k, x in v.items()} for c, v in by_cond.items()},
        margins_pct=margins, knock={"nominal_deg": knock_nominal, "adverse_deg": knock_adverse},
        violations=violations, warnings=warnings, stress_index=round(max(utilisation), 4),
        curves={name: _curve_points(r) for name, r in results.items() if r["curve"]},
        events={name: r["events"] for name, r in results.items()}, limits=limits.to_dict(),
        eval_seconds=round(time.perf_counter() - t0, 3))


# ---------------------------------------------------------------------------------------- objectives
GOALS: dict[str, dict] = {
    "max_power": {"label": "Top-end power", "weights": {"top_power_kw": 0.7, "mean_power_kw": 0.3}},
    "power_band": {"label": "Area under the power curve", "weights": {"mean_power_kw": 0.7, "top_power_kw": 0.3}},
    "midrange_torque": {"label": "Mid-range torque", "weights": {"mean_torque_nm": 1.0}},
    "efficiency": {"label": "Part-load efficiency (BSFC at equal torque) without losing power",
                   "weights": {"part_load_bsfc_g_kwh": -1.0, "top_power_kw": 0.2}},
    "balanced": {"label": "Balanced: power, torque, efficiency, low stress",
                 "weights": {"mean_power_kw": 0.4, "mean_torque_nm": 0.3, "part_load_bsfc_g_kwh": -0.3}},
}
# Peak power/torque are reported but not optimised: a single binned maximum moves between rpm bins and
# makes the response jumpy (measured: ±1 % from the peak moving 600 rpm while the top-end mean is smooth).


def objective_from(metrics: dict[str, float], stress_index: float, baseline_metrics: dict[str, float],
                   baseline_stress_index: float, goal: str, stress_weight: float = 0.25) -> float:
    """``objective`` on plain values (used for predicted candidates)."""
    if goal not in GOALS:
        raise ValueError(f"Unknown goal {goal!r}")
    total = 0.0
    for key, w in GOALS[goal]["weights"].items():
        b = baseline_metrics.get(key)
        v = metrics.get(key)
        if not b or v is None:
            continue
        total += abs(w) * ((v / b - 1.0) * 100.0) * (1.0 if w > 0 else -1.0)
    penalty = stress_weight * max(0.0, (stress_index - baseline_stress_index) * 100.0)
    return round(total - penalty, 4) if math.isfinite(total) else -1e9


def objective(score: Scorecard, baseline: Scorecard, goal: str, stress_weight: float = 0.25) -> float:
    """Percent improvement over the baseline, weighted per goal, minus a penalty for added stress.

    Negative weights mean "lower is better" (BSFC). The stress penalty is ``stress_weight`` × the
    increase of the stress index (in percent of the limits) — a tuned engine that gains 2 % power by
    using 10 % more of its pressure or temperature budget scores lower than one that gains 2 % for free.
    """
    return objective_from(score.metrics, score.stress_index, baseline.metrics, baseline.stress_index, goal,
                          stress_weight)


# ---------------------------------------------------------------------------------------- verdicts
@dataclass
class Verdict:
    passed: bool
    reasons: list[str]
    inherited: list[str]  # tight margins already present in the baseline (not the tuner's doing)

    def to_dict(self) -> dict:
        return asdict(self)


def margin_floor(baseline: Scorecard, key: str, required_pct: float, tolerance_pp: float) -> float:
    """Lowest acceptable margin for a stress key.

    Normally the required margin. Where the *baseline* is already tighter than that (e.g. exhaust
    temperature held near its limit by component protection on a hot day), a candidate may not make it
    worse than the baseline by more than ``tolerance_pp`` percentage points — the tuner is not blamed for
    the starting point, but may not eat into it either. Never below zero (the limit itself).
    """
    return max(0.0, min(required_pct, baseline.margins_pct.get(key, required_pct) - tolerance_pp))


def judge(candidate: Scorecard, baseline: Scorecard, tolerance_pp: float = 0.5,
          floors: dict[str, float] | None = None) -> Verdict:
    """Accept/reject a candidate against the limits *and* the baseline it was derived from.

    Rejected when: any absolute violation (beyond a limit, DTC, limp/overboost/coolant protection, knock
    control in nominal conditions, heavy knock control in adverse conditions), or any stress margin below
    its floor (``margin_floor``, or explicit ``floors``), or adverse-condition knock retard more than 1° above
    the baseline's.
    """
    req = float(candidate.limits.get("required_margin_pct", 3.0))
    reasons = list(candidate.violations)
    inherited = []
    for key in STRESS_KEYS:
        if key not in candidate.margins_pct:
            continue  # indicator without a limit
        floor = floors[key] if floors and key in floors else margin_floor(baseline, key, req, tolerance_pp)
        m = candidate.margins_pct[key]
        if m < floor:
            if not any(r.startswith(key + " ") for r in reasons):
                reasons.append(f"{key} margin {m:.2f} % below floor {floor:.2f} % "
                               f"(baseline {baseline.margins_pct.get(key, 0.0):.2f} %)")
        elif m < req:
            inherited.append(f"{key} margin {m:.2f} % (baseline {baseline.margins_pct.get(key, 0.0):.2f} %) — "
                             "tight in the baseline already")
    if candidate.knock["adverse_deg"] > baseline.knock["adverse_deg"] + 1.0:
        reasons.append(f"adverse-condition knock retard {candidate.knock['adverse_deg']:.1f}° vs baseline "
                       f"{baseline.knock['adverse_deg']:.1f}°")
    return Verdict(passed=not reasons, reasons=reasons, inherited=inherited)
