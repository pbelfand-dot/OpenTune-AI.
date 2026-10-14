"""OTCI — the OpenTune *fictional* calibration image format for the virtual ECU.

This format exists only inside OpenTune. It is not, and does not imitate, any
production ECU's flash layout, container format or checksum scheme.

Layout (all integers big-endian)::

    offset size  field
    0      4     magic "OTCI"
    4      2     format version (1)
    6      2     calibration schema version
    8      16    firmware family, ASCII, NUL padded (e.g. "OTVECU-1")
    24     32    engine id, ASCII, NUL padded
    56     32    SHA-256 of the payload
    88     4     payload length (bytes)
    92     4     image sequence number (set by the programmer, informational)
    96     28    reserved (zero)
    124    4     CRC-32 of bytes 0..123 (header CRC)
    128    n     payload: zlib-compressed canonical calibration JSON
    128+n  4     CRC-32 of header + payload (image CRC)

Integrity is checked three ways: header CRC (fast "is this an image?"),
image CRC (transfer/storage corruption) and payload SHA-256 (content identity,
equal to nothing else in OpenTune but stable for a given payload).
"""

from __future__ import annotations

import hashlib
import json
import struct
import zlib
from dataclasses import dataclass

from opentune.lab.calibration.tables import Calibration, CalibrationError

MAGIC = b"OTCI"
FORMAT_VERSION = 1
HEADER_SIZE = 128
TRAILER_SIZE = 4
_HEADER = struct.Struct(">4sHH16s32s32sII28s")  # 124 bytes, then 4-byte header CRC


class ImageError(ValueError):
    pass


@dataclass
class ImageHeader:
    format_version: int
    schema_version: int
    firmware_family: str
    engine_id: str
    payload_sha256: str
    payload_length: int
    sequence: int

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def _pad(text: str, size: int) -> bytes:
    raw = text.encode("ascii")
    if len(raw) > size:
        raise ImageError(f"Field too long: {text!r}")
    return raw.ljust(size, b"\0")


def build_image(calibration: Calibration, sequence: int = 0) -> bytes:
    """Serialise a calibration into an OTCI image."""
    problems = calibration.validate()
    if problems:
        raise CalibrationError("Refusing to build an image from an invalid calibration: " + "; ".join(problems[:3]))
    payload_json = json.dumps(calibration.to_dict(), sort_keys=True, separators=(",", ":")).encode()
    payload = zlib.compress(payload_json, 9)
    sha = hashlib.sha256(payload).digest()
    head = _HEADER.pack(MAGIC, FORMAT_VERSION, calibration.schema_version, _pad(calibration.firmware_family, 16),
                        _pad(calibration.engine_id, 32), sha, len(payload), sequence, b"\0" * 28)
    head += struct.pack(">I", crc32(head))
    body = head + payload
    return body + struct.pack(">I", crc32(body))


def parse_header(data: bytes) -> ImageHeader:
    if len(data) < HEADER_SIZE:
        raise ImageError("Too short for an OTCI header")
    head = data[:124]
    (magic, fmt, schema, family, engine, sha, length, seq, _res) = _HEADER.unpack(head)
    if magic != MAGIC:
        raise ImageError("Not an OTCI image (bad magic)")
    (hcrc,) = struct.unpack(">I", data[124:128])
    if hcrc != crc32(head):
        raise ImageError("Header CRC mismatch")
    if fmt != FORMAT_VERSION:
        raise ImageError(f"Unsupported image format version {fmt}")
    return ImageHeader(fmt, schema, family.rstrip(b"\0").decode("ascii"), engine.rstrip(b"\0").decode("ascii"),
                       sha.hex(), length, seq)


def image_length(header: ImageHeader) -> int:
    return HEADER_SIZE + header.payload_length + TRAILER_SIZE


def verify_image(data: bytes) -> ImageHeader:
    """Full integrity check (header CRC, image CRC, payload SHA-256). Returns the header."""
    header = parse_header(data)
    total = image_length(header)
    if len(data) < total:
        raise ImageError("Image truncated")
    body = data[: total - TRAILER_SIZE]
    (icrc,) = struct.unpack(">I", data[total - TRAILER_SIZE: total])
    if icrc != crc32(body):
        raise ImageError("Image CRC mismatch")
    payload = data[HEADER_SIZE: HEADER_SIZE + header.payload_length]
    if hashlib.sha256(payload).hexdigest() != header.payload_sha256:
        raise ImageError("Payload SHA-256 mismatch")
    return header


def decode_image(data: bytes) -> Calibration:
    header = verify_image(data)
    payload = data[HEADER_SIZE: HEADER_SIZE + header.payload_length]
    try:
        content = json.loads(zlib.decompress(payload))
    except (zlib.error, ValueError) as exc:
        raise ImageError(f"Payload does not decode: {exc}") from exc
    return Calibration.from_dict(content)  # also verifies the calibration content hash
