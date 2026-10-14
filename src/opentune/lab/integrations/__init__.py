"""Optional external integrations (rusEFI simulator, engine-sim, MATLAB).

None of them is required. Each reports whether it is available; the lab never
substitutes its own model while claiming an external tool produced a result.
"""

from __future__ import annotations


def integration_status() -> list[dict]:
    from opentune.lab.integrations import enginesim, matlab, rusefi

    return [rusefi.status(), enginesim.status(), matlab.status()]
