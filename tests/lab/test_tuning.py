"""Tuning engine: actions, evaluator/scorecard, verdicts, response model, knowledge base, sessions."""

from __future__ import annotations

import ast
import math
from pathlib import Path

import numpy as np
import pytest

from opentune.lab.calibration.defaults import build_baseline
from opentune.lab.physics.presets import get_preset
from opentune.lab.tuning.actions import ACTIONS, apply_offsets, describe_offsets, validate_offsets
from opentune.lab.tuning.benchmark import SUITES, Task, aggregate, variant_config
from opentune.lab.tuning.evaluator import (
    GOALS,
    BatteryConfig,
    Scorecard,
    StressLimits,
    evaluate,
    judge,
    margin_floor,
    objective,
    pedal_for_torque,
    reference_torque,
)
from opentune.lab.tuning.knowledge import (
    OUTPUTS,
    KnowledgeBase,
    ResponseModel,
    engineering_prior,
)
from opentune.lab.tuning.session import TuningRequest, TuningSession

TUNING_DIR = Path(__file__).resolve().parents[2] / "src" / "opentune" / "lab" / "tuning"


def mini_battery(conditions=("nominal", "hot_knock_prone"), fidelity="test") -> BatteryConfig:
    """Short sweeps, one part-load point, sequential — enough to exercise every code path quickly."""
    return BatteryConfig(fidelity, rpm_start=2500.0, rpm_end_offset=500.0, ramp_rpm_s=900.0,
                         conditions=conditions, part_load_points=((2500.0, 0.3),), tip_in=False, workers=1)


@pytest.fixture(scope="module")
def b48():
    cfg = get_preset("b48-inspired")
    return cfg, build_baseline(cfg)


@pytest.fixture(scope="module")
def base_score(b48):
    cfg, cal = b48
    return evaluate(cfg, cal, mini_battery())


# ---------------------------------------------------------------------------------------- actions
def test_actions_change_only_their_tables_and_zones(b48):
    _, base = b48
    cal = apply_offsets(base, {"torque.mid": 6.0})
    d = base.diff(cal)
    assert {t for t, td in d.tables.items() if td.changed_cells} == {"torque_limit", "boost_max"}
    tl0, tl1 = base.table("torque_limit"), cal.table("torque_limit")
    for rpm, a, b in zip(tl0.axes[0].values, tl0.values, tl1.values, strict=True):
        if 3000 <= rpm <= 4500:
            assert b == pytest.approx(a * 1.06)
        elif rpm <= 2500 or rpm >= 5000:
            assert b == pytest.approx(a)
    spark = apply_offsets(base, {"spark.high": 1.0})
    ign0, ign1 = base.table("ign_base"), spark.table("ign_base")
    load = ign0.axes[1].values
    for i, rpm in enumerate(ign0.axes[0].values):
        for j, ld in enumerate(load):
            delta = ign1.values[i, j] - ign0.values[i, j]
            if rpm >= 5000 and ld >= 120:
                assert delta == pytest.approx(1.0)
            elif ld < 100 or rpm <= 4500:
                assert delta == pytest.approx(0.0)
    assert apply_offsets(base, {}).content_hash() == base.content_hash()


def test_action_bounds_and_lambda_never_lean_of_stoich(b48):
    _, base = b48
    assert validate_offsets({"torque.mid": 30.0})
    assert validate_offsets({"nope": 1.0})
    with pytest.raises(ValueError):
        apply_offsets(base, {"spark.mid": 10.0})
    lean = apply_offsets(base, {"lambda.high": 0.06, "lambda.mid": 0.06, "lambda.low": 0.06})
    lam0, lam1 = base.table("lambda_target").values, lean.table("lambda_target").values
    assert np.all(lam1 <= np.maximum(1.0, lam0) + 1e-9)
    assert np.all(lam1 >= 0.75 - 1e-9)
    assert lean.metadata["provenance"].endswith("not for a vehicle)")
    assert describe_offsets({"torque.high": 3.0, "lambda.high": -0.02}) == [
        f"{ACTIONS['torque.high'].label}: +3.0 %", f"{ACTIONS['lambda.high'].label}: -0.020 λ"]


# ---------------------------------------------------------------------------------------- evaluator
def test_baseline_scorecard_is_clean(b48, base_score):
    cfg, _ = b48
    s = base_score
    assert s.provenance == "simulated" and "not mean safe" in s.note
    assert not s.violations, s.violations
    assert set(s.stress_by_condition) == {"nominal", "hot_knock_prone"}
    assert {"top_power_kw", "mean_power_kw", "mean_torque_nm", "part_load_bsfc_g_kwh"} <= set(s.metrics)
    # charge-air temperature is reported but has no limit by default
    assert "iat_c" in s.stress and "iat_c" not in s.margins_pct
    assert 0.5 < s.stress_index < 1.0
    assert s.knock["nominal_deg"] == 0.0
    # worst-case stress across conditions
    assert s.stress["egt_c"] == max(v["egt_c"] for v in s.stress_by_condition.values())
    assert s.stress["min_lambda"] == min(v["min_lambda"] for v in s.stress_by_condition.values())
    assert Scorecard.from_dict(s.to_dict()).to_dict() == s.to_dict()
    assert StressLimits.from_config(cfg).max_torque_nm == pytest.approx(1.3 * cfg.reference_ratings["torque_nm"])


def test_part_load_is_compared_at_equal_torque(b48):
    cfg, base = b48
    ref = reference_torque(cfg)
    for cal in (base, apply_offsets(base, {"torque.low": 9.0, "torque.mid": 9.0})):
        pedal = pedal_for_torque(cal, 2500.0, 0.3 * ref)
        demand = cal.lookup("pedal_map", 2500.0, pedal) / 100.0 * cal.lookup("torque_limit", 2500.0)
        assert demand == pytest.approx(0.3 * ref, rel=1e-3)


def test_aggressive_calibration_is_rejected(b48, base_score):
    cfg, base = b48
    hot = apply_offsets(base, {"spark.mid": 3.0, "spark.high": 3.0, "torque.high": 15.0})
    s = evaluate(cfg, hot, mini_battery())
    v = judge(s, base_score)
    assert not v.passed
    assert any("knock" in r for r in v.reasons)
    assert objective(base_score, base_score, "max_power") == 0.0


def test_parallel_and_sequential_evaluations_agree(b48):
    cfg, cal = b48
    seq = evaluate(cfg, cal, mini_battery(conditions=("nominal", "hot")))
    par_battery = mini_battery(conditions=("nominal", "hot"))
    par_battery.workers = 2
    par = evaluate(cfg, cal, par_battery)
    assert par.metrics == seq.metrics and par.stress == seq.stress


def _card(margins: dict, knock=(0.0, 1.0), violations=()) -> Scorecard:
    return Scorecard("h", "e", "test", {"mean_power_kw": 100.0}, {}, {}, margins,
                     {"nominal_deg": knock[0], "adverse_deg": knock[1]}, list(violations), [], 0.9, {}, {},
                     {"required_margin_pct": 3.0}, 0.0)


def test_margin_floor_and_judge_semantics():
    base = _card({"egt_c": 0.9, "turbo_krpm": 20.0})
    assert margin_floor(base, "turbo_krpm", 3.0, 0.5) == 3.0  # normal: the required margin
    assert margin_floor(base, "egt_c", 3.0, 0.5) == pytest.approx(0.4)  # already tight: no worse than baseline
    assert margin_floor(_card({"egt_c": -2.0}), "egt_c", 3.0, 0.5) == 0.0  # never below the limit itself
    ok = judge(_card({"egt_c": 0.6, "turbo_krpm": 10.0}), base)
    assert ok.passed and ok.inherited  # tight but inherited from the baseline
    assert not judge(_card({"egt_c": 0.3, "turbo_krpm": 10.0}), base).passed
    assert not judge(_card({"egt_c": 0.9, "turbo_krpm": 2.0}), base).passed
    assert not judge(_card({"egt_c": 0.9, "turbo_krpm": 20.0}, knock=(0.0, 2.5)), base).passed
    assert not judge(_card({"egt_c": 0.9, "turbo_krpm": 20.0}, violations=["DTCs in 'hot': P0299"]), base).passed


# ---------------------------------------------------------------------------------------- response model
def test_response_model_learns_a_quadratic_response():
    actions = ["torque.mid", "lambda.high"]
    model = ResponseModel(actions, engineering_prior(actions))
    truth = {"mean_power_kw": lambda t, lam: 1.2 * t - 0.3 * lam - 0.25 * lam * lam}
    for t, lam in [(1, 0), (0, 1), (0, -1), (2, 0), (0, 2), (1, -2), (-1, 1)]:
        y = {k: 0.0 for k in OUTPUTS}
        y["mean_power_kw"] = truth["mean_power_kw"](t, lam)
        model.add({"torque.mid": t, "lambda.high": lam}, y)
    m, s = model.predict({"torque.mid": 1.5, "lambda.high": -1.0}, raw=True)["mean_power_kw"]
    assert m == pytest.approx(truth["mean_power_kw"](1.5, -1.0), abs=0.05)
    assert s < 0.15
    slope, _ = model.coefficient("torque.mid", "mean_power_kw")
    curv, _ = model.coefficient("lambda.high", "mean_power_kw", curvature=True)
    assert slope == pytest.approx(1.2, abs=0.05) and curv == pytest.approx(-0.25, abs=0.05)
    # prediction between two calibrations is anchored on the reference: a zero move predicts zero change
    assert model.predict({"torque.mid": 1.0}, {"torque.mid": 1.0})["mean_power_kw"][0] == 0.0


def test_knowledge_base_prior_caution_and_surprises(tmp_path):
    kb = KnowledgeBase(tmp_path / "kb.sqlite")
    actions = ["torque.mid", "spark.mid"]
    fresh = kb.prior(actions, "b48-inspired")
    assert set(fresh.source.values()) == {"engineering prior"}
    sid = kb.new_session("b48-inspired", "b48-inspired", "max_power", {})
    for i, (t, sp) in enumerate([(3.0, 0.0), (0.0, 1.0), (6.0, 0.0), (0.0, -1.0)]):
        delta = {k: 0.0 for k in OUTPUTS}
        delta["mean_torque_nm"] = 0.6 * t
        pred = {"z_raw": {"mean_torque_nm": 4.0}}  # badly over-confident earlier predictions
        kb.add_trial(sid, i, "b48-inspired", "probe", {}, {"torque.mid": t, "spark.mid": sp}, delta, pred, 0.0,
                     False, True, [], "quick")
    kb.add_mistake(sid, 2, "b48-inspired", "constraint_surprise", ["torque.mid"], "verdict", "x")
    learned = kb.prior(actions, "b48-inspired")
    assert learned.source["torque.mid"].startswith("engine")
    i = actions.index("torque.mid")
    assert learned.mean["mean_torque_nm"][i] == pytest.approx(1.8, abs=0.1)  # 0.6 %/% × 3 %/unit
    assert math.sqrt(learned.cov["mean_torque_nm"][i, i]) < math.sqrt(fresh.cov["mean_torque_nm"][i, i])
    # same family, different engine: weaker transfer
    fam = kb.prior(actions, "b48-inspired~h9")
    assert fam.source["torque.mid"].startswith("family")
    assert fam.cov["mean_torque_nm"][i, i] > learned.cov["mean_torque_nm"][i, i]
    # other fidelity: not used
    assert kb.prior(actions, "b48-inspired", "standard").source["torque.mid"] == "engineering prior"
    assert kb.caution()["mean_torque_nm"] == pytest.approx(4.0)
    assert kb.surprises("b48-inspired") == {"torque.mid": 1}
    summary = kb.summary()
    assert summary["trials"] == 4 and "synthetic" in summary["note"]
    kb.reset()
    assert kb.trial_count() == 0 and not kb.mistakes()


# ---------------------------------------------------------------------------------------- sessions
@pytest.fixture(scope="module")
def session_runs(b48):
    cfg, base = b48
    kb = KnowledgeBase(":memory:")
    req = TuningRequest(goal="midrange_torque", budget=3, actions=["torque.mid", "spark.mid"], min_gain=0.05)
    battery = mini_battery()
    vbat = mini_battery(("nominal", "hot", "knock_prone"), fidelity="test-validation")
    first = TuningSession(cfg, base, req, kb, battery=battery, validation_battery=vbat).run()
    second = TuningSession(cfg, base, req, kb, battery=mini_battery(), validation_battery=vbat).run()
    return kb, first, second


def test_session_report_structure_and_safety(session_runs, b48):
    _, first, _ = session_runs
    assert first["ready_for_vehicle"] is False and first["summary"]["ready_for_vehicle"] is False
    assert any("not evidence" in d.lower() or "not a flashable" in d.lower() for d in first["disclaimer"])
    phases = [s["phase"] for s in first["steps"]]
    assert phases[0] == "baseline" and phases.count("probe") == 2
    assert not validate_offsets(first["final"]["offsets"])
    for st in first["steps"]:
        if st["phase"] == "search" and st["accepted"]:
            assert st["passed"]
            assert st["predicted"]["safe"]  # only proposals that cleared the safety screen are tried
    assert first["summary"]["evaluations"]["baseline"] == 2  # search + validation batteries
    assert first["floors"] and "iat_c" not in first["floors"]
    assert first["model"]["calibration"]["n"] > 0
    # the recommended calibration is exactly baseline + offsets
    cfg, base = b48
    rec = apply_offsets(base, first["final"]["offsets"])
    assert rec.content_hash() == first["final"]["calibration_hash"]
    assert first["calibration"]["content_hash"] == first["final"]["calibration_hash"]


def test_second_session_reuses_knowledge(session_runs):
    kb, first, second = session_runs
    assert first["learning"]["probes_skipped"] == []
    assert set(second["learning"]["probes_skipped"]) == {"torque.mid", "spark.mid"}
    assert second["summary"]["evaluations"]["probe"] == 0
    assert all(src.startswith("engine") for src in second["model"]["prior_sources"].values())
    assert {s["id"] for s in kb.sessions()} == {first["id"], second["id"]}
    assert kb.session_report(first["id"])["id"] == first["id"]


def test_learning_off_leaves_the_knowledge_base_untouched(b48):
    cfg, base = b48
    kb = KnowledgeBase(":memory:")
    req = TuningRequest(goal="max_power", budget=0, actions=["torque.mid"], validate=False, learn=False)
    report = TuningSession(cfg, base, req, kb, battery=mini_battery(("nominal",))).run()
    assert report["summary"]["evaluations"] == {"baseline": 1, "probe": 1, "repair": 0, "search": 0, "validation": 0}
    assert kb.trial_count() == 0 and not kb.sessions()


def test_request_validation():
    assert TuningRequest(goal="nope").problems()
    assert TuningRequest(actions=["warp.drive"]).problems()
    assert TuningRequest(limits={"max_fun": 1}).problems()
    assert not TuningRequest().problems()
    assert set(GOALS) == {"max_power", "power_band", "midrange_torque", "efficiency", "balanced"}


# ---------------------------------------------------------------------------------------- benchmark
def test_held_out_variants_are_deterministic_and_different():
    nominal = get_preset("b58-inspired")
    a, b = variant_config(nominal, 1), variant_config(nominal, 1)
    assert a.to_dict() == b.to_dict()
    assert a.id == "b58-inspired~h1" and "held-out" in a.name
    assert a.combustion.eta_ig_ch != nominal.combustion.eta_ig_ch
    assert a.geometry.bore == nominal.geometry.bore  # documented values are never perturbed
    fuel = variant_config(nominal, 3, -3.0)
    assert fuel.combustion.octane_aki == nominal.combustion.octane_aki - 3.0
    assert Task("b58-inspired", "max_power", variant=3, octane_delta=-3.0).engine_key == "b58-inspired~h3~aki-3"
    assert any(t.held_out for t in SUITES["quick"]) and not any(t.held_out for t in SUITES["smoke"])
    agg = aggregate([{"task": {"held_out": True}, "validated_gain": 1.0, "validated": True, "evaluations": 10,
                      "constraint_surprises": 0, "validation_failures": 1, "prediction_misses": 2,
                      "coverage_2sigma": 0.9, "seconds": 1.0}])
    assert agg["gain_per_100_evaluations"] == 10.0 and agg["mean_validated_gain_held_out"] == 1.0


# ---------------------------------------------------------------------------------------- safety
def test_tuning_code_cannot_reach_programming_or_hardware():
    """The tuning engine only edits calibration objects in memory; it never imports flashing,
    programming, OBD or serial code."""
    banned = ("opentune.lab.flash", "opentune.ecu", "opentune.obd", "serial", "socket")
    for path in TUNING_DIR.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert not any(name == b or name.startswith(b + ".") for b in banned), f"{path.name}: {name}"


# ---------------------------------------------------------------------------------------- repair
def _egt_limited(b48, below_c: float) -> tuple:
    cfg, base = b48
    battery = mini_battery(("nominal", "hot_knock_prone"))
    egt = evaluate(cfg, base, battery).stress["egt_c"]
    return cfg, base, battery, {"max_egt_c": round(egt - below_c, 1)}


def test_unsafe_baseline_is_repaired_before_tuning(b48):
    cfg, base, battery, limits = _egt_limited(b48, 4.0)
    req = TuningRequest(goal="max_power", budget=4, actions=["torque.high", "torque.mid", "lambda.high", "lambda.mid"],
                        limits=limits, validate=False)
    r = TuningSession(cfg, base, req, KnowledgeBase(":memory:"), battery=battery).run()
    assert r["baseline"]["violations"] and r["diagnosis"][0]["finding"].startswith("the starting calibration exceeds")
    assert r["repair"]["complete"] and r["summary"]["repaired"] and r["summary"]["final_within_limits"]
    assert r["final"]["offsets"].get("torque.high", 0.0) < 0.0  # derated where the exhaust is hottest
    assert r["final"]["score"]["margins_pct"]["egt_c"] >= 0.0
    assert any(s["phase"] == "repair" and s["accepted"] for s in r["steps"])


def test_unrepairable_baseline_recommends_nothing(b48):
    cfg, base, battery, limits = _egt_limited(b48, 150.0)
    req = TuningRequest(goal="max_power", budget=2, actions=["spark.low"], limits=limits, validate=False)
    r = TuningSession(cfg, base, req, KnowledgeBase(":memory:"), battery=battery).run()
    assert not r["repair"]["complete"] and not r["summary"]["repaired"]
    assert r["final"]["offsets"] == {} and r["summary"]["changes"] == []
    assert "nothing is recommended" in r["stop_reason"]
    assert r["ready_for_vehicle"] is False
