"""Synthetic baseline ("stock-like") calibration generator.

The baseline is derived the way a calibration engineer would derive a first
calibration from dyno data — except the "dyno" is OpenTune's own engine model:

* VE table from the model's volumetric-efficiency sub-model at the exhaust
  pressures predicted by the steady-state turbo-matching solver;
* base ignition = min(MBT, knock limit − margin) at reference conditions, with
  an intake-temperature correction table;
* wastegate feed-forward from the steady-state solver;
* torque limits shaped to the engine preset's published reference rating.

Every table is labelled ``provenance = "assumption"``. Nothing here is copied
from, or claims to represent, a BMW DME calibration.
"""

from __future__ import annotations

import math

import numpy as np

from opentune.lab.calibration.tables import Axis, Calibration, Scalar, Table
from opentune.lab.physics.config import EngineConfig
from opentune.lab.physics.constants import R_AIR
from opentune.lab.physics.plant import RHO_REF, EnginePlant
from opentune.lab.physics.steady import steady_operating_point

RPM_16 = [500, 750, 1000, 1250, 1500, 1750, 2000, 2500, 3000, 3500, 4000, 4500, 5000, 5500, 6000, 7000]
RPM_12 = [750, 1000, 1500, 2000, 2500, 3000, 3500, 4000, 4500, 5000, 6000, 7000]
RPM_8 = [750, 1500, 2500, 3500, 4500, 5500, 6500, 7500]
MAP_16 = [15, 25, 35, 45, 55, 70, 85, 100, 115, 130, 150, 170, 190, 210, 235, 260]
LOAD_16 = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 120, 140, 160, 180, 200, 230]
PEDAL_6 = [0, 20, 40, 60, 80, 100]
PEDAL_11 = [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
BOOST_8 = [100, 120, 140, 160, 180, 200, 230, 260]
ECT_8 = [-20, 0, 20, 40, 60, 80, 95, 110]
IAT_8 = [-20, 0, 20, 40, 50, 60, 80, 100]


def _rpm_axis(values: list[float], max_rpm: float) -> list[float]:
    vals = [v for v in values if v <= max_rpm + 1]
    if vals[-1] < max_rpm:
        vals.append(float(max_rpm))
    return [float(v) for v in vals]


def _axis_rpm(values):
    return Axis("rpm", "Engine speed", "rpm", values, 0, 12000, 0)


def _table(tid, name, desc, cat, unit, lo, hi, axes, values, decimals=1) -> Table:
    t = Table(tid, name, desc, cat, unit, lo, hi, axes, np.round(np.asarray(values, dtype=float), 4), decimals)
    problems = t.validate()
    if problems:
        raise ValueError("; ".join(problems))
    return t


def _scalar(sid, name, desc, cat, unit, value, lo, hi, decimals=2) -> Scalar:
    return Scalar(sid, name, desc, cat, unit, float(value), lo, hi, decimals)


def _rated_torque(cfg: EngineConfig, rpm: float) -> float:
    rating = cfg.reference_ratings
    t_max = float(rating.get("torque_nm", 450.0))
    p_max = float(rating.get("power_kw", 250.0)) * 1000.0
    torque = min(t_max, p_max / max(rpm * 2 * math.pi / 60.0, 1.0))
    ramp = min(1.0, max(0.55, 0.55 + 0.45 * (rpm - 1000.0) / 600.0))
    return torque * ramp


def build_baseline(cfg: EngineConfig, name: str | None = None) -> Calibration:
    plant = EnginePlant(cfg)
    max_rpm = cfg.limits.max_rpm
    rpm16 = _rpm_axis(RPM_16, max_rpm)
    rpm12 = _rpm_axis(RPM_12, max_rpm)
    rpm8 = _rpm_axis(RPM_8[:-1], max_rpm)
    baro = 101.325

    # ---- valvetrain targets (intake advance and exhaust retard for low-speed scavenging)
    def vvt_in(rpm, load):
        hi_load = min(1.0, max(0.0, (load - 60.0) / 60.0))
        speed = min(1.0, max(0.0, (5500.0 - rpm) / 3500.0))
        return min(cfg.valvetrain.intake_vvt_range_deg, 40.0 * hi_load * speed + 8.0 * min(1.0, load / 60.0))

    def vvt_ex(rpm, load):
        hi_load = min(1.0, max(0.0, (load - 80.0) / 60.0))
        speed = min(1.0, max(0.0, (4500.0 - rpm) / 3000.0))
        return min(cfg.valvetrain.exhaust_vvt_range_deg, 20.0 * hi_load * speed)

    load8 = [10.0, 30.0, 50.0, 70.0, 90.0, 120.0, 160.0, 220.0]
    vvt_intake = [[vvt_in(r, ld) for ld in load8] for r in rpm8]
    vvt_exhaust = [[vvt_ex(r, ld) for ld in load8] for r in rpm8]

    # ---- lambda target
    def lam(rpm, load):
        if load < 95.0:
            return 1.0
        x = min(1.0, (load - 95.0) / 70.0)
        y = min(1.0, max(0.0, (rpm - 2500.0) / 4000.0))
        return 1.0 - x * (0.10 + 0.05 * y)

    lambda_vals = [[lam(r, ld) for ld in LOAD_16] for r in rpm16]

    # ---- steady-state exhaust pressure estimate for the VE table
    p_em_cache: dict[tuple[float, float], float] = {}

    def p_em_estimate(rpm: float, map_kpa: float) -> float:
        key = (rpm, map_kpa)
        if key not in p_em_cache:
            if cfg.turbo.enabled and map_kpa > baro + 5 and rpm >= 1000:
                sp = steady_operating_point(plant, rpm, map_kpa * 1000.0, lam=lam(rpm, map_kpa))
                p_em_cache[key] = sp.p_em
            else:
                m = 1.0  # small flows: exhaust pressure ≈ ambient + system loss
                p_em_cache[key] = baro * 1000.0 * 1.03 * m
        return p_em_cache[key]

    ve_vals = []
    for r in rpm16:
        row = []
        for mp in MAP_16:
            load_est = mp / baro * 95.0
            ve = plant.volumetric_efficiency(r, mp * 1000.0, p_em_estimate(r, mp), vvt_in(r, load_est),
                                             vvt_ex(r, load_est))
            row.append(min(130.0, max(20.0, ve * 100.0)))
        ve_vals.append(row)

    # ---- base ignition: min(MBT, knock limit − margin) at IAT 40 °C, coolant 90 °C
    ign_vals = []
    for r in rpm16:
        row = []
        for ld in LOAD_16:
            rel = ld / 100.0
            ve_guess = 0.9
            p_im = rel * RHO_REF * R_AIR * 313.15 / ve_guess
            mbt = plant.mbt_advance(r, rel)
            klim = plant.knock_limit(r, p_im, 313.15, 363.15, lam(r, ld))
            row.append(min(max(min(mbt, klim - 2.0), -5.0), 50.0))
        ign_vals.append(row)

    # ---- torque limit (shaped to the published reference rating)
    torque_limit = [_rated_torque(cfg, r) for r in rpm16]

    # ---- maximum boost targets: enough to reach the torque limit, within turbo capability
    bmax_by_rpm = []
    for r in rpm12:
        if not cfg.turbo.enabled or r < 1000:
            bmax_by_rpm.append(baro + 5.0)
            continue
        target = _rated_torque(cfg, r)

        def ok(p_im: float, r: float = r) -> tuple[bool, float]:
            sp = steady_operating_point(plant, r, p_im, lam=lam(r, p_im / 1000.0 / baro * 95.0))
            # Past compressor choke at low pressure ratio is not a boost limit (more boost fixes it).
            reachable = sp.feasible or "choke" in sp.reason
            return reachable and sp.turbo_rpm < 0.9 * cfg.limits.turbo_speed_limit_rpm, sp.brake_torque

        # March up from atmospheric: turbo matching can have a second, high-boost equilibrium
        # at low speed that the engine cannot spool into from below, so stop at the first
        # infeasible point (or once the torque limit is reached), then refine by bisection.
        lo, hi, step = baro * 1000.0, None, 6e3
        p = lo
        while p < 300e3:
            p += step
            feasible, torque = ok(p)
            if not feasible or torque >= target:
                hi = p
                break
            lo = p
        if hi is not None:
            for _ in range(8):
                mid = 0.5 * (lo + hi)
                feasible, torque = ok(mid)
                if feasible and torque < target:
                    lo = mid
                else:
                    hi = mid
            reached_target = ok(hi)[0]
        else:
            reached_target = False
        # Torque-limited: headroom for the controller's boost reserve. Spool-limited: the
        # steady-state limit itself (the turbo only approaches it asymptotically).
        best = hi + 12e3 if reached_target else lo
        bmax_by_rpm.append(min(260.0, best / 1000.0))
    boost_max = [[baro + (b - baro) * min(1.0, p / 70.0) ** 0.8 for p in PEDAL_6] for b in bmax_by_rpm]

    # ---- wastegate feed-forward from the steady-state solver
    wg_vals = []
    for r in rpm12:
        row = []
        for b in BOOST_8:
            if not cfg.turbo.enabled or b <= baro + 8.0 or r < 1000:
                row.append(100.0)
                continue
            p_im = (b - 5.0) * 1000.0
            sp = steady_operating_point(plant, r, p_im, lam=lam(r, b / baro * 95.0))
            row.append(100.0 * sp.wastegate_pos if sp.feasible else 0.0)
        wg_vals.append(row)

    # ---- ECU torque model ("inner torque" efficiency + friction + pumping), fitted to the engine model
    friction_rpm = rpm8
    friction_vals = [plant.friction_mep(r, 368.15) / 1e5 + cfg.friction.accessory_torque * 4 * math.pi
                     / plant.vd / 1e5 for r in friction_rpm]
    load_10 = [10.0, 20.0, 35.0, 50.0, 70.0, 90.0, 120.0, 150.0, 190.0, 230.0]
    ign_table = Table("tmp", "", "", "", "", -90, 90, [_axis_rpm(rpm16), Axis("load", "", "%",
                      [float(v) for v in LOAD_16], 0, 300)], np.asarray(ign_vals))
    eff_vals, pmep_vals = [], []
    map_10 = [20.0, 35.0, 50.0, 70.0, 90.0, 100.0, 140.0, 180.0, 220.0, 260.0]
    for r in rpm12:
        row = []
        for ld in load_10:
            rel = ld / 100.0
            air_cyc = rel * RHO_REF * plant.vd
            t_im = 313.15
            ve_guess = 0.9
            p_im = air_cyc * R_AIR * t_im / (ve_guess * plant.vd)
            p_em = p_em_estimate(r, min(260.0, p_im / 1000.0)) if p_im > baro * 1000.0 else baro * 1000.0 * 1.03
            lam_v = lam(r, ld)
            fuel_cyc = air_cyc / (cfg.fuel.stoich_afr * lam_v)
            sp_adv = ign_table.lookup(r, ld)
            ct = plant.combustion_terms(max(r, 600.0), p_im, t_im, 363.15, air_cyc, fuel_cyc, sp_adv, p_em=p_em)
            energy = air_cyc * cfg.fuel.lower_heating_value / (cfg.fuel.stoich_afr * lam_v) * min(1.0, lam_v)
            row.append(min(0.6, max(0.15, ct.imep * plant.vd / energy)) if ct.combusting else 0.3)
        eff_vals.append(row)
        prow = []
        for mp in map_10:
            p_em = p_em_estimate(r, mp) if mp > baro + 5 else baro * 1000.0 * 1.03
            prow.append((p_em - mp * 1000.0) / 1e5)
        pmep_vals.append(prow)

    rail_vals = [[min(cfg.fuel_system.rail_pressure_max / 1e5,
                      cfg.fuel_system.rail_pressure_idle / 1e5
                      + (cfg.fuel_system.rail_pressure_max - cfg.fuel_system.rail_pressure_idle) / 1e5
                      * min(1.0, ld / 180.0) ** 1.2 * (0.7 + 0.3 * min(1.0, r / 4000.0)))
                  for ld in LOAD_16] for r in rpm16]

    fs = cfg.fuel_system
    tables = [
        _table("ve", "Volumetric efficiency (air model)",
               "Speed-density air-charge model used for fuelling and load calculation. Fuel delivery depends "
               "directly on this table: a VE error produces a lambda error that closed-loop trims must correct.",
               "fuel", "%", 10, 150,
               [_axis_rpm(rpm16), Axis("map", "Manifold pressure", "kPa", [float(v) for v in MAP_16], 10, 320)],
               ve_vals),
        _table("ign_base", "Base ignition timing", "Spark advance before temperature, knock and idle corrections.",
               "ignition", "° BTDC", -15, 55,
               [_axis_rpm(rpm16), Axis("load", "Relative air charge", "%", [float(v) for v in LOAD_16], 0, 300)],
               ign_vals),
        _table("lambda_target", "Lambda target", "Commanded air–fuel equivalence ratio.", "fuel", "λ", 0.70, 1.20,
               [_axis_rpm(rpm16), Axis("load", "Relative air charge", "%", [float(v) for v in LOAD_16], 0, 300)],
               lambda_vals, 3),
        _table("boost_max", "Boost target (maximum)",
               "Upper limit for the boost target (absolute pressure before the throttle). The actual target "
               "follows the torque request and never exceeds this table.",
               "boost", "kPa", 90, 320,
               [_axis_rpm(rpm12), Axis("pedal", "Pedal position", "%", [float(v) for v in PEDAL_6], 0, 100)],
               boost_max, 0),
        _table("wastegate_ff", "Wastegate feed-forward", "Open-loop wastegate position for a boost target "
               "(PID correction is added on top).", "boost", "% open", 0, 100,
               [_axis_rpm(rpm12), Axis("boost", "Boost target", "kPa", [float(v) for v in BOOST_8], 50, 350)],
               wg_vals, 0),
        _table("torque_limit", "Torque limit", "Maximum engine torque request.", "torque", "N·m", 0, 1200,
               [_axis_rpm(rpm16)], torque_limit, 0),
        _table("pedal_map", "Throttle response (pedal map)", "Driver torque demand as a percentage of the torque "
               "limit.", "driver", "%", 0, 100,
               [_axis_rpm(rpm8), Axis("pedal", "Pedal position", "%", [float(v) for v in PEDAL_11], 0, 100)],
               [[100.0 * (p / 100.0) ** 1.15 for p in PEDAL_11] for _ in rpm8], 1),
        _table("idle_target", "Idle speed target", "Target idle speed versus coolant temperature.", "idle", "rpm",
               400, 1600, [Axis("ect", "Coolant temperature", "°C", [float(v) for v in ECT_8], -40, 140)],
               [1100, 1000, 900, 850, 780, 720, 700, 750], 0),
        _table("iat_spark_corr", "Intake-temperature spark correction", "Added to base ignition.",
               "temperature", "°", -20, 10,
               [Axis("iat", "Intake air temperature", "°C", [float(v) for v in IAT_8], -40, 150)],
               [2.0, 1.5, 0.5, 0.0, -2.0, -4.5, -9.0, -13.0]),
        _table("warmup_enrich", "Warm-up enrichment", "Extra fuel versus coolant temperature.", "temperature", "%",
               0, 80, [Axis("ect", "Coolant temperature", "°C", [float(v) for v in ECT_8], -40, 140)],
               [35, 22, 12, 6, 2, 0, 0, 0]),
        _table("knock_retard_step", "Knock retard step", "Spark retard applied per 50 ms of detected knock.",
               "knock", "°", 0, 10, [_axis_rpm(rpm8)], [2.0 if r < 2000 else 2.5 if r < 3000 else 3.0 for r in rpm8]),
        _table("egt_protect_lambda", "EGT protection (lambda cap)",
               "Maximum lambda allowed versus measured exhaust temperature (component protection).",
               "protection", "λ", 0.70, 1.25,
               [Axis("egt", "Exhaust temperature", "°C", [800.0, 900.0, 950.0, 1000.0, 1050.0], 0, 1200)],
               [1.2, 1.2, 0.88, 0.80, 0.75], 3),
        _table("coolant_torque_limit", "Coolant-temperature torque limit", "Torque available versus coolant "
               "temperature (overheat protection).", "protection", "%", 0, 100,
               [Axis("ect", "Coolant temperature", "°C", [100.0, 110.0, 115.0, 120.0, 125.0], -40, 150)],
               [100, 100, 85, 60, 40], 0),
        _table("vvt_intake", "Intake cam advance target", "Intake phaser target.", "valvetrain", "°", 0,
               cfg.valvetrain.intake_vvt_range_deg,
               [_axis_rpm(rpm8), Axis("load", "Relative air charge", "%", load8, 0, 300)], vvt_intake),
        _table("vvt_exhaust", "Exhaust cam retard target", "Exhaust phaser target.", "valvetrain", "°", 0,
               cfg.valvetrain.exhaust_vvt_range_deg,
               [_axis_rpm(rpm8), Axis("load", "Relative air charge", "%", load8, 0, 300)], vvt_exhaust),
        _table("rail_target", "Fuel rail pressure target", "High-pressure fuel rail set-point.", "fuel", "bar", 20,
               max(40.0, fs.rail_pressure_max / 1e5),
               [_axis_rpm(rpm16), Axis("load", "Relative air charge", "%", [float(v) for v in LOAD_16], 0, 300)],
               rail_vals, 0),
        _table("friction_est", "Friction estimate (torque model)", "ECU estimate of friction + accessory MEP.",
               "torque", "bar", 0, 5, [_axis_rpm(friction_rpm)], friction_vals, 3),
        _table("torque_eff", "Torque model efficiency", "Indicated efficiency (fuel energy → indicated work) "
               "used to convert between torque and air charge ('inner torque' structure).", "torque", "", 0.1,
               0.7, [_axis_rpm(rpm12), Axis("load", "Relative air charge", "%", load_10, 0, 300)], eff_vals, 4),
        _table("pmep_est", "Pumping loss estimate (torque model)", "Pumping mean effective pressure versus "
               "manifold pressure (negative = net positive pumping work under boost).", "torque", "bar", -2, 3,
               [_axis_rpm(rpm12), Axis("map", "Manifold pressure", "kPa", map_10, 10, 320)], pmep_vals, 3),
    ]
    scalars = [
        _scalar("displacement_l", "Displacement", "Engine displacement used by the air model.", "engine", "L",
                plant.vd * 1000.0, 0.5, 10.0, 3),
        _scalar("cylinders", "Cylinders", "Number of cylinders.", "engine", "", cfg.geometry.cylinders, 1, 12, 0),
        _scalar("stoich_afr", "Stoichiometric AFR", "Fuel stoichiometric air–fuel ratio.", "engine", ":1",
                cfg.fuel.stoich_afr, 5, 20),
        _scalar("fuel_lhv_mj", "Fuel heating value", "Lower heating value used by the torque model.", "engine",
                "MJ/kg", cfg.fuel.lower_heating_value / 1e6, 15, 50),
        _scalar("injector_flow_gs", "Injector flow", "Static flow at reference pressure.", "fuel", "g/s",
                fs.injector_flow_ref * 1000.0, 1, 50),
        _scalar("injector_ref_bar", "Injector reference pressure", "Rating pressure for injector flow.", "fuel",
                "bar", fs.injector_ref_pressure / 1e5, 2, 500, 0),
        _scalar("injector_dead_time_ms", "Injector dead time", "Opening delay added to every pulse.", "fuel", "ms",
                fs.injector_dead_time * 1000.0, 0.0, 3.0, 3),
        _scalar("throttle_diameter_mm", "Throttle bore", "Used by the inverse throttle model.", "torque", "mm",
                cfg.intake.throttle_diameter * 1000.0, 30, 110, 1),
        _scalar("throttle_closed_deg", "Throttle closed angle", "Plate angle at 0 %.", "torque", "°",
                cfg.intake.throttle_closed_angle_deg, 0, 15, 1),
        _scalar("throttle_cd", "Throttle discharge coefficient", "Inverse throttle model.", "torque", "",
                cfg.intake.throttle_discharge_coeff, 0.5, 1.0, 3),
        _scalar("throttle_kp", "Throttle MAP control P", "% throttle per kPa MAP error.", "torque", "%/kPa", 0.4,
                0, 5, 3),
        _scalar("throttle_ki", "Throttle MAP control I", "% throttle per kPa·s.", "torque", "%/(kPa·s)", 2.0, 0,
                20, 3),
        _scalar("throttle_idle_pos", "Throttle rest position", "Position with engine stopped.", "idle", "%", 3.0,
                0, 20, 1),
        _scalar("throttle_min_pos", "Throttle minimum", "Minimum position while running.", "idle", "%", 0.3, 0, 10,
                2),
        _scalar("throttle_limp_pos", "Limp-home throttle", "Fixed throttle in TPS-fault limp mode.", "protection",
                "%", 8.0, 0, 30, 1),
        _scalar("idle_kp", "Idle P gain", "N·m per rpm of idle speed error.", "idle", "N·m/rpm", 0.20, 0, 1, 3),
        _scalar("idle_ki", "Idle I gain", "N·m per rpm·s.", "idle", "N·m/(rpm·s)", 0.10, 0, 2, 3),
        _scalar("idle_kd", "Idle D gain", "N·m per rpm/s.", "idle", "N·m·s/rpm", 0.010, 0, 0.1, 4),
        _scalar("idle_torque_ff_nm", "Idle torque feed-forward", "Base torque request at idle (accessories).",
                "idle", "N·m", 8.0, 0, 60, 1),
        _scalar("idle_spark_reserve_deg", "Idle spark reserve", "Spark retard at idle for fast torque control.",
                "idle", "°", 4.0, 0, 15, 1),
        _scalar("idle_spark_gain", "Idle spark gain", "Spark change per 100 rpm idle error.", "idle", "°/100rpm",
                1.5, 0, 10, 2),
        _scalar("boost_kp", "Boost P gain", "% wastegate per kPa boost error.", "boost", "%/kPa", 0.6, 0, 5, 3),
        _scalar("boost_ki", "Boost I gain", "% wastegate per kPa·s.", "boost", "%/(kPa·s)", 1.2, 0, 20, 3),
        _scalar("boost_kd", "Boost D gain", "% wastegate per kPa/s.", "boost", "%·s/kPa", 0.015, 0, 1, 4),
        _scalar("underboost_enable_rpm", "Underboost monitor enable speed",
                "P0299 is only evaluated above this speed (below it the turbo is still spooling).", "boost",
                "rpm", 2500.0, 0, 8000, 0),
        _scalar("boost_reserve_kpa", "Boost reserve", "Boost above the MAP request so the throttle keeps "
                "authority.", "boost", "kPa", 10.0, 0, 50, 1),
        _scalar("overboost_margin_kpa", "Overboost margin", "Boost above target that triggers protection.",
                "protection", "kPa", 30.0, 5, 100, 1),
        _scalar("overboost_time_s", "Overboost time", "Time over margin before protection.", "protection", "s",
                0.5, 0.05, 5, 2),
        _scalar("lambda_kp", "Lambda P gain", "% trim per unit λ error.", "fuel", "%/λ", 20.0, 0, 200, 1),
        _scalar("lambda_ki", "Lambda I gain", "% trim per λ·s.", "fuel", "%/(λ·s)", 60.0, 0, 500, 1),
        _scalar("stft_limit_pct", "Fuel trim limit", "Maximum short/long-term trim.", "fuel", "%", 25.0, 0, 50, 1),
        _scalar("ltft_rate_pct_s", "Long-term trim rate", "Adaptation speed.", "fuel", "%/s", 1.0, 0, 20, 2),
        _scalar("fuel_trim_dtc_pct", "Fuel trim DTC threshold", "Total trim (short + long) that sets P0171/P0172.",
                "fuel", "%", 20.0, 5, 50, 1),
        _scalar("cl_min_coolant_c", "Closed-loop minimum coolant", "Closed-loop lambda enable temperature.", "fuel",
                "°C", 40.0, -20, 100, 0),
        _scalar("knock_threshold", "Knock threshold", "Processed knock level that counts as knock.", "knock", "",
                0.35, 0.05, 5, 2),
        _scalar("knock_recovery_deg_s", "Knock recovery rate", "Spark advance restored per second.", "knock",
                "°/s", 1.5, 0.1, 20, 2),
        _scalar("knock_max_retard_deg", "Maximum knock retard", "Retard limit.", "knock", "°", 10.0, 0, 25, 1),
        _scalar("knock_sensor_fault_retard_deg", "Knock-sensor fault retard", "Fixed retard when the knock sensor "
                "fails.", "knock", "°", 6.0, 0, 20, 1),
        _scalar("spark_min_deg", "Spark minimum", "Lower spark clamp.", "ignition", "° BTDC", -10.0, -20, 10, 1),
        _scalar("spark_max_deg", "Spark maximum", "Upper spark clamp.", "ignition", "° BTDC", 45.0, 10, 60, 1),
        _scalar("rev_limit_rpm", "Rev limit", "Fuel-cut engine speed.", "protection", "rpm", max_rpm, 2000, 12000,
                0),
        _scalar("rev_limit_hyst_rpm", "Rev-limit hysteresis", "Fuel resumes below limit − hysteresis.",
                "protection", "rpm", 150.0, 10, 1000, 0),
        _scalar("limp_torque_nm", "Limp-mode torque", "Torque cap in limp mode.", "protection", "N·m",
                0.3 * max(torque_limit), 0, 1000, 0),
        _scalar("overrun_cut_rpm_above_idle", "Overrun cut threshold", "Fuel cut on overrun above idle + this.",
                "driver", "rpm", 600.0, 100, 3000, 0),
        _scalar("overrun_resume_rpm_above_idle", "Overrun resume", "Fuel resumes below idle + this.", "driver",
                "rpm", 300.0, 50, 2000, 0),
        _scalar("start_rpm", "Start detect speed", "Cranking → running transition.", "startup", "rpm", 450.0, 100,
                1000, 0),
        _scalar("stall_rpm", "Stall speed", "Running → stalled transition.", "startup", "rpm", 250.0, 50, 800, 0),
        _scalar("crank_fuel_mg", "Cranking fuel", "Fuel per cylinder per cycle while cranking (warm).", "startup",
                "mg", 30.0 * plant.vd_cyl / 0.0005, 1, 200, 1),
        _scalar("crank_spark_deg", "Cranking spark", "Spark advance while cranking.", "startup", "° BTDC", 8.0, -5,
                30, 1),
    ]
    cal = Calibration(
        name=name or f"{cfg.name} — synthetic baseline",
        engine_id=cfg.id,
        tables={t.id: t for t in tables},
        scalars={s.id: s for s in scalars},
        metadata={
            "provenance": "assumption",
            "description": "Synthetic baseline derived from OpenTune's engine model; not extracted from any ECU.",
            "reference_rating": cfg.reference_ratings,
        },
    )
    problems = cal.validate()
    if problems:
        raise ValueError("Generated calibration is invalid: " + "; ".join(problems))
    return cal


TABLE_CATEGORIES = {
    "fuel": "Fuel delivery",
    "ignition": "Ignition timing",
    "boost": "Boost control",
    "torque": "Torque structure",
    "driver": "Throttle response",
    "idle": "Idle control",
    "temperature": "Temperature compensation",
    "knock": "Knock control",
    "protection": "Protection strategies",
    "valvetrain": "Valvetrain",
    "engine": "Engine constants",
    "startup": "Start-up",
}
