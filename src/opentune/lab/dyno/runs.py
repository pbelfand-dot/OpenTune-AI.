"""Virtual engine dyno: sweeps, partial-throttle sweeps, steady points, transients, repeats.

The dyno drives the closed-loop simulation (engine model + virtual ECU) with a
speed-controlled absorber, exactly like an engine test cell: the absorber holds
or ramps engine speed while the "driver" sets the pedal. Everything it reports
is a *simulated* result of a synthetic model — never a measurement of a real
engine — and every run carries that label and a list of limitations.

Torque convention: ``torque_nm`` is crank brake torque with the engine's own
rotating inertia compensated (what a steady-state engine dyno would read).
``absorber_torque_nm`` is what the absorber actually saw during the ramp
(brake torque minus the torque spent accelerating the crank inertia).
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from opentune.lab.calibration.tables import Calibration
from opentune.lab.dyno.corrections import correction_factor
from opentune.lab.dyno.drivetrain import DrivetrainLoss
from opentune.lab.physics.config import EngineConfig
from opentune.lab.physics.plant import Ambient
from opentune.lab.sim.loads import DynoLoad
from opentune.lab.sim.session import Frame, Simulation
from opentune.lab.units import W_PER_HP, W_PER_PS, nm_to_lbft

TEST_KINDS = ("sweep", "partial", "steady", "transient", "repeat")

Progress = Callable[[float, str], None]
Cancel = Callable[[], bool]


class DynoError(RuntimeError):
    pass


class DynoCancelled(DynoError):
    pass


@dataclass(frozen=True)
class Channel:
    id: str
    label: str
    unit: str
    group: str
    extract: Callable[[Frame], float]


def _k_to_c(t: float) -> float:
    return t - 273.15


CHANNELS: tuple[Channel, ...] = (
    Channel("rpm", "Engine speed", "rpm", "engine", lambda fr: fr.plant.rpm),
    Channel("torque_nm", "Brake torque", "N·m", "output", lambda fr: fr.plant.brake_torque),
    Channel("power_kw", "Brake power", "kW", "output", lambda fr: fr.plant.brake_power / 1000.0),
    Channel("absorber_torque_nm", "Absorber torque", "N·m", "output", lambda fr: fr.load_torque),
    Channel("map_kpa", "Manifold pressure", "kPa abs", "air", lambda fr: fr.plant.p_im / 1000.0),
    Channel("boost_kpa", "Boost pressure (pre-throttle)", "kPa abs", "air", lambda fr: fr.plant.p_boost / 1000.0),
    Channel("boost_target_kpa", "Boost target", "kPa abs", "air", lambda fr: fr.ecu.boost_target_kpa),
    Channel("exhaust_pressure_kpa", "Exhaust manifold pressure", "kPa abs", "air", lambda fr: fr.plant.p_em / 1000.0),
    Channel("throttle_pct", "Throttle position", "%", "air", lambda fr: fr.plant.throttle_pos * 100.0),
    Channel("wastegate_pct", "Wastegate position", "% open", "air", lambda fr: fr.plant.wastegate_pos * 100.0),
    Channel("turbo_krpm", "Turbo speed", "krpm", "air", lambda fr: fr.plant.turbo_rpm / 1000.0),
    Channel("compressor_pr", "Compressor pressure ratio", "–", "air", lambda fr: fr.plant.compressor_pr),
    Channel("compressor_eta_pct", "Compressor efficiency", "%", "air", lambda fr: fr.plant.compressor_eta * 100.0),
    Channel("ve_pct", "Volumetric efficiency", "%", "air", lambda fr: fr.plant.volumetric_efficiency * 100.0),
    Channel("load_pct", "Relative air charge", "%", "air", lambda fr: fr.plant.relative_load * 100.0),
    Channel("lambda", "Lambda (actual)", "λ", "fuel", lambda fr: fr.plant.lambda_value),
    Channel("lambda_target", "Lambda target", "λ", "fuel", lambda fr: fr.ecu.lambda_target),
    Channel("fuel_lph", "Fuel flow", "L/h", "fuel", lambda fr: fr.plant.fuel_rate_lph),
    Channel("bsfc_g_kwh", "Brake specific fuel consumption", "g/kWh", "fuel", lambda fr: fr.plant.bsfc_g_kwh),
    Channel("rail_bar", "Fuel rail pressure", "bar", "fuel", lambda fr: fr.plant.rail_pressure / 1e5),
    Channel("spark_deg", "Spark advance", "° BTDC", "ignition", lambda fr: fr.plant.spark_advance),
    Channel("mbt_deg", "MBT advance (model)", "° BTDC", "ignition", lambda fr: fr.plant.mbt_advance),
    Channel("knock_limit_deg", "Knock-limited advance (model)", "° BTDC", "ignition",
            lambda fr: fr.plant.knock_limit_advance),
    Channel("knock_retard_deg", "Knock retard (ECU)", "°", "ignition", lambda fr: fr.ecu.knock_retard),
    Channel("knock_intensity", "Knock intensity (model)", "–", "ignition", lambda fr: fr.plant.knock_intensity),
    Channel("iat_c", "Charge air temperature", "°C", "thermal", lambda fr: _k_to_c(fr.plant.t_im)),
    Channel("compressor_out_c", "Compressor outlet temperature", "°C", "thermal",
            lambda fr: _k_to_c(fr.plant.t_compressor_out)),
    Channel("coolant_c", "Coolant temperature", "°C", "thermal", lambda fr: _k_to_c(fr.plant.t_coolant)),
    Channel("oil_c", "Oil temperature", "°C", "thermal", lambda fr: _k_to_c(fr.plant.t_oil)),
    Channel("egt_c", "Exhaust gas temperature (pre-turbine)", "°C", "thermal",
            lambda fr: _k_to_c(fr.plant.t_exhaust_gas)),
    Channel("charge_loop_c", "Charge-cooler loop temperature", "°C", "thermal",
            lambda fr: _k_to_c(fr.plant.t_charge_loop)),
    Channel("imep_bar", "IMEP (gross)", "bar", "cylinder", lambda fr: fr.plant.imep / 1e5),
    Channel("pmep_bar", "PMEP", "bar", "cylinder", lambda fr: fr.plant.pmep / 1e5),
    Channel("fmep_bar", "FMEP", "bar", "cylinder", lambda fr: fr.plant.fmep / 1e5),
    Channel("bmep_bar", "BMEP", "bar", "cylinder", lambda fr: fr.plant.bmep / 1e5),
    Channel("peak_pressure_bar", "Peak cylinder pressure (estimate)", "bar", "cylinder",
            lambda fr: fr.plant.peak_pressure_est / 1e5),
    Channel("intake_cam_deg", "Intake cam advance", "°", "valvetrain", lambda fr: fr.plant.intake_cam),
    Channel("exhaust_cam_deg", "Exhaust cam retard", "°", "valvetrain", lambda fr: fr.plant.exhaust_cam),
    Channel("torque_request_nm", "ECU torque request", "N·m", "ecu", lambda fr: fr.ecu.torque_request),
    Channel("torque_limit_nm", "ECU torque limit", "N·m", "ecu", lambda fr: fr.ecu.torque_limit),
)
CHANNEL_BY_ID = {c.id: c for c in CHANNELS}

LIMITATIONS = (
    "Simulated result of a synthetic mean-value engine model with a synthetic calibration — not a measurement "
    "and not a prediction of any real engine's output.",
    "Model coefficients marked 'assumption' were chosen so full-load output lands near a published rating; "
    "agreement with that rating is a calibration target, not a validation.",
    "Mean-value model: no individual cylinder events, no cylinder-to-cylinder variation, no acoustic/wave "
    "tuning, empirical knock model.",
    "Brake torque is inertia-compensated crank torque; wheel figures exist only when a drivetrain loss is "
    "stated and are derived from that stated loss.",
)


def channel_list() -> list[dict]:
    return [{"id": c.id, "label": c.label, "unit": c.unit, "group": c.group} for c in CHANNELS]


@dataclass
class DynoTest:
    kind: str = "sweep"
    label: str = ""
    rpm_start: float = 1500.0
    rpm_end: float | None = None  # default: max rpm − 300
    ramp_rpm_s: float = 300.0
    pedal: float = 100.0
    settle_s: float = 2.0
    steady_points: list[tuple[float, float]] = field(default_factory=list)  # (rpm, pedal %)
    steady_settle_s: float = 3.0
    steady_measure_s: float = 1.0
    transient_rpm: float = 2500.0
    transient_from_pedal: float = 10.0
    transient_to_pedal: float = 100.0
    transient_duration_s: float = 4.0
    repeats: int = 3
    cooldown_s: float = 5.0
    sample_every_s: float = 0.02
    bin_rpm: float = 100.0
    ambient_temp_c: float = 25.0
    ambient_pressure_kpa: float = 101.325
    fan_airflow: float = 1.0
    coolant_start_c: float = 90.0
    correction: str = "none"
    drivetrain: DrivetrainLoss | None = None
    seed: int = 1

    def validate(self, config: EngineConfig) -> list[str]:
        p = []
        if self.kind not in TEST_KINDS:
            p.append(f"Unknown test kind {self.kind!r}")
        max_rpm = config.limits.max_rpm
        end = self.rpm_end if self.rpm_end is not None else max_rpm - 300.0
        if self.kind in ("sweep", "partial", "repeat"):
            if not 800.0 <= self.rpm_start < end <= max_rpm:
                p.append(f"Sweep range must satisfy 800 ≤ start < end ≤ {max_rpm:.0f} rpm")
            if not 50.0 <= self.ramp_rpm_s <= 2000.0:
                p.append("Ramp rate must be 50–2000 rpm/s")
        if not 0.0 <= self.pedal <= 100.0:
            p.append("Pedal must be 0–100 %")
        if self.kind == "steady":
            if not self.steady_points:
                p.append("Steady test needs at least one (rpm, pedal) point")
            if len(self.steady_points) > 200:
                p.append("At most 200 steady points per test")
            for rpm, pedal in self.steady_points:
                if not (800.0 <= rpm <= max_rpm and 0.0 <= pedal <= 100.0):
                    p.append(f"Steady point ({rpm}, {pedal}) out of range")
                    break
        if self.kind == "transient" and not 800.0 <= self.transient_rpm <= max_rpm:
            p.append("Transient speed out of range")
        if self.kind == "repeat" and not 2 <= self.repeats <= 10:
            p.append("Repeat test needs 2–10 runs")
        if not -30.0 <= self.ambient_temp_c <= 50.0:
            p.append("Ambient temperature must be −30…50 °C")
        if not 60.0 <= self.ambient_pressure_kpa <= 110.0:
            p.append("Ambient pressure must be 60–110 kPa")
        if self.drivetrain is not None:
            p += self.drivetrain.validate()
        return p

    def to_dict(self) -> dict:
        d = asdict(self)
        d["drivetrain"] = self.drivetrain.to_dict() if self.drivetrain else None
        return d

    @classmethod
    def from_dict(cls, d: dict) -> DynoTest:
        d = dict(d)
        dt = d.pop("drivetrain", None)
        known = {f for f in cls.__dataclass_fields__}
        test = cls(**{k: v for k, v in d.items() if k in known})
        test.steady_points = [(float(a), float(b)) for a, b in test.steady_points]
        test.drivetrain = DrivetrainLoss.from_dict(dt) if dt else None
        return test


@dataclass
class DynoRun:
    id: str
    label: str
    kind: str
    engine_id: str
    engine_name: str
    calibration_name: str
    calibration_hash: str
    created_utc: str
    test: dict
    samples: list[dict]
    curve: list[dict]
    summary: dict
    correction: dict
    events: dict
    metrics: dict = field(default_factory=dict)
    limitations: list[str] = field(default_factory=lambda: list(LIMITATIONS))
    provenance: str = "simulated"
    uncertainty: dict | None = None

    def to_dict(self, include_samples: bool = True) -> dict:
        d = asdict(self)
        if not include_samples:
            d["samples"] = []
        return d

    @classmethod
    def from_dict(cls, d: dict) -> DynoRun:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class DynoResult:
    runs: list[DynoRun]
    repeatability: dict | None = None

    def to_dict(self, include_samples: bool = True) -> dict:
        return {"runs": [r.to_dict(include_samples) for r in self.runs], "repeatability": self.repeatability}


# ---------------------------------------------------------------------------------------- helpers
def _sample(fr: Frame, t0: float, phase: str) -> dict:
    row = {"t": round(fr.plant.time - t0, 4), "phase": phase}
    for c in CHANNELS:
        v = c.extract(fr)
        row[c.id] = round(float(v), 4) if math.isfinite(v) else None
    return row


def _bin_curve(rows: list[dict], bin_rpm: float) -> list[dict]:
    """Average samples into rpm bins (bin centre = multiple of ``bin_rpm``)."""
    bins: dict[int, list[dict]] = {}
    for r in rows:
        key = int(round(r["rpm"] / bin_rpm))
        bins.setdefault(key, []).append(r)
    curve = []
    for key in sorted(bins):
        group = bins[key]
        if len(group) < 2:
            continue
        point = {"rpm": key * bin_rpm, "n": len(group)}
        for c in CHANNELS:
            if c.id == "rpm":
                continue
            vals = [g[c.id] for g in group if g[c.id] is not None]
            point[c.id] = round(sum(vals) / len(vals), 4) if vals else None
        curve.append(point)
    return curve


def _decorate(points: list[dict], cf: float, drivetrain: DrivetrainLoss | None) -> None:
    """Add hp/PS/lb·ft, corrected and wheel columns to curve points (in place)."""
    for p in points:
        tq, rpm = p["torque_nm"], p["rpm"]
        kw = tq * rpm * 2.0 * math.pi / 60.0 / 1000.0  # recompute from binned torque and bin speed
        p["power_kw"] = round(kw, 4)
        p["power_hp"] = round(kw * 1000.0 / W_PER_HP, 4)
        p["power_ps"] = round(kw * 1000.0 / W_PER_PS, 4)
        p["torque_lbft"] = round(nm_to_lbft(tq), 4)
        if cf != 1.0:
            p["torque_corr_nm"] = round(tq * cf, 4)
            p["power_corr_kw"] = round(kw * cf, 4)
        if drivetrain is not None:
            wt = drivetrain.wheel_torque_crank_ref(tq, rpm)
            p["wheel_torque_nm"] = round(wt, 4)
            p["wheel_power_kw"] = round(wt * rpm * 2.0 * math.pi / 60.0 / 1000.0, 4)
            p["wheel_power_hp"] = round(p["wheel_power_kw"] * 1000.0 / W_PER_HP, 4)


def _summary(curve: list[dict], samples: list[dict], cf: float, drivetrain: DrivetrainLoss | None) -> dict:
    if not curve:
        return {}
    pt = max(curve, key=lambda p: p["torque_nm"])
    pp = max(curve, key=lambda p: p["power_kw"])

    def mx(key, rows=samples):
        vals = [r[key] for r in rows if r.get(key) is not None]
        return round(max(vals), 3) if vals else None

    def mn(key, rows=samples):
        vals = [r[key] for r in rows if r.get(key) is not None]
        return round(min(vals), 3) if vals else None

    s = {
        "peak_torque_nm": round(pt["torque_nm"], 2), "peak_torque_rpm": pt["rpm"],
        "peak_torque_lbft": round(nm_to_lbft(pt["torque_nm"]), 2),
        "peak_power_kw": round(pp["power_kw"], 2), "peak_power_rpm": pp["rpm"],
        "peak_power_hp": round(pp["power_kw"] * 1000.0 / W_PER_HP, 2),
        "peak_power_ps": round(pp["power_kw"] * 1000.0 / W_PER_PS, 2),
        "max_boost_kpa": mx("boost_kpa"), "max_map_kpa": mx("map_kpa"), "max_egt_c": mx("egt_c"),
        "max_iat_c": mx("iat_c"), "max_coolant_c": mx("coolant_c"), "max_knock_retard_deg": mx("knock_retard_deg"),
        "min_lambda": mn("lambda"), "max_turbo_krpm": mx("turbo_krpm"), "max_peak_pressure_bar": mx("peak_pressure_bar"),
        "correction_factor": round(cf, 5),
    }
    if cf != 1.0:
        s["peak_torque_corr_nm"] = round(pt["torque_nm"] * cf, 2)
        s["peak_power_corr_kw"] = round(pp["power_kw"] * cf, 2)
    if drivetrain is not None:
        pw = max(curve, key=lambda p: p["wheel_power_kw"])
        tw = max(curve, key=lambda p: p["wheel_torque_nm"])
        s.update({"peak_wheel_power_kw": round(pw["wheel_power_kw"], 2), "peak_wheel_power_rpm": pw["rpm"],
                  "peak_wheel_power_hp": round(pw["wheel_power_hp"], 2),
                  "peak_wheel_torque_nm": round(tw["wheel_torque_nm"], 2),
                  "drivetrain_loss": drivetrain.name})
    return s


class _Recorder:
    def __init__(self, sim: Simulation, every: float, progress: Progress | None, cancel: Cancel | None,
                 total_s: float):
        self.sim = sim
        self.every = every
        self.samples: list[dict] = []
        self.dtcs: set[str] = set()
        self.protections: set[str] = set()
        self.modes: set[str] = set()
        self.t0 = sim.time
        self.progress = progress
        self.cancel = cancel
        self.total_s = max(total_s, 1e-6)
        self.elapsed = 0.0

    def run(self, seconds: float, phase: str, record: bool = True) -> Frame:
        frame = None
        remaining = seconds
        while remaining > 1e-9:
            chunk = min(self.every, remaining)
            frame = self.sim.run(chunk)
            remaining -= chunk
            self.elapsed += chunk
            self.dtcs.update(frame.ecu.dtcs)
            self.protections.update(frame.ecu.protections)
            if record:
                self.samples.append(_sample(frame, self.t0, phase))
                self.modes.add(frame.ecu.mode)
            if self.cancel is not None and self.cancel():
                raise DynoCancelled("Dyno run cancelled")
            if self.progress is not None and int(self.elapsed / 0.5) != int((self.elapsed - chunk) / 0.5):
                self.progress(min(0.99, self.elapsed / self.total_s), phase)
        assert frame is not None
        return frame

    def events(self) -> dict:
        return {"dtcs": sorted(self.dtcs), "protections": sorted(self.protections), "modes": sorted(self.modes)}


def _prepare(config: EngineConfig, calibration: Calibration, test: DynoTest) -> tuple[Simulation, DynoLoad]:
    ambient = Ambient(pressure=test.ambient_pressure_kpa * 1000.0, temperature=test.ambient_temp_c + 273.15)
    sim = Simulation(config, calibration.copy(), ambient=ambient, seed=test.seed)
    sim.warm_up(test.coolant_start_c)
    sim.plant.state.t_charge_loop = ambient.temperature + 5.0
    if not sim.start_engine():
        raise DynoError("Engine did not start (check the calibration's start-up tables)")
    sim.run(1.0)
    load = DynoLoad(target_rpm=max(sim.plant.state.omega * 60.0 / (2.0 * math.pi), 800.0),
                    fan_airflow=test.fan_airflow)
    sim.set_load(load)
    return sim, load


def _new_run(config: EngineConfig, calibration: Calibration, test: DynoTest, label: str, kind: str,
             samples: list[dict], curve: list[dict], events: dict, metrics: dict | None = None) -> DynoRun:
    turbo = config.turbo.enabled
    corr = correction_factor(test.correction, test.ambient_pressure_kpa * 1000.0, test.ambient_temp_c + 273.15,
                             turbocharged=turbo)
    _decorate(curve, corr.factor, test.drivetrain)
    summary = _summary(curve, samples, corr.factor, test.drivetrain)
    return DynoRun(
        id=uuid.uuid4().hex[:12], label=label, kind=kind, engine_id=config.id, engine_name=config.name,
        calibration_name=calibration.name, calibration_hash=calibration.content_hash(),
        created_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"), test=test.to_dict(),
        samples=samples, curve=curve, summary=summary, correction=corr.to_dict(), events=events,
        metrics=metrics or {},
    )


def _ramp(rec: _Recorder, load: DynoLoad, start: float, end: float, rate: float, pedal: float,
          settle: float, phase: str) -> int:
    """Hold ``start`` for ``settle`` s, then ramp to ``end``. Returns the index of the first ramp sample."""
    rec.sim.driver.pedal = 0.0
    load.target_rpm = start
    rec.run(1.0, "approach", record=False)
    rec.sim.driver.pedal = pedal
    rec.run(settle, "settle", record=False)
    first = len(rec.samples)
    rpm = start
    while rpm < end:
        rpm = min(end, rpm + rate * rec.every)
        load.target_rpm = rpm
        rec.run(rec.every, phase)
    rec.sim.driver.pedal = 0.0
    return first


def _sweep_time(test: DynoTest, end: float) -> float:
    return 1.0 + test.settle_s + (end - test.rpm_start) / test.ramp_rpm_s


# ---------------------------------------------------------------------------------------- public API
def run_dyno(config: EngineConfig, calibration: Calibration, test: DynoTest, progress: Progress | None = None,
             cancel: Cancel | None = None) -> DynoResult:
    """Run a virtual dyno test with the closed-loop simulation."""
    problems = test.validate(config)
    if problems:
        raise DynoError("; ".join(problems))
    end = test.rpm_end if test.rpm_end is not None else config.limits.max_rpm - 300.0
    label = test.label or f"{test.kind} — {calibration.name}"
    sim, load = _prepare(config, calibration, test)

    if test.kind in ("sweep", "partial"):
        pedal = test.pedal if test.kind == "partial" else 100.0
        rec = _Recorder(sim, test.sample_every_s, progress, cancel, _sweep_time(test, end))
        first = _ramp(rec, load, test.rpm_start, end, test.ramp_rpm_s, pedal, test.settle_s, "sweep")
        samples = rec.samples[first:]
        curve = _bin_curve(samples, test.bin_rpm)
        run = _new_run(config, calibration, test, label, test.kind, samples, curve, rec.events())
        return DynoResult([run])

    if test.kind == "repeat":
        runs = []
        total = test.repeats * (_sweep_time(test, end) + test.cooldown_s)
        rec = _Recorder(sim, test.sample_every_s, progress, cancel, total)
        for i in range(test.repeats):
            rec.samples = []
            rec.dtcs, rec.protections, rec.modes = set(), set(), set()
            first = _ramp(rec, load, test.rpm_start, end, test.ramp_rpm_s, 100.0, test.settle_s, "sweep")
            samples = rec.samples[first:]
            run = _new_run(config, calibration, test, f"{label} #{i + 1}", "repeat", samples,
                           _bin_curve(samples, test.bin_rpm), rec.events())
            run.metrics = {"run_index": i + 1, "start_iat_c": samples[0]["iat_c"],
                           "start_coolant_c": samples[0]["coolant_c"], "start_oil_c": samples[0]["oil_c"],
                           "start_charge_loop_c": samples[0]["charge_loop_c"]}
            runs.append(run)
            if test.cooldown_s > 0 and i < test.repeats - 1:
                load.target_rpm = 1500.0
                rec.run(test.cooldown_s, "cooldown", record=False)
        return DynoResult(runs, repeatability(runs))

    if test.kind == "steady":
        total = len(test.steady_points) * (test.steady_settle_s + test.steady_measure_s)
        rec = _Recorder(sim, test.sample_every_s, progress, cancel, total)
        points = []
        for rpm, pedal in test.steady_points:
            load.target_rpm = rpm
            sim.driver.pedal = pedal
            rec.run(test.steady_settle_s, "settle", record=False)
            start = len(rec.samples)
            rec.run(test.steady_measure_s, f"point {rpm:.0f}/{pedal:.0f}")
            window = rec.samples[start:]
            point = {"rpm": rpm, "pedal_pct": pedal, "n": len(window)}
            for c in CHANNELS:
                if c.id == "rpm":
                    continue
                vals = [w[c.id] for w in window if w[c.id] is not None]
                point[c.id] = round(sum(vals) / len(vals), 4) if vals else None
            tq = [w["torque_nm"] for w in window]
            mean = sum(tq) / len(tq)
            point["torque_std_nm"] = round(math.sqrt(sum((v - mean) ** 2 for v in tq) / len(tq)), 4)
            point["measured_rpm"] = round(sum(w["rpm"] for w in window) / len(window), 2)
            points.append(point)
        run = _new_run(config, calibration, test, label, "steady", rec.samples, points, rec.events())
        return DynoResult([run])

    # transient: tip-in at constant speed
    total = 1.0 + test.settle_s + test.transient_duration_s
    rec = _Recorder(sim, min(test.sample_every_s, 0.01), progress, cancel, total)
    load.target_rpm = test.transient_rpm
    sim.driver.pedal = test.transient_from_pedal
    rec.run(1.0 + test.settle_s, "settle", record=False)
    rec.run(0.5, "before")
    step_index = len(rec.samples)
    sim.driver.pedal = test.transient_to_pedal
    rec.run(test.transient_duration_s, "after")
    samples = rec.samples
    t_step = samples[step_index]["t"] - rec.every
    metrics = transient_metrics(samples, t_step)
    run = _new_run(config, calibration, test, label, "transient", samples, [], rec.events(), metrics)
    run.summary = {"final_torque_nm": metrics.get("final_torque_nm"), "t90_s": metrics.get("t90_s"),
                   "max_boost_kpa": max(s["boost_kpa"] for s in samples)}
    return DynoResult([run])


def transient_metrics(samples: list[dict], t_step: float) -> dict:
    """Response metrics of a pedal step: 10/50/90 % torque times, overshoot, boost response."""
    before = [s for s in samples if s["t"] <= t_step]
    after = [s for s in samples if s["t"] > t_step]
    if not before or len(after) < 10:
        return {}
    t_end = after[-1]["t"]
    final = [s for s in after if s["t"] >= t_end - 0.5]
    tq0 = sum(s["torque_nm"] for s in before) / len(before)
    tq1 = sum(s["torque_nm"] for s in final) / len(final)
    b1 = sum(s["boost_kpa"] for s in final) / len(final)
    out = {"initial_torque_nm": round(tq0, 2), "final_torque_nm": round(tq1, 2), "final_boost_kpa": round(b1, 2)}
    span = tq1 - tq0
    for frac in (0.1, 0.5, 0.9):
        hit = next((s["t"] for s in after if (s["torque_nm"] - tq0) >= frac * span), None) if span > 1.0 else None
        out[f"t{int(frac * 100)}_s"] = round(hit - t_step, 4) if hit is not None else None
    peak = max(s["torque_nm"] for s in after)
    out["torque_overshoot_pct"] = round(max(0.0, (peak - tq1) / span * 100.0), 2) if span > 1.0 else 0.0
    bpk = max(s["boost_kpa"] for s in after)
    out["boost_overshoot_kpa"] = round(max(0.0, bpk - b1), 2)
    hit_b = next((s["t"] for s in after if s["boost_kpa"] >= b1 - 0.1 * max(b1 - after[0]["boost_kpa"], 1.0)), None)
    out["boost_t90_s"] = round(hit_b - t_step, 4) if hit_b is not None else None
    return out


def repeatability(runs: list[DynoRun]) -> dict:
    """Run-to-run statistics for repeated sweeps (heat soak shows up as a trend)."""
    peaks_p = [r.summary["peak_power_kw"] for r in runs]
    peaks_t = [r.summary["peak_torque_nm"] for r in runs]

    def stats(v):
        m = sum(v) / len(v)
        sd = math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1)) if len(v) > 1 else 0.0
        return {"values": [round(x, 2) for x in v], "mean": round(m, 3), "std": round(sd, 3),
                "spread": round(max(v) - min(v), 3), "cv_pct": round(sd / m * 100.0, 3) if m else 0.0}

    return {
        "peak_power_kw": stats(peaks_p), "peak_torque_nm": stats(peaks_t),
        "start_iat_c": [r.metrics.get("start_iat_c") for r in runs],
        "start_coolant_c": [r.metrics.get("start_coolant_c") for r in runs],
        "start_charge_loop_c": [r.metrics.get("start_charge_loop_c") for r in runs],
        "power_change_first_to_last_pct": round((peaks_p[-1] - peaks_p[0]) / peaks_p[0] * 100.0, 3)
        if peaks_p[0] else 0.0,
        "note": "Run-to-run variation in the simulator comes from heat soak (charge-air, coolant, oil) and "
                "modelled sensor noise only; real dyno repeatability also includes measurement error.",
    }
