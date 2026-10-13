"""Telemetry channel definitions and samples.

Every channel is stored in a canonical metric unit. ``obd_pid`` records the
SAE J1979 Mode 01 PID the channel can be read from on a real car; channels with
``obd_pid=None`` are only available from the simulator (for example the boost
*target*, which generic OBD-II does not expose).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Channel:
    key: str
    name: str
    unit: str
    minimum: float
    maximum: float
    decimals: int = 0
    category: str = "engine"
    obd_pid: int | None = None
    warn_high: float | None = None
    warn_low: float | None = None
    description: str = ""

    @property
    def simulation_only(self) -> bool:
        return self.obd_pid is None


@dataclass
class Sample:
    """One telemetry frame: seconds since session start plus channel values."""

    timestamp: float
    values: dict[str, float] = field(default_factory=dict)

    def get(self, key: str, default: float | None = None) -> float | None:
        return self.values.get(key, default)


_CHANNEL_LIST: tuple[Channel, ...] = (
    Channel("rpm", "Engine Speed", "rpm", 0, 8000, 0, "engine", 0x0C),
    Channel("vehicle_speed", "Vehicle Speed", "km/h", 0, 300, 0, "vehicle", 0x0D),
    Channel("boost", "Boost (gauge)", "bar", -1.0, 2.0, 2, "air", None,
            description="Manifold pressure minus barometric pressure. Derived from PIDs 0x0B and 0x33 on a real car."),
    Channel("boost_target", "Boost Target", "bar", -1.0, 2.0, 2, "air", None,
            description="Simulator only: generic OBD-II does not expose the boost target."),
    Channel("map_kpa", "Manifold Abs. Pressure", "kPa", 0, 255, 0, "air", 0x0B,
            description="PID 0x0B saturates at 255 kPa absolute (about 22 psi boost at sea level)."),
    Channel("baro_kpa", "Barometric Pressure", "kPa", 70, 110, 1, "air", 0x33),
    Channel("throttle", "Throttle Position", "%", 0, 100, 1, "driver", 0x11),
    Channel("pedal", "Accelerator Pedal", "%", 0, 100, 1, "driver", 0x49),
    Channel("engine_load", "Calculated Load", "%", 0, 100, 1, "engine", 0x04),
    Channel("coolant_temp", "Coolant Temp", "°C", -40, 140, 0, "temperature", 0x05, warn_high=112),
    Channel("oil_temp", "Oil Temp", "°C", -40, 160, 0, "temperature", 0x5C, warn_high=130),
    Channel("intake_air_temp", "Intake Air Temp", "°C", -40, 100, 0, "temperature", 0x0F, warn_high=50),
    Channel("ambient_temp", "Ambient Temp", "°C", -40, 60, 0, "temperature", 0x46),
    Channel("timing_advance", "Timing Advance", "°", -20, 50, 1, "ignition", 0x0E),
    Channel("maf", "Mass Air Flow", "g/s", 0, 450, 1, "air", 0x10),
    Channel("lambda_cmd", "Commanded Lambda", "λ", 0.6, 1.4, 3, "fuel", 0x44),
    Channel("stft", "Short Term Fuel Trim", "%", -25, 25, 1, "fuel", 0x06),
    Channel("ltft", "Long Term Fuel Trim", "%", -25, 25, 1, "fuel", 0x07),
    Channel("fuel_rail_pressure", "Fuel Rail Pressure", "bar", 0, 400, 0, "fuel", 0x23),
    Channel("fuel_rate", "Fuel Rate", "L/h", 0, 160, 1, "fuel", 0x5E),
    Channel("fuel_level", "Fuel Level", "%", 0, 100, 0, "fuel", 0x2F, warn_low=10),
    Channel("battery_voltage", "Control Module Voltage", "V", 8, 16, 2, "electrical", 0x42,
            warn_low=12.0),
    Channel("gear", "Gear", "", 0, 8, 0, "vehicle", None,
            description="Simulator only: gear is not a generic OBD-II PID."),
    Channel("torque_est", "Torque (estimate)", "Nm", 0, 700, 0, "engine", None,
            description="Simulator only: modelled crank torque estimate."),
    Channel("run_time", "Run Time", "s", 0, 65535, 0, "engine", 0x1F),
)

CHANNELS: dict[str, Channel] = {c.key: c for c in _CHANNEL_LIST}


def channel(key: str) -> Channel:
    """Look up a channel definition by key."""
    try:
        return CHANNELS[key]
    except KeyError as exc:
        raise KeyError(f"Unknown telemetry channel: {key!r}") from exc
