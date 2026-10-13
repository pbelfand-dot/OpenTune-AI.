"""ELM327 + engine ECU emulator backed by :class:`SimulatedVehicle`.

Implements the :class:`~opentune.obd.transport.Transport` interface so the real
ELM327/OBD client code can be exercised end-to-end without hardware. It
emulates an ISO 15765-4 CAN (11-bit, 500 kbaud) vehicle — protocol 6 — and a
single engine ECU. Only generic SAE J1979 services are emulated:
01 (live data), 03 (stored DTCs), 04 (clear), 07 (pending), 09 (VIN), 0A
(permanent). Nothing here models BMW-specific diagnostics or programming.
"""

from __future__ import annotations

from opentune.obd.dtc import encode_dtc
from opentune.obd.pids import (
    PIDS,
    SUPPORT_PIDS,
    encode_monitor_status,
    encode_supported,
)
from opentune.simulation.vehicle import SimulatedVehicle

EMULATED_PIDS: frozenset[int] = frozenset(
    {0x01, 0x04, 0x05, 0x06, 0x07, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F, 0x10, 0x11, 0x1F, 0x21, 0x23,
     0x2F, 0x33, 0x42, 0x44, 0x45, 0x46, 0x49, 0x4C, 0x5C, 0x5E}
)
VERSION_STRING = "ELM327 v1.5"


class Elm327Emulator:
    def __init__(self, vehicle: SimulatedVehicle, realtime: bool = True) -> None:
        self.vehicle = vehicle
        self.realtime = realtime
        self._open = False
        self._pending = b""
        self._reset_settings()
        self.request_count = 0

    def _reset_settings(self) -> None:
        self.echo = True
        self.spaces = True
        self.headers = False
        self.linefeeds = False
        self.protocol = "0"
        self.protocol_found = False

    # ------------------------------------------------------------------ Transport API
    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open

    def write(self, data: bytes) -> None:
        if not self._open:
            raise OSError("Emulator port not open")
        cmd = data.decode("ascii", errors="replace").strip().upper().replace(" ", "")
        self.request_count += 1
        reply = self._handle(cmd)
        eol = "\r\n" if self.linefeeds else "\r"
        out = ""
        if self.echo:
            out += cmd + eol
        out += eol.join(reply) + eol + eol
        self._pending = out.encode("ascii")

    def read_until_prompt(self, timeout: float) -> bytes:
        data, self._pending = self._pending, b""
        return data

    # ------------------------------------------------------------------ command handling
    def _fmt(self, data: bytes) -> str:
        sep = " " if self.spaces else ""
        return sep.join(f"{b:02X}" for b in data)

    def _frames(self, payload: bytes) -> list[str]:
        """Format a response the way an ELM327 shows CAN replies with headers off."""
        if len(payload) <= 7:
            return [self._fmt(payload)]
        sep = " " if self.spaces else ""
        lines = [f"{len(payload):03X}"]
        first, rest = payload[:6], payload[6:]
        lines.append(f"0:{sep}{self._fmt(first)}")
        index = 1
        while rest:
            chunk, rest = rest[:7], rest[7:]
            chunk = chunk.ljust(7, b"\x00")
            lines.append(f"{index % 16:X}:{sep}{self._fmt(chunk)}")
            index += 1
        return lines

    def _handle(self, cmd: str) -> list[str]:
        if cmd.startswith("AT"):
            return self._handle_at(cmd[2:])
        if not cmd or len(cmd) % 2 or any(c not in "0123456789ABCDEF" for c in cmd):
            return ["?"]
        search = [] if self.protocol_found else ["SEARCHING..."]
        self.protocol_found = True
        if self.realtime:
            self.vehicle.sync()
        try:
            payload = self._obd(bytes.fromhex(cmd))
        except LookupError:
            return [*search, "NO DATA"]
        return [*search, *self._frames(payload)]

    def _handle_at(self, at: str) -> list[str]:
        if at == "Z":
            self._reset_settings()
            return ["", VERSION_STRING]
        if at == "I":
            return [VERSION_STRING]
        if at == "@1":
            return ["OpenTune ELM327 Emulator"]
        if at in ("E0", "E1"):
            self.echo = at == "E1"
            return ["OK"]
        if at in ("S0", "S1"):
            self.spaces = at == "S1"
            return ["OK"]
        if at in ("H0", "H1"):
            self.headers = at == "H1"
            return ["OK"]
        if at in ("L0", "L1"):
            self.linefeeds = at == "L1"
            return ["OK"]
        if at.startswith("SP") and len(at) == 3:
            self.protocol = at[2]
            self.protocol_found = at[2] != "0"
            return ["OK"]
        if at == "DPN":
            return ["A6" if self.protocol == "0" else "6"]
        if at == "DP":
            return ["AUTO, ISO 15765-4 (CAN 11/500)" if self.protocol == "0" else "ISO 15765-4 (CAN 11/500)"]
        if at == "RV":
            return [f"{self.vehicle.sample().values['battery_voltage']:.1f}V"]
        if at == "D":
            self._reset_settings()
            return ["OK"]
        if at == "WS":
            self._reset_settings()
            return ["", VERSION_STRING]
        return ["?"]

    def _obd(self, request: bytes) -> bytes:
        mode = request[0]
        if mode == 0x01 and len(request) == 2:
            return bytes((0x41, request[1])) + self._mode01(request[1])
        if mode in (0x03, 0x07, 0x0A) and len(request) == 1:
            memory = self.vehicle.dtc_memory()
            codes = {0x03: memory.stored, 0x07: memory.pending, 0x0A: memory.permanent}[mode]
            body = bytes((len(codes),)) + b"".join(encode_dtc(c) for c in codes)
            return bytes((mode + 0x40,)) + body
        if mode == 0x04 and len(request) == 1:
            self.vehicle.clear_dtcs()
            return b"\x44"
        if mode == 0x09 and len(request) == 2:
            if request[1] == 0x00:
                return b"\x49\x00" + encode_supported(0x00, {0x02})
            if request[1] == 0x02:
                return b"\x49\x02\x01" + self.vehicle.vin.encode("ascii")
        raise LookupError("unsupported request")

    def _mode01(self, pid: int) -> bytes:
        if pid in SUPPORT_PIDS:
            supported = {p for p in EMULATED_PIDS if pid < p <= pid + 0x20}
            if any(p > pid + 0x20 for p in EMULATED_PIDS):
                supported.add(pid + 0x20)
            if not supported and pid != 0x00:
                raise LookupError(pid)
            return encode_supported(pid, supported)
        if pid not in EMULATED_PIDS:
            raise LookupError(pid)
        if pid == 0x01:
            memory = self.vehicle.dtc_memory()
            return encode_monitor_status(memory.mil_on, len(memory.stored))
        values = self.vehicle.sample().values
        definition = PIDS[pid]
        if pid == 0x21:
            return definition.encode(0.0)
        if pid in (0x45, 0x4C):
            return definition.encode(values["throttle"])
        assert definition.channel is not None
        return definition.from_channel(values[definition.channel])
