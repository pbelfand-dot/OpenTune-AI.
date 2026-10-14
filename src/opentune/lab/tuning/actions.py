"""Calibration actions: the bounded, explainable changes the tuning engine is allowed to make.

A tuning engine that may edit any cell of any table learns slowly and explains nothing. Instead it
works with a small vocabulary of *composite* actions, the way a calibrator thinks:

* ``torque.<zone>``  — raise/lower the torque limit in an rpm zone; the maximum-boost table at high pedal
  follows so the extra torque can actually be produced (more air needs more boost).
* ``spark.<zone>``   — advance/retard base ignition at high load in an rpm zone.
* ``lambda.<zone>``  — enrich/lean the full-load lambda target in an rpm zone (never leaner than λ 1.0).
* ``part_spark``     — advance/retard base ignition at part load (30–90 % load), all speeds.

Each action has a *step* (one action unit, e.g. 3 % torque or 1° spark) and bounds on the cumulative
change from the baseline. A candidate calibration is always ``baseline + offsets``: reproducible and easy
to explain ("+6 % torque limit 3000–4500 rpm, −0.04 λ above 5000 rpm").

Not offered: part-load lambda (catalyst efficiency and emissions are not modelled, so a "better" lean
part-load calibration would be an artefact of the model); idle, cold-start, fuel-system and protection
tables (safety functions are not tuning targets); the ECU torque model (kept consistent with the
baseline engine hardware).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np

from opentune.lab.calibration.tables import Calibration

BARO_KPA = 101.325
ZONE_TAPER_RPM = 500.0

# rpm zones (inclusive core; weights taper to zero over ZONE_TAPER_RPM outside)
ZONES: dict[str, tuple[float, float | None]] = {
    "low": (1250.0, 2500.0),
    "mid": (3000.0, 4500.0),
    "high": (5000.0, None),  # None = up to the table's last breakpoint
}


@dataclass(frozen=True)
class ActionSpec:
    id: str
    kind: str
    zone: str
    label: str
    unit: str
    step: float  # physical change per action unit
    lower: float  # bounds on the cumulative physical change from the baseline
    upper: float
    description: str

    def to_dict(self) -> dict:
        return asdict(self)


def _zone_text(zone: str) -> str:
    lo, hi = ZONES[zone]
    return f"{lo:.0f}–{hi:.0f} rpm" if hi else f"≥ {lo:.0f} rpm"


def _specs() -> dict[str, ActionSpec]:
    out: dict[str, ActionSpec] = {}
    for zone in ZONES:
        z = _zone_text(zone)
        out[f"torque.{zone}"] = ActionSpec(
            f"torque.{zone}", "torque", zone, f"Torque limit {z}", "%", 3.0, -30.0, 15.0,
            f"Scales the torque limit {z} and the high-pedal maximum boost with it (reductions go further than "
            "increases: derating is the mechanically safe direction).")
        out[f"spark.{zone}"] = ActionSpec(
            f"spark.{zone}", "spark", zone, f"Full-load spark {z}", "°", 1.0, -4.0, 3.0,
            f"Adds to base ignition at ≥ 120 % load (half at 100 %) {z}.")
        out[f"lambda.{zone}"] = ActionSpec(
            f"lambda.{zone}", "lambda", zone, f"Full-load lambda {z}", "λ", 0.02, -0.06, 0.06,
            f"Adds to the lambda target at ≥ 120 % load (half at 100 %) {z}; clipped to 0.75…1.00.")
    out["part_spark"] = ActionSpec(
        "part_spark", "part_spark", "all", "Part-load spark", "°", 1.0, -3.0, 3.0,
        "Adds to base ignition at 30–90 % load (half at 20 % and 100 %), all speeds.")
    return out


ACTIONS: dict[str, ActionSpec] = _specs()
DEFAULT_ACTIONS: tuple[str, ...] = tuple(ACTIONS)


def zone_weight(rpm: float, zone: str, last_rpm: float) -> float:
    if zone == "all":
        return 1.0
    lo, hi = ZONES[zone]
    hi = last_rpm if hi is None else hi
    if lo <= rpm <= hi:
        return 1.0
    d = lo - rpm if rpm < lo else rpm - hi
    return max(0.0, 1.0 - d / ZONE_TAPER_RPM)


def _high_load_weight(load: float) -> float:
    return 1.0 if load >= 120.0 else 0.5 if load >= 100.0 else 0.0


def _part_load_weight(load: float) -> float:
    if 30.0 <= load <= 90.0:
        return 1.0
    return 0.5 if load in (20.0, 100.0) else 0.0


def _pedal_weight(pedal: float) -> float:
    return 1.0 if pedal >= 80.0 else 0.5 if pedal >= 60.0 else 0.0


def validate_offsets(offsets: dict[str, float]) -> list[str]:
    problems = []
    for aid, val in offsets.items():
        spec = ACTIONS.get(aid)
        if spec is None:
            problems.append(f"Unknown action {aid!r}")
        elif not spec.lower - 1e-9 <= val <= spec.upper + 1e-9:
            problems.append(f"{aid}: {val:+g} {spec.unit} outside {spec.lower:+g}…{spec.upper:+g}")
    return problems


def clamp_offset(aid: str, value: float) -> float:
    spec = ACTIONS[aid]
    return min(spec.upper, max(spec.lower, value))


def apply_offsets(baseline: Calibration, offsets: dict[str, float], name: str | None = None) -> Calibration:
    """Return ``baseline`` with the given cumulative action offsets (physical units) applied."""
    problems = validate_offsets(offsets)
    if problems:
        raise ValueError("; ".join(problems))
    cal = baseline.copy(name or f"{baseline.name} + tuning")
    torque = cal.table("torque_limit")
    boost = cal.table("boost_max")
    ign = cal.table("ign_base")
    lam = cal.table("lambda_target")
    t_rpm = torque.axes[0].values
    b_rpm, b_pedal = boost.axes[0].values, boost.axes[1].values
    i_rpm, i_load = ign.axes[0].values, ign.axes[1].values
    l_rpm, l_load = lam.axes[0].values, lam.axes[1].values
    tv, bv, iv, lv = (torque.values.copy(), boost.values.copy(), ign.values.copy(), lam.values.copy())
    lam_base = lv.copy()

    for aid, val in offsets.items():
        if abs(val) < 1e-12:
            continue
        spec = ACTIONS[aid]
        if spec.kind == "torque":
            frac = val / 100.0
            for i, r in enumerate(t_rpm):
                tv[i] *= 1.0 + frac * zone_weight(r, spec.zone, t_rpm[-1])
            for i, r in enumerate(b_rpm):
                wz = zone_weight(r, spec.zone, b_rpm[-1])
                if wz == 0.0 or r < 1000.0:
                    continue
                for j, p in enumerate(b_pedal):
                    w = wz * _pedal_weight(p)
                    if w and bv[i, j] > BARO_KPA + 8.0:
                        # torque ∝ air charge ∝ absolute manifold pressure: scale the absolute boost
                        # cap, plus 20 % for the extra pumping work and charge heating
                        bv[i, j] *= 1.0 + 1.2 * frac * w
        elif spec.kind in ("spark", "lambda"):
            rpm_ax, load_ax = (i_rpm, i_load) if spec.kind == "spark" else (l_rpm, l_load)
            arr = iv if spec.kind == "spark" else lv
            for i, r in enumerate(rpm_ax):
                wz = zone_weight(r, spec.zone, rpm_ax[-1])
                if wz == 0.0:
                    continue
                for j, ld in enumerate(load_ax):
                    arr[i, j] += val * wz * _high_load_weight(ld)
        elif spec.kind == "part_spark":
            for j, ld in enumerate(i_load):
                iv[:, j] += val * _part_load_weight(ld)

    # lambda: never leaner than stoichiometric where the baseline was enriched, never below 0.75
    changed = np.abs(lv - lam_base) > 1e-12
    lv = np.where(changed, np.clip(lv, 0.75, np.maximum(1.0, lam_base)), lv)
    for table, values in ((torque, tv), (boost, bv), (ign, iv), (lam, lv)):
        table.values = np.clip(np.round(values, 6), table.minimum, table.maximum)
        table.invalidate()
    cal.metadata = {**cal.metadata, "tuning_offsets": {k: v for k, v in offsets.items() if abs(v) > 1e-12},
                    "provenance": "simulated tuning-engine candidate (unvalidated, not for a vehicle)"}
    return cal


def describe_offsets(offsets: dict[str, float]) -> list[str]:
    """Human-readable list of the cumulative changes."""
    out = []
    for aid in sorted(offsets, key=lambda a: list(ACTIONS).index(a) if a in ACTIONS else 99):
        val = offsets[aid]
        if abs(val) < 1e-9:
            continue
        spec = ACTIONS[aid]
        fmt = f"{val:+.3f}" if spec.unit == "λ" else f"{val:+.1f}"
        out.append(f"{spec.label}: {fmt} {spec.unit}")
    return out


def describe_step(step: dict[str, float]) -> str:
    """Describe a step given in action units."""
    parts = []
    for aid, units in step.items():
        spec = ACTIONS[aid]
        phys = units * spec.step
        fmt = f"{phys:+.3f}" if spec.unit == "λ" else f"{phys:+.1f}"
        parts.append(f"{spec.label} {fmt} {spec.unit}")
    return ", ".join(parts) if parts else "no change"
