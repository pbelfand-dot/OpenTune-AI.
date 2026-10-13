from datetime import datetime

import pytest

from opentune.core.models import Sample
from opentune.datalog import (
    DatalogError,
    DataLogger,
    list_logs,
    make_log_name,
    read_log,
    summarize_log,
)


def test_round_trip(tmp_path):
    path = tmp_path / "a.otlog.csv"
    with DataLogger(path, ["rpm", "boost", "coolant_temp", "not_a_channel"], "B58", "simulator",
                    notes="pull\n93 octane") as log:
        for i in range(50):
            log.write(Sample(10.0 + i * 0.1, {"rpm": 1000 + i * 10, "boost": 0.5, "coolant_temp": 90.0}))
        assert log.rows == 50
    assert log.closed
    data = read_log(path)
    assert data.channels == ["rpm", "boost", "coolant_temp"]
    assert data.engine == "B58"
    assert data.metadata["source"] == "simulator"
    assert data.metadata["notes"] == "pull 93 octane"
    assert data.metadata["format"] == "opentune-datalog-v1"
    assert len(data.samples) == 50
    assert data.samples[0].timestamp == 0.0  # timestamps are relative to the first sample
    assert data.duration_s == pytest.approx(4.9)
    assert data.samples[-1].values["rpm"] == pytest.approx(1490)
    assert data.series("boost")[0] == (0.0, 0.5)


def test_missing_values_are_skipped(tmp_path):
    path = tmp_path / "b.otlog.csv"
    with DataLogger(path, ["rpm", "boost"], "B48", "obd") as log:
        log.write(Sample(0, {"rpm": 900}))
        log.write(Sample(1, {"rpm": 950, "boost": -0.5}))
    data = read_log(path)
    assert "boost" not in data.samples[0].values
    assert data.samples[1].values["boost"] == -0.5


def test_write_after_close_fails(tmp_path):
    log = DataLogger(tmp_path / "c.otlog.csv", ["rpm"], "B58", "sim")
    log.close()
    with pytest.raises(ValueError):
        log.write(Sample(0, {"rpm": 1}))


def test_invalid_files(tmp_path):
    empty = tmp_path / "empty.otlog.csv"
    empty.write_text("# only metadata\n")
    with pytest.raises(DatalogError):
        read_log(empty)
    wrong = tmp_path / "wrong.csv"
    wrong.write_text("a,b\n1,2\n")
    with pytest.raises(DatalogError, match="time_s"):
        read_log(wrong)
    with pytest.raises(DatalogError):
        read_log(tmp_path / "missing.csv")


def test_names_and_listing(tmp_path):
    name = make_log_name("B58", "ELM327 (/dev/cu.usb)", datetime(2026, 1, 2, 3, 4, 5))
    assert name == "20260102-030405_B58_elm327---dev-cu-usb.otlog.csv"
    for n in ("1.otlog.csv", "2.otlog.csv"):
        DataLogger(tmp_path / n, ["rpm"], "B58", "sim").close()
    (tmp_path / "other.txt").write_text("x")
    assert {p.name for p in list_logs(tmp_path)} == {"1.otlog.csv", "2.otlog.csv"}
    assert list_logs(tmp_path / "nope") == []


def test_summary_matches_full_read(tmp_path):
    path = tmp_path / "s.otlog.csv"
    with DataLogger(path, ["rpm"], "B48", "simulator") as log:
        for i in range(30):
            log.write(Sample(i * 0.5, {"rpm": 800 + i}))
    info = summarize_log(path)
    full = read_log(path)
    assert info.sample_count == len(full.samples) == 30
    assert info.duration_s == pytest.approx(full.duration_s)
    assert info.engine == "B48"
