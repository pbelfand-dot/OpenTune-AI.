"""Engine profile data model.

An :class:`EngineProfile` holds two clearly separated kinds of data:

* ``variants`` — published reference specifications (power, torque, geometry)
  with the source each figure came from. These are facts about the engine.
* ``sim`` — parameters for OpenTune's *simulation model* (boost targets, vehicle
  mass, gear ratios). These are plausible modelling choices, **not** BMW
  calibration data, and must never be presented as factory values.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass(frozen=True)
class EngineVariant:
    """A published production variant of an engine family."""

    name: str
    applications: str
    power_kw: float
    torque_nm: float
    compression_ratio: str
    injection_pressure_bar: int | None
    source: str


@dataclass(frozen=True)
class SimulationParameters:
    """Inputs for the physics model in :mod:`opentune.simulation`.

    All values are approximations chosen to make the simulator behave
    plausibly. They are NOT factory calibration values.
    """

    idle_rpm: float
    rev_limit_rpm: float
    peak_boost_bar: float
    boost_taper_bar: float  # boost target at the rev limiter
    spool_start_rpm: float
    full_spool_rpm: float
    peak_torque_nm: float
    torque_plateau_rpm: tuple[float, float]
    peak_power_kw: float
    max_rail_pressure_bar: float
    vehicle_mass_kg: float
    drag_area_m2: float
    rolling_resistance: float
    wheel_radius_m: float
    gear_ratios: tuple[float, ...]  # generic 8-speed automatic ratios (model input)
    final_drive: float
    drivetrain_efficiency: float = 0.85
    volumetric_efficiency: float = 0.88
    fuel_tank_l: float = 59.0


@dataclass(frozen=True)
class EngineProfile:
    code: str
    family_name: str
    cylinders: int
    displacement_cc: int
    bore_mm: float
    stroke_mm: float
    layout: str
    aspiration: str
    fuel_system: str
    published_redline_rpm: int | None
    variants: tuple[EngineVariant, ...]
    sim: SimulationParameters
    notes: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    # Conservative application warning thresholds (OpenTune defaults, not BMW limits).
    thresholds: dict[str, float] = field(default_factory=dict)

    @property
    def displacement_l(self) -> float:
        return self.displacement_cc / 1000.0

    def computed_displacement_cc(self) -> float:
        """Displacement computed from bore and stroke (sanity check for profile data)."""
        bore_cm = self.bore_mm / 10.0
        stroke_cm = self.stroke_mm / 10.0
        return math.pi / 4.0 * bore_cm**2 * stroke_cm * self.cylinders

    def max_torque_at(self, rpm: float) -> float:
        """Simulation full-load torque curve (Nm): flat torque plateau, then constant power."""
        s = self.sim
        if rpm <= 0:
            return 0.0
        low, _high = s.torque_plateau_rpm
        if rpm < low:
            # Ramp from ~45% of peak at idle up to the plateau as the turbo spools.
            frac = max(0.0, (rpm - s.idle_rpm) / max(1.0, low - s.idle_rpm))
            torque = s.peak_torque_nm * (0.45 + 0.55 * min(1.0, frac))
        else:
            torque = s.peak_torque_nm
        power_limited = s.peak_power_kw * 1000.0 / (rpm * 2.0 * math.pi / 60.0)
        return min(torque, power_limited)
