import pytest

from opentune.simulation import SimulatedVehicle


@pytest.mark.parametrize("scenario", ["wot_pull", "track", "city", "cruise", "idle"])
def test_no_false_dtcs_on_healthy_engine(profile, scenario):
    vehicle = SimulatedVehicle(profile, scenario)
    vehicle.advance(90)
    memory = vehicle.dtc_memory()
    assert memory.stored == [] and memory.pending == [] and memory.permanent == []


@pytest.mark.parametrize(
    ("fault", "scenario", "code"),
    [
        ("boost_leak", "wot_pull", "P0299"),
        ("wastegate_stuck", "wot_pull", "P0234"),
        ("overheat", "cruise", "P0217"),
        ("lean", "cruise", "P0171"),
        ("low_battery", "idle", "P0562"),
    ],
)
def test_faults_set_expected_codes(b58, fault, scenario, code):
    vehicle = SimulatedVehicle(b58, scenario)
    vehicle.inject_fault(fault)
    vehicle.advance(90)
    memory = vehicle.dtc_memory()
    assert code in memory.stored
    assert memory.mil_on


def test_misfire_code_matches_cylinder_count(b58, b48):
    for profile, code in ((b58, "P0303"), (b48, "P0303")):
        vehicle = SimulatedVehicle(profile, "idle")
        vehicle.inject_fault("misfire")
        vehicle.advance(10)
        assert code in vehicle.dtc_memory().stored


def test_permanent_code_clears_after_repair(b58):
    vehicle = SimulatedVehicle(b58, "idle")
    vehicle.inject_fault("low_battery")
    vehicle.advance(20)
    assert "P0562" in vehicle.dtc_memory().permanent
    vehicle.clear_dtcs()
    assert "P0562" in vehicle.dtc_memory().permanent  # Mode 04 cannot clear permanent codes
    vehicle.advance(10)
    assert "P0562" in vehicle.dtc_memory().stored  # fault still present -> re-detected
    vehicle.clear_fault("low_battery")  # "repair"
    vehicle.advance(5)
    vehicle.clear_dtcs()
    vehicle.advance(30)  # monitor runs and passes
    memory = vehicle.dtc_memory()
    assert "P0562" not in memory.permanent and not memory.stored
