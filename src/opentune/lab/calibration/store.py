"""SQLite-backed calibration version history.

Each save creates an immutable version row (JSON content + SHA-256). Versions
form lineages via ``parent_id``; one version per engine can be marked baseline.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from opentune.lab.calibration.tables import Calibration, CalibrationError

SCHEMA = """
CREATE TABLE IF NOT EXISTS calibration_versions (
    id TEXT PRIMARY KEY,
    lineage TEXT NOT NULL,
    name TEXT NOT NULL,
    engine_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    parent_id TEXT,
    created_utc TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    is_baseline INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cal_lineage ON calibration_versions(lineage, version);
"""


@dataclass
class VersionInfo:
    id: str
    lineage: str
    name: str
    engine_id: str
    version: int
    parent_id: str | None
    created_utc: str
    note: str
    is_baseline: bool
    content_hash: str

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class CalibrationStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as db:
            db.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _info(row: sqlite3.Row) -> VersionInfo:
        return VersionInfo(row["id"], row["lineage"], row["name"], row["engine_id"], row["version"],
                           row["parent_id"], row["created_utc"], row["note"], bool(row["is_baseline"]),
                           row["content_hash"])

    def save(self, calibration: Calibration, note: str = "", parent_id: str | None = None,
             lineage: str | None = None) -> VersionInfo:
        problems = calibration.validate()
        if problems:
            raise CalibrationError("Cannot save invalid calibration: " + "; ".join(problems[:5]))
        with self._lock, self._connect() as db:
            if parent_id and lineage is None:
                row = db.execute("SELECT lineage FROM calibration_versions WHERE id=?", (parent_id,)).fetchone()
                if row is None:
                    raise CalibrationError(f"Unknown parent version {parent_id}")
                lineage = row["lineage"]
            lineage = lineage or uuid.uuid4().hex[:12]
            version = (db.execute("SELECT MAX(version) FROM calibration_versions WHERE lineage=?",
                                  (lineage,)).fetchone()[0] or 0) + 1
            vid = uuid.uuid4().hex[:12]
            info = VersionInfo(vid, lineage, calibration.name, calibration.engine_id, version, parent_id,
                               datetime.now(timezone.utc).isoformat(timespec="seconds"), note, False,
                               calibration.content_hash())
            db.execute(
                "INSERT INTO calibration_versions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (info.id, info.lineage, info.name, info.engine_id, info.version, info.parent_id, info.created_utc,
                 info.note, 0, info.content_hash, json.dumps(calibration.to_dict())))
        return info

    def load(self, version_id: str) -> Calibration:
        with self._connect() as db:
            row = db.execute("SELECT data FROM calibration_versions WHERE id=?", (version_id,)).fetchone()
        if row is None:
            raise CalibrationError(f"Unknown calibration version {version_id}")
        cal = Calibration.from_dict(json.loads(row["data"]))  # verifies the content hash
        cal.metadata["version_id"] = version_id
        return cal

    def info(self, version_id: str) -> VersionInfo:
        with self._connect() as db:
            row = db.execute("SELECT * FROM calibration_versions WHERE id=?", (version_id,)).fetchone()
        if row is None:
            raise CalibrationError(f"Unknown calibration version {version_id}")
        return self._info(row)

    def list(self, engine_id: str | None = None) -> list[VersionInfo]:
        sql = "SELECT * FROM calibration_versions"
        args: tuple = ()
        if engine_id:
            sql += " WHERE engine_id=?"
            args = (engine_id,)
        sql += " ORDER BY created_utc DESC, version DESC"
        with self._connect() as db:
            return [self._info(r) for r in db.execute(sql, args).fetchall()]

    def history(self, lineage: str) -> list[VersionInfo]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM calibration_versions WHERE lineage=? ORDER BY version",
                              (lineage,)).fetchall()
        return [self._info(r) for r in rows]

    def set_baseline(self, version_id: str) -> None:
        info = self.info(version_id)
        with self._lock, self._connect() as db:
            db.execute("UPDATE calibration_versions SET is_baseline=0 WHERE engine_id=?", (info.engine_id,))
            db.execute("UPDATE calibration_versions SET is_baseline=1 WHERE id=?", (version_id,))

    def baseline(self, engine_id: str) -> VersionInfo | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM calibration_versions WHERE engine_id=? AND is_baseline=1",
                             (engine_id,)).fetchone()
        return self._info(row) if row else None

    def set_baseline_for_lineage(self, version_id: str) -> None:
        """Mark one version as the baseline of its lineage (projects keep separate baselines)."""
        info = self.info(version_id)
        with self._lock, self._connect() as db:
            db.execute("UPDATE calibration_versions SET is_baseline=0 WHERE lineage=?", (info.lineage,))
            db.execute("UPDATE calibration_versions SET is_baseline=1 WHERE id=?", (version_id,))

    def lineage_baseline(self, lineage: str) -> VersionInfo | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM calibration_versions WHERE lineage=? AND is_baseline=1",
                             (lineage,)).fetchone()
        return self._info(row) if row else None

    def delete_lineage(self, lineage: str) -> None:
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM calibration_versions WHERE lineage=?", (lineage,))

    def delete(self, version_id: str) -> None:
        with self._lock, self._connect() as db:
            children = db.execute("SELECT COUNT(*) FROM calibration_versions WHERE parent_id=?",
                                  (version_id,)).fetchone()[0]
            if children:
                raise CalibrationError("Cannot delete a version that other versions derive from")
            db.execute("DELETE FROM calibration_versions WHERE id=?", (version_id,))
