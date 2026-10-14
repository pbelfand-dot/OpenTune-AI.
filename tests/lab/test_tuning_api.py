"""Tuning-engine REST API and CLI (small budgets; no network, no hardware)."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from opentune.lab.api.app import create_app
from opentune.lab.cli import main as lab_main


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    with TestClient(create_app(tmp_path_factory.mktemp("lab"))) as c:
        yield c


@pytest.fixture(scope="module")
def pid(client):
    return client.post("/api/projects", json={"name": "tuning api", "preset_id": "b48-inspired"}).json()["id"]


def _wait(client, job_id, timeout=180.0):
    end = time.time() + timeout
    while time.time() < end:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in ("queued", "running"):
            return job
        time.sleep(0.1)
    raise AssertionError("job timed out")


def test_meta(client, pid):
    m = client.get(f"/api/tuning/meta?project_id={pid}").json()
    assert m["ready_for_vehicle"] is False and m["disclaimer"]
    assert {g["id"] for g in m["goals"]} >= {"max_power", "efficiency"}
    assert "torque.mid" in m["default_actions"] and m["limits"]["max_iat_c"] is None
    assert set(m["tables_changed"]) == {"torque_limit", "boost_max", "ign_base", "lambda_target"}


def test_session_lifecycle_and_apply(client, pid):
    bad = client.post("/api/tuning/sessions", json={"project_id": pid, "request": {"goal": "nope"}})
    assert bad.status_code == 400
    job = client.post("/api/tuning/sessions", json={
        "project_id": pid, "request": {"goal": "midrange_torque", "budget": 1, "actions": ["torque.mid"],
                                       "validate": False}}).json()
    done = _wait(client, job["id"])
    assert done["status"] == "done", done.get("error")
    sid = done["result"]["session_id"]
    assert done["result"]["ready_for_vehicle"] is False
    rep = client.get(f"/api/tuning/sessions/{sid}").json()
    assert rep["id"] == sid and "calibration" not in rep
    assert client.get(f"/api/tuning/sessions/{sid}?include_calibration=true").json()["calibration"]
    assert any(s["id"] == sid for s in client.get(f"/api/tuning/sessions?project_id={pid}").json())
    applied = client.post(f"/api/projects/{pid}/tuning/apply", json={"session_id": sid}).json()
    assert applied["ready_for_vehicle"] is False
    if rep["final"]["changes"]:
        assert applied["changed_tables"]
        wc = client.get(f"/api/projects/{pid}/calibration").json()
        assert wc["dirty"] is True
    other = client.post("/api/projects", json={"name": "other", "preset_id": "b48-inspired"}).json()["id"]
    assert client.post(f"/api/projects/{other}/tuning/apply", json={"session_id": sid}).status_code == 404
    assert client.get("/api/tuning/sessions/unknown").status_code == 404


def test_knowledge_and_mistakes_endpoints(client, pid):
    k = client.get(f"/api/tuning/knowledge?project_id={pid}").json()
    assert k["engine_key"] == "b48-inspired" and k["trials"] >= 1
    assert k["sources"]["torque.mid"].startswith("engine")
    assert len(k["sensitivities"]["torque.mid"]["mean_torque_nm"]) == 2
    assert isinstance(client.get("/api/tuning/mistakes").json(), list)
    assert client.get("/api/tuning/benchmarks").json() == []
    assert client.post("/api/tuning/knowledge/reset", json={}).status_code == 400
    assert client.post("/api/tuning/knowledge/reset", json={"confirm": True}).json() == {"ok": True}
    assert client.get("/api/tuning/knowledge").json()["trials"] == 0


def test_cli_tune(tmp_path, capsys):
    rc = lab_main(["tune", "--engine", "b48-inspired", "--goal", "efficiency", "--budget", "0", "--no-validate",
                   "--action", "part_spark", "--data-dir", str(tmp_path), "--out", str(tmp_path / "out")])
    out = capsys.readouterr().out
    assert rc == 0 and "SIMULATED tuning session" in out and "ready_for_vehicle: False" in out
    assert list((tmp_path / "out").glob("tuning_*.json"))
    assert (tmp_path / "lab" / "tuning.sqlite").exists()
