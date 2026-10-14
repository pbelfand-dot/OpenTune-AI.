"""Drivetrain loss models: crank power → wheel power.

The virtual dyno measures at the crankshaft (engine dyno). Wheel figures are
only produced when a drivetrain loss model is chosen, and every number it
produces is labelled as derived from that *stated* loss. The built-in examples
are illustrative assumptions, not measurements of any vehicle: real losses
depend on the gearbox, gear, differential, tyres, temperature and the type of
chassis dyno, and the common "flat percentage" rule is itself a simplification.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from opentune.lab.provenance import Provenance


@dataclass
class DrivetrainLoss:
    id: str = "custom"
    name: str = "Custom drivetrain loss"
    fraction: float = 0.0  # proportional loss of crank torque (0–0.5)
    parasitic_nm: float = 0.0  # constant drag torque referred to the crank
    viscous_nm_per_krpm: float = 0.0  # speed-dependent drag referred to the crank
    provenance: str = Provenance.ASSUMPTION.value
    note: str = "Illustrative assumption; not measured."

    def validate(self) -> list[str]:
        problems = []
        if not 0.0 <= self.fraction <= 0.5:
            problems.append("Drivetrain loss fraction must be between 0 and 0.5")
        if self.parasitic_nm < 0 or self.viscous_nm_per_krpm < 0:
            problems.append("Drag terms must be non-negative")
        return problems

    def loss_torque(self, crank_torque_nm: float, rpm: float) -> float:
        """Torque lost between crank and wheels, referred to the crank."""
        if crank_torque_nm <= 0.0:
            return 0.0
        return (self.fraction * crank_torque_nm + self.parasitic_nm + self.viscous_nm_per_krpm * rpm / 1000.0)

    def wheel_torque_crank_ref(self, crank_torque_nm: float, rpm: float) -> float:
        """Wheel torque expressed at crank speed (multiply by the overall ratio for axle torque)."""
        return max(0.0, crank_torque_nm - self.loss_torque(crank_torque_nm, rpm))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> DrivetrainLoss:
        known = {k: d[k] for k in ("id", "name", "fraction", "parasitic_nm", "viscous_nm_per_krpm", "provenance",
                                   "note") if k in d}
        loss = cls(**known)
        problems = loss.validate()
        if problems:
            raise ValueError("; ".join(problems))
        return loss


DRIVETRAIN_EXAMPLES: dict[str, DrivetrainLoss] = {d.id: d for d in (
    DrivetrainLoss("flat_10", "Flat 10 % (illustrative)", 0.10),
    DrivetrainLoss("flat_15", "Flat 15 % (illustrative)", 0.15),
    DrivetrainLoss("mixed_rwd_auto", "RWD automatic, 8 % + drag (illustrative)", 0.08, 8.0, 2.0),
    DrivetrainLoss("mixed_awd_auto", "AWD automatic, 11 % + drag (illustrative)", 0.11, 12.0, 3.0),
)}
