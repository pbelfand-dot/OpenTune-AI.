"""Dimensional-consistency and numeric checks for every engineering formula.

The formulas are plain arithmetic, so feeding them ``pint`` quantities verifies the
units of the result; numeric checks pin known textbook values.
"""

import math

import pytest

from opentune.lab import units
from opentune.lab.physics import formulas as f

pint = pytest.importorskip("pint")
ureg = pint.UnitRegistry()
Q = ureg.Quantity

BORE = Q(82.0, "mm")
STROKE = Q(94.6, "mm")
N = Q(3000 / 60, "1/s")  # engine speed in revolutions per second
R = Q(287.05, "J/(kg*K)")
CP = Q(1005.0, "J/(kg*K)")


def dim(q, expected):
    return q.check(expected) if hasattr(q, "check") else False


def test_geometry_dimensions_and_values():
    vd = f.engine_displacement(BORE, STROKE, 6)
    assert dim(vd, "[volume]")
    assert vd.to("cm**3").magnitude == pytest.approx(2997.5, abs=0.5)  # B58: 2,998 cc published
    assert dim(f.mean_piston_speed(STROKE, N), "[velocity]")
    assert f.mean_piston_speed(STROKE, N).to("m/s").magnitude == pytest.approx(9.46)
    vc = f.clearance_volume(f.cylinder_swept_volume(BORE, STROKE), 10.2)
    assert dim(vc, "[volume]")


def test_cylinder_volume_kinematics():
    bore, stroke, rod, rc = 0.082, 0.0946, 0.1445, 10.2
    vd = f.cylinder_swept_volume(bore, stroke)
    vc = f.clearance_volume(vd, rc)
    assert f.cylinder_volume(0.0, bore, stroke, rod, rc) == pytest.approx(vc)
    assert f.cylinder_volume(math.pi, bore, stroke, rod, rc) == pytest.approx(vc + vd)
    # Rod length changes the volume at 90° but not at TDC/BDC.
    v_short = f.cylinder_volume(math.pi / 2, bore, stroke, 0.12, rc)
    v_long = f.cylinder_volume(math.pi / 2, bore, stroke, 0.18, rc)
    assert v_short > v_long
    assert dim(f.cylinder_volume(math.pi / 2, BORE, STROKE, Q(144.5, "mm"), rc), "[volume]")


def test_otto_efficiency():
    assert f.otto_efficiency(10.0, 1.4) == pytest.approx(1 - 10 ** -0.4)
    assert f.otto_efficiency(10.2, 1.3) == pytest.approx(0.5018, abs=1e-3)  # 1 − 10.2^−0.3


def test_flow_function_continuity_at_critical_ratio():
    for gamma in (1.3, 1.33, 1.4):
        pc = f.critical_pressure_ratio(gamma)
        choked = math.sqrt(gamma * (2 / (gamma + 1)) ** ((gamma + 1) / (gamma - 1)))
        assert f.flow_function(pc, gamma) == pytest.approx(choked, rel=1e-9)
        assert f.flow_function(0.1, gamma) == pytest.approx(choked, rel=1e-9)
        assert f.flow_function(1.0, gamma) == 0.0
    assert f.critical_pressure_ratio(1.4) == pytest.approx(0.5283, abs=1e-4)


def test_restriction_flow_dimensions():
    mdot = f.restriction_mass_flow(Q(10, "cm**2"), Q(200, "kPa"), Q(320, "K"), Q(100, "kPa"), R, 1.4)
    assert dim(mdot, "[mass]/[time]")
    assert mdot.to("kg/s").magnitude > 0


def test_throttle_area():
    d, closed = 0.074, math.radians(7)
    assert f.throttle_open_area(d, closed, closed, 0.0) == pytest.approx(0.0, abs=1e-12)
    wide = f.throttle_open_area(d, math.radians(90), closed, 0.0)
    assert wide == pytest.approx(math.pi * d**2 / 4, rel=1e-9)  # cos 90° = 0 → full bore
    assert dim(f.throttle_open_area(Q(74, "mm"), math.radians(45), closed, Q(1, "mm**2")), "[area]")


def test_speed_density():
    mdot = f.speed_density_mass_flow(0.95, Q(200, "kPa"), Q(320, "K"), R, Q(3.0, "L"), N)
    assert dim(mdot, "[mass]/[time]")
    # ρ = 200e3/(287.05·320) = 2.177 kg/m³ → 0.95·2.177·0.003·25 = 0.155 kg/s
    assert mdot.to("kg/s").magnitude == pytest.approx(0.1551, rel=1e-3)
    assert f.backpressure_ve_factor(10.2, 1.0, 1.0, 1.4) == pytest.approx(1.0)
    assert f.backpressure_ve_factor(10.2, 2.0, 1.0, 1.4) < 1.0


def test_torque_power_mep():
    t = f.torque_from_mep(Q(20, "bar"), Q(2.998, "L"))
    assert dim(t, "[energy]")  # N·m
    assert t.to("N*m").magnitude == pytest.approx(477.1, abs=0.2)
    assert f.mep_from_torque(477.1, 2.998e-3) == pytest.approx(20e5, rel=1e-3)
    p = f.shaft_power(Q(500, "N*m"), N)
    assert dim(p, "[power]")
    assert p.to("kW").magnitude == pytest.approx(157.08, abs=0.01)


def test_friction_mep_dimensions():
    fm = f.quadratic_fmep(Q(0.4, "bar"), Q(0.035, "bar*s/m"), Q(0.0012, "bar*s**2/m**2"), Q(10, "m/s"))
    assert dim(fm, "[pressure]")
    assert fm.to("bar").magnitude == pytest.approx(0.87)


def test_turbomachinery_dimensions():
    omega = Q(150000 * 2 * math.pi / 60, "rad/s")
    u = f.tip_speed(Q(50, "mm"), omega)
    assert dim(u, "[velocity]")
    dh = f.compressor_isentropic_enthalpy_rise(CP, Q(298, "K"), 2.0, 1.4)
    assert dim(dh, "[energy]/[mass]")
    psi = f.compressor_head_parameter(dh, u)
    assert psi.to("").dimensionless
    phi = f.compressor_flow_parameter(Q(0.2, "kg/s"), Q(1.18, "kg/m**3"), Q(50, "mm"), u)
    assert phi.to("").dimensionless
    assert dim(f.compressor_power(Q(0.2, "kg/s"), dh, 0.75), "[power]")
    assert dim(f.compressor_outlet_temperature(Q(298, "K"), 2.0, 1.4, 0.75), "[temperature]")
    dh_t = f.turbine_isentropic_enthalpy_drop(Q(1150, "J/(kg*K)"), Q(1150, "K"), 0.5, 1.33)
    assert dim(f.turbine_power(Q(0.2, "kg/s"), dh_t, 0.7), "[power]")
    assert f.blade_speed_ratio(u, dh_t).to("").dimensionless


def test_compressor_temperature_value():
    # PR 2, η 0.75, 298 K: ΔT_is = 298·(2^0.2857 − 1) = 65.3 K → actual 87.1 K
    assert f.compressor_outlet_temperature(298.0, 2.0, 1.4, 0.75) == pytest.approx(298 + 87.1, abs=0.2)


def test_heat_and_fuel_relationships():
    assert f.heat_exchanger_outlet_temperature(400.0, 300.0, 0.8) == pytest.approx(320.0)
    assert dim(f.temperature_rise(Q(1000, "J"), Q(1, "kg"), CP), "[temperature]")
    assert f.fuel_mass_flow_from_lambda(0.2, 0.85, 14.6) == pytest.approx(0.2 / (0.85 * 14.6))
    bsfc = f.specific_fuel_consumption(Q(20, "g/s"), Q(250, "kW"))
    assert bsfc.to("g/(kW*h)").magnitude == pytest.approx(288.0)


def test_wiebe():
    assert f.wiebe_fraction(-10, 0, 50, 5, 2) == 0.0
    assert f.wiebe_fraction(50, 0, 50, 5, 2) == pytest.approx(1 - math.exp(-5))
    y50 = f.wiebe_fraction_position(0.5, 5, 2)
    assert f.wiebe_fraction(50 * y50, 0, 50, 5, 2) == pytest.approx(0.5)


def test_unit_conversions():
    assert units.kw_to_hp(units.W_PER_HP / 1000) == pytest.approx(1.0)
    assert units.kw_to_ps(units.W_PER_PS / 1000) == pytest.approx(1.0)
    assert units.HP_RPM_CONSTANT == pytest.approx(5252.113, abs=1e-3)
    # Consistency: hp from lb·ft and rpm equals hp from SI power
    p_w = units.power_w(500.0, 5252.113)
    assert units.kw_to_hp(p_w / 1000) == pytest.approx(units.hp_from_lbft_rpm(units.nm_to_lbft(500.0), 5252.113))
    assert units.c_to_k(25.0) == pytest.approx(298.15)
    assert units.pa_to_psi(units.PA_PER_PSI) == pytest.approx(1.0)
