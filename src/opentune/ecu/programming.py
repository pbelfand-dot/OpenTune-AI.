"""Programming session orchestration (simulated ECUs only in v0.1).

Order of operations::

    safety gate -> image validation -> preflight (voltage, engine off)
    -> backup current calibration -> enter programming -> access
    -> erase -> transfer blocks -> verify -> exit

Any failure after erase leaves the ECU in RECOVERY state, exactly as a real
interrupted flash would; :meth:`ProgrammingSession.run` can then be used again
as a recovery flash (backup is skipped because nothing valid can be read).
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from opentune.calibration.library import CalibrationLibrary
from opentune.calibration.simcal import CalibrationFormatError, SimCalibration
from opentune.ecu.interface import EcuError, EcuState, EcuTarget
from opentune.ecu.safety import guard_write

MIN_BATTERY_V = 12.5
BLOCK_SIZE = 256

ProgressCallback = Callable[[float, str], None]


@dataclass
class ProgrammingResult:
    success: bool
    message: str
    steps: list[str] = field(default_factory=list)
    backup_id: str | None = None
    ecu_state: EcuState = EcuState.DEFAULT
    duration_s: float = 0.0


class ProgrammingCancelled(EcuError):
    pass


def validate_image(image: bytes, engine: str) -> list[str]:
    """Return problems that make ``image`` unsafe to flash to a simulated ``engine`` ECU."""
    try:
        cal = SimCalibration.from_bytes(image)
    except CalibrationFormatError as exc:
        return [f"Image is not a valid OTSIM simulator calibration: {exc}"]
    problems = cal.validate()
    if cal.engine != engine:
        problems.append(f"Image is for engine {cal.engine}, target ECU is {engine}")
    return problems


class ProgrammingSession:
    def __init__(self, target: EcuTarget, library: CalibrationLibrary | None = None,
                 engine: str | None = None) -> None:
        self.target = target
        self.library = library
        self.engine = engine or target.identify().engine
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def preflight(self, image: bytes) -> list[str]:
        """Checks that must pass before anything is erased. Empty list = OK."""
        problems = validate_image(image, self.engine)
        voltage = self.target.battery_voltage()
        if voltage < MIN_BATTERY_V:
            problems.append(f"Supply voltage {voltage:.2f} V is below the {MIN_BATTERY_V} V minimum "
                            "(connect a battery maintainer)")
        if self.target.engine_running():
            problems.append("Engine is running — switch the engine off (ignition on) first")
        return problems

    def run(self, image: bytes, progress: ProgressCallback | None = None,
            backup: bool = True) -> ProgrammingResult:
        started = time.monotonic()
        steps: list[str] = []
        notify = progress or (lambda _f, _m: None)

        def log(fraction: float, message: str) -> None:
            steps.append(message)
            notify(fraction, message)

        # 1. Safety gate — raises EcuWriteBlockedError for anything but a simulated ECU.
        guard_write(self.target)
        log(0.0, f"Safety gate passed: {self.target.name} is simulated")

        # 2. Validation and preflight.
        problems = self.preflight(image)
        if problems:
            for p in problems:
                log(0.0, f"Preflight failed: {p}")
            return ProgrammingResult(False, "Preflight checks failed", steps,
                                     ecu_state=self.target.identify().state,
                                     duration_s=time.monotonic() - started)
        log(0.02, "Preflight checks passed (image, voltage, engine off)")

        # 3. Backup.
        backup_id = None
        ident = self.target.identify()
        if backup and self.library is not None and ident.state is not EcuState.RECOVERY:
            current = self.target.read_calibration()
            record = self.library.add_bytes(
                current, f"Backup {ident.software_version}", self.engine, origin="ecu-backup",
                notes=f"Automatic backup before programming {self.target.name}")
            backup_id = record.id
            log(0.05, f"Backed up current calibration ({record.short_hash}) as {record.id}")
        elif ident.state is EcuState.RECOVERY:
            log(0.05, "ECU in recovery mode — skipping backup (recovery flash)")

        sha = hashlib.sha256(image).hexdigest()
        try:
            self.target.enter_programming()
            log(0.07, "Entered programming session")
            self.target.authorize(self.target.request_access())
            log(0.08, "Programming access granted (simulated)")
            self.target.erase()
            log(0.10, "Erased calibration area")
            self.target.prepare_transfer(len(image))
            total = len(image)
            for offset in range(0, total, BLOCK_SIZE):
                if self._cancelled:
                    raise ProgrammingCancelled("Cancelled by user during transfer")
                self.target.write_block(offset, image[offset:offset + BLOCK_SIZE])
                done = min(total, offset + BLOCK_SIZE)
                notify(0.10 + 0.80 * done / total, f"Transferred {done}/{total} bytes")
            log(0.90, f"Transferred {total} bytes")
            self.target.finalize(sha)
            log(0.97, "Verified image hash and structure")
            self.target.exit_programming()
            log(1.0, "Programming complete — ECU reset")
        except EcuError as exc:
            try:
                self.target.exit_programming()
            except EcuError:
                pass
            state = self.target.identify().state
            hint = " ECU needs a recovery flash." if state is EcuState.RECOVERY else ""
            log(1.0, f"FAILED: {exc}.{hint}")
            return ProgrammingResult(False, f"{exc}.{hint}".strip(), steps, backup_id, state,
                                     time.monotonic() - started)
        return ProgrammingResult(True, "Programming complete", steps, backup_id,
                                 self.target.identify().state, time.monotonic() - started)
