import copy
import math

import pytest

from opentune.calibration.simcal import default_calibration
from opentune.simulation import (
    FAULTS,
    EngineSimulator,
    SimulatedVehicle,
    SimulatorConfig,
    run_simulation,
)


def _peak(samples, key):
    return max(s.values[key] for s in samples)


def test_deterministic_with_seed(b58):
    a = run_simulation(b58, "city", duration_s=20, rate_hz=10, seed=7)
    b = run_simulation(b58, "city", duration_s=20, rate_hz=10, seed=7)
    assert [s.values for s in a] == [s.values for s in b]


def test_idle_is_stable(profile):
    samples = run_simulation(profile, "idle", duration_s=10, rate_hz=10)
    rpms = [s.values["rpm"] for s in samples[20:]]
    assert all(abs(r - profile.sim.idle_rpm) < 60 for r in rpms)
    assert _peak(samples, "vehicle_speed") == 0
    assert all(s.values["boost"] < -0.3 for s in samples[20:])  # vacuum at idle


def test_wot_pull_reaches_rev_limit_and_boost(profile):
    samples = run_simulation(profile, "wot_pull", duration_s=20, rate_hz=20)
    assert _peak(samples, "rpm") >= profile.sim.rev_limit_rpm - 300
    assert _peak(samples, "rpm") <= profile.sim.rev_limit_rpm + 100
    assert _peak(samples, "boost") == pytest.approx(profile.sim.peak_boost_bar, abs=0.08)
    assert min(s.values["lambda_cmd"] for s in samples) < 0.92  # full-load enrichment


def test_modelled_power_matches_published_output(profile):
    samples = run_simulation(profile, "wot_pull", duration_s=20, rate_hz=20)
    peak_kw = max(s.values["torque_est"] * s.values["rpm"] * 2 * math.pi / 60_000 for s in samples)
    peak_nm = _peak(samples, "torque_est")
    assert peak_kw == pytest.approx(profile.sim.peak_power_kw, rel=0.08)
    assert peak_nm == pytest.approx(profile.sim.peak_torque_nm, rel=0.05)


def test_b58_is_stronger_than_b48(b58, b48):
    big = run_simulation(b58, "wot_pull", duration_s=15, rate_hz=20)
    small = run_simulation(b48, "wot_pull", duration_s=15, rate_hz=20)
    assert _peak(big, "torque_est") > _peak(small, "torque_est")
    assert _peak(big, "maf") > _peak(small, "maf")


def test_values_stay_within_channel_ranges(profile):
    from opentune.core.models import CHANNELS

    for scenario in ("city", "track", "cruise"):
        for s in run_simulation(profile, scenario, duration_s=40, rate_hz=10):
            for key, value in s.values.items():
                ch = CHANNELS[key]
                assert ch.minimum - 1e-6 <= value <= ch.maximum + 1e-6, (scenario, key, value)


@pytest.mark.parametrize(
    ("fault", "check"),
    [
        ("boost_leak", lambda clean, bad: _peak(bad, "boost") < _peak(clean, "boost") - 0.3),
        ("wastegate_stuck", lambda clean, bad: _peak(bad, "boost") > _peak(clean, "boost") + 0.2),
        ("overheat", lambda clean, bad: _peak(bad, "coolant_temp") > 115),
        ("lean", lambda clean, bad: bad[-1].values["stft"] + bad[-1].values["ltft"] > 20),
        ("knock", lambda clean, bad: min(s.values["timing_advance"] for s in bad)
         < min(s.values["timing_advance"] for s in clean) - 2),
        ("low_battery", lambda clean, bad: bad[-1].values["battery_voltage"] < 12.0),
        ("misfire", lambda clean, bad: _peak(bad, "torque_est") < _peak(clean, "torque_est") * 0.9),
    ],
)
def test_faults_change_behaviour(b58, fault, check):
    clean = run_simulation(b58, "wot_pull", duration_s=60, rate_hz=10)
    bad = run_simulation(b58, "wot_pull", duration_s=60, rate_hz=10, faults=(fault,))
    assert check(clean, bad)


def test_unknown_fault_rejected(b58):
    sim = EngineSimulator(b58)
    with pytest.raises(KeyError):
        sim.inject_fault("flux_capacitor")
    assert set(FAULTS) >= {"boost_leak", "overheat", "misfire"}


def test_engine_off(b58):
    vehicle = SimulatedVehicle(b58, "idle")
    vehicle.advance(2)
    vehicle.set_engine_running(False)
    sample = vehicle.advance(10)
    assert sample.values["rpm"] == 0
    assert sample.values["maf"] == 0
    assert sample.values["battery_voltage"] < 13.0


def test_manual_input(b58):
    vehicle = SimulatedVehicle(b58, "idle")
    vehicle.set_manual_input(pedal=60)
    sample = vehicle.advance(6)
    assert vehicle.scenario_name == "manual"
    assert sample.values["vehicle_speed"] > 30


def test_calibration_drives_behaviour(b58):
    tuned = copy.deepcopy(default_calibration(b58))
    boost = tuned.table("boost_target")
    for row in boost.values:
        row[-1] += 0.3  # +0.3 bar at full pedal
    sim_stock = run_simulation(b58, "wot_pull", duration_s=20, rate_hz=20)
    sim_tuned = run_simulation(b58, "wot_pull", duration_s=20, rate_hz=20, calibration=tuned)
    assert _peak(sim_tuned, "boost") > _peak(sim_stock, "boost") + 0.2


def test_invalid_calibration_rejected(b58):
    bad = copy.deepcopy(default_calibration(b58))
    bad.table("boost_target").values[0][0] = 5.0
    sim = EngineSimulator(b58)
    with pytest.raises(ValueError, match="rejected"):
        sim.load_calibration(bad)


def test_step_requires_positive_dt(b58):
    with pytest.raises(ValueError):
        EngineSimulator(b58).step(0)


def test_realtime_sync_uses_clock(b58):
    now = [100.0]
    vehicle = SimulatedVehicle(b58, "idle", clock=lambda: now[0])
    vehicle.sync()
    now[0] += 0.5
    sample = vehicle.sync()
    assert sample.timestamp == pytest.approx(0.5, abs=1e-6)
    now[0] += 100  # long pause is capped to avoid a huge jump
    assert vehicle.sync().timestamp == pytest.approx(1.5, abs=1e-6)


def test_cold_start_warms_up(b58):
    sim = EngineSimulator(b58, "cruise", config=SimulatorConfig(warm_start=False, ambient_c=10))
    first = sim.step(0.1).values["coolant_temp"]
    for _ in range(3000):
        sim.step(0.1)
    assert sim.sample().values["coolant_temp"] > first + 40
