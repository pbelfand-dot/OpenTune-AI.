"""Steady-state operating-point solver (turbo matching) for the mean-value model.

Given engine speed and a target intake-manifold pressure at wide-open throttle,
solve the algebraic steady state of the same sub-models used by the dynamic
plant: compressor speed line → turbo speed and efficiency; charge cooling;
speed-density air flow; combustion; and the turbine/wastegate split that makes
turbine power balance compressor power. ~1 ms per point, so the optimiser and the
baseline-calibration generator can call it thousands of times.

Returns ``feasible = False`` when the target cannot be reached even with the
wastegate closed (turbo too small / too slow), or when the compressor would be
past choke.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

from opentune.lab.physics import formulas as f
from opentune.lab.physics.constants import (
    CP_AIR,
    CP_EXHAUST,
    GAMMA_AIR,
    GAMMA_EXHAUST,
    R_AIR,
    R_EXHAUST,
)
from opentune.lab.physics.plant import EnginePlant, flow_function_regularized

TWO_PI = 2.0 * math.pi


@dataclass
class SteadyPoint:
    feasible: bool
    reason: str
    rpm: float
    p_im: float
    p_boost: float
    p_em: float
    t_im: float
    t_exhaust: float
    t_compressor_out: float
    air_flow: float
    fuel_flow: float
    lambda_value: float
    spark: float
    mbt: float
    knock_limit: float
    volumetric_efficiency: float
    imep: float
    pmep: float
    fmep: float
    brake_torque: float
    brake_power: float
    bsfc_g_kwh: float
    turbo_rpm: float
    compressor_pr: float
    compressor_eta: float
    compressor_flow_param: float
    wastegate_pos: float
    peak_pressure: float

    def to_dict(self) -> dict:
        return asdict(self)


def steady_operating_point(plant: EnginePlant, rpm: float, p_im: float, lam: float = 0.88,
                           spark: float | None = None, knock_margin: float = 1.5,
                           t_coolant: float = 363.15, t_oil: float = 368.15,
                           t_charge_loop: float | None = None, intake_cam: float = 0.0,
                           exhaust_cam: float = 0.0) -> SteadyPoint:
    """Solve the wide-open-throttle steady state at ``rpm`` with manifold pressure ``p_im``.

    ``spark`` defaults to min(MBT, knock limit − margin).
    """
    cfg = plant.config
    amb = plant.ambient
    tc = cfg.turbo
    ip = cfg.intake
    n = rpm / 60.0
    t_loop = t_charge_loop if t_charge_loop is not None else amb.temperature + 8.0
    p_em = p_im
    t_im = amb.temperature + 15.0
    result: SteadyPoint | None = None
    for _ in range(6):  # fixed-point iteration over p_em and t_im
        ve = plant.volumetric_efficiency(rpm, p_im, p_em, intake_cam, exhaust_cam)
        m_air = f.speed_density_mass_flow(ve, p_im, t_im, R_AIR, plant.vd, n)
        air_cyc = m_air / (n / 2.0)
        fuel_cyc = air_cyc / (plant.fuel.stoich_afr * lam)
        m_fuel = fuel_cyc * n / 2.0
        rel = air_cyc / (plant.vd * 101325.0 / (R_AIR * 298.15))
        mbt = plant.mbt_advance(rpm, rel)
        klim = plant.knock_limit(rpm, p_im, t_im, t_coolant, lam)
        sp = spark if spark is not None else min(mbt, klim - knock_margin)
        ct = plant.combustion_terms(rpm, p_im, t_im, t_coolant, air_cyc, fuel_cyc, sp, p_em=p_em)
        t_em = ct.t_exhaust
        m_e = m_air + m_fuel
        p_out = amb.pressure + cfg.exhaust.backpressure_coeff * m_e * m_e
        p1 = max(amb.pressure - ip.air_filter_dp_coeff * m_air * m_air, 0.7 * amb.pressure)
        if tc.enabled:
            p_boost = p_im + ip.intercooler_dp_coeff * m_air * m_air
            pr = max(p_boost / p1, 1.0)
            dh = f.compressor_isentropic_enthalpy_rise(CP_AIR, amb.temperature, pr, GAMMA_AIR)
            rho1 = p1 / (R_AIR * amb.temperature)
            phi_u = m_air / (rho1 * plant.comp_area)
            u2 = (2.0 * dh + tc.compressor_head_slope * phi_u * phi_u) / tc.compressor_head_max
            u = math.sqrt(max(u2, 1.0))
            phi = phi_u / u
            omega_tc = 2.0 * u / tc.compressor_diameter
            eta_c = max(0.45, min(tc.compressor_eta_max, tc.compressor_eta_max
                                  - tc.compressor_eta_curvature * (phi - tc.compressor_phi_opt) ** 2))
            p_c = m_air * dh / eta_c
            t_c = amb.temperature + dh / (CP_AIR * eta_c)
            if ip.intercooler_type == "air_to_water":
                t_ic = f.heat_exchanger_outlet_temperature(t_c, t_loop, ip.intercooler_effectiveness)
            elif ip.intercooler_type == "air_to_air":
                eff = ip.intercooler_effectiveness * (0.35 + 0.65 * amb.airflow)
                t_ic = f.heat_exchanger_outlet_temperature(t_c, amb.temperature, eff)
            else:
                t_ic = t_c
            # turbine: find p_em with η_m·P_t = P_c using the turbine-only flow; wastegate takes the rest
            k = 1.0 / math.sqrt(R_EXHAUST * t_em)

            def turbine_power(p: float, p_out: float = p_out, k: float = k, t_em: float = t_em,
                              omega_tc: float = omega_tc) -> tuple[float, float]:
                psi = flow_function_regularized(p_out / p, GAMMA_EXHAUST)
                m_t = tc.turbine_area * p * k * psi
                dh_t = f.turbine_isentropic_enthalpy_drop(CP_EXHAUST, t_em, p_out / p, GAMMA_EXHAUST)
                if dh_t <= 0.0:
                    return 0.0, m_t
                bsr = f.blade_speed_ratio(f.tip_speed(tc.turbine_diameter, omega_tc), dh_t)
                eta_t = max(0.2, tc.turbine_eta_max * (1.0 - ((bsr - tc.turbine_bsr_opt) / tc.turbine_bsr_opt) ** 2))
                return tc.mechanical_efficiency * m_t * dh_t * eta_t, m_t

            # p where turbine alone swallows all exhaust flow (wastegate closed)
            lo, hi = p_out * 1.0001, 20.0 * p_out
            for _ in range(60):
                mid = 0.5 * (lo + hi)
                if turbine_power(mid)[1] < m_e:
                    lo = mid
                else:
                    hi = mid
            p_closed = hi
            p_avail, _ = turbine_power(p_closed)
            feasible = phi <= tc.compressor_flow_choke
            reason = "" if feasible else "compressor past choke"
            if p_avail < p_c:
                feasible = False
                reason = reason or "turbo cannot reach target (wastegate closed)"
                p_em_new, wg = p_closed, 0.0
            else:
                lo, hi = p_out * 1.0001, p_closed
                for _ in range(60):
                    mid = 0.5 * (lo + hi)
                    if turbine_power(mid)[0] < p_c:
                        lo = mid
                    else:
                        hi = mid
                p_em_new = hi
                _, m_t = turbine_power(p_em_new)
                m_wg = max(0.0, m_e - m_t)
                cap = tc.wastegate_area_max * p_em_new * k * flow_function_regularized(p_out / p_em_new,
                                                                                         GAMMA_EXHAUST)
                wg = min(1.0, m_wg / cap) if cap > 0 else 1.0
            turbo_rpm = omega_tc * 60.0 / TWO_PI
        else:
            p_boost = p1
            pr, eta_c, phi, t_c, wg, turbo_rpm = 1.0, 0.0, 0.0, amb.temperature, 1.0, 0.0
            t_ic = amb.temperature
            p_em_new = p_out
            feasible, reason = p_im <= p1, "" if p_im <= p1 else "naturally aspirated: target above ambient"
        t_im_new = t_ic + 3.0
        converged = abs(p_em_new - p_em) < 10.0 and abs(t_im_new - t_im) < 0.05
        p_em, t_im = p_em_new, t_im_new
        pmep = p_em - p_im
        fmep = plant.friction_mep(rpm, t_oil)
        bmep = ct.imep - pmep - fmep
        torque = f.torque_from_mep(bmep, plant.vd) - cfg.friction.accessory_torque
        power = torque * TWO_PI * n
        result = SteadyPoint(
            feasible=feasible, reason=reason, rpm=rpm, p_im=p_im, p_boost=p_boost, p_em=p_em, t_im=t_im,
            t_exhaust=t_em, t_compressor_out=t_c, air_flow=m_air, fuel_flow=m_fuel, lambda_value=lam, spark=sp,
            mbt=mbt, knock_limit=klim, volumetric_efficiency=ve, imep=ct.imep, pmep=pmep, fmep=fmep,
            brake_torque=torque, brake_power=power, bsfc_g_kwh=m_fuel / power * 3.6e9 if power > 1000 else 0.0,
            turbo_rpm=turbo_rpm, compressor_pr=pr, compressor_eta=eta_c, compressor_flow_param=phi,
            wastegate_pos=wg, peak_pressure=plant.peak_pressure_estimate(p_im, ct.imep, sp, mbt, True),
        )
        if converged:
            break
    assert result is not None
    return result
