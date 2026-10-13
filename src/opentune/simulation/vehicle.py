"""Thread-safe simulated vehicle: engine model + diagnostic monitors + clock.

``SimulatedVehicle`` is what the rest of the application talks to. The GUI
polls it from a worker thread while UI controls (pedal slider, fault toggles)
change it from the main thread, so every public method takes a lock.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from opentune.calibration.simcal import SimCalibration
from opentune.core.models import Sample
from opentune.engines.profile import EngineProfile
from opentune.simulation.dtc_monitor import DtcMemory, DtcMonitorSet
from opentune.simulation.engine_model import EngineSimulator, SimulatorConfig
from opentune.simulation.scenarios import ManualScenario, Scenario

SIMULATED_VIN = {"B58": "SMLTB58DEV0000001", "B48": "SMLTB48DEV0000001"}


class SimulatedVehicle:
    """Owns an :class:`EngineSimulator` and advances it in fixed steps."""

    STEP_S = 0.02

    def __init__(
        self,
        profile: EngineProfile,
        scenario: Scenario | str = "idle",
        calibration: SimCalibration | None = None,
        config: SimulatorConfig | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.profile = profile
        self._lock = threading.RLock()
        self.sim = EngineSimulator(profile, scenario, calibration, config)
        self.monitors = DtcMonitorSet(self.sim)
        self._clock = clock
        self._last_sync: float | None = None
        self.vin = SIMULATED_VIN.get(profile.code, "SMLTSIMDEV0000001")

    # ------------------------------------------------------------------ time
    def advance(self, seconds: float) -> Sample:
        """Advance simulated time by ``seconds`` (deterministic; used by tests and CLI)."""
        with self._lock:
            remaining = seconds
            while remaining > 1e-9:
                dt = min(self.STEP_S, remaining)
                self.sim.step(dt)
                self.monitors.update(dt)
                remaining -= dt
            return self.sim.sample()

    def sync(self) -> Sample:
        """Advance simulated time to match the wall clock (used for live/real-time mode)."""
        with self._lock:
            now = self._clock()
            if self._last_sync is None:
                self._last_sync = now
                return self.sim.sample()
            elapsed = min(now - self._last_sync, 1.0)  # avoid huge jumps after a pause
            self._last_sync = now
            if elapsed > 0:
                return self.advance(elapsed)
            return self.sim.sample()

    def sample(self) -> Sample:
        with self._lock:
            return self.sim.sample()

    # ------------------------------------------------------------------ control
    def set_scenario(self, name: str) -> None:
        with self._lock:
            self.sim.set_scenario(name)

    @property
    def scenario_name(self) -> str:
        with self._lock:
            return self.sim.scenario.name

    def set_manual_input(self, pedal: float, brake: float = 0.0, hold_gear: int | None = None) -> None:
        with self._lock:
            if not isinstance(self.sim.scenario, ManualScenario):
                self.sim.set_scenario("manual")
            scenario = self.sim.scenario
            assert isinstance(scenario, ManualScenario)
            scenario.pedal = pedal
            scenario.brake = brake
            scenario.hold_gear = hold_gear

    def inject_fault(self, fault: str) -> None:
        with self._lock:
            self.sim.inject_fault(fault)

    def clear_fault(self, fault: str) -> None:
        with self._lock:
            self.sim.clear_fault(fault)

    @property
    def faults(self) -> set[str]:
        with self._lock:
            return set(self.sim.faults)

    def set_engine_running(self, running: bool) -> None:
        with self._lock:
            self.sim.set_engine_running(running)

    @property
    def engine_running(self) -> bool:
        with self._lock:
            return self.sim.state.engine_running

    def load_calibration(self, calibration: SimCalibration) -> None:
        with self._lock:
            self.sim.load_calibration(calibration)

    @property
    def calibration(self) -> SimCalibration:
        with self._lock:
            return self.sim.calibration

    # ------------------------------------------------------------------ diagnostics
    def dtc_memory(self) -> DtcMemory:
        with self._lock:
            mem = self.monitors.memory
            return DtcMemory(list(mem.stored), list(mem.pending), list(mem.permanent))

    def clear_dtcs(self) -> None:
        with self._lock:
            self.monitors.clear()
