"""Lab REST/WebSocket API (FastAPI TestClient, no network, no browser)."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from opentune.lab.api.app import create_app

FAST_SWEEP = {"kind": "sweep", "rpm_start": 2000, "rpm_end": 3200, "ramp_rpm_s": 600, "settle_s": 0.5}


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    app = create_app(tmp_path_factory.mktemp("lab"))
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def pid(client):
    r = client.post("/api/projects", json={"name": "api test", "preset_id": "b48-inspired"})
    assert r.status_code == 200
    return r.json()["id"]


def _wait(client, job_id, timeout=90.0):
    end = time.time() + timeout
    while time.time() < end:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in ("queued", "running"):
            return job
        time.sleep(0.05)
    raise AssertionError("job timed out")


def test_system_reports_safety(client):
    s = client.get("/api/system").json()
    assert s["safety"]["real_ecu_write_enabled"] is False
    assert "none" in s["safety"]["physical_hardware_access"]
    assert {p["id"] for p in s["provenance_kinds"]} == {"measured", "documented", "simulated", "assumption",
                                                        "speculative"}
    assert client.get("/api/health").json() == {"ok": True}


def test_demo_projects_and_presets(client):
    names = [p["name"] for p in client.get("/api/projects").json()]
    assert any("B58" in n for n in names) and any("B48" in n for n in names)
    presets = client.get("/api/engines/presets").json()
    assert {p["id"] for p in presets} == {"b58-inspired", "b48-inspired"}
    assert all(p["documented_parameters"] for p in presets)
    schema = client.get("/api/engines/schema").json()
    assert any(s["path"] == "geometry.bore" for s in schema)


def test_engine_validate_and_preview(client):
    cfg = client.get("/api/engines/presets/b58-inspired").json()
    assert client.post("/api/engines/validate", json={"config": cfg}).json()["valid"]
    cfg["geometry"]["bore"] = -1
    assert not client.post("/api/engines/validate", json={"config": cfg}).json()["valid"]
    pv = client.post("/api/engines/preview", json={"config": client.get("/api/engines/presets/b58-inspired").json(),
                                                   "rpm_step": 1000}).json()
    assert len(pv["points"]) >= 6 and all("torque_nm" in p for p in pv["points"])


def test_project_crud(client):
    p = client.post("/api/projects", json={"name": "tmp", "preset_id": "b58-inspired"}).json()
    assert p["active_version"]["version"] == 1 and p["flashed_version"]["version"] == 1  # factory-programmed
    p2 = client.patch(f"/api/projects/{p['id']}", json={"name": "renamed", "notes": "n"}).json()
    assert p2["name"] == "renamed" and p2["notes"] == "n"
    cfg = p2["config"]
    cfg["turbo"]["compressor_diameter"] = 0.052
    p3 = client.patch(f"/api/projects/{p['id']}", json={"config": cfg}).json()
    assert p3["config"]["turbo"]["compressor_diameter"] == 0.052
    assert client.post(f"/api/projects/{p['id']}/regenerate-baseline").json()["active_version"]["version"] == 2
    assert client.delete(f"/api/projects/{p['id']}").status_code == 200
    assert client.get(f"/api/projects/{p['id']}").status_code == 404
    assert client.post("/api/projects", json={"name": ""}).status_code == 422


def test_calibration_edit_undo_save_diff(client, pid):
    base = client.get(f"/api/projects/{pid}/calibration").json()
    assert not base["dirty"] and len(base["tables"]) >= 15
    t = client.get(f"/api/projects/{pid}/calibration/tables/boost_max").json()
    old = t["values"][5][5]
    r = client.post(f"/api/projects/{pid}/calibration/tables/boost_max/edit",
                    json={"op": "set_cell", "index": [5, 5], "value": old - 10}).json()
    assert r["values"][5][5] == pytest.approx(old - 10)
    assert r["delta"][5][5] == pytest.approx(-10)
    assert client.get(f"/api/projects/{pid}/calibration").json()["dirty"]
    bad = client.post(f"/api/projects/{pid}/calibration/tables/boost_max/edit",
                      json={"op": "set_cell", "index": [5, 5], "value": 9999})
    assert bad.status_code == 400 and "allowed range" in bad.json()["detail"]
    client.post(f"/api/projects/{pid}/calibration/undo")
    assert client.get(f"/api/projects/{pid}/calibration/tables/boost_max").json()["values"][5][5] == old
    client.post(f"/api/projects/{pid}/calibration/redo")
    client.post(f"/api/projects/{pid}/calibration/scalars/rev_limit_rpm", json={"value": 6300})
    saved = client.post(f"/api/projects/{pid}/calibration/save", json={"note": "less boost"}).json()
    assert saved["version"]["version"] == 2 and not saved["dirty"]
    d = client.get(f"/api/projects/{pid}/calibration/diff").json()
    assert d["tables"]["boost_max"]["changed_cells"] == 1
    assert "rev_limit_rpm" in d["scalars"]
    vers = client.get(f"/api/projects/{pid}/calibration/versions").json()
    assert [v["version"] for v in vers] == [1, 2] and vers[0]["is_baseline"] and vers[1]["active"]


def test_calibration_checkout_requires_clean_copy(client, pid):
    vers = client.get(f"/api/projects/{pid}/calibration/versions").json()
    client.post(f"/api/projects/{pid}/calibration/tables/ign_base/edit",
                json={"op": "region", "operation": "add", "amount": 0.5, "region": [[0, 2], [0, 2]]})
    r = client.post(f"/api/projects/{pid}/calibration/versions/{vers[1]['id']}/checkout", json={})
    assert r.status_code == 409
    r = client.post(f"/api/projects/{pid}/calibration/versions/{vers[1]['id']}/checkout",
                    json={"discard_changes": True})
    assert r.status_code == 200 and not r.json()["dirty"]


def test_calibration_import_export(client, pid):
    text = client.get(f"/api/projects/{pid}/calibration/export.json").text
    assert client.post(f"/api/projects/{pid}/calibration/import", json={"text": text}).status_code == 200
    csv_text = client.get(f"/api/projects/{pid}/calibration/tables/lambda_target/csv").text
    assert client.post(f"/api/projects/{pid}/calibration/tables/lambda_target/csv",
                       json={"text": csv_text}).status_code == 200
    mat = client.get(f"/api/projects/{pid}/calibration/export.mat")
    assert mat.status_code == 200 and mat.content[:6] == b"MATLAB"
    assert client.post(f"/api/projects/{pid}/calibration/import", json={"text": "{}"}).status_code in (400, 404)


def test_dyno_job_runs_and_compare(client, pid):
    j1 = client.post("/api/dyno/run", json={"project_id": pid, "test": FAST_SWEEP,
                                            "calibration": {"source": "baseline"}}).json()
    j2 = client.post("/api/dyno/run", json={"project_id": pid, "test": FAST_SWEEP,
                                            "calibration": {"source": "active"}}).json()
    r1, r2 = _wait(client, j1["id"]), _wait(client, j2["id"])
    assert r1["status"] == "done" and r2["status"] == "done", (r1["error"], r2["error"])
    ids = [r1["result"]["run_ids"][0], r2["result"]["run_ids"][0]]
    runs = client.get(f"/api/dyno/runs?project_id={pid}").json()
    assert {r["id"] for r in runs} >= set(ids)
    run = client.get(f"/api/dyno/runs/{ids[0]}").json()
    assert run["provenance"] == "simulated" and run["samples"] == [] and run["curve"]
    cmp = client.post("/api/dyno/compare", json={"run_ids": ids}).json()
    assert len(cmp["runs"]) == 2 and cmp["rpm"]
    fig = client.post("/api/dyno/figure", json={"run_ids": ids}).json()
    assert len(fig["data"]) == 4
    csv_text = client.get(f"/api/dyno/runs/{ids[0]}/curve.csv").text
    assert csv_text.startswith("# OpenTune Virtual Engine Lab — SIMULATED")
    uj = client.post(f"/api/dyno/runs/{ids[0]}/uncertainty", json={"samples": 8}).json()
    assert _wait(client, uj["id"])["status"] == "done"
    assert client.get(f"/api/dyno/runs/{ids[0]}").json()["uncertainty"]["samples"] == 8
    bad = client.post("/api/dyno/run", json={"project_id": pid, "test": {"kind": "warp"}})
    assert bad.status_code == 400


def test_dyno_job_cancel(client, pid):
    j = client.post("/api/dyno/run", json={"project_id": pid, "test": {"kind": "repeat", "repeats": 5}}).json()
    client.delete(f"/api/jobs/{j['id']}")
    assert _wait(client, j["id"])["status"] == "cancelled"


def test_live_session_controls_and_diagnostics(client, pid):
    st = client.post("/api/live/start", json={"project_id": pid}).json()
    assert st["state"] == "OFF" and "flashed, bank A" in st["calibration"]
    client.post("/api/live/control", json={"action": "crank"})
    end = time.time() + 10
    while time.time() < end and client.get("/api/live/status").json()["state"] != "RUNNING":
        time.sleep(0.05)
    assert client.get("/api/live/status").json()["state"] == "RUNNING"
    client.post("/api/live/control", json={"action": "load", "load": "dyno", "target_rpm": 2500})
    client.post("/api/live/control", json={"action": "pedal", "value": 30})
    assert client.post("/api/live/control", json={"action": "pedal", "value": 300}).status_code in (400, 422)
    client.post("/api/diagnostics/faults", json={"sensor": "ect", "kind": "open_circuit"})
    end = time.time() + 10
    codes = []
    while time.time() < end and "P0118" not in codes:
        time.sleep(0.1)
        codes = [d["code"] for d in client.get("/api/diagnostics/dtcs").json()]
    assert "P0118" in codes
    sensors = {s["id"]: s for s in client.get("/api/diagnostics/sensors").json()}
    assert sensors["ect"]["fault"] == "open_circuit" and sensors["ect"]["volts"] > 4.8
    client.delete("/api/diagnostics/faults")
    client.post("/api/diagnostics/dtcs/clear")
    assert client.get("/api/diagnostics/dtcs").json() == []
    hist = client.get("/api/live/history?seconds=5").json()
    assert hist and hist[-1]["type"] == "frame" and "rpm" in hist[-1]["ch"]
    meta = client.get("/api/diagnostics/meta").json()
    assert "open_circuit" in meta["fault_types"]
    assert all(d["description"] for d in meta["dtcs"])
    client.post("/api/live/stop")
    assert client.get("/api/live/status").json() == {"running": False}


def test_websocket_streams_frames(client, pid):
    client.post("/api/live/start", json={"project_id": pid, "speed": 2.0})
    with client.websocket_connect("/ws/live") as ws:
        first = ws.receive_json()
        assert first["type"] == "status" and first["running"] is True and first["project_id"] == pid
        kinds = set()
        for _ in range(10):
            msg = ws.receive_json()
            kinds.add(msg["type"])
            if msg["type"] == "frame":
                assert set(msg["ch"]) >= {"rpm", "map_kpa", "lambda", "spark_deg"}
        assert "frame" in kinds
    client.post("/api/live/stop")


def test_unknown_resources_are_404(client):
    assert client.get("/api/projects/nope").status_code == 404
    assert client.get("/api/dyno/runs/nope").status_code == 404
    assert client.get("/api/jobs/nope").status_code == 404
    assert client.get("/api/does-not-exist").status_code == 404


def test_ui_or_fallback_page_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "OpenTune" in r.text
