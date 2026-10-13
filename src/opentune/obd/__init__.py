"""Modular OBD-II diagnostics: PIDs, DTCs, ELM327 protocol, transports and an emulator."""

from opentune.obd.client import DtcReport, ObdClient
from opentune.obd.dtc import DtcInfo, decode_dtc, describe, encode_dtc
from opentune.obd.elm327 import Elm327, ElmError, NoDataError
from opentune.obd.transport import SerialTransport, Transport, TransportError, list_serial_ports


def simulated_client(vehicle, realtime: bool = True) -> ObdClient:
    """Return a connected :class:`ObdClient` talking to an emulated ELM327 + ECU."""
    from opentune.obd.emulator import Elm327Emulator

    client = ObdClient(Elm327Emulator(vehicle, realtime=realtime))
    client.connect()
    return client


__all__ = [
    "DtcInfo",
    "DtcReport",
    "Elm327",
    "ElmError",
    "NoDataError",
    "ObdClient",
    "SerialTransport",
    "Transport",
    "TransportError",
    "decode_dtc",
    "describe",
    "encode_dtc",
    "list_serial_ports",
    "simulated_client",
]
