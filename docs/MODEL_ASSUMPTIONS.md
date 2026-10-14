# Model assumptions

This page lists every number the Virtual Engine Lab uses, with its provenance:

* **documented** — publicly documented specification, source cited in the code
  (`EngineConfig.provenance`);
* **assumption** — engineering assumption or synthetic value chosen by OpenTune.

There are **no measured values** in the lab. Simulation outputs are **simulated**;
explanations that go beyond the data (including any language-model text) are
**speculative**.

## The presets are synthetic

`b58-inspired` and `b48-inspired` are synthetic mean-value models built around a handful of
publicly documented specifications (cylinder count, bore, stroke, compression ratio,
injection type, rail pressure, single twin-scroll turbo, air-to-water intercooler). All
other coefficients (VE curve, turbo maps, knock coefficients, friction, thermal masses, rod
length, firing order, …) are assumptions chosen so the model's full-load output lands near
a published rating:

| Preset | Sanity target (documented) | Source | Result of the closed loop (simulated) |
|---|---|---|---|
| B58-inspired | G20 M340i (US): 285 kW / 500 N·m | BMW Group PressClub USA T0286917EN_US | ≈ 281–283 kW / ≈ 490–497 N·m |
| B48-inspired | G20 330i (US): 190 kW / 400 N·m | BMW Group PressClub USA T0285572EN_US | ≈ 186–188 kW / ≈ 392–396 N·m |

Matching a rating that the coefficients were tuned towards is a **calibration target, not a
validation**. The models are not BMW factory simulations and the calibrations are not
extracted from any BMW DME.

Known disagreements between sources are kept visible rather than resolved by picking a
number: the B48 compression ratio varies by variant (9.5–11.0:1 published; 10.2 used as an
assumption), and the B58 Gen1 vs TU rail pressure differs (200 vs 350 bar; the TU value is
used).

## Engine model parameters (generated from the code)

**geometry**

| Parameter | Unit | B58-inspired | B48-inspired | Provenance |
|---|---|---|---|---|
| Cylinders |  | 6 | 4 | documented |
| Layout |  | inline | inline | documented |
| Bore | mm | 82 | 82 | documented |
| Stroke | mm | 94.6 | 94.6 | documented |
| Connecting rod length | mm | 144.5 | 144.5 | assumption |
| Compression ratio | :1 | 10.2 | 10.2 | B58: documented · B48: assumption |

**valvetrain**

| Parameter | Unit | B58-inspired | B48-inspired | Provenance |
|---|---|---|---|---|
| Intake cam centreline | ° ATDC | 110 | 110 | assumption |
| Exhaust cam centreline | ° BTDC | 110 | 110 | assumption |
| Intake duration | ° | 250 | 250 | assumption |
| Exhaust duration | ° | 250 | 250 | assumption |
| Intake VVT authority | ° | 70 | 70 | assumption |
| Exhaust VVT authority | ° | 55 | 55 | assumption |
| VVT slew rate | °/s | 200 | 200 | assumption |
| Peak volumetric efficiency |  | 0.97 | 0.97 | assumption |
| VE peak speed | rpm | 4200 | 3800 | assumption |
| VE curvature | 1/(krpm)² | 0.012 | 0.012 | assumption |
| Minimum VE |  | 0.7 | 0.7 | assumption |
| VE peak shift per intake advance | rpm/° | -25 | -25 | assumption |
| Scavenging gain |  | 0.15 | 0.15 | assumption |

**intake**

| Parameter | Unit | B58-inspired | B48-inspired | Provenance |
|---|---|---|---|---|
| Air filter restriction | Pa/(kg/s)² | 6000 | 6000 | assumption |
| Throttle bore | mm | 74 | 68 | assumption |
| Throttle closed angle | ° | 7 | 7 | assumption |
| Throttle leak area | mm² | 2 | 2 | assumption |
| Throttle discharge coefficient |  | 0.85 | 0.85 | assumption |
| Throttle actuator time constant | ms | 25 | 25 | assumption |
| Intake manifold volume | L | 4 | 2.8 | assumption |
| Boost volume | L | 6 | 4.5 | assumption |
| Charge-air cooler |  | air_to_water | air_to_water | B58: documented · B48: assumption |
| Charge-air cooler effectiveness |  | 0.8 | 0.8 | assumption |
| Charge-air cooler restriction | Pa/(kg/s)² | 1.2e+04 | 1.2e+04 | assumption |
| Charge-cooler loop heat capacity | kJ/K | 18 | 18 | assumption |
| Charge-cooler loop UA (airflow) | W/K | 260 | 260 | assumption |
| Charge-cooler loop UA (still air) | W/K | 60 | 60 | assumption |

**turbo**

| Parameter | Unit | B58-inspired | B48-inspired | Provenance |
|---|---|---|---|---|
| Turbocharged |  | yes | yes | documented |
| Compressor wheel diameter | mm | 50 | 44 | assumption |
| Compressor head at zero flow |  | 1 | 1 | assumption |
| Compressor head slope |  | 4.3 | 4.3 | assumption |
| Compressor choke flow parameter |  | 0.36 | 0.36 | assumption |
| Compressor peak efficiency |  | 0.76 | 0.76 | assumption |
| Compressor optimum flow parameter |  | 0.2 | 0.2 | assumption |
| Compressor efficiency curvature |  | 4 | 4 | assumption |
| Turbine wheel diameter | mm | 45 | 40 | assumption |
| Turbine effective area | cm² | 4.7 | 2.9 | assumption |
| Turbine peak efficiency |  | 0.7 | 0.7 | assumption |
| Turbine optimum blade speed ratio |  | 0.7 | 0.7 | assumption |
| Wastegate max area | cm² | 16 | 12 | assumption |
| Wastegate actuator time constant | ms | 60 | 60 | assumption |
| Turbo rotor inertia | kg·cm² | 0.25 | 0.18 | assumption |
| Turbo mechanical efficiency |  | 0.95 | 0.95 | assumption |
| Twin-scroll turbine |  | yes | yes | documented |

**exhaust**

| Parameter | Unit | B58-inspired | B48-inspired | Provenance |
|---|---|---|---|---|
| Exhaust system restriction | Pa/(kg/s)² | 7e+04 | 7e+04 | assumption |
| EGT sensor time constant | s | 0.6 | 0.6 | assumption |

**fuel_system**

| Parameter | Unit | B58-inspired | B48-inspired | Provenance |
|---|---|---|---|---|
| Fuel |  | gasoline_e0 | gasoline_e0 | assumption |
| Injection |  | direct | direct | documented |
| Injector static flow | g/s | 10.5 | 10.5 | assumption |
| Injector reference pressure | bar | 200 | 200 | assumption |
| Injector dead time | ms | 0.25 | 0.25 | assumption |
| Max rail pressure | bar | 350 | 350 | B58: documented · B48: assumption |
| Idle rail pressure | bar | 50 | 50 | assumption |

**combustion**

| Parameter | Unit | B58-inspired | B48-inspired | Provenance |
|---|---|---|---|---|
| Fuel octane | AKI | 93 | 93 | assumption |
| Indicated efficiency factor |  | 0.76 | 0.76 | assumption |
| Combustion efficiency |  | 0.98 | 0.98 | assumption |
| Spark efficiency curvature | 1/deg² | 0.0003 | 0.0003 | assumption |
| Burn duration (reference) | ° | 58 | 58 | assumption |
| Burn duration speed exponent |  | 0.25 | 0.25 | assumption |
| Burn duration load exponent |  | -0.2 | -0.2 | assumption |
| Ignition delay | ° | 9 | 9 | assumption |
| CA50 at MBT | ° ATDC | 8 | 8 | assumption |
| Wiebe a |  | 5 | 5 | assumption |
| Wiebe m |  | 2 | 2 | assumption |
| Lean limit | λ | 1.45 | 1.45 | assumption |
| Residual-gas dilution penalty |  | 0.035 | 0.035 | assumption |
| Knock limit base | ° BTDC | 22 | 22 | assumption |
| Knock limit per octane | °/AKI | 1.2 | 1.2 | assumption |
| Knock limit per bar MAP | °/bar | 6.5 | 6.5 | assumption |
| Knock limit per K IAT | °/K | 0.18 | 0.18 | assumption |
| Knock limit per K coolant | °/K | 0.1 | 0.1 | assumption |
| Knock limit per 1000 rpm | °/krpm | 1.4 | 1.4 | assumption |
| Knock limit per unit enrichment | ° | 12 | 12 | assumption |
| Knock intensity gain | 1/° | 0.25 | 0.25 | assumption |

**friction**

| Parameter | Unit | B58-inspired | B48-inspired | Provenance |
|---|---|---|---|---|
| FMEP constant | bar | 0.4 | 0.4 | assumption |
| FMEP linear | bar/(m/s) | 0.035 | 0.035 | assumption |
| FMEP quadratic | bar/(m/s)² | 0.0012 | 0.0012 | assumption |
| Cold-oil friction increase |  | 0.6 | 0.6 | assumption |
| Accessory torque | N·m | 6 | 6 | assumption |

**thermal**

| Parameter | Unit | B58-inspired | B48-inspired | Provenance |
|---|---|---|---|---|
| Coolant-side heat capacity | kJ/K | 90 | 90 | assumption |
| Oil-side heat capacity | kJ/K | 18 | 18 | assumption |
| Radiator UA (airflow) | W/K | 2600 | 2600 | assumption |
| Radiator UA (fan only) | W/K | 700 | 700 | assumption |
| Thermostat opening | °C | 95 | 95 | assumption |
| Thermostat fully open | °C | 105 | 105 | assumption |
| Wall heat fraction |  | 0.5 | 0.5 | assumption |
| Oil-to-coolant UA | W/K | 160 | 160 | assumption |

**mechanical**

| Parameter | Unit | B58-inspired | B48-inspired | Provenance |
|---|---|---|---|---|
| Rotating inertia | kg·m² | 0.2 | 0.16 | assumption |
| Starter stall torque | N·m | 110 | 110 | assumption |
| Starter free speed | rpm | 350 | 350 | assumption |

**limits**

| Parameter | Unit | B58-inspired | B48-inspired | Provenance |
|---|---|---|---|---|
| Mechanical speed limit | rpm | 7000 | 6500 | assumption |
| EGT limit | °C | 950 | 950 | assumption |
| Turbo speed limit | rpm | 2.3e+05 | 2.3e+05 | assumption |
| Peak cylinder pressure limit | bar | 140 | 140 | assumption |
| Intake temperature limit | °C | 60 | 60 | assumption |
| Coolant limit | °C | 115 | 115 | assumption |
| Oil limit | °C | 140 | 140 | assumption |
| Knock intensity limit |  | 1 | 1 | assumption |
| Compressor outlet limit | °C | 190 | 190 | assumption |

## Fuels

| Fuel | LHV | Stoich. AFR | Density | Provenance | Source |
|---|---|---|---|---|---|
| Gasoline E0 | 43.2 MJ/kg | 14.6 | 745 kg/m³ | documented | Concawe report 13-2014; IEA-AMF; EN 228. Published LHVs span 42.8–44.2 MJ/kg, AFRs 14.57–14.7. |
| Gasoline E10 | 41.5 MJ/kg | 14.0 | 750 kg/m³ | assumption | Blended from E0 and ethanol (calculation); no single authoritative table found. |
| E85 | 29.1 MJ/kg | 9.8 | 780 kg/m³ | documented (octane: assumption) | US DOE Alternative Fuels Data Center. Composition varies seasonally. |

## Synthetic calibration

`calibration/defaults.py` generates every table from the engine model: the VE table from
the model's volumetric efficiency, base spark from min(MBT, knock limit − 2°), boost limits
from the steady-state turbo-matching solver (marched up from atmospheric so they stay on
the branch the turbo can spool into), wastegate feed-forward from the steady solver,
torque-model efficiency and pumping tables from the combustion model, and simple shapes
for lambda, idle, warm-up, knock retard, EGT protection and cam targets. All tables carry
`provenance = "assumption"`. Gains (idle, boost, throttle, lambda) were tuned on the model.

## Virtual ECU and sensors

* Sensor transfer functions (3-bar MAP 0.5–4.5 V, NTC 2.5 kΩ / β 3435 K with 2.49 kΩ
  pull-up, wideband 0.68–1.36 λ) are generic typical characteristics, not those of any
  specific part.
* Fault-detection windows (0.2 / 4.8 V), confirmation times, the 20 % fuel-trim DTC
  threshold, the 2500 rpm underboost-monitor enable speed and all protection thresholds are
  assumptions.
* DTC descriptions are generic SAE J2012 code texts (verified wording only; see
  `obd/dtc.py`). Which conditions set them is OpenTune's own logic.
* The virtual ECU, its OTCI image format and OTVL protocol are fictional; they do not
  model any production ECU, bootloader or security-access scheme.

## Dyno, uncertainty and corrections

* Correction formulas (SAE J1349, DIN 70020, ISO 1585 SI) come from secondary references;
  they have not been checked against the official standard texts.
* Drivetrain-loss examples (10 %, 15 %, 8 % + drag, 11 % + drag) are illustrative.
* Monte Carlo spreads (1 σ) — themselves assumptions:

| Parameter | Meaning | Spread |
|---|---|---|
| `combustion.eta_ig_ch` | gross indicated efficiency factor | ±0.02 (relative) |
| `combustion.spark_efficiency_coeff` | spark-timing loss curvature | ±0.15 (relative) |
| `combustion.knock_base_deg` | knock-limited advance (only used for limits) | ±1.5 (absolute, °) |
| `valvetrain.ve_peak` | peak volumetric efficiency | ±0.025 (relative) |
| `valvetrain.ve_curvature` | VE fall-off away from the peak | ±0.15 (relative) |
| `friction.fmep_a` | friction MEP, constant term | ±0.1 (relative) |
| `friction.fmep_b` | friction MEP, linear term | ±0.1 (relative) |
| `friction.fmep_c` | friction MEP, quadratic term | ±0.1 (relative) |
| `turbo.compressor_eta_max` | peak compressor efficiency | ±0.03 (relative) |
| `turbo.turbine_eta_max` | peak turbine efficiency | ±0.04 (relative) |
| `intake.intercooler_effectiveness` | charge-cooler effectiveness | ±0.05 (relative) |
| `exhaust.backpressure_coeff` | post-turbine back-pressure | ±0.15 (relative) |
| `thermal.wall_heat_fraction` | in-cylinder wall heat loss | ±0.08 (relative) |
| `combustion.dilution_coeff` | residual-gas efficiency penalty | ±0.25 (relative) |
| virtual torque transducer | measurement error | ±0.5 % (relative) |

## Sound

Firing-frequency harmonics follow from engine speed and cylinder count. Harmonic
amplitudes, noise levels, the turbo blade count (6) and the turbo pitch scaling are
artistic choices.
