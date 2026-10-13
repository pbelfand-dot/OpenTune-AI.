"""PID controller with feed-forward, output saturation and anti-windup.

Derivative acts on the measurement (no derivative kick on set-point steps) and is
low-pass filtered. Integration is conditional: the integrator does not grow
while the output is saturated in the same direction (anti-windup).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PID:
    kp: float
    ki: float
    kd: float = 0.0
    out_min: float = float("-inf")
    out_max: float = float("inf")
    derivative_tau: float = 0.02
    anti_windup: bool = True  # False only for control-lab demonstrations of integrator windup
    integrator: float = 0.0
    _prev_measurement: float | None = None
    _d_state: float = 0.0
    last_output: float = 0.0
    saturated: bool = False

    def reset(self, integrator: float = 0.0) -> None:
        self.integrator = integrator
        self._prev_measurement = None
        self._d_state = 0.0
        self.saturated = False

    def update(self, setpoint: float, measurement: float, dt: float, feedforward: float = 0.0) -> float:
        error = setpoint - measurement
        if self._prev_measurement is None or dt <= 0.0:
            d_raw = 0.0
        else:
            d_raw = -(measurement - self._prev_measurement) / dt
        self._prev_measurement = measurement
        alpha = dt / (self.derivative_tau + dt) if dt > 0 else 1.0
        self._d_state += alpha * (d_raw - self._d_state)
        unsat = feedforward + self.kp * error + self.integrator + self.kd * self._d_state
        out = min(self.out_max, max(self.out_min, unsat))
        self.saturated = out != unsat
        # Conditional integration (anti-windup): skip increments that push further into saturation.
        increment = self.ki * error * dt
        if not self.anti_windup or not ((out >= self.out_max and increment > 0.0)
                                        or (out <= self.out_min and increment < 0.0)):
            self.integrator += increment
        self.last_output = out
        return out
