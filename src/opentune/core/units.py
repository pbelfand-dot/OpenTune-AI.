"""Unit conversion helpers.

All telemetry is stored in canonical metric units (see :mod:`opentune.core.models`).
Conversion happens only at display time.
"""

from __future__ import annotations

from enum import Enum


class UnitSystem(str, Enum):
    METRIC = "metric"
    IMPERIAL = "imperial"


KPA_PER_PSI = 6.894757
BAR_TO_PSI = 14.503774
KMH_TO_MPH = 0.621371
GS_TO_LBMIN = 0.132277  # grams/second -> pounds/minute
L_TO_GAL = 0.264172

# canonical unit -> (imperial unit, converter)
_IMPERIAL = {
    "°C": ("°F", lambda v: v * 9.0 / 5.0 + 32.0),
    "bar": ("psi", lambda v: v * BAR_TO_PSI),
    "kPa": ("psi", lambda v: v / KPA_PER_PSI),
    "km/h": ("mph", lambda v: v * KMH_TO_MPH),
    "km": ("mi", lambda v: v * KMH_TO_MPH),
    "g/s": ("lb/min", lambda v: v * GS_TO_LBMIN),
    "L/h": ("gal/h", lambda v: v * L_TO_GAL),
    "Nm": ("lb-ft", lambda v: v * 0.737562),
    "kW": ("hp", lambda v: v * 1.341022),
}


def display_unit(unit: str, system: UnitSystem | str) -> str:
    """Return the unit label used for ``unit`` in the given unit system."""
    if UnitSystem(system) is UnitSystem.IMPERIAL and unit in _IMPERIAL:
        return _IMPERIAL[unit][0]
    return unit


def convert(value: float, unit: str, system: UnitSystem | str) -> float:
    """Convert a canonical value to the requested unit system."""
    if UnitSystem(system) is UnitSystem.IMPERIAL and unit in _IMPERIAL:
        return _IMPERIAL[unit][1](value)
    return value


def celsius_to_fahrenheit(value: float) -> float:
    return value * 9.0 / 5.0 + 32.0


def bar_to_psi(value: float) -> float:
    return value * BAR_TO_PSI
