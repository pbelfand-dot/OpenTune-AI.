"""Control-systems laboratory: generic loops, response metrics, frequency response, tuning, identification.

Open-source replacement for the kind of experiments usually done in
MATLAB/Simulink: a discrete-time simulation of a PID loop around a simple plant
(first-order, second-order or integrating, each with dead time), with
feed-forward, actuator saturation and rate limits, sensor noise and delay,
controller sample time, set-point steps and input disturbances.

Tuning suggestions use the SIMC rules (S. Skogestad, "Simple analytic rules for
model reduction and PID controller tuning", Journal of Process Control 13
(2003) 291–309). The first-order-plus-dead-time initial guess uses Smith's
two-point (28 % / 63 %) method before a least-squares refinement.
"""

from __future__ import annotations

import math
import random
from collections import deque
from dataclasses import asdict, dataclass, field

import numpy as np

from opentune.lab.ecu.pid import PID

PLANT_KINDS = ("fopdt", "sopdt", "integrator")


@dataclass
class PlantModel:
    kind: str = "fopdt"
    gain: float = 1.0
    tau: float = 1.0  # s (fopdt)
    delay: float = 0.1  # s
    wn: float = 2.0  # rad/s (sopdt)
    zeta: float = 0.7  # (sopdt)

    def validate(self) -> list[str]:
        p = []
        if self.kind not in PLANT_KINDS:
            p.append(f"Unknown plant {self.kind!r}")
        if self.gain == 0 or not math.isfinite(self.gain):
            p.append("Plant gain must be non-zero")
        if self.tau <= 0 or self.wn <= 0 or self.zeta < 0 or self.delay < 0 or self.delay > 10:
            p.append("τ, ωn must be > 0; ζ ≥ 0; 0 ≤ dead time ≤ 10 s")
        return p

    def response(self, w: np.ndarray) -> np.ndarray:
        s = 1j * w
        dead = np.exp(-s * self.delay)
        if self.kind == "fopdt":
            return self.gain * dead / (1.0 + s * self.tau)
        if self.kind == "sopdt":
            return self.gain * self.wn ** 2 * dead / (s * s + 2 * self.zeta * self.wn * s + self.wn ** 2)
        return self.gain * dead / s


@dataclass
class ControllerConfig:
    kp: float = 1.0
    ki: float = 1.0
    kd: float = 0.0
    derivative_tau: float = 0.02
    feedforward: float = 0.0  # fraction of the static inverse r/K added to the output (0–1)
    out_min: float = -1e9
    out_max: float = 1e9
    rate_limit: float = 0.0  # max |du/dt| (units/s), 0 = none
    sample_time: float = 0.01  # controller period (s)
    anti_windup: bool = True


@dataclass
class Experiment:
    setpoint_initial: float = 0.0
    setpoint_final: float = 1.0
    step_time: float = 0.5
    disturbance: float = 0.0  # input disturbance added to the plant input
    disturbance_time: float = 0.0  # 0 = none
    noise_std: float = 0.0  # measurement noise (1 σ)
    sensor_delay: float = 0.0
    duration: float = 6.0
    dt: float = 0.001
    seed: int = 1


@dataclass
class LoopResult:
    t: list[float]
    setpoint: list[float]
    output: list[float]
    measured: list[float]
    control: list[float]
    metrics: dict
    disturbance_metrics: dict
    frequency: dict | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _plant_step(plant: PlantModel, state: list[float], u: float, dt: float) -> float:
    if plant.kind == "fopdt":
        state[0] += (plant.gain * u - state[0]) * dt / plant.tau
    elif plant.kind == "sopdt":
        y, v = state
        a = plant.wn ** 2 * (plant.gain * u - y) - 2 * plant.zeta * plant.wn * v
        v += a * dt
        y += v * dt
        state[0], state[1] = y, v
    else:
        state[0] += plant.gain * u * dt
    return state[0]


def step_metrics(t: np.ndarray, y: np.ndarray, r: np.ndarray, u: np.ndarray, t_step: float, y0: float,
                 r_final: float, band: float = 0.02, umin: float = -1e9, umax: float = 1e9) -> dict:
    """Rise time (10–90 %), overshoot, 2 % settling time, steady-state error, IAE/ISE/ITAE, effort."""
    mask = t >= t_step
    ts, ys, rs = t[mask] - t_step, y[mask], r[mask]
    span = r_final - y0
    out: dict = {"steady_state_error": None, "rise_time_s": None, "overshoot_pct": None, "settling_time_s": None}
    if ts.size < 3:
        return out
    e = rs - ys
    dt = np.diff(ts, prepend=ts[0])
    out["iae"] = float(np.sum(np.abs(e) * dt))
    out["ise"] = float(np.sum(e * e * dt))
    out["itae"] = float(np.sum(ts * np.abs(e) * dt))
    tail = ys[ts >= ts[-1] * 0.9]
    out["steady_state_error"] = float(r_final - tail.mean())
    us = u[mask]
    out["control_effort"] = float(np.sum(np.abs(np.diff(us))))
    out["saturated_fraction"] = float(np.mean((us >= umax - 1e-9) | (us <= umin + 1e-9)))
    if abs(span) < 1e-12:
        return out
    frac = (ys - y0) / span
    i10 = np.argmax(frac >= 0.1) if np.any(frac >= 0.1) else None
    i90 = np.argmax(frac >= 0.9) if np.any(frac >= 0.9) else None
    if i10 is not None and i90 is not None:
        out["rise_time_s"] = float(ts[i90] - ts[i10])
    out["overshoot_pct"] = float(max(0.0, (frac.max() - 1.0) * 100.0))
    outside = np.abs(ys - r_final) > band * abs(span)
    if not outside[-1]:
        idx = np.nonzero(outside)[0]
        out["settling_time_s"] = float(ts[idx[-1] + 1]) if idx.size else 0.0
    return out


def frequency_response(plant: PlantModel, ctrl: ControllerConfig, w_min: float = 1e-2, w_max: float = 1e3,
                       points: int = 400) -> dict:
    """Open-loop L = C·P (continuous approximation), gain and phase margins."""
    w = np.logspace(math.log10(w_min), math.log10(w_max), points)
    s = 1j * w
    c = ctrl.kp + ctrl.ki / s + ctrl.kd * s / (1.0 + ctrl.derivative_tau * s)
    loop = c * plant.response(w)
    mag = np.abs(loop)
    phase = np.unwrap(np.angle(loop)) * 180.0 / math.pi
    # a stable, sensible loop has a phase near −90…−180° at crossover; shift by 360° if unwrap drifted
    while phase[0] > 0:
        phase -= 360.0
    pm = gm = wc = w180 = None
    cross = np.nonzero((mag[:-1] >= 1.0) & (mag[1:] < 1.0))[0]
    if cross.size:
        i = cross[0]
        wc = float(w[i])
        pm = float(180.0 + phase[i])
    pc = np.nonzero((phase[:-1] > -180.0) & (phase[1:] <= -180.0))[0]
    if pc.size:
        i = pc[0]
        w180 = float(w[i])
        gm = float(-20.0 * math.log10(max(mag[i], 1e-12)))
    return {"w": w.tolist(), "magnitude_db": (20 * np.log10(np.maximum(mag, 1e-12))).tolist(),
            "phase_deg": phase.tolist(), "gain_crossover_rad_s": wc, "phase_margin_deg": pm,
            "phase_crossover_rad_s": w180, "gain_margin_db": gm,
            "note": "Continuous-time approximation; ignores controller sampling, saturation and sensor delay."}


def simulate_loop(plant: PlantModel, ctrl: ControllerConfig, exp: Experiment, frequency: bool = True) -> LoopResult:
    problems = plant.validate()
    if problems:
        raise ValueError("; ".join(problems))
    if not (1e-4 <= exp.dt <= 0.05 and 0.1 <= exp.duration <= 120.0 and exp.dt <= ctrl.sample_time <= 1.0):
        raise ValueError("Need 1e-4 ≤ dt ≤ 0.05 s, 0.1 ≤ duration ≤ 120 s, dt ≤ sample time ≤ 1 s")
    rng = random.Random(exp.seed)
    pid = PID(ctrl.kp, ctrl.ki, ctrl.kd, ctrl.out_min, ctrl.out_max, ctrl.derivative_tau,
              anti_windup=ctrl.anti_windup)
    n = int(round(exp.duration / exp.dt))
    delay_n = int(round(plant.delay / exp.dt))
    sensor_n = int(round(exp.sensor_delay / exp.dt))
    u_hist: deque[float] = deque([0.0] * (delay_n + 1), maxlen=delay_n + 1)
    y_hist: deque[float] = deque([0.0] * (sensor_n + 1), maxlen=sensor_n + 1)
    state = [0.0, 0.0]
    # start at the initial set-point in steady state
    y0 = exp.setpoint_initial
    u_init = y0 / plant.gain if plant.kind != "integrator" else 0.0
    state[0] = y0
    u_hist.extend([u_init] * (delay_n + 1))
    y_hist.extend([y0] * (sensor_n + 1))
    pid.reset(integrator=u_init)
    u = u_init
    period_n = max(1, int(round(ctrl.sample_time / exp.dt)))
    rows_t, rows_r, rows_y, rows_m, rows_u = [], [], [], [], []
    stride = max(1, n // 1500)
    y_meas = y0
    for k in range(n):
        t = k * exp.dt
        r = exp.setpoint_final if t >= exp.step_time else exp.setpoint_initial
        if k % period_n == 0:
            y_meas = y_hist[0] + (rng.gauss(0.0, exp.noise_std) if exp.noise_std else 0.0)
            ff = ctrl.feedforward * r / plant.gain if plant.kind != "integrator" else 0.0
            u_new = pid.update(r, y_meas, period_n * exp.dt, feedforward=ff)
            if ctrl.rate_limit > 0:
                du = ctrl.rate_limit * period_n * exp.dt
                u_new = min(u + du, max(u - du, u_new))
            u = u_new
        d = exp.disturbance if exp.disturbance_time and t >= exp.disturbance_time else 0.0
        u_hist.append(u + d)
        y = _plant_step(plant, state, u_hist[0], exp.dt)
        y_hist.append(y)
        if k % stride == 0:
            rows_t.append(round(t, 5))
            rows_r.append(r)
            rows_y.append(y)
            rows_m.append(y_meas)
            rows_u.append(u)
    t_arr, y_arr, r_arr, u_arr = (np.asarray(rows_t), np.asarray(rows_y), np.asarray(rows_r), np.asarray(rows_u))
    end_mask = t_arr < (exp.disturbance_time if exp.disturbance_time else math.inf)
    metrics = step_metrics(t_arr[end_mask], y_arr[end_mask], r_arr[end_mask], u_arr[end_mask], exp.step_time, y0,
                           exp.setpoint_final, umin=ctrl.out_min, umax=ctrl.out_max)
    dist: dict = {}
    if exp.disturbance_time and exp.disturbance:
        m = t_arr >= exp.disturbance_time
        dev = y_arr[m] - exp.setpoint_final
        dist["peak_deviation"] = float(dev[np.argmax(np.abs(dev))]) if dev.size else 0.0
        outside = np.abs(dev) > 0.02 * max(abs(exp.setpoint_final - exp.setpoint_initial), 1e-9)
        idx = np.nonzero(outside)[0]
        dist["recovery_time_s"] = (float(t_arr[m][idx[-1]] - exp.disturbance_time) if idx.size and not outside[-1]
                                   else (0.0 if not idx.size else None))
    notes = []
    if metrics.get("saturated_fraction", 0) > 0.05:
        notes.append("Actuator saturated for a significant part of the response.")
    if not ctrl.anti_windup and metrics.get("saturated_fraction", 0) > 0.0:
        notes.append("Anti-windup disabled: expect extra overshoot after saturation (integrator windup).")
    freq = frequency_response(plant, ctrl) if frequency else None
    return LoopResult(rows_t, rows_r, [float(v) for v in rows_y], [float(v) for v in rows_m],
                      [float(v) for v in rows_u], metrics, dist, freq, notes)


# ---------------------------------------------------------------------------------------- tuning
def simc_tuning(plant: PlantModel, tau_c: float | None = None) -> dict:
    """SIMC PI settings (Skogestad 2003) for FOPDT or integrating-plus-delay plants."""
    theta = plant.delay
    tc = tau_c if tau_c is not None else max(theta, 1e-3)
    if plant.kind == "fopdt":
        kc = plant.tau / (plant.gain * (tc + theta))
        ti = min(plant.tau, 4.0 * (tc + theta))
    elif plant.kind == "integrator":
        kc = 1.0 / (plant.gain * (tc + theta))
        ti = 4.0 * (tc + theta)
    else:
        raise ValueError("SIMC suggestions implemented for FOPDT and integrating plants")
    return {"kp": kc, "ki": kc / ti, "kd": 0.0, "tau_c": tc, "ti": ti,
            "method": "SIMC PI (Skogestad 2003), τc = θ by default (good robustness/speed trade-off)",
            "provenance": "documented"}


def fit_fopdt(t: list[float] | np.ndarray, y: list[float] | np.ndarray, u_step: float, t_step: float) -> dict:
    """Identify K, τ, θ from an open-loop step response (Smith two-point guess + least squares)."""
    from scipy.optimize import least_squares

    t = np.asarray(t, dtype=float)
    y = np.asarray(y, dtype=float)
    if abs(u_step) < 1e-12:
        raise ValueError("Step size must be non-zero")
    pre = y[t < t_step]
    if pre.size < 2 or np.count_nonzero(t >= t_step) < 10:
        raise ValueError("Need samples before and after the step")
    y0 = float(pre[-max(1, pre.size // 5):].mean())
    post_t, post_y = t[t >= t_step] - t_step, y[t >= t_step] - y0
    y_inf = float(post_y[-max(3, post_y.size // 10):].mean())
    k = y_inf / u_step
    frac = post_y / y_inf if abs(y_inf) > 1e-12 else np.zeros_like(post_y)
    t28 = float(post_t[np.argmax(frac >= 0.283)]) if np.any(frac >= 0.283) else post_t[-1] / 4
    t63 = float(post_t[np.argmax(frac >= 0.632)]) if np.any(frac >= 0.632) else post_t[-1] / 2
    tau0 = max(1.5 * (t63 - t28), 1e-3)
    theta0 = max(t63 - tau0, 0.0)

    def model(p):
        kk, tau, th = p
        return np.where(post_t > th, kk * u_step * (1.0 - np.exp(-(post_t - th) / max(tau, 1e-6))), 0.0)

    res = least_squares(lambda p: model(p) - post_y, [k, tau0, theta0],
                        bounds=([-np.inf, 1e-4, 0.0], [np.inf, 1e3, post_t[-1]]))
    kk, tau, th = (float(v) for v in res.x)
    rmse = float(np.sqrt(np.mean(res.fun ** 2)))
    return {"gain": kk, "tau": tau, "delay": th, "rmse": rmse, "initial_guess": {"gain": k, "tau": tau0,
                                                                                    "delay": theta0},
            "fit_quality": "good" if rmse < 0.05 * abs(y_inf) else "poor",
            "method": "Smith two-point initial guess refined by least squares"}
