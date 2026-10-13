"""High-level, read-only OBD-II diagnostic client.

``ObdClient`` exposes generic SAE J1979 services only. It never writes to an
ECU's memory; the single state-changing service is Mode 04 (clear DTCs),
which every generic scan tool provides and which callers must confirm.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from opentune.obd.dtc import DtcInfo, describe, parse_dtc_payload
from opentune.obd.elm327 import Elm327, ElmError, NoDataError
from opentune.obd.pids import (
    CHANNEL_TO_PID,
    MONITOR_STATUS_PID,
    PIDS,
    SUPPORT_PIDS,
    MonitorStatus,
    decode_monitor_status,
    decode_supported,
)
from opentune.obd.transport import Transport


@dataclass
class DtcReport:
    stored: list[DtcInfo] = field(default_factory=list)
    pending: list[DtcInfo] = field(default_factory=list)
    permanent: list[DtcInfo] = field(default_factory=list)
    mil_on: bool = False

    @property
    def total(self) -> int:
        return len({d.code for d in self.stored + self.pending + self.permanent})


class ObdClient:
    def __init__(self, transport: Transport, timeout: float = 5.0) -> None:
        self.elm = Elm327(transport, timeout=timeout)
        self._supported: set[int] | None = None
        self.connected = False

    # ------------------------------------------------------------------ session
    def connect(self) -> None:
        self.elm.initialize()
        self._supported = None
        self.connected = True

    def close(self) -> None:
        self.connected = False
        self.elm.close()

    @property
    def is_can(self) -> bool:
        return bool(self.elm.protocol and self.elm.protocol.is_can)

    # ------------------------------------------------------------------ Mode 01
    def supported_pids(self) -> set[int]:
        if self._supported is not None:
            return set(self._supported)
        supported: set[int] = set()
        for base in SUPPORT_PIDS:
            if base != 0x00 and base not in supported:
                break
            try:
                data = self.elm.request(0x01, base)
            except NoDataError:
                break
            supported |= decode_supported(base, data)
        self._supported = supported
        return set(supported)

    def read_pid(self, pid: int) -> float:
        definition = PIDS[pid]
        data = self.elm.request(0x01, pid)
        if len(data) < definition.num_bytes:
            raise ElmError(f"PID {pid:02X}: expected {definition.num_bytes} bytes, got {len(data)}")
        return definition.to_channel(data)

    def available_channels(self) -> list[str]:
        """Channels that can be read from this vehicle via generic OBD-II."""
        supported = self.supported_pids()
        channels = [ch for ch, pid in CHANNEL_TO_PID.items() if pid in supported]
        if "map_kpa" in channels and "baro_kpa" in channels:
            channels.append("boost")
        return channels

    def read_channels(self, channels: list[str]) -> dict[str, float]:
        """Read the requested channels; unsupported or unanswered ones are skipped."""
        values: dict[str, float] = {}
        supported = self.supported_pids()
        wanted = set(channels)
        if "boost" in wanted:
            wanted |= {"map_kpa", "baro_kpa"}
        for key in sorted(wanted):
            pid = CHANNEL_TO_PID.get(key)
            if pid is None or pid not in supported:
                continue
            try:
                values[key] = self.read_pid(pid)
            except NoDataError:
                continue
        if "boost" in channels and "map_kpa" in values and "baro_kpa" in values:
            values["boost"] = (values["map_kpa"] - values["baro_kpa"]) / 100.0
        return values

    def monitor_status(self) -> MonitorStatus:
        return decode_monitor_status(self.elm.request(0x01, MONITOR_STATUS_PID))

    # ------------------------------------------------------------------ DTC services
    def _read_dtc_mode(self, mode: int) -> list[DtcInfo]:
        try:
            payloads = self.elm.request_all(mode)
        except NoDataError:
            return []
        codes: list[str] = []
        for payload in payloads:
            for code in parse_dtc_payload(payload, self.is_can):
                if code not in codes:
                    codes.append(code)
        return [describe(code) for code in codes]

    def read_dtcs(self) -> DtcReport:
        status = self.monitor_status()
        return DtcReport(
            stored=self._read_dtc_mode(0x03),
            pending=self._read_dtc_mode(0x07),
            permanent=self._read_dtc_mode(0x0A),
            mil_on=status.mil_on,
        )

    def clear_dtcs(self) -> None:
        """Mode 04. Also resets readiness monitors and freeze frames on a real car."""
        self.elm.request(0x04)

    # ------------------------------------------------------------------ Mode 09
    def read_vin(self) -> str | None:
        try:
            data = self.elm.request(0x09, 0x02)
        except NoDataError:
            return None
        # CAN replies start with a 'number of data items' byte before the 17 ASCII chars.
        text = bytes(b for b in data if 0x20 < b < 0x7F).decode("ascii")
        return text[-17:] if len(text) >= 17 else (text or None)

    def adapter_voltage(self) -> float | None:
        return self.elm.battery_voltage()
