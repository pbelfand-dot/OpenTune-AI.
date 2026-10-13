"""OTSIM — OpenTune's *fictional* calibration container for the simulated ECU.

This is NOT a BMW/Bosch format and contains no real calibration addresses,
checksums or map layouts. It exists so the full tuning-development workflow
(edit maps -> validate -> flash simulated ECU -> log -> analyse) can be
exercised safely. The simulator reads its boost, ignition, lambda and rev-limit
behaviour from these tables, so edits have visible effects.

Binary layout (little-endian)::

    header  : magic "OTSIMCAL"(8s) format_version(H) engine(8s) cal_version(16s) table_count(H)
    table*  : name(24s) unit(8s) row_label(8s) col_label(8s) rows(H) cols(H) scale(f)
              row_axis(rows * f) col_axis(cols * f) data(rows * cols * h)   value = raw * scale
    trailer : crc32(I) over every preceding byte
"""

from __future__ import annotations

import bisect
import struct
import zlib
from dataclasses import dataclass, field

from opentune.engines.profile import EngineProfile

MAGIC = b"OTSIMCAL"
FORMAT_VERSION = 1
_HEADER = struct.Struct("<8sH8s16sH")
_TABLE = struct.Struct("<24s8s8s8sHHf")
_CRC = struct.Struct("<I")

RPM_AXIS = (800.0, 1200.0, 1600.0, 2000.0, 2500.0, 3000.0, 4000.0, 5000.0, 6000.0, 7000.0)
PEDAL_AXIS = (0.0, 20.0, 40.0, 60.0, 80.0, 100.0)
LOAD_AXIS = (0.0, 20.0, 40.0, 60.0, 80.0, 100.0)

REQUIRED_TABLES = ("boost_target", "ignition_base", "lambda_target", "rev_limit")


class CalibrationFormatError(ValueError):
    """Raised when bytes are not a valid OTSIM calibration."""


def _pack_str(value: str, size: int) -> bytes:
    raw = value.encode("ascii", errors="replace")[:size]
    return raw.ljust(size, b"\x00")


def _unpack_str(raw: bytes) -> str:
    return raw.rstrip(b"\x00").decode("ascii", errors="replace")


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _interp_index(axis: tuple[float, ...] | list[float], value: float) -> tuple[int, int, float]:
    """Return (lower index, upper index, fraction) for linear interpolation, clamped."""
    if len(axis) == 1 or value <= axis[0]:
        return 0, 0, 0.0
    if value >= axis[-1]:
        last = len(axis) - 1
        return last, last, 0.0
    hi = bisect.bisect_right(axis, value)
    lo = hi - 1
    span = axis[hi] - axis[lo]
    frac = 0.0 if span == 0 else (value - axis[lo]) / span
    return lo, hi, frac


@dataclass
class CalTable:
    name: str
    unit: str
    row_label: str
    col_label: str
    row_axis: list[float]
    col_axis: list[float]
    values: list[list[float]]
    scale: float = 0.001

    def __post_init__(self) -> None:
        if len(self.values) != len(self.row_axis):
            raise CalibrationFormatError(f"{self.name}: row count does not match row axis")
        for row in self.values:
            if len(row) != len(self.col_axis):
                raise CalibrationFormatError(f"{self.name}: column count does not match column axis")
        for axis_name, axis in (("row", self.row_axis), ("column", self.col_axis)):
            if any(b <= a for a, b in zip(axis, axis[1:], strict=False)):
                raise CalibrationFormatError(f"{self.name}: {axis_name} axis must be strictly increasing")

    @property
    def shape(self) -> tuple[int, int]:
        return len(self.row_axis), len(self.col_axis)

    def lookup(self, row_value: float, col_value: float = 0.0) -> float:
        """Bilinear interpolation, clamped to the table edges."""
        r0, r1, rf = _interp_index(self.row_axis, row_value)
        c0, c1, cf = _interp_index(self.col_axis, col_value)
        v00 = self.values[r0][c0]
        v01 = self.values[r0][c1]
        v10 = self.values[r1][c0]
        v11 = self.values[r1][c1]
        top = v00 + (v01 - v00) * cf
        bottom = v10 + (v11 - v10) * cf
        return top + (bottom - top) * rf

    def quantized(self) -> CalTable:
        """Return a copy with values rounded to the storage resolution."""
        values = [[round(v / self.scale) * self.scale for v in row] for row in self.values]
        return CalTable(self.name, self.unit, self.row_label, self.col_label,
                        list(self.row_axis), list(self.col_axis), values, self.scale)


@dataclass
class SimCalibration:
    engine: str
    version: str
    tables: dict[str, CalTable] = field(default_factory=dict)

    def table(self, name: str) -> CalTable:
        try:
            return self.tables[name]
        except KeyError as exc:
            raise CalibrationFormatError(f"Calibration is missing table {name!r}") from exc

    @property
    def rev_limit(self) -> float:
        return self.table("rev_limit").values[0][0]

    def validate(self) -> list[str]:
        """Return a list of problems (empty when the calibration is usable)."""
        problems = [f"missing table: {name}" for name in REQUIRED_TABLES if name not in self.tables]
        if problems:
            return problems
        boost = self.table("boost_target")
        if max(max(row) for row in boost.values) > 3.0:
            problems.append("boost_target exceeds the simulator safety ceiling of 3.0 bar")
        lam = self.table("lambda_target")
        if min(min(row) for row in lam.values) < 0.70:
            problems.append("lambda_target below 0.70 is outside the simulator's supported range")
        ign = self.table("ignition_base")
        if max(max(row) for row in ign.values) > 60.0:
            problems.append("ignition_base above 60 degrees is outside the simulator's range")
        if not 3000 <= self.rev_limit <= 9000:
            problems.append("rev_limit must be between 3000 and 9000 rpm")
        for table in self.tables.values():
            limit = 32767 * table.scale
            for row in table.values:
                for value in row:
                    if abs(value) > limit:
                        problems.append(f"{table.name}: value {value} exceeds storage range ±{limit:g}")
                        break
        return problems

    # ------------------------------------------------------------------ binary I/O
    def to_bytes(self) -> bytes:
        out = bytearray(_HEADER.pack(MAGIC, FORMAT_VERSION, _pack_str(self.engine, 8),
                                     _pack_str(self.version, 16), len(self.tables)))
        for table in self.tables.values():
            rows, cols = table.shape
            out += _TABLE.pack(_pack_str(table.name, 24), _pack_str(table.unit, 8),
                               _pack_str(table.row_label, 8), _pack_str(table.col_label, 8),
                               rows, cols, table.scale)
            out += struct.pack(f"<{rows}f", *table.row_axis)
            out += struct.pack(f"<{cols}f", *table.col_axis)
            raw = []
            for row in table.values:
                for value in row:
                    q = round(value / table.scale)
                    if not -32768 <= q <= 32767:
                        raise CalibrationFormatError(f"{table.name}: value {value} out of storage range")
                    raw.append(q)
            out += struct.pack(f"<{rows * cols}h", *raw)
        out += _CRC.pack(zlib.crc32(bytes(out)) & 0xFFFFFFFF)
        return bytes(out)

    @classmethod
    def from_bytes(cls, data: bytes) -> SimCalibration:
        if len(data) < _HEADER.size + _CRC.size or not data.startswith(MAGIC):
            raise CalibrationFormatError("Not an OTSIM calibration (bad magic)")
        body, (crc,) = data[:-_CRC.size], _CRC.unpack(data[-_CRC.size:])
        if zlib.crc32(body) & 0xFFFFFFFF != crc:
            raise CalibrationFormatError("OTSIM checksum mismatch — file is corrupt or was modified externally")
        magic, version, engine, cal_version, count = _HEADER.unpack_from(body, 0)
        if version != FORMAT_VERSION:
            raise CalibrationFormatError(f"Unsupported OTSIM format version {version}")
        offset = _HEADER.size
        tables: dict[str, CalTable] = {}
        try:
            for _ in range(count):
                name, unit, rlabel, clabel, rows, cols, scale = _TABLE.unpack_from(body, offset)
                offset += _TABLE.size
                row_axis = list(struct.unpack_from(f"<{rows}f", body, offset))
                offset += 4 * rows
                col_axis = list(struct.unpack_from(f"<{cols}f", body, offset))
                offset += 4 * cols
                flat = struct.unpack_from(f"<{rows * cols}h", body, offset)
                offset += 2 * rows * cols
                values = [[flat[r * cols + c] * scale for c in range(cols)] for r in range(rows)]
                table = CalTable(_unpack_str(name), _unpack_str(unit), _unpack_str(rlabel),
                                 _unpack_str(clabel), row_axis, col_axis, values, scale)
                tables[table.name] = table
        except struct.error as exc:
            raise CalibrationFormatError(f"Truncated OTSIM calibration: {exc}") from exc
        if offset != len(body):
            raise CalibrationFormatError("Unexpected trailing data in OTSIM calibration")
        return cls(_unpack_str(engine), _unpack_str(cal_version), tables)


def is_simcal(data: bytes) -> bool:
    return data.startswith(MAGIC)


def default_calibration(profile: EngineProfile, version: str | None = None) -> SimCalibration:
    """Build the simulator's 'stock' calibration for an engine profile.

    The table values are modelling choices derived from the profile's
    simulation parameters — they are not BMW factory values.
    """
    s = profile.sim
    rpm_axis = [r for r in RPM_AXIS if r <= s.rev_limit_rpm] or list(RPM_AXIS)
    if rpm_axis[-1] < s.rev_limit_rpm:
        rpm_axis.append(float(s.rev_limit_rpm))

    def full_boost(rpm: float) -> float:
        spool = _clamp((rpm - s.spool_start_rpm) / max(1.0, s.full_spool_rpm - s.spool_start_rpm), 0.0, 1.0)
        taper_start = s.torque_plateau_rpm[1]
        if rpm <= taper_start:
            peak = s.peak_boost_bar
        else:
            t = _clamp((rpm - taper_start) / max(1.0, s.rev_limit_rpm - taper_start), 0.0, 1.0)
            peak = s.peak_boost_bar + (s.boost_taper_bar - s.peak_boost_bar) * t
        return peak * spool

    boost = [[-0.65 + (p / 100.0) ** 1.3 * (0.65 + full_boost(rpm)) for p in PEDAL_AXIS] for rpm in rpm_axis]

    def ignition(rpm: float, load: float) -> float:
        light = 12.0 + 26.0 * _clamp((rpm - 600.0) / 2400.0, 0.0, 1.0)
        full = 2.0 + 12.0 * _clamp((rpm - 1000.0) / 5500.0, 0.0, 1.0)
        frac = _clamp(load / 100.0, 0.0, 1.0)
        return light * (1.0 - frac) + full * frac

    ign = [[ignition(rpm, load) for load in LOAD_AXIS] for rpm in rpm_axis]

    def lam(rpm: float, load: float) -> float:
        enrich = _clamp((load - 70.0) / 30.0, 0.0, 1.0) * (0.12 + 0.06 * _clamp((rpm - 3000.0) / 4000.0, 0.0, 1.0))
        return 1.0 - enrich

    lam_tbl = [[lam(rpm, load) for load in LOAD_AXIS] for rpm in rpm_axis]

    tables = {
        "boost_target": CalTable("boost_target", "bar", "rpm", "pedal%", rpm_axis, list(PEDAL_AXIS), boost, 0.001),
        "ignition_base": CalTable("ignition_base", "deg", "rpm", "load%", rpm_axis, list(LOAD_AXIS), ign, 0.1),
        "lambda_target": CalTable("lambda_target", "lambda", "rpm", "load%", rpm_axis, list(LOAD_AXIS), lam_tbl, 0.001),
        "rev_limit": CalTable("rev_limit", "rpm", "-", "-", [0.0], [0.0], [[float(s.rev_limit_rpm)]], 1.0),
    }
    cal = SimCalibration(profile.code, version or f"SIM-{profile.code}-STOCK", tables)
    # Round-trip through storage resolution so in-memory and on-disk versions match exactly.
    return SimCalibration.from_bytes(cal.to_bytes())
