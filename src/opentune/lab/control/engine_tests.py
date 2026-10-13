"""Engine-in-the-loop control experiments using the full closed-loop simulation.

* ``boost_step``        dyno-held speed, pedal step → boost tracking of the ECU's target
* ``idle_disturbance``  idle in neutral, accessory load torque step → speed dip and recovery
* ``torque_tracking``   dyno-held speed, pedal step → delivered vs requested torque
* ``wastegate_step``    open-loop actuator test: wastegate step at WOT → identify a
                        first-order-plus-dead-time boost model and suggest SIMC gains

Gains can be overridden per experiment (applied to a copy of the calibration), so
"what if I change boost_ki?" is answered by simulation instead of guesswork.
All results are simulated.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from opentune.lab.calibration.editor import CalibrationEditor
from opentune.lab.calibration.tables import Calibration
from opentune.lab.control.loop import PlantModel, fit_fopdt, simc_tuning, step_metrics
from opentune.lab.physics.config import EngineConfig
from opentune.lab.sim.loads import DynoLoad, TorqueStepLoad
from opentune.lab.sim.session import Simulation

ENGINE_EXPERIMENTS = ("boost_step", "idle_disturbance", "torque_tracking", "wastegate_step")
GAIN_SCALARS = ("boost_kp", "boost_ki", "boost_kd", "idle_kp", "idle_ki", "idle_kd", "throttle_kp", "throttle_ki",
                "lambda_kp", "lambda_ki", "boost_reserve_kpa", "idle_spark_gain")


@dataclass
class EngineExperiment:
    kind: str = "boost_step"
    rpm: float = 3000.0
    pedal_from: float = 30.0
    pedal_to: float = 100.0
    load_step_nm: float = 25.0
    wastegate_from: float = 20.0
    wastegate_to: float = 10.0
    tau_c: float | None = None  # SIMC closed-loop time constant for suggestions (default max(θ, τ/2))
    duration: float = 5.0
    gains: dict[str, float] = field(default_factory=dict)
    seed: int = 1


def _sim(config: EngineConfig, calibration: Calibration, exp: EngineExperiment) -> Simulation:
    cal = calibration.copy()
    ed = CalibrationEditor(cal)
    for key, value in exp.gains.items():
        if key not in GAIN_SCALARS:
            raise ValueError(f"{key!r} is not an adjustable control gain")
        ed.set_scalar(key, value)
    sim = Simulation(config, cal, seed=exp.seed)
    sim.warm_up()
    if not sim.start_engine():
        raise RuntimeError("Engine did not start")
    sim.run(1.5)
    return sim


def _record(sim: Simulation, seconds: float, rows: list[dict], t0: float, every: float = 0.01) -> None:
    def cb(fr):
        rows.append({"t": round(sim.time - t0, 4), "rpm": fr.plant.rpm, "boost_kpa": fr.plant.p_boost / 1000,
                     "boost_target_kpa": fr.ecu.boost_target_kpa, "map_kpa": fr.plant.p_im / 1000,
                     "map_request_kpa": fr.ecu.map_request_kpa, "wastegate_cmd": fr.ecu.wastegate_cmd,
                     "throttle_cmd": fr.ecu.throttle_cmd, "torque_nm": fr.plant.brake_torque,
                     "torque_request_nm": fr.ecu.torque_request, "idle_target_rpm": fr.ecu.idle_target_rpm,
                     "spark_deg": fr.plant.spark_advance, "pedal": sim.driver.pedal, "turbo_krpm":
                     fr.plant.turbo_rpm / 1000})
    sim.run(seconds, every=every, callback=cb)


def run_engine_experiment(config: EngineConfig, calibration: Calibration, exp: EngineExperiment) -> dict:
    if exp.kind not in ENGINE_EXPERIMENTS:
        raise ValueError(f"Unknown experiment {exp.kind!r}")
    if not (1.0 <= exp.duration <= 20.0 and 1000.0 <= exp.rpm <= config.limits.max_rpm - 300):
        raise ValueError("duration 1–20 s and a speed inside the engine's range required")
    sim = _sim(config, calibration, exp)
    rows: list[dict] = []
    t_step = 1.0
    result: dict = {"kind": exp.kind, "gains": exp.gains, "simulated": True}

    if exp.kind in ("boost_step", "torque_tracking", "wastegate_step"):
        sim.set_load(DynoLoad(target_rpm=exp.rpm))
        sim.driver.pedal = exp.pedal_from if exp.kind != "wastegate_step" else 100.0
        if exp.kind == "wastegate_step":
            sim.ecu.actuator_overrides = {"wastegate_cmd": exp.wastegate_from}
        sim.run(3.0)
        t0 = sim.time
        _record(sim, t_step, rows, t0)
        if exp.kind == "wastegate_step":
            sim.ecu.actuator_overrides = {"wastegate_cmd": exp.wastegate_to}
        else:
            sim.driver.pedal = exp.pedal_to
        _record(sim, exp.duration, rows, t0)
    else:  # idle_disturbance
        load = TorqueStepLoad(step_nm=exp.load_step_nm, t_on=1e9)
        sim.set_load(load)
        sim.run(2.0)
        t0 = sim.time
        _record(sim, t_step, rows, t0)
        load.t_on = load.elapsed
        _record(sim, exp.duration, rows, t0)

    t = np.array([r["t"] for r in rows])
    result["series"] = rows[:: max(1, len(rows) // 1200)]

    def tracking(meas: str, target: str) -> dict:
        y = np.array([r[meas] for r in rows])
        r = np.array([r[target] for r in rows])
        after = t >= t_step
        r_final = float(r[after][-max(5, after.sum() // 10):].mean())
        y0 = float(y[t < t_step][-20:].mean())
        u = np.array([row["wastegate_cmd"] for row in rows])
        m = step_metrics(t, y, r, u, t_step, y0, r_final, band=0.05)
        m["target_final"] = r_final
        m["initial"] = y0
        return m

    if exp.kind == "boost_step":
        result["metrics"] = tracking("boost_kpa", "boost_target_kpa")
        result["metrics"]["max_boost_error_kpa"] = float(max(abs(r["boost_kpa"] - r["boost_target_kpa"])
                                                             for r in rows if r["t"] > t_step + 1.0))
        result["description"] = "Boost pressure tracking after a pedal step (5 % settling band)."
    elif exp.kind == "torque_tracking":
        result["metrics"] = tracking("torque_nm", "torque_request_nm")
        result["description"] = "Delivered brake torque vs ECU torque request after a pedal step (5 % band)."
    elif exp.kind == "idle_disturbance":
        rpm = np.array([r["rpm"] for r in rows])
        target = float(np.mean([r["idle_target_rpm"] for r in rows]))
        after = t >= t_step
        dip = float(rpm[after].min() - target)
        outside = np.abs(rpm[after] - target) > 25.0
        idx = np.nonzero(outside)[0]
        recovery = float(t[after][idx[-1]] - t_step) if idx.size and not outside[-1] else (0.0 if not idx.size
                                                                                            else None)
        result["metrics"] = {"idle_target_rpm": target, "speed_dip_rpm": dip, "recovery_time_s": recovery,
                             "steady_state_error_rpm": float(target - rpm[t >= t[-1] - 1.0].mean()),
                             "stalled": bool(rpm.min() < 300.0)}
        result["description"] = f"Idle speed response to a {exp.load_step_nm:g} N·m load step (±25 rpm band)."
    else:
        y = [r["boost_kpa"] for r in rows]
        fit = fit_fopdt(t, y, exp.wastegate_to - exp.wastegate_from, t_step)
        plant = PlantModel("fopdt", fit["gain"], fit["tau"], fit["delay"])
        # The wastegate→boost path is strongly non-linear, so default to a smoother target than
        # SIMC's "tight" τc = θ.
        tuning = simc_tuning(plant, exp.tau_c if exp.tau_c is not None else max(fit["delay"], 0.5 * fit["tau"]))
        # The ECU's boost PID acts on "% wastegate open" with negative gains on boost error; the
        # calibration stores the magnitudes (see VirtualEcu.reload_calibration).
        suggested, clamped = {}, []
        for key, value in (("boost_kp", abs(tuning["kp"])), ("boost_ki", abs(tuning["ki"])), ("boost_kd", 0.0)):
            sc = calibration.scalars[key]
            v = min(max(value, sc.minimum), sc.maximum)
            if v != value:
                clamped.append(f"{key} {value:.3g} clamped to {v:.3g} (calibration range)")
            suggested[key] = round(v, 4)
        result["identified_model"] = fit
        result["suggested_gains"] = suggested
        result["tuning"] = tuning
        result["notes"] = clamped + ["Local linear model around this operating point only; verify with "
                                     "'boost_step' at several speeds."]
        result["metrics"] = {"boost_change_kpa": fit["gain"] * (exp.wastegate_to - exp.wastegate_from)}
        result["description"] = ("Open-loop wastegate step at full load (simulated actuator test), first-order-plus-"
                                 "dead-time fit and SIMC PI suggestion. Test the suggestion with 'boost_step'.")
    result["final_dtcs"] = sorted(sim.ecu.dtcs)
    return result
