"""Atmospheric correction factors for dyno results.

Correction factors scale observed power to reference atmospheric conditions so
runs made in different weather can be compared. The formulas below are the
commonly published forms. They were checked against secondary references only
(the standard texts are not freely available), so verify them against the
official standard before using the numbers for anything that matters.

All of them were derived for naturally aspirated engines. On a boost-controlled
turbo engine the ECU compensates for air density by itself (it targets an
absolute manifold pressure), so a correction factor over-corrects. The lab
therefore reports corrected figures alongside *uncorrected* ones and marks the
correction as not applicable for turbocharged engines.

Simulation note: the virtual dyno's ambient conditions are exactly known and
repeatable, so in the lab a correction is only useful when comparing runs that
were deliberately simulated in different ambient conditions.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

SECONDARY = "secondary references (not checked against the official standard text)"


@dataclass
class CorrectionStandard:
    id: str
    name: str
    formula: str
    reference: str
    valid_range: str
    source_note: str = SECONDARY


STANDARDS: dict[str, CorrectionStandard] = {s.id: s for s in (
    CorrectionStandard("none", "Uncorrected", "CF = 1", "as tested", "any", "—"),
    CorrectionStandard(
        "sae_j1349", "SAE J1349",
        "CF = 1.176 · (99 / p_d) · √(T / 298) − 0.176   (p_d dry-air pressure, kPa; T in K)",
        "99 kPa dry air, 25 °C", "about 15–35 °C and 90–105 kPa"),
    CorrectionStandard(
        "din_70020", "DIN 70020",
        "CF = (1013 / p) · √(T / 293)   (p total pressure, mbar; T in K)",
        "1013 mbar, 20 °C", "not stated here"),
    CorrectionStandard(
        "iso_1585_si", "ISO 1585 / UN R85 (spark ignition)",
        "CF = (99 / p_d)^1.2 · (T / 298)^0.6   (p_d dry-air pressure, kPa; T in K)",
        "99 kPa dry air, 25 °C", "CF commonly limited to about 0.93–1.07"),
)}


@dataclass
class CorrectionResult:
    standard: str
    factor: float
    applicable: bool
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["formula"] = STANDARDS[self.standard].formula
        d["source_note"] = STANDARDS[self.standard].source_note
        return d


def correction_factor(standard: str, pressure_pa: float, temperature_k: float, vapour_pressure_pa: float = 0.0,
                      turbocharged: bool = False) -> CorrectionResult:
    """Correction factor for observed power at the given ambient conditions."""
    if standard not in STANDARDS:
        raise KeyError(f"Unknown correction standard {standard!r}. Options: {', '.join(STANDARDS)}")
    if pressure_pa <= 0 or temperature_k <= 0:
        raise ValueError("Pressure and temperature must be positive")
    notes: list[str] = []
    p_dry_kpa = (pressure_pa - vapour_pressure_pa) / 1000.0
    t_c = temperature_k - 273.15
    if standard == "none":
        return CorrectionResult(standard, 1.0, True, ["Uncorrected (observed) values."])
    if standard == "sae_j1349":
        cf = 1.176 * (99.0 / p_dry_kpa) * math.sqrt(temperature_k / 298.0) - 0.176
        if not (15.0 <= t_c <= 35.0 and 90.0 <= p_dry_kpa <= 105.0):
            notes.append("Ambient conditions outside the range the correction is intended for.")
    elif standard == "din_70020":
        cf = (1013.0 / (pressure_pa / 100.0)) * math.sqrt(temperature_k / 293.0)
    else:  # iso_1585_si
        cf = (99.0 / p_dry_kpa) ** 1.2 * (temperature_k / 298.0) ** 0.6
        if not 0.93 <= cf <= 1.07:
            notes.append("Correction factor outside the commonly quoted 0.93–1.07 limit.")
    applicable = not turbocharged
    if turbocharged:
        notes.append("Not applicable: these corrections were derived for naturally aspirated engines; a "
                     "boost-controlled turbo engine compensates for air density itself, so the factor "
                     "over-corrects. Compare uncorrected values instead.")
    notes.append(f"Formula from {SECONDARY}.")
    return CorrectionResult(standard, cf, applicable, notes)


def standards_list() -> list[dict]:
    return [asdict(s) for s in STANDARDS.values()]
