"""Driver scenarios that feed pedal/brake inputs into the engine simulator."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from opentune.simulation.engine_model import EngineState


@dataclass
class DriverInput:
    pedal: float = 0.0  # 0-100 %
    brake: float = 0.0  # 0-1
    hold_gear: int | None = None  # lock the transmission in this gear (manual mode)


class Scenario:
    """Base class. ``control`` is called once per simulation step."""

    name = "base"
    description = ""

    def reset(self, rng: random.Random) -> None:
        self.rng = rng

    def control(self, t: float, state: EngineState) -> DriverInput:  # pragma: no cover - abstract
        raise NotImplementedError


def _speed_hold(target_kmh: float, speed_kmh: float, aggressiveness: float = 6.0) -> DriverInput:
    err = target_kmh - speed_kmh
    if err < -6.0:
        return DriverInput(pedal=0.0, brake=min(1.0, -err / 30.0))
    pedal = 6.0 + target_kmh * 0.16 + aggressiveness * err
    return DriverInput(pedal=max(0.0, min(65.0, pedal)))


class IdleScenario(Scenario):
    name = "idle"
    description = "Engine idling in park"

    def control(self, t, state):
        return DriverInput(brake=1.0)


class ManualScenario(Scenario):
    """Pedal and brake are set externally (GUI slider or tests)."""

    name = "manual"
    description = "Pedal controlled by the user"

    def __init__(self) -> None:
        self.pedal = 0.0
        self.brake = 0.0
        self.hold_gear: int | None = None

    def control(self, t, state):
        return DriverInput(self.pedal, self.brake, self.hold_gear)


class CruiseScenario(Scenario):
    name = "cruise"
    description = "Steady highway cruise"

    def __init__(self, target_kmh: float = 110.0) -> None:
        self.target_kmh = target_kmh

    def control(self, t, state):
        return _speed_hold(self.target_kmh, state.speed_kmh)


class WotPullScenario(Scenario):
    """Classic datalog pull: roll in 3rd gear, full throttle to the rev limiter, lift, repeat."""

    name = "wot_pull"
    description = "3rd-gear full-throttle pull to redline (repeats)"

    def __init__(self, pull_gear: int = 3, roll_in_kmh: float = 40.0) -> None:
        self.pull_gear = pull_gear
        self.roll_in_kmh = roll_in_kmh

    def reset(self, rng):
        super().reset(rng)
        self.phase = "idle"
        self.phase_start = 0.0
        self.pull_count = 0

    def _enter(self, phase: str, t: float) -> None:
        self.phase = phase
        self.phase_start = t

    def control(self, t, state):
        elapsed = t - self.phase_start
        if self.phase == "idle":
            if elapsed > 2.0:
                self._enter("roll_in", t)
            return DriverInput(brake=1.0)
        if self.phase == "roll_in":
            if abs(state.speed_kmh - self.roll_in_kmh) < 3.0 and elapsed > 3.0:
                self._enter("settle", t)
            return _speed_hold(self.roll_in_kmh, state.speed_kmh, aggressiveness=4.0)
        if self.phase == "settle":
            if elapsed > 1.0:
                self._enter("pull", t)
            inp = _speed_hold(self.roll_in_kmh, state.speed_kmh, aggressiveness=4.0)
            inp.hold_gear = self.pull_gear
            return inp
        if self.phase == "pull":
            if state.rpm >= state.rev_limit - 150 or elapsed > 15.0:
                self.pull_count += 1
                self._enter("lift", t)
            return DriverInput(pedal=100.0, hold_gear=self.pull_gear)
        if self.phase == "lift":
            if elapsed > 1.5:
                self._enter("slow", t)
            return DriverInput(pedal=0.0, hold_gear=self.pull_gear)
        # "slow": brake back down to the roll-in speed, then pull again
        if state.speed_kmh <= self.roll_in_kmh + 2.0:
            self._enter("roll_in", t)
        return DriverInput(pedal=0.0, brake=0.5)


class CityScenario(Scenario):
    name = "city"
    description = "Stop-and-go city driving"

    def reset(self, rng):
        super().reset(rng)
        self.phase = "stop"
        self.phase_start = 0.0
        self.target = 50.0
        self.phase_len = 4.0
        self.accel_pedal = 35.0

    def control(self, t, state):
        elapsed = t - self.phase_start
        if self.phase == "stop":
            if elapsed > self.phase_len:
                self.phase, self.phase_start = "accel", t
                self.target = self.rng.choice([40.0, 50.0, 60.0])
                self.accel_pedal = self.rng.uniform(28.0, 45.0)
            return DriverInput(brake=1.0)
        if self.phase == "accel":
            if state.speed_kmh >= self.target - 2.0:
                self.phase, self.phase_start = "cruise", t
                self.phase_len = self.rng.uniform(6.0, 14.0)
            return DriverInput(pedal=self.accel_pedal)
        if self.phase == "cruise":
            if elapsed > self.phase_len:
                self.phase, self.phase_start = "brake", t
            return _speed_hold(self.target, state.speed_kmh)
        # brake
        if state.speed_kmh < 0.5:
            self.phase, self.phase_start = "stop", t
            self.phase_len = self.rng.uniform(3.0, 8.0)
        return DriverInput(brake=0.35)


class TrackScenario(Scenario):
    name = "track"
    description = "Repeated full-throttle straights and hard braking"

    def reset(self, rng):
        super().reset(rng)
        self.phase = "launch"

    def control(self, t, state):
        if self.phase == "launch":
            if state.speed_kmh > 190.0:
                self.phase = "brake"
            return DriverInput(pedal=100.0)
        if state.speed_kmh < 80.0:
            self.phase = "launch"
        return DriverInput(brake=0.9)


SCENARIOS: dict[str, type[Scenario]] = {
    cls.name: cls
    for cls in (IdleScenario, CityScenario, CruiseScenario, WotPullScenario, TrackScenario, ManualScenario)
}


def make_scenario(name: str) -> Scenario:
    try:
        return SCENARIOS[name]()
    except KeyError as exc:
        raise KeyError(f"Unknown scenario {name!r}. Options: {', '.join(SCENARIOS)}") from exc
