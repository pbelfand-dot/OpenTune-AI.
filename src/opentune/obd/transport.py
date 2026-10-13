"""Byte transports for ELM327-compatible adapters.

A transport only moves bytes; command semantics live in :mod:`opentune.obd.elm327`.
``SerialTransport`` talks to USB/Bluetooth-serial adapters through pyserial
(optional dependency). The ELM327 emulator in :mod:`opentune.obd.emulator`
implements the same interface for cloud testing without a car.
"""

from __future__ import annotations

import time
from typing import Protocol, runtime_checkable

PROMPT = b">"


class TransportError(IOError):
    pass


@runtime_checkable
class Transport(Protocol):
    def open(self) -> None: ...

    def close(self) -> None: ...

    @property
    def is_open(self) -> bool: ...

    def write(self, data: bytes) -> None: ...

    def read_until_prompt(self, timeout: float) -> bytes:
        """Read until the ELM327 '>' prompt (prompt excluded) or timeout."""
        ...


class SerialTransport:
    """pyserial-backed transport (USB, or Bluetooth serial ports on macOS)."""

    def __init__(self, port: str, baudrate: int = 38400) -> None:
        self.port = port
        self.baudrate = baudrate
        self._serial = None

    def open(self) -> None:
        try:
            import serial  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise TransportError("pyserial is not installed: pip install pyserial") from exc
        try:
            self._serial = serial.Serial(self.port, self.baudrate, timeout=0.05)
        except (OSError, serial.SerialException) as exc:  # pragma: no cover - hardware
            raise TransportError(f"Cannot open {self.port}: {exc}") from exc

    def close(self) -> None:
        if self._serial is not None:
            self._serial.close()
            self._serial = None

    @property
    def is_open(self) -> bool:
        return self._serial is not None and self._serial.is_open

    def write(self, data: bytes) -> None:  # pragma: no cover - hardware
        if self._serial is None:
            raise TransportError("Port not open")
        self._serial.reset_input_buffer()
        self._serial.write(data)
        self._serial.flush()

    def read_until_prompt(self, timeout: float) -> bytes:  # pragma: no cover - hardware
        if self._serial is None:
            raise TransportError("Port not open")
        deadline = time.monotonic() + timeout
        buf = bytearray()
        while time.monotonic() < deadline:
            chunk = self._serial.read(self._serial.in_waiting or 1)
            if chunk:
                buf += chunk
                if PROMPT in buf:
                    return bytes(buf[: buf.index(PROMPT)])
        raise TransportError(f"Timed out waiting for adapter prompt (got {bytes(buf)!r})")


def list_serial_ports() -> list[str]:
    """Return available serial port device names (empty if pyserial is missing)."""
    try:
        from serial.tools import list_ports  # type: ignore[import-not-found]
    except ImportError:
        return []
    return sorted(p.device for p in list_ports.comports())
