"""Engine presets inspired by the BMW B58 and B48.

These are *synthetic* models built around a handful of publicly documented
specifications. Documented values carry a source in ``config.provenance``; every
other parameter (VE curve, turbo maps, knock coefficients, friction, thermal
masses, rod length, firing order, ...) is an engineering assumption chosen so the
model's full-load output lands near the published ratings. They are NOT BMW
factory data and the model is NOT an exact B58/B48 simulation.
"""

from __future__ import annotations

from opentune.lab.physics.config import EngineConfig
from opentune.lab.provenance import Provenance

_BMW_PRESS_WARDS = "BMW Group PressClub USA, T0304716EN_US (Wards 10 Best Engines 2020)"
_WIKI_B58 = "Wikipedia 'BMW B58' (cross-checked with BMW/Toyota press material)"
_WIKI_B48 = "Wikipedia 'BMW B48'"


def _doc(source: str, note: str = "") -> dict[str, str]:
    return {"provenance": Provenance.DOCUMENTED.value, "source": source, "note": note}


def b58_inspired() -> EngineConfig:
    cfg = EngineConfig(
        id="b58-inspired",
        name="B58-inspired 3.0 L turbo I6 (synthetic model)",
        description=(
            "Synthetic mean-value model of a 3.0-litre-class turbocharged direct-injection inline six with "
            "double cam phasing, built around published B58 geometry and the published G20 M340i (US) rating. "
            "Not a BMW factory simulation."
        ),
    )
    g = cfg.geometry
    g.cylinders, g.layout = 6, "inline"
    g.bore, g.stroke = 0.082, 0.0946
    g.compression_ratio = 10.2
    g.firing_order = [1, 5, 3, 6, 2, 4]
    g.rod_length = 0.1445
    cfg.fuel_system.injection = "direct"
    cfg.fuel_system.rail_pressure_max = 350e5
    cfg.intake.intercooler_type = "air_to_water"
    cfg.turbo.enabled = True
    cfg.turbo.twin_scroll = True
    cfg.provenance = {
        "geometry.cylinders": _doc(_WIKI_B58),
        "geometry.layout": _doc(_WIKI_B58),
        "geometry.bore": _doc(_WIKI_B58, "82.0 mm"),
        "geometry.stroke": _doc(_WIKI_B58, "94.6 mm"),
        "geometry.compression_ratio": _doc(
            "Toyota Pressroom MY2021 GR Supra; Wikipedia 'BMW B58'", "10.2:1 for the high-output TU variant"),
        "fuel_system.injection": _doc(_BMW_PRESS_WARDS),
        "fuel_system.rail_pressure_max": _doc(
            "bmwblog 2018-10-31 'B58 TU1 technical updates'", "350 bar on the TU engine (200 bar Gen1)"),
        "turbo.enabled": _doc(_BMW_PRESS_WARDS, "single twin-scroll turbocharger"),
        "turbo.twin_scroll": _doc(_BMW_PRESS_WARDS),
        "intake.intercooler_type": _doc(_WIKI_B58, "air-to-liquid cooler integrated in the intake plenum"),
        "geometry.firing_order": {
            "provenance": Provenance.ASSUMPTION.value, "source": "",
            "note": "1-5-3-6-2-4 is the common BMW inline-six order; not verified for the B58 specifically",
        },
        "geometry.rod_length": {
            "provenance": Provenance.ASSUMPTION.value, "source": "",
            "note": "No OEM figure found; 144.5 mm chosen within the range of published aftermarket rods",
        },
    }
    cfg.reference_ratings = {
        "label": "G20 M340i (US) published rating",
        "power_kw": 285.0,
        "torque_nm": 500.0,
        "source": "press.bmwgroup.com/usa T0286917EN_US",
        "note": "Used only as a sanity target for the synthetic calibration; not a model validation.",
    }
    cfg.limits.max_rpm = 7000.0
    return cfg


def b48_inspired() -> EngineConfig:
    cfg = b58_inspired()
    cfg.id = "b48-inspired"
    cfg.name = "B48-inspired 2.0 L turbo I4 (synthetic model)"
    cfg.description = (
        "Synthetic mean-value model of a 2.0-litre-class turbocharged direct-injection inline four built around "
        "published B48 geometry and the published G20 330i (US) rating. Not a BMW factory simulation."
    )
    g = cfg.geometry
    g.cylinders = 4
    g.firing_order = [1, 3, 4, 2]
    g.compression_ratio = 10.2
    tc = cfg.turbo
    tc.compressor_diameter = 0.044
    tc.turbine_diameter = 0.040
    tc.turbine_area = 0.32e-3
    tc.wastegate_area_max = 1.2e-3
    tc.inertia = 1.8e-5
    cfg.intake.throttle_diameter = 0.068
    cfg.intake.intake_manifold_volume = 0.0028
    cfg.intake.boost_volume = 0.0045
    cfg.valvetrain.ve_peak_rpm = 3800.0
    cfg.mechanical.crank_inertia = 0.16
    cfg.fuel_system.injector_flow_ref = 0.0105
    cfg.limits.max_rpm = 6500.0
    cfg.provenance = {
        "geometry.cylinders": _doc(_WIKI_B48),
        "geometry.layout": _doc(_WIKI_B48),
        "geometry.bore": _doc(_WIKI_B48, "82.0 mm"),
        "geometry.stroke": _doc(_WIKI_B48, "94.6 mm"),
        "fuel_system.injection": _doc(_WIKI_B48),
        "turbo.enabled": _doc(_WIKI_B48, "single twin-scroll turbocharger"),
        "turbo.twin_scroll": _doc(_WIKI_B48),
        "geometry.compression_ratio": {
            "provenance": Provenance.ASSUMPTION.value, "source": "",
            "note": "Published B48 compression ratios vary by variant (9.5–11.0:1); 10.2 chosen",
        },
        "geometry.firing_order": {
            "provenance": Provenance.ASSUMPTION.value, "source": "",
            "note": "1-3-4-2 is the common inline-four order; not verified for the B48 specifically",
        },
    }
    cfg.reference_ratings = {
        "label": "G20 330i (US) published rating",
        "power_kw": 190.0,
        "torque_nm": 400.0,
        "source": "press.bmwgroup.com/usa T0285572EN_US",
        "note": "Used only as a sanity target for the synthetic calibration; not a model validation.",
    }
    return cfg


PRESETS = {"b58-inspired": b58_inspired, "b48-inspired": b48_inspired}


def get_preset(preset_id: str) -> EngineConfig:
    try:
        return PRESETS[preset_id]()
    except KeyError as exc:
        raise KeyError(f"Unknown engine preset {preset_id!r}. Options: {', '.join(PRESETS)}") from exc
