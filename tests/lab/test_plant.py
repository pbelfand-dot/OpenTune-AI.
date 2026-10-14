import math

import pytest

from opentune.lab.physics import EnginePlant, PlantInputs, get_preset
from opentune.lab.physics.plant import (
    bidirectional_flow,
    flow_function_regularized,
    lambda_efficiency,
)

from .conftest import hold_point


def test_stopped_engine_stays_stopped():
    plant = EnginePlant(get_preset("b58-inspired"))
    for _ in range(500):
        out = plant.step(PlantInputs(), 0.0, 0.001)
    assert out.rpm == 0.0 and out.brake_torque == 0.0
    assert out.p_im == pytest.approx(out.p_ambient, rel=1e-3)


def test_starter_and_fuel_start_the_engine():
    plant = EnginePlant(get_preset("b58-inspired"))
    cfg = plant.config
    inp = PlantInputs(throttle_cmd=4.0, starter=True, fuel_enable=False, spark_advance=10.0, rail_pressure_cmd=50e5)
    out = None
    for _ in range(4000):
        if out is not None and out.rpm > 150:
            inp.fuel_enable = True
            q = plant.injector_flow(plant.state.rail_pressure)
            fuel = max(out.air_per_cycle, 1e-6) / (cfg.fuel.stoich_afr * 0.95) / cfg.geometry.cylinders
            inp.injector_pw = fuel / q + cfg.fuel_system.injector_dead_time
        if out is not None and out.rpm > 600:
            inp.starter = False
        out = plant.step(inp, 0.0, 0.001)
    assert out.combusting
    assert out.rpm > 600


def test_idle_manifold_vacuum(b58_plant):
    out = hold_point(b58_plant, 750, throttle=2.0, lam=1.0, seconds=2.0, wastegate=100)
    assert out.p_im < 0.5 * out.p_ambient
    assert out.brake_torque < 60


@pytest.mark.parametrize("rpm", [2000, 3500, 5500])
def test_full_load_torque_is_plausible(b58_plant, rpm):
    out = hold_point(b58_plant, rpm, boost_target=2.1e5, seconds=4.0)
    assert out.p_boost == pytest.approx(2.1e5, rel=0.03)
    assert 380 < out.brake_torque < 650
    assert 230 < out.bsfc_g_kwh < 340  # rich full-load BSFC band for SI engines
    assert 700 < out.t_exhaust_gas - 273.15 < 1050
    assert out.knock_limit_advance < out.mbt_advance  # boosted operation is knock limited


def test_energy_balance(b58_plant):
    out = hold_point(b58_plant, 3000, boost_target=1.8e5, seconds=2.0)
    fuel_power = out.m_fuel * b58_plant.fuel.lower_heating_value
    indicated_power = out.imep * b58_plant.vd * (out.rpm / 60) / 2
    assert 0.3 < indicated_power / fuel_power < 0.45
    assert out.brake_power < indicated_power < fuel_power
    assert out.heat_to_coolant < fuel_power


def test_spark_retard_costs_torque_and_raises_egt(b58_plant):
    base = hold_point(b58_plant, 3000, throttle=30, lam=1.0, spark_offset=0.0, seconds=2.0, wastegate=100)
    plant2 = EnginePlant(get_preset("b58-inspired"))
    plant2.warm_start(3000)
    retarded = hold_point(plant2, 3000, throttle=30, lam=1.0, spark_offset=-12.0, seconds=2.0, wastegate=100)
    assert retarded.brake_torque < base.brake_torque * 0.97
    assert retarded.t_exhaust_gas > base.t_exhaust_gas + 20


def test_knock_limit_trends(b58_plant):
    p = b58_plant
    ref = p.knock_limit(3000, 1.8e5, 313.15, 363.15, 1.0)
    assert p.knock_limit(3000, 2.2e5, 313.15, 363.15, 1.0) < ref  # more boost → less advance
    assert p.knock_limit(3000, 1.8e5, 333.15, 363.15, 1.0) < ref  # hotter charge → less advance
    assert p.knock_limit(3000, 1.8e5, 313.15, 363.15, 0.85) > ref  # richer → more advance
    assert p.knock_limit(4500, 1.8e5, 313.15, 363.15, 1.0) > ref  # faster → more advance
    p.config.combustion.octane_aki = 98
    assert p.knock_limit(3000, 1.8e5, 313.15, 363.15, 1.0) > ref  # higher octane → more advance


def test_knock_occurs_beyond_limit(b58_plant):
    out = hold_point(b58_plant, 2500, boost_target=2.0e5, spark_offset=6.0, seconds=2.0)
    assert out.knock_intensity > 0.5
    plant2 = EnginePlant(get_preset("b58-inspired"))
    plant2.warm_start(2500)
    safe = hold_point(plant2, 2500, boost_target=2.0e5, spark_offset=-2.0, seconds=2.0)
    assert safe.knock_intensity == 0.0


def test_lambda_shape():
    assert lambda_efficiency(1.0) == pytest.approx(1.0)
    assert lambda_efficiency(0.88) > 1.0  # slight rich power benefit
    assert lambda_efficiency(0.70) < lambda_efficiency(0.85)
    assert lambda_efficiency(1.1) > 1.0  # best efficiency slightly lean
    assert lambda_efficiency(1.4) < 1.0


def test_flow_helpers():
    assert flow_function_regularized(1.0, 1.4) == 0.0
    assert flow_function_regularized(0.999, 1.4) < flow_function_regularized(0.98, 1.4)
    fwd = bidirectional_flow(1e-3, 2e5, 300, 1e5, 300, 287, 1.4)
    rev = bidirectional_flow(1e-3, 1e5, 300, 2e5, 300, 287, 1.4)
    assert fwd == pytest.approx(-rev)


def test_deterministic(b58_plant):
    a = hold_point(b58_plant, 3000, boost_target=2.0e5, seconds=1.0, spark_offset=4.0)
    b_plant = EnginePlant(get_preset("b58-inspired"))
    b_plant.warm_start(2000)
    b = hold_point(b_plant, 3000, boost_target=2.0e5, seconds=1.0, spark_offset=4.0)
    assert a.to_dict() == b.to_dict()


def test_time_step_convergence():
    results = []
    for dt in (0.002, 0.001, 0.0005):
        plant = EnginePlant(get_preset("b58-inspired"))
        plant.warm_start(3000)
        results.append(hold_point(plant, 3000, boost_target=1.9e5, seconds=3.0, dt=dt).brake_torque)
    assert results[0] == pytest.approx(results[2], rel=0.02)
    assert results[1] == pytest.approx(results[2], rel=0.01)


def test_naturally_aspirated_variant():
    cfg = get_preset("b58-inspired")
    cfg.turbo.enabled = False
    plant = EnginePlant(cfg)
    plant.warm_start(4000)
    out = hold_point(plant, 4000, seconds=2.0)
    assert out.p_boost == pytest.approx(out.p_compressor_in, rel=1e-6)
    assert out.p_im < out.p_ambient
    assert 200 < out.brake_torque < 330  # ~NA 3-litre territory


def test_b48_smaller_than_b58(b58_plant, b48_plant):
    big = hold_point(b58_plant, 3500, boost_target=2.0e5, seconds=3.0)
    small = hold_point(b48_plant, 3500, boost_target=2.0e5, seconds=3.0)
    assert small.brake_torque < big.brake_torque
    assert small.brake_torque / big.brake_torque == pytest.approx(4 / 6, abs=0.08)


def test_warm_up_and_thermostat():
    plant = EnginePlant(get_preset("b58-inspired"))
    plant.reset(coolant_c=20.0)
    st = plant.state
    st.omega = 2500 * 2 * math.pi / 60
    first = None
    out = None
    for _ in range(12):
        out = hold_point(plant, 2500, throttle=25, lam=1.0, seconds=10.0, dt=0.002, wastegate=100)
        first = first or out.t_coolant
    assert out.t_coolant > first + 15
    assert out.t_coolant - 273.15 < 112
