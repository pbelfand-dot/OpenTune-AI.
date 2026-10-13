"""Placeholder package for future BMW-specific ECU communication modules.

Nothing in this package talks to a real BMW ECU in v0.1. Planned work is
listed below so contributors know where modules belong. Rules for anything
added here (also in CLAUDE.md):

* Implement :class:`opentune.ecu.interface.EcuTarget` and register through
  :mod:`opentune.ecu.registry`.
* Never invent security-access algorithms, keys, calibration addresses,
  checksum routines or programming sequences. Every protocol detail must cite
  a public standard or documentation the project is licensed to use.
* Read-only capabilities (identification, DTCs, live data) come first.
  Programming stays blocked by :mod:`opentune.ecu.safety` until a dedicated,
  reviewed release enables it.
"""

PLANNED_MODULES: tuple[dict[str, str], ...] = (
    {
        "name": "ENET diagnostics transport",
        "status": "not started",
        "notes": "F/G-series cars expose diagnostics over Ethernet (ENET cable). Research the "
                 "public ISO 13400 (DoIP) standard before writing any code.",
    },
    {
        "name": "Read-only ECU identification",
        "status": "not started",
        "notes": "Identify DME hardware/software versions from documented sources only. "
                 "Confirm the ECU family per car by part number — do not assume it from the engine code.",
    },
    {
        "name": "Manufacturer-specific DTC descriptions",
        "status": "not started",
        "notes": "Only from licensed or published BMW documentation; never guessed.",
    },
)
