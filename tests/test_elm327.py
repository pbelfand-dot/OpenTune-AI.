import pytest

from opentune.engines import B58
from opentune.obd.client import ObdClient
from opentune.obd.elm327 import Elm327, ElmError, NoDataError, clean_lines, parse_response
from opentune.obd.emulator import Elm327Emulator
from opentune.simulation.vehicle import SimulatedVehicle


class ScriptedTransport:
    """Replays canned adapter replies keyed by command (for non-CAN / edge cases)."""

    def __init__(self, replies: dict[str, str]) -> None:
        self.replies = replies
        self.sent: list[str] = []
        self._next = b""
        self._open = False

    def open(self):
        self._open = True

    def close(self):
        self._open = False

    @property
    def is_open(self):
        return self._open

    def write(self, data: bytes):
        cmd = data.decode().strip()
        self.sent.append(cmd)
        self._next = self.replies.get(cmd, "?").encode() + b"\r\r"

    def read_until_prompt(self, timeout: float) -> bytes:
        out, self._next = self._next, b""
        return out


def test_clean_lines_strips_searching_and_spaces():
    assert clean_lines("SEARCHING...\r41 0C 1A F8\r\r") == ["410C1AF8"]


def test_parse_single_frame():
    assert parse_response("41 0C 1A F8\r") == [bytes.fromhex("410C1AF8")]


def test_parse_multiple_ecus():
    msgs = parse_response("41 00 BE 1F A8 13\r41 00 80 00 00 00\r")
    assert len(msgs) == 2


def test_parse_multiframe_vin():
    raw = "014\r0: 49 02 01 31 44 34\r1: 47 50 30 30 52 35 35\r2: 42 31 32 33 34 35 36\r"
    (msg,) = parse_response(raw)
    assert msg[:3] == bytes([0x49, 0x02, 0x01])
    assert msg[3:].decode() == "1D4GP00R55B123456"
    assert len(msg) == 0x14


@pytest.mark.parametrize("raw", ["NO DATA\r", "SEARCHING...\rNO DATA\r", ""])
def test_no_data(raw):
    with pytest.raises(NoDataError):
        parse_response(raw)


@pytest.mark.parametrize("raw", ["?\r", "UNABLE TO CONNECT\r", "CAN ERROR\r", "BUS INIT: ...ERROR\r", "STOPPED\r",
                                 "41 0C 1\r"])
def test_adapter_errors(raw):
    with pytest.raises(ElmError):
        parse_response(raw)


def test_initialize_against_emulator():
    elm = Elm327(Elm327Emulator(SimulatedVehicle(B58), realtime=False))
    elm.initialize()
    assert elm.version == "ELM327 v1.5"
    assert elm.protocol is not None and elm.protocol.is_can and elm.protocol.number == "6"


def test_emulator_echo_and_spaces_respected():
    emu = Elm327Emulator(SimulatedVehicle(B58), realtime=False)
    emu.open()
    emu.write(b"010C\r")
    reply = emu.read_until_prompt(1).decode()
    assert reply.startswith("010C")  # echo is on after reset
    assert "SEARCHING..." in reply
    emu.write(b"ATE0\r")
    emu.read_until_prompt(1)
    emu.write(b"ATS0\r")
    emu.read_until_prompt(1)
    emu.write(b"010D\r")
    assert emu.read_until_prompt(1).decode().strip() == "410D00"


def test_emulator_unknown_commands():
    elm = Elm327(Elm327Emulator(SimulatedVehicle(B58), realtime=False))
    elm.initialize()
    assert "?" in elm.command("ATXYZ")
    with pytest.raises(NoDataError):
        elm.request(0x01, 0x0A)  # fuel pressure is not emulated
    with pytest.raises(NoDataError):
        elm.request(0x22, 0x01)  # manufacturer services are not emulated


def test_init_failure_is_reported():
    transport = ScriptedTransport({"ATZ": "ELM327 v1.5", "ATE0": "OK", "ATL0": "?"})
    with pytest.raises(ElmError, match="ATL0"):
        Elm327(transport).initialize()


def test_legacy_protocol_dtc_parsing_through_client():
    replies = {
        "ATZ": "ELM327 v1.5", "ATE0": "OK", "ATL0": "OK", "ATS0": "OK", "ATH0": "OK", "ATSP0": "OK",
        "0100": "SEARCHING...\r41 00 80 00 00 00", "ATDPN": "A3",  # ISO 9141-2 (not CAN)
        "0101": "41 01 82 07 E1 00",
        "03": "43 01 33 03 00 00 00",
        "07": "NO DATA",
        "0A": "NO DATA",
    }
    client = ObdClient(ScriptedTransport(replies))
    client.connect()
    assert not client.is_can
    report = client.read_dtcs()
    assert [d.code for d in report.stored] == ["P0133", "P0300"]
    assert report.pending == [] and report.permanent == []
    assert report.mil_on
