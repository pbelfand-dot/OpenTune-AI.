"""ECU write safety gate.

OpenTune AI v0.1 never writes to a real ECU. This is enforced in code, not by
configuration: ``REAL_ECU_WRITE_ENABLED`` is a constant, there is no setting,
environment variable or CLI flag that changes it, and every programming
operation calls :func:`guard_write` first. Only target classes registered via
:func:`register_simulated_target_type` (the in-process simulated ECU) pass.

Enabling real writes in a future version requires, at minimum: a documented
and legally obtained protocol implementation, bench testing on a spare ECU,
recovery procedures, and an explicit code review of this module.
"""

from __future__ import annotations

from typing import Final

REAL_ECU_WRITE_ENABLED: Final[bool] = False

_SIMULATED_TARGET_TYPES: set[type] = set()


class EcuWriteBlockedError(PermissionError):
    """Raised whenever something attempts to program a non-simulated ECU."""


def register_simulated_target_type(cls: type) -> type:
    """Class decorator used only by simulated ECU implementations."""
    _SIMULATED_TARGET_TYPES.add(cls)
    return cls


def is_simulated_target(target: object) -> bool:
    return type(target) in _SIMULATED_TARGET_TYPES and getattr(target, "is_simulated", False) is True


def guard_write(target: object) -> None:
    """Allow programming only for registered simulated targets."""
    if is_simulated_target(target):
        return
    if not REAL_ECU_WRITE_ENABLED:
        raise EcuWriteBlockedError(
            "Real ECU writing is disabled in OpenTune AI v0.1. "
            "Only the built-in simulated ECU can be programmed."
        )
    raise EcuWriteBlockedError("No real-ECU programming backend is implemented.")  # pragma: no cover
