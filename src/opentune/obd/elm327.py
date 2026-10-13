"""ELM327 command layer: initialisation, request/response parsing, error mapping.

Assumes the adapter is configured with echo off (ATE0), linefeeds off (ATL0),
spaces off (ATS0) and headers off (ATH0). Multi-frame ISO 15765 (CAN) replies
are shown by the ELM327 as a byte-count line followed by ``N:`` numbered
lines; :func:`parse_response` reassembles them.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass

from opentune.obd.transport import Transport

INIT_COMMANDS = ("ATZ", "ATE0", "ATL0", "ATS0", "ATH0", "ATSP0")

_ERROR_STRINGS = (
    "UNABLE TO CONNECT",
    "BUS INIT",
    "CAN ERROR",
    "BUS ERROR",
    "BUFFER FULL",
    "DATA ERROR",
    "FB ERROR",
    "LV RESET",
    "STOPPED",
    "ERR",
)
_HEX_RE = re.compile(r"^[0-9A-F]+$")
_FRAME_RE = re.compile(r"^([0-9A-F]):([0-9A-F]+)$")


class ElmError(IOError):
    """Adapter reported an error (or replied with something unparseable)."""


class NoDataError(ElmError):
    """The vehicle did not answer the request (unsupported PID or no ECU)."""


@dataclass(frozen=True)
class Protocol:
    number: str
    is_can: bool


def clean_lines(raw: str) -> list[str]:
    """Split adapter output into non-empty lines, dropping 'SEARCHING...' and spaces."""
    lines = []
    for line in re.split(r"[\r\n]+", raw):
        line = line.strip()
        if not line or line.upper().startswith("SEARCHING"):
            continue
        lines.append(line.replace(" ", "").upper())
    return lines


def parse_response(raw: str) -> list[bytes]:
    """Turn raw adapter output into one byte message per responding ECU.

    Raises :class:`NoDataError` / :class:`ElmError` for adapter error strings.
    """
    lines = clean_lines(raw)
    if not lines:
        raise NoDataError("Empty response")
    for line in lines:
        if line == "NODATA":
            raise NoDataError("NO DATA")
        if line == "?":
            raise ElmError("Adapter did not understand the command ('?')")
        for err in _ERROR_STRINGS:
            if line.startswith(err.replace(" ", "")):
                raise ElmError(f"Adapter error: {err}")

    messages: list[bytes] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # Multi-frame: a 3-hex-digit byte count followed by numbered frames.
        if len(line) == 3 and _HEX_RE.match(line) and i + 1 < len(lines) and _FRAME_RE.match(lines[i + 1]):
            total = int(line, 16)
            data = bytearray()
            i += 1
            while i < len(lines) and (m := _FRAME_RE.match(lines[i])):
                data += bytes.fromhex(m.group(2))
                i += 1
            messages.append(bytes(data[:total]))
            continue
        if not _HEX_RE.match(line) or len(line) % 2:
            raise ElmError(f"Unexpected adapter response line: {line!r}")
        messages.append(bytes.fromhex(line))
        i += 1
    return messages


class Elm327:
    """Thread-safe command interface to an ELM327-compatible adapter."""

    def __init__(self, transport: Transport, timeout: float = 5.0) -> None:
        self.transport = transport
        self.timeout = timeout
        self._lock = threading.Lock()
        self.version = ""
        self.protocol: Protocol | None = None

    def command(self, cmd: str, timeout: float | None = None) -> str:
        """Send one command and return the raw text before the prompt."""
        with self._lock:
            self.transport.write((cmd + "\r").encode("ascii"))
            raw = self.transport.read_until_prompt(timeout or self.timeout)
        return raw.decode("ascii", errors="replace")

    def initialize(self) -> None:
        if not self.transport.is_open:
            self.transport.open()
        for cmd in INIT_COMMANDS:
            reply = self.command(cmd, timeout=10.0 if cmd == "ATZ" else None)
            if cmd == "ATZ":
                lines = [ln.strip() for ln in re.split(r"[\r\n]+", reply) if ln.strip()]
                lines = [ln for ln in lines if ln.upper().replace(" ", "") != "ATZ"]
                self.version = " ".join(lines) or "ELM327"
            elif "OK" not in reply.upper():
                raise ElmError(f"{cmd} failed: {reply.strip()!r}")
        # Trigger the automatic protocol search, then ask which protocol was found.
        self.request(0x01, 0x00)
        self.protocol = self.describe_protocol()

    def describe_protocol(self) -> Protocol:
        reply = " ".join(clean_lines(self.command("ATDPN")))
        number = reply.lstrip("A") or "0"
        # Protocols 6-9 are ISO 15765-4 CAN; A-C are SAE J1939 / user CAN.
        return Protocol(number=number, is_can=number[:1] in "6789ABC")

    def battery_voltage(self) -> float | None:
        reply = " ".join(clean_lines(self.command("ATRV")))
        match = re.search(r"(\d+(?:\.\d+)?)V", reply)
        return float(match.group(1)) if match else None

    def request_all(self, mode: int, pid: int | None = None) -> list[bytes]:
        """Send an OBD request; return the payload (after mode/PID) from every responding ECU."""
        cmd = f"{mode:02X}" if pid is None else f"{mode:02X}{pid:02X}"
        messages = parse_response(self.command(cmd))
        expected_mode = mode + 0x40
        payloads = []
        for msg in messages:
            if not msg or msg[0] != expected_mode:
                continue
            if pid is None:
                payloads.append(msg[1:])
            elif len(msg) >= 2 and msg[1] == pid:
                payloads.append(msg[2:])
        if not payloads:
            raise ElmError(f"No matching reply for {cmd}: {[m.hex() for m in messages]}")
        return payloads

    def request(self, mode: int, pid: int | None = None) -> bytes:
        """Send an OBD request and return the first matching payload."""
        return self.request_all(mode, pid)[0]

    def close(self) -> None:
        self.transport.close()
