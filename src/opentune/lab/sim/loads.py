"""External loads on the crankshaft: free revving, dynamometer, simple vehicle."""

from __future__ import annotations

import math
from dataclasses import dataclass

from opentune.lab.physics.plant import PlantOutputs

TWO_PI = 2.0 * math.pi


class Load:
    kind = "none"

    def torque(self, out: PlantOutputs | None, omega: float, dt: float) -> float:
        return 0.0

    def airflow(self) -> float:
        """Cooling airflow fraction provided to radiators (0–1)."""
        return 0.2

    def vehicle_speed_kmh(self, omega: float) -> float:
        return 0.0

    def to_dict(self) -> dict:
        return {"kind": self.kind}


class FreeRevLoad(Load):
    """Engine in neutral: only its own inertia."""

    kind = "neutral"


@dataclass
class DynoLoad(Load):
    """Speed-controlled absorber dyno (eddy-current style).

    A PI controller on engine speed computes the absorber torque, so the engine is
    held at ``target_rpm`` (or follows a ramp). Absorbers can only brake (torque ≥ 0)
    unless ``motoring`` is enabled.
    """

    target_rpm: float = 2000.0
    kp: float = 4.0  # N·m per rpm
    ki: float = 30.0  # N·m per rpm·s
    motoring: bool = True
    max_torque: float = 2500.0
    fan_airflow: float = 1.0
    integrator: float = 0.0
    kind = "dyno"

    def torque(self, out, omega, dt):
        rpm = omega * 60.0 / TWO_PI
        error = rpm - self.target_rpm
        self.integrator += self.ki * error * dt
        self.integrator = max(-self.max_torque, min(self.max_torque, self.integrator))
        t = self.kp * error + self.integrator
        if not self.motoring:
            t = max(0.0, t)
        return max(-self.max_torque, min(self.max_torque, t))

    def airflow(self) -> float:
        return self.fan_airflow

    def to_dict(self) -> dict:
        return {"kind": self.kind, "target_rpm": self.target_rpm}


@dataclass
class VehicleLoad(Load):
    """Single-gear vehicle: reflected inertia plus rolling and aerodynamic resistance."""

    mass_kg: float = 1700.0
    gear_ratio: float = 2.143
    final_drive: float = 2.81
    wheel_radius_m: float = 0.33
    drag_area_m2: float = 0.62
    rolling_resistance: float = 0.012
    drivetrain_efficiency: float = 0.88
    speed_ms: float = 0.0
    kind = "vehicle"

    @property
    def ratio(self) -> float:
        return self.gear_ratio * self.final_drive

    def torque(self, out, omega, dt):
        self.speed_ms = omega / self.ratio * self.wheel_radius_m
        v = self.speed_ms
        resist = 0.5 * 1.2 * self.drag_area_m2 * v * v + (self.rolling_resistance * self.mass_kg * 9.81 if v > 0.1
                                                           else 0.0)
        # Reflect vehicle inertia onto the crank: accelerating the car needs J_eq·dω/dt.
        j_eq = self.mass_kg * (self.wheel_radius_m / self.ratio) ** 2
        brake_torque = out.brake_torque if out is not None else 0.0
        # Approximate split: the load torque the engine sees includes road load and the share of
        # net torque spent accelerating the vehicle inertia.
        road = resist * self.wheel_radius_m / self.ratio / self.drivetrain_efficiency
        accel_share = (brake_torque - road) * j_eq / (j_eq + 0.2)
        return road + accel_share

    def airflow(self) -> float:
        return min(1.0, 0.15 + self.speed_ms / 30.0)

    def vehicle_speed_kmh(self, omega: float) -> float:
        return omega / self.ratio * self.wheel_radius_m * 3.6

    def to_dict(self) -> dict:
        return {"kind": self.kind, "gear_ratio": self.gear_ratio, "speed_kmh": self.speed_ms * 3.6}


@dataclass
class TorqueStepLoad(Load):
    """Engine in neutral plus an accessory-style load torque switched on at ``t_on`` (seconds of load time)."""

    step_nm: float = 20.0
    t_on: float = 1.0
    elapsed: float = 0.0
    kind = "torque_step"

    def torque(self, out, omega, dt):
        self.elapsed += dt
        return self.step_nm if self.elapsed >= self.t_on and omega > 1.0 else 0.0

    def to_dict(self) -> dict:
        return {"kind": self.kind, "step_nm": self.step_nm, "t_on": self.t_on}
