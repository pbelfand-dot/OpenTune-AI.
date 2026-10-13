import copy
import hashlib

import pytest

from opentune.calibration import CalibrationLibrary, default_calibration
from opentune.ecu import (
    REAL_ECU_WRITE_ENABLED,
    EcuState,
    EcuWriteBlockedError,
    ProgrammingSession,
    SimulatedEcu,
    available_modules,
    get_module,
    guard_write,
    register_module,
    safety,
    validate_image,
)
from opentune.ecu.interface import EcuError, EcuTarget
from opentune.simulation import SimulatedVehicle


def test_real_writes_are_disabled():
    assert REAL_ECU_WRITE_ENABLED is False
    assert safety.REAL_ECU_WRITE_ENABLED is False


def test_env_vars_cannot_enable_writes():
    import inspect
    import os
    import subprocess
    import sys

    source = inspect.getsource(safety)
    assert "os.environ" not in source and "getenv" not in source
    env = dict(os.environ, OPENTUNE_ENABLE_REAL_WRITE="1", REAL_ECU_WRITE_ENABLED="true")
    out = subprocess.run(
        [sys.executable, "-c", "from opentune.ecu.safety import REAL_ECU_WRITE_ENABLED as w; print(w)"],
        env=env, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


class PretendRealEcu:
    is_simulated = True  # claiming to be simulated is not enough


class FakeBmwEcu(EcuTarget):
    """A future real-ECU module: must be blocked by the safety gate."""

    name = "Real DME"

    def identify(self):
        raise NotImplementedError

    def read_calibration(self):
        return b""

    def battery_voltage(self):
        return 13.0

    def engine_running(self):
        return False

    def enter_programming(self):
        raise AssertionError("must never be called")

    def request_access(self):
        raise AssertionError("must never be called")

    def authorize(self, token):
        raise AssertionError("must never be called")

    def erase(self):
        raise AssertionError("must never be called")

    def write_block(self, offset, data):
        raise AssertionError("must never be called")

    def finalize(self, expected_sha256):
        raise AssertionError("must never be called")

    def exit_programming(self):
        raise AssertionError("must never be called")


@pytest.mark.parametrize("target", [PretendRealEcu(), object(), None])
def test_guard_blocks_anything_not_simulated(target):
    with pytest.raises(EcuWriteBlockedError, match="disabled"):
        guard_write(target)


def test_programming_session_blocks_real_targets(b58):
    image = default_calibration(b58).to_bytes()
    with pytest.raises(EcuWriteBlockedError):
        ProgrammingSession(FakeBmwEcu(), engine="B58").run(image)


def test_simulated_ecu_passes_guard(b58):
    guard_write(SimulatedEcu(b58))


@pytest.fixture
def rig(tmp_path, profile):
    vehicle = SimulatedVehicle(profile, "idle")
    vehicle.set_engine_running(False)
    ecu = SimulatedEcu(profile, vehicle)
    lib = CalibrationLibrary(tmp_path / "cals")
    return profile, vehicle, ecu, lib


def _tuned_image(profile, delta=0.2):
    cal = copy.deepcopy(default_calibration(profile))
    for row in cal.table("boost_target").values:
        row[-1] += delta
    cal.version = f"SIM-{profile.code}-MOD001"
    return cal.to_bytes()


def test_successful_flash_updates_running_calibration(rig):
    profile, vehicle, ecu, lib = rig
    image = _tuned_image(profile)
    progress = []
    result = ProgrammingSession(ecu, lib).run(image, lambda f, m: progress.append(f))
    assert result.success, result.steps
    assert result.ecu_state is EcuState.DEFAULT
    assert ecu.identify().software_version == f"SIM-{profile.code}-MOD001"
    assert ecu.identify().calibration_sha256 == hashlib.sha256(image).hexdigest()
    assert vehicle.calibration.version == f"SIM-{profile.code}-MOD001"
    assert progress == sorted(progress) and progress[-1] == 1.0
    backup = lib.get(result.backup_id)
    assert backup.origin == "ecu-backup"
    assert lib.load_simcal(backup.id).version == f"SIM-{profile.code}-STOCK"


def test_power_loss_then_recovery(rig):
    profile, _vehicle, ecu, lib = rig
    image = _tuned_image(profile)
    ecu.fail_at_fraction = 0.5
    result = ProgrammingSession(ecu, lib).run(image)
    assert not result.success
    assert result.ecu_state is EcuState.RECOVERY
    assert "recovery" in result.message.lower()
    with pytest.raises(EcuError):
        ecu.read_calibration()
    ecu.fail_at_fraction = None
    recovery = ProgrammingSession(ecu, lib).run(image)
    assert recovery.success
    assert recovery.backup_id is None  # nothing valid to back up in recovery mode
    assert ecu.identify().state is EcuState.DEFAULT


def test_cancel_mid_transfer_leaves_recovery(rig):
    profile, _vehicle, ecu, lib = rig
    session = ProgrammingSession(ecu, lib)

    def progress(fraction, _message):
        if fraction > 0.3:
            session.cancel()

    result = session.run(_tuned_image(profile), progress)
    assert not result.success and "Cancelled" in result.message
    assert ecu.identify().state is EcuState.RECOVERY


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        (lambda v, e: setattr(e, "supply_voltage_override", 11.9), "below"),
        (lambda v, e: v.set_engine_running(True), "Engine is running"),
    ],
)
def test_preflight_failures_do_not_touch_ecu(rig, setup, expected):
    profile, vehicle, ecu, lib = rig
    setup(vehicle, ecu)
    before = ecu.identify().calibration_sha256
    result = ProgrammingSession(ecu, lib).run(_tuned_image(profile))
    assert not result.success
    assert any(expected in step for step in result.steps)
    assert ecu.identify().calibration_sha256 == before
    assert lib.list() == []  # no backup taken either


def test_wrong_engine_and_garbage_images(b58, b48):
    assert validate_image(default_calibration(b48).to_bytes(), "B58")
    assert validate_image(b"\x00" * 64, "B58")
    assert validate_image(default_calibration(b58).to_bytes(), "B58") == []


def test_ecu_refuses_programming_with_engine_running(b58):
    vehicle = SimulatedVehicle(b58, "idle")
    ecu = SimulatedEcu(b58, vehicle)
    with pytest.raises(EcuError):
        ecu.enter_programming()


def test_simulated_access_token_required(b58):
    ecu = SimulatedEcu(b58)
    ecu.enter_programming()
    with pytest.raises(EcuError):
        ecu.erase()
    with pytest.raises(EcuError):
        ecu.authorize("wrong")
    ecu.authorize(ecu.request_access())
    ecu.erase()
    with pytest.raises(EcuError, match="Out-of-order"):
        ecu.write_block(10, b"x")


def test_registry(b58):
    keys = {m.key for m in available_modules()}
    assert {"sim-b58", "sim-b48"} <= keys
    module = get_module("sim-b58")
    assert module.simulated and "program" in module.capabilities
    ecu = module.factory()
    assert isinstance(ecu, SimulatedEcu) and ecu.profile.code == "B58"
    assert [m.key for m in available_modules("B48")] == ["sim-b48"]
    with pytest.raises(ValueError):
        register_module(module)
    with pytest.raises(KeyError):
        get_module("bmw-real")
