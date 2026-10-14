"""Optional integrations: rusEFI simulator client, engine-sim adapter, MATLAB export.

Real external tools are exercised only when configured:
``OPENTUNE_RUSEFI_SIMULATOR`` (rusEFI simulator binary) and
``OPENTUNE_ENGINESIM_BIN`` + ``OPENTUNE_ENGINESIM_ASSETS`` (headless engine-sim).
"""

from __future__ import annotations

import os
import socket
import stat
import struct
import threading
import zlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from opentune.lab.api.app import create_app
from opentune.lab.calibration import build_baseline
from opentune.lab.integrations import enginesim, integration_status, matlab, rusefi
from opentune.lab.physics import get_preset


# ---------------------------------------------------------------------- mock TunerStudio server
class MockTs:
    """Single-connection TunerStudio-protocol server that records every command byte received."""

    def __init__(self, corrupt: bool = False) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.received: list[bytes] = []
        self.corrupt = corrupt
        channels = bytearray(64)
        struct.pack_into("<H", channels, 4, 3000)  # rpm
        struct.pack_into("<h", channels, 16, 9050)  # coolant 90.50 °C
        struct.pack_into("<h", channels, 18, 3000)  # intake 30.00 °C
        self.channels = bytes(channels)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        conn, _ = self.sock.accept()
        with conn:
            while True:
                hdr = conn.recv(2)
                if len(hdr) < 2:
                    return
                (n,) = struct.unpack(">H", hdr)
                payload = conn.recv(n)
                conn.recv(4)
                cmd = payload[:1]
                self.received.append(cmd)
                if cmd == b"S":
                    body = b"\x00rusEFI master.2026.10.13.mock\x00"
                elif cmd == b"V":
                    body = b"\x00rusEFI v20261013@mock\x00"
                elif cmd == b"O":
                    off, cnt = struct.unpack("<HH", payload[1:5])
                    body = b"\x00" + self.channels[off: off + cnt]
                else:
                    body = b"\x83"
                crc = zlib.crc32(body) & 0xFFFFFFFF
                if self.corrupt:
                    crc ^= 1
                conn.sendall(struct.pack(">H", len(body)) + body + struct.pack(">I", crc))

    def close(self) -> None:
        self.sock.close()


def test_rusefi_snapshot_uses_only_read_commands():
    srv = MockTs()
    with rusefi.RusefiTsClient(port=srv.port) as c:
        snap = c.snapshot()
    srv.close()
    assert snap.values == {"rpm": 3000, "coolant_c": 90.5, "intake_air_c": 30.0}
    assert snap.signature.startswith("rusEFI master.2026.10")
    assert snap.warnings == [] and "external process" in snap.source
    assert set(srv.received) <= {b"S", b"V", b"O"}


@pytest.mark.parametrize("cmd", [b"C", b"B", b"E", b"Z", b"W", b"R", b"k", b"\x00"])
def test_rusefi_write_and_other_commands_never_sent(cmd):
    srv = MockTs()
    with rusefi.RusefiTsClient(port=srv.port) as c:
        with pytest.raises(rusefi.RusefiError, match="whitelist"):
            c.request(cmd + b"\x00\x00\x00\x00")
        c.signature()
    srv.close()
    assert srv.received == [b"S"]


def test_rusefi_rejects_remote_hosts_and_bad_crc():
    for host in ("192.168.1.10", "10.0.0.5", "example.com", "8.8.8.8"):
        with pytest.raises(rusefi.RusefiError, match="loopback"):
            rusefi.RusefiTsClient(host=host)
    srv = MockTs(corrupt=True)
    with rusefi.RusefiTsClient(port=srv.port) as c, pytest.raises(rusefi.RusefiError, match="CRC"):
        c.signature()
    srv.close()


def test_rusefi_unavailable_is_reported_not_faked():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(rusefi.RusefiUnavailable):
        rusefi.RusefiTsClient(port=port).connect()


@pytest.mark.skipif(rusefi.simulator_binary() is None, reason="OPENTUNE_RUSEFI_SIMULATOR not set")
def test_real_rusefi_simulator():
    with rusefi.RusefiSimulatorProcess(rusefi.simulator_binary(), seconds=20), rusefi.RusefiTsClient() as c:
        snap = c.snapshot()
    assert snap.signature.startswith("rusEFI")
    assert snap.values["rpm"] >= 0


# ---------------------------------------------------------------------- engine-sim
SAMPLE = """engine=Demo I4 cylinders=4 displacement_L=2.000 sim_freq=20000
t=0.00s steps=1 rpm=0 map_kPa=101.3 afr=14.70 dyno_torque_Nm=0.0 step_avg_dyno_Nm=0.0 dyno_power_kW=0.0 latency=0.010 audio_samples=0
t=0.50s steps=10000 rpm=3000 map_kPa=95.1 afr=13.20 dyno_torque_Nm=120.5 step_avg_dyno_Nm=118.0 dyno_power_kW=37.9 latency=0.018 audio_samples=24000
done: total physics steps=10000
"""


def test_enginesim_output_parsing():
    head, rows = enginesim.parse_output(SAMPLE)
    assert head == {"engine_name": "Demo I4", "cylinders": 4, "displacement_l": 2.0, "simulation_frequency_hz": 20000}
    assert rows[1]["rpm"] == 3000 and rows[1]["dyno_torque_nm"] == 120.5 and rows[1]["map_kpa"] == 95.1


def test_enginesim_not_configured_raises(monkeypatch):
    monkeypatch.delenv("OPENTUNE_ENGINESIM_BIN", raising=False)
    monkeypatch.delenv("OPENTUNE_ENGINESIM_ASSETS", raising=False)
    with pytest.raises(enginesim.EngineSimUnavailable):
        enginesim.run(seconds=1.0)
    assert enginesim.status()["available"] is False


def test_enginesim_runs_external_binary_and_labels_source(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "main.mr").write_text("// demo")
    fake = tmp_path / "driver.sh"
    fake.write_text("#!/bin/sh\ncat <<'EOF'\n" + SAMPLE + "EOF\nprintf 'RIFF' > headless_out.wav\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    res = enginesim.run(seconds=1.0, binary=fake, assets=assets)
    assert res.source == "engine-sim (external process)"
    assert res.engine_name == "Demo I4" and len(res.samples) == 2 and res.wav == b"RIFF"
    assert res.command[0] == str(fake) and len(res.binary_sha256) == 64
    with pytest.raises(ValueError):
        enginesim.run(seconds=1.0, binary=fake, assets=assets, script="../../etc/passwd")
    bad = tmp_path / "bad.sh"
    bad.write_text("#!/bin/sh\necho nothing useful\n")
    bad.chmod(bad.stat().st_mode | stat.S_IEXEC)
    with pytest.raises(enginesim.EngineSimUnavailable, match="no recognisable output"):
        enginesim.run(seconds=1.0, binary=bad, assets=assets)


@pytest.mark.skipif(not all(enginesim.configured()), reason="engine-sim not configured")
def test_real_enginesim():
    res = enginesim.run(seconds=2.0, throttle=0.5)
    assert res.samples and res.cylinders > 0 and res.wav and res.wav[:4] == b"RIFF"


# ---------------------------------------------------------------------- MATLAB and API
def test_matlab_script_and_availability():
    cal = build_baseline(get_preset("b48-inspired"))
    text = matlab.m_script(cal, "x.mat")
    assert "SYNTHETIC" in text and "griddedInterpolant" in text
    assert text.count("griddedInterpolant(") == len(cal.tables)
    if not matlab.available():
        with pytest.raises(matlab.MatlabUnavailable):
            matlab.run("disp(1)")


def test_integration_api(tmp_path):
    with TestClient(create_app(tmp_path)) as c:
        ids = {i["id"] for i in c.get("/api/integrations").json()}
        assert ids == {"rusefi", "engine-sim", "matlab"}
        assert c.get("/api/system").json()["integrations"]
        srv = MockTs()
        r = c.post("/api/integrations/rusefi/probe", json={"port": srv.port})
        srv.close()
        assert r.status_code == 200 and r.json()["values"]["rpm"] == 3000
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            free = s.getsockname()[1]
        assert c.post("/api/integrations/rusefi/probe", json={"port": free}).status_code == 503
        if not all(enginesim.configured()):
            assert c.post("/api/integrations/enginesim/run", json={}).status_code == 409
        pid = c.get("/api/projects").json()[0]["id"]
        m = c.get(f"/api/projects/{pid}/calibration/export.m")
        assert m.status_code == 200 and "griddedInterpolant" in m.text
    assert len(integration_status()) == 3
    assert Path(os.devnull).exists()
