"""BMW B58 (B58B30) 3.0 L turbocharged inline-6.

Published figures below were cross-checked against BMW Group PressClub USA,
Toyota Pressroom and Wikipedia (see ``sources``). Simulation parameters are
modelling approximations, not BMW calibration data.
"""

from opentune.engines.profile import EngineProfile, EngineVariant, SimulationParameters

B58 = EngineProfile(
    code="B58",
    family_name="BMW B58B30 3.0L Turbo I6",
    cylinders=6,
    displacement_cc=2998,
    bore_mm=82.0,
    stroke_mm=94.6,
    layout="Inline-6",
    aspiration="Single twin-scroll turbocharger",
    fuel_system="Gasoline direct injection (200 bar Gen1, 350 bar TU)",
    published_redline_rpm=7000,
    variants=(
        EngineVariant(
            name="B58 Gen1",
            applications="F30 340i (US)",
            power_kw=240,  # 320 hp
            torque_nm=450,
            compression_ratio="11.0:1",
            injection_pressure_bar=200,
            source="BMW Group trade press; Wikipedia 'BMW B58'",
        ),
        EngineVariant(
            name="B58 TU (high output)",
            applications="G20 M340i (US)",
            power_kw=285,  # 382 hp
            torque_nm=500,  # 369 lb-ft
            compression_ratio="10.2:1",
            injection_pressure_bar=350,
            source="press.bmwgroup.com/usa T0286917EN_US",
        ),
        EngineVariant(
            name="B58 TU (high output)",
            applications="Toyota GR Supra 3.0 MY2021+ (US)",
            power_kw=285,  # 382 hp
            torque_nm=499,  # 368 lb-ft
            compression_ratio="10.2:1",
            injection_pressure_bar=350,
            source="Toyota Pressroom MY2021 GR Supra release",
        ),
    ),
    sim=SimulationParameters(
        idle_rpm=650,
        rev_limit_rpm=7000,
        peak_boost_bar=1.2,
        boost_taper_bar=0.9,
        spool_start_rpm=1300,
        full_spool_rpm=1900,
        peak_torque_nm=500,
        torque_plateau_rpm=(1800, 5000),
        peak_power_kw=285,
        max_rail_pressure_bar=350,
        vehicle_mass_kg=1750,
        drag_area_m2=0.62,
        rolling_resistance=0.012,
        wheel_radius_m=0.33,
        gear_ratios=(5.000, 3.200, 2.143, 1.720, 1.314, 1.000, 0.822, 0.640),
        final_drive=2.81,
        fuel_tank_l=59.0,
    ),
    notes=(
        "Engine-code naming (M0 / M1 / O1, 'TU', 'Gen2') differs between sources; "
        "OpenTune refers to 'Gen1' and 'TU' only.",
        "Stock boost varies with conditions (the DME targets load/torque); no reputable "
        "published 'normal' boost value exists, so the simulator's boost map is a model.",
        "Generic OBD-II PID 0x0B saturates at 255 kPa absolute (~22 psi boost at sea level).",
    ),
    sources=(
        "https://en.wikipedia.org/wiki/BMW_B58",
        "https://press.bmwgroup.com/usa/article/detail/T0286917EN_US",
        "https://pressroom.toyota.com/",
        "https://www.bmwblog.com/2018/10/31/bmw-b58-tu1-technical-updates-bring-388-horsepower/",
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
