"""SAE J1979 Mode 01 PID definitions (decode + encode).

Formulas follow the publicly documented SAE J1979 scaling (as summarised on
Wikipedia's "OBD-II PIDs" page and the ELM327 datasheet). ``decode`` converts
the raw data bytes into the SAE unit; ``channel_factor`` converts that into the
OpenTune channel's canonical unit (e.g. kPa -> bar). ``encode`` is the inverse
and is used by the ELM327 emulator.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass


def _clamp_int(value: float, low: int, high: int) -> int:
    return int(max(low, min(high, round(value))))


def _u16(data: Sequence[int]) -> int:
    return data[0] * 256 + data[1]


def _split16(value: float) -> bytes:
    v = _clamp_int(value, 0, 0xFFFF)
    return bytes((v >> 8, v & 0xFF))


@dataclass(frozen=True)
class PidDefinition:
    pid: int
    name: str
    num_bytes: int
    unit: str
    decode: Callable[[Sequence[int]], float]
    encode: Callable[[float], bytes]
    channel: str | None = None
    channel_factor: float = 1.0

    def to_channel(self, data: Sequence[int]) -> float:
        return self.decode(data) * self.channel_factor

    def from_channel(self, value: float) -> bytes:
        return self.encode(value / self.channel_factor)


def _percent(pid: int, name: str, channel: str | None) -> PidDefinition:
    return PidDefinition(pid, name, 1, "%", lambda d: d[0] * 100.0 / 255.0,
                         lambda v: bytes((_clamp_int(v * 255.0 / 100.0, 0, 255),)), channel)


def _temperature(pid: int, name: str, channel: str | None) -> PidDefinition:
    return PidDefinition(pid, name, 1, "°C", lambda d: d[0] - 40.0,
                         lambda v: bytes((_clamp_int(v + 40.0, 0, 255),)), channel)


def _fuel_trim(pid: int, name: str, channel: str | None) -> PidDefinition:
    return PidDefinition(pid, name, 1, "%", lambda d: d[0] * 100.0 / 128.0 - 100.0,
                         lambda v: bytes((_clamp_int((v + 100.0) * 128.0 / 100.0, 0, 255),)), channel)


_DEFINITIONS: tuple[PidDefinition, ...] = (
    _percent(0x04, "Calculated engine load", "engine_load"),
    _temperature(0x05, "Engine coolant temperature", "coolant_temp"),
    _fuel_trim(0x06, "Short term fuel trim - Bank 1", "stft"),
    _fuel_trim(0x07, "Long term fuel trim - Bank 1", "ltft"),
    PidDefinition(0x0A, "Fuel pressure (gauge)", 1, "kPa", lambda d: d[0] * 3.0,
                  lambda v: bytes((_clamp_int(v / 3.0, 0, 255),))),
    PidDefinition(0x0B, "Intake manifold absolute pressure", 1, "kPa", lambda d: float(d[0]),
                  lambda v: bytes((_clamp_int(v, 0, 255),)), "map_kpa"),
    PidDefinition(0x0C, "Engine speed", 2, "rpm", lambda d: _u16(d) / 4.0,
                  lambda v: _split16(v * 4.0), "rpm"),
    PidDefinition(0x0D, "Vehicle speed", 1, "km/h", lambda d: float(d[0]),
                  lambda v: bytes((_clamp_int(v, 0, 255),)), "vehicle_speed"),
    PidDefinition(0x0E, "Timing advance", 1, "° before TDC", lambda d: d[0] / 2.0 - 64.0,
                  lambda v: bytes((_clamp_int((v + 64.0) * 2.0, 0, 255),)), "timing_advance"),
    _temperature(0x0F, "Intake air temperature", "intake_air_temp"),
    PidDefinition(0x10, "Mass air flow rate", 2, "g/s", lambda d: _u16(d) / 100.0,
                  lambda v: _split16(v * 100.0), "maf"),
    _percent(0x11, "Throttle position", "throttle"),
    PidDefinition(0x1F, "Run time since engine start", 2, "s", lambda d: float(_u16(d)),
                  lambda v: _split16(v), "run_time"),
    PidDefinition(0x21, "Distance traveled with MIL on", 2, "km", lambda d: float(_u16(d)),
                  lambda v: _split16(v)),
    PidDefinition(0x23, "Fuel rail gauge pressure", 2, "kPa", lambda d: _u16(d) * 10.0,
                  lambda v: _split16(v / 10.0), "fuel_rail_pressure", channel_factor=0.01),
    _percent(0x2F, "Fuel tank level input", "fuel_level"),
    PidDefinition(0x33, "Absolute barometric pressure", 1, "kPa", lambda d: float(d[0]),
                  lambda v: bytes((_clamp_int(v, 0, 255),)), "baro_kpa"),
    PidDefinition(0x42, "Control module voltage", 2, "V", lambda d: _u16(d) / 1000.0,
                  lambda v: _split16(v * 1000.0), "battery_voltage"),
    PidDefinition(0x44, "Commanded air-fuel equivalence ratio", 2, "λ", lambda d: _u16(d) * 2.0 / 65536.0,
                  lambda v: _split16(v * 65536.0 / 2.0), "lambda_cmd"),
    _percent(0x45, "Relative throttle position", None),
    _temperature(0x46, "Ambient air temperature", "ambient_temp"),
    _percent(0x49, "Accelerator pedal position D", "pedal"),
    _percent(0x4C, "Commanded throttle actuator", None),
    _temperature(0x5C, "Engine oil temperature", "oil_temp"),
    PidDefinition(0x5E, "Engine fuel rate", 2, "L/h", lambda d: _u16(d) / 20.0,
                  lambda v: _split16(v * 20.0), "fuel_rate"),
)

PIDS: dict[int, PidDefinition] = {d.pid: d for d in _DEFINITIONS}
CHANNEL_TO_PID: dict[str, int] = {d.channel: d.pid for d in _DEFINITIONS if d.channel}

SUPPORT_PIDS = (0x00, 0x20, 0x40, 0x60, 0x80, 0xA0, 0xC0)
MONITOR_STATUS_PID = 0x01


def decode_supported(base_pid: int, data: Sequence[int]) -> set[int]:
    """Decode a 4-byte 'PIDs supported [base+1 .. base+0x20]' bitmask.

    Bits are read MSB-first: bit 7 of byte A is ``base+1``; bit 0 of byte D is
    ``base+0x20`` (which also signals that the next range query is supported).
    """
    supported: set[int] = set()
    for byte_index, byte in enumerate(data[:4]):
        for bit in range(8):
            if byte & (0x80 >> bit):
                supported.add(base_pid + byte_index * 8 + bit + 1)
    return supported


def encode_supported(base_pid: int, supported: set[int]) -> bytes:
    out = bytearray(4)
    for pid in supported:
        offset = pid - base_pid - 1
        if 0 <= offset < 32:
            out[offset // 8] |= 0x80 >> (offset % 8)
    return bytes(out)


@dataclass(frozen=True)
class MonitorStatus:
    mil_on: bool
    dtc_count: int
    raw: bytes


def decode_monitor_status(data: Sequence[int]) -> MonitorStatus:
    """PID 0x01: bit 7 of A is the MIL; bits 0-6 are the confirmed DTC count."""
    a = data[0]
    return MonitorStatus(mil_on=bool(a & 0x80), dtc_count=a & 0x7F, raw=bytes(data[:4]))


def encode_monitor_status(mil_on: bool, dtc_count: int) -> bytes:
    a = (0x80 if mil_on else 0) | min(dtc_count, 0x7F)
    # B: spark-ignition; misfire/fuel/component monitors available and complete.
    return bytes((a, 0x07, 0xE1, 0x00))
