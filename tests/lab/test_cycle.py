"""Cross-validation between the crank-angle cycle model and the mean-value plant."""

import statistics

import pytest

from opentune.lab.physics import EnginePlant, get_preset
from opentune.lab.physics.cycle import mbt_sweep, simulate_cycle

POINTS = [(1500, 0.6e5), (2000, 1.0e5), (3000, 2.0e5), (4500, 2.1e5), (6000, 2.1e5), (5000, 1.2e5)]


@pytest.fixture(scope="module")
def setup():
    cfg = get_preset("b58-inspired")
    return cfg, EnginePlant(cfg)


def _mbt(cfg, plant, rpm, p_im):
    rel = p_im / 101325 * 0.95
    burn = plant.burn_duration(rpm, rel)
    sweep = mbt_sweep(cfg, p_im, 350, burn, 1.0, range(0, 61, 1), rpm=rpm)
    return max(sweep, key=lambda r: r[1]), sweep, burn, rel


@pytest.mark.parametrize(("rpm", "p_im"), POINTS)
def test_mbt_phasing_matches_literature_and_plant(setup, rpm, p_im):
    cfg, plant = setup
    best, _, burn, rel = _mbt(cfg, plant, rpm, p_im)
    res = simulate_cycle(cfg, p_im, 350, best[0], burn, rpm=rpm)
    assert 3.0 <= res.ca50 <= 14.0  # literature: CA50 ≈ 8–10° ATDC at MBT
    assert plant.mbt_advance(rpm, rel) == pytest.approx(best[0], abs=6.0)


def test_spark_efficiency_curvature_consistent_with_plant(setup):
    cfg, plant = setup
    curvatures = []
    for rpm, p_im in POINTS:
        best, sweep, _, _ = _mbt(cfg, plant, rpm, p_im)
        curvatures += [(1 - imep / best[1]) / (adv - best[0]) ** 2
                       for adv, imep, _ in sweep if 0 < abs(adv - best[0]) <= 20]
    fitted = statistics.fmean(curvatures)
    assert fitted == pytest.approx(cfg.combustion.spark_efficiency_coeff, rel=0.25)


def test_peak_pressure_correlation(setup):
    cfg, plant = setup
    for rpm, p_im in POINTS:
        best, _, burn, _ = _mbt(cfg, plant, rpm, p_im)
        for retard in (0, 10):
            res = simulate_cycle(cfg, p_im, 350, best[0] - retard, burn, rpm=rpm)
            est = plant.peak_pressure_estimate(p_im, res.imep_gross, best[0] - retard, best[0], True)
            assert est == pytest.approx(res.peak_pressure, rel=0.25)


def test_compression_ratio_and_heat_transfer_effects(setup):
    cfg, _ = setup
    base = simulate_cycle(cfg, 1.5e5, 340, 25, 50, rpm=3000)
    high_cr = cfg.copy()
    high_cr.geometry.compression_ratio = 12.0
    hi = simulate_cycle(high_cr, 1.5e5, 340, 25, 50, rpm=3000)
    assert hi.gross_efficiency > base.gross_efficiency
    assert hi.peak_pressure > base.peak_pressure
    adiabatic = simulate_cycle(cfg, 1.5e5, 340, 25, 50, rpm=3000, heat_transfer=False)
    assert adiabatic.gross_efficiency > base.gross_efficiency


def test_rod_length_changes_trace_but_not_compression_volumes(setup):
    cfg, _ = setup
    short = cfg.copy()
    short.geometry.rod_length = 0.130
    long_ = cfg.copy()
    long_.geometry.rod_length = 0.165
    a = simulate_cycle(short, 1.5e5, 340, 25, 50, rpm=3000)
    b = simulate_cycle(long_, 1.5e5, 340, 25, 50, rpm=3000)
    assert a.volume[0] == pytest.approx(b.volume[0])  # BDC volume identical
    assert min(a.volume) == pytest.approx(min(b.volume))  # TDC volume identical
    assert a.peak_pressure != pytest.approx(b.peak_pressure, rel=1e-4)


def test_cycle_result_serialisable(setup):
    cfg, _ = setup
    data = simulate_cycle(cfg, 1.0e5, 330, 20, 50, rpm=2000).to_dict(stride=10)
    assert len(data["theta"]) == len(data["pressure"]) > 50
