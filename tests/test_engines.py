import pytest

from opentune.engines import ENGINES, get_profile


def test_registry_contains_b58_and_b48():
    assert set(ENGINES) == {"B58", "B48"}


def test_get_profile_is_case_insensitive():
    assert get_profile(" b58 ").code == "B58"


def test_unknown_engine_raises():
    with pytest.raises(KeyError, match="Supported engines"):
        get_profile("S58")


def test_displacement_matches_bore_and_stroke(profile):
    # Published displacement must agree with published bore x stroke within 0.5 %.
    assert profile.computed_displacement_cc() == pytest.approx(profile.displacement_cc, rel=0.005)


def test_published_reference_figures():
    b58, b48 = get_profile("B58"), get_profile("B48")
    assert (b58.cylinders, b58.displacement_cc, b58.bore_mm, b58.stroke_mm) == (6, 2998, 82.0, 94.6)
    assert (b48.cylinders, b48.displacement_cc, b48.bore_mm, b48.stroke_mm) == (4, 1998, 82.0, 94.6)
    m340i = next(v for v in b58.variants if "M340i" in v.applications)
    assert (m340i.power_kw, m340i.torque_nm) == (285, 500)
    g20_330i = next(v for v in b48.variants if "330i" in v.applications)
    assert (g20_330i.power_kw, g20_330i.torque_nm) == (190, 400)
    for p in (b58, b48):
        assert p.sources, "every profile must cite sources"
        assert all(v.source for v in p.variants)


def test_thresholds_present(profile):
    for key in ("coolant_warn", "coolant_critical", "oil_warn", "oil_critical", "iat_warn", "battery_low"):
        assert key in profile.thresholds
    assert profile.thresholds["coolant_warn"] < profile.thresholds["coolant_critical"]


def test_torque_curve_shape(profile):
    s = profile.sim
    assert profile.max_torque_at(0) == 0
    plateau = profile.max_torque_at(sum(s.torque_plateau_rpm) / 2)
    assert plateau == pytest.approx(s.peak_torque_nm)
    assert profile.max_torque_at(s.idle_rpm) < plateau
    # Above the plateau, torque falls so power never exceeds the modelled peak.
    import math

    for rpm in range(int(s.torque_plateau_rpm[1]), int(s.rev_limit_rpm), 250):
        power_kw = profile.max_torque_at(rpm) * rpm * 2 * math.pi / 60_000
        assert power_kw <= s.peak_power_kw + 1e-6
