"""Optional adapter for AngeTheGreat's engine-sim (MIT), run as an external headless process.

engine-sim (https://github.com/ange-yaghi/engine-sim) is a real-time
crank-angle-resolved engine simulator with sound synthesis. It ships its own
example engines written in its ``.mr`` scripting language; it has **no** BMW
B58/B48 definition, no turbocharger, no knock model and no ECU, so it
complements rather than replaces the lab's mean-value model.

How it is used here: ``scripts/build_enginesim_headless.sh`` builds a small
headless driver (``third_party/engine-sim-headless``) against pinned engine-sim
sources. Set ``OPENTUNE_ENGINESIM_BIN`` (the driver) and
``OPENTUNE_ENGINESIM_ASSETS`` (engine-sim's ``assets`` folder). This module then
runs the driver as a subprocess and parses its output.

Honesty rules enforced in code:

* If the binary is not configured or fails, an error is raised. The lab never
  substitutes its own model while claiming engine-sim produced a result.
* Every result carries ``source = "engine-sim (external process)"``, the exact
  command line, the binary's SHA-256 and its raw output.
* engine-sim uses unseeded randomness, so repeated runs differ slightly.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

LINE = re.compile(r"^t=(?P<t>[\d.]+)s .*?rpm=(?P<rpm>[-\d.]+) map_kPa=(?P<map>[-\d.]+) afr=(?P<afr>[-\d.]+) "
                  r"dyno_torque_Nm=(?P<tq>[-\d.]+) step_avg_dyno_Nm=(?P<tq_avg>[-\d.]+) "
                  r"dyno_power_kW=(?P<pw>[-\d.]+)")
HEAD = re.compile(r"^engine=(?P<name>.*) cylinders=(?P<cyl>\d+) displacement_L=(?P<disp>[\d.]+) "
                  r"sim_freq=(?P<freq>\d+)")


class EngineSimUnavailable(RuntimeError):
    pass


@dataclass
class EngineSimResult:
    engine_name: str
    cylinders: int
    displacement_l: float
    simulation_frequency_hz: int
    samples: list[dict]
    wav: bytes | None
    command: list[str]
    binary_sha256: str
    stdout: str
    source: str = "engine-sim (external process)"
    notes: list[str] = field(default_factory=lambda: [
        "Produced by engine-sim, not by the OpenTune model. engine-sim's example engine, not a B58/B48.",
        "Dyno torque sign and filtering follow engine-sim's own dynamometer conventions.",
        "engine-sim uses unseeded randomness; repeated runs differ slightly.",
    ])

    def to_dict(self, include_wav: bool = False) -> dict:
        d = asdict(self)
        d["wav"] = None if not include_wav else (self.wav and len(self.wav))
        d["wav_bytes"] = len(self.wav) if self.wav else 0
        return d


def configured() -> tuple[Path | None, Path | None]:
    b = os.environ.get("OPENTUNE_ENGINESIM_BIN")
    a = os.environ.get("OPENTUNE_ENGINESIM_ASSETS")
    binary = Path(b) if b and Path(b).is_file() and os.access(b, os.X_OK) else None
    assets = Path(a) if a and (Path(a) / "main.mr").is_file() else None
    return binary, assets


def parse_output(stdout: str) -> tuple[dict, list[dict]]:
    head: dict = {}
    rows: list[dict] = []
    for line in stdout.splitlines():
        if m := HEAD.match(line):
            head = {"engine_name": m["name"], "cylinders": int(m["cyl"]), "displacement_l": float(m["disp"]),
                    "simulation_frequency_hz": int(m["freq"])}
        elif m := LINE.match(line):
            rows.append({"t": float(m["t"]), "rpm": float(m["rpm"]), "map_kpa": float(m["map"]),
                         "afr": float(m["afr"]), "dyno_torque_nm": float(m["tq"]),
                         "dyno_torque_step_avg_nm": float(m["tq_avg"]), "dyno_power_kw": float(m["pw"])})
    return head, rows


def run(seconds: float = 5.0, throttle: float = 1.0, dyno_rpm: float = 0.0, script: str = "main.mr",
        binary: Path | None = None, assets: Path | None = None, timeout: float = 300.0) -> EngineSimResult:
    """Run the headless driver. Raises :class:`EngineSimUnavailable` instead of ever faking a result."""
    if binary is None or assets is None:
        b, a = configured()
        binary, assets = binary or b, assets or a
    if binary is None or assets is None:
        raise EngineSimUnavailable("engine-sim is not configured (set OPENTUNE_ENGINESIM_BIN and "
                                   "OPENTUNE_ENGINESIM_ASSETS; see scripts/build_enginesim_headless.sh)")
    if not (0.5 <= seconds <= 120.0 and 0.0 <= throttle <= 1.0 and 0.0 <= dyno_rpm <= 12000.0):
        raise ValueError("seconds 0.5–120, throttle 0–1, dyno_rpm 0–12000")
    script_path = (assets / script).resolve()
    if assets.resolve() not in script_path.parents or not script_path.is_file():
        raise ValueError("script must be a .mr file inside the engine-sim assets folder")
    sha = hashlib.sha256(binary.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix="otlab-es-") as tmp:
        # The driver resolves engine-sim's "es/" library relative to its working directory.
        work = Path(tmp) / "bin"
        work.mkdir()
        es_lib = assets.parent / "es"
        if es_lib.is_dir():
            (Path(tmp) / "es").symlink_to(es_lib, target_is_directory=True)
        cmd = [str(binary), str(script_path), f"{seconds:g}", f"{throttle:g}", f"{dyno_rpm:g}"]
        try:
            proc = subprocess.run(cmd, cwd=work, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise EngineSimUnavailable(f"engine-sim failed to run: {exc}") from exc
        if proc.returncode != 0:
            raise EngineSimUnavailable(f"engine-sim exited with {proc.returncode}: {proc.stderr.strip()[-400:]}")
        wav_path = work / "headless_out.wav"
        wav = wav_path.read_bytes() if wav_path.exists() else None
    head, rows = parse_output(proc.stdout)
    if not head or not rows:
        raise EngineSimUnavailable("engine-sim ran but produced no recognisable output")
    return EngineSimResult(head["engine_name"], head["cylinders"], head["displacement_l"],
                           head["simulation_frequency_hz"], rows, wav, cmd, sha, proc.stdout[-20000:])


def status() -> dict:
    b, a = configured()
    return {"id": "engine-sim", "name": "engine-sim (AngeTheGreat, MIT) — optional external process",
            "available": bool(b and a), "binary": str(b) if b else None, "assets": str(a) if a else None,
            "license": "engine-sim, piranha, csv-io, simple-2d-constraint-solver, delta-studio: MIT",
            "detail": "Configured." if b and a else "Not configured: run scripts/build_enginesim_headless.sh and set "
                                                   "OPENTUNE_ENGINESIM_BIN / OPENTUNE_ENGINESIM_ASSETS."}
