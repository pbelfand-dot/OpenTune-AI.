"""Registry of ECU communication modules.

v0.1 ships only simulated modules. Future BMW-specific modules register here
with their capabilities; anything with ``program`` capability is still subject
to :func:`opentune.ecu.safety.guard_write`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from opentune.ecu.interface import EcuTarget
from opentune.engines import ENGINES


@dataclass(frozen=True)
class EcuModuleInfo:
    key: str
    name: str
    description: str
    engines: tuple[str, ...]
    capabilities: frozenset[str]
    simulated: bool
    factory: Callable[..., EcuTarget]


_REGISTRY: dict[str, EcuModuleInfo] = {}


def register_module(info: EcuModuleInfo) -> None:
    if info.key in _REGISTRY:
        raise ValueError(f"ECU module {info.key!r} already registered")
    _REGISTRY[info.key] = info


def get_module(key: str) -> EcuModuleInfo:
    try:
        return _REGISTRY[key]
    except KeyError as exc:
        raise KeyError(f"Unknown ECU module {key!r}") from exc


def available_modules(engine: str | None = None) -> list[EcuModuleInfo]:
    return [m for m in _REGISTRY.values() if engine is None or engine in m.engines]


def _register_builtin() -> None:
    from opentune.ecu.simulated import SimulatedEcu

    for code, profile in ENGINES.items():
        register_module(EcuModuleInfo(
            key=f"sim-{code.lower()}",
            name=f"Simulated {code} ECU",
            description="In-process simulated ECU using the fictional OTSIM calibration format.",
            engines=(code,),
            capabilities=frozenset({"identify", "read", "program"}),
            simulated=True,
            factory=lambda vehicle=None, _p=profile: SimulatedEcu(_p, vehicle),
        ))


_register_builtin()
