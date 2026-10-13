"""Mean-value engine plant model (MVEM).

A cycle-averaged, zero-dimensional model in the tradition of Hendricks (1989) and
Eriksson & Nielsen (2014): manifolds are filling/emptying control volumes, the
throttle, turbine and wastegate are compressible-flow restrictions, the
compressor follows a dimensionless head/flow speed-line, cylinder air flow uses
speed-density with a residual-gas (backpressure) correction, and brake torque is
built from gross indicated work minus pumping and friction work.

Documented simplifications (see docs/SIMULATION_MODELS.md):

* cycle-averaged: no crank-angle-resolved gas dynamics (see :mod:`cycle` for a
  single-zone crank-angle model used for cross-checks);
* isothermal manifolds; exhaust manifold pressure is quasi-steady;
* knock limit, VE map, burn duration and efficiency factors are empirical
  trend models whose coefficients are assumptions, not BMW data;
* no Valvetronic-style valve-lift load control — load is controlled by the
  throttle (adds pumping loss at part load relative to a lift-controlled engine).

Numerics: the stiff intake/boost pressure pair is integrated with a linearly
implicit (Rosenbrock–Euler) step; everything else uses explicit Euler.
"""

from __future__ import annotations

import math
import random
from dataclasses import asdict, dataclass, field

from opentune.lab.physics import formulas as f
from opentune.lab.physics.config import EngineConfig
from opentune.lab.physics.constants import (
    CP_AIR,
    CP_EXHAUST,
    GAMMA_AIR,
    GAMMA_CYCLE,
    GAMMA_EXHAUST,
    R_AIR,
    R_EXHAUST,
    STANDARD_PRESSURE,
    STANDARD_TEMPERATURE,
)

TWO_PI = 2.0 * math.pi
RHO_REF = STANDARD_PRESSURE / (R_AIR * STANDARD_TEMPERATURE)
PI_LIN = 0.985  # flow-function linearisation threshold (avoids infinite slope at Π→1)


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def flow_function_regularized(pressure_ratio: float, gamma: float) -> float:
    """Isentropic flow function, linearised for Π above ``PI_LIN`` (Eriksson & Nielsen)."""
    if pressure_ratio >= 1.0:
        return 0.0
    if pressure_ratio > PI_LIN:
        return f.flow_function(PI_LIN, gamma) * (1.0 - pressure_ratio) / (1.0 - PI_LIN)
    return f.flow_function(pressure_ratio, gamma)


def bidirectional_flow(area: float, p_a: float, t_a: float, p_b: float, t_b: float,
                       r_gas: float, gamma: float) -> float:
    """Flow from volume a to b through a restriction (negative when b > a)."""
    if area <= 0.0:
        return 0.0
    if p_a >= p_b:
        return area * p_a / math.sqrt(r_gas * t_a) * flow_function_regularized(p_b / p_a, gamma)
    return -area * p_b / math.sqrt(r_gas * t_b) * flow_function_regularized(p_a / p_b, gamma)


def lambda_efficiency(lam: float) -> float:
    """Relative indicated efficiency vs air–fuel equivalence ratio (heuristic shape).

    Captures the qualitative trends in Heywood (2018) ch. 15: maximum power slightly
    rich (λ ≈ 0.85–0.9), best fuel conversion efficiency slightly lean. Energy release
    is separately capped by available oxygen (min(1, λ)).
    """
    if lam < 0.85:
        return 1.0375 - 0.6 * (0.85 - lam)
    if lam < 1.0:
        return 1.0 + 0.25 * (1.0 - lam)
    if lam <= 1.2:
        return 1.0 + 0.08 * (lam - 1.0)
    return 1.016 - 0.30 * (lam - 1.2)


@dataclass
class PlantInputs:
    throttle_cmd: float = 0.0  # % open (0–100)
    wastegate_cmd: float = 100.0  # % open (100 = fully open)
    injector_pw: float = 0.0  # s per injection (one injection per cylinder per cycle)
    rail_pressure_cmd: float = 50e5  # Pa
    spark_advance: float = 10.0  # ° BTDC
    intake_cam_cmd: float = 0.0  # ° advance from base timing
    exhaust_cam_cmd: float = 0.0  # ° retard from base timing
    fuel_enable: bool = False
    ignition_on: bool = True
    starter: bool = False


@dataclass
class Ambient:
    pressure: float = STANDARD_PRESSURE  # Pa
    temperature: float = STANDARD_TEMPERATURE  # K
    airflow: float = 1.0  # 0–1: fraction of full cooling airflow (vehicle speed / dyno fan)


@dataclass
class PlantState:
    omega: float = 0.0  # crank rad/s
    p_im: float = STANDARD_PRESSURE
    p_boost: float = STANDARD_PRESSURE
    omega_tc: float = 0.0  # turbo rad/s
    t_coolant: float = STANDARD_TEMPERATURE
    t_oil: float = STANDARD_TEMPERATURE
    t_charge_loop: float = STANDARD_TEMPERATURE
    throttle_pos: float = 0.0  # 0–1
    wastegate_pos: float = 1.0  # 0–1 open
    intake_cam: float = 0.0  # ° advance
    exhaust_cam: float = 0.0  # ° retard
    rail_pressure: float = 5e5  # Pa
    p_em: float = STANDARD_PRESSURE
    t_egt_sensor: float = STANDARD_TEMPERATURE
    m_c: float = 0.0
    m_th: float = 0.0
    crank_angle: float = 0.0  # 0–720 °
    time: float = 0.0


@dataclass
class PlantOutputs:
    """Ground-truth plant signals for one step (SI units unless named otherwise)."""

    time: float = 0.0
    rpm: float = 0.0
    omega: float = 0.0
    p_ambient: float = STANDARD_PRESSURE
    t_ambient: float = STANDARD_TEMPERATURE
    p_compressor_in: float = STANDARD_PRESSURE
    p_boost: float = STANDARD_PRESSURE
    p_im: float = STANDARD_PRESSURE
    p_em: float = STANDARD_PRESSURE
    p_turbine_out: float = STANDARD_PRESSURE
    t_compressor_out: float = STANDARD_TEMPERATURE
    t_im: float = STANDARD_TEMPERATURE
    t_exhaust_gas: float = STANDARD_TEMPERATURE
    t_egt_sensor: float = STANDARD_TEMPERATURE
    t_coolant: float = STANDARD_TEMPERATURE
    t_oil: float = STANDARD_TEMPERATURE
    t_charge_loop: float = STANDARD_TEMPERATURE
    m_compressor: float = 0.0
    m_throttle: float = 0.0
    m_cylinder: float = 0.0
    m_fuel: float = 0.0
    m_wastegate: float = 0.0
    m_turbine: float = 0.0
    air_per_cycle: float = 0.0  # kg (whole engine)
    fuel_per_cycle: float = 0.0  # kg (whole engine)
    relative_load: float = 0.0  # air charge relative to NA at standard conditions
    volumetric_efficiency: float = 0.0
    lambda_value: float = float("inf")
    combusting: bool = False
    spark_advance: float = 0.0
    mbt_advance: float = 0.0
    knock_limit_advance: float = 0.0
    knock_intensity: float = 0.0
    burn_duration_deg: float = 0.0
    eta_spark: float = 0.0
    eta_lambda: float = 0.0
    eta_indicated: float = 0.0
    imep: float = 0.0
    pmep: float = 0.0
    fmep: float = 0.0
    bmep: float = 0.0
    indicated_torque: float = 0.0
    brake_torque: float = 0.0
    load_torque: float = 0.0
    starter_torque: float = 0.0
    brake_power: float = 0.0
    fuel_rate_lph: float = 0.0
    bsfc_g_kwh: float = 0.0
    peak_pressure_est: float = 0.0
    turbo_rpm: float = 0.0
    compressor_pr: float = 1.0
    compressor_eta: float = 0.0
    compressor_flow_param: float = 0.0
    compressor_power: float = 0.0
    turbine_power: float = 0.0
    turbine_eta: float = 0.0
    surge: bool = False
    throttle_pos: float = 0.0
    wastegate_pos: float = 1.0
    intake_cam: float = 0.0
    exhaust_cam: float = 0.0
    rail_pressure: float = 0.0
    injector_pw: float = 0.0
    battery_voltage: float = 12.4
    heat_to_coolant: float = 0.0
    radiator_heat: float = 0.0
    crank_angle: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class _AirPath:
    """Intermediate air-path quantities for one evaluation (internal)."""

    p1: float
    t1: float
    m_c: float
    t_c: float
    pr: float
    eta_c: float
    phi: float
    p_c: float
    surge: bool
    t_ic: float
    t_im: float
    p_th_up: float
    m_th: float
    m_cyl: float
    m_recirc: float
    ve: float


@dataclass
class EnginePlant:
    config: EngineConfig
    ambient: Ambient = field(default_factory=Ambient)
    seed: int = 1
    state: PlantState = field(default_factory=PlantState)

    def __post_init__(self) -> None:
        self.rng = random.Random(self.seed)
        self._precompute()
        self.reset()

    # ------------------------------------------------------------------ setup
    def _precompute(self) -> None:
        cfg = self.config
        g = cfg.geometry
        self.vd = cfg.displacement
        self.vd_cyl = cfg.cylinder_volume
        self.n_cyl = g.cylinders
        self.rc = g.compression_ratio
        self.eta_otto = f.otto_efficiency(self.rc, GAMMA_CYCLE)
        self.fuel = cfg.fuel
        self.comp_area = math.pi / 4.0 * cfg.turbo.compressor_diameter**2
        self.y50 = f.wiebe_fraction_position(0.5, cfg.combustion.wiebe_a, cfg.combustion.wiebe_m)
        closed = math.radians(cfg.intake.throttle_closed_angle_deg)
        self.throttle_closed = closed
        self.throttle_max_area = f.throttle_open_area(cfg.intake.throttle_diameter, math.radians(90.0), closed,
                                                      cfg.intake.throttle_leak_area)

    def reset(self, warm: bool = False, coolant_c: float | None = None) -> None:
        """Engine stopped at ambient (or warm) conditions."""
        amb = self.ambient
        t_eng = amb.temperature if coolant_c is None else coolant_c + 273.15
        if warm and coolant_c is None:
            t_eng = 90.0 + 273.15
        self.state = PlantState(
            p_im=amb.pressure, p_boost=amb.pressure, p_em=amb.pressure,
            t_coolant=t_eng, t_oil=t_eng, t_charge_loop=amb.temperature + (8.0 if warm else 0.0),
            t_egt_sensor=amb.temperature, rail_pressure=5e5,
        )

    def warm_start(self, rpm: float = 750.0) -> None:
        """Running, warm engine at idle-like conditions (useful for tests and dyno setup)."""
        self.reset(warm=True)
        st = self.state
        st.omega = rpm * TWO_PI / 60.0
        st.p_im = 0.35 * self.ambient.pressure
        st.rail_pressure = self.config.fuel_system.rail_pressure_idle
        st.t_oil = 95.0 + 273.15
        st.omega_tc = 20000.0 * TWO_PI / 60.0 if self.config.turbo.enabled else 0.0

    # ------------------------------------------------------------------ sub-models
    def base_volumetric_efficiency(self, rpm: float, intake_cam: float) -> float:
        vt = self.config.valvetrain
        peak_rpm = vt.ve_peak_rpm + vt.ve_cam_shift_rpm_per_deg * intake_cam
        ve = vt.ve_peak - vt.ve_curvature * ((rpm - peak_rpm) / 1000.0) ** 2
        return max(vt.ve_min, ve)

    def valve_overlap_deg(self, intake_cam: float, exhaust_cam: float) -> float:
        vt = self.config.valvetrain
        ivo = (vt.intake_centerline_deg - intake_cam) - vt.intake_duration_deg / 2.0  # ATDC (negative = BTDC)
        evc = -(vt.exhaust_centerline_deg - exhaust_cam) + vt.exhaust_duration_deg / 2.0  # ATDC
        return max(0.0, evc - ivo)

    def volumetric_efficiency(self, rpm: float, p_im: float, p_em: float, intake_cam: float,
                              exhaust_cam: float) -> float:
        ve = self.base_volumetric_efficiency(rpm, intake_cam)
        ve *= max(0.3, f.backpressure_ve_factor(self.rc, p_em, max(p_im, 1000.0), GAMMA_AIR))
        overlap = self.valve_overlap_deg(intake_cam, exhaust_cam)
        delta = _clamp((p_im - p_em) / max(p_im, 1000.0), -0.3, 0.3)
        ve *= 1.0 + self.config.valvetrain.scavenging_gain * (overlap / 60.0) * delta
        return max(0.05, ve)

    def burn_duration(self, rpm: float, rel_load: float) -> float:
        c = self.config.combustion
        return c.burn_duration_ref_deg * (max(rpm, 400.0) / 2000.0) ** c.burn_rpm_exponent \
            * (max(rel_load, 0.08) / 0.5) ** c.burn_load_exponent

    def mbt_advance(self, rpm: float, rel_load: float) -> float:
        """Spark advance (° BTDC) that places CA50 at the MBT phasing."""
        c = self.config.combustion
        return self.y50 * self.burn_duration(rpm, rel_load) + c.ignition_delay_deg - c.ca50_mbt_deg

    def knock_limit(self, rpm: float, p_im: float, t_im: float, t_coolant: float, lam: float) -> float:
        """Empirical knock-limited spark advance (° BTDC). Coefficients are assumptions."""
        c = self.config.combustion
        return (c.knock_base_deg
                + c.knock_octane_coeff * (c.octane_aki - 91.0)
                - c.knock_pressure_coeff * (p_im / 1e5 - 1.0)
                - c.knock_iat_coeff * (t_im - 313.15)
                - c.knock_coolant_coeff * max(0.0, t_coolant - 363.15)
                + c.knock_rpm_coeff * (rpm - 2000.0) / 1000.0
                + c.knock_lambda_coeff * max(0.0, 1.0 - lam))

    def injector_flow(self, rail_pressure: float) -> float:
        fs = self.config.fuel_system
        return fs.injector_flow_ref * math.sqrt(max(rail_pressure, 0.0) / fs.injector_ref_pressure)

    def friction_mep(self, rpm: float, t_oil: float) -> float:
        fr = self.config.friction
        sp = f.mean_piston_speed(self.config.geometry.stroke, rpm / 60.0)
        cold = _clamp((363.15 - t_oil) / 70.0, 0.0, 1.0)
        return f.quadratic_fmep(fr.fmep_a, fr.fmep_b, fr.fmep_c, sp) * (1.0 + fr.cold_oil_factor * cold)

    def wall_heat_fraction(self, rpm: float) -> float:
        base = self.config.thermal.wall_heat_fraction
        return _clamp(base * (2000.0 / max(rpm, 600.0)) ** 0.15, 0.1, 0.75)

    def compressor(self, omega_tc: float, p1: float, t1: float, p2: float) -> tuple[float, float, float, float, bool]:
        """Returns (mass flow, efficiency, flow parameter, outlet temperature, surge flag)."""
        tc = self.config.turbo
        u = f.tip_speed(tc.compressor_diameter, omega_tc)
        pr = p2 / p1
        dh = f.compressor_isentropic_enthalpy_rise(CP_AIR, t1, pr, GAMMA_AIR)
        rho1 = p1 / (R_AIR * t1)
        radicand = (tc.compressor_head_max * u * u - 2.0 * dh) / tc.compressor_head_slope
        surge = radicand <= 0.0 and pr > 1.0
        phi_u = math.sqrt(radicand) if radicand > 0.0 else 0.0
        if pr >= 1.0:
            phi_u = min(phi_u, tc.compressor_flow_choke * u)
        m = rho1 * self.comp_area * phi_u
        phi = phi_u / u if u > 1.0 else 0.0
        eta = _clamp(tc.compressor_eta_max - tc.compressor_eta_curvature * (phi - tc.compressor_phi_opt) ** 2,
                     0.45, tc.compressor_eta_max)
        t2 = t1 + dh / (CP_AIR * eta) if dh > 0.0 else t1
        return m, eta, phi, t2, surge

    def solve_exhaust_pressure(self, m_e: float, t_em: float, p_out: float, wastegate_pos: float,
                               guess: float) -> float:
        """Quasi-steady exhaust manifold pressure: turbine + wastegate flow = engine outflow."""
        tc = self.config.turbo
        area = tc.turbine_area + tc.wastegate_area_max * wastegate_pos
        if m_e <= 1e-6 or area <= 0.0:
            return p_out
        k = area / math.sqrt(R_EXHAUST * t_em)

        def residual(p: float) -> float:
            return k * p * flow_function_regularized(p_out / p, GAMMA_EXHAUST) - m_e

        lo, hi = p_out * (1.0 + 1e-9), 20.0 * p_out
        if residual(hi) < 0.0:
            return hi
        p = _clamp(guess, lo * 1.0001, hi)
        for _ in range(12):  # Newton with numeric derivative, bisection fallback
            r = residual(p)
            if abs(r) < 1e-7:
                return p
            dp = max(p * 1e-6, 1.0)
            d = (residual(p + dp) - r) / dp
            p_new = p - r / d if d > 0.0 else 0.5 * (lo + hi)
            if r > 0.0:
                hi = min(hi, p)
            else:
                lo = max(lo, p)
            if not (lo < p_new < hi):
                p_new = 0.5 * (lo + hi)
            p = p_new
        return p

    # ------------------------------------------------------------------ air path
    def _air_path(self, st: PlantState, p_boost: float, p_im: float, rpm: float, n: float) -> _AirPath:
        cfg = self.config
        amb = self.ambient
        ip = cfg.intake
        p1 = max(amb.pressure - ip.air_filter_dp_coeff * st.m_c * st.m_c, 0.7 * amb.pressure)
        t1 = amb.temperature
        if cfg.turbo.enabled:
            m_c, eta_c, phi, t_c, surge = self.compressor(st.omega_tc, p1, t1, p_boost)
            pr = p_boost / p1
        else:
            m_c, eta_c, phi, t_c, surge, pr = 0.0, 0.0, 0.0, t1, False, 1.0
            p_boost = p1
        # charge-air cooler
        if ip.intercooler_type == "air_to_water":
            t_ic = f.heat_exchanger_outlet_temperature(t_c, st.t_charge_loop, ip.intercooler_effectiveness)
        elif ip.intercooler_type == "air_to_air":
            eff = ip.intercooler_effectiveness * (0.35 + 0.65 * amb.airflow)
            t_ic = f.heat_exchanger_outlet_temperature(t_c, amb.temperature, eff)
        else:
            t_ic = t_c
        t_im = t_ic + 3.0  # small heat pick-up in the manifold (assumption)
        dp_ic = ip.intercooler_dp_coeff * st.m_th * st.m_th if cfg.turbo.enabled else 0.0
        p_th_up = max(p_boost - dp_ic, 0.5 * p_boost)
        angle = self.throttle_closed + (math.pi / 2.0 - self.throttle_closed) * st.throttle_pos
        area = ip.throttle_discharge_coeff * f.throttle_open_area(ip.throttle_diameter, angle, self.throttle_closed,
                                                                  ip.throttle_leak_area)
        m_th = bidirectional_flow(area, p_th_up, t_ic, p_im, t_im, R_AIR, GAMMA_AIR)
        ve = self.volumetric_efficiency(rpm, p_im, st.p_em, st.intake_cam, st.exhaust_cam)
        m_cyl = f.speed_density_mass_flow(ve, p_im, t_im, R_AIR, self.vd, n) if n > 0.0 else 0.0
        # recirculation (diverter) valve: vents boost on throttle lift-off to avoid surge
        m_recirc = 0.0
        if cfg.turbo.enabled and p_boost - p_im > 0.3e5 and st.throttle_pos < 0.2:
            m_recirc = bidirectional_flow(4e-4, p_boost, t_c, p1, t1, R_AIR, GAMMA_AIR)
        return _AirPath(p1, t1, m_c, t_c, pr, eta_c, phi,
                        f.compressor_power(m_c, f.compressor_isentropic_enthalpy_rise(CP_AIR, t1, pr, GAMMA_AIR),
                                           eta_c) if m_c > 0.0 and pr > 1.0 and eta_c > 0.0 else 0.0,
                        surge, t_ic, t_im, p_th_up, m_th, m_cyl, m_recirc, ve)

    def _pressure_derivatives(self, st: PlantState, p_boost: float, p_im: float, rpm: float, n: float):
        ap = self._air_path(st, p_boost, p_im, rpm, n)
        ip = self.config.intake
        d_im = R_AIR * ap.t_im / ip.intake_manifold_volume * (ap.m_th - ap.m_cyl)
        if self.config.turbo.enabled:
            d_b = R_AIR * ap.t_c / ip.boost_volume * (ap.m_c - ap.m_th - ap.m_recirc)
        else:
            d_b = 0.0
        return d_b, d_im, ap

    # ------------------------------------------------------------------ step
    def step(self, inputs: PlantInputs, load_torque: float, dt: float) -> PlantOutputs:
        cfg = self.config
        st = self.state
        amb = self.ambient
        comb = cfg.combustion

        # --- actuators
        tp_cmd = _clamp(inputs.throttle_cmd / 100.0, 0.0, 1.0)
        st.throttle_pos += (tp_cmd - st.throttle_pos) * (1.0 - math.exp(-dt / cfg.intake.throttle_time_constant_s))
        wg_cmd = _clamp(inputs.wastegate_cmd / 100.0, 0.0, 1.0)
        st.wastegate_pos += (wg_cmd - st.wastegate_pos) * (1.0 - math.exp(-dt / cfg.turbo.wastegate_time_constant_s))
        rate = cfg.valvetrain.vvt_rate_deg_s * dt
        ic_cmd = _clamp(inputs.intake_cam_cmd, 0.0, cfg.valvetrain.intake_vvt_range_deg)
        ec_cmd = _clamp(inputs.exhaust_cam_cmd, 0.0, cfg.valvetrain.exhaust_vvt_range_deg)
        rpm = st.omega * 60.0 / TWO_PI
        if rpm > 400.0:  # hydraulic phasers need oil pressure
            st.intake_cam += _clamp(ic_cmd - st.intake_cam, -rate, rate)
            st.exhaust_cam += _clamp(ec_cmd - st.exhaust_cam, -rate, rate)
        fs = cfg.fuel_system
        if rpm > 50.0:
            rail_target = _clamp(inputs.rail_pressure_cmd, fs.rail_pressure_idle * 0.5, fs.rail_pressure_max)
        else:
            rail_target = 5e5  # low-pressure pump only
        st.rail_pressure += (rail_target - st.rail_pressure) * (1.0 - math.exp(-dt / 0.08))

        n = st.omega / TWO_PI

        # --- intake / boost pressures: linearly implicit Euler on the stiff pair
        p_b0, p_im0 = st.p_boost, st.p_im
        d_b, d_im, ap = self._pressure_derivatives(st, p_b0, p_im0, rpm, n)
        eps_b = max(1.0, 1e-4 * p_b0)
        eps_im = max(1.0, 1e-4 * p_im0)
        if cfg.turbo.enabled:
            db_b, dim_b, _ = self._pressure_derivatives(st, p_b0 + eps_b, p_im0, rpm, n)
            db_im, dim_im, _ = self._pressure_derivatives(st, p_b0, p_im0 + eps_im, rpm, n)
            j11 = (db_b - d_b) / eps_b
            j12 = (db_im - d_b) / eps_im
            j21 = (dim_b - d_im) / eps_b
            j22 = (dim_im - d_im) / eps_im
            a11, a12, a21, a22 = 1.0 - dt * j11, -dt * j12, -dt * j21, 1.0 - dt * j22
            det = a11 * a22 - a12 * a21
            r1, r2 = dt * d_b, dt * d_im
            if abs(det) > 1e-12:
                delta_b = (r1 * a22 - a12 * r2) / det
                delta_im = (a11 * r2 - a21 * r1) / det
            else:
                delta_b, delta_im = r1, r2
            st.p_boost = max(p_b0 + delta_b, 0.5 * amb.pressure)
        else:
            _, dim_im, _ = self._pressure_derivatives(st, p_b0, p_im0 + eps_im, rpm, n)
            j22 = (dim_im - d_im) / eps_im
            delta_im = dt * d_im / (1.0 - dt * j22)
            st.p_boost = ap.p1
        st.p_im = _clamp(p_im0 + delta_im, 2000.0, 5.0 * amb.pressure)
        st.m_c = ap.m_c
        st.m_th = ap.m_th

        # --- fuel and combustion (cycle-averaged)
        m_cyl = ap.m_cyl
        t_im = ap.t_im
        air_per_cycle = m_cyl / (n / 2.0) if n > 0.1 else 0.0
        q_inj = self.injector_flow(st.rail_pressure)
        dead = fs.injector_dead_time
        fuel_per_inj = q_inj * max(0.0, inputs.injector_pw - dead) if inputs.fuel_enable else 0.0
        fuel_per_cycle = fuel_per_inj * self.n_cyl
        m_fuel = fuel_per_cycle * n / 2.0
        afr_s = self.fuel.stoich_afr
        lam = air_per_cycle / (fuel_per_cycle * afr_s) if fuel_per_cycle > 0.0 else float("inf")
        rel_load = air_per_cycle / (RHO_REF * self.vd) if self.vd > 0 else 0.0
        combusting = (inputs.fuel_enable and inputs.ignition_on and fuel_per_cycle > 0.0 and rpm > 60.0
                      and 0.55 < lam < comb.lean_limit_lambda)

        spark = inputs.spark_advance
        burn = self.burn_duration(rpm, rel_load)
        mbt = self.mbt_advance(rpm, rel_load)
        lam_eff = lam if math.isfinite(lam) else 1.0
        knock_lim = self.knock_limit(rpm, st.p_im, t_im, st.t_coolant, lam_eff)
        knock = 0.0
        eta_spark = eta_lam = eta_ind = 0.0
        imep = 0.0
        q_released = 0.0
        if combusting:
            eta_spark = max(0.0, 1.0 - comb.spark_efficiency_coeff * (spark - mbt) ** 2)
            eta_lam = lambda_efficiency(lam)
            eta_ind = self.eta_otto * comb.eta_ig_ch * eta_spark * eta_lam
            q_released = fuel_per_cycle * self.fuel.lower_heating_value * min(1.0, lam) * comb.combustion_efficiency
            imep = q_released * eta_ind / self.vd
            if spark > knock_lim:
                knock = (spark - knock_lim) * comb.knock_intensity_gain * (0.85 + 0.3 * self.rng.random())
        w_ig = imep * self.vd

        # --- exhaust side
        m_e = m_cyl + m_fuel
        x_wall = self.wall_heat_fraction(rpm)
        if combusting and air_per_cycle > 0.0:
            q_exh = (q_released - w_ig) * (1.0 - x_wall)
            t_em = t_im + q_exh / ((air_per_cycle + fuel_per_cycle) * CP_EXHAUST)
        else:
            t_em = t_im
        p_out = amb.pressure + cfg.exhaust.backpressure_coeff * m_e * m_e
        m_t = m_wg = p_t = eta_t = 0.0
        if cfg.turbo.enabled:
            st.p_em = self.solve_exhaust_pressure(m_e, t_em, p_out, st.wastegate_pos, st.p_em)
            tc = cfg.turbo
            k = math.sqrt(R_EXHAUST * t_em)
            psi = flow_function_regularized(p_out / st.p_em, GAMMA_EXHAUST)
            m_t = tc.turbine_area * st.p_em / k * psi
            m_wg = tc.wastegate_area_max * st.wastegate_pos * st.p_em / k * psi
            dh_t = f.turbine_isentropic_enthalpy_drop(CP_EXHAUST, t_em, p_out / st.p_em, GAMMA_EXHAUST)
            if dh_t > 1.0 and m_t > 0.0:
                bsr = f.blade_speed_ratio(f.tip_speed(tc.turbine_diameter, st.omega_tc), dh_t)
                eta_t = _clamp(tc.turbine_eta_max * (1.0 - ((bsr - tc.turbine_bsr_opt) / tc.turbine_bsr_opt) ** 2),
                               0.2, tc.turbine_eta_max)
                p_t = f.turbine_power(m_t, dh_t, eta_t)
        else:
            st.p_em = p_out

        # --- torque structure
        pmep = st.p_em - st.p_im
        fmep = self.friction_mep(rpm, st.t_oil) if rpm > 1.0 else 0.0
        bmep = imep - pmep - fmep
        indicated_torque = f.torque_from_mep(imep, self.vd)
        brake_torque = f.torque_from_mep(bmep, self.vd) - (cfg.friction.accessory_torque if rpm > 100.0 else 0.0)
        starter_torque = 0.0
        if inputs.starter:
            mech = cfg.mechanical
            starter_torque = max(0.0, mech.starter_stall_torque * (1.0 - rpm / mech.starter_free_speed_rpm))

        # --- dynamics
        net = brake_torque + starter_torque - load_torque
        if st.omega <= 0.0 and net < 0.0:
            st.omega = 0.0
        else:
            st.omega = max(0.0, st.omega + net / cfg.mechanical.crank_inertia * dt)
        if cfg.turbo.enabled:
            tc = cfg.turbo
            torque_net = (tc.mechanical_efficiency * p_t - ap.p_c) / max(st.omega_tc, 300.0)
            st.omega_tc = max(0.0, st.omega_tc + torque_net / tc.inertia * dt)

        # --- thermal model
        th = cfg.thermal
        heat_wall = (q_released - w_ig) * x_wall * n / 2.0 if combusting else 0.0
        friction_power = max(0.0, fmep) * self.vd * n / 2.0
        open_frac = _clamp((st.t_coolant - (th.thermostat_open_c + 273.15))
                           / (th.thermostat_full_c - th.thermostat_open_c), 0.03, 1.0)
        ua_rad = th.radiator_ua_still + (th.radiator_ua - th.radiator_ua_still) * amb.airflow
        q_rad = ua_rad * open_frac * (st.t_coolant - amb.temperature)
        q_oil_cool = th.oil_cooler_ua * (st.t_oil - st.t_coolant)
        st.t_coolant += (0.85 * heat_wall + q_oil_cool - q_rad) / th.coolant_capacity * dt
        st.t_oil += (0.15 * heat_wall + 0.8 * friction_power - q_oil_cool) / th.oil_capacity * dt
        ip = cfg.intake
        if cfg.turbo.enabled and ip.intercooler_type == "air_to_water":
            q_ic = max(0.0, ap.m_th) * CP_AIR * (ap.t_c - ap.t_ic)
            ua_loop = ip.charge_cooler_ua_still + (ip.charge_cooler_ua - ip.charge_cooler_ua_still) * amb.airflow
            st.t_charge_loop += (q_ic - ua_loop * (st.t_charge_loop - amb.temperature)) / ip.charge_cooler_capacity * dt
        st.t_egt_sensor += (t_em - st.t_egt_sensor) * (1.0 - math.exp(-dt / cfg.exhaust.egt_sensor_time_constant_s))
        st.crank_angle = (st.crank_angle + math.degrees(st.omega) * dt) % 720.0
        st.time += dt

        # --- outputs
        rpm_new = st.omega * 60.0 / TWO_PI
        power = brake_torque * TWO_PI * n
        fuel_lph = m_fuel * 3600.0 / self.fuel.density * 1000.0
        bsfc = m_fuel / power * 3.6e9 if power > 1000.0 else 0.0
        peak_p = self.peak_pressure_estimate(st.p_im, imep, spark, mbt, combusting)
        battery = 14.2 if rpm > 500.0 else (10.8 if inputs.starter else 12.4)
        return PlantOutputs(
            time=st.time, rpm=rpm_new, omega=st.omega, p_ambient=amb.pressure, t_ambient=amb.temperature,
            p_compressor_in=ap.p1, p_boost=st.p_boost, p_im=st.p_im, p_em=st.p_em, p_turbine_out=p_out,
            t_compressor_out=ap.t_c, t_im=t_im, t_exhaust_gas=t_em, t_egt_sensor=st.t_egt_sensor,
            t_coolant=st.t_coolant, t_oil=st.t_oil, t_charge_loop=st.t_charge_loop,
            m_compressor=ap.m_c, m_throttle=ap.m_th, m_cylinder=m_cyl, m_fuel=m_fuel, m_wastegate=m_wg,
            m_turbine=m_t, air_per_cycle=air_per_cycle, fuel_per_cycle=fuel_per_cycle, relative_load=rel_load,
            volumetric_efficiency=ap.ve, lambda_value=lam, combusting=combusting, spark_advance=spark,
            mbt_advance=mbt, knock_limit_advance=knock_lim, knock_intensity=knock, burn_duration_deg=burn,
            eta_spark=eta_spark, eta_lambda=eta_lam, eta_indicated=eta_ind, imep=imep, pmep=pmep, fmep=fmep,
            bmep=bmep, indicated_torque=indicated_torque, brake_torque=brake_torque, load_torque=load_torque,
            starter_torque=starter_torque, brake_power=power, fuel_rate_lph=fuel_lph, bsfc_g_kwh=bsfc,
            peak_pressure_est=peak_p, turbo_rpm=st.omega_tc * 60.0 / TWO_PI, compressor_pr=ap.pr,
            compressor_eta=ap.eta_c, compressor_flow_param=ap.phi, compressor_power=ap.p_c, turbine_power=p_t,
            turbine_eta=eta_t, surge=ap.surge, throttle_pos=st.throttle_pos, wastegate_pos=st.wastegate_pos,
            intake_cam=st.intake_cam, exhaust_cam=st.exhaust_cam, rail_pressure=st.rail_pressure,
            injector_pw=inputs.injector_pw, battery_voltage=battery, heat_to_coolant=heat_wall, radiator_heat=q_rad,
            crank_angle=st.crank_angle,
        )

    # ------------------------------------------------------------------ peak pressure
    def peak_pressure_estimate(self, p_im: float, imep: float, spark: float, mbt: float, combusting: bool) -> float:
        """Correlation for peak cylinder pressure (Pa), fitted to the Wiebe/Woschni cycle model.

        p_max ≈ p_im·r_c^1.32 + k·IMEP·(1 + s·(spark − MBT)); k and s were fitted by least
        squares to :func:`opentune.lab.physics.cycle.simulate_cycle` over 1000–6000 rpm,
        0.35–2.1 bar and 0–15° retard (max error ≈ 23 % vs that model; see
        tests/lab/test_cycle.py). Not validated against measurements.
        """
        p_comp = p_im * self.rc ** 1.32
        if not combusting:
            return p_comp
        phasing = _clamp(1.0 + PEAK_PRESSURE_PHASING * (spark - mbt), 0.3, 1.5)
        return p_comp + PEAK_PRESSURE_GAIN * imep * phasing


PEAK_PRESSURE_GAIN = 3.5
PEAK_PRESSURE_PHASING = 0.033
