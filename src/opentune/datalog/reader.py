"""Read OpenTune CSV datalogs back into memory."""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from opentune.core.models import Sample
from opentune.datalog.logger import LOG_SUFFIX


class DatalogError(ValueError):
    pass


@dataclass
class Datalog:
    path: Path
    metadata: dict[str, str] = field(default_factory=dict)
    channels: list[str] = field(default_factory=list)
    samples: list[Sample] = field(default_factory=list)

    @property
    def engine(self) -> str:
        return self.metadata.get("engine", "B58")

    @property
    def duration_s(self) -> float:
        if len(self.samples) < 2:
            return 0.0
        return self.samples[-1].timestamp - self.samples[0].timestamp

    def series(self, key: str) -> list[tuple[float, float]]:
        return [(s.timestamp, s.values[key]) for s in self.samples if key in s.values]


def read_log(path: str | Path) -> Datalog:
    path = Path(path)
    metadata: dict[str, str] = {}
    data_lines: list[str] = []
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("#"):
                    key, _, value = line[1:].partition(":")
                    metadata[key.strip()] = value.strip()
                elif line.strip():
                    data_lines.append(line)
    except OSError as exc:
        raise DatalogError(f"Cannot read {path}: {exc}") from exc
    if not data_lines:
        raise DatalogError(f"{path.name} contains no data")
    reader = csv.reader(data_lines)
    header = next(reader)
    if not header or header[0] != "time_s":
        raise DatalogError(f"{path.name} is not an OpenTune datalog (missing time_s column)")
    channels = header[1:]
    samples: list[Sample] = []
    for row_no, row in enumerate(reader, start=2):
        if not row:
            continue
        try:
            t = float(row[0])
        except (ValueError, IndexError) as exc:
            raise DatalogError(f"{path.name}: bad time value on data row {row_no}") from exc
        values: dict[str, float] = {}
        for key, cell in zip(channels, row[1:], strict=False):
            if cell == "":
                continue
            try:
                values[key] = float(cell)
            except ValueError:
                continue
        samples.append(Sample(t, values))
    return Datalog(path, metadata, channels, samples)


def list_logs(directory: str | Path) -> list[Path]:
    directory = Path(directory)
    if not directory.exists():
        return []
    return sorted(directory.glob(f"*{LOG_SUFFIX}"), key=lambda p: p.stat().st_mtime, reverse=True)


@dataclass
class LogSummary:
    path: Path
    metadata: dict[str, str]
    sample_count: int
    duration_s: float

    @property
    def engine(self) -> str:
        return self.metadata.get("engine", "?")


def summarize_log(path: str | Path) -> LogSummary:
    """Metadata, row count and duration without parsing every value (fast for big logs)."""
    path = Path(path)
    metadata: dict[str, str] = {}
    count = 0
    first = last = None
    try:
        with path.open(encoding="utf-8") as fh:
            header_seen = False
            for line in fh:
                if line.startswith("#"):
                    key, _, value = line[1:].partition(":")
                    metadata[key.strip()] = value.strip()
                    continue
                if not line.strip():
                    continue
                if not header_seen:
                    header_seen = True
                    continue
                stamp = line.split(",", 1)[0]
                try:
                    t = float(stamp)
                except ValueError:
                    continue
                first = t if first is None else first
                last = t
                count += 1
    except OSError as exc:
        raise DatalogError(f"Cannot read {path}: {exc}") from exc
    duration = (last - first) if first is not None and last is not None else 0.0
    return LogSummary(path, metadata, count, duration)
