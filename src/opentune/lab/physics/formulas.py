"""Engineering relationships used by the engine model.

Every function here is plain arithmetic on its arguments so it works with
floats *and* with ``pint`` quantities. The test-suite exploits this to check the
dimensional consistency of each formula (``tests/lab/test_formulas.py``).

Conventions: SI units; engine speed ``n`` in revolutions per second (1/s);
angular speed ``omega`` in rad/s; pressure ratios are dimensionless.

Primary references (relationships, not numbers):

* J. B. Heywood, *Internal Combustion Engine Fundamentals*, 2nd ed., McGraw-Hill
  (2018): mean effective pressure, Otto efficiency, volumetric efficiency,
  compressible flow through restrictions (App. C), throttle area, Wiebe function.
* L. Eriksson & L. Nielsen, *Modeling and Control of Engines and Drivelines*,
  Wiley (2014): mean-value engine models — manifold filling/emptying, throttle as
  a restriction, compressor head/flow parameters, turbine as a restriction,
  turbo shaft power balance, torque structure with gross/pumping/friction work.
"""

from __future__ import annotations

import math

PI = math.pi


# --------------------------------------------------------------------------- geometry
def cylinder_swept_volume(bore, stroke):
    """V_d,cyl = π/4 · B² · S."""
    return PI / 4.0 * bore**2 * stroke


def engine_displacement(bore, stroke, cylinders):
    return cylinder_swept_volume(bore, stroke) * cylinders


def clearance_volume(swept_volume_cyl, compression_ratio):
    """V_c = V_d / (r_c − 1)."""
    return swept_volume_cyl / (compression_ratio - 1.0)


def mean_piston_speed(stroke, n):
    """S̄_p = 2·S·n  (n in rev/s)."""
    return 2.0 * stroke * n


def cylinder_volume(theta_rad, bore, stroke, rod_length, compression_ratio):
    """Instantaneous cylinder volume vs crank angle (θ = 0 at TDC), slider-crank kinematics.

    V(θ) = V_c + (π B²/4)·(l + a − s),  s = a·cos θ + √(l² − a² sin² θ),  a = S/2.
    """
    a = stroke / 2.0
    vc = clearance_volume(cylinder_swept_volume(bore, stroke), compression_ratio)
    s = a * math.cos(theta_rad) + (rod_length**2 - a**2 * math.sin(theta_rad) ** 2) ** 0.5
    return vc + PI * bore**2 / 4.0 * (rod_length + a - s)


# --------------------------------------------------------------------------- thermodynamics
def otto_efficiency(compression_ratio, gamma):
    """Ideal constant-volume cycle efficiency η = 1 − r_c^(1−γ)."""
    return 1.0 - compression_ratio ** (1.0 - gamma)


def air_density(pressure, temperature, r_gas):
    """Ideal gas ρ = p / (R·T)."""
    return pressure / (r_gas * temperature)


def critical_pressure_ratio(gamma):
    """(2/(γ+1))^(γ/(γ−1)); below this downstream/upstream ratio the flow is choked."""
    return (2.0 / (gamma + 1.0)) ** (gamma / (gamma - 1.0))


def flow_function(pressure_ratio, gamma):
    """Dimensionless isentropic flow function Ψ(Π) for Π = p_down/p_up (0 ≤ Π ≤ 1).

    Ψ = √(2γ/(γ−1) · (Π^(2/γ) − Π^((γ+1)/γ))), constant (choked) for Π below critical.
    """
    pr = min(max(pressure_ratio, 0.0), 1.0)
    pr = max(pr, critical_pressure_ratio(gamma))
    value = 2.0 * gamma / (gamma - 1.0) * (pr ** (2.0 / gamma) - pr ** ((gamma + 1.0) / gamma))
    return max(value, 0.0) ** 0.5


def restriction_mass_flow(area, p_up, t_up, p_down, r_gas, gamma, discharge_coefficient=1.0):
    """Compressible flow through a restriction: ṁ = C_d·A·p_up/√(R·T_up)·Ψ(p_down/p_up)."""
    psi = flow_function(p_down / p_up, gamma)
    return discharge_coefficient * area * p_up / (r_gas * t_up) ** 0.5 * psi


def throttle_open_area(diameter, angle_rad, closed_angle_rad, leak_area):
    """Projected throttle open area (Heywood): A = πD²/4·(1 − cos ψ / cos ψ₀) + A_leak."""
    return PI * diameter**2 / 4.0 * (1.0 - math.cos(angle_rad) / math.cos(closed_angle_rad)) + leak_area


def speed_density_mass_flow(volumetric_efficiency, pressure, temperature, r_gas, displacement, n):
    """Four-stroke cylinder air flow ṁ = η_v · ρ · V_d · n/2."""
    return volumetric_efficiency * pressure / (r_gas * temperature) * displacement * n / 2.0


def backpressure_ve_factor(compression_ratio, p_exhaust, p_intake, gamma):
    """Residual-gas correction of volumetric efficiency (Eriksson & Nielsen):

    (r_c − (p_em/p_im)^(1/γ)) / (r_c − 1) — equals 1 when p_em = p_im.
    """
    return (compression_ratio - (p_exhaust / p_intake) ** (1.0 / gamma)) / (compression_ratio - 1.0)


# --------------------------------------------------------------------------- work, torque, power
def torque_from_mep(mean_effective_pressure, displacement):
    """Four-stroke: T = MEP · V_d / (4π)."""
    return mean_effective_pressure * displacement / (4.0 * PI)


def mep_from_torque(torque, displacement):
    return 4.0 * PI * torque / displacement


def shaft_power(torque, n):
    """P = T·ω = 2π·n·T."""
    return 2.0 * PI * n * torque


def mep_from_work_per_cycle(work_per_cycle, displacement):
    return work_per_cycle / displacement


def quadratic_fmep(a, b, c, piston_speed):
    """Friction MEP as a quadratic in mean piston speed: FMEP = a + b·S̄p + c·S̄p²."""
    return a + b * piston_speed + c * piston_speed**2


def specific_fuel_consumption(fuel_mass_flow, power):
    """BSFC = ṁ_f / P (kg/J). Multiply by 3.6e9 for g/kWh."""
    return fuel_mass_flow / power


def fuel_mass_flow_from_lambda(air_mass_flow, lambda_value, stoich_afr):
    return air_mass_flow / (lambda_value * stoich_afr)


# --------------------------------------------------------------------------- turbomachinery
def tip_speed(diameter, omega):
    """U = ω·D/2."""
    return omega * diameter / 2.0


def compressor_isentropic_enthalpy_rise(cp, t_in, pressure_ratio, gamma):
    """Δh_is = c_p·T₁·(Π^((γ−1)/γ) − 1), Π = p₂/p₁ ≥ 1."""
    return cp * t_in * (pressure_ratio ** ((gamma - 1.0) / gamma) - 1.0)


def compressor_head_parameter(dh_isentropic, tip_speed_value):
    """Ψ = 2·Δh_is / U²  (dimensionless)."""
    return 2.0 * dh_isentropic / tip_speed_value**2


def compressor_flow_parameter(mass_flow, density, diameter, tip_speed_value):
    """Φ = ṁ / (ρ₁·(π/4)·D²·U)  (dimensionless)."""
    return mass_flow / (density * PI / 4.0 * diameter**2 * tip_speed_value)


def compressor_power(mass_flow, dh_isentropic, efficiency):
    """P_c = ṁ·Δh_is / η_c."""
    return mass_flow * dh_isentropic / efficiency


def compressor_outlet_temperature(t_in, pressure_ratio, gamma, efficiency):
    """T₂ = T₁·(1 + (Π^((γ−1)/γ) − 1)/η_c)."""
    return t_in * (1.0 + (pressure_ratio ** ((gamma - 1.0) / gamma) - 1.0) / efficiency)


def turbine_isentropic_enthalpy_drop(cp, t_in, expansion_ratio, gamma):
    """Δh_is = c_p·T₃·(1 − Π^((γ−1)/γ)), Π = p₄/p₃ ≤ 1."""
    return cp * t_in * (1.0 - expansion_ratio ** ((gamma - 1.0) / gamma))


def turbine_power(mass_flow, dh_isentropic, efficiency):
    return mass_flow * dh_isentropic * efficiency


def blade_speed_ratio(tip_speed_value, dh_isentropic):
    """BSR = U / C_s with C_s = √(2·Δh_is)."""
    return tip_speed_value / (2.0 * dh_isentropic) ** 0.5


# --------------------------------------------------------------------------- heat
def heat_exchanger_outlet_temperature(t_in, t_coolant, effectiveness):
    """ε-model: T_out = T_in − ε·(T_in − T_coolant)."""
    return t_in - effectiveness * (t_in - t_coolant)


def temperature_rise(heat, mass, cp):
    """ΔT = Q / (m·c_p)."""
    return heat / (mass * cp)


# --------------------------------------------------------------------------- combustion
def wiebe_fraction(theta, theta_start, duration, a, m):
    """Wiebe mass-fraction-burned x_b(θ) = 1 − exp(−a·((θ−θ₀)/Δθ)^(m+1)), clamped to [0, 1]."""
    if theta <= theta_start:
        return 0.0
    y = (theta - theta_start) / duration
    if y >= 1.0:
        y = 1.0
    return 1.0 - math.exp(-a * y ** (m + 1.0))


def wiebe_fraction_position(fraction, a, m):
    """Normalised position y (0..1) where the Wiebe curve reaches ``fraction``."""
    return (-math.log(1.0 - fraction) / a) ** (1.0 / (m + 1.0))
