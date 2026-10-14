"""Optional MATLAB interoperability (MATLAB is not required and not bundled).

* ``.mat`` export of calibrations is done with SciPy (``calibration.io.export_mat``).
* :func:`m_script` writes a plain MATLAB script that loads that file and builds
  ``griddedInterpolant`` objects (documented, public MATLAB functions only)
  with the same clamped behaviour as the lab: points outside the breakpoints
  are clamped to the table edge.
* If the official "MATLAB Engine API for Python" is installed (it needs a
  licensed MATLAB), :func:`run` can execute a script in it. Nothing in the lab
  depends on it, and no MathWorks software or proprietary interface is copied.
"""

from __future__ import annotations

import importlib.util

from opentune.lab.calibration.tables import Calibration


class MatlabUnavailable(RuntimeError):
    pass


def available() -> bool:
    try:
        return importlib.util.find_spec("matlab.engine") is not None
    except (ImportError, ValueError):
        return False


def m_script(calibration: Calibration, mat_filename: str) -> str:
    lines = [
        "% OpenTune Virtual Engine Lab — SYNTHETIC calibration for the fictional OTVECU-1 virtual ECU.",
        "% Not extracted from any production ECU. Not for use on a vehicle.",
        f"% engine: {calibration.engine_id}   sha256: {calibration.content_hash()}",
        f"S = load('{mat_filename}');",
        "lut = struct();",
    ]
    for t in calibration.tables.values():
        bps = ", ".join(f"S.tbl_{t.id}.bp{i + 1}(:)" for i in range(t.ndim))
        lines.append(f"% {t.name} [{t.unit}] — axes: " + ", ".join(f"{a.name} [{a.unit}]" for a in t.axes))
        if t.ndim == 1:
            lines.append(f"lut.{t.id} = griddedInterpolant({bps}, S.tbl_{t.id}.data(:), 'linear', 'nearest');")
        else:
            lines.append(f"lut.{t.id} = griddedInterpolant({{{bps}}}, S.tbl_{t.id}.data, 'linear', 'nearest');")
    lines += ["% Example: spark advance at 3000 rpm, 80 % load", "% lut.ign_base(3000, 80)", ""]
    return "\n".join(lines)


def run(script: str) -> str:
    """Execute MATLAB code via the MATLAB Engine API for Python (only if installed)."""
    if not available():
        raise MatlabUnavailable("MATLAB Engine API for Python is not installed")
    import io

    import matlab.engine  # type: ignore[import-not-found]

    eng = matlab.engine.start_matlab()
    out = io.StringIO()
    try:
        eng.eval(script, nargout=0, stdout=out)
    finally:
        eng.quit()
    return out.getvalue()


def status() -> dict:
    ok = available()
    return {"id": "matlab", "name": "MATLAB Engine API for Python (optional)", "available": ok,
            "detail": "Detected." if ok else "Not installed — .mat and .m export work without MATLAB.",
            "license": "Proprietary MathWorks product; not bundled, not required."}
