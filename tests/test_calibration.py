import copy

import pytest

from opentune.calibration import (
    CalibrationFormatError,
    CalibrationLibrary,
    CalibrationLibraryError,
    CalTable,
    SimCalibration,
    default_calibration,
    diff_bytes,
)


def test_simcal_round_trip(profile):
    cal = default_calibration(profile)
    data = cal.to_bytes()
    again = SimCalibration.from_bytes(data)
    assert again.engine == profile.code
    assert again.version == f"SIM-{profile.code}-STOCK"
    assert again.to_bytes() == data
    assert set(again.tables) == {"boost_target", "ignition_base", "lambda_target", "rev_limit"}
    assert again.rev_limit == profile.sim.rev_limit_rpm
    assert again.validate() == []


def test_default_calibration_is_sane(profile):
    cal = default_calibration(profile)
    boost = cal.table("boost_target")
    assert boost.lookup(500, 0) == pytest.approx(-0.65, abs=0.01)  # closed pedal: vacuum
    assert boost.lookup(3000, 100) == pytest.approx(profile.sim.peak_boost_bar, abs=0.01)
    lam = cal.table("lambda_target")
    assert lam.lookup(2000, 40) == pytest.approx(1.0)
    assert lam.lookup(6000, 100) < 0.9
    ign = cal.table("ignition_base")
    assert ign.lookup(3000, 20) > ign.lookup(3000, 100)  # less advance at high load


def test_crc_detects_corruption(b58):
    data = bytearray(default_calibration(b58).to_bytes())
    data[40] ^= 0xFF
    with pytest.raises(CalibrationFormatError, match="checksum"):
        SimCalibration.from_bytes(bytes(data))


@pytest.mark.parametrize("data", [b"", b"NOTACAL" * 10, b"OTSIMCAL"])
def test_rejects_garbage(data):
    with pytest.raises(CalibrationFormatError):
        SimCalibration.from_bytes(data)


def test_truncated_body_detected(b58):
    import struct
    import zlib

    body = default_calibration(b58).to_bytes()[:-4][:-10]
    data = body + struct.pack("<I", zlib.crc32(body) & 0xFFFFFFFF)
    with pytest.raises(CalibrationFormatError):
        SimCalibration.from_bytes(data)


def test_bilinear_lookup_and_clamping():
    t = CalTable("t", "x", "r", "c", [0.0, 10.0], [0.0, 10.0], [[0.0, 10.0], [10.0, 20.0]])
    assert t.lookup(5, 5) == pytest.approx(10.0)
    assert t.lookup(-100, -100) == 0.0
    assert t.lookup(100, 100) == 20.0
    assert t.lookup(0, 2.5) == pytest.approx(2.5)


def test_table_validation():
    with pytest.raises(CalibrationFormatError):
        CalTable("t", "x", "r", "c", [0.0, 0.0], [0.0], [[1.0], [2.0]])  # axis not increasing
    with pytest.raises(CalibrationFormatError):
        CalTable("t", "x", "r", "c", [0.0, 1.0], [0.0], [[1.0]])  # wrong row count


def test_validate_flags_unsafe_values(b58):
    cal = copy.deepcopy(default_calibration(b58))
    cal.table("lambda_target").values[0][0] = 0.5
    cal.table("rev_limit").values[0][0] = 12000
    problems = cal.validate()
    assert any("lambda" in p for p in problems)
    assert any("rev_limit" in p for p in problems)


# ---------------------------------------------------------------------------- library
@pytest.fixture
def lib(tmp_path):
    return CalibrationLibrary(tmp_path / "cals")


def test_library_add_list_get(lib, b58, b48):
    r1 = lib.add_simcal(default_calibration(b58), "stock 58")
    r2 = lib.add_simcal(default_calibration(b48), "stock 48")
    assert {r.id for r in lib.list()} == {r1.id, r2.id}
    assert [r.id for r in lib.list("B48")] == [r2.id]
    assert lib.get(r1.id[:6]).id == r1.id
    assert r1.format == "otsim" and r1.software_version == "SIM-B58-STOCK"
    assert lib.verify(r1.id)


def test_library_persists(tmp_path, b58):
    root = tmp_path / "cals"
    rec = CalibrationLibrary(root).add_simcal(default_calibration(b58), "stock")
    again = CalibrationLibrary(root)
    assert again.get(rec.id).sha256 == rec.sha256
    assert again.load_simcal(rec.id).engine == "B58"


def test_import_binary_and_dedupe(lib, tmp_path):
    f = tmp_path / "read.bin"
    f.write_bytes(bytes(range(256)) * 16)
    a = lib.import_file(f, engine="B58", notes="bench read")
    b = lib.import_file(f, name="copy", engine="B58")
    assert a.sha256 == b.sha256 and a.id != b.id
    assert a.format == "binary"
    assert len(list(lib.root.glob("*.bin"))) == 1  # stored once
    lib.remove(a.id)
    assert lib.read_bytes(b.id) == f.read_bytes()  # shared blob kept
    lib.remove(b.id)
    assert not list(lib.root.glob("*.bin"))


def test_tamper_detection(lib, tmp_path):
    f = tmp_path / "x.bin"
    f.write_bytes(b"\x01" * 100)
    rec = lib.import_file(f)
    (lib.root / f"{rec.sha256}.bin").write_bytes(b"\x02" * 100)
    assert not lib.verify(rec.id)
    with pytest.raises(CalibrationLibraryError, match="Integrity"):
        lib.read_bytes(rec.id)


def test_rejections(lib, tmp_path, b58):
    empty = tmp_path / "empty.bin"
    empty.write_bytes(b"")
    with pytest.raises(CalibrationLibraryError):
        lib.import_file(empty)
    with pytest.raises(CalibrationLibraryError, match="not a file"):
        lib.import_file(tmp_path / "missing.bin")
    with pytest.raises(CalibrationLibraryError, match="B58, not B48"):
        lib.add_bytes(default_calibration(b58).to_bytes(), "x", "B48")
    with pytest.raises(CalibrationLibraryError):
        lib.get("nope")
    bad = copy.deepcopy(default_calibration(b58))
    bad.table("boost_target").values[0][0] = 9.0
    with pytest.raises(CalibrationLibraryError, match="validation"):
        lib.add_simcal(bad, "unsafe")


def test_update_and_export(lib, tmp_path, b58):
    rec = lib.add_simcal(default_calibration(b58), "stock")
    lib.update(rec.id, name="renamed", notes="n", tags=["a"])
    assert lib.get(rec.id).name == "renamed"
    with pytest.raises(CalibrationLibraryError):
        lib.update(rec.id, sha256="0" * 64)
    out = lib.export(rec.id, tmp_path)
    assert out.name == "renamed.otsim"
    assert out.read_bytes() == lib.read_bytes(rec.id)


def test_compare_and_lineage(lib, b58):
    stock = default_calibration(b58)
    tuned = copy.deepcopy(stock)
    tuned.table("boost_target").values[5][5] += 0.1
    tuned.version = "SIM-B58-MOD001"
    a = lib.add_simcal(stock, "stock")
    b = lib.add_simcal(tuned, "tuned", origin="simulated-modified", parent_id=a.id)
    assert lib.get(b.id).parent_id == a.id
    diff = lib.compare(a.id, b.id)
    assert not diff.identical
    assert diff.changed_bytes >= 2  # at least the table cell and the CRC
    assert lib.compare(a.id, a.id).identical


def test_diff_bytes_regions():
    a = bytes(100)
    b = bytearray(a)
    b[10] = 1
    b[12] = 1  # merged with 10 (gap <= 8)
    b[60] = 1
    diff = diff_bytes(a, bytes(b) + b"\x00\x00")
    assert diff.changed_bytes == 5
    assert [(r.start, r.end) for r in diff.regions] == [(10, 13), (60, 61), (100, 102)]
