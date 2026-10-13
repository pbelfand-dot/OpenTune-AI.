import pytest

from opentune.obd.pids import (
    CHANNEL_TO_PID,
    PIDS,
    decode_monitor_status,
    decode_supported,
    encode_monitor_status,
    encode_supported,
)


@pytest.mark.parametrize(
    ("pid", "data", "expected"),
    [
        (0x0C, [0x1A, 0xF8], 1726.0),  # (256*26 + 248) / 4
        (0x05, [0x7B], 83.0),  # 123 - 40
        (0x0F, [0x28], 0.0),
        (0x06, [0x80], 0.0),  # 128 -> 0 %
        (0x06, [0x00], -100.0),
        (0x07, [0xFF], pytest.approx(99.22, abs=0.01)),
        (0x0E, [0x80], 0.0),  # 128/2 - 64
        (0x0E, [0x00], -64.0),
        (0x10, [0x01, 0x00], 2.56),
        (0x11, [0xFF], 100.0),
        (0x04, [0x00], 0.0),
        (0x0B, [0xFF], 255.0),
        (0x0D, [0x64], 100.0),
        (0x1F, [0x01, 0x00], 256.0),
        (0x23, [0x01, 0x00], 2560.0),  # 10 * 256 kPa
        (0x33, [0x65], 101.0),
        (0x42, [0x36, 0xB0], 14.0),  # 14000 mV
        (0x44, [0x80, 0x00], 1.0),  # 2/65536 * 32768
        (0x5C, [0x82], 90.0),
        (0x5E, [0x00, 0x64], 5.0),
        (0x0A, [0x64], 300.0),
    ],
)
def test_decode_formulas(pid, data, expected):
    assert PIDS[pid].decode(data) == expected


def test_rail_pressure_channel_is_bar():
    # 0x23 decodes to kPa per SAE; the OpenTune channel is bar.
    assert PIDS[0x23].to_channel([0x0D, 0xAC]) == pytest.approx(350.0)  # 3500 * 10 kPa


@pytest.mark.parametrize("pid", sorted(PIDS))
def test_encode_decode_round_trip(pid):
    definition = PIDS[pid]
    # Pick a value comfortably inside each PID's range.
    samples = {"%": 42.0, "°C": 87.0, "kPa": 180.0, "rpm": 3456.25, "km/h": 88.0, "° before TDC": 12.5,
               "g/s": 123.45, "s": 1200.0, "km": 77.0, "V": 14.123, "λ": 0.85, "L/h": 12.35}
    value = samples[definition.unit]
    if pid in (0x06, 0x07):
        value = -7.8125  # exactly representable trim
    if pid == 0x23:
        value = 20_000.0
    if pid == 0x0A:
        value = 300.0
    raw = definition.encode(value)
    assert len(raw) == definition.num_bytes
    resolution = {"%": 100 / 255, "°C": 1, "kPa": 10, "rpm": 0.25, "km/h": 1, "° before TDC": 0.5, "g/s": 0.01,
                  "s": 1, "km": 1, "V": 0.001, "λ": 2 / 65536, "L/h": 0.05}[definition.unit]
    assert definition.decode(raw) == pytest.approx(value, abs=resolution)


def test_encode_clamps_out_of_range():
    assert PIDS[0x0B].encode(400.0) == bytes([255])  # MAP saturates at 255 kPa
    assert PIDS[0x05].encode(-80.0) == bytes([0])
    assert PIDS[0x0C].encode(1e9) == bytes([0xFF, 0xFF])


def test_supported_bitmask_matches_published_example():
    # Example response "BE 1F A8 13" from the OBD-II PIDs reference.
    supported = decode_supported(0x00, [0xBE, 0x1F, 0xA8, 0x13])
    assert supported == {0x01, 0x03, 0x04, 0x05, 0x06, 0x07, 0x0C, 0x0D, 0x0E, 0x0F, 0x10, 0x11,
                         0x13, 0x15, 0x1C, 0x1F, 0x20}


def test_supported_bitmask_round_trip():
    pids = {0x21, 0x23, 0x2F, 0x33, 0x40}
    assert decode_supported(0x20, encode_supported(0x20, pids)) == pids


def test_monitor_status():
    status = decode_monitor_status(encode_monitor_status(True, 3))
    assert status.mil_on and status.dtc_count == 3
    status = decode_monitor_status([0x02, 0, 0, 0])
    assert not status.mil_on and status.dtc_count == 2


def test_channel_mapping_is_consistent():
    for channel, pid in CHANNEL_TO_PID.items():
        assert PIDS[pid].channel == channel
