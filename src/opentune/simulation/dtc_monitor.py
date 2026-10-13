"""Simulated OBD-II diagnostic monitors.

Each monitor watches the simulator state and moves a generic SAE DTC through
pending -> confirmed (stored, MIL on) -> permanent, roughly mirroring how
emissions monitors behave. Only generic SAE J2012 P0xxx codes are used —
manufacturer-specific codes are never invented.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from opentune.simulation.engine_model import EngineSimulator, EngineState


@dataclass
class Monitor:
    code: str
    condition: Callable[[EngineState, EngineSimulator], bool]
    pending_after_s: float
    confirm_after_s: float
    active_time: float = 0.0
    passing_time: float = 0.0


@dataclass
class DtcMemory:
    stored: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    permanent: list[str] = field(default_factory=list)

    @property
    def mil_on(self) -> bool:
        return bool(self.stored)


def _misfire_code(state: EngineState, sim: EngineSimulator) -> bool:
    return state.misfire_cylinder is not None and state.engine_running


class DtcMonitorSet:
    PERMANENT_CLEAR_AFTER_S = 10.0

    def __init__(self, simulator: EngineSimulator) -> None:
        self.sim = simulator
        self.memory = DtcMemory()
        self.monitors: list[Monitor] = [
            Monitor("P0299", lambda s, _: s.engine_running and s.boost_target_filtered > 0.3
                    and min(s.boost_target_filtered, s.boost_target_bar) - s.boost_bar > 0.3, 1.5, 3.0),
            Monitor("P0234", lambda s, _: s.engine_running and s.boost_target_bar > 0.2
                    and s.boost_bar - max(s.boost_target_bar, s.boost_target_filtered) > 0.25, 0.5, 1.0),
            Monitor("P0217", lambda s, _: s.coolant_c > 118.0, 1.0, 2.0),
            Monitor("P0171", lambda s, _: s.engine_running and s.stft + s.ltft > 20.0, 2.0, 5.0),
            Monitor("P0562", lambda s, _: s.engine_running and s.battery_v < 11.8, 2.0, 5.0),
            Monitor(self._misfire_dtc(), _misfire_code, 1.0, 3.0),
        ]

    def _misfire_dtc(self) -> str:
        cylinder = min(3, self.sim.profile.cylinders)
        return f"P030{cylinder}"

    def update(self, dt: float) -> None:
        state = self.sim.state
        mem = self.memory
        for mon in self.monitors:
            if mon.condition(state, self.sim):
                mon.active_time += dt
                mon.passing_time = 0.0
                if mon.active_time >= mon.pending_after_s and mon.code not in mem.pending:
                    mem.pending.append(mon.code)
                if mon.active_time >= mon.confirm_after_s:
                    if mon.code not in mem.stored:
                        mem.stored.append(mon.code)
                    if mon.code not in mem.permanent:
                        mem.permanent.append(mon.code)
            else:
                mon.active_time = 0.0
                mon.passing_time += dt
                if (mon.code in mem.permanent and mon.code not in mem.stored
                        and mon.passing_time >= self.PERMANENT_CLEAR_AFTER_S):
                    mem.permanent.remove(mon.code)

    def clear(self) -> None:
        """Mode 04: clear stored and pending DTCs. Permanent DTCs remain (per SAE J1979)."""
        self.memory.stored.clear()
        self.memory.pending.clear()
        for mon in self.monitors:
            mon.active_time = 0.0
            mon.passing_time = 0.0
