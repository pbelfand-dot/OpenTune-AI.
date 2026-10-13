import pytest

from opentune.obd import simulated_client
from opentune.simulation import SimulatedVehicle


@pytest.fixture
def setup(profile):
    vehicle = SimulatedVehicle(profile, "wot_pull")
    client = simulated_client(vehicle, realtime=False)
    yield vehicle, client
    client.close()


def test_supported_pids_and_channels(setup):
    _vehicle, client = setup
    supported = client.supported_pids()
    assert {0x0C, 0x0D, 0x05, 0x0B, 0x33, 0x42} <= supported
    channels = client.available_channels()
    assert "boost" in channels and "rpm" in channels
    assert "boost_target" not in channels  # not available over generic OBD-II


def test_live_values_match_simulator(setup):
    vehicle, client = setup
    vehicle.advance(9.0)
    truth = vehicle.sample().values
    got = client.read_channels(["rpm", "coolant_temp", "maf", "lambda_cmd", "battery_voltage",
                                "fuel_rail_pressure", "boost", "timing_advance"])
    assert got["rpm"] == pytest.approx(truth["rpm"], abs=0.25)
    assert got["coolant_temp"] == pytest.approx(truth["coolant_temp"], abs=1)
    assert got["maf"] == pytest.approx(truth["maf"], abs=0.01)
    assert got["lambda_cmd"] == pytest.approx(truth["lambda_cmd"], abs=1e-4)
    assert got["battery_voltage"] == pytest.approx(truth["battery_voltage"], abs=0.001)
    assert got["fuel_rail_pressure"] == pytest.approx(truth["fuel_rail_pressure"], abs=0.1)
    assert got["timing_advance"] == pytest.approx(truth["timing_advance"], abs=0.5)
    # Boost derives from MAP and baro, each with 1 kPa resolution.
    assert got["boost"] == pytest.approx(truth["boost"], abs=0.02)


def test_vin(setup, profile):
    _vehicle, client = setup
    vin = client.read_vin()
    assert vin is not None and len(vin) == 17 and profile.code in vin
    assert not set(vin) & set("IOQ")  # valid VIN alphabet


def test_dtc_lifecycle(setup):
    vehicle, client = setup
    assert client.read_dtcs().total == 0
    vehicle.inject_fault("boost_leak")
    vehicle.inject_fault("misfire")
    vehicle.advance(30)
    report = client.read_dtcs()
    stored = {d.code for d in report.stored}
    assert "P0299" in stored
    assert any(code.startswith("P030") for code in stored)
    assert report.mil_on
    assert client.monitor_status().dtc_count == len(report.stored)

    client.clear_dtcs()
    report = client.read_dtcs()
    assert report.stored == [] and report.pending == [] and not report.mil_on
    assert {d.code for d in report.permanent} >= {"P0299"}  # Mode 04 cannot clear permanent codes


def test_adapter_voltage(setup):
    _vehicle, client = setup
    assert 11.0 < client.adapter_voltage() < 15.5
