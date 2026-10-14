"""OTVL — fictional OpenTune Virtual Link protocol and its in-process loopback transport.

The protocol is invented for the simulator. It deliberately does not implement
or imitate UDS/KWP services, BMW programming sequences, or any security-access
algorithm. The only transport is :class:`LoopbackLink`, which calls a
:class:`~opentune.lab.flash.device.VirtualEcuDevice` object in the same Python
process; there is no serial, CAN, USB, J2534 or network transport and none can
be plugged in (the programmer type-checks the link and the device).

Frame (big-endian)::

    "OT" | seq u8 | cmd u8 | length u16 | payload[length] | CRC-32 u32 (over everything before it)

Payload: ``json_length u16 | JSON object | raw data``. Responses use
``cmd | 0x80`` and carry a ``status`` field (0 = OK).
"""

from __future__ import annotations

import json
import random
import struct
from dataclasses import dataclass

from opentune.lab.flash.image import crc32

SYNC = b"OT"

CMD_HELLO = 0x01
CMD_STATUS = 0x02
CMD_START_SESSION = 0x10
CMD_END_SESSION = 0x11
CMD_READ = 0x20
CMD_ERASE = 0x30
CMD_WRITE = 0x31
CMD_VERIFY = 0x32
CMD_ACTIVATE = 0x33
CMD_RESET = 0x40

COMMAND_NAMES = {CMD_HELLO: "HELLO", CMD_STATUS: "STATUS", CMD_START_SESSION: "START_SESSION",
                 CMD_END_SESSION: "END_SESSION", CMD_READ: "READ", CMD_ERASE: "ERASE", CMD_WRITE: "WRITE",
                 CMD_VERIFY: "VERIFY", CMD_ACTIVATE: "ACTIVATE", CMD_RESET: "RESET"}

ST_OK = 0x00
ST_BAD_FRAME = 0x10
ST_BAD_SEQUENCE = 0x11
ST_NOT_PROGRAMMING = 0x20
ST_BAD_TOKEN = 0x21
ST_BANK_IN_USE = 0x22
ST_ENGINE_RUNNING = 0x23
ST_OUT_OF_RANGE = 0x30
ST_NOT_ERASED = 0x31
ST_VERIFY_FAILED = 0x40
ST_INCOMPATIBLE = 0x41
ST_UNKNOWN_COMMAND = 0x7F

STATUS_TEXT = {ST_OK: "ok", ST_BAD_FRAME: "frame CRC error", ST_BAD_SEQUENCE: "sequence error",
               ST_NOT_PROGRAMMING: "programming session required", ST_BAD_TOKEN: "invalid session handle",
               ST_BANK_IN_USE: "bank is in use by the running application",
               ST_ENGINE_RUNNING: "engine running — programming refused", ST_OUT_OF_RANGE: "address out of range",
               ST_NOT_ERASED: "target region not erased", ST_VERIFY_FAILED: "verification failed",
               ST_INCOMPATIBLE: "image incompatible with this ECU", ST_UNKNOWN_COMMAND: "unknown command"}


class LinkError(RuntimeError):
    pass


class LinkTimeout(LinkError):
    pass


class LinkDown(LinkError):
    pass


class FrameError(ValueError):
    pass


def encode_payload(header: dict, data: bytes = b"") -> bytes:
    blob = json.dumps(header, separators=(",", ":")).encode()
    return struct.pack(">H", len(blob)) + blob + data


def decode_payload(payload: bytes) -> tuple[dict, bytes]:
    if len(payload) < 2:
        raise FrameError("payload too short")
    (n,) = struct.unpack(">H", payload[:2])
    try:
        header = json.loads(payload[2: 2 + n]) if n else {}
    except ValueError as exc:
        raise FrameError("bad JSON header") from exc
    return header, payload[2 + n:]


def encode_frame(seq: int, cmd: int, payload: bytes) -> bytes:
    if len(payload) > 0xFFFF:
        raise FrameError("payload too long")
    body = SYNC + struct.pack(">BBH", seq & 0xFF, cmd & 0xFF, len(payload)) + payload
    return body + struct.pack(">I", crc32(body))


def decode_frame(frame: bytes) -> tuple[int, int, bytes]:
    if len(frame) < 10 or frame[:2] != SYNC:
        raise FrameError("bad sync")
    seq, cmd, length = struct.unpack(">BBH", frame[2:6])
    if len(frame) != 6 + length + 4:
        raise FrameError("length mismatch")
    (crc,) = struct.unpack(">I", frame[-4:])
    if crc != crc32(frame[:-4]):
        raise FrameError("CRC mismatch")
    return seq, cmd, frame[6:-4]


@dataclass
class LinkFaults:
    corrupt_rate: float = 0.0  # probability a frame (request or response) gets one byte flipped
    drop_rate: float = 0.0  # probability a response is lost (timeout)
    disconnect_after_frames: int | None = None  # cable pulled after N frames
    latency_ms: float = 2.0  # simulated per-frame turnaround
    bitrate: float = 500_000.0  # simulated bits per second


class LoopbackLink:
    """In-process link to a virtual ECU device. Tracks simulated time and frame statistics."""

    is_loopback = True

    def __init__(self, device, faults: LinkFaults | None = None, seed: int = 1) -> None:
        self.device = device
        self.faults = faults or LinkFaults()
        self.rng = random.Random(seed)
        self.frames = 0
        self.corrupted = 0
        self.dropped = 0
        self.sim_time = 0.0
        self.down = False

    def _maybe_corrupt(self, frame: bytes) -> bytes:
        if self.faults.corrupt_rate and self.rng.random() < self.faults.corrupt_rate:
            self.corrupted += 1
            i = self.rng.randrange(len(frame))
            return frame[:i] + bytes([frame[i] ^ (1 << self.rng.randrange(8))]) + frame[i + 1:]
        return frame

    def transfer(self, frame: bytes) -> bytes:
        if self.down:
            raise LinkDown("link disconnected")
        f = self.faults
        if f.disconnect_after_frames is not None and self.frames >= f.disconnect_after_frames:
            self.down = True
            raise LinkDown("link disconnected")
        self.frames += 1
        self.sim_time += f.latency_ms / 1000.0 + len(frame) * 10 / f.bitrate
        response = self.device.handle(self._maybe_corrupt(frame))
        if response is None:
            raise LinkTimeout("no response from ECU")
        if f.drop_rate and self.rng.random() < f.drop_rate:
            self.dropped += 1
            raise LinkTimeout("response lost")
        self.sim_time += len(response) * 10 / f.bitrate
        return self._maybe_corrupt(response)

    def reconnect(self) -> None:
        self.down = False
        self.faults.disconnect_after_frames = None

    def stats(self) -> dict:
        return {"frames": self.frames, "corrupted": self.corrupted, "dropped": self.dropped,
                "simulated_seconds": round(self.sim_time, 4), "down": self.down}
