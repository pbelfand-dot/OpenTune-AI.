# Simulation models

Every equation below is implemented in `src/opentune/lab/`. Symbols use SI units
internally (Pa, K, kg, s, m, W, N·m, rad/s); rpm and kPa appear only at the interface.
Where a relation is a modelling choice rather than physics, it says so. Coefficients
are listed in [MODEL_ASSUMPTIONS.md](MODEL_ASSUMPTIONS.md).

## 1. Mean-value engine plant (`physics/plant.py`)

A cycle-averaged, zero-dimensional model in the tradition of Hendricks & Sorenson (SAE
900616, 1990) and Eriksson & Nielsen, *Modeling and Control of Engines and Drivelines*
(Wiley, 2014).

### Geometry and basic relations (`physics/formulas.py`)

* Swept volume per cylinder `V_s = π/4·B²·S`; displacement `V_d = z·V_s`.
* Mean piston speed `S̄_p = 2·S·n` (n in rev/s).
* Otto efficiency `η_otto = 1 − r_c^(1−γ)` with γ = 1.30 (cycle-average value; assumption).
* Torque from mean effective pressure (four-stroke) `T = MEP·V_d / (4π)`; power `P = T·ω`.

All formula functions are unit-checked with `pint` in `tests/lab/test_formulas.py`.

### Air path

State variables: boost pressure `p_b` (compressor outlet → throttle), intake manifold
pressure `p_im`, turbo speed `ω_tc`, engine speed `ω`, actuator positions, temperatures.

* **Control volumes** (isothermal filling/emptying):
  `dp/dt = R·T/V · (ṁ_in − ṁ_out)`.
* **Restrictions** (throttle, turbine, wastegate) — compressible isentropic flow:
  `ṁ = C_d·A·p_up/√(R·T_up) · Ψ(Π)`, with
  `Ψ(Π) = √(2γ/(γ−1)·(Π^(2/γ) − Π^((γ+1)/γ)))`, choked below the critical ratio
  `Π* = (2/(γ+1))^(γ/(γ−1))`. For Π > 0.985 Ψ is linearised to keep the slope finite
  (Eriksson & Nielsen).
* **Throttle area** (Heywood): `A = πD²/4·(1 − cos(θ)/cos(θ₀))` plus a leak area.
* **Cylinder air flow — speed density**: `ṁ_cyl = η_v·p_im/(R·T_im)·V_d·n/2`.
  `η_v = η_v,base(rpm, cam) · f_bp · f_scav` where
  * `η_v,base = VE_peak − c·((rpm − rpm_peak(cam))/1000)²`, floored at VE_min (shape:
    assumption);
  * residual-gas/back-pressure factor `f_bp = (r_c − (p_em/p_im)^(1/γ)) / (r_c − 1)`
    (ideal-cycle residual relation; Heywood);
  * scavenging heuristic `f_scav = 1 + g·(overlap/60°)·clamp((p_im − p_em)/p_im, ±0.3)`
    (assumption).
* **Compressor**: dimensionless speed line `Ψ_c = Ψ_max − k·Φ²` with head parameter
  `Ψ_c = 2·Δh_is/U²`, flow parameter `Φ = ṁ/(ρ₁·A_c·U)`, `U = ω_tc·D_c/2`;
  isentropic enthalpy rise `Δh_is = c_p·T₁·(Π^((γ−1)/γ) − 1)`; efficiency
  `η_c = η_max − a·(Φ − Φ_opt)²`; power `P_c = ṁ·Δh_is/η_c`; outlet temperature
  `T₂ = T₁ + Δh_is/(c_p·η_c)`. Surge is flagged when no positive flow solves the speed line;
  flow is capped at the choke Φ.
* **Charge cooler**: ε-NTU outlet `T_out = T_in − ε·(T_in − T_coolant)`; for the
  air-to-water cooler the coolant loop is a lumped heat capacity with UA to ambient that
  scales with airflow (heat soak). Pressure loss `Δp = k·ṁ²`.
* **Exhaust manifold pressure** is quasi-steady: solve
  `ṁ_turbine(p_em) + ṁ_wastegate(p_em) = ṁ_engine` (Newton with bisection fallback).
* **Turbine**: `ṁ_t` through the effective nozzle area; isentropic drop
  `Δh_t = c_p,e·T_em·(1 − Π_t^((γ_e−1)/γ_e))`; efficiency vs blade-speed ratio
  `BSR = U_t/√(2Δh_t)`: `η_t = η_max·(1 − ((BSR − BSR_opt)/BSR_opt)²)`.
* **Turbo shaft**: `J_tc·ω_tc·dω_tc/dt = η_m·P_t − P_c`.

Numerics: the stiff `(p_b, p_im)` pair uses a linearly implicit (Rosenbrock–Euler)
step with a numerical Jacobian; the rest is explicit Euler at 1 ms.

### Combustion and torque

* Fuel per cycle from injector pulse width; `λ = m_air/(m_fuel·AFR_s)`.
* Burn duration `Δθ_b = Δθ_ref·(rpm/2000)^a·(load/0.5)^b` (assumption, trend from
  Heywood ch. 9).
* **MBT timing** from the Wiebe function: spark such that 50 % mass fraction burned
  (CA50) lands at `CA50_MBT` (8° ATDC): `θ_MBT = y₅₀·Δθ_b + θ_delay − CA50_MBT`.
* **Spark efficiency** `η_spark = 1 − C·(θ − θ_MBT)²` — C = 3.0·10⁻⁴ /deg² was fitted
  to the crank-angle cycle model (section 2), not to an engine.
* **Indicated efficiency**
  `η_i = η_otto · η_ig/ch · η_spark · η_λ · η_dilution` where `η_ig/ch` (fraction of the
  fuel–air Otto efficiency actually reached) is an assumption, `η_λ` is a heuristic
  piecewise curve following the trends in Heywood ch. 15, and
  `η_dilution = 1 − d·max(0, p_em/p_im − 1)` penalises residual dilution at part load
  (heuristic).
* Heat released `Q = m_fuel·LHV·min(1, λ)·η_comb`; gross `IMEP = Q·η_i/V_d`.
* Brake: `BMEP = IMEP − PMEP − FMEP` with `PMEP = p_em − p_im` and
  `FMEP = a + b·S̄_p + c·S̄_p²` (the Chen–Flynn correlation without its peak-pressure term;
  coefficients assumed), increased for cold oil. Accessory torque is subtracted.
* **Exhaust temperature**: energy not converted to work and not lost to the walls heats
  the exhaust: `T_em = T_im + Q·(1 − η_i)·(1 − x_wall)/(m·c_p,e)`;
  `x_wall = x₀·(2000/rpm)^0.15` (assumption).
* **Knock**: empirical knock-limited advance
  `θ_KL = θ₀ + k_oct·(AKI − 91) − k_p·(p_im − 1 bar) − k_T·(T_im − 40 °C)
  − k_cl·max(0, T_cool − 90 °C) + k_n·(rpm − 2000)/1000 + k_λ·max(0, 1 − λ)`.
  Knock intensity grows with advance beyond θ_KL. All coefficients are assumptions;
  this is the least validated part of the model.
* **Peak cylinder pressure** correlation `p_max ≈ p_im·r_c^1.32 + k·IMEP·(1 + s·(θ − θ_MBT))`,
  k = 3.5, s = 0.033, fitted to the cycle model (max error ≈ 23 % vs that model).

### Mechanical and thermal

* Crank dynamics `J·dω/dt = T_brake − T_load` (+ starter torque while cranking).
* Lumped coolant, oil and charge-loop capacities with heat inputs from combustion and
  UA-based rejection (radiator UA depends on airflow and a thermostat ramp).
* Actuators are first-order lags (throttle, wastegate, rail pressure) and rate-limited
  cam phasers that need oil pressure.

## 2. Crank-angle cycle model (`physics/cycle.py`)

Single-zone closed-cycle model used to cross-check the mean-value relations:
slider-crank volume, Wiebe heat release `x_b = 1 − exp(−a·((θ − θ₀)/Δθ)^(m+1))`
(a = 5, m = 2), first-law integration `dp/dθ = ((γ−1)·(dQ/dθ − dQ_w/dθ) − γ·p·dV/dθ)/V`,
Woschni wall heat transfer. `mbt_sweep` finds the spark for maximum work;
`tests/lab/test_cycle.py` checks that the resulting CA50 at MBT falls in the 3–14° ATDC
range reported in the literature.

## 3. Steady-state solver (`physics/steady.py`)

Algebraic steady state of the same sub-models at a given speed and manifold pressure:
fixed-point iteration over `p_em` and `T_im`, compressor operating point from the speed
line, turbine/wastegate split by bisection so that `η_m·P_t = P_c`. Returns
`feasible = False` when the turbo cannot reach the target with the wastegate closed or
the compressor is past choke. ≈ 1–2 ms per point; agrees with the dynamic plant within
≈ 3 % (test). Used by the baseline-calibration generator, the uncertainty Monte Carlo
and the optimiser.

## 4. Sensors (`ecu/sensors.py`)

Analogue sensors map physical values to 0–5 V with typical generic transfer
characteristics (assumptions): 3-bar MAP 20–300 kPa ↔ 0.5–4.5 V; NTC thermistor
(R₂₅ = 2.5 kΩ, β = 3435 K) with a 2.49 kΩ pull-up; wideband λ 0.68–1.36 ↔ 0.5–4.5 V,
saturating at the range ends. First-order lags, Gaussian noise, a lambda transport delay
of about two revolutions + 30 ms, and injectable faults (stuck, open, short to ground or
supply, offset, noise, drift, delay, intermittent). The ECU treats < 0.2 V or > 4.8 V as a
circuit fault.

## 5. Virtual ECU (`ecu/controller.py`)

Torque-based structure, 5 ms task:

1. **State machine** OFF → READY → CRANKING → RUNNING; STALLED; SHUTDOWN.
2. **Torque request**: pedal map × torque-limit table; limited by coolant temperature,
   limp mode and overboost lockout. With the pedal released an **idle speed governor**
   (PID on rpm, feed-forward, output floor at −0.8 × friction+pumping torque so it can ask
   for *less* than zero brake torque) sets the request.
3. **Inverse torque model**: `torque_eff`, `friction_est` and `pmep_est` tables → air charge
   → MAP request through the VE table.
4. **Throttle**: inverse throttle model (feed-forward) + PI on MAP.
5. **Boost**: target = MAP request + reserve, capped by `boost_max`; wastegate = feed-forward
   table + PID (anti-windup), overboost/underboost monitoring.
6. **Fuel**: speed-density air estimate, lambda target table, warm-up enrichment, EGT
   protection (lambda cap), closed-loop PI short-term trim and slow long-term adaptation.
7. **Spark**: base table + IAT correction − knock retard (+ recovery) − idle reserve +
   idle speed term; clamped.
8. **Cams, rail pressure** from tables.
9. **Diagnostics**: circuit monitors with pending → stored DTCs and freeze frames;
   substitutes (alpha-N MAP from throttle angle, default IAT/ECT), limp mode, crank-signal
   loss detection, fuel-trim limits, cam plausibility.

## 6. Virtual dyno (`dyno/`)

Speed-controlled absorber (PI on engine speed) drives the closed loop. Reported brake
torque is crank torque with the engine's own inertia compensated; absorber torque is also
logged. Power `P = T·ω`; hp = W/745.69987, PS = W/735.49875, lb·ft = N·m/1.35582.
Curves are binned by rpm (default 100 rpm). Optional SAE J1349, DIN 70020 and ISO 1585
correction factors (formulas from secondary references, flagged as not applicable to
boost-controlled engines). Wheel figures use a stated loss: `T_wheel = T − (f·T + T₀ + k·rpm)`.

**Uncertainty**: Monte Carlo over assumption parameters only (documented values are never
perturbed), re-solving the steady state at each run point with the ECU's operating point
held fixed, plus a 0.5 % virtual transducer error; one-at-a-time ±1σ sensitivity ranking.

## 7. Control lab, optimisation, sound

* Loop lab: discrete simulation of FOPDT/SOPDT/integrating plants with dead time; PID
  with derivative-on-measurement and conditional-integration anti-windup; metrics
  (10–90 % rise, overshoot, 2 % settling, IAE/ISE/ITAE); continuous-time Bode with gain and
  phase margins; SIMC PI tuning (Skogestad 2003): `K_c = τ/(K(τ_c + θ))`,
  `τ_I = min(τ, 4(τ_c + θ))`; FOPDT identification (Smith two-point + least squares).
* Optimiser: SLSQP (multi-start, finite differences) on the steady-state model; see
  [VALIDATION.md](VALIDATION.md) for why its results are always "unvalidated".
* Sound: additive synthesis at the firing frequency `f = rpm/60 · z/2` and its harmonics,
  load-dependent noise and a scaled turbo blade-pass tone. Illustrative only.

## References

* J. B. Heywood, *Internal Combustion Engine Fundamentals*, 2nd ed., McGraw-Hill, 2018.
* L. Eriksson, L. Nielsen, *Modeling and Control of Engines and Drivelines*, Wiley, 2014.
* E. Hendricks, S. C. Sorenson, "Mean value modelling of spark ignition engines", SAE Technical
  Paper 900616, 1990, doi:10.4271/900616.
* G. Woschni, "A universally applicable equation for the instantaneous heat transfer
  coefficient in the internal combustion engine", SAE 670931, 1967.
* S. Skogestad, "Simple analytic rules for model reduction and PID controller tuning",
  *Journal of Process Control* 13 (2003) 291–309.
