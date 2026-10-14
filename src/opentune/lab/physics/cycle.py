"""Crank-angle-resolved single-zone cycle model (closed part of the cycle).

Used for cross-checks and Engine Builder visualisation, not inside the real-time
loop. Between IVC (taken at BDC, −180°) and EVO (+180°) the cylinder is a closed
ideal-gas system; heat release follows a Wiebe function and the first law gives

    dp/dθ = (γ − 1)/V · dQ/dθ − γ · p/V · dV/dθ          (Heywood 2018, ch. 9)

Wall heat transfer uses the Woschni (1967) correlation (Heywood 2018, ch. 12):

    h = 3.26 · B^−0.2 · p^0.8 · T^−0.55 · w^0.8        [W/m²K; B in m, p in kPa]
    w = 2.28·S̄p + 3.24e−3 · (V_d·T_r/(p_r·V_r)) · (p − p_mot)

with a uniform wall temperature. Slider-crank kinematics use the configured rod
length, so rod length and compression ratio affect the pressure trace.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from opentune.lab.physics import formulas as f
from opentune.lab.physics.config import EngineConfig
from opentune.lab.physics.constants import GAMMA_CYCLE, R_AIR


@dataclass
class CycleResult:
    theta: list[float]  # crank angle, ° (0 = firing TDC)
    volume: list[float]  # m³
    pressure: list[float]  # Pa
    burned: list[float]  # mass fraction burned
    imep_gross: float  # Pa
    peak_pressure: float  # Pa
    peak_pressure_angle: float  # ° ATDC
    ca10: float
    ca50: float
    ca90: float
    gross_efficiency: float  # gross indicated work / fuel energy released

    def to_dict(self, stride: int = 4) -> dict:
        return {
            "theta": self.theta[::stride], "volume": self.volume[::stride], "pressure": self.pressure[::stride],
            "burned": self.burned[::stride], "imep_gross": self.imep_gross, "peak_pressure": self.peak_pressure,
            "peak_pressure_angle": self.peak_pressure_angle, "ca10": self.ca10, "ca50": self.ca50, "ca90": self.ca90,
            "gross_efficiency": self.gross_efficiency,
        }


def simulate_cycle(config: EngineConfig, p_ivc: float, t_ivc: float, spark_btdc: float, burn_duration_deg: float,
                   lambda_value: float = 1.0, ignition_delay_deg: float | None = None,
                   heat_release_fraction: float | None = None, step_deg: float = 0.5,
                   gamma: float = GAMMA_CYCLE, rpm: float = 3000.0, wall_temperature: float = 450.0,
                   heat_transfer: bool = True) -> CycleResult:
    g = config.geometry
    comb = config.combustion
    delay = comb.ignition_delay_deg if ignition_delay_deg is None else ignition_delay_deg
    v_ivc = f.cylinder_volume(math.pi, g.bore, g.stroke, g.rod_length, g.compression_ratio)
    mass = p_ivc * v_ivc / (R_AIR * t_ivc)  # trapped charge (air approximation)
    fuel = config.fuel
    air = mass / (1.0 + 1.0 / (lambda_value * fuel.stoich_afr))
    fuel_mass = air / (lambda_value * fuel.stoich_afr)
    if heat_release_fraction is None:
        heat_release_fraction = comb.combustion_efficiency
    q_total = fuel_mass * fuel.lower_heating_value * min(1.0, lambda_value) * heat_release_fraction
    theta_start = -spark_btdc + delay
    swept = f.cylinder_swept_volume(g.bore, g.stroke)
    piston_speed = f.mean_piston_speed(g.stroke, rpm / 60.0)
    head_area = math.pi * g.bore**2 / 4.0
    seconds_per_degree = 1.0 / (6.0 * max(rpm, 100.0))
    q_wall_total = 0.0

    thetas: list[float] = []
    volumes: list[float] = []
    pressures: list[float] = []
    burned: list[float] = []
    p = p_ivc
    theta = -180.0
    v = v_ivc
    xb_prev = 0.0
    work = 0.0
    steps = int(round(360.0 / step_deg))
    for _ in range(steps + 1):
        thetas.append(theta)
        volumes.append(v)
        pressures.append(p)
        burned.append(xb_prev)
        if theta >= 180.0:
            break
        theta_next = theta + step_deg
        v_next = f.cylinder_volume(math.radians(theta_next), g.bore, g.stroke, g.rod_length, g.compression_ratio)
        xb_next = f.wiebe_fraction(theta_next, theta_start, burn_duration_deg, comb.wiebe_a, comb.wiebe_m)
        dq = q_total * (xb_next - xb_prev)
        if heat_transfer:
            temperature = p * v / (mass * R_AIR)
            p_motored = p_ivc * (v_ivc / v) ** gamma
            w = 2.28 * piston_speed
            if theta >= theta_start:
                w += 3.24e-3 * swept * t_ivc / (p_ivc * v_ivc) * max(0.0, p - p_motored)
            h = 3.26 * g.bore ** -0.2 * (p / 1000.0) ** 0.8 * temperature ** -0.55 * w ** 0.8
            area = 2.0 * head_area + math.pi * g.bore * (v - f.clearance_volume(swept, g.compression_ratio)) / head_area
            dq_wall = h * area * (temperature - wall_temperature) * seconds_per_degree * step_deg
            q_wall_total += dq_wall
            dq -= dq_wall
        # Integrate the energy equation over the step with a midpoint volume (second-order accurate).
        v_mid = 0.5 * (v + v_next)
        dv = v_next - v
        p_pred = p + ((gamma - 1.0) * dq - gamma * p * dv) / v_mid
        p_mid = 0.5 * (p + p_pred)
        p_next = p + ((gamma - 1.0) * dq - gamma * p_mid * dv) / v_mid
        work += p_mid * dv
        p, v, theta, xb_prev = p_next, v_next, theta_next, xb_next

    def angle_at(fraction: float) -> float:
        for th, xb in zip(thetas, burned, strict=False):
            if xb >= fraction:
                return th
        return thetas[-1]

    peak_index = max(range(len(pressures)), key=pressures.__getitem__)
    return CycleResult(
        theta=thetas, volume=volumes, pressure=pressures, burned=burned,
        imep_gross=work / swept, peak_pressure=pressures[peak_index], peak_pressure_angle=thetas[peak_index],
        ca10=angle_at(0.10), ca50=angle_at(0.50), ca90=angle_at(0.90),
        gross_efficiency=work / q_total if q_total > 0 else 0.0,
    )


def mbt_sweep(config: EngineConfig, p_ivc: float, t_ivc: float, burn_duration_deg: float,
              lambda_value: float = 1.0, advances: range | None = None,
              rpm: float = 3000.0) -> list[tuple[float, float, float]]:
    """IMEP and peak pressure vs spark advance: list of (advance °BTDC, IMEP Pa, peak Pa)."""
    advances = advances or range(0, 52, 2)
    out = []
    for adv in advances:
        res = simulate_cycle(config, p_ivc, t_ivc, float(adv), burn_duration_deg, lambda_value, rpm=rpm)
        out.append((float(adv), res.imep_gross, res.peak_pressure))
    return out
