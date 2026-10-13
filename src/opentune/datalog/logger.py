"""CSV datalog writer.

Format: ``#``-prefixed metadata lines, then a CSV header ``time_s,<channel>...``,
then one row per sample. Values are canonical metric units; the unit row
(``# units: ...``) documents them. Files open fine in Excel/Numbers/pandas
(use ``comment='#'``).
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from pathlib import Path

from opentune import __version__
from opentune.core.models import CHANNELS, Sample

LOG_SUFFIX = ".otlog.csv"
FORMAT_ID = "opentune-datalog-v1"


def make_log_name(engine: str, source: str, when: datetime | None = None) -> str:
    when = when or datetime.now()
    safe_source = "".join(ch if ch.isalnum() else "-" for ch in source.lower()).strip("-") or "log"
    return f"{when:%Y%m%d-%H%M%S}_{engine}_{safe_source}{LOG_SUFFIX}"


class DataLogger:
    """Write samples to a CSV datalog. Use as a context manager or call close()."""

    def __init__(self, path: str | Path, channels: list[str], engine: str, source: str,
                 notes: str = "", flush_every: int = 20) -> None:
        self.path = Path(path)
        self.channels = [c for c in channels if c in CHANNELS]
        self.engine = engine
        self.source = source
        self.flush_every = max(1, flush_every)
        self.rows = 0
        self._t0: float | None = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh: io.TextIOWrapper | None = self.path.open("w", newline="", encoding="utf-8")
        units = ",".join(CHANNELS[c].unit for c in self.channels)
        header = [
            f"# format: {FORMAT_ID}",
            f"# app_version: {__version__}",
            f"# engine: {engine}",
            f"# source: {source}",
            f"# started_utc: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
            f"# units: s,{units}",
        ]
        if notes:
            header.append(f"# notes: {notes.replace(chr(10), ' ')}")
        self._fh.write("\n".join(header) + "\n")
        self._writer = csv.writer(self._fh)
        self._writer.writerow(["time_s", *self.channels])

    def write(self, sample: Sample) -> None:
        if self._fh is None:
            raise ValueError("Logger is closed")
        if self._t0 is None:
            self._t0 = sample.timestamp
        row = [f"{sample.timestamp - self._t0:.3f}"]
        for key in self.channels:
            value = sample.values.get(key)
            row.append("" if value is None else f"{value:.{max(CHANNELS[key].decimals, 1) + 1}f}")
        self._writer.writerow(row)
        self.rows += 1
        if self.rows % self.flush_every == 0:
            self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    @property
    def closed(self) -> bool:
        return self._fh is None

    def __enter__(self) -> DataLogger:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
