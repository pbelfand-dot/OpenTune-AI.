import pytest

from opentune.lab.physics import PARAM_SPECS, EngineConfig, get_preset, param_schema
from opentune.lab.physics.constants import FUELS
from opentune.lab.provenance import Provenance


@pytest.mark.parametrize("preset", ["b58-inspired", "b48-inspired"])
def test_presets_validate(preset):
    cfg = get_preset(preset)
    assert cfg.validate() == []
    assert cfg.reference_ratings["source"]


def test_b58_documented_geometry_and_provenance():
    cfg = get_preset("b58-inspired")
    assert cfg.displacement * 1e6 == pytest.approx(2998, abs=1)
    for path in ("geometry.bore", "geometry.stroke", "geometry.cylinders", "geometry.compression_ratio"):
        assert cfg.provenance_of(path) is Provenance.DOCUMENTED
        assert cfg.provenance[path]["source"]
    # Unverified details must be labelled as assumptions, never as documented facts.
    assert cfg.provenance_of("geometry.firing_order") is Provenance.ASSUMPTION
    assert cfg.provenance_of("geometry.rod_length") is Provenance.ASSUMPTION
    assert cfg.provenance_of("turbo.turbine_area") is Provenance.ASSUMPTION
    assert "Not a BMW factory simulation" in cfg.description


def test_b48_preset():
    cfg = get_preset("b48-inspired")
    assert cfg.geometry.cylinders == 4
    assert cfg.displacement * 1e6 == pytest.approx(1998, abs=1)
    assert cfg.provenance_of("geometry.compression_ratio") is Provenance.ASSUMPTION


def test_round_trip_and_copy():
    cfg = get_preset("b58-inspired")
    again = EngineConfig.from_dict(cfg.to_dict())
    assert again.to_dict() == cfg.to_dict()
    clone = cfg.copy()
    clone.geometry.bore = 0.09
    assert cfg.geometry.bore == 0.082


def test_validation_catches_problems():
    cfg = get_preset("b58-inspired")
    cfg.set("geometry.bore", 0.5)
    cfg.geometry.firing_order = [1, 2, 3]
    cfg.fuel_system.fuel = "jet-a"
    problems = cfg.validate()
    assert any("Bore" in p for p in problems)
    assert any("Firing order" in p for p in problems)
    assert any("Fuel" in p for p in problems)
    with pytest.raises(KeyError):
        cfg.set("geometry.nonexistent", 1)


def test_schema_covers_every_parameter():
    schema = param_schema()
    assert len(schema) == len(PARAM_SPECS)
    paths = {s["path"] for s in schema}
    assert "turbo.turbine_area" in paths and "combustion.octane_aki" in paths
    for spec in schema:
        assert spec["unit"] is not None and spec["description"]


def test_fuel_properties_are_sourced():
    for fuel in FUELS.values():
        assert fuel.source
        assert 25e6 < fuel.lower_heating_value < 46e6
        assert 9 < fuel.stoich_afr < 15
