"""Virtual ECU: PID, sensors and fault injection, state machine, DTCs, limp strategies."""

from __future__ import annotations

import math

import pytest

from opentune.lab.calibration import build_baseline
from opentune.lab.ecu.controller import VirtualEcu, sensor_list
from opentune.lab.ecu.pid import PID
from opentune.lab.ecu.sensors import (
    FAULT_TYPES,
    SENSOR_SPECS,
    V_VALID_MAX,
    V_VALID_MIN,
    SensorBank,
    ntc_decode,
    ntc_encode,
)
from opentune.lab.physics import get_preset
from opentune.obd.dtc import describe


@pytest.fixture(scope="module")
def b58_cal():
    return build_baseline(get_preset("b58-inspired"))


# ---------------------------------------------------------------------- PID
def _first_order(pid: PID, setpoint: float, tau: float = 0.2, gain: float = 1.0, seconds: float = 5.0,
                 dt: float = 0.001):
    y = 0.0
    for _ in range(int(seconds / dt)):
        u = pid.update(setpoint, y, dt)
        y += (gain * u - y) * dt / tau
    return y


def test_pid_integral_action_removes_steady_state_error():
    assert _first_order(PID(1.0, 0.0), 1.0) == pytest.approx(0.5, abs=0.01)  # P only: e = 1/(1+K)
    assert _first_order(PID(1.0, 5.0), 1.0) == pytest.approx(1.0, abs=0.01)


def test_pid_feedforward_passes_through():
    pid = PID(0.0, 0.0)
    assert pid.update(5.0, 5.0, 0.01, feedforward=42.0) == 42.0


def test_pid_anti_windup_limits_integrator():
    pid = PID(1.0, 10.0, out_min=0.0, out_max=1.0)
    for _ in range(1000):  # unreachable set-point: output saturated for 10 s
        pid.update(100.0, 0.0, 0.01)
    assert pid.saturated
    assert pid.integrator < 1.0  # without anti-windup it would be ~10 000
    # recovers immediately once the error reverses
    assert pid.update(0.0, 1.0, 0.01) < 1.0


def test_pid_derivative_on_measurement_has_no_setpoint_kick():
    pid = PID(0.0, 0.0, kd=1.0)
    pid.update(0.0, 0.0, 0.01)
    assert pid.update(100.0, 0.0, 0.01) == pytest.approx(0.0)  # set-point step: no kick
    assert pid.update(100.0, 1.0, 0.01) < 0.0  # measurement rising → derivative opposes


# ---------------------------------------------------------------------- sensors
def test_ntc_transfer_round_trip():
    for t in (-30.0, 0.0, 25.0, 90.0, 120.0):
        assert ntc_decode(ntc_encode(t)) == pytest.approx(t, abs=1e-6)
    assert ntc_encode(-30) > ntc_encode(90)  # NTC: hotter → lower voltage


def test_linear_sensor_round_trip():
    spec = SENSOR_SPECS["map"]
    assert spec.encode(20) == pytest.approx(0.5)
    assert spec.encode(300) == pytest.approx(4.5)
    assert spec.decode(spec.encode(123.4)) == pytest.approx(123.4)


def _truth(**over):
    base = {"map": 100.0, "boost": 101.0, "iat": 30.0, "ect": 90.0, "oil_temp": 95.0, "tps": 10.0, "pedal": 0.0,
            "lambda": 1.0, "rail_pressure": 200.0, "knock": 0.0, "crank": 2000.0, "cam_intake": 10.0,
            "cam_exhaust": 5.0, "egt": 600.0, "vss": 0.0, "battery": 14.0}
    base.update(over)
    return base


@pytest.mark.parametrize("sid", ["map", "iat", "ect", "tps", "lambda"])
@pytest.mark.parametrize("kind", ["open_circuit", "short_ground", "short_supply"])
def test_circuit_faults_leave_valid_voltage_window(sid, kind):
    bank = SensorBank(seed=1)
    healthy = bank.sample(_truth(), 0.005)[sid]
    assert V_VALID_MIN <= healthy.volts <= V_VALID_MAX
    bank.inject(sid, kind)
    r = bank.sample(_truth(), 0.005)[sid]
    assert not V_VALID_MIN <= r.volts <= V_VALID_MAX
    assert r.fault == kind


def test_lean_wideband_saturates_instead_of_reporting_a_circuit_fault():
    bank = SensorBank(seed=1)
    for _ in range(200):
        r = bank.sample(_truth(**{"lambda": 9.99}), 0.005)["lambda"]
    assert V_VALID_MIN <= r.volts <= V_VALID_MAX
    assert r.value == pytest.approx(1.36, abs=0.02)


def test_offset_stuck_and_drift_faults():
    bank = SensorBank(seed=3)
    bank.inject("map", "offset", 15.0)
    r = bank.sample(_truth(), 0.005)["map"]
    assert r.value == pytest.approx(115.0, abs=2.0)
    bank.clear("map")
    bank.inject("tps", "stuck")
    first = bank.sample(_truth(tps=10.0), 0.005)["tps"].value
    later = bank.sample(_truth(tps=80.0), 0.005)["tps"].value
    assert later == pytest.approx(first)
    bank.clear()
    bank.inject("ect", "drift", 2.0)  # +2 °C per second
    for _ in range(1000):
        r = bank.sample(_truth(), 0.005)["ect"]
    assert r.value - 90.0 == pytest.approx(10.0, abs=1.5)


def test_sensor_bank_rejects_unknown_ids_and_is_deterministic():
    with pytest.raises(KeyError):
        SensorBank().inject("nope", "offset")
    with pytest.raises(KeyError):
        SensorBank().inject("map", "gremlins")
    a = [SensorBank(seed=9).sample(_truth(), 0.005)["map"].value for _ in range(2)]
    assert a[0] == a[1]
    assert set(FAULT_TYPES) >= {"stuck", "open_circuit", "short_ground", "short_supply", "offset", "noise",
                                "drift", "delay", "intermittent"}


def test_sensor_dtcs_have_verified_generic_descriptions():
    for row in sensor_list():
        for code in (row["dtc_low"], row["dtc_high"]):
            if code:
                assert describe(code).description, code
                assert "Unknown" not in describe(code).description


# ---------------------------------------------------------------------- ECU state machine and DTCs
def _readings(rpm=0.0, **over):
    bank = SensorBank(seed=5)
    bank.rng.seed(5)
    t = _truth(crank=rpm, **over)
    return bank.sample(t, 0.005)


def test_state_machine_off_ready_cranking_running(b58_cal):
    ecu = VirtualEcu(b58_cal)
    assert ecu.step(_readings(0), ignition=False, start_request=False).state == "OFF"
    assert ecu.step(_readings(0), ignition=True, start_request=False).state == "READY"
    out = ecu.step(_readings(200), ignition=True, start_request=True)
    assert out.state == "CRANKING" and out.starter
    assert out.spark_advance == pytest.approx(b58_cal.scalar("crank_spark_deg"))
    out = ecu.step(_readings(800), ignition=True, start_request=True)
    assert out.state == "RUNNING" and not out.starter
    out = ecu.step(_readings(100), ignition=True, start_request=False)
    assert out.state == "STALLED"
    assert ecu.step(_readings(0), ignition=True, start_request=False).state == "READY"
    assert ecu.step(_readings(500), ignition=False, start_request=False).state == "SHUTDOWN"


def test_running_outputs_are_finite_and_in_range(b58_cal):
    ecu = VirtualEcu(b58_cal)
    ecu.step(_readings(0), True, False)
    ecu.step(_readings(200), True, True)
    for _ in range(50):
        out = ecu.step(_readings(800, map=35.0, pedal=20.0), True, False)
    for k, v in out.to_dict().items():
        if isinstance(v, float):
            assert math.isfinite(v), k
    assert 0.0 <= out.throttle_cmd <= 100.0
    assert 0.0 <= out.wastegate_cmd <= 100.0
    assert b58_cal.scalar("spark_min_deg") <= out.spark_advance <= b58_cal.scalar("spark_max_deg")


def test_dtc_goes_pending_then_stored_with_freeze_frame(b58_cal):
    ecu = VirtualEcu(b58_cal)
    bank = SensorBank(seed=2)
    bank.inject("ect", "open_circuit")
    ecu.step(bank.sample(_truth(crank=0.0), 0.005), True, False)
    statuses = set()
    for _ in range(300):  # 1.5 s
        ecu.step(bank.sample(_truth(crank=0.0), 0.005), True, False)
        if "P0118" in ecu.dtcs:
            statuses.add(ecu.dtcs["P0118"].status)
    assert statuses == {"pending", "stored"}
    entry = ecu.dtcs["P0118"]
    assert entry.freeze_frame and "ect_c" in entry.freeze_frame
    assert ecu.last.mil
    ecu.clear_dtcs()
    assert ecu.dtcs == {}


def test_map_fault_triggers_limp_and_substitute(b58_cal):
    ecu = VirtualEcu(b58_cal)
    bank = SensorBank(seed=2)
    ecu.step(bank.sample(_truth(crank=0.0), 0.005), True, False)
    ecu.step(bank.sample(_truth(crank=200.0), 0.005), True, True)
    ecu.step(bank.sample(_truth(crank=900.0), 0.005), True, True)
    bank.inject("map", "open_circuit")
    for _ in range(100):
        out = ecu.step(bank.sample(_truth(crank=2500.0, pedal=80.0, tps=20.0), 0.005), True, False)
    assert "P0107" in out.dtcs
    assert out.limp and "substitute:map" in out.protections
    assert out.torque_limit <= b58_cal.scalar("limp_torque_nm") + 1e-9
    assert out.boost_target_kpa <= 101.3 + 1e-6  # no boost in limp mode
    assert 20.0 < out.load_pct < 120.0  # alpha-N substitute gives a plausible load


def test_calibration_reload_changes_behaviour(b58_cal):
    ecu = VirtualEcu(b58_cal)
    cal2 = b58_cal.copy()
    cal2.scalars["rev_limit_rpm"].value = 3000.0
    ecu.reload_calibration(cal2)
    ecu.step(_readings(0), True, False)
    ecu.step(_readings(200), True, True)
    ecu.step(_readings(900), True, True)
    out = ecu.step(_readings(3200, pedal=100.0), True, False)
    assert "rev_limiter" in out.protections and not out.fuel_enable
