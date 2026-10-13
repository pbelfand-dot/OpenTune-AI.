"""Engine profiles. Add new engines by defining a profile and registering it here."""

from __future__ import annotations

from opentune.engines.b48 import B48
from opentune.engines.b58 import B58
from opentune.engines.profile import EngineProfile, EngineVariant, SimulationParameters

ENGINES: dict[str, EngineProfile] = {p.code: p for p in (B58, B48)}


def get_profile(code: str) -> EngineProfile:
    """Return the engine profile for ``code`` (case-insensitive), e.g. ``"B58"``."""
    try:
        return ENGINES[code.strip().upper()]
    except KeyError as exc:
        known = ", ".join(sorted(ENGINES))
        raise KeyError(f"Unknown engine {code!r}. Supported engines: {known}") from exc


__all__ = ["B48", "B58", "ENGINES", "EngineProfile", "EngineVariant", "SimulationParameters", "get_profile"]
