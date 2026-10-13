"""Engine configuration: every parameter the physics model uses, with units,
allowed ranges and provenance.

Values are stored in SI units. :data:`PARAM_SPECS` describes each parameter for
validation and for the Engine Builder UI (display unit + scale, range, group,
description). A preset records which parameters are publicly documented (with a
source); everything else is an engineering assumption.
"""

from __future__ import annotations

import copy
import math
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from typing import Any

from opentune.lab.physics import formulas as f
from opentune.lab.physics.constants import FUELS
from opentune.lab.provenance import Provenance


@dataclass
class Geometry:
    cylinders: int = 6
    layout: str = "inline"
    firing_order: list[int] = field(default_factory=lambda: [1, 5, 3, 6, 2, 4])
    bore: float = 0.082
    stroke: float = 0.0946
    rod_length: float = 0.1445
    compression_ratio: float = 10.2


@dataclass
class Valvetrain:
    intake_centerline_deg: float = 110.0  # crank degrees ATDC (overlap TDC) of peak intake lift
    exhaust_centerline_deg: float = 110.0  # crank degrees BTDC of peak exhaust lift
    intake_duration_deg: float = 250.0
    exhaust_duration_deg: float = 250.0
    intake_vvt_range_deg: float = 70.0  # advance authority of the intake phaser
    exhaust_vvt_range_deg: float = 55.0  # retard authority of the exhaust phaser
    vvt_rate_deg_s: float = 200.0
    ve_peak: float = 0.97
    ve_peak_rpm: float = 4200.0
    ve_curvature: float = 0.012  # VE drop per (1000 rpm)² away from the peak
    ve_min: float = 0.70
    ve_cam_shift_rpm_per_deg: float = -25.0  # intake advance moves the VE peak to lower rpm
    scavenging_gain: float = 0.15


@dataclass
class Intake:
    air_filter_dp_coeff: float = 6000.0  # Pa per (kg/s)²
    throttle_diameter: float = 0.074
    throttle_closed_angle_deg: float = 7.0
    throttle_leak_area: float = 2.0e-6
    throttle_discharge_coeff: float = 0.85
    throttle_time_constant_s: float = 0.025
    intake_manifold_volume: float = 0.004
    boost_volume: float = 0.006
    intercooler_type: str = "air_to_water"
    intercooler_effectiveness: float = 0.80
    intercooler_dp_coeff: float = 12000.0  # Pa per (kg/s)²
    charge_cooler_capacity: float = 18000.0  # J/K (coolant + core of the low-temperature loop)
    charge_cooler_ua: float = 260.0  # W/K loop-to-ambient at full airflow
    charge_cooler_ua_still: float = 60.0  # W/K with no airflow (pump + natural convection)


@dataclass
class Turbo:
    enabled: bool = True
    compressor_diameter: float = 0.050
    compressor_head_max: float = 1.00  # Ψ at zero flow
    compressor_head_slope: float = 4.3  # Ψ = Ψ_max − k·Φ²
    compressor_flow_choke: float = 0.36  # Φ at choke
    compressor_eta_max: float = 0.76
    compressor_phi_opt: float = 0.20
    compressor_eta_curvature: float = 4.0
    turbine_diameter: float = 0.045
    turbine_area: float = 0.47e-3  # effective nozzle area, m²
    turbine_eta_max: float = 0.70
    turbine_bsr_opt: float = 0.70
    wastegate_area_max: float = 1.6e-3
    wastegate_time_constant_s: float = 0.06
    inertia: float = 2.5e-5  # kg·m²
    mechanical_efficiency: float = 0.95
    twin_scroll: bool = True


@dataclass
class Exhaust:
    backpressure_coeff: float = 70000.0  # Pa per (kg/s)² downstream of the turbine
    egt_sensor_time_constant_s: float = 0.6


@dataclass
class FuelSystem:
    fuel: str = "gasoline_e0"
    injection: str = "direct"
    injector_flow_ref: float = 0.0105  # kg/s per injector at the reference pressure
    injector_ref_pressure: float = 200e5  # Pa
    injector_dead_time: float = 0.00025  # s at 14 V
    rail_pressure_max: float = 350e5  # Pa
    rail_pressure_idle: float = 50e5  # Pa


@dataclass
class Combustion:
    octane_aki: float = 93.0
    eta_ig_ch: float = 0.76  # gross indicated efficiency as a fraction of the fuel–air Otto value
    combustion_efficiency: float = 0.98
    spark_efficiency_coeff: float = 3.0e-4  # 1/deg²: η_spark = 1 − C·(θ − θ_MBT)², fitted to cycle.py
    burn_duration_ref_deg: float = 58.0  # Wiebe duration at 2000 rpm, 50 % relative load
    burn_rpm_exponent: float = 0.25
    burn_load_exponent: float = -0.20
    ignition_delay_deg: float = 9.0
    ca50_mbt_deg: float = 8.0  # crank degrees ATDC for maximum brake torque
    wiebe_a: float = 5.0
    wiebe_m: float = 2.0
    lean_limit_lambda: float = 1.45
    knock_base_deg: float = 22.0  # knock-limited advance at 1 bar MAP, 2000 rpm, 40 °C, 91 AKI
    knock_octane_coeff: float = 1.2  # deg per AKI
    knock_pressure_coeff: float = 6.5  # deg per bar of manifold pressure above 1 bar
    knock_iat_coeff: float = 0.18  # deg per K above 40 °C
    knock_coolant_coeff: float = 0.10  # deg per K above 90 °C
    knock_rpm_coeff: float = 1.4  # deg per 1000 rpm above 2000 rpm
    knock_lambda_coeff: float = 12.0  # deg per unit of enrichment below λ = 1
    knock_intensity_gain: float = 0.25  # intensity per degree beyond the knock limit


@dataclass
class Friction:
    fmep_a: float = 0.40e5  # Pa
    fmep_b: float = 0.035e5  # Pa per (m/s)
    fmep_c: float = 0.0012e5  # Pa per (m/s)²
    cold_oil_factor: float = 0.6  # +60 % friction with oil at 20 °C, fading to 0 at 90 °C
    accessory_torque: float = 6.0  # N·m (alternator, pumps)


@dataclass
class Thermal:
    coolant_capacity: float = 60000.0  # J/K
    oil_capacity: float = 18000.0  # J/K
    radiator_ua: float = 900.0  # W/K at full airflow, thermostat open
    radiator_ua_still: float = 250.0  # W/K at zero airflow (fan)
    thermostat_open_c: float = 95.0
    thermostat_full_c: float = 105.0
    wall_heat_fraction: float = 0.50  # share of unconverted fuel energy lost to walls (at 2000 rpm)
    oil_cooler_ua: float = 160.0  # W/K oil ↔ coolant


@dataclass
class Mechanical:
    crank_inertia: float = 0.20  # kg·m² (crank, flywheel, converter)
    starter_stall_torque: float = 110.0  # N·m at the crank
    starter_free_speed_rpm: float = 350.0


@dataclass
class Limits:
    """Modelled operating constraints used to flag results (assumptions, not OEM limits)."""

    max_rpm: float = 7000.0
    egt_limit_c: float = 950.0
    turbo_speed_limit_rpm: float = 230000.0
    peak_pressure_limit_bar: float = 140.0
    iat_limit_c: float = 60.0
    coolant_limit_c: float = 115.0
    oil_limit_c: float = 140.0
    knock_intensity_limit: float = 1.0
    compressor_outlet_limit_c: float = 190.0


SECTIONS: dict[str, type] = {
    "geometry": Geometry,
    "valvetrain": Valvetrain,
    "intake": Intake,
    "turbo": Turbo,
    "exhaust": Exhaust,
    "fuel_system": FuelSystem,
    "combustion": Combustion,
    "friction": Friction,
    "thermal": Thermal,
    "mechanical": Mechanical,
    "limits": Limits,
}


@dataclass
class EngineConfig:
    id: str = "custom"
    name: str = "Custom engine"
    description: str = ""
    geometry: Geometry = field(default_factory=Geometry)
    valvetrain: Valvetrain = field(default_factory=Valvetrain)
    intake: Intake = field(default_factory=Intake)
    turbo: Turbo = field(default_factory=Turbo)
    exhaust: Exhaust = field(default_factory=Exhaust)
    fuel_system: FuelSystem = field(default_factory=FuelSystem)
    combustion: Combustion = field(default_factory=Combustion)
    friction: Friction = field(default_factory=Friction)
    thermal: Thermal = field(default_factory=Thermal)
    mechanical: Mechanical = field(default_factory=Mechanical)
    limits: Limits = field(default_factory=Limits)
    # path -> {"provenance": ..., "source": ...}; unspecified paths are assumptions
    provenance: dict[str, dict[str, str]] = field(default_factory=dict)
    reference_ratings: dict[str, Any] = field(default_factory=dict)

    # ---------------------------------------------------------------- derived values
    @property
    def displacement(self) -> float:
        g = self.geometry
        return f.engine_displacement(g.bore, g.stroke, g.cylinders)

    @property
    def cylinder_volume(self) -> float:
        return f.cylinder_swept_volume(self.geometry.bore, self.geometry.stroke)

    @property
    def fuel(self):
        return FUELS[self.fuel_system.fuel]

    def provenance_of(self, path: str) -> Provenance:
        entry = self.provenance.get(path)
        return Provenance(entry["provenance"]) if entry else Provenance.ASSUMPTION

    # ---------------------------------------------------------------- (de)serialisation
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> EngineConfig:
        config = cls()
        for key in ("id", "name", "description"):
            if key in data:
                setattr(config, key, str(data[key]))
        config.provenance = dict(data.get("provenance", {}))
        config.reference_ratings = dict(data.get("reference_ratings", {}))
        for section, kind in SECTIONS.items():
            values = data.get(section, {})
            current = getattr(config, section)
            known = {fld.name for fld in fields(kind)}
            for key, value in values.items():
                if key in known:
                    setattr(current, key, copy.deepcopy(value))
        return config

    def copy(self) -> EngineConfig:
        return EngineConfig.from_dict(self.to_dict())

    def get(self, path: str) -> Any:
        section, _, name = path.partition(".")
        return getattr(getattr(self, section), name)

    def set(self, path: str, value: Any) -> None:
        section, _, name = path.partition(".")
        obj = getattr(self, section)
        if not hasattr(obj, name):
            raise KeyError(f"Unknown parameter {path!r}")
        setattr(obj, name, value)

    def validate(self) -> list[str]:
        """Return human-readable problems; an empty list means the config is usable."""
        problems: list[str] = []
        for spec in PARAM_SPECS.values():
            value = self.get(spec.path)
            if spec.kind == "choice":
                if value not in spec.choices:
                    problems.append(f"{spec.label}: {value!r} is not one of {spec.choices}")
                continue
            if spec.kind == "bool":
                continue
            if spec.kind == "int" and int(value) != value:
                problems.append(f"{spec.label} must be a whole number")
            shown = value * spec.scale
            if not (spec.minimum <= shown <= spec.maximum):
                problems.append(
                    f"{spec.label} = {shown:g} {spec.unit} is outside {spec.minimum:g}–{spec.maximum:g} {spec.unit}")
        g = self.geometry
        if sorted(g.firing_order) != list(range(1, g.cylinders + 1)):
            problems.append("Firing order must list each cylinder exactly once")
        if g.rod_length <= g.stroke / 2.0 * 1.2:
            problems.append("Connecting rod is too short for the stroke (rod/crank-radius ratio must exceed 1.2)")
        if self.thermal.thermostat_full_c <= self.thermal.thermostat_open_c:
            problems.append("Thermostat full-open temperature must exceed its opening temperature")
        if self.fuel_system.rail_pressure_idle > self.fuel_system.rail_pressure_max:
            problems.append("Idle rail pressure cannot exceed maximum rail pressure")
        return problems


@dataclass(frozen=True)
class ParamSpec:
    path: str
    label: str
    unit: str
    scale: float  # display value = SI value × scale
    minimum: float  # in display units
    maximum: float
    group: str
    description: str
    kind: str = "float"  # float | int | bool | choice
    choices: tuple[str, ...] = ()
    step: float | None = None


def _spec(path, label, unit, scale, lo, hi, description, kind="float", choices=(), step=None):
    return ParamSpec(path, label, unit, scale, lo, hi, path.split(".")[0], description, kind, choices, step)


PARAM_SPECS: dict[str, ParamSpec] = {s.path: s for s in (
    # geometry
    _spec("geometry.cylinders", "Cylinders", "", 1, 1, 12, "Number of cylinders", "int"),
    _spec("geometry.layout", "Layout", "", 1, 0, 0, "Cylinder arrangement", "choice", ("inline", "v", "flat")),
    _spec("geometry.bore", "Bore", "mm", 1000, 60, 110, "Cylinder bore diameter"),
    _spec("geometry.stroke", "Stroke", "mm", 1000, 60, 120, "Piston stroke"),
    _spec("geometry.rod_length", "Connecting rod length", "mm", 1000, 100, 220,
          "Centre-to-centre connecting rod length (affects piston motion and cylinder volume vs crank angle)"),
    _spec("geometry.compression_ratio", "Compression ratio", ":1", 1, 7.0, 15.0, "Geometric compression ratio"),
    # valvetrain
    _spec("valvetrain.intake_centerline_deg", "Intake cam centreline", "° ATDC", 1, 80, 140,
          "Crank angle of peak intake lift (base timing)"),
    _spec("valvetrain.exhaust_centerline_deg", "Exhaust cam centreline", "° BTDC", 1, 80, 140,
          "Crank angle of peak exhaust lift (base timing)"),
    _spec("valvetrain.intake_duration_deg", "Intake duration", "°", 1, 180, 320, "Intake cam duration"),
    _spec("valvetrain.exhaust_duration_deg", "Exhaust duration", "°", 1, 180, 320, "Exhaust cam duration"),
    _spec("valvetrain.intake_vvt_range_deg", "Intake VVT authority", "°", 1, 0, 90, "Intake phaser advance range"),
    _spec("valvetrain.exhaust_vvt_range_deg", "Exhaust VVT authority", "°", 1, 0, 90, "Exhaust phaser retard range"),
    _spec("valvetrain.vvt_rate_deg_s", "VVT slew rate", "°/s", 1, 20, 1000, "Cam phaser rate limit"),
    _spec("valvetrain.ve_peak", "Peak volumetric efficiency", "", 1, 0.6, 1.2, "Base VE at the VE peak (no backpressure)"),
    _spec("valvetrain.ve_peak_rpm", "VE peak speed", "rpm", 1, 1500, 9000, "Engine speed of peak base VE"),
    _spec("valvetrain.ve_curvature", "VE curvature", "1/(krpm)²", 1, 0.0, 0.1, "VE loss per (1000 rpm)² from the peak"),
    _spec("valvetrain.ve_min", "Minimum VE", "", 1, 0.3, 1.0, "Lower clamp for base VE"),
    _spec("valvetrain.ve_cam_shift_rpm_per_deg", "VE peak shift per intake advance", "rpm/°", 1, -100, 100,
          "How intake cam advance moves the VE peak (heuristic)"),
    _spec("valvetrain.scavenging_gain", "Scavenging gain", "", 1, 0.0, 0.6,
          "VE gain from valve overlap when intake pressure exceeds exhaust pressure (heuristic)"),
    # intake
    _spec("intake.air_filter_dp_coeff", "Air filter restriction", "Pa/(kg/s)²", 1, 0, 1e5, "Δp = k·ṁ²"),
    _spec("intake.throttle_diameter", "Throttle bore", "mm", 1000, 30, 110, "Throttle body bore"),
    _spec("intake.throttle_closed_angle_deg", "Throttle closed angle", "°", 1, 0, 15, "Plate angle when closed"),
    _spec("intake.throttle_leak_area", "Throttle leak area", "mm²", 1e6, 0, 50, "Leakage area at closed throttle"),
    _spec("intake.throttle_discharge_coeff", "Throttle discharge coefficient", "", 1, 0.5, 1.0, "C_d"),
    _spec("intake.throttle_time_constant_s", "Throttle actuator time constant", "ms", 1000, 5, 200, "First-order lag"),
    _spec("intake.intake_manifold_volume", "Intake manifold volume", "L", 1000, 0.5, 15, "Volume after the throttle"),
    _spec("intake.boost_volume", "Boost volume", "L", 1000, 0.5, 20,
          "Compressor-to-throttle volume including the charge-air cooler"),
    _spec("intake.intercooler_type", "Charge-air cooler", "", 1, 0, 0, "Cooler type", "choice",
          ("air_to_water", "air_to_air", "none")),
    _spec("intake.intercooler_effectiveness", "Charge-air cooler effectiveness", "", 1, 0.0, 0.98, "ε"),
    _spec("intake.intercooler_dp_coeff", "Charge-air cooler restriction", "Pa/(kg/s)²", 1, 0, 2e5, "Δp = k·ṁ²"),
    _spec("intake.charge_cooler_capacity", "Charge-cooler loop heat capacity", "kJ/K", 1e-3, 1, 200,
          "Thermal mass of the low-temperature loop (heat soak)"),
    _spec("intake.charge_cooler_ua", "Charge-cooler loop UA (airflow)", "W/K", 1, 10, 5000, "Heat rejection at full airflow"),
    _spec("intake.charge_cooler_ua_still", "Charge-cooler loop UA (still air)", "W/K", 1, 0, 2000,
          "Heat rejection with no vehicle airflow"),
    # turbo
    _spec("turbo.enabled", "Turbocharged", "", 1, 0, 1, "Disable for a naturally aspirated engine", "bool"),
    _spec("turbo.compressor_diameter", "Compressor wheel diameter", "mm", 1000, 25, 110, "Exducer diameter"),
    _spec("turbo.compressor_head_max", "Compressor head at zero flow", "", 1, 0.5, 1.6, "Ψ_max"),
    _spec("turbo.compressor_head_slope", "Compressor head slope", "", 1, 0.5, 20, "k in Ψ = Ψ_max − k·Φ²"),
    _spec("turbo.compressor_flow_choke", "Compressor choke flow parameter", "", 1, 0.1, 0.8, "Φ at choke"),
    _spec("turbo.compressor_eta_max", "Compressor peak efficiency", "", 1, 0.5, 0.85, "η_c,max"),
    _spec("turbo.compressor_phi_opt", "Compressor optimum flow parameter", "", 1, 0.05, 0.6, "Φ at peak efficiency"),
    _spec("turbo.compressor_eta_curvature", "Compressor efficiency curvature", "", 1, 0.1, 30, "η drop per Φ²"),
    _spec("turbo.turbine_diameter", "Turbine wheel diameter", "mm", 1000, 25, 110, "Turbine tip diameter"),
    _spec("turbo.turbine_area", "Turbine effective area", "cm²", 1e4, 1, 50, "Effective nozzle area (A/R proxy)"),
    _spec("turbo.turbine_eta_max", "Turbine peak efficiency", "", 1, 0.4, 0.85, "η_t,max"),
    _spec("turbo.turbine_bsr_opt", "Turbine optimum blade speed ratio", "", 1, 0.4, 0.9, "BSR at peak efficiency"),
    _spec("turbo.wastegate_area_max", "Wastegate max area", "cm²", 1e4, 1, 60, "Fully open wastegate area"),
    _spec("turbo.wastegate_time_constant_s", "Wastegate actuator time constant", "ms", 1000, 10, 500, "First-order lag"),
    _spec("turbo.inertia", "Turbo rotor inertia", "kg·cm²", 1e4, 0.05, 5.0, "Polar moment of inertia"),
    _spec("turbo.mechanical_efficiency", "Turbo mechanical efficiency", "", 1, 0.8, 1.0, "Bearing losses"),
    _spec("turbo.twin_scroll", "Twin-scroll turbine", "", 1, 0, 1, "Informational (pulse separation)", "bool"),
    # exhaust
    _spec("exhaust.backpressure_coeff", "Exhaust system restriction", "Pa/(kg/s)²", 1, 0, 5e5,
          "Δp downstream of the turbine = k·ṁ²"),
    _spec("exhaust.egt_sensor_time_constant_s", "EGT sensor time constant", "s", 1, 0.05, 5, "Thermocouple lag"),
    # fuel
    _spec("fuel_system.fuel", "Fuel", "", 1, 0, 0, "Fuel blend", "choice", tuple(FUELS)),
    _spec("fuel_system.injection", "Injection", "", 1, 0, 0, "Injection type", "choice", ("direct", "port")),
    _spec("fuel_system.injector_flow_ref", "Injector static flow", "g/s", 1000, 1, 40, "Per injector at reference pressure"),
    _spec("fuel_system.injector_ref_pressure", "Injector reference pressure", "bar", 1e-5, 2, 500, "Rating pressure"),
    _spec("fuel_system.injector_dead_time", "Injector dead time", "ms", 1000, 0.05, 2.0, "At 14 V"),
    _spec("fuel_system.rail_pressure_max", "Max rail pressure", "bar", 1e-5, 3, 500, "Maximum fuel rail pressure"),
    _spec("fuel_system.rail_pressure_idle", "Idle rail pressure", "bar", 1e-5, 3, 500, "Rail pressure at idle"),
    # combustion
    _spec("combustion.octane_aki", "Fuel octane", "AKI", 1, 80, 115, "(R+M)/2 anti-knock index"),
    _spec("combustion.eta_ig_ch", "Indicated efficiency factor", "", 1, 0.4, 0.95,
          "Gross indicated efficiency as a fraction of fuel–air Otto efficiency"),
    _spec("combustion.combustion_efficiency", "Combustion efficiency", "", 1, 0.8, 1.0, "Fraction of fuel energy released"),
    _spec("combustion.spark_efficiency_coeff", "Spark efficiency curvature", "1/deg²", 1, 1e-5, 2e-3,
          "η_spark = 1 − C·(θ − θ_MBT)²"),
    _spec("combustion.burn_duration_ref_deg", "Burn duration (reference)", "°", 1, 25, 100,
          "Wiebe 0–100 % duration at 2000 rpm, 50 % load"),
    _spec("combustion.burn_rpm_exponent", "Burn duration speed exponent", "", 1, -0.5, 1.0, "Δθ ∝ (N/2000)^k"),
    _spec("combustion.burn_load_exponent", "Burn duration load exponent", "", 1, -1.0, 0.5, "Δθ ∝ (load/0.5)^k"),
    _spec("combustion.ignition_delay_deg", "Ignition delay", "°", 1, 0, 25, "Spark to start of Wiebe heat release"),
    _spec("combustion.ca50_mbt_deg", "CA50 at MBT", "° ATDC", 1, 0, 20, "Combustion phasing for maximum brake torque"),
    _spec("combustion.wiebe_a", "Wiebe a", "", 1, 2, 10, "Wiebe efficiency parameter"),
    _spec("combustion.wiebe_m", "Wiebe m", "", 1, 0.5, 5, "Wiebe form factor"),
    _spec("combustion.lean_limit_lambda", "Lean limit", "λ", 1, 1.1, 2.0, "Lean misfire limit"),
    _spec("combustion.knock_base_deg", "Knock limit base", "° BTDC", 1, 0, 50,
          "Knock-limited spark advance at 1 bar, 2000 rpm, 40 °C IAT, 91 AKI"),
    _spec("combustion.knock_octane_coeff", "Knock limit per octane", "°/AKI", 1, 0, 5, "Higher octane → more advance"),
    _spec("combustion.knock_pressure_coeff", "Knock limit per bar MAP", "°/bar", 1, 0, 20, "Higher charge pressure → less advance"),
    _spec("combustion.knock_iat_coeff", "Knock limit per K IAT", "°/K", 1, 0, 1, "Hotter charge → less advance"),
    _spec("combustion.knock_coolant_coeff", "Knock limit per K coolant", "°/K", 1, 0, 1, "Hotter engine → less advance"),
    _spec("combustion.knock_rpm_coeff", "Knock limit per 1000 rpm", "°/krpm", 1, 0, 5, "Higher speed → more advance"),
    _spec("combustion.knock_lambda_coeff", "Knock limit per unit enrichment", "°", 1, 0, 40, "Richer → more advance"),
    _spec("combustion.knock_intensity_gain", "Knock intensity gain", "1/°", 1, 0.01, 2, "Intensity per degree past the limit"),
    # friction
    _spec("friction.fmep_a", "FMEP constant", "bar", 1e-5, 0, 2, "FMEP = a + b·S̄p + c·S̄p²"),
    _spec("friction.fmep_b", "FMEP linear", "bar/(m/s)", 1e-5, 0, 0.2, "Linear piston-speed term"),
    _spec("friction.fmep_c", "FMEP quadratic", "bar/(m/s)²", 1e-5, 0, 0.02, "Quadratic piston-speed term"),
    _spec("friction.cold_oil_factor", "Cold-oil friction increase", "", 1, 0, 2, "Extra friction at 20 °C oil"),
    _spec("friction.accessory_torque", "Accessory torque", "N·m", 1, 0, 40, "Alternator, pumps, etc."),
    # thermal
    _spec("thermal.coolant_capacity", "Coolant-side heat capacity", "kJ/K", 1e-3, 5, 500, "Coolant + block thermal mass"),
    _spec("thermal.oil_capacity", "Oil-side heat capacity", "kJ/K", 1e-3, 2, 200, "Oil + sump thermal mass"),
    _spec("thermal.radiator_ua", "Radiator UA (airflow)", "W/K", 1, 50, 10000, "Heat rejection at full airflow"),
    _spec("thermal.radiator_ua_still", "Radiator UA (fan only)", "W/K", 1, 10, 5000, "Heat rejection with fan only"),
    _spec("thermal.thermostat_open_c", "Thermostat opening", "°C", 1, 60, 120, "Start of opening"),
    _spec("thermal.thermostat_full_c", "Thermostat fully open", "°C", 1, 65, 130, "Fully open"),
    _spec("thermal.wall_heat_fraction", "Wall heat fraction", "", 1, 0.1, 0.7, "Share of unconverted energy to walls"),
    _spec("thermal.oil_cooler_ua", "Oil-to-coolant UA", "W/K", 1, 0, 2000, "Oil cooler conductance"),
    # mechanical
    _spec("mechanical.crank_inertia", "Rotating inertia", "kg·m²", 1, 0.02, 2.0, "Crank + flywheel/converter"),
    _spec("mechanical.starter_stall_torque", "Starter stall torque", "N·m", 1, 10, 400, "At the crankshaft"),
    _spec("mechanical.starter_free_speed_rpm", "Starter free speed", "rpm", 1, 100, 800, "Crank speed with no load"),
    # limits
    _spec("limits.max_rpm", "Mechanical speed limit", "rpm", 1, 2000, 12000, "Constraint for analysis"),
    _spec("limits.egt_limit_c", "EGT limit", "°C", 1, 600, 1150, "Pre-turbine temperature constraint"),
    _spec("limits.turbo_speed_limit_rpm", "Turbo speed limit", "rpm", 1, 50000, 400000, "Rotor speed constraint"),
    _spec("limits.peak_pressure_limit_bar", "Peak cylinder pressure limit", "bar", 1, 40, 250, "Constraint"),
    _spec("limits.iat_limit_c", "Intake temperature limit", "°C", 1, 20, 120, "Constraint"),
    _spec("limits.coolant_limit_c", "Coolant limit", "°C", 1, 80, 140, "Constraint"),
    _spec("limits.oil_limit_c", "Oil limit", "°C", 1, 80, 170, "Constraint"),
    _spec("limits.knock_intensity_limit", "Knock intensity limit", "", 1, 0.1, 10, "Constraint"),
    _spec("limits.compressor_outlet_limit_c", "Compressor outlet limit", "°C", 1, 100, 250, "Constraint"),
)}


def param_schema() -> list[dict]:
    """JSON-friendly schema for the Engine Builder UI."""
    return [asdict(spec) for spec in PARAM_SPECS.values()]


def _check_specs_cover_config() -> None:  # pragma: no cover - import-time sanity check
    config = EngineConfig()
    for section in SECTIONS:
        obj = getattr(config, section)
        assert is_dataclass(obj)
        for fld in fields(obj):
            path = f"{section}.{fld.name}"
            if path == "geometry.firing_order":
                continue
            assert path in PARAM_SPECS, f"missing ParamSpec for {path}"
    assert math.isfinite(config.displacement)


_check_specs_cover_config()
