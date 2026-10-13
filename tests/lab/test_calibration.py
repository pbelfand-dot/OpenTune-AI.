"""Calibration tables: interpolation, validation, editing, undo/redo, versions, import/export."""

from __future__ import annotations

import json
import sqlite3

import numpy as np
import pytest

from opentune.lab.calibration import (
    TABLE_CATEGORIES,
    Axis,
    Calibration,
    CalibrationEditor,
    CalibrationError,
    CalibrationStore,
    Scalar,
    Table,
    build_baseline,
)
from opentune.lab.calibration.io import (
    export_json,
    export_mat,
    import_json,
    table_from_csv,
    table_to_csv,
)
from opentune.lab.physics import get_preset


def _axis(name, values, lo=0.0, hi=10000.0):
    return Axis(name, name, "u", list(values), lo, hi)


def _t1():
    return Table("t1", "One-D", "", "fuel", "%", -100, 100, [_axis("x", [0, 10, 20])], [0.0, 10.0, 40.0])


def _t2():
    vals = np.array([[0.0, 1.0, 2.0], [10.0, 11.0, 12.0]])
    return Table("t2", "Two-D", "", "ignition", "°", -50, 50, [_axis("r", [1000, 2000]), _axis("c", [0, 50, 100])],
                 vals)


def _t3():
    # f(x, y, z) = x + 10 y + 100 z is reproduced exactly by multilinear interpolation
    xs, ys, zs = [0, 1, 2], [0, 1], [0, 2]
    vals = np.array([[[x + 10 * y + 100 * z for z in zs] for y in ys] for x in xs], dtype=float)
    return Table("t3", "Three-D", "", "boost", "kPa", -1, 1000, [_axis("x", xs), _axis("y", ys), _axis("z", zs)],
                 vals)


def _cal():
    cal = Calibration("unit", "test-engine")
    for t in (_t1(), _t2(), _t3()):
        cal.tables[t.id] = t
    cal.scalars["gain"] = Scalar("gain", "Gain", "", "idle", "N·m/rpm", 0.2, 0.0, 1.0)
    return cal


# ---------------------------------------------------------------------- interpolation
def test_lookup_hits_breakpoints_exactly():
    t = _t2()
    for i, r in enumerate(t.axes[0].values):
        for j, c in enumerate(t.axes[1].values):
            assert t.lookup(r, c) == pytest.approx(t.values[i, j])


def test_lookup_linear_between_breakpoints():
    assert _t1().lookup(5) == pytest.approx(5.0)
    assert _t1().lookup(15) == pytest.approx(25.0)
    assert _t2().lookup(1500, 25) == pytest.approx(5.5)


def test_lookup_clamps_instead_of_extrapolating():
    t = _t1()
    assert t.lookup(-50) == pytest.approx(0.0)
    assert t.lookup(1e6) == pytest.approx(40.0)
    assert _t2().lookup(0, -10) == pytest.approx(0.0)


def test_trilinear_is_exact_for_a_linear_function():
    t = _t3()
    for x, y, z in [(0.5, 0.5, 1.0), (1.25, 0.1, 1.9), (2, 1, 2), (0.0, 0.0, 0.0)]:
        assert t.lookup(x, y, z) == pytest.approx(x + 10 * y + 100 * z)


def test_lookup_rejects_wrong_dimension():
    with pytest.raises(CalibrationError):
        _t2().lookup(1000)


def test_validation_finds_problems():
    t = _t1()
    t.values[1] = 500.0
    assert any("outside allowed" in p for p in t.validate())
    t = _t1()
    t.axes[0].values = [0, 20, 10]
    assert any("strictly increasing" in p for p in t.validate())
    t = _t1()
    t.values = np.array([0.0, 1.0])
    assert any("shape" in p for p in t.validate())
    t = _t1()
    t.values[0] = np.nan
    assert any("non-finite" in p for p in t.validate())


# ---------------------------------------------------------------------- identity and diff
def test_content_hash_is_stable_and_content_sensitive():
    a, b = _cal(), _cal()
    assert a.content_hash() == b.content_hash()
    b.name = "renamed"  # the name is metadata, not content
    b.metadata["note"] = "x"
    assert a.content_hash() == b.content_hash()
    b.tables["t1"].values[0] = 1.0
    assert a.content_hash() != b.content_hash()


def test_dict_round_trip_and_tamper_detection():
    cal = _cal()
    data = cal.to_dict()
    back = Calibration.from_dict(json.loads(json.dumps(data)))
    assert back.content_hash() == cal.content_hash()
    assert back.lookup("t3", 1.25, 0.1, 1.9) == pytest.approx(cal.lookup("t3", 1.25, 0.1, 1.9))
    data["tables"]["t1"]["values"][0] = 3.0
    with pytest.raises(CalibrationError, match="hash"):
        Calibration.from_dict(data)


def test_diff_reports_changed_cells_and_scalars():
    a = _cal()
    b = a.copy()
    assert a.diff(b).identical
    b.tables["t2"].values[1, 2] += 2.5
    b.scalars["gain"].value = 0.4
    d = a.diff(b)
    assert not d.identical
    assert d.tables["t2"].changed_cells == 1
    assert d.tables["t2"].max_abs_delta == pytest.approx(2.5)
    assert d.scalars["gain"] == (0.2, 0.4)
    assert d.tables["t1"].changed_cells == 0


def test_copy_is_deep():
    a = _cal()
    b = a.copy("b")
    b.tables["t1"].values[0] = 5.0
    assert a.tables["t1"].values[0] == 0.0
    assert b.name == "b"


# ---------------------------------------------------------------------- editor and undo/redo
def test_set_cell_undo_redo():
    cal = _cal()
    ed = CalibrationEditor(cal)
    h0 = cal.content_hash()
    ed.set_cell("t2", (0, 1), 7.0)
    assert cal.lookup("t2", 1000, 50) == pytest.approx(7.0)  # lookup cache invalidated
    h1 = cal.content_hash()
    ed.undo()
    assert cal.content_hash() == h0
    assert cal.lookup("t2", 1000, 50) == pytest.approx(1.0)
    ed.redo()
    assert cal.content_hash() == h1
    assert not ed.can_redo


def test_out_of_range_edit_is_rejected_without_change():
    cal = _cal()
    ed = CalibrationEditor(cal)
    h0 = cal.content_hash()
    with pytest.raises(CalibrationError, match="allowed range"):
        ed.set_cell("t2", (0, 0), 999.0)
    with pytest.raises(CalibrationError):
        ed.apply_region("t2", "add", 100.0)
    with pytest.raises(CalibrationError):
        ed.set_scalar("gain", 5.0)
    assert cal.content_hash() == h0
    assert not ed.can_undo


def test_region_operations():
    cal = _cal()
    ed = CalibrationEditor(cal)
    ed.apply_region("t2", "add", 1.0, region=((0, 1), (0, 3)))
    np.testing.assert_allclose(cal.tables["t2"].values[0], [1, 2, 3])
    np.testing.assert_allclose(cal.tables["t2"].values[1], [10, 11, 12])
    ed.apply_region("t2", "percent", -50.0, region=((1, 2), (0, 3)))
    np.testing.assert_allclose(cal.tables["t2"].values[1], [5, 5.5, 6])
    ed.apply_region("t2", "set", 0.0)
    assert np.all(cal.tables["t2"].values == 0)
    for _ in range(3):
        ed.undo()
    np.testing.assert_allclose(cal.tables["t2"].values, _t2().values)
    with pytest.raises(CalibrationError):
        ed.apply_region("t2", "explode", 1.0)
    with pytest.raises(CalibrationError):
        ed.apply_region("t2", "add", 1.0, region=((0, 5), (0, 1)))


def test_smooth_and_interpolate_regions():
    cal = _cal()
    ed = CalibrationEditor(cal)
    t = cal.tables["t1"]
    ed.replace_values("t1", [0.0, 50.0, 0.0])
    ed.apply_region("t1", "smooth")
    assert t.values[1] < 50.0  # spike reduced
    ed.replace_values("t1", [0.0, 77.0, 40.0])
    ed.apply_region("t1", "interpolate")
    np.testing.assert_allclose(t.values, [0.0, 20.0, 40.0])  # linear between the region ends


def test_axis_edit_resamples_and_stays_monotonic():
    cal = _cal()
    ed = CalibrationEditor(cal)
    ed.set_axis_value("t1", 0, 1, 15.0)  # breakpoint 10 → 15, data re-interpolated
    t = cal.tables["t1"]
    assert t.axes[0].values == [0, 15.0, 20]
    assert t.values[1] == pytest.approx(25.0)  # old curve value at x = 15
    with pytest.raises(CalibrationError, match="increasing"):
        ed.set_axis_value("t1", 0, 1, 25.0)
    ed.undo()
    assert t.axes[0].values == [0, 10, 20]
    assert t.values[1] == pytest.approx(10.0)


def test_new_edit_clears_redo_and_history_is_bounded():
    cal = _cal()
    ed = CalibrationEditor(cal, max_history=3)
    for v in range(5):
        ed.set_scalar("gain", v / 10)
    assert len(ed.undo_stack) == 3
    ed.undo()
    assert ed.can_redo
    ed.set_scalar("gain", 0.9)
    assert not ed.can_redo
    with pytest.raises(CalibrationError):
        CalibrationEditor(_cal()).undo()


# ---------------------------------------------------------------------- version store
def test_store_versions_lineage_and_baseline(tmp_path):
    store = CalibrationStore(tmp_path / "cal.sqlite")
    cal = _cal()
    v1 = store.save(cal, note="base")
    store.set_baseline(v1.id)
    ed = CalibrationEditor(cal)
    ed.set_cell("t1", (0,), 1.0)
    v2 = store.save(cal, note="edit", parent_id=v1.id)
    assert (v1.version, v2.version) == (1, 2)
    assert v2.lineage == v1.lineage
    assert [v.id for v in store.history(v1.lineage)] == [v1.id, v2.id]
    assert store.baseline("test-engine").id == v1.id
    loaded = store.load(v2.id)
    assert loaded.content_hash() == v2.content_hash == cal.content_hash()
    assert store.load(v1.id).lookup("t1", 0) == pytest.approx(0.0)
    with pytest.raises(CalibrationError, match="derive"):
        store.delete(v1.id)
    store.delete(v2.id)
    store.delete(v1.id)
    assert store.list() == []


def test_store_rejects_invalid_and_detects_corruption(tmp_path):
    store = CalibrationStore(tmp_path / "cal.sqlite")
    bad = _cal()
    bad.tables["t1"].values[0] = 1000.0
    with pytest.raises(CalibrationError, match="invalid"):
        store.save(bad)
    v = store.save(_cal())
    db = sqlite3.connect(tmp_path / "cal.sqlite")
    data = json.loads(db.execute("SELECT data FROM calibration_versions WHERE id=?", (v.id,)).fetchone()[0])
    data["scalars"]["gain"]["value"] = 0.9
    db.execute("UPDATE calibration_versions SET data=? WHERE id=?", (json.dumps(data), v.id))
    db.commit()
    db.close()
    with pytest.raises(CalibrationError, match="hash"):
        store.load(v.id)


# ---------------------------------------------------------------------- import / export
def test_json_round_trip():
    cal = _cal()
    back = import_json(export_json(cal))
    assert back.content_hash() == cal.content_hash()
    with pytest.raises(CalibrationError):
        import_json("{not json")


@pytest.mark.parametrize("make", [_t1, _t2])
def test_csv_round_trip(make):
    t = make()
    np.testing.assert_allclose(table_from_csv(t, table_to_csv(t)), t.values)


def test_csv_rejects_mismatched_axes():
    t = _t2()
    with pytest.raises(CalibrationError, match="column axis"):
        table_from_csv(t, table_to_csv(t).replace(",50.0,", ",55.0,"))
    with pytest.raises(CalibrationError, match="row axis"):
        table_from_csv(t, table_to_csv(t).replace("2000.0,", "2500.0,"))
    with pytest.raises(CalibrationError):
        table_from_csv(t, "garbage\n1,2")


def test_csv_writes_3d_tables_as_blocks():
    text = table_to_csv(_t3())
    assert text.count("x=") == 3


def test_mat_export_loads_with_scipy(tmp_path):
    from scipy.io import loadmat

    path = export_mat(_cal(), tmp_path / "cal.mat")
    m = loadmat(path, simplify_cells=True)
    np.testing.assert_allclose(m["tbl_t2"]["data"], _t2().values)
    np.testing.assert_allclose(m["tbl_t2"]["bp2"], [0, 50, 100])
    assert m["scalars"]["gain"] == pytest.approx(0.2)


# ---------------------------------------------------------------------- shipped synthetic baselines
@pytest.fixture(scope="module", params=["b58-inspired", "b48-inspired"])
def baseline(request):
    return build_baseline(get_preset(request.param))


def test_baseline_is_valid_synthetic_and_categorised(baseline):
    assert baseline.validate() == []
    for t in baseline.tables.values():
        assert t.category in TABLE_CATEGORIES
        assert t.provenance == "assumption"  # generated from the model, never extracted from an ECU
    for s in baseline.scalars.values():
        assert s.category in TABLE_CATEGORIES
    text = json.dumps(baseline.metadata).lower()
    assert "synthetic" in text or "fictional" in text


def test_baseline_covers_the_calibration_categories(baseline):
    used = {t.category for t in baseline.tables.values()}
    for cat in ("fuel", "ignition", "boost", "torque", "driver", "idle", "temperature", "knock", "protection",
                "valvetrain"):
        assert cat in used, cat


def test_baseline_generation_is_deterministic():
    cfg = get_preset("b58-inspired")
    assert build_baseline(cfg).content_hash() == build_baseline(cfg).content_hash()


def test_baseline_tables_are_physically_sensible(baseline):
    # boost target never below atmospheric, lambda target never lean at full load,
    # idle target falls as the engine warms up.
    assert baseline.table("boost_max").values.min() >= 100.0
    assert baseline.lookup("lambda_target", 5500, 180) <= 1.0
    assert baseline.lookup("idle_target", -10) > baseline.lookup("idle_target", 90)
    assert baseline.lookup("ign_base", 2000, 30) > baseline.lookup("ign_base", 2000, 180)
