"""ECU abstraction, simulated ECU programming environment and write-safety gate."""

from opentune.ecu.interface import EcuError, EcuIdentification, EcuState, EcuTarget
from opentune.ecu.programming import (
    MIN_BATTERY_V,
    ProgrammingResult,
    ProgrammingSession,
    validate_image,
)
from opentune.ecu.registry import EcuModuleInfo, available_modules, get_module, register_module
from opentune.ecu.safety import REAL_ECU_WRITE_ENABLED, EcuWriteBlockedError, guard_write
from opentune.ecu.simulated import SimulatedEcu, SimulatedPowerLoss

__all__ = [
    "MIN_BATTERY_V",
    "REAL_ECU_WRITE_ENABLED",
    "EcuError",
    "EcuIdentification",
    "EcuModuleInfo",
    "EcuState",
    "EcuTarget",
    "EcuWriteBlockedError",
    "ProgrammingResult",
    "ProgrammingSession",
    "SimulatedEcu",
    "SimulatedPowerLoss",
    "available_modules",
    "get_module",
    "guard_write",
    "register_module",
    "validate_image",
]
