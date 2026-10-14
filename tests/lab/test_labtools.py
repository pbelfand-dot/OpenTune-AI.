"""Control lab, optimisation, AI analyst, scenarios A–G and sound synthesis."""

from __future__ import annotations

import io
import json
import time
import wave

import numpy as np
import pytest
from fastapi.testclient import TestClient

from opentune.analysis.local_model import LocalModelClient, LocalModelConfig, LocalModelError
from opentune.lab.analysis import analyze_comparison, analyze_run, answer_without_model, narrate
from opentune.lab.api.app import create_app
from opentune.lab.calibration import CalibrationEditor, build_baseline
from opentune.lab.control import (
    ControllerConfig,
    EngineExperiment,
    Experiment,
    PlantModel,
    fit_fopdt,
    frequency_response,
    run_engine_experiment,
    simc_tuning,
    simulate_loop,
)
from opentune.lab.dyno import DynoTest, run_dyno
from opentune.lab.optimize import OptimizationProblem, candidate_calibration, optimize
from opentune.lab.physics import get_preset
from opentune.lab.scenarios import SCENARIOS, run_scenarios
from opentune.lab.sound import PRESETS, firing_frequency, rev_trace, synthesize, to_wav

FAST = {"rpm_start": 2000.0, "rpm_end": 4500.0, "ramp_rpm_s": 600.0, "settle_s": 1.0}


@pytest.fixture(scope="module")
def b48():
    cfg = get_preset("b48-inspired")
    return cfg, build_baseline(cfg)


# ---------------------------------------------------------------------- control lab
def test_simc_matches_published_rule():
    p = PlantModel("fopdt", gain=2.0, tau=1.0, delay=0.2)
    t = simc_tuning(p)  # τc = θ: Kc = τ / (K (τc + θ)), τI = min(τ, 4 (τc + θ))
    assert t["kp"] == pytest.approx(1.0 / (2.0 * 0.4))
    assert t["ti"] == pytest.approx(1.0)
    assert t["ki"] == pytest.approx(t["kp"] / 1.0)
    assert simc_tuning(PlantModel("integrator", gain=0.5, delay=0.1))["ti"] == pytest.approx(0.8)


def test_simc_loop_has_textbook_robustness():
    p = PlantModel("fopdt", 2.0, 1.0, 0.2)
    t = simc_tuning(p)
    r = simulate_loop(p, ControllerConfig(kp=t["kp"], ki=t["ki"]), Experiment())
    assert r.metrics["overshoot_pct"] < 10.0
    assert abs(r.metrics["steady_state_error"]) < 0.01
    # Skogestad (2003): τc = θ gives a phase margin of about 61° and a gain margin of about 3 (≈ 9.5 dB)
    assert r.frequency["phase_margin_deg"] == pytest.approx(61.0, abs=3.0)
    assert r.frequency["gain_margin_db"] == pytest.approx(9.5, abs=1.0)


def test_integrator_windup_demonstration():
    p = PlantModel("fopdt", 2.0, 1.0, 0.2)
    kw = dict(kp=2.0, ki=4.0, out_min=-0.6, out_max=0.6)
    with_aw = simulate_loop(p, ControllerConfig(**kw), Experiment(), frequency=False)
    without = simulate_loop(p, ControllerConfig(**kw, anti_windup=False), Experiment(), frequency=False)
    assert without.metrics["overshoot_pct"] > with_aw.metrics["overshoot_pct"] + 5.0
    assert any("windup" in n for n in without.notes)


def test_disturbance_noise_delay_and_rate_limit():
    p = PlantModel("sopdt", 1.0, wn=3.0, zeta=0.6, delay=0.05)
    r = simulate_loop(p, ControllerConfig(kp=0.8, ki=1.5, kd=0.02, rate_limit=2.0, sample_time=0.01),
                      Experiment(disturbance=0.3, disturbance_time=4.0, noise_std=0.01, sensor_delay=0.02,
                                 duration=8.0))
    assert r.disturbance_metrics["peak_deviation"] > 0.0
    # rate limit: |Δu| per controller period (10 ms) ≤ 2.0 /s × 0.01 s
    assert np.abs(np.diff(r.control)).max() <= 2.0 * 0.01 + 1e-9
    with pytest.raises(ValueError):
        simulate_loop(PlantModel("magic"), ControllerConfig(), Experiment())


def test_fopdt_identification_recovers_parameters():
    t = np.arange(0.0, 6.0, 0.01)
    y = np.where(t > 1.3, -4.0 * 0.5 * (1 - np.exp(-(t - 1.3) / 0.7)), 0.0)
    y = y + np.random.default_rng(2).normal(0, 0.005, t.size)
    fit = fit_fopdt(t, y, 0.5, 1.0)
    assert fit["gain"] == pytest.approx(-4.0, rel=0.03)
    assert fit["tau"] == pytest.approx(0.7, rel=0.08)
    assert fit["delay"] == pytest.approx(0.3, abs=0.03)
    fr = frequency_response(PlantModel("fopdt", 1.0, 1.0, 0.1), ControllerConfig(kp=1.0, ki=1.0))
    assert len(fr["w"]) == len(fr["phase_deg"]) and fr["phase_margin_deg"] > 0


@pytest.mark.parametrize("kind", ["boost_step", "idle_disturbance", "wastegate_step"])
def test_engine_experiments(b48, kind):
    cfg, cal = b48
    r = run_engine_experiment(cfg, cal, EngineExperiment(kind=kind, duration=3.0))
    assert r["simulated"] and r["series"]
    if kind == "boost_step":
        assert r["metrics"]["target_final"] > 150.0 and r["metrics"]["rise_time_s"] is not None
    elif kind == "idle_disturbance":
        assert not r["metrics"]["stalled"] and r["metrics"]["speed_dip_rpm"] < 0
    else:
        assert r["identified_model"]["gain"] < 0  # opening the wastegate lowers boost
        sc = cal.scalars["boost_kp"]
        assert sc.minimum <= r["suggested_gains"]["boost_kp"] <= sc.maximum
        assert r["final_dtcs"] == []  # monitors suppressed during the actuator test


def test_engine_experiment_rejects_non_gain_scalars(b48):
    cfg, cal = b48
    with pytest.raises(ValueError):
        run_engine_experiment(cfg, cal, EngineExperiment(gains={"rev_limit_rpm": 9000}))


# ---------------------------------------------------------------------- optimisation
def test_optimizer_respects_constraints_and_gate(b48):
    cfg, cal = b48
    res = optimize(cfg, OptimizationProblem(rpm_points=[3000.0, 5000.0], robustness_samples=4))
    assert res.ready_for_vehicle is False and res.validation["status"] == "unvalidated"
    assert res.validation["ready_for_vehicle"] is False
    for p in res.points:
        assert p.success
        assert all(v >= -0.05 for v in p.constraints.values()), p.constraints
        assert p.spark_deg <= p.knock_limit_deg - 2.0 + 0.05  # knock margin
        assert p.egt_c <= cfg.limits.egt_limit_c - 25.0 + 1.0
        assert p.robustness is not None and "any_violation_probability" in p.robustness
    cand, changes = candidate_calibration(cal, res)
    assert "SIMULATION ONLY" in cand.name and cand.metadata["ready_for_vehicle"] is False
    assert cand.validate() == []
    assert any(c["table"] == "torque_limit" for c in changes if not c.get("informational"))


def test_min_bsfc_is_more_efficient_than_max_torque(b48):
    cfg, _ = b48
    a = optimize(cfg, OptimizationProblem(objective="max_torque", rpm_points=[3000.0], robustness_samples=0))
    b = optimize(cfg, OptimizationProblem(objective="min_bsfc", rpm_points=[3000.0], robustness_samples=0))
    assert b.points[0].bsfc_g_kwh < a.points[0].bsfc_g_kwh
    assert b.points[0].torque_nm >= 0.8 * a.points[0].torque_nm - 1.0
    with pytest.raises(ValueError):
        optimize(cfg, OptimizationProblem(objective="max_fun"))


# ---------------------------------------------------------------------- analyst
@pytest.fixture(scope="module")
def runs(b48):
    cfg, cal = b48
    agg = cal.copy("aggressive")
    ed = CalibrationEditor(agg)
    ed.apply_region("boost_max", "add", 45.0)
    ed.apply_region("ign_base", "add", 4.0)
    ed.apply_region("torque_limit", "percent", 30.0)
    base = run_dyno(cfg, cal, DynoTest(**FAST)).runs[0]
    hot = run_dyno(cfg, agg, DynoTest(**FAST)).runs[0]
    return base, hot, cal.diff(agg).to_dict()


def test_analyst_tags_every_statement(b48, runs):
    cfg, _ = b48
    base, hot, diff = runs
    findings = analyze_run(hot, cfg) + analyze_comparison(base, hot, diff)
    kinds = {e.kind for f in findings for e in f.evidence}
    assert kinds <= {"documented", "simulated", "assumption", "speculative"}
    assert "measured" not in kinds  # the lab has no measured data
    assert any(f.id == "knock_retard" for f in findings)
    cmp = next(f for f in findings if f.id == "comparison")
    assert any("Controlled comparison" in e.text for e in cmp.evidence)


def test_analyst_refuses_vehicle_advice_without_model(runs, b48):
    cfg, _ = b48
    text = answer_without_model("Is it safe to flash my car with this?", analyze_run(runs[1], cfg))
    assert "must not be used" in text
    assert "knock" in answer_without_model("tell me about knock", analyze_run(runs[1], cfg)).lower()
    assert narrate([], None)[0] is None


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_llm_narrative_is_local_and_speculative(runs, b48):
    cfg, _ = b48
    seen = {}

    def opener(req, timeout):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data)
        return _FakeResponse(json.dumps({"response": "[simulated] Knock retard appeared."}).encode())

    client = LocalModelClient(LocalModelConfig(model="test"), opener=opener)
    text, status = narrate(analyze_run(runs[1], cfg), client, question="why?")
    assert text.startswith("[simulated]") and "test" in status
    assert seen["url"].startswith("http://127.0.0.1")
    assert "never claim [measured]" in seen["body"]["system"]
    with pytest.raises(LocalModelError):
        LocalModelClient(LocalModelConfig(base_url="https://cloud.example.com"))


# ---------------------------------------------------------------------- scenarios
def test_all_scenarios_registered():
    assert list(SCENARIOS) == ["A", "B", "C", "D", "E", "F", "G"]


@pytest.mark.parametrize("ids", [["A", "C"], ["E", "F"]])
def test_scenarios_pass_quick(b48, ids):
    results = run_scenarios(*b48, ids=ids, quick=True)
    for r in results:
        failed = [(c.name, c.detail) for c in r.checks if c.passed is False]
        assert r.passed, (r.id, failed)
        assert "no claim" in r.disclaimer
        json.dumps(r.to_dict())


def test_scenario_d_heat_stress_reduces_power(b48):
    (r,) = run_scenarios(*b48, ids=["D"], quick=True)
    assert r.passed, [(c.name, c.detail) for c in r.checks if c.passed is False]
    assert r.metrics["loss_vs_25c_baseline_pct"] < 0


# ---------------------------------------------------------------------- sound
def test_sound_follows_firing_frequency():
    assert firing_frequency(750.0, 6) == pytest.approx(37.5)
    assert firing_frequency(750.0, 4) == pytest.approx(25.0)
    t = [0.0, 2.0]
    audio = synthesize(t, [1200.0, 1200.0], [0.5, 0.5], None, 4, "exhaust")
    spec = np.abs(np.fft.rfft(audio[2000:-2000]))
    f = np.fft.rfftfreq(audio[2000:-2000].size, 1 / 22050)
    assert f[np.argmax(spec[1:]) + 1] == pytest.approx(40.0, abs=1.0)  # 1200/60 × 4/2
    assert np.max(np.abs(audio)) <= 1.0


def test_sound_presets_and_wav():
    t, rpm, load, turbo = rev_trace()
    for p in PRESETS:
        data = to_wav(synthesize(t, rpm, load, turbo, 6, p))
        with wave.open(io.BytesIO(data)) as w:
            assert w.getnchannels() == 1 and w.getframerate() == 22050 and w.getnframes() > 22050 * 5
    with pytest.raises(KeyError):
        synthesize(t, rpm, load, turbo, 6, "dubstep")


# ---------------------------------------------------------------------- API
@pytest.fixture(scope="module")
def client(tmp_path_factory):
    with TestClient(create_app(tmp_path_factory.mktemp("tools"))) as c:
        yield c


def _wait(client, jid, timeout=180):
    end = time.time() + timeout
    while time.time() < end:
        j = client.get(f"/api/jobs/{jid}").json()
        if j["status"] not in ("queued", "running"):
            return j
        time.sleep(0.05)
    raise AssertionError("timeout")


def test_api_lab_tools(client):
    pid = client.get("/api/projects").json()[0]["id"]
    assert client.get("/api/control/meta").json()["plant_kinds"] == ["fopdt", "sopdt", "integrator"]
    loop = client.post("/api/control/loop", json={"plant": {"gain": 2, "tau": 1, "delay": 0.2},
                                                  "controller": {"kp": 1.25, "ki": 1.25}}).json()
    assert loop["frequency"]["phase_margin_deg"] > 50
    assert client.post("/api/control/loop", json={"plant": {"bogus": 1}}).status_code == 400
    j = _wait(client, client.post("/api/optimize", json={"project_id": pid, "problem": {
        "rpm_points": [3000], "robustness_samples": 2}}).json()["id"])
    assert j["status"] == "done" and j["result"]["validation"]["status"] == "unvalidated"
    applied = client.post(f"/api/projects/{pid}/optimize/apply-candidate",
                          json={"report_id": j["result"]["report_id"]}).json()
    assert applied["ready_for_vehicle"] is False
    assert client.get(f"/api/projects/{pid}/calibration").json()["can_undo"]
    j = _wait(client, client.post("/api/scenarios/run", json={"project_id": pid, "ids": ["F"],
                                                              "quick": True}).json()["id"])
    assert j["result"]["passed"]
    assert client.post("/api/scenarios/run", json={"project_id": pid, "ids": ["Z"]}).status_code == 400
    assert len(client.get("/api/scenarios").json()["scenarios"]) == 7
    assert client.put("/api/ai/settings", json={"enabled": True, "url": "http://example.com:11434"}).status_code == 400
    assert client.get("/api/ai/status").json()["enabled"] is False
    j = _wait(client, client.post("/api/dyno/run", json={"project_id": pid, "test": FAST}).json()["id"])
    rid = j["result"]["run_ids"][0]
    a = client.post("/api/ai/analyze", json={"project_id": pid, "run_ids": [rid]}).json()
    assert a["narrative"] is None and a["findings"] and "Nothing here is a measurement" in a["disclaimer"]
    ask = client.post("/api/ai/ask", json={"project_id": pid, "run_ids": [rid], "question": "power?"}).json()
    assert ask["provenance"] == "rule-based"
    w = client.get(f"/api/dyno/runs/{rid}/sound.wav")
    assert w.status_code == 200 and w.content[:4] == b"RIFF"
    assert client.get("/api/sound/rev.wav?cylinders=7").status_code == 400
