import re

import pytest

from opentune.cli import main


def run(capsys, *args):
    code = main(list(args))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_info(capsys):
    code, out, _ = run(capsys, "info")
    assert code == 0
    assert "DISABLED" in out and "B58" in out and "B48" in out


def test_help_without_command(capsys):
    code, out, _ = run(capsys)
    assert code == 0 and "simulate" in out


def test_simulate_with_log_and_faults(capsys, tmp_path):
    log = tmp_path / "run.otlog.csv"
    code, out, _ = run(capsys, "simulate", "--engine", "b48", "--duration", "40", "--print-rate", "0",
                       "--fault", "boost_leak", "--log", str(log))
    assert code == 0
    assert "Wrote 800 samples" in out
    assert "P0299" in out
    code, out, _ = run(capsys, "analyze", str(log), "--fail-on-critical")
    assert code == 1 and "Underboost" in out


def test_simulate_via_obd(capsys):
    code, out, _ = run(capsys, "simulate", "--duration", "2", "--via-obd", "--units", "imperial")
    assert code == 0
    assert "emulated adapter" in out and "boost(psi)" in out


def test_dtc_read_and_clear(capsys):
    code, out, _ = run(capsys, "dtc", "--fault", "overheat", "--scenario", "cruise", "--run-seconds", "90",
                       "--clear")
    assert code == 0
    assert "P0217" in out and "MIL (check engine light): ON" in out
    assert "DTCs cleared" in out
    assert out.rstrip().endswith("Engine Coolant Over Temperature Condition")  # permanent remains


def test_calibration_commands(capsys, tmp_path):
    data = ["--data-dir", str(tmp_path / "data")]
    assert run(capsys, *data, "cal", "list")[1].startswith("Library is empty")
    code, out, _ = run(capsys, *data, "cal", "new-stock", "--engine", "B58")
    cal_id = re.search(r"Created (\w+)", out).group(1)
    binary = tmp_path / "read.bin"
    binary.write_bytes(b"\xAA" * 64)
    code, out, _ = run(capsys, *data, "cal", "import", str(binary), "--name", "bench read")
    bin_id = re.search(r"Imported (\w+)", out).group(1)
    code, out, _ = run(capsys, *data, "cal", "show", cal_id)
    assert "integrity        OK" in out and "boost_target" in out
    code, out, _ = run(capsys, *data, "cal", "diff", cal_id, bin_id)
    assert "differ" in out
    code, out, _ = run(capsys, *data, "cal", "export", cal_id, str(tmp_path))
    assert (tmp_path / "B58 simulator stock.otsim").exists()
    code, out, _ = run(capsys, *data, "cal", "remove", bin_id)
    assert code == 0
    code, _, err = run(capsys, *data, "cal", "show", "doesnotexist")
    assert code == 1 and "error" in err


def test_flash_sim(capsys, tmp_path):
    data = ["--data-dir", str(tmp_path / "data")]
    code, out, _ = run(capsys, *data, "flash-sim", "--engine", "B48")
    assert code == 0 and "SUCCESS" in out and "Backup stored" in out
    code, out, _ = run(capsys, *data, "flash-sim", "--power-loss", "30")
    assert code == 1 and "recovery" in out.lower()
    code, out, _ = run(capsys, *data, "flash-sim", "--power-loss", "30", "--recover")
    assert code == 0 and "Recovery: SUCCESS" in out
    code, out, _ = run(capsys, *data, "flash-sim", "--voltage", "11.5")
    assert code == 1 and "below" in out


def test_selftest(capsys):
    code, out, _ = run(capsys, "selftest")
    assert code == 0 and "SELFTEST PASSED" in out
    assert "FAIL]" not in out


def test_scan_requires_port(capsys):
    code, _, err = run(capsys, "scan")
    assert code == 2 and "--port" in err


@pytest.mark.parametrize("argv", [["simulate", "--engine", "N55"], ["simulate", "--fault", "nope"]])
def test_argument_validation(argv):
    with pytest.raises(SystemExit):
        main(argv)
