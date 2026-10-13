import pytest

from opentune.obd.dtc import (
    DtcFormatError,
    decode_dtc,
    describe,
    encode_dtc,
    is_generic,
    parse_dtc_payload,
)


@pytest.mark.parametrize(
    ("a", "b", "code"),
    [
        (0x01, 0x33, "P0133"),
        (0x03, 0x00, "P0300"),
        (0x02, 0x99, "P0299"),
        (0x41, 0x23, "C0123"),
        (0x81, 0x23, "B0123"),
        (0xC1, 0x00, "U0100"),
        (0x1A, 0xBC, "P1ABC"),
        (0x30, 0x01, "P3001"),
    ],
)
def test_decode(a, b, code):
    assert decode_dtc(a, b) == code
    assert encode_dtc(code) == bytes([a, b])


def test_padding_decodes_to_none():
    assert decode_dtc(0, 0) is None


def test_parse_can_payload_uses_count_byte():
    # 43 | 02 | 01 71 | 03 03 | 00 (padding beyond the count must be ignored)
    assert parse_dtc_payload([0x02, 0x01, 0x71, 0x03, 0x03, 0x00], is_can=True) == ["P0171", "P0303"]
    assert parse_dtc_payload([0x00], is_can=True) == []


def test_parse_legacy_payload_without_count():
    # ELM327 datasheet style: 43 01 33 00 00 00 00
    assert parse_dtc_payload([0x01, 0x33, 0x00, 0x00, 0x00, 0x00], is_can=False) == ["P0133"]


def test_describe_known_generic():
    info = describe("p0299")
    assert info.code == "P0299"
    assert "Underboost" in info.description
    assert info.system == "Powertrain"
    assert info.generic


def test_describe_never_guesses_manufacturer_codes():
    info = describe("P1ABC")
    assert not info.generic
    assert "Manufacturer-specific" in info.description


def test_describe_unknown_generic():
    info = describe("P0999")
    assert info.generic
    assert "not in OpenTune" in info.description


@pytest.mark.parametrize("bad", ["", "P012", "X0123", "P4123", "P01G3"])
def test_invalid_codes(bad):
    with pytest.raises(DtcFormatError):
        encode_dtc(bad)


def test_generic_ranges():
    assert is_generic("P0420") and is_generic("P2187") and is_generic("P3400") and is_generic("U0100")
    assert not is_generic("P1234") and not is_generic("P3000")
