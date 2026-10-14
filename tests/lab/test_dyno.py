"""Virtual dyno: corrections, drivetrain loss, test kinds, comparisons, exports, uncertainty."""

from __future__ import annotations

import json
import math

import pytest

from opentune.lab.calibration import CalibrationEditor, build_baseline
from opentune.lab.dyno import (
    DRIVETRAIN_EXAMPLES,
    DrivetrainLoss,
    DynoCancelled,
    DynoError,
    DynoRun,
    DynoTest,
    channel_list,
    compare_runs,
    correction_factor,
    curve_to_csv,
    estimate_uncertainty,
    figure_spec,
    run_dyno,
    samples_to_csv,
)
from opentune.lab.dyno.uncertainty import UNCERTAIN_PARAMETERS
from opentune.lab.physics import get_preset
from opentune.lab.provenance import Provenance
from opentune.lab.units import HP_RPM_CONSTANT, W_PER_HP, W_PER_PS

FAST = {"rpm_start": 2000.0, "rpm_end": 4000.0, "ramp_rpm_s": 500.0, "settle_s": 1.0}


@pytest.fixture(scope="module")
def b58():
    cfg = get_preset("b58-inspired")
    return cfg, build_baseline(cfg)


@pytest.fixture(scope="module")
def sweep(b58):
    cfg, cal = b58
    return run_dyno(cfg, cal, DynoTest(kind="sweep", drivetrain=DRIVETRAIN_EXAMPLES["flat_15"],
                                       correction="sae_j1349", **FAST)).runs[0]


# ---------------------------------------------------------------------- corrections
@pytest.mark.parametrize("std,p,t", [("sae_j1349", 99_000.0, 298.0), ("din_70020", 101_300.0, 293.0),
                                     ("iso_1585_si", 99_000.0, 298.0), ("none", 90_000.0, 310.0)])
def test_correction_is_unity_at_reference_conditions(std, p, t):
    assert correction_factor(std, p, t).factor == pytest.approx(1.0, abs=1e-9)


@pytest.mark.parametrize("std", ["sae_j1349", "din_70020", "iso_1585_si"])
def test_correction_direction(std):
    ref = correction_factor(std, 99_000.0, 298.0).factor
    assert correction_factor(std, 95_000.0, 298.0).factor > ref  # thinner air → corrected up
    assert correction_factor(std, 99_000.0, 308.0).factor > ref  # hotter air → corrected up


def test_correction_vapour_pressure_and_applicability():
    dry = correction_factor("sae_j1349", 100_000.0, 298.0).factor
    humid = correction_factor("sae_j1349", 100_000.0, 298.0, vapour_pressure_pa=2000.0).factor
    assert humid > dry
    turbo = correction_factor("sae_j1349", 100_000.0, 298.0, turbocharged=True)
    assert not turbo.applicable and any("naturally aspirated" in n for n in turbo.notes)
    assert any("secondary" in n for n in turbo.notes)
    with pytest.raises(KeyError):
        correction_factor("made_up", 1e5, 298.0)


# ---------------------------------------------------------------------- drivetrain
def test_drivetrain_loss_model():
    d = DrivetrainLoss(fraction=0.10, parasitic_nm=5.0, viscous_nm_per_krpm=2.0)
    assert d.wheel_torque_crank_ref(400.0, 3000.0) == pytest.approx(400.0 - 40.0 - 5.0 - 6.0)
    assert d.wheel_torque_crank_ref(-50.0, 3000.0) == 0.0
    assert DrivetrainLoss(fraction=0.9).validate()
    with pytest.raises(ValueError):
        DrivetrainLoss.from_dict({"fraction": -0.1})
    for ex in DRIVETRAIN_EXAMPLES.values():
        assert ex.provenance == Provenance.ASSUMPTION.value and "Illustrative" in ex.note


# ---------------------------------------------------------------------- sweeps
def test_sweep_curve_units_are_consistent(sweep):
    assert sweep.provenance == "simulated" and sweep.limitations
    assert sweep.curve[0]["rpm"] <= 2100 and sweep.curve[-1]["rpm"] >= 3900
    for p in sweep.curve:
        kw = p["torque_nm"] * p["rpm"] * 2 * math.pi / 60 / 1000
        assert p["power_kw"] == pytest.approx(kw, abs=1e-3)
        assert p["power_hp"] == pytest.approx(kw * 1000 / W_PER_HP, abs=1e-3)
        assert p["power_ps"] == pytest.approx(kw * 1000 / W_PER_PS, abs=1e-3)
        assert p["power_hp"] == pytest.approx(p["torque_lbft"] * p["rpm"] / HP_RPM_CONSTANT, rel=1e-4)
        assert p["wheel_torque_nm"] == pytest.approx(0.85 * p["torque_nm"], abs=1e-3)
        assert p["torque_corr_nm"] == pytest.approx(p["torque_nm"] * sweep.correction["factor"], abs=1e-3)
    assert sweep.correction["applicable"] is False  # turbo engine


def test_sweep_summary_and_events(sweep):
    s = sweep.summary
    best = max(sweep.curve, key=lambda p: p["power_kw"])
    assert s["peak_power_kw"] == pytest.approx(best["power_kw"], abs=0.01)
    assert s["peak_wheel_power_kw"] < s["peak_power_kw"]
    assert 380 < s["peak_torque_nm"] < 560
    assert s["min_lambda"] < 0.95  # enrichment at full load
    assert sweep.events["dtcs"] == []
    assert "wot" in sweep.events["modes"]


def test_partial_sweep_makes_less_torque(b58, sweep):
    cfg, cal = b58
    part = run_dyno(cfg, cal, DynoTest(kind="partial", pedal=35.0, **FAST)).runs[0]
    assert part.summary["peak_torque_nm"] < 0.7 * sweep.summary["peak_torque_nm"]
    assert part.summary["max_map_kpa"] < sweep.summary["max_map_kpa"]


def test_steady_points(b58):
    cfg, cal = b58
    run = run_dyno(cfg, cal, DynoTest(kind="steady", steady_points=[(2000.0, 25.0), (3500.0, 100.0)],
                                      steady_settle_s=2.0, steady_measure_s=0.5)).runs[0]
    lo, hi = run.curve
    assert lo["torque_nm"] < hi["torque_nm"]
    for p in run.curve:
        assert abs(p["measured_rpm"] - p["rpm"]) < 15.0
        assert p["torque_std_nm"] < 0.03 * max(p["torque_nm"], 50.0)
        assert 200.0 < p["bsfc_g_kwh"] < 450.0


def test_transient_tip_in_metrics(b58):
    cfg, cal = b58
    run = run_dyno(cfg, cal, DynoTest(kind="transient", transient_rpm=2500.0, transient_duration_s=3.0,
                                      settle_s=1.0)).runs[0]
    m = run.metrics
    assert m["final_torque_nm"] > m["initial_torque_nm"] + 200.0
    assert 0.0 <= m["t10_s"] <= m["t50_s"] <= m["t90_s"] < 3.0
    assert m["boost_t90_s"] is not None


def test_repeat_runs_show_heat_soak(b58):
    cfg, cal = b58
    res = run_dyno(cfg, cal, DynoTest(kind="repeat", repeats=2, cooldown_s=0.5, **FAST))
    assert len(res.runs) == 2
    rep = res.repeatability
    assert rep["start_charge_loop_c"][1] > rep["start_charge_loop_c"][0]
    assert rep["peak_power_kw"]["std"] >= 0.0 and len(rep["peak_power_kw"]["values"]) == 2


def test_runs_are_deterministic(b58):
    cfg, cal = b58
    a = run_dyno(cfg, cal, DynoTest(kind="sweep", seed=3, **FAST)).runs[0]
    b = run_dyno(cfg, cal, DynoTest(kind="sweep", seed=3, **FAST)).runs[0]
    assert a.curve == b.curve


def test_invalid_tests_are_rejected(b58):
    cfg, cal = b58
    for bad in (DynoTest(kind="warp"), DynoTest(rpm_start=5000, rpm_end=3000), DynoTest(kind="steady"),
                DynoTest(ambient_pressure_kpa=20.0), DynoTest(kind="repeat", repeats=50)):
        with pytest.raises(DynoError):
            run_dyno(cfg, cal, bad)


def test_progress_and_cancel(b58):
    cfg, cal = b58
    seen = []
    run_dyno(cfg, cal, DynoTest(kind="sweep", **FAST), progress=lambda f, msg: seen.append(f))
    assert seen and all(0.0 <= f < 1.0 for f in seen) and seen == sorted(seen)
    with pytest.raises(DynoCancelled):
        run_dyno(cfg, cal, DynoTest(kind="sweep", **FAST), cancel=lambda: True)


# ---------------------------------------------------------------------- comparison and export
def test_before_after_comparison(b58, sweep):
    cfg, cal = b58
    detuned = cal.copy("detuned")
    CalibrationEditor(detuned).apply_region("torque_limit", "percent", -20.0)
    after = run_dyno(cfg, detuned, DynoTest(kind="sweep", drivetrain=DRIVETRAIN_EXAMPLES["flat_15"],
                                            correction="sae_j1349", **FAST)).runs[0]
    cmp = compare_runs([sweep, after])
    assert cmp["runs"][1]["peak_torque_delta_nm"] < -50.0
    assert cmp["runs"][1]["mean_power_delta_kw"] < 0.0
    assert cmp["runs"][0]["peak_torque_delta_nm"] == 0.0
    assert len(cmp["rpm"]) == len(cmp["runs"][1]["torque_delta_nm"])
    assert after.calibration_hash != sweep.calibration_hash
    with pytest.raises(ValueError):
        compare_runs([sweep])


def test_csv_exports_are_labelled_simulated(sweep):
    text = curve_to_csv(sweep)
    assert text.startswith("# OpenTune Virtual Engine Lab — SIMULATED")
    assert "torque_nm (N·m)" in text and "# drivetrain loss (stated" in text
    lines = [ln for ln in samples_to_csv(sweep).splitlines() if not ln.startswith("#")]
    assert len(lines) == len(sweep.samples) + 1


def test_figure_spec_and_serialisation(sweep):
    fig = figure_spec([sweep])
    assert len(fig["data"]) == 2 and "SIMULATED" in fig["layout"]["annotations"][0]["text"]
    blob = json.dumps(sweep.to_dict(), allow_nan=False)
    back = DynoRun.from_dict(json.loads(blob))
    assert back.summary == sweep.summary
    assert {c["id"] for c in channel_list()} >= {"torque_nm", "power_kw", "boost_kpa", "lambda", "spark_deg",
                                                 "egt_c", "iat_c", "knock_retard_deg"}
    test = DynoTest.from_dict(sweep.test)
    assert test.drivetrain is not None and test.drivetrain.fraction == 0.15


# ---------------------------------------------------------------------- uncertainty
def test_uncertainty_band(b58, sweep):
    cfg, _ = b58
    u = estimate_uncertainty(cfg, sweep, samples=12, max_points=5, sensitivity=False)
    for lo, mid, hi in zip(u.torque_p05, u.torque_p50, u.torque_p95, strict=True):
        assert lo <= mid <= hi
        assert hi - lo > 0.0
    assert u.peak_power_p05_kw <= u.peak_power_p50_kw <= u.peak_power_p95_kw
    assert abs(u.model_vs_run_pct) < 6.0  # steady solver agrees with the dynamic run
    assert any("not a confidence interval" in n for n in u.notes)
    again = estimate_uncertainty(cfg, sweep, samples=12, max_points=5, sensitivity=False)
    assert again.torque_p50 == u.torque_p50


def test_uncertainty_never_perturbs_documented_values(b58, sweep):
    cfg, _ = b58
    u = estimate_uncertainty(cfg, sweep, samples=8, max_points=3, sensitivity=True)
    for p in u.parameters:
        assert cfg.provenance_of(p["path"]) != Provenance.DOCUMENTED
    assert {p.path for p in UNCERTAIN_PARAMETERS} >= {s["path"] for s in u.sensitivity}
    assert u.sensitivity == sorted(u.sensitivity, key=lambda s: -s["swing_kw"])
