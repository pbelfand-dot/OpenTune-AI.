"""Physical constants and fuel properties (with provenance).

Values are typical published figures; real fuels vary by blend, season and
region. Sources are listed per entry and summarised in docs/MODEL_ASSUMPTIONS.md.
"""

from __future__ import annotations

from dataclasses import dataclass

from opentune.lab.provenance import Provenance

# Dry air near 300 K
R_AIR = 287.05  # J/(kg·K)
CP_AIR = 1005.0  # J/(kg·K)
GAMMA_AIR = 1.4

# Hot exhaust gas — representative values used in mean-value engine models.
R_EXHAUST = 287.0  # J/(kg·K)
CP_EXHAUST = 1150.0  # J/(kg·K)
GAMMA_EXHAUST = 1.33

# Effective ratio of specific heats for the closed cycle (Otto efficiency factor).
GAMMA_CYCLE = 1.30

STANDARD_PRESSURE = 101_325.0  # Pa
STANDARD_TEMPERATURE = 298.15  # K (25 °C, SAE J1349 reference temperature)

CONSTANT_SOURCES = {
    "R_AIR": "Standard dry-air gas constant (R_u / M_air = 8.314 / 0.02897)",
    "CP_AIR": "Dry air at ~300 K (engineering tables)",
    "GAMMA_AIR": "Dry air at ~300 K",
    "CP_EXHAUST": "Modelling choice: c_p = γR/(γ−1) ≈ 1.16 kJ/(kg·K) for γ = 1.33",
    "GAMMA_EXHAUST": "Burned-gas γ spans 1.25–1.40 with temperature and λ; 1.33 is a common turbine-side value",
    "GAMMA_CYCLE": "Effective γ for fuel–air cycle efficiency estimates (assumption, ~1.3)",
}


@dataclass(frozen=True)
class FuelProperties:
    key: str
    name: str
    lower_heating_value: float  # J/kg
    stoich_afr: float  # kg air / kg fuel
    density: float  # kg/m³ (liquid)
    heat_of_vaporization: float  # J/kg
    octane_aki: float  # anti-knock index (R+M)/2 used by the knock model
    provenance: Provenance
    source: str

    @property
    def density_kg_per_l(self) -> float:
        return self.density / 1000.0


FUELS: dict[str, FuelProperties] = {
    "gasoline_e0": FuelProperties(
        "gasoline_e0", "Gasoline (E0)", 43.2e6, 14.6, 745.0, 306e3, 93.0,
        Provenance.DOCUMENTED,
        "Concawe report 13-2014 (LHV 43.2 MJ/kg, AFR 14.57); IEA-AMF (AFR 14.6); EN 228 density "
        "720–775 kg/m³. Published LHVs span 42.8–44.2 MJ/kg and AFRs 14.57–14.7.",
    ),
    "gasoline_e10": FuelProperties(
        "gasoline_e10", "Gasoline (E10)", 41.5e6, 14.0, 750.0, 360e3, 91.0,
        Provenance.ASSUMPTION,
        "Blended from E0 and ethanol properties (calculation, ~10 vol-% ethanol); AFR 14.0–14.1 per "
        "fuel-supplier specifications. No single authoritative table found.",
    ),
    "e85": FuelProperties(
        "e85", "E85 (51–83 % ethanol)", 29.1e6, 9.8, 780.0, 800e3, 105.0,
        Provenance.DOCUMENTED,
        "US DOE AFDC fuel properties (83,950–95,450 Btu/gal → ≈29 MJ/kg); AFR 9.8–10; SG ≈ 0.78. "
        "Composition varies seasonally; octane rating is an assumption.",
    ),
}


def get_fuel(key: str) -> FuelProperties:
    try:
        return FUELS[key]
    except KeyError as exc:
        raise KeyError(f"Unknown fuel {key!r}. Options: {', '.join(FUELS)}") from exc
