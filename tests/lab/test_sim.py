"""Closed-loop simulation: plant + sensors + virtual ECU + loads."""

from __future__ import annotations

import json
import math

import pytest

from opentune.lab.calibration import build_baseline
from opentune.lab.physics import get_preset
from opentune.lab.sim import DynoLoad, Simulation

PRESETS = ["b58-inspired", "b48-inspired"]


@pytest.fixture(scope="module")
def baselines():
    return {p: build_baseline(get_preset(p)) for p in PRESETS}


def _started(preset, cal, seed=1):
    sim = Simulation(get_preset(preset), cal.copy(), seed=seed)
    sim.warm_up()
    assert sim.start_engine()
    return sim


@pytest.mark.parametrize("preset", PRESETS)
def test_start_and_idle_settle_on_target(preset, baselines):
    sim = _started(preset, baselines[preset])
    rpms = []
    sim.run(8.0, every=0.05, callback=lambda fr: rpms.append(fr.plant.rpm))
    frame = sim.run(0.05)
    target = frame.ecu.idle_target_rpm
    late = rpms[-40:]  # last 2 s
    assert min(rpms) > 400.0  # never close to stalling
    assert max(late) - min(late) < 30.0
    assert abs(sum(late) / len(late) - target) < 40.0
    assert frame.ecu.state == "RUNNING" and frame.ecu.mode == "idle"
    assert frame.ecu.dtcs == []
    assert 0.97 < frame.plant.lambda_value < 1.03


def test_simulation_is_deterministic_for_a_seed(baselines):
    cal = baselines["b58-inspired"]
    a = _started("b58-inspired", cal, seed=4).run(1.0).flat()
    b = _started("b58-inspired", cal, seed=4).run(1.0).flat()
    assert a == b


@pytest.mark.parametrize("preset", PRESETS)
def test_wot_sweep_matches_reference_rating_order_of_magnitude(preset, baselines):
    """Calibration sanity check against the published rating — NOT a model validation."""
    cfg = get_preset(preset)
    sim = _started(preset, baselines[preset])
    sim.run(1.0)
    load = DynoLoad(target_rpm=1500.0)
    sim.set_load(load)
    sim.driver.pedal = 100.0
    sim.run(2.0)
    best_p = best_t = 0.0
    rpm = 1500.0
    while rpm < cfg.limits.max_rpm - 400:
        rpm += 15.0
        load.target_rpm = rpm
        fr = sim.run(0.05)
        p = fr.plant
        # power uses ω at the start of the step, p.omega is after it: agree to integration error
        assert p.brake_power == pytest.approx(p.brake_torque * p.omega, rel=1e-4)
        best_p, best_t = max(best_p, p.brake_power), max(best_t, p.brake_torque)
    ref = cfg.reference_ratings
    assert best_p / 1000.0 == pytest.approx(ref["power_kw"], rel=0.06)
    assert best_t == pytest.approx(ref["torque_nm"], rel=0.06)
    assert fr.ecu.dtcs == []
    assert "coolant_torque_limit" not in fr.ecu.protections


def test_crank_sensor_loss_stops_engine_and_sets_p0335(baselines):
    sim = _started("b58-inspired", baselines["b58-inspired"])
    sim.run(2.0)
    sim.sensors.inject("crank", "open_circuit")
    fr = sim.run(3.0)
    assert "P0335" in fr.ecu.dtcs
    assert fr.plant.rpm < 50.0
    assert not fr.plant.combusting


def test_lowered_rev_limit_caps_free_rev(baselines):
    cal = baselines["b58-inspired"].copy()
    cal.scalars["rev_limit_rpm"].value = 3000.0
    sim = _started("b58-inspired", cal)
    sim.run(1.0)
    sim.driver.pedal = 100.0
    peak = 0.0
    for _ in range(40):
        peak = max(peak, sim.run(0.05).plant.rpm)
    assert 2900.0 < peak < 3400.0


def test_frames_are_json_safe(baselines):
    sim = Simulation(get_preset("b58-inspired"), baselines["b58-inspired"].copy())
    frames = [sim.step().flat()]  # engine off: no fuel, λ must still be finite
    sim.warm_up()
    sim.start_engine()
    frames.append(sim.run(0.5).flat())
    for f in frames:
        json.dumps(f, allow_nan=False)
        assert all(math.isfinite(v) for v in f.values() if isinstance(v, float))
