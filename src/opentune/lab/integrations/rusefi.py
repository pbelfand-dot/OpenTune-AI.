"""Read-only client for the rusEFI *simulator* (TunerStudio binary protocol over TCP, loopback only).

rusEFI (https://github.com/rusefi/rusefi) is open-source engine-management
firmware licensed under the GNU GPL v3 (plus additional terms in its
repository). OpenTune does not include, link or copy any rusEFI code. This
module is an independent client of the documented TunerStudio framing that the
rusEFI simulator speaks, so the two programs only exchange data over a socket.

Framing (rusEFI ``firmware/console/binary/tunerstudio*.cpp``):
``[u16 BE length][payload][u32 BE CRC-32 of payload]``; response payload byte 0
is a status code (0 = OK). Command bytes are defined in rusEFI's
``firmware/integration/ts_protocol.txt``.

Safety: the client sends **only** read-only commands (``S`` hello/signature,
``V`` firmware version, ``O`` output channels) and connects **only** to a
loopback address, i.e. a simulator on this machine. Write (``C``), burn (``B``),
execute (``E``), I/O test (``Z``) and every other command are refused before
anything is sent. It cannot talk to an ECU over USB/serial at all.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import struct
import subprocess
import time
import zlib
from dataclasses import asdict, dataclass
from pathlib import Path

READ_ONLY_COMMANDS = frozenset({b"S", b"V", b"O"})
DEFAULT_PORT = 29001
TS_RESPONSE_OK = 0x00

# Output-channel offsets from rusEFI firmware/live_data_generated/output_channels_generated.h
# (checked at rusEFI commit 2670d5b, 2026-10-13). They change between firmware versions, so
# values are labelled with this layout and a warning is added if the signature differs.
LAYOUT_SOURCE = "rusEFI output_channels_generated.h @ 2670d5b (2026-10-13)"
LAYOUT_SIGNATURE_PREFIX = "rusEFI master.2026.10"
FIELDS = {  # name: (offset, struct format, scale, unit)
    "rpm": (4, "<H", 1.0, "rpm"),
    "coolant_c": (16, "<h", 0.01, "°C"),
    "intake_air_c": (18, "<h", 0.01, "°C"),
}


class RusefiError(RuntimeError):
    pass


class RusefiUnavailable(RusefiError):
    pass


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass
class RusefiSnapshot:
    signature: str
    firmware_version: str
    values: dict[str, float]
    units: dict[str, str]
    layout_source: str
    warnings: list[str]
    source: str = "rusEFI simulator (external process, read-only)"

    def to_dict(self) -> dict:
        return asdict(self)


class RusefiTsClient:
    """Minimal read-only TunerStudio-protocol client for a local rusEFI simulator."""

    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_PORT, timeout: float = 3.0) -> None:
        if not _is_loopback(host):
            raise RusefiError("Only a rusEFI simulator on this machine (loopback address) may be contacted")
        self.host, self.port, self.timeout = host, port, timeout
        self._sock: socket.socket | None = None
        self.sent_commands: list[bytes] = []

    def __enter__(self) -> RusefiTsClient:
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def connect(self) -> None:
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as exc:
            raise RusefiUnavailable(f"No rusEFI simulator listening on {self.host}:{self.port} ({exc})") from exc

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def _recv_exact(self, n: int) -> bytes:
        assert self._sock is not None
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise RusefiError("Connection closed by the simulator")
            buf += chunk
        return buf

    def request(self, payload: bytes) -> bytes:
        cmd = payload[:1]
        if cmd not in READ_ONLY_COMMANDS:
            raise RusefiError(f"Command {cmd!r} is not on the read-only whitelist; nothing was sent")
        if self._sock is None:
            self.connect()
        assert self._sock is not None
        frame = struct.pack(">H", len(payload)) + payload + struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF)
        self.sent_commands.append(cmd)
        try:
            self._sock.sendall(frame)
            (n,) = struct.unpack(">H", self._recv_exact(2))
            body = self._recv_exact(n)
            (crc,) = struct.unpack(">I", self._recv_exact(4))
        except (OSError, struct.error) as exc:
            raise RusefiError(f"Transfer failed: {exc}") from exc
        if crc != (zlib.crc32(body) & 0xFFFFFFFF):
            raise RusefiError("Response CRC mismatch")
        if not body or body[0] != TS_RESPONSE_OK:
            raise RusefiError(f"Simulator returned status 0x{body[0]:02x}" if body else "Empty response")
        return body[1:]

    def signature(self) -> str:
        return self.request(b"S").rstrip(b"\0").decode("ascii", "replace")

    def firmware_version(self) -> str:
        return self.request(b"V").rstrip(b"\0").decode("ascii", "replace")

    def output_channels(self, offset: int, count: int) -> bytes:
        if not (0 <= offset <= 0xFFFF and 0 < count <= 2048):
            raise RusefiError("offset/count out of range")
        data = self.request(b"O" + struct.pack("<HH", offset, count))
        if len(data) != count:
            raise RusefiError(f"Expected {count} bytes of output channels, got {len(data)}")
        return data

    def snapshot(self) -> RusefiSnapshot:
        sig = self.signature()
        ver = self.firmware_version()
        span = max(off + struct.calcsize(fmt) for off, fmt, _s, _u in FIELDS.values())
        raw = self.output_channels(0, span)
        values = {k: round(struct.unpack_from(fmt, raw, off)[0] * scale, 3) for k, (off, fmt, scale, _u) in
                  FIELDS.items()}
        warnings = []
        if not sig.startswith(LAYOUT_SIGNATURE_PREFIX):
            warnings.append(f"Signature {sig!r} differs from the layout this client was checked against "
                            f"({LAYOUT_SIGNATURE_PREFIX}…); channel offsets may be wrong.")
        return RusefiSnapshot(sig, ver, values, {k: v[3] for k, v in FIELDS.items()}, LAYOUT_SOURCE, warnings)


def simulator_binary() -> Path | None:
    """Path from ``OPENTUNE_RUSEFI_SIMULATOR`` (a locally built rusEFI simulator), if it exists."""
    p = os.environ.get("OPENTUNE_RUSEFI_SIMULATOR")
    if p and Path(p).is_file() and os.access(p, os.X_OK):
        return Path(p)
    return None


class RusefiSimulatorProcess:
    """Start a locally built rusEFI simulator for ``seconds`` and wait until its TS port answers."""

    def __init__(self, binary: Path, seconds: int = 30, port: int = DEFAULT_PORT) -> None:
        self.binary, self.seconds, self.port = binary, seconds, port
        self.proc: subprocess.Popen | None = None

    def __enter__(self) -> RusefiSimulatorProcess:
        self.proc = subprocess.Popen([str(self.binary), str(self.seconds)], cwd=self.binary.parent,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        end = time.time() + 15
        while time.time() < end:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    return self
            except OSError:
                time.sleep(0.2)
        self.__exit__()
        raise RusefiUnavailable("rusEFI simulator did not open its TunerStudio port")

    def __exit__(self, *exc) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def status() -> dict:
    sim = simulator_binary()
    return {"id": "rusefi", "name": "rusEFI simulator (read-only TS client)", "available": True,
            "simulator_configured": sim is not None, "simulator_path": str(sim) if sim else None,
            "license": "rusEFI: GPL-3.0 (+ additional terms). Not bundled; OpenTune only talks to it over a socket.",
            "detail": "Client is built in. Run a local rusEFI simulator (or set OPENTUNE_RUSEFI_SIMULATOR) to use it.",
            "build_help": "See docs/LAB_INTEGRATIONS.md (rusEFI simulator: Linux x86, 32-bit multilib build)."}
