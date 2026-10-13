"""Provenance labels: where a number comes from.

The lab distinguishes five kinds of information (also used by the AI analyst):

1. ``MEASURED``    — verified real-world measurement (the project ships none yet)
2. ``DOCUMENTED``  — publicly documented specification, with a cited source
3. ``SIMULATED``   — computed by the simulation
4. ``ASSUMPTION``  — engineering assumption or synthetic calibration value
5. ``SPECULATIVE`` — speculative explanation (e.g. an LLM hypothesis)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Provenance(str, Enum):
    MEASURED = "measured"
    DOCUMENTED = "documented"
    SIMULATED = "simulated"
    ASSUMPTION = "assumption"
    SPECULATIVE = "speculative"

    @property
    def label(self) -> str:
        return {
            "measured": "Verified measurement",
            "documented": "Publicly documented specification",
            "simulated": "Simulated calculation",
            "assumption": "Engineering assumption",
            "speculative": "Speculative explanation",
        }[self.value]


@dataclass(frozen=True)
class SourcedValue:
    """A value with its unit, provenance and (for documented values) a source."""

    value: float
    unit: str
    provenance: Provenance
    source: str = ""
    note: str = ""

    def to_dict(self) -> dict:
        return {"value": self.value, "unit": self.unit, "provenance": self.provenance.value,
                "source": self.source, "note": self.note}
