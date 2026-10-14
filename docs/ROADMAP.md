# Roadmap

## Done

* **v0.1** — OBD-II diagnostics and datalogging (ELM327, read-only), simulator, calibration
  library, rule-based analysis with optional local LLM, desktop GUI, simulated ECU
  programming with real writes hard-disabled.
* **v0.2 — Virtual Engine Lab**
  * M1 mean-value engine physics, cycle model, B58-/B48-inspired presets with provenance
  * M2 virtual ECU, editable calibration tables, closed-loop simulation
  * M3 virtual dyno with comparisons, corrections and model-uncertainty bands
  * M4 local FastAPI backend and React/TypeScript browser UI (9 pages)
  * M5 simulated ECU communication, flashing, fault injection and recovery
  * M6 control lab, constrained optimisation with a validation gate, AI analyst, scenarios
    A–G, illustrative sound
  * M7 optional integrations (rusEFI simulator read-only client, engine-sim adapter, MATLAB
    export), Docker, CI, documentation

## Next milestone (proposed): M8 — measured data and model identification

The single most valuable step is replacing assumptions with measurements:

1. **Import measured logs** (OpenTune `.otlog.csv`, generic CSV with a channel mapping) and
   label them `measured` in the provenance system.
2. **Parameter estimation**: fit VE, friction, compressor/turbine and knock coefficients to
   logged data (SciPy least squares with bounds), report residuals and hold-out error.
3. **Model-vs-log overlays** on the dyno and dashboard pages; uncertainty bands that shrink
   as parameters are identified.
4. **Validation report generator**: what was measured, what was fitted, error statistics,
   what remains an assumption.

Good first data source: the user's own read-only OBD-II logs from v0.1 (speed, load, MAP,
IAT, timing, lambda) — limited, but real.

## Later

* Valve-lift (Valvetronic-style) load control in the model; per-cylinder spread; simple
  acoustic intake/exhaust model.
* Gear-by-gear vehicle model and road-load drive cycles; chassis-dyno (wheel) simulation.
* Additional presets (only with sourced specifications).
* Collaborative project export/import (zip of the SQLite project + device file).
* rusEFI integration beyond read-only (e.g. comparing the lab's virtual ECU with rusEFI's
  strategies on the rusEFI *simulator*) — still never on hardware.
* macOS packaging (app bundle) for the lab.

## Explicitly out of scope

Programming real vehicle ECUs, security-access or immobiliser circumvention, checksum or
map-layout reverse engineering of production ECUs, emissions-equipment tampering, and any
"ready to flash" output. These stay out regardless of version.
