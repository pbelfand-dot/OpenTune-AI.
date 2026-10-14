# Virtual Engine Lab — user guide and how-tos

The lab is a simulation-only engineering environment. Nothing in it can talk to a car.

## Install and start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[lab]"            # or ".[dev]" for development (adds tests, GUI, lint)
opentune-lab serve --open           # http://127.0.0.1:8765
```

macOS: see [INSTALL_MACOS.md](INSTALL_MACOS.md) for Python; nothing else is needed (the browser
UI is pre-built). The server listens on 127.0.0.1 only.

Headless / cloud (no browser):

```bash
opentune-lab selftest
opentune-lab simulate --engine b48-inspired --seconds 10 --pedal 40 --dyno-rpm 3000
opentune-lab dyno --engine b58-inspired --kind sweep --uncertainty 32 --out results/
opentune-lab dyno --engine b58-inspired --kind steady --point 2000:30 --point 4500:100
opentune-lab dyno --engine b48-inspired --kind repeat --repeats 3
```

Docker:

```bash
docker build -t opentune-lab .
docker run --rm -p 127.0.0.1:8765:8765 -v opentune-data:/data opentune-lab
```

## The 10-step workflow

The Dashboard shows these steps with a tick when done.

1. **Project.** In *Project Library*, create a project from a preset. The B48-inspired preset
   suits a G20 330i-class engine; the B58-inspired one an M340i-class engine. A synthetic
   baseline calibration (v1) is generated and factory-programmed into the project's virtual ECU.
2. **Engine parameters.** In *Engine Builder*, every parameter shows its unit, range and
   provenance (documented with a source, or assumption). Change something (e.g. compressor
   diameter), validate, save, use *Steady-state preview* to see the effect, then *Regenerate
   baseline* so the tables match the new hardware.
3. **Virtual engine.** On the *Dashboard*: *Start session* → *Crank / start*. Use the pedal and
   the load (neutral free rev, dyno holding a speed, or a one-gear vehicle). Optional
   illustrative sound.
4. **Tables.** In *ECU Tuning → Tables*, pick a table (grouped by category). The grid is a heat
   map; toggle 3D for a surface plot and *Δ vs baseline* to see changes.
5. **Edit.** Drag to select a region and apply set / add / multiply / percent / smooth /
   interpolate; double-click a cell to type a value; click an axis value to move a breakpoint
   (data is re-interpolated). Values outside the table's allowed range are rejected. Undo/redo
   are unlimited within the session.
6. **Save and compare.** *Versions & compare*: save with a note (v2, v3, …), see the diff
   against the baseline, check out older versions, change which version is the baseline,
   export JSON / CSV / MATLAB `.mat` / `.m`, import JSON.
7. **Flash (simulated).** *Virtual ECU (flash)*: flash a saved version. The log shows every
   step (connect, integrity and compatibility checks, backup, programming session, erase,
   write, verify, activate, reset, post-check). Programming is refused while the engine runs.
   Add faults (frame corruption, lost responses, cable pull, power loss during
   erase/write/activate, flash-cell bit flip) to see how the bootloader keeps the ECU
   bootable; use rollback, power cycle, read-back and backup restore.
8. **Dyno.** *Virtual Dyno*: choose the calibration (working copy, active, baseline, or what
   is in the virtual ECU), run sweeps, partial-throttle sweeps, steady points, tip-in
   transients or repeated pulls. Overlay up to eight runs, pick any channels, add a
   model-uncertainty band, compare deltas, export CSV, listen to an illustrative sound.
9. **Experiments.** *Test Laboratory* runs scenarios A–G. *Engine Diagnostics* injects sensor
   faults into the live engine and shows DTCs, freeze frames and sensor voltages. *Control
   Systems* has the PID loop lab, engine-in-the-loop experiments and the optimiser.
10. **Analysis.** *AI Engineer* turns runs and calibration changes into findings, each tagged
    with its evidence category. A local model (optional) can add a narrative, always labelled
    speculative.

## How to …

### …compare two calibrations fairly
Run both on the dyno with the same seed, ambient and test settings. The comparison then
differs only by calibration, which the AI analyst reports as a controlled comparison
(causal *within the model*).

### …test whether a change survives heat
Run *Test Laboratory → D Heat stress* and *G Repeated runs* with the working copy, or a dyno
`repeat` test at 35–40 °C with low cooling airflow.

### …tune a boost controller
*Control Systems → Engine-in-the-loop*: run `wastegate_step` (open-loop actuator test) to
identify a first-order-plus-dead-time model and get SIMC suggestions; try them with
`boost_step`; compare IAE/overshoot against the baseline gains; write the better gains to
the working copy. Note: the wastegate feed-forward table does most of the work during
spool-up, so a small-signal suggestion can be worse for large steps — that is a real result.

### …use the optimiser responsibly
Run it, read which constraints are active and the robustness column, apply the candidate to
the working copy, verify it on the dyno and with scenarios C and D. The result stays
"unvalidated"; see [VALIDATION.md](VALIDATION.md).

### …use a local language model
Install [Ollama](https://ollama.com), `ollama pull llama3.2`, then in *AI Engineer → Local
model* enable it with `http://127.0.0.1:11434`. Only localhost URLs are accepted.

### …add an engine preset
Add a function in `src/opentune/lab/physics/presets.py`. Put every documented value in
`cfg.provenance` with its source; leave the rest as assumptions. Add a `reference_ratings`
entry only from a published source. Register it in `PRESETS`, then check
`opentune-lab selftest` and the closed-loop tests.

### …add a calibration table
Generate it from the model in `calibration/defaults.py` (unit, range, axes, category), read it
in `ecu/controller.py`, add a test. The UI shows new tables automatically.

### …add a scenario
Write a function in `scenarios/library.py` returning a `ScenarioResult` with expectations
about model behaviour (never real-engine safety claims) and register it in `SCENARIOS`.

### …use rusEFI or engine-sim
See [LAB_INTEGRATIONS.md](LAB_INTEGRATIONS.md).

## Where data lives

`<data dir>/lab/`: `lab.sqlite` (projects, versions, runs, reports), `ecu/` (virtual ECU
memory and backups), `ai_settings.json`. Data dir: `$OPENTUNE_DATA_DIR`, or
`~/Library/Application Support/OpenTune AI` on macOS.
