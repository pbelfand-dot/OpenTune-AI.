"""Simulated ECU communication and flashing: image format, protocol, faults, recovery, API."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from opentune.ecu.safety import EcuWriteBlockedError, guard_write
from opentune.lab.api.app import create_app
from opentune.lab.calibration import CalibrationEditor, build_baseline
from opentune.lab.flash import (
    BANK_SIZE,
    DeviceFaults,
    ImageError,
    LinkFaults,
    LoopbackLink,
    Programmer,
    VirtualEcuDevice,
    build_image,
    decode_image,
    verify_image,
)
from opentune.lab.flash import link as L
from opentune.lab.physics import get_preset


@pytest.fixture(scope="module")
def cal():
    return build_baseline(get_preset("b58-inspired"))


@pytest.fixture
def tuned(cal):
    c = cal.copy("tuned")
    CalibrationEditor(c).apply_region("ign_base", "add", 1.0)
    return c


@pytest.fixture
def dev(cal):
    d = VirtualEcuDevice("b58-inspired")
    d.factory_program(build_image(cal))
    return d


def flash(dev, image, link_faults=None, **kw):
    return Programmer(LoopbackLink(dev, link_faults), **kw).flash(image)


# ---------------------------------------------------------------------- image format
def test_image_round_trip_and_integrity(cal):
    img = build_image(cal, sequence=7)
    h = verify_image(img)
    assert (h.engine_id, h.firmware_family, h.sequence) == ("b58-inspired", "OTVECU-1", 7)
    assert decode_image(img).content_hash() == cal.content_hash()
    assert len(img) < BANK_SIZE


@pytest.mark.parametrize("offset", [0, 10, 130, -2])
def test_image_corruption_detected(cal, offset):
    img = bytearray(build_image(cal))
    img[offset] ^= 0x01
    with pytest.raises(ImageError):
        verify_image(bytes(img))


def test_frame_codec_detects_corruption():
    frame = L.encode_frame(5, L.CMD_HELLO, L.encode_payload({"a": 1}, b"xyz"))
    seq, cmd, payload = L.decode_frame(frame)
    assert (seq, cmd) == (5, L.CMD_HELLO) and L.decode_payload(payload) == ({"a": 1}, b"xyz")
    bad = bytearray(frame)
    bad[7] ^= 0x10
    with pytest.raises(L.FrameError):
        L.decode_frame(bytes(bad))


# ---------------------------------------------------------------------- normal flash
def test_successful_flash_alternates_banks(dev, tuned):
    assert dev.running_bank == "A"
    r = flash(dev, build_image(tuned))
    assert r.success, r.error
    assert [s.step for s in r.steps][-4:] == ["verify", "activate", "reset", "post_check"]
    assert dev.running_bank == "B" and r.previous_bank == "A"
    assert decode_image(r.backup).content_hash() != tuned.content_hash()  # backup = old calibration
    assert Programmer(LoopbackLink(dev)).read_calibration().content_hash() == tuned.content_hash()
    again = tuned.copy("again")
    CalibrationEditor(again).set_scalar("rev_limit_rpm", 6600.0)
    assert flash(dev, build_image(again)).success and dev.running_bank == "A"
    assert r.to_dict()["label"].startswith("SIMULATED")


def test_rollback_switches_to_previous_bank(dev, tuned, cal):
    flash(dev, build_image(tuned))
    r = Programmer(LoopbackLink(dev)).rollback()
    assert r.success and dev.running_bank == "A"
    assert Programmer(LoopbackLink(dev)).read_calibration().content_hash() == cal.content_hash()


# ---------------------------------------------------------------------- refused before writing
def _unchanged(dev, before):
    return dev.running_bank == before[0] and bytes(dev.banks["B"]) == before[1]


@pytest.mark.parametrize("mutate,check", [
    (lambda c: setattr(c, "engine_id", "b48-inspired"), "engine_match"),
    (lambda c: setattr(c, "firmware_family", "OTVECU-9"), "firmware_family"),
    (lambda c: setattr(c, "schema_version", 99), "schema_version"),
])
def test_incompatible_images_rejected_without_writing(dev, cal, mutate, check):
    bad = cal.copy()
    mutate(bad)
    before = (dev.running_bank, bytes(dev.banks["B"]))
    r = flash(dev, build_image(bad))
    assert not r.success
    assert any(s.step == check and s.status == "failed" for s in r.steps)
    assert _unchanged(dev, before)


def test_corrupt_image_rejected(dev, tuned):
    img = bytearray(build_image(tuned))
    img[200] ^= 0xFF
    before = (dev.running_bank, bytes(dev.banks["B"]))
    r = flash(dev, bytes(img))
    assert not r.success and any(s.step == "image_integrity" and s.status == "failed" for s in r.steps)
    assert _unchanged(dev, before)


def test_programming_refused_while_engine_running(dev, tuned):
    dev.engine_running = True
    r = flash(dev, build_image(tuned))
    assert not r.success and "Engine running" in r.error
    assert dev.bank_status("B")["state"] == "erased"


# ---------------------------------------------------------------------- interrupted transfers and recovery
@pytest.mark.parametrize("stage,block", [("erase", 0), ("write", 0), ("write", 7), ("activate", 0)])
def test_power_loss_never_bricks_the_ecu(dev, tuned, cal, stage, block):
    dev.faults = DeviceFaults(power_loss_stage=stage, power_loss_block=block)
    r = flash(dev, build_image(tuned))
    assert not r.success and not dev.powered
    assert any("power" in a for a in r.advice)
    boot = dev.boot()  # power cycle
    assert boot["mode"] == "application" and boot["bank"] == "A"
    assert dev.running_sha == verify_image(build_image(cal)).payload_sha256
    assert flash(dev, build_image(tuned)).success  # a retry after the power cycle works


def test_link_drop_mid_transfer_keeps_old_calibration(dev, tuned):
    r = flash(dev, build_image(tuned), LinkFaults(disconnect_after_frames=12))
    assert not r.success and "LinkDown" in r.error
    assert dev.running_bank == "A" and dev.bank_status("B")["state"] in ("invalid", "erased")
    assert dev.boot()["bank"] == "A"


@pytest.mark.parametrize("seed", range(6))
def test_noisy_link_recovers_with_retries(cal, tuned, seed):
    d = VirtualEcuDevice("b58-inspired")
    d.factory_program(build_image(cal))
    r = flash(d, build_image(tuned), LinkFaults(corrupt_rate=0.15, drop_rate=0.05), retries=6)
    assert r.success, r.error
    assert r.retries > 0 or r.link["corrupted"] == 0
    assert Programmer(LoopbackLink(d)).read_calibration().content_hash() == tuned.content_hash()


def test_duplicate_request_is_not_executed_twice(dev, tuned):
    prog = Programmer(LoopbackLink(dev))
    token = prog.request(L.CMD_START_SESSION, {"mode": "programming"})[0]["token"]
    prog.request(L.CMD_ERASE, {"bank": "B", "token": token})
    frame = L.encode_frame(200, L.CMD_WRITE, L.encode_payload({"bank": "B", "offset": 0, "token": token}, b"\0" * 64))
    first = dev.handle(frame)
    assert dev.handle(frame) == first  # replayed, not "region not erased"
    assert L.decode_payload(L.decode_frame(first)[2])[0]["status"] == L.ST_OK


def test_flash_cell_bitflip_caught_by_verify(dev, tuned):
    dev.faults = DeviceFaults(bitflip_block=2)
    r = flash(dev, build_image(tuned))
    assert not r.success and "VERIFY" in r.error
    assert dev.running_bank == "A" and dev.bcb_state()["active_bank"] == "A"


def test_both_banks_bad_enters_bootloader(dev):
    dev.banks["A"][300] ^= 0xFF
    boot = dev.boot()
    assert boot["mode"] == "bootloader" and dev.running_image() is None
    with pytest.raises(Exception, match="bootloader"):
        Programmer(LoopbackLink(dev)).read_calibration()


def test_device_side_rejects_activation_of_incompatible_bank(dev, cal):
    """Defence in depth: even if the host skipped its checks, the ECU will not activate a foreign image."""
    bad = cal.copy()
    bad.engine_id = "b48-inspired"
    img = build_image(bad)
    prog = Programmer(LoopbackLink(dev))
    token = prog.request(L.CMD_START_SESSION, {"mode": "programming"})[0]["token"]
    prog.request(L.CMD_ERASE, {"bank": "B", "token": token})
    for i in range(0, len(img), 512):
        prog.request(L.CMD_WRITE, {"bank": "B", "offset": i, "token": token}, img[i: i + 512])
    with pytest.raises(Exception, match="incompatible"):
        prog.request(L.CMD_ACTIVATE, {"bank": "B", "token": token})
    assert dev.boot()["bank"] == "A"


def test_protocol_access_rules(dev):
    prog = Programmer(LoopbackLink(dev))
    with pytest.raises(Exception, match="programming session required"):
        prog.request(L.CMD_ERASE, {"bank": "B"})
    prog.request(L.CMD_START_SESSION, {"mode": "programming"})
    with pytest.raises(Exception, match="session handle"):
        prog.request(L.CMD_ERASE, {"bank": "B", "token": "guess"})
    with pytest.raises(Exception, match="in use"):
        prog.request(L.CMD_ERASE, {"bank": "A", "token": dev.token})
    with pytest.raises(Exception, match="out of range"):
        prog.request(L.CMD_READ, {"bank": "A", "offset": BANK_SIZE - 10, "length": 100})


# ---------------------------------------------------------------------- safety gate
def test_programmer_refuses_anything_but_the_virtual_loopback(dev):
    class FakeLink(LoopbackLink):
        pass

    with pytest.raises(EcuWriteBlockedError):
        Programmer(FakeLink(dev))

    class Imposter:
        is_simulated = True

    with pytest.raises(EcuWriteBlockedError):
        Programmer(LoopbackLink(Imposter()))
    with pytest.raises(EcuWriteBlockedError):
        guard_write(Imposter())
    guard_write(dev)  # the registered virtual device passes


def test_device_persistence(dev, tuned):
    flash(dev, build_image(tuned))
    clone = VirtualEcuDevice.from_dict(dev.to_dict())
    assert clone.running_bank == dev.running_bank and clone.running_sha == dev.running_sha


# ---------------------------------------------------------------------- API workflow
@pytest.fixture(scope="module")
def client(tmp_path_factory):
    with TestClient(create_app(tmp_path_factory.mktemp("flash"))) as c:
        yield c


def _wait(client, jid):
    end = time.time() + 60
    while time.time() < end:
        j = client.get(f"/api/jobs/{jid}").json()
        if j["status"] not in ("queued", "running"):
            return j
        time.sleep(0.03)
    raise AssertionError("timeout")


def test_api_edit_save_flash_live(client):
    pid = client.post("/api/projects", json={"name": "flash", "preset_id": "b48-inspired"}).json()["id"]
    ecu = client.get(f"/api/projects/{pid}/ecu").json()
    assert ecu["status"]["running_bank"] == "A" and ecu["flashed_version"]["version"] == 1
    client.post(f"/api/projects/{pid}/calibration/scalars/rev_limit_rpm", json={"value": 6000})
    v2 = client.post(f"/api/projects/{pid}/calibration/save", json={"note": "rev limit"}).json()["version"]
    # engine running → refused
    client.post("/api/live/start", json={"project_id": pid})
    client.post("/api/live/control", json={"action": "crank"})
    end = time.time() + 10
    while time.time() < end and client.get("/api/live/status").json()["state"] != "RUNNING":
        time.sleep(0.05)
    job = _wait(client, client.post(f"/api/projects/{pid}/ecu/flash", json={}).json()["id"])
    assert job["status"] == "done" and not job["result"]["success"] and "Engine running" in job["result"]["error"]
    client.post("/api/live/control", json={"action": "stop_engine"})
    end = time.time() + 10
    while time.time() < end and client.get("/api/live/status").json()["state"] not in ("OFF", "READY"):
        time.sleep(0.05)
    job = _wait(client, client.post(f"/api/projects/{pid}/ecu/flash", json={"link_corrupt_rate": 0.1}).json()["id"])
    assert job["result"]["success"], job["result"]["error"]
    p = client.get(f"/api/projects/{pid}").json()
    assert p["flashed_version"]["id"] == v2["id"]
    assert "v2 (flashed, bank B)" in client.get("/api/live/status").json()["calibration"]
    read = client.post(f"/api/projects/{pid}/ecu/read", json={}).json()
    assert read["matches_version"] == v2["id"] and not read["differs_from_working_copy"]
    backups = client.get(f"/api/projects/{pid}/ecu/backups").json()
    assert backups and backups[0]["valid"]
    rb = client.post(f"/api/projects/{pid}/ecu/rollback").json()
    assert rb["success"] and client.get(f"/api/projects/{pid}").json()["flashed_version"]["version"] == 1
    rs = client.post(f"/api/projects/{pid}/ecu/backups/{backups[0]['name']}/restore").json()
    assert rs["success"]
    reports = client.get(f"/api/projects/{pid}/reports").json()
    assert sum(r["kind"] == "flash" for r in reports) >= 3
    client.post("/api/live/stop")


def test_api_power_loss_and_recovery(client):
    pid = client.post("/api/projects", json={"name": "brick test", "preset_id": "b58-inspired"}).json()["id"]
    client.post(f"/api/projects/{pid}/calibration/scalars/rev_limit_rpm", json={"value": 6400})
    client.post(f"/api/projects/{pid}/calibration/save", json={})
    job = _wait(client, client.post(f"/api/projects/{pid}/ecu/flash",
                                    json={"power_loss_stage": "write", "power_loss_block": 3}).json()["id"])
    assert not job["result"]["success"]
    assert client.get(f"/api/projects/{pid}/ecu").json()["status"]["mode"] == "off"
    boot = client.post(f"/api/projects/{pid}/ecu/power-cycle").json()["boot"]
    assert boot["mode"] == "application" and boot["bank"] == "A"
    assert client.post(f"/api/projects/{pid}/ecu/backups/../../etc/passwd/restore").status_code in (400, 404, 405)
    assert client.post(f"/api/projects/{pid}/ecu/backups/..%2F..%2Fx.otci/restore").status_code in (400, 404, 405)
    assert client.post(f"/api/projects/{pid}/ecu/backups/a b;.otci/restore").status_code == 400
    meta = client.get("/api/flash/meta").json()
    assert "fictional" in meta["protocol"]
