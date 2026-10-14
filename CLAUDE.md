# CLAUDE.md — OpenTune AI

Guidance for AI assistants and human contributors working in this repository.

## Mission

OpenTune AI is a **subscription-free, independent BMW diagnostics and ECU-tuning
development platform** — an open alternative to MHD and bootmod3. It starts with
the **BMW B58** (3.0 L I6) and **B48** (2.0 L I4) engines, runs on **macOS** first,
and is written in **Python + PySide6**.

Version 0.1 is **simulation-first**: every feature works against a built-in engine
simulator, and **real ECU writing is hard-disabled**.

The Virtual Engine Lab (v0.2) exists **to train, test, validate and continuously improve the
tuning engine** (`lab/tuning/`) — it is not a game. Keep the simulator integrated with the tuning
workflow (evaluator battery → actions → response model → session → benchmark), and never treat a
simulated result as proof that a calibration is safe for a real vehicle.

## Non-negotiable rules

1. **Never enable real ECU writing.** `opentune/ecu/safety.py` defines
   `REAL_ECU_WRITE_ENABLED: Final = False`. Do not add settings, environment
   variables, CLI flags or code paths that change it. Every programming operation
   must go through `ProgrammingSession`, which calls `guard_write()` first.
2. **Never invent** ECU security-access algorithms or keys, seed/key values,
   calibration addresses, map layouts, checksum routines or BMW programming
   sequences. If something is not documented in a public standard or a source the
   project may legally use, it does not go in the code. The simulator uses a
   fictional calibration format (OTSIM) and a random "access token" precisely so
   that nothing real is implied.
3. **Never guess DTC descriptions.** Only generic SAE codes with verified wording
   live in `obd/dtc.py`; manufacturer-specific codes are labelled as such.
4. **Verify facts before stating them.** Engine specifications in `engines/` must
   cite a source (`EngineVariant.source`, `EngineProfile.sources`). Where sources
   disagree (e.g. B48 compression ratio, B58 stock boost) the code says so instead
   of picking a number. Simulation parameters (`SimulationParameters`) are
   modelling choices and must never be presented as factory values.
5. **No required paid services or subscriptions.** The AI module talks only to
   local model servers (localhost) by default; cloud endpoints are rejected unless
   `allow_remote=True` is set explicitly in code.
6. **Real-car access is read-only** in v0.1: generic OBD-II Mode 01/03/07/09/0A,
   plus Mode 04 (clear DTCs) behind a confirmation.
7. **Tests must run without a car, an adapter, a display or the network.**
8. **The Virtual Engine Lab (`opentune.lab`) has no path to hardware.** No serial, CAN, USB,
   J2534 or vendor-interface code may be added under `src/opentune/lab/`
   (`tests/lab/test_hardware_safety.py` scans for it). The only programmable ECU is the
   in-process `VirtualEcuDevice`; `Programmer` type-checks its link and device and every
   memory write passes `guard_write()`. The rusEFI client is read-only (`S`, `V`, `O`) and
   loopback-only.
9. **Label provenance.** Every number is *measured*, *documented*, *simulated*, *assumption*
   or *speculative* (`lab/provenance.py`). The lab has no measured data. Presets are
   "B58-/B48-*inspired* synthetic models", never BMW factory simulations; calibration tables
   are synthetic for the fictional OTVECU-1 and never described as extracted from a DME.
   Agreement with a published rating is a calibration target, not validation.
10. **No ready-to-flash output.** Optimiser results stay `unvalidated` with
    `ready_for_vehicle = False`; candidates are for simulation only.
11. **Integrations never fake results.** If rusEFI, engine-sim or MATLAB is unavailable, raise
    or report it; never substitute the lab's own model while claiming an external tool ran.
12. **The tuning engine recommends, it never certifies.** Reports keep `ready_for_vehicle = False`
    and the disclaimer; a candidate must pass the battery (stress margins, knock, DTCs, protection
    modes) and the validation battery; an unsafe starting calibration that cannot be repaired yields
    *no* recommendation. Stress limits are assumptions and must be labelled so. Do not weaken the
    safety screen, the floors or the validation gate to make benchmark numbers look better; new
    actions must be bounded, explainable edits of tuning tables (never protection, idle or
    cold-start functions). `tests/lab/test_tuning.py` checks the tuning package cannot import
    flashing, programming, OBD or serial code.

## Architecture

```
src/opentune/
├── __init__.py          version
├── cli.py               `opentune` command (simulate, dtc, scan, analyze, cal, flash-sim, selftest, gui)
├── config.py            data directory (OPENTUNE_DATA_DIR override) + JSON settings
├── core/                Channel/Sample models (canonical metric units), unit conversion
├── engines/             EngineProfile: published specs (with sources) + simulation parameters
│   ├── b58.py, b48.py
├── simulation/          physics-ish engine/vehicle model, driver scenarios, fault injection,
│                        generic DTC monitors, thread-safe SimulatedVehicle
├── obd/                 modular OBD-II stack
│   ├── pids.py          SAE J1979 Mode 01 decode/encode
│   ├── dtc.py           DTC encode/decode + generic descriptions
│   ├── elm327.py        ELM327 command layer + response parser (multi-frame aware)
│   ├── transport.py     Transport protocol; pyserial SerialTransport
│   ├── emulator.py      ELM327 + ECU emulator over SimulatedVehicle (CAN 11/500)
│   └── client.py        ObdClient: live data, DTCs, VIN (read-only)
├── telemetry/           TelemetrySource: SimulatedSource / ObdSource + factory
├── datalog/             CSV logger and reader (`*.otlog.csv`)
├── calibration/         CalibrationLibrary (content-addressed, SHA-256) + OTSIM simulator format
├── ecu/                 EcuTarget interface, safety gate, simulated ECU, programming session,
│   │                    module registry
│   └── bmw/             placeholder for future BMW-specific modules (no real code yet)
├── analysis/            rule-based analyzer, Markdown report, optional local LLM (Ollama /
│                        OpenAI-compatible) narrative
└── gui/                 PySide6 app: theme, widgets (gauge, tiles, strip chart), controller
                         (worker-thread polling), pages (dashboard, diagnostics, logs,
                         calibrations, analysis, programming, connection)
```

Data flow: `TelemetrySource.read()` → `Sample` (canonical units) → GUI / `DataLogger`
→ `read_log()` → `Analyzer` → `AnalysisReport` → Markdown / local LLM narrative.

Key design points:

- **Core has zero third-party dependencies.** PySide6 is only imported under `gui/`;
  pyserial only inside `SerialTransport`. The CLI simulation mode runs on a bare
  Python install.
- **The emulator exercises the real OBD stack.** `simulator-obd` mode and most OBD
  tests go through `Elm327Emulator`, so parser/decoder bugs show up without hardware.
- **Calibrations drive the simulator.** Boost, ignition, lambda and rev limit come
  from the OTSIM tables, so edit → flash (simulated) → log → analyse is a real loop.
- **GUI threading:** telemetry polling runs in a `QThread` worker; blocking jobs
  (DTC reads, flashing, model calls) use `run_async()` on the global thread pool.
  `SimulatedVehicle` and `Elm327` are lock-protected.

### Virtual Engine Lab (`src/opentune/lab/`, v0.2)

```
lab/
├── provenance.py, units.py
├── physics/       formulas (pint-checked), constants (fuels with sources), EngineConfig + PARAM_SPECS,
│                  presets (b58-inspired, b48-inspired), plant (mean-value model), cycle (Wiebe/Woschni),
│                  steady (turbo-matching steady-state solver)
├── calibration/   Table/Scalar/Calibration (SHA-256), editor (undo/redo), store (SQLite lineage),
│                  io (JSON/CSV/.mat), defaults (synthetic baseline generated from the model)
├── ecu/           VirtualEcu (torque-based controller, DTCs, limp), sensors (0–5 V + faults), PID
├── sim/           Simulation (plant 1 ms ↔ sensors ↔ ECU 5 ms ↔ load), loads
├── dyno/          run_dyno (sweep/partial/steady/transient/repeat), corrections, drivetrain,
│                  uncertainty (Monte Carlo), compare/export
├── flash/         OTCI image, OTVL loopback protocol, VirtualEcuDevice (A/B banks), Programmer
├── control/       loop lab (PID, Bode, SIMC, FOPDT fit), engine-in-the-loop experiments
├── optimize/      SLSQP operating-point optimiser + validation gate
├── analysis/      AI analyst (evidence-tagged findings, optional local LLM)
├── scenarios/     Test Laboratory A–G
├── tuning/        tuning engine: evaluator (battery, stress limits, scorecard, judge), actions,
│                  knowledge (response model, SQLite experience store, caution), session (repair,
│                  search, validation), benchmark (held-out engines, curriculum)
├── sound/         illustrative synthesis
├── storage/       ProjectLibrary (SQLite)
├── integrations/  rusefi (read-only TS client), enginesim (subprocess), matlab (optional)
├── api/           FastAPI app, LabState, JobManager, LiveSession (WebSocket), routes/
├── web/dist/      built browser UI (committed; source in /web, React + TypeScript + Vite)
└── cli.py         `opentune-lab serve|presets|simulate|dyno|tune|train|benchmark|selftest`
```

Docs: `docs/ARCHITECTURE.md`, `SIMULATION_MODELS.md`, `MODEL_ASSUMPTIONS.md`,
`VALIDATION.md`, `LICENSING.md`, `ROADMAP.md`, `LAB_USER_GUIDE.md`, `LAB_INTEGRATIONS.md`,
`TUNING_ENGINE.md`.

Lab conventions: SI units internally; every model coefficient lives in `EngineConfig` with a
`ParamSpec`; documented values carry a source in `cfg.provenance`; seeded randomness only;
results that leave the lab (CSV, reports, API) say they are simulated.

## Adding things

- **New engine:** add `engines/<code>.py` with sourced `EngineVariant`s and
  `SimulationParameters`, register it in `engines/__init__.py`, add tests.
- **New PID:** add a `PidDefinition` in `obd/pids.py` with decode **and** encode,
  add it to `EMULATED_PIDS` if the simulator can provide it, add a decode test with
  a known byte example.
- **New analysis rule:** add a method on `Analyzer`, then prove it fires on the
  matching simulator fault and stays silent on healthy runs for **both** engines
  across all scenarios (see `tests/test_analysis.py`).
- **Lab preset / table / scenario / integration:** see the extension table in
  `docs/ARCHITECTURE.md`. New analysis rules go in `lab/analysis/analyst.py` and must tag every
  evidence item with a provenance kind.
- **Tuning action / goal / stress limit:** actions in `lab/tuning/actions.py` (bounded offsets,
  plus an engineering-prior rule in `knowledge.engineering_rules`), goals in
  `evaluator.GOALS`, limits in `evaluator.StressLimits` (+ `STRESS_KEYS`). Prove on both presets
  that the benchmark has no new constraint surprises (`opentune-lab benchmark --suite quick`).
- **Browser UI:** edit `web/src`, then `cd web && npm ci && npm run build` and commit
  `src/opentune/lab/web/dist` (CI fails if the bundle is stale).
- **Future BMW module:** implement `ecu.interface.EcuTarget`, register via
  `ecu.registry`, read-only capabilities first. Programming stays blocked.

## Development workflow

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
ruff check src tests          # lint
pytest -q                     # full suite (GUI tests run headless)
opentune selftest             # end-to-end check without pytest
opentune simulate --engine B58 --scenario wot_pull --duration 20
opentune-lab selftest         # lab end-to-end check
opentune-lab serve            # browser UI on 127.0.0.1:8765
```

On Linux CI/cloud machines set `QT_QPA_PLATFORM=offscreen` and install
`libegl1 libgl1 libxkbcommon0 libfontconfig1 libdbus-1-3` for Qt.

Conventions: Python ≥ 3.10, `from __future__ import annotations`, dataclasses,
ruff (line length 100), canonical metric units internally, conversion only at
display time. Keep commits focused; run lint + tests before pushing.
