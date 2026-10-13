"""Diagnostic trouble code encoding, decoding and descriptions.

Two-byte DTC encoding (SAE J1979 / J2012):
  * byte A bits 7-6: system — 00=P (powertrain), 01=C (chassis), 10=B (body), 11=U (network)
  * byte A bits 5-4: first digit (0-3)
  * byte A bits 3-0, byte B bits 7-4, byte B bits 3-0: remaining three hex digits

Descriptions cover generic SAE codes only. Manufacturer-specific codes
(P1xxx, P3xxx, U3xxx and BMW-internal fault codes) are reported without a
description rather than guessed.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

_SYSTEMS = "PCBU"
_SYSTEM_NAMES = {"P": "Powertrain", "C": "Chassis", "B": "Body", "U": "Network"}
_DTC_RE = re.compile(r"^[PCBU][0-3][0-9A-F]{3}$")

# Generic SAE J2012 descriptions (wording varies slightly between revisions).
DESCRIPTIONS: dict[str, str] = {
    "P0101": "Mass or Volume Air Flow Sensor \"A\" Circuit Range/Performance",
    "P0128": "Coolant Thermostat (Coolant Temperature Below Thermostat Regulating Temperature)",
    "P0171": "System Too Lean (Bank 1)",
    "P0172": "System Too Rich (Bank 1)",
    "P0217": "Engine Coolant Over Temperature Condition",
    "P0234": "Turbocharger/Supercharger \"A\" Overboost Condition",
    "P0299": "Turbocharger/Supercharger \"A\" Underboost Condition",
    "P0300": "Random/Multiple Cylinder Misfire Detected",
    "P0301": "Cylinder 1 Misfire Detected",
    "P0302": "Cylinder 2 Misfire Detected",
    "P0303": "Cylinder 3 Misfire Detected",
    "P0304": "Cylinder 4 Misfire Detected",
    "P0305": "Cylinder 5 Misfire Detected",
    "P0306": "Cylinder 6 Misfire Detected",
    "P0325": "Knock Sensor 1 Circuit (Bank 1 or Single Sensor)",
    "P0335": "Crankshaft Position Sensor \"A\" Circuit",
    "P0420": "Catalyst System Efficiency Below Threshold (Bank 1)",
    "P0430": "Catalyst System Efficiency Below Threshold (Bank 2)",
    "P0507": "Idle Air Control System RPM Higher Than Expected",
    "P0562": "System Voltage Low",
    "P0563": "System Voltage High",
    "P0601": "Internal Control Module Memory Check Sum Error",
}


class DtcFormatError(ValueError):
    pass


def decode_dtc(a: int, b: int) -> str | None:
    """Decode two bytes into a DTC string. Returns None for the 00 00 padding value."""
    if a == 0 and b == 0:
        return None
    system = _SYSTEMS[(a >> 6) & 0x03]
    first = (a >> 4) & 0x03
    return f"{system}{first}{a & 0x0F:X}{(b >> 4) & 0x0F:X}{b & 0x0F:X}"


def encode_dtc(code: str) -> bytes:
    code = code.strip().upper()
    if not _DTC_RE.match(code):
        raise DtcFormatError(f"Invalid DTC {code!r}")
    a = (_SYSTEMS.index(code[0]) << 6) | (int(code[1]) << 4) | int(code[2], 16)
    b = int(code[3:5], 16)
    return bytes((a, b))


def parse_dtc_payload(payload: Sequence[int], is_can: bool) -> list[str]:
    """Parse the bytes following the response mode byte (0x43 / 0x47 / 0x4A).

    On CAN the first byte is the number of DTCs. On legacy protocols there is no
    count byte and unused slots are padded with 00 00.
    """
    data = list(payload)
    if is_can and data:
        count = data[0]
        data = data[1:1 + count * 2]
    codes: list[str] = []
    for i in range(0, len(data) - 1, 2):
        code = decode_dtc(data[i], data[i + 1])
        if code is not None:
            codes.append(code)
    return codes


@dataclass(frozen=True)
class DtcInfo:
    code: str
    description: str
    system: str
    generic: bool

    @property
    def summary(self) -> str:
        return f"{self.code} — {self.description}"


def is_generic(code: str) -> bool:
    """SAE-defined generic ranges: P0xxx, P2xxx, P34xx-P39xx, C0xxx, B0xxx, U0xxx."""
    code = code.upper()
    if code[1] == "0":
        return True
    if code[0] == "P" and code[1] == "2":
        return True
    return code[0] == "P" and code[1] == "3" and code[2] in "456789"


def describe(code: str) -> DtcInfo:
    code = code.strip().upper()
    if not _DTC_RE.match(code):
        raise DtcFormatError(f"Invalid DTC {code!r}")
    generic = is_generic(code)
    if code in DESCRIPTIONS:
        text = DESCRIPTIONS[code]
    elif generic:
        text = "Generic SAE code — description not in OpenTune's built-in table"
    else:
        text = "Manufacturer-specific code — consult BMW service information"
    return DtcInfo(code, text, _SYSTEM_NAMES[code[0]], generic)
