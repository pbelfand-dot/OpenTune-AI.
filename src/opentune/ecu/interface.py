"""Abstract ECU target interface.

Future BMW-specific communication modules implement :class:`EcuTarget`. The
programming primitives are only ever invoked through
:class:`opentune.ecu.programming.ProgrammingSession`, which runs the safety
gate and preflight checks first.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum


class EcuState(str, Enum):
    DEFAULT = "default"
    PROGRAMMING = "programming"
    RECOVERY = "recovery"  # application erased/corrupt; needs a recovery flash


@dataclass(frozen=True)
class EcuIdentification:
    target: str
    engine: str
    hardware: str
    software_version: str
    calibration_sha256: str
    vin: str
    state: EcuState
    is_simulated: bool


class EcuError(RuntimeError):
    pass


class EcuTarget(ABC):
    name: str = "ECU"
    is_simulated: bool = False

    @abstractmethod
    def identify(self) -> EcuIdentification: ...

    @abstractmethod
    def read_calibration(self) -> bytes: ...

    @abstractmethod
    def battery_voltage(self) -> float: ...

    @abstractmethod
    def engine_running(self) -> bool: ...

    # --- programming primitives (guarded; call via ProgrammingSession only)
    @abstractmethod
    def enter_programming(self) -> None: ...

    @abstractmethod
    def request_access(self) -> str: ...

    @abstractmethod
    def authorize(self, token: str) -> None: ...

    @abstractmethod
    def erase(self) -> None: ...

    def prepare_transfer(self, size: int) -> None:  # noqa: B027 - optional hook
        """Optional: announce the total image size before blocks are written."""

    @abstractmethod
    def write_block(self, offset: int, data: bytes) -> None: ...

    @abstractmethod
    def finalize(self, expected_sha256: str) -> None: ...

    @abstractmethod
    def exit_programming(self) -> None: ...
