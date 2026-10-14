# OpenTune AI

**Subscription-free BMW diagnostics and ECU-tuning development platform.**
Starts with the **B58** (3.0 L turbo I6) and **B48** (2.0 L turbo I4). Built with
Python + PySide6, macOS first.

![OpenTune AI dashboard — simulated B58 full-throttle pull](docs/images/dashboard.png)

> **Version 0.1 is simulation-first.** Every feature works against a built-in engine
> simulator. Real cars can be read (live data, trouble codes, VIN) through a
> standard ELM327-compatible adapter. **Writing to a real ECU is disabled in code**
> and cannot be switched on by any setting.

## Features (v0.1)

| Area | What you get |
|---|---|
| **Dashboard** | Dark-themed live gauges (RPM, boost with target marker, speed), 14 readouts, 30-second strip chart, metric/imperial units |
| **Simulator** | B58/B48 engine + 8-speed car model: turbo spool, boost taper, ignition, enrichment, heat soak, shifting. Scenarios: idle, city, cruise, 3rd-gear pulls, track. Inject faults (boost leak, stuck wastegate, overheat, vacuum leak, misfire, knock, weak charging) |
| **OBD-II diagnostics** | Modular stack: SAE J1979 PID decoders, ELM327 protocol layer (multi-frame aware), pyserial transport, and an ELM327 emulator so the real code path is testable without a car |
| **Trouble codes** | Stored, pending and permanent DTCs, MIL status, clear (with confirmation), VIN. Generic SAE descriptions only — manufacturer codes are never guessed |
| **Data logging** | One-click CSV recording of every channel (`*.otlog.csv`) with metadata; opens in Excel/Numbers/pandas |
| **Calibration files** | Content-addressed library with SHA-256 integrity, lineage, byte-level compare, export. Map editor for the simulator's own (fictional) OTSIM calibrations |
| **AI analysis** | Offline rule engine: pull detection, power estimate, under/overboost, possible knock, heat soak, fuel trims, voltage, rough idle → health score + Markdown report. Optional plain-language narrative from a **local** model (Ollama or any OpenAI-compatible local server) |
| **ECU programming (simulated)** | Full workflow against a simulated ECU: safety gate → validation → preflight (voltage, engine off) → automatic backup → transfer → verify → recovery after a simulated power loss |
| **CLI / cloud mode** | `opentune simulate`, `dtc`, `scan`, `analyze`, `cal`, `flash-sim`, `selftest` — no display needed |

No account. No subscription. No cloud services required.

## Virtual Engine Lab (v0.2)

A simulation-only engineering laboratory in the browser: a physics-based engine model, a
virtual ECU you can calibrate, flash (simulated) and test on a virtual dyno — with every
number labelled *documented*, *simulated*, *assumption* or *speculative*.

![Virtual Engine Lab — live dashboard](docs/images/lab/01_dashboard.png)

```bash
pip install -e ".[lab]"
opentune-lab serve --open        # http://127.0.0.1:8765 (local only)
```

| Page | What it does |
|---|---|
| **Dashboard** | Start/crank the virtual engine, pedal, dyno or vehicle load, gauges, live charts, illustrative sound, 10-step workflow |
| **Engine Builder** | ~100 model parameters with units, ranges and provenance (documented + source, or assumption); steady-state full-load preview |
| **ECU Tuning** | Heat-map table editor (regions, smoothing, axis edits), 2D/3D views, undo/redo, versions, baseline diff, JSON/CSV/MATLAB export; simulated flashing with fault injection, rollback and backups |
| **Virtual Dyno** | WOT/partial sweeps, steady points, transients, repeats; ~40 channels; kW/hp/PS; optional corrections; wheel figures from a stated loss; Monte Carlo model-uncertainty bands; comparisons; CSV |
| **Control Systems** | PID loop lab with Bode plots and SIMC tuning, engine-in-the-loop experiments, constrained optimiser with a validation gate |
| **Engine Diagnostics** | Inject sensor faults, see DTCs, freeze frames, limp modes and sensor voltages |
| **AI Engineer** | Evidence-tagged findings; optional local LLM (localhost only) |
| **Test Laboratory** | Scenarios A–G: stock, moderate, aggressive, heat stress, sensor failures, interrupted ECU update, repeatability |
| **Project Library** | Projects, calibration lineage, dyno runs and reports in one local SQLite file |

<p>
<img src="docs/images/lab/03_tuning.png" width="49%" alt="ECU tuning table editor" />
<img src="docs/images/lab/04_dyno.png" width="49%" alt="Virtual dyno" />
<img src="docs/images/lab/07_flash.png" width="49%" alt="Simulated flash log" />
<img src="docs/images/lab/05b_optimise.png" width="49%" alt="Optimiser with validation gate" />
</p>

Headless: `opentune-lab selftest`, `opentune-lab dyno --engine b48-inspired --kind sweep --out results/`,
Docker: `docker build -t opentune-lab . && docker run -p 127.0.0.1:8765:8765 opentune-lab`.

**Read before using the numbers:** the B58-/B48-inspired engines are synthetic models tuned
to land near published ratings — not BMW factory simulations — and the calibrations are
synthetic tables for a fictional virtual ECU, not extracted from any DME. Nothing the lab
produces is a calibration for a vehicle. Details: [User guide](docs/LAB_USER_GUIDE.md) ·
[Architecture](docs/ARCHITECTURE.md) · [Simulation models](docs/SIMULATION_MODELS.md) ·
[Model assumptions](docs/MODEL_ASSUMPTIONS.md) · [Validation](docs/VALIDATION.md) ·
[Integrations (rusEFI, engine-sim, MATLAB)](docs/LAB_INTEGRATIONS.md) ·
[Licensing](docs/LICENSING.md) · [Roadmap](docs/ROADMAP.md).

## Quick start (macOS)

Full instructions: [docs/INSTALL_MACOS.md](docs/INSTALL_MACOS.md).

```bash
git clone https://github.com/pbelfand-dot/OpenTune-AI..git OpenTune-AI
cd OpenTune-AI
./scripts/setup_macos.sh          # creates .venv and installs everything
source .venv/bin/activate
opentune-gui                      # launch the desktop app
```

## Command line (works anywhere, including headless cloud machines)

```bash
opentune info                                               # versions, safety status, engine specs
opentune selftest                                           # end-to-end check, no car needed
opentune simulate --engine B58 --scenario wot_pull --duration 20
opentune simulate --engine B48 --fault boost_leak --log pull.otlog.csv
opentune simulate --via-obd --units imperial                # through the ELM327 emulator
opentune analyze pull.otlog.csv                             # rule-based report
opentune analyze pull.otlog.csv --local-model ollama:llama3.2
opentune dtc --fault overheat --scenario cruise --run-seconds 90 --clear
opentune cal new-stock --engine B58 && opentune cal list
opentune flash-sim --engine B58 --power-loss 40 --recover   # simulated ECU only
opentune scan --list-ports                                  # real adapter (read-only)
opentune scan --port /dev/cu.usbserial-XXXX
```

## Development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
ruff check src tests
pytest -q            # ~490 tests: GUI headless, lab physics/ECU/dyno/flash/API, no car or network
cd web && npm ci && npm run build   # only when changing the browser UI
```

Architecture, rules and conventions for contributors (human or AI) are in
[CLAUDE.md](CLAUDE.md). Verified reference facts and their sources are in
[docs/RESEARCH.md](docs/RESEARCH.md).

## Safety and legal

- OpenTune AI **cannot write to a real ECU**. The simulated programming environments (v0.1
  and the lab's virtual ECU) exist to develop and test workflows safely; the lab has no code
  path to serial, CAN, USB or J2534 hardware, and tests enforce it.
- Clearing DTCs on a real car also resets emissions readiness monitors.
- Modifying engine calibrations can damage engines and may violate emissions laws
  (e.g. the US Clean Air Act tampering provisions). See [docs/RESEARCH.md](docs/RESEARCH.md).
- OpenTune AI is not affiliated with BMW AG, Toyota, MHD, bootmod3 or any adapter maker.
