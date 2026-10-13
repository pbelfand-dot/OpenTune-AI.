"""Project library: engine configurations, calibration lineages, dyno runs and reports.

Everything lives in one SQLite file (``<data dir>/lab/lab.sqlite``) next to the
calibration version store, so a project can be backed up by copying one file.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from opentune.lab.calibration.defaults import build_baseline
from opentune.lab.calibration.store import CalibrationStore
from opentune.lab.calibration.tables import Calibration
from opentune.lab.dyno.runs import DynoRun
from opentune.lab.physics.config import EngineConfig
from opentune.lab.physics.presets import get_preset

SCHEMA = """
CREATE TABLE IF NOT EXISTS lab_projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    preset_id TEXT NOT NULL,
    config TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    calibration_lineage TEXT NOT NULL,
    active_version_id TEXT NOT NULL,
    flashed_version_id TEXT,
    created_utc TEXT NOT NULL,
    updated_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lab_dyno_runs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    label TEXT NOT NULL,
    kind TEXT NOT NULL,
    created_utc TEXT NOT NULL,
    summary TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_project ON lab_dyno_runs(project_id, created_utc);
CREATE TABLE IF NOT EXISTS lab_reports (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    created_utc TEXT NOT NULL,
    data TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class LibraryError(KeyError):
    pass


@dataclass
class Project:
    id: str
    name: str
    preset_id: str
    config: dict
    notes: str
    calibration_lineage: str
    active_version_id: str
    flashed_version_id: str | None
    created_utc: str
    updated_utc: str

    def engine_config(self) -> EngineConfig:
        return EngineConfig.from_dict(self.config)

    def to_dict(self) -> dict:
        return asdict(self)


class ProjectLibrary:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as db:
            db.executescript(SCHEMA)
        self.calibrations = CalibrationStore(self.path)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    # ------------------------------------------------------------------ projects
    @staticmethod
    def _project(row: sqlite3.Row) -> Project:
        return Project(row["id"], row["name"], row["preset_id"], json.loads(row["config"]), row["notes"],
                       row["calibration_lineage"], row["active_version_id"], row["flashed_version_id"],
                       row["created_utc"], row["updated_utc"])

    def create_project(self, name: str, preset_id: str = "b58-inspired", config: EngineConfig | None = None,
                       notes: str = "") -> Project:
        cfg = config or get_preset(preset_id)
        problems = cfg.validate()
        if problems:
            raise ValueError("Invalid engine configuration: " + "; ".join(problems[:5]))
        cal = build_baseline(cfg)
        version = self.calibrations.save(cal, note="Synthetic baseline generated from the engine model")
        self.calibrations.set_baseline_for_lineage(version.id)
        pid = uuid.uuid4().hex[:12]
        now = _now()
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO lab_projects VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (pid, name, preset_id, json.dumps(cfg.to_dict()), notes, version.lineage, version.id, None,
                        now, now))
        return self.get_project(pid)

    def get_project(self, project_id: str) -> Project:
        with self._connect() as db:
            row = db.execute("SELECT * FROM lab_projects WHERE id=?", (project_id,)).fetchone()
        if row is None:
            raise LibraryError(f"Unknown project {project_id}")
        return self._project(row)

    def list_projects(self) -> list[Project]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM lab_projects ORDER BY updated_utc DESC").fetchall()
        return [self._project(r) for r in rows]

    def update_project(self, project_id: str, *, name: str | None = None, notes: str | None = None,
                       config: EngineConfig | None = None, active_version_id: str | None = None,
                       flashed_version_id: str | None = None) -> Project:
        project = self.get_project(project_id)
        if config is not None:
            problems = config.validate()
            if problems:
                raise ValueError("Invalid engine configuration: " + "; ".join(problems[:5]))
        for vid in (active_version_id, flashed_version_id):
            if vid is not None and self.calibrations.info(vid).lineage != project.calibration_lineage:
                raise ValueError("Calibration version belongs to another project")
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE lab_projects SET name=?, notes=?, config=?, active_version_id=?, flashed_version_id=?, "
                "updated_utc=? WHERE id=?",
                (name if name is not None else project.name, notes if notes is not None else project.notes,
                 json.dumps(config.to_dict()) if config is not None else json.dumps(project.config),
                 active_version_id or project.active_version_id,
                 flashed_version_id if flashed_version_id is not None else project.flashed_version_id,
                 _now(), project_id))
        return self.get_project(project_id)

    def delete_project(self, project_id: str) -> None:
        project = self.get_project(project_id)
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM lab_dyno_runs WHERE project_id=?", (project_id,))
            db.execute("DELETE FROM lab_reports WHERE project_id=?", (project_id,))
            db.execute("DELETE FROM lab_projects WHERE id=?", (project_id,))
        self.calibrations.delete_lineage(project.calibration_lineage)

    # ------------------------------------------------------------------ calibrations
    def load_calibration(self, project_id: str, which: str = "active", version_id: str | None = None) -> Calibration:
        project = self.get_project(project_id)
        if which == "version":
            if not version_id or self.calibrations.info(version_id).lineage != project.calibration_lineage:
                raise LibraryError("Unknown calibration version for this project")
            return self.calibrations.load(version_id)
        if which == "baseline":
            info = self.calibrations.lineage_baseline(project.calibration_lineage)
            return self.calibrations.load(info.id if info else project.active_version_id)
        if which == "flashed":
            if not project.flashed_version_id:
                raise LibraryError("Nothing has been flashed to the virtual ECU yet")
            return self.calibrations.load(project.flashed_version_id)
        return self.calibrations.load(project.active_version_id)

    # ------------------------------------------------------------------ dyno runs
    def save_run(self, project_id: str, run: DynoRun) -> None:
        self.get_project(project_id)
        with self._lock, self._connect() as db:
            db.execute("INSERT OR REPLACE INTO lab_dyno_runs VALUES (?,?,?,?,?,?,?)",
                       (run.id, project_id, run.label, run.kind, run.created_utc, json.dumps(run.summary),
                        json.dumps(run.to_dict())))

    def list_runs(self, project_id: str | None = None) -> list[dict]:
        sql = "SELECT id, project_id, label, kind, created_utc, summary FROM lab_dyno_runs"
        args: tuple = ()
        if project_id:
            sql += " WHERE project_id=?"
            args = (project_id,)
        with self._connect() as db:
            rows = db.execute(sql + " ORDER BY created_utc DESC, rowid DESC", args).fetchall()
        return [{"id": r["id"], "project_id": r["project_id"], "label": r["label"], "kind": r["kind"],
                 "created_utc": r["created_utc"], "summary": json.loads(r["summary"])} for r in rows]

    def get_run(self, run_id: str) -> DynoRun:
        with self._connect() as db:
            row = db.execute("SELECT data FROM lab_dyno_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise LibraryError(f"Unknown dyno run {run_id}")
        return DynoRun.from_dict(json.loads(row["data"]))

    def run_project(self, run_id: str) -> str:
        with self._connect() as db:
            row = db.execute("SELECT project_id FROM lab_dyno_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise LibraryError(f"Unknown dyno run {run_id}")
        return row["project_id"]

    def delete_run(self, run_id: str) -> None:
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM lab_dyno_runs WHERE id=?", (run_id,))

    # ------------------------------------------------------------------ reports
    def save_report(self, project_id: str, kind: str, title: str, data: dict) -> str:
        rid = uuid.uuid4().hex[:12]
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO lab_reports VALUES (?,?,?,?,?,?)",
                       (rid, project_id, kind, title, _now(), json.dumps(data)))
        return rid

    def list_reports(self, project_id: str | None = None) -> list[dict]:
        sql = "SELECT id, project_id, kind, title, created_utc FROM lab_reports"
        args: tuple = ()
        if project_id:
            sql += " WHERE project_id=?"
            args = (project_id,)
        with self._connect() as db:
            rows = db.execute(sql + " ORDER BY created_utc DESC, rowid DESC", args).fetchall()
        return [dict(r) for r in rows]

    def get_report(self, report_id: str) -> dict:
        with self._connect() as db:
            row = db.execute("SELECT * FROM lab_reports WHERE id=?", (report_id,)).fetchone()
        if row is None:
            raise LibraryError(f"Unknown report {report_id}")
        d = dict(row)
        d["data"] = json.loads(d["data"])
        return d

    def delete_report(self, report_id: str) -> None:
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM lab_reports WHERE id=?", (report_id,))
