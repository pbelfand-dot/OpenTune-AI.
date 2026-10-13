"""Optional external integrations (rusEFI simulator, engine-sim, MATLAB).

None of them is required. Each reports whether it is available; the lab never
substitutes its own model while claiming an external tool produced a result.
"""

from __future__ import annotations

import importlib.util


def integration_status() -> list[dict]:
    return [
        {"id": "matlab", "name": "MATLAB Engine for Python",
         "available": importlib.util.find_spec("matlab") is not None,
         "detail": "Optional. Calibrations export to .mat without MATLAB."},
    ]
