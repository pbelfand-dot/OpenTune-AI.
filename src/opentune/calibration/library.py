"""Calibration file library: import, catalogue, verify, compare and export.

Files are stored content-addressed (``<sha256>.bin``) next to a JSON index, so
the same binary is never stored twice and silent corruption is detectable.
The library treats files as opaque bytes — it does not interpret real ECU
binaries (no addresses, checksums or map definitions for BMW ECUs are
included). Only OpenTune's fictional OTSIM format is parsed for map editing.
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
import zlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from opentune.calibration.simcal import SimCalibration, is_simcal

INDEX_NAME = "index.json"
MAX_FILE_BYTES = 64 * 1024 * 1024


class CalibrationLibraryError(ValueError):
    pass


@dataclass
class CalibrationRecord:
    id: str
    name: str
    engine: str
    sha256: str
    crc32: str
    size: int
    imported_utc: str
    origin: str = "imported"  # imported | simulated-stock | simulated-modified | ecu-backup
    source_filename: str = ""
    software_version: str = ""
    notes: str = ""
    tags: list[str] = field(default_factory=list)
    parent_id: str | None = None
    format: str = "binary"  # binary | otsim

    @property
    def short_hash(self) -> str:
        return self.sha256[:12]


@dataclass(frozen=True)
class DiffRegion:
    start: int
    end: int  # exclusive

    @property
    def length(self) -> int:
        return self.end - self.start


@dataclass
class CalibrationDiff:
    size_a: int
    size_b: int
    changed_bytes: int
    regions: list[DiffRegion]

    @property
    def identical(self) -> bool:
        return self.changed_bytes == 0 and self.size_a == self.size_b


def diff_bytes(a: bytes, b: bytes, merge_gap: int = 8) -> CalibrationDiff:
    """Byte-level comparison; nearby changes (within ``merge_gap``) merge into one region."""
    common = min(len(a), len(b))
    regions: list[DiffRegion] = []
    changed = 0
    start: int | None = None
    last_diff = -1
    for i in range(common):
        if a[i] != b[i]:
            changed += 1
            if start is None:
                start = i
            elif i - last_diff > merge_gap:
                regions.append(DiffRegion(start, last_diff + 1))
                start = i
            last_diff = i
    if start is not None:
        regions.append(DiffRegion(start, last_diff + 1))
    if len(a) != len(b):
        changed += abs(len(a) - len(b))
        regions.append(DiffRegion(common, max(len(a), len(b))))
    return CalibrationDiff(len(a), len(b), changed, regions)


class CalibrationLibrary:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._records: dict[str, CalibrationRecord] = {}
        self._load()

    # ------------------------------------------------------------------ persistence
    @property
    def index_path(self) -> Path:
        return self.root / INDEX_NAME

    def _load(self) -> None:
        if not self.index_path.exists():
            return
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise CalibrationLibraryError(f"Calibration index is corrupt: {exc}") from exc
        for item in data.get("calibrations", []):
            record = CalibrationRecord(**item)
            self._records[record.id] = record

    def _save(self) -> None:
        payload = {"version": 1, "calibrations": [asdict(r) for r in self._records.values()]}
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.index_path)

    def _blob_path(self, sha256: str) -> Path:
        return self.root / f"{sha256}.bin"

    # ------------------------------------------------------------------ queries
    def list(self, engine: str | None = None) -> list[CalibrationRecord]:
        with self._lock:
            records = [r for r in self._records.values() if engine is None or r.engine == engine]
        return sorted(records, key=lambda r: r.imported_utc, reverse=True)

    def get(self, cal_id: str) -> CalibrationRecord:
        with self._lock:
            if cal_id in self._records:
                return self._records[cal_id]
            matches = [r for r in self._records.values() if r.id.startswith(cal_id)]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise CalibrationLibraryError(f"No calibration with id {cal_id!r}")
        raise CalibrationLibraryError(f"Ambiguous calibration id prefix {cal_id!r}")

    def read_bytes(self, cal_id: str, verify: bool = True) -> bytes:
        record = self.get(cal_id)
        data = self._blob_path(record.sha256).read_bytes()
        if verify and hashlib.sha256(data).hexdigest() != record.sha256:
            raise CalibrationLibraryError(f"Integrity check failed for {record.name} ({record.id})")
        return data

    def verify(self, cal_id: str) -> bool:
        try:
            self.read_bytes(cal_id, verify=True)
        except (CalibrationLibraryError, OSError):
            return False
        return True

    def load_simcal(self, cal_id: str) -> SimCalibration:
        return SimCalibration.from_bytes(self.read_bytes(cal_id))

    # ------------------------------------------------------------------ mutations
    def add_bytes(self, data: bytes, name: str, engine: str, origin: str = "imported",
                  source_filename: str = "", software_version: str = "", notes: str = "",
                  tags: list[str] | None = None, parent_id: str | None = None) -> CalibrationRecord:
        if not data:
            raise CalibrationLibraryError("Refusing to import an empty file")
        if len(data) > MAX_FILE_BYTES:
            raise CalibrationLibraryError("File is larger than the 64 MiB library limit")
        sha = hashlib.sha256(data).hexdigest()
        fmt = "otsim" if is_simcal(data) else "binary"
        if fmt == "otsim":
            cal = SimCalibration.from_bytes(data)  # validates checksum/structure
            if cal.engine and cal.engine != engine:
                raise CalibrationLibraryError(
                    f"OTSIM calibration is for {cal.engine}, not {engine}")
            software_version = software_version or cal.version
        with self._lock:
            blob = self._blob_path(sha)
            if not blob.exists():
                tmp = blob.with_suffix(".part")
                tmp.write_bytes(data)
                tmp.replace(blob)
            record = CalibrationRecord(
                id=uuid.uuid4().hex[:12],
                name=name.strip() or source_filename or "calibration",
                engine=engine,
                sha256=sha,
                crc32=f"{zlib.crc32(data) & 0xFFFFFFFF:08X}",
                size=len(data),
                imported_utc=datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                origin=origin,
                source_filename=source_filename,
                software_version=software_version,
                notes=notes,
                tags=list(tags or []),
                parent_id=parent_id,
                format=fmt,
            )
            self._records[record.id] = record
            self._save()
        return record

    def import_file(self, path: str | Path, name: str = "", engine: str = "B58", **kwargs) -> CalibrationRecord:
        path = Path(path)
        if not path.is_file():
            raise CalibrationLibraryError(f"{path} is not a file")
        return self.add_bytes(path.read_bytes(), name or path.stem, engine,
                              source_filename=path.name, **kwargs)

    def add_simcal(self, cal: SimCalibration, name: str, origin: str = "simulated-stock",
                   notes: str = "", parent_id: str | None = None) -> CalibrationRecord:
        problems = cal.validate()
        if problems:
            raise CalibrationLibraryError("Calibration failed validation: " + "; ".join(problems))
        return self.add_bytes(cal.to_bytes(), name, cal.engine, origin=origin,
                              software_version=cal.version, notes=notes, parent_id=parent_id)

    def update(self, cal_id: str, **changes) -> CalibrationRecord:
        allowed = {"name", "notes", "tags", "software_version"}
        unknown = set(changes) - allowed
        if unknown:
            raise CalibrationLibraryError(f"Cannot edit fields: {', '.join(sorted(unknown))}")
        with self._lock:
            record = self.get(cal_id)
            for key, value in changes.items():
                setattr(record, key, value)
            self._save()
        return record

    def remove(self, cal_id: str) -> None:
        with self._lock:
            record = self.get(cal_id)
            del self._records[record.id]
            if not any(r.sha256 == record.sha256 for r in self._records.values()):
                self._blob_path(record.sha256).unlink(missing_ok=True)
            self._save()

    def export(self, cal_id: str, destination: str | Path) -> Path:
        record = self.get(cal_id)
        destination = Path(destination)
        if destination.is_dir():
            suffix = ".otsim" if record.format == "otsim" else ".bin"
            destination = destination / f"{record.name}{suffix}"
        data = self.read_bytes(cal_id)
        destination.write_bytes(data)
        return destination

    def compare(self, cal_a: str, cal_b: str) -> CalibrationDiff:
        return diff_bytes(self.read_bytes(cal_a), self.read_bytes(cal_b))
