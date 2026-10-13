# CLAUDE.md — OpenTune AI

Guidance for AI assistants and human contributors working in this repository.

## Mission

OpenTune AI is a **subscription-free, independent BMW diagnostics and ECU-tuning
development platform** — an open alternative to MHD and bootmod3. It starts with
the **BMW B58** (3.0 L I6) and **B48** (2.0 L I4) engines, runs on **macOS** first,
and is written in **Python + PySide6**.

Version 0.1 is **simulation-first**: every feature works against a built-in engine
simulator, and **real ECU writing is hard-disabled**.

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

## Adding things

- **New engine:** add `engines/<code>.py` with sourced `EngineVariant`s and
  `SimulationParameters`, register it in `engines/__init__.py`, add tests.
- **New PID:** add a `PidDefinition` in `obd/pids.py` with decode **and** encode,
  add it to `EMULATED_PIDS` if the simulator can provide it, add a decode test with
  a known byte example.
- **New analysis rule:** add a method on `Analyzer`, then prove it fires on the
  matching simulator fault and stays silent on healthy runs for **both** engines
  across all scenarios (see `tests/test_analysis.py`).
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
```

On Linux CI/cloud machines set `QT_QPA_PLATFORM=offscreen` and install
`libegl1 libxkbcommon0 libfontconfig1 libdbus-1-3` for Qt.

Conventions: Python ≥ 3.10, `from __future__ import annotations`, dataclasses,
ruff (line length 100), canonical metric units internally, conversion only at
display time. Keep commits focused; run lint + tests before pushing.
