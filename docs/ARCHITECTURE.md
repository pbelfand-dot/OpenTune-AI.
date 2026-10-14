# Architecture

OpenTune AI has two parts that share one Python package:

1. **v0.1 tools** (`opentune.*` outside `lab/`): OBD-II diagnostics and datalogging, the original
   arcade-grade simulator, calibration library, desktop GUI (PySide6). See the README.
2. **Virtual Engine Lab** (`opentune.lab`, v0.2): a simulation-only engineering laboratory —
   physics engine model, virtual ECU, calibration suite, virtual dyno, simulated flashing,
   control/optimisation tools, AI analyst and a browser UI. Its purpose is to be the training and
   test ground of the **tuning engine** (`lab/tuning/`, see [TUNING_ENGINE.md](TUNING_ENGINE.md)).

The lab can **not** reach vehicle hardware. There is no serial/CAN/USB/J2534 code in it,
its only "ECU" is an in-process object, and automated tests enforce both
(`tests/lab/test_hardware_safety.py`).

## Layers

```
 Browser UI (React + TypeScript, web/ → src/opentune/lab/web/dist, served locally)
        │  REST + WebSocket (127.0.0.1 only by default)
 FastAPI app  opentune.lab.api        app.py · state.py · jobs.py · live.py · routes/*
        │
 tuning/       tuning engine: battery + scorecard (evaluator), actions, response model + experience
               store (knowledge), session loop (repair, search, validation), benchmark/training
        │      uses dyno.run_dyno for every experiment (parallel worker processes)
 Lab services ──────────────────────────────────────────────────────────────────────────
  dyno/         virtual dyno: sweeps, steady, transient, repeat, corrections, uncertainty
  control/      PID loop lab, SIMC tuning, identification, engine-in-the-loop experiments
  optimize/     SciPy constrained optimisation + validation gate
  analysis/     AI analyst (rule-based findings with evidence categories, optional local LLM)
  scenarios/    Test Laboratory A–G
  flash/        OTCI image, OTVL protocol, virtual ECU flash device, programmer
  sound/        illustrative sound synthesis
  storage/      SQLite project library (configs, calibration lineage, runs, reports)
  integrations/ optional: rusEFI simulator client, engine-sim adapter, MATLAB
        │
 Closed loop  sim/session.py  Simulation: plant (1 ms) ↔ sensors ↔ ECU (5 ms) ↔ load
        │
 ecu/          VirtualEcu (torque-based controller, DTCs, limp), SensorBank, PID
 calibration/  Tables, editor (undo/redo), version store, IO, synthetic baseline generator
 physics/      formulas, constants, EngineConfig + presets, mean-value plant, cycle model,
               steady-state solver
 provenance.py measured / documented / simulated / assumption / speculative
```

Rules for dependencies: lower layers never import higher ones. `physics` knows nothing
about the ECU; `ecu` knows nothing about the API; the UI talks only to the API.

## Data flow

```
EngineConfig ─► EnginePlant ◄─── PlantInputs (throttle, wastegate, injection, spark, cams)
                    │ PlantOutputs (truth)                                 ▲
                    ▼                                                      │
               SensorBank (0–5 V, lag, noise, faults) ─► VirtualEcu ───────┘
                                                          ▲      │ reads Calibration
 Load (dyno / vehicle / neutral / step) ──────────────────┘      ▼
                                                     EcuOutputs (state, DTCs, protections)
Frame = plant + ECU + sensors ─► dyno channels / live WebSocket frames / scenario metrics
```

* **Calibration** objects are plain data (tables + scalars, unit- and range-checked,
  SHA-256 content hash). The ECU reads them through `lookup()`; editing happens through
  `CalibrationEditor` (undo/redo); persistence through `CalibrationStore` (SQLite lineage).
* **Simulated flashing** turns a saved calibration into an OTCI image, writes it to the
  project's `VirtualEcuDevice` through the OTVL loopback link, and the live session then
  runs exactly what the device's running bank holds.
* **Jobs** (dyno, uncertainty, flash, control, optimisation, scenarios, tuning, benchmark,
  training) run on a thread pool with progress and cancel; results are stored in the project
  library or the tuning knowledge base. A tuning evaluation fans its battery out to up to four
  worker processes ("spawn" start method, safe in the threaded server).

## Persistence

`<data dir>/lab/` (data dir: `OPENTUNE_DATA_DIR` or the per-user app-data folder):

| Path | Content |
|---|---|
| `lab.sqlite` | projects, calibration versions (lineage, baseline, hash), dyno runs, reports |
| `ecu/<project>.json` | virtual ECU memory (banks, boot control block, logs) |
| `ecu/backups/<project>/*.otci` | read-back backups taken before every flash |
| `tuning.sqlite` | tuning sessions and reports, every experiment (offsets, effects, predictions), mistakes, benchmark history |
| `ai_settings.json` | optional local-model settings |

## Threads and timing

* The live session runs on its own thread: it advances the simulation by 1/rate_hz × speed
  simulated seconds per tick and sleeps to stay real-time; WebSocket subscribers get frames
  via their asyncio queues (slow clients drop frames, never slow the simulation).
* Simulation cost: ~1 ms plant step + 5 ms ECU task; ≈ 10–12× faster than real time on one
  core. A 1500→6700 rpm sweep at 300 rpm/s (17 s simulated) takes ≈ 2 s.
* Everything is deterministic for a given seed (sensor noise uses seeded RNGs).

## Extension points

| Add | Where | Notes |
|---|---|---|
| Engine preset | `physics/presets.py` | cite every documented value in `cfg.provenance`; everything else is an assumption |
| Model parameter | `physics/config.py` + `PARAM_SPECS` | an import-time check fails if a field has no spec |
| Calibration table | `calibration/defaults.py` | generate from the model, mark synthetic; read it in `ecu/controller.py` |
| ECU feature / DTC | `ecu/controller.py` | DTC descriptions only from verified generic codes in `obd/dtc.py` |
| Dyno channel | `dyno/runs.py` `CHANNELS` | |
| Scenario | `scenarios/library.py` | expectations are about model behaviour, never real-engine safety |
| API route | `api/routes/*.py`, register in `api/app.py` or `routes/extra.py` | |
| UI page | `web/src/pages/*.tsx`, add to `App.tsx` | rebuild: `cd web && npm run build` |
| Integration | `integrations/*.py` | must report unavailability, never fake results |
| Tuning action / goal / limit | `tuning/actions.py`, `tuning/evaluator.py` | bounded, explainable; prove no new constraint surprises on the benchmark |
