"""Helpers for lab tests: open-loop operating points without the virtual ECU."""

from __future__ import annotations

import math

import pytest

from opentune.lab.physics import EnginePlant, PlantInputs, get_preset


def hold_point(plant: EnginePlant, rpm: float, throttle: float = 100.0, boost_target: float | None = None,
               lam: float = 0.88, spark_offset: float = 0.0, seconds: float = 4.0, dt: float = 0.001,
               wastegate: float | None = None, spark: float | None = None):
    """Hold engine speed (ideal dyno) with simple open-loop fuel/spark and a PI wastegate."""
    cfg = plant.config
    st = plant.state
    inp = PlantInputs(throttle_cmd=throttle, wastegate_cmd=100.0, rail_pressure_cmd=300e5,
                      fuel_enable=True, spark_advance=15.0)
    out = None
    integ = 50.0
    for _ in range(int(seconds / dt)):
        st.omega = rpm * 2 * math.pi / 60
        if out is not None:
            q = plant.injector_flow(st.rail_pressure)
            fuel_per_cyl = out.air_per_cycle / (cfg.fuel.stoich_afr * lam) / cfg.geometry.cylinders
            inp.injector_pw = fuel_per_cyl / q + cfg.fuel_system.injector_dead_time if q > 0 else 0.0
            if spark is not None:
                inp.spark_advance = spark
            else:
                inp.spark_advance = min(out.mbt_advance, out.knock_limit_advance - 1.0) + spark_offset
            if wastegate is not None:
                inp.wastegate_cmd = wastegate
            elif boost_target is not None:
                err = (out.p_boost - boost_target) / 1e5
                integ = min(100.0, max(0.0, integ + 60.0 * err * dt))
                inp.wastegate_cmd = min(100.0, max(0.0, integ + 80.0 * err))
        out = plant.step(inp, 0.0, dt)
    return out


@pytest.fixture
def b58_plant():
    plant = EnginePlant(get_preset("b58-inspired"))
    plant.warm_start(2000)
    return plant


@pytest.fixture
def b48_plant():
    plant = EnginePlant(get_preset("b48-inspired"))
    plant.warm_start(2000)
    return plant
