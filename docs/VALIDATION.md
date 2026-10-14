# Validation and verification

**Bottom line: the Virtual Engine Lab is verified software, not a validated engine model.**
Verification means the code does what the equations and specifications say; it is
extensive and automated. Validation would mean the model predicts a real engine; that has
**not** been done, because no measured data was available. Treat every output as a
simulation of a synthetic engine.

## What is verified (automated, `pytest`)

| Area | Evidence | Tests |
|---|---|---|
| Formulas and units | Every physics formula is checked with `pint` for dimensional consistency and against hand-calculated values (e.g. Otto efficiency 1 − 10.2^−0.3 = 0.5018) | `tests/lab/test_formulas.py` |
| Cycle model vs literature | Crank-angle Wiebe/Woschni model: CA50 at MBT falls in the 3–14° ATDC range reported in the literature | `test_cycle.py` |
| Plant consistency | Mass and energy bookkeeping, steady-state solver agrees with the dynamic plant within ≈ 3 %, deterministic for a seed | `test_plant.py`, `test_physics_config.py` |
| Calibration suite | Multilinear interpolation exact at breakpoints and for linear functions, clamping, range validation, hash stability and tamper detection, undo/redo, axis resampling, version lineage, JSON/CSV/.mat round trips | `test_calibration.py` |
| Virtual ECU | State machine, PID and anti-windup, sensor transfer functions, every injectable circuit fault leaving the valid window and setting the right DTC, limp/substitute strategies | `test_ecu.py` |
| Closed loop | Idle settles on target (±40 rpm), WOT sweep reaches the calibration targets, crank-signal loss stops the engine and sets P0335, frames are JSON-safe | `test_sim.py` |
| Dyno | kW/hp/PS/lb·ft consistency (hp = lb·ft × rpm / 5252.11), correction factors equal 1 at reference conditions and move in the right direction, wheel figures only from a stated loss, determinism, cancel/progress | `test_dyno.py` |
| Flashing | Image integrity (CRC-32 ×2 + SHA-256), incompatible images rejected before any write, power loss during erase/write/activate never bricks the ECU, noisy links recover without executing a command twice, verify catches flash-cell faults, rollback, programmer refuses any non-virtual link | `test_flash.py` |
| Hardware safety | AST scan: no serial/CAN/USB/J2534 libraries or raw device strings in the lab; sockets only in the loopback rusEFI client; no hardware-like API routes; `REAL_ECU_WRITE_ENABLED is False` | `test_hardware_safety.py` |
| Control lab | SIMC formulas; with τc = θ the loop shows ≈ 61° phase margin and ≈ 9.5 dB (≈ 3×) gain margin as reported by Skogestad (2003); FOPDT identification recovers known parameters; windup demonstration | `test_labtools.py` |
| Optimiser | Every constraint satisfied at the reported optimum; min-BSFC beats max-torque on BSFC; results always `unvalidated` | `test_labtools.py` |
| Scenarios A–G | Each scenario's expectations pass on both presets (quick mode in CI) | `test_labtools.py` |
| API and UI | REST/WebSocket behaviour; a headless-Chromium test opens every page and starts the engine (skipped without a browser) | `test_api.py`, `test_ui_e2e.py` |
| Integrations | rusEFI client framing, read-only whitelist (write/burn/execute never sent), loopback-only, CRC errors; engine-sim adapter parsing and "never fake" behaviour; real tools when configured | `test_integrations.py` |

Full suite: ≈ 490 tests, ≈ 1.5 min on one core, with no car, adapter, display or network.
CI runs Linux and macOS on Python 3.10 and 3.12, rebuilds the browser UI and checks the
committed bundle, and builds and smoke-tests the Docker image.

## Cross-checks performed during development (not automated)

* **rusEFI simulator**: the read-only client was run against a rusEFI simulator built from
  source (commit `2670d5b`, October 2026): signature, firmware version and output channels
  (1200 rpm idle) read correctly; output-channel offsets taken from rusEFI's generated
  header at that commit.
* **engine-sim**: the headless build script was run from a clean clone (Ubuntu 24.04,
  Clang 18); the adapter ran engine-sim's default example engine and parsed its output.
* **Browser walk-through**: the 10-step workflow was driven end to end in headless
  Chromium with no console errors.
* **Docker**: image built, self-test passed inside the container with networking disabled.

## What is NOT validated

| Item | Status |
|---|---|
| Engine output vs a real B58/B48 | Not validated. Full-load output was *tuned* to land near published ratings; part-load, transient and thermal behaviour have no reference. |
| Knock model | Empirical trend model with assumed coefficients; the optimiser's knock-margin constraint is therefore unvalidated. |
| Turbo maps | Synthetic speed-line/efficiency shapes, not manufacturer maps. |
| Thermal model | Lumped capacities and UA values are assumptions; heat-soak magnitudes are illustrative. |
| Emissions | Not modelled. |
| Virtual ECU strategies | Generic torque-based structure written for the lab; not a model of BMW DME software. |
| Correction standards | Formulas from secondary references; not checked against the standard texts. |
| Uncertainty bands | Spreads are assumptions; the band is a sensitivity range of the model, not a confidence interval. |
| Sound | Illustrative synthesis only. |

## What validation would require

1. Steady-state engine-dyno maps (torque, airflow, fuel flow, MAP, exhaust pressure and
   temperature, spark, lambda) across speed and load.
2. Cylinder-pressure traces at representative points (burn rates, MBT, knock onset).
3. Turbocharger maps or measured compressor/turbine operating points.
4. Transient tip-in data and thermal soak tests.
5. A documented parameter-estimation procedure and hold-out data.

Until then the lab is a learning, development and software-testing environment. Nothing it
produces — tables, optimiser candidates, dyno numbers — is suitable for a vehicle.
