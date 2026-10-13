"""Telemetry sources: where live samples come from.

* :class:`SimulatedSource` — reads the engine simulator directly (fast, all channels,
  including simulator-only ones such as boost target and gear).
* :class:`ObdSource` — polls generic OBD-II PIDs through an :class:`ObdClient`. Works
  with a real ELM327 adapter (read-only) or with the ELM327 emulator.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod

from opentune.core.models import CHANNELS, Sample
from opentune.engines.profile import EngineProfile
from opentune.obd.client import ObdClient
from opentune.obd.emulator import Elm327Emulator
from opentune.obd.transport import SerialTransport
from opentune.simulation.vehicle import SimulatedVehicle

# Channels polled over OBD by default, ordered by importance (fewer = faster refresh).
DEFAULT_OBD_CHANNELS = (
    "rpm", "boost", "throttle", "pedal", "vehicle_speed", "coolant_temp", "oil_temp",
    "intake_air_temp", "timing_advance", "maf", "lambda_cmd", "stft", "ltft",
    "fuel_rail_pressure", "battery_voltage", "engine_load",
)


class TelemetrySource(ABC):
    """Common interface for anything that produces telemetry samples."""

    label = "source"

    def __init__(self, profile: EngineProfile) -> None:
        self.profile = profile

    @property
    def is_simulated(self) -> bool:
        return self.vehicle is not None

    @property
    def vehicle(self) -> SimulatedVehicle | None:
        return None

    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def read(self) -> Sample: ...

    @abstractmethod
    def channels(self) -> list[str]: ...

    @abstractmethod
    def diagnostics_client(self) -> ObdClient:
        """An OBD client for DTC/VIN services on the same vehicle."""


class SimulatedSource(TelemetrySource):
    label = "Simulator"

    def __init__(self, profile: EngineProfile, vehicle: SimulatedVehicle | None = None,
                 realtime: bool = True, fixed_dt: float = 0.05) -> None:
        super().__init__(profile)
        self._vehicle = vehicle or SimulatedVehicle(profile, "city")
        self.realtime = realtime
        self.fixed_dt = fixed_dt
        self._client: ObdClient | None = None

    @property
    def vehicle(self) -> SimulatedVehicle:
        return self._vehicle

    def open(self) -> None:
        self._vehicle.sync()

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def read(self) -> Sample:
        if self.realtime:
            return self._vehicle.sync()
        return self._vehicle.advance(self.fixed_dt)

    def channels(self) -> list[str]:
        return list(CHANNELS)

    def diagnostics_client(self) -> ObdClient:
        if self._client is None:
            self._client = ObdClient(Elm327Emulator(self._vehicle, realtime=False))
            self._client.connect()
        return self._client


class ObdSource(TelemetrySource):
    label = "OBD-II"

    def __init__(self, profile: EngineProfile, client: ObdClient,
                 channels: tuple[str, ...] = DEFAULT_OBD_CHANNELS,
                 vehicle: SimulatedVehicle | None = None) -> None:
        super().__init__(profile)
        self.client = client
        self.requested = channels
        self._available: list[str] = []
        self._start = 0.0
        self._vehicle = vehicle

    @property
    def vehicle(self) -> SimulatedVehicle | None:
        return self._vehicle

    def open(self) -> None:
        if not self.client.connected:
            self.client.connect()
        available = set(self.client.available_channels())
        self._available = [c for c in self.requested if c in available]
        self._start = time.monotonic()

    def close(self) -> None:
        self.client.close()

    def read(self) -> Sample:
        values = self.client.read_channels(self._available)
        return Sample(time.monotonic() - self._start, values)

    def channels(self) -> list[str]:
        return list(self._available)

    def diagnostics_client(self) -> ObdClient:
        return self.client


def create_source(kind: str, profile: EngineProfile, serial_port: str = "",
                  baudrate: int = 38400, scenario: str = "city") -> TelemetrySource:
    """Factory used by the GUI and CLI.

    ``kind``: ``"simulator"`` (direct), ``"simulator-obd"`` (simulator behind the
    ELM327 emulator) or ``"elm327"`` (real adapter on ``serial_port``, read-only).
    """
    if kind == "simulator":
        return SimulatedSource(profile, SimulatedVehicle(profile, scenario))
    if kind == "simulator-obd":
        vehicle = SimulatedVehicle(profile, scenario)
        return ObdSource(profile, ObdClient(Elm327Emulator(vehicle, realtime=True)), vehicle=vehicle)
    if kind == "elm327":
        if not serial_port:
            raise ValueError("A serial port is required for an ELM327 connection")
        source = ObdSource(profile, ObdClient(SerialTransport(serial_port, baudrate)))
        source.label = f"ELM327 ({serial_port})"
        return source
    raise ValueError(f"Unknown connection type {kind!r}")
