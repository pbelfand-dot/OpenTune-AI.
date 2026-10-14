"""Unit-labelled, range-checked calibration tables (0–3 dimensional).

A :class:`Calibration` is a named collection of :class:`Table` objects (1-D, 2-D
or 3-D maps with labelled axes) and :class:`Scalar` parameters. Every value has
an explicit unit and allowed range; every calibration carries a schema version
and a firmware-compatibility string used by the simulated flashing workflow.

All shipped calibrations are synthetic (``provenance = "assumption"``). They are
NOT extracted from any production ECU.
"""

from __future__ import annotations

import bisect
import copy
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

import numpy as np

SCHEMA_VERSION = 1
FIRMWARE_FAMILY = "OTVECU-1"  # OpenTune virtual ECU firmware family (fictional)


class CalibrationError(ValueError):
    """Raised for invalid calibration content or edits."""


@dataclass
class Axis:
    name: str
    label: str
    unit: str
    values: list[float]
    minimum: float
    maximum: float
    decimals: int = 0

    def __post_init__(self) -> None:
        # Canonical numeric types, so 10 and 10.0 hash identically after a JSON round trip.
        self.values = [float(v) for v in self.values]
        self.minimum = float(self.minimum)
        self.maximum = float(self.maximum)

    def validate(self, table_id: str) -> list[str]:
        problems = []
        if len(self.values) < 2:
            problems.append(f"{table_id}: axis '{self.name}' needs at least 2 breakpoints")
        for a, b in zip(self.values, self.values[1:], strict=False):
            if not b > a:
                problems.append(f"{table_id}: axis '{self.name}' must be strictly increasing")
                break
        for v in self.values:
            if not (self.minimum <= v <= self.maximum):
                problems.append(f"{table_id}: axis '{self.name}' value {v} outside {self.minimum}–{self.maximum} "
                                f"{self.unit}")
                break
        return problems

    def to_dict(self) -> dict:
        return {"name": self.name, "label": self.label, "unit": self.unit, "values": list(self.values),
                "minimum": self.minimum, "maximum": self.maximum, "decimals": self.decimals}

    @classmethod
    def from_dict(cls, d: dict) -> Axis:
        return cls(d["name"], d.get("label", d["name"]), d["unit"], [float(v) for v in d["values"]],
                   float(d["minimum"]), float(d["maximum"]), int(d.get("decimals", 0)))


def _locate(values: list[float], x: float) -> tuple[int, float]:
    """Lower index and interpolation fraction, clamped to the axis ends."""
    if x <= values[0]:
        return 0, 0.0
    last = len(values) - 1
    if x >= values[last]:
        return last - 1, 1.0
    hi = bisect.bisect_right(values, x)
    lo = hi - 1
    return lo, (x - values[lo]) / (values[hi] - values[lo])


@dataclass
class Table:
    id: str
    name: str
    description: str
    category: str
    unit: str
    minimum: float
    maximum: float
    axes: list[Axis]
    values: np.ndarray
    decimals: int = 1
    provenance: str = "assumption"

    def __post_init__(self) -> None:
        self.values = np.asarray(self.values, dtype=float)
        self.minimum = float(self.minimum)
        self.maximum = float(self.maximum)
        self._cache: list[float] | None = None
        self._axis_cache: list[list[float]] | None = None

    # ------------------------------------------------------------------ structure
    @property
    def ndim(self) -> int:
        return len(self.axes)

    @property
    def shape(self) -> tuple[int, ...]:
        return tuple(len(a.values) for a in self.axes)

    def invalidate(self) -> None:
        self._cache = None
        self._axis_cache = None

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not 1 <= self.ndim <= 3:
            problems.append(f"{self.id}: tables must have 1–3 axes")
        for axis in self.axes:
            problems += axis.validate(self.id)
        if self.values.shape != self.shape:
            problems.append(f"{self.id}: values shape {self.values.shape} does not match axes {self.shape}")
            return problems
        if not np.all(np.isfinite(self.values)):
            problems.append(f"{self.id}: contains non-finite values")
        lo, hi = float(self.values.min()), float(self.values.max())
        if lo < self.minimum or hi > self.maximum:
            problems.append(f"{self.id}: values {lo:g}…{hi:g} outside allowed {self.minimum:g}…{self.maximum:g} "
                            f"{self.unit}")
        return problems

    # ------------------------------------------------------------------ interpolation
    def lookup(self, *coords: float) -> float:
        """Multilinear interpolation, clamped at the axis ends (no extrapolation)."""
        if len(coords) != self.ndim:
            raise CalibrationError(f"{self.id}: expected {self.ndim} coordinates, got {len(coords)}")
        if self._cache is None:
            self._cache = self.values.ravel().tolist()
            self._axis_cache = [list(a.values) for a in self.axes]
        flat = self._cache
        axes = self._axis_cache
        if self.ndim == 1:
            i, t = _locate(axes[0], coords[0])
            return flat[i] + (flat[i + 1] - flat[i]) * t
        if self.ndim == 2:
            i, ti = _locate(axes[0], coords[0])
            j, tj = _locate(axes[1], coords[1])
            n1 = len(axes[1])
            a = flat[i * n1 + j]
            b = flat[i * n1 + j + 1]
            c = flat[(i + 1) * n1 + j]
            d = flat[(i + 1) * n1 + j + 1]
            top = a + (b - a) * tj
            bottom = c + (d - c) * tj
            return top + (bottom - top) * ti
        i, ti = _locate(axes[0], coords[0])
        j, tj = _locate(axes[1], coords[1])
        k, tk = _locate(axes[2], coords[2])
        n1, n2 = len(axes[1]), len(axes[2])
        result = 0.0
        for di, wi in ((0, 1.0 - ti), (1, ti)):
            for dj, wj in ((0, 1.0 - tj), (1, tj)):
                for dk, wk in ((0, 1.0 - tk), (1, tk)):
                    w = wi * wj * wk
                    if w:
                        result += w * flat[((i + di) * n1 + (j + dj)) * n2 + (k + dk)]
        return result

    def resample(self, new_axes: list[list[float]]) -> np.ndarray:
        """Values interpolated onto new breakpoints (used when axes are edited)."""
        grids = np.meshgrid(*[np.asarray(a, dtype=float) for a in new_axes], indexing="ij")
        out = np.empty(grids[0].shape)
        for idx in np.ndindex(out.shape):
            out[idx] = self.lookup(*(g[idx] for g in grids))
        return out

    # ------------------------------------------------------------------ serialisation
    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "description": self.description, "category": self.category,
                "unit": self.unit, "minimum": self.minimum, "maximum": self.maximum, "decimals": self.decimals,
                "provenance": self.provenance, "axes": [a.to_dict() for a in self.axes],
                "values": self.values.round(6).tolist()}

    @classmethod
    def from_dict(cls, d: dict) -> Table:
        return cls(d["id"], d["name"], d.get("description", ""), d.get("category", "general"), d["unit"],
                   float(d["minimum"]), float(d["maximum"]), [Axis.from_dict(a) for a in d["axes"]],
                   np.asarray(d["values"], dtype=float), int(d.get("decimals", 1)), d.get("provenance", "assumption"))


@dataclass
class Scalar:
    id: str
    name: str
    description: str
    category: str
    unit: str
    value: float
    minimum: float
    maximum: float
    decimals: int = 2
    provenance: str = "assumption"

    def __post_init__(self) -> None:
        self.value = float(self.value)
        self.minimum = float(self.minimum)
        self.maximum = float(self.maximum)

    def validate(self) -> list[str]:
        if not (self.minimum <= self.value <= self.maximum):
            return [f"{self.id}: {self.value:g} outside allowed {self.minimum:g}…{self.maximum:g} {self.unit}"]
        return []

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "description": self.description, "category": self.category,
                "unit": self.unit, "value": self.value, "minimum": self.minimum, "maximum": self.maximum,
                "decimals": self.decimals, "provenance": self.provenance}

    @classmethod
    def from_dict(cls, d: dict) -> Scalar:
        return cls(d["id"], d["name"], d.get("description", ""), d.get("category", "general"), d["unit"],
                   float(d["value"]), float(d["minimum"]), float(d["maximum"]), int(d.get("decimals", 2)),
                   d.get("provenance", "assumption"))


@dataclass
class TableDiff:
    id: str
    changed_cells: int
    max_abs_delta: float
    mean_delta: float
    delta: list | None  # same shape as the table when axes match
    axes_changed: bool

    def to_dict(self) -> dict:
        return {"id": self.id, "changed_cells": self.changed_cells, "max_abs_delta": self.max_abs_delta,
                "mean_delta": self.mean_delta, "delta": self.delta, "axes_changed": self.axes_changed}


@dataclass
class CalibrationDiff:
    tables: dict[str, TableDiff]
    scalars: dict[str, tuple[float, float]]
    added: list[str]
    removed: list[str]

    @property
    def identical(self) -> bool:
        return not self.added and not self.removed and not self.scalars and \
            all(t.changed_cells == 0 and not t.axes_changed for t in self.tables.values())

    def to_dict(self) -> dict:
        return {"identical": self.identical, "tables": {k: v.to_dict() for k, v in self.tables.items()},
                "scalars": {k: {"a": a, "b": b} for k, (a, b) in self.scalars.items()},
                "added": self.added, "removed": self.removed}


@dataclass
class Calibration:
    name: str
    engine_id: str
    tables: dict[str, Table] = field(default_factory=dict)
    scalars: dict[str, Scalar] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION
    firmware_family: str = FIRMWARE_FAMILY
    metadata: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ access
    def table(self, table_id: str) -> Table:
        try:
            return self.tables[table_id]
        except KeyError as exc:
            raise CalibrationError(f"Unknown table {table_id!r}") from exc

    def scalar(self, scalar_id: str) -> float:
        try:
            return self.scalars[scalar_id].value
        except KeyError as exc:
            raise CalibrationError(f"Unknown scalar {scalar_id!r}") from exc

    def lookup(self, table_id: str, *coords: float) -> float:
        return self.table(table_id).lookup(*coords)

    def validate(self) -> list[str]:
        problems: list[str] = []
        for t in self.tables.values():
            problems += t.validate()
        for s in self.scalars.values():
            problems += s.validate()
        return problems

    # ------------------------------------------------------------------ identity
    def content_dict(self) -> dict:
        """Canonical content used for hashing (excludes free-form metadata)."""
        return {"engine_id": self.engine_id, "schema_version": self.schema_version,
                "firmware_family": self.firmware_family,
                "tables": {k: self.tables[k].to_dict() for k in sorted(self.tables)},
                "scalars": {k: self.scalars[k].to_dict() for k in sorted(self.scalars)}}

    def content_hash(self) -> str:
        blob = json.dumps(self.content_dict(), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(blob).hexdigest()

    def to_dict(self) -> dict:
        data = self.content_dict()
        data.update({"name": self.name, "metadata": self.metadata, "content_hash": self.content_hash()})
        return data

    @classmethod
    def from_dict(cls, d: dict) -> Calibration:
        cal = cls(
            name=d.get("name", "imported"),
            engine_id=d["engine_id"],
            tables={k: Table.from_dict(v) for k, v in d.get("tables", {}).items()},
            scalars={k: Scalar.from_dict(v) for k, v in d.get("scalars", {}).items()},
            schema_version=int(d.get("schema_version", SCHEMA_VERSION)),
            firmware_family=d.get("firmware_family", FIRMWARE_FAMILY),
            metadata=dict(d.get("metadata", {})),
        )
        expected = d.get("content_hash")
        if expected and expected != cal.content_hash():
            raise CalibrationError("Calibration content hash mismatch — file corrupt or edited outside OpenTune")
        return cal

    def copy(self, name: str | None = None) -> Calibration:
        cal = copy.deepcopy(self)
        for t in cal.tables.values():
            t.invalidate()
        if name:
            cal.name = name
        return cal

    # ------------------------------------------------------------------ comparison
    def diff(self, other: Calibration) -> CalibrationDiff:
        """Differences other − self."""
        tables: dict[str, TableDiff] = {}
        for tid in sorted(set(self.tables) & set(other.tables)):
            a, b = self.tables[tid], other.tables[tid]
            axes_changed = [ax.values for ax in a.axes] != [ax.values for ax in b.axes]
            if a.values.shape == b.values.shape and not axes_changed:
                delta = b.values - a.values
                changed = int(np.count_nonzero(np.abs(delta) > 1e-9))
                tables[tid] = TableDiff(tid, changed, float(np.abs(delta).max(initial=0.0)),
                                        float(delta.mean()) if delta.size else 0.0, delta.round(6).tolist(), False)
            else:
                tables[tid] = TableDiff(tid, int(b.values.size), float("nan"), float("nan"), None, True)
        scalars = {sid: (self.scalars[sid].value, other.scalars[sid].value)
                   for sid in sorted(set(self.scalars) & set(other.scalars))
                   if abs(self.scalars[sid].value - other.scalars[sid].value) > 1e-12}
        added = sorted((set(other.tables) | set(other.scalars)) - (set(self.tables) | set(self.scalars)))
        removed = sorted((set(self.tables) | set(self.scalars)) - (set(other.tables) | set(other.scalars)))
        return CalibrationDiff(tables, scalars, added, removed)

    def summary(self) -> dict:
        categories: dict[str, list[str]] = {}
        for t in self.tables.values():
            categories.setdefault(t.category, []).append(t.id)
        for s in self.scalars.values():
            categories.setdefault(s.category, []).append(s.id)
        return {"name": self.name, "engine_id": self.engine_id, "tables": len(self.tables),
                "scalars": len(self.scalars), "categories": categories, "content_hash": self.content_hash()}
