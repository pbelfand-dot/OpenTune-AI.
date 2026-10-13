"""In-process simulated ECU for developing programming workflows safely.

The flow (enter programming -> access -> erase -> transfer -> verify -> exit)
is a generic abstraction chosen for OpenTune's simulator. It does NOT model
BMW's real programming sequence, security access or memory layout, and the
"access token" is a random value handed out by the simulator itself — not a
security algorithm.
"""

from __future__ import annotations

import hashlib
import secrets

from opentune.calibration.simcal import CalibrationFormatError, SimCalibration, default_calibration
from opentune.ecu.interface import EcuError, EcuIdentification, EcuState, EcuTarget
from opentune.ecu.safety import register_simulated_target_type
from opentune.engines.profile import EngineProfile
from opentune.simulation.vehicle import SimulatedVehicle


class SimulatedPowerLoss(EcuError):
    """Injected failure: supply interrupted mid-transfer."""


@register_simulated_target_type
class SimulatedEcu(EcuTarget):
    is_simulated = True

    def __init__(self, profile: EngineProfile, vehicle: SimulatedVehicle | None = None,
                 calibration: SimCalibration | None = None) -> None:
        self.profile = profile
        self.vehicle = vehicle
        self.name = f"Simulated {profile.code} ECU"
        cal = calibration or (vehicle.calibration if vehicle else default_calibration(profile))
        self._flash = bytearray(cal.to_bytes())
        self.state = EcuState.DEFAULT
        self._token: str | None = None
        self._authorized = False
        self._buffer = bytearray()
        self.fail_at_fraction: float | None = None  # failure injection for tests/UI
        self.supply_voltage_override: float | None = None
        self._expected_size = 0

    # ------------------------------------------------------------------ status
    def battery_voltage(self) -> float:
        if self.supply_voltage_override is not None:
            return self.supply_voltage_override
        if self.vehicle is not None:
            return float(self.vehicle.sample().values["battery_voltage"])
        return 12.6

    def engine_running(self) -> bool:
        return bool(self.vehicle and self.vehicle.engine_running)

    def identify(self) -> EcuIdentification:
        if self.state is EcuState.RECOVERY:
            software, sha = "RECOVERY (no valid application)", ""
        else:
            data = bytes(self._flash)
            sha = hashlib.sha256(data).hexdigest()
            try:
                software = SimCalibration.from_bytes(data).version
            except CalibrationFormatError:
                software = "UNKNOWN"
        return EcuIdentification(
            target=self.name,
            engine=self.profile.code,
            hardware="OPENTUNE-SIM-HW-01",
            software_version=software,
            calibration_sha256=sha,
            vin=self.vehicle.vin if self.vehicle else "",
            state=self.state,
            is_simulated=True,
        )

    def read_calibration(self) -> bytes:
        if self.state is EcuState.RECOVERY:
            raise EcuError("ECU is in recovery mode; no valid calibration to read")
        return bytes(self._flash)

    # ------------------------------------------------------------------ programming
    def enter_programming(self) -> None:
        if self.engine_running():
            raise EcuError("Refusing to enter programming mode with the engine running")
        self.state = EcuState.PROGRAMMING if self.state is not EcuState.RECOVERY else EcuState.RECOVERY
        self._authorized = False

    def request_access(self) -> str:
        self._token = secrets.token_hex(8)
        return self._token

    def authorize(self, token: str) -> None:
        if self._token is None or not secrets.compare_digest(token, self._token):
            raise EcuError("Simulated access denied")
        self._authorized = True

    def _require_authorized(self) -> None:
        if not self._authorized:
            raise EcuError("Programming access not granted")

    def erase(self) -> None:
        self._require_authorized()
        self._flash = bytearray()
        self._buffer = bytearray()
        self.state = EcuState.RECOVERY  # until a verified image is finalized

    def prepare_transfer(self, size: int) -> None:
        self._require_authorized()
        self._expected_size = size

    def write_block(self, offset: int, data: bytes) -> None:
        self._require_authorized()
        if offset != len(self._buffer):
            raise EcuError(f"Out-of-order block at offset {offset} (expected {len(self._buffer)})")
        if self.fail_at_fraction is not None and self._expected_size:
            if (offset + len(data)) / self._expected_size >= self.fail_at_fraction:
                self._authorized = False
                self.state = EcuState.RECOVERY
                raise SimulatedPowerLoss(f"Simulated power loss at {self.fail_at_fraction:.0%}")
        self._buffer += data

    def finalize(self, expected_sha256: str) -> None:
        self._require_authorized()
        data = bytes(self._buffer)
        if hashlib.sha256(data).hexdigest() != expected_sha256:
            raise EcuError("Verification failed: written image hash mismatch")
        try:
            cal = SimCalibration.from_bytes(data)
        except CalibrationFormatError as exc:
            raise EcuError(f"Verification failed: {exc}") from exc
        if cal.engine != self.profile.code:
            raise EcuError(f"Image is for {cal.engine}, ECU is {self.profile.code}")
        if self.vehicle is not None:
            self.vehicle.load_calibration(cal)
        self._flash = bytearray(data)
        self._buffer = bytearray()
        self.state = EcuState.DEFAULT

    def exit_programming(self) -> None:
        self._authorized = False
        self._token = None
        if self.state is EcuState.PROGRAMMING:
            self.state = EcuState.DEFAULT
