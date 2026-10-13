"""BMW B48 (B48B20) 2.0 L turbocharged inline-4.

Published figures below were cross-checked against BMW Group PressClub USA,
Toyota Pressroom and Wikipedia (see ``sources``). The B48 shares the B58's
500 cc-per-cylinder modular geometry (82.0 x 94.6 mm). Rev limit and per-variant
compression ratios are not consistently published, so they are not stated as
facts here. Simulation parameters are modelling approximations.
"""

from opentune.engines.profile import EngineProfile, EngineVariant, SimulationParameters

B48 = EngineProfile(
    code="B48",
    family_name="BMW B48B20 2.0L Turbo I4",
    cylinders=4,
    displacement_cc=1998,
    bore_mm=82.0,
    stroke_mm=94.6,
    layout="Inline-4",
    aspiration="Single twin-scroll turbocharger",
    fuel_system="Gasoline direct injection (200 bar Gen1, 350 bar TU)",
    published_redline_rpm=None,  # not consistently published; see notes
    variants=(
        EngineVariant(
            name="B48 (30i output)",
            applications="G20 330i (US)",
            power_kw=190,  # 255 hp
            torque_nm=400,  # 295 lb-ft
            compression_ratio="varies by variant (sources disagree)",
            injection_pressure_bar=None,
            source="press.bmwgroup.com/usa T0285572EN_US",
        ),
        EngineVariant(
            name="B48 (30i output)",
            applications="Toyota GR Supra 2.0 (US)",
            power_kw=190,  # 255 hp
            torque_nm=400,  # 295 lb-ft
            compression_ratio="varies by variant (sources disagree)",
            injection_pressure_bar=None,
            source="Toyota Pressroom MY2021 GR Supra release",
        ),
    ),
    sim=SimulationParameters(
        idle_rpm=700,
        rev_limit_rpm=6500,
        peak_boost_bar=1.3,
        boost_taper_bar=1.0,
        spool_start_rpm=1250,
        full_spool_rpm=1700,
        peak_torque_nm=400,
        torque_plateau_rpm=(1550, 4400),
        peak_power_kw=190,
        max_rail_pressure_bar=350,
        vehicle_mass_kg=1600,
        drag_area_m2=0.60,
        rolling_resistance=0.012,
        wheel_radius_m=0.32,
        gear_ratios=(5.000, 3.200, 2.143, 1.720, 1.314, 1.000, 0.822, 0.640),
        final_drive=2.81,
        fuel_tank_l=59.0,
    ),
    notes=(
        "Compression ratio differs by output level and generation; sources disagree "
        "(9.5:1 to 11.0:1), so OpenTune does not state a single value.",
        "Rev limit is not consistently published for BMW B48 applications; the simulator "
        "uses 6,500 rpm as a modelling assumption.",
        "Some US F30 330i cars were reportedly coded B46; treat engine code from the "
        "vehicle's own identification as authoritative.",
    ),
    sources=(
        "https://en.wikipedia.org/wiki/BMW_B48",
        "https://www.press.bmwgroup.com/usa/article/detail/T0285572EN_US",
        "https://pressroom.toyota.com/",
    ),
    thresholds={
        "coolant_warn": 112.0,
        "coolant_critical": 120.0,
        "oil_warn": 130.0,
        "oil_critical": 140.0,
        "iat_warn": 50.0,
        "iat_critical": 65.0,
        "battery_low": 12.0,
    },
)
