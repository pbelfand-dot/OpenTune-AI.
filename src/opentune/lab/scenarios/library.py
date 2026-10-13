"""Test Laboratory: scripted scenarios A–G with expectations, metrics and findings.

A  Stock calibration baseline          E  Sensor failures (DTCs, substitutes, limp)
B  Moderate synthetic tune             F  Interrupted virtual-ECU update and recovery
C  Aggressive synthetic tune           G  Repeated runs / thermal repeatability
D  Heat stress (hot ambient, low airflow, back-to-back pulls)

"Expectations" check that the *model and virtual ECU* behave as designed (e.g.
knock control responds to an aggressive tune, the bootloader survives a power
loss). They make no claim about the safety or behaviour of any real engine.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

from opentune.lab.analysis.analyst import (
    Finding,
    analyze_comparison,
    analyze_repeatability,
    analyze_run,
)
from opentune.lab.calibration.editor import CalibrationEditor
from opentune.lab.calibration.tables import Calibration
from opentune.lab.dyno.runs import DynoRun, DynoTest, run_dyno
from opentune.lab.flash import (
    DeviceFaults,
    LinkFaults,
    LoopbackLink,
    Programmer,
    VirtualEcuDevice,
    build_image,
    verify_image,
)
from opentune.lab.physics.config import EngineConfig
from opentune.lab.sim.loads import DynoLoad
from opentune.lab.sim.session import Simulation

DISCLAIMER = ("Scenario results describe the behaviour of a synthetic engine model and a fictional virtual ECU. "
              "They make no claim about the safety, durability or behaviour of any real engine or vehicle.")


@dataclass
class Check:
    name: str
    passed: bool | None  # None = observation only
    detail: str
    kind: str = "expectation"


@dataclass
class ScenarioResult:
    id: str
    name: str
    description: str
    checks: list[Check] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    runs: list[DynoRun] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    duration_s: float = 0.0
    disclaimer: str = DISCLAIMER

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks if c.kind == "expectation")

    def to_dict(self, include_runs: bool = False) -> dict:
        return {"id": self.id, "name": self.name, "description": self.description, "passed": self.passed,
                "checks": [asdict(c) for c in self.checks], "metrics": self.metrics,
                "run_ids": [r.id for r in self.runs],
                "runs": [r.to_dict(include_samples=False) for r in self.runs] if include_runs else [],
                "findings": [f.to_dict() for f in self.findings], "duration_s": round(self.duration_s, 2),
                "disclaimer": self.disclaimer}


Progress = Callable[[float, str], None]


@dataclass
class Context:
    config: EngineConfig
    calibration: Calibration
    quick: bool = False
    progress: Progress = lambda f, m: None  # noqa: E731
    cancel: Callable[[], bool] = lambda: False  # noqa: E731
    _baseline: DynoRun | None = None

    def sweep(self, cal: Calibration, label: str, **kw) -> DynoRun:
        if self.cancel():
            raise RuntimeError("Scenario cancelled")
        test = DynoTest(kind=kw.pop("kind", "sweep"), label=label, **self.sweep_args(), **kw)
        result = run_dyno(self.config, cal, test, cancel=self.cancel)
        run = result.runs[0]
        if result.repeatability:
            run.metrics["repeatability"] = result.repeatability
            run.metrics["all_runs"] = result.runs
        return run

    def sweep_args(self) -> dict:
        end = self.config.limits.max_rpm - (1200.0 if self.quick else 300.0)
        return {"rpm_start": 2000.0 if self.quick else 1500.0, "rpm_end": end,
                "ramp_rpm_s": 600.0 if self.quick else 300.0, "settle_s": 1.0 if self.quick else 2.0}

    def baseline(self) -> DynoRun:
        if self._baseline is None:
            self._baseline = self.sweep(self.calibration, "A: stock baseline sweep")
        return self._baseline


def _tune(cal: Calibration, name: str, boost_kpa: float, spark_deg: float, torque_pct: float) -> Calibration:
    out = cal.copy(f"{cal.name} — {name} (synthetic)")
    ed = CalibrationEditor(out)
    bm = out.table("boost_max")
    hi_pedal = len(bm.axes[1].values)
    start = next(i for i, r in enumerate(bm.axes[0].values) if r >= 2000)
    ed.apply_region("boost_max", "add", boost_kpa, ((start, len(bm.axes[0].values)), (hi_pedal - 2, hi_pedal)))
    ign = out.table("ign_base")
    loads = ign.axes[1].values
    lo = next(i for i, v in enumerate(loads) if v >= 100)
    ed.apply_region("ign_base", "add", spark_deg, ((0, len(ign.axes[0].values)), (lo, len(loads))))
    ed.apply_region("torque_limit", "percent", torque_pct)
    return out


# ---------------------------------------------------------------------------------------- scenarios
def scenario_a(ctx: Context) -> ScenarioResult:
    r = ScenarioResult("A", "Stock calibration baseline",
                       "Full-load sweep, tip-in transient and warm idle on the unmodified synthetic baseline.")
    sweep = ctx.baseline()
    tr = run_dyno(ctx.config, ctx.calibration, DynoTest(kind="transient", transient_rpm=2500.0,
                                                        transient_duration_s=3.0, settle_s=1.0)).runs[0]
    sim = Simulation(ctx.config, ctx.calibration.copy())
    sim.warm_up()
    sim.start_engine()
    rpms: list[float] = []
    lams: list[float] = []
    sim.run(6.0 if ctx.quick else 10.0, every=0.05,
            callback=lambda fr: (rpms.append(fr.plant.rpm), lams.append(fr.plant.lambda_value)))
    late, late_l = rpms[-40:], lams[-40:]
    target = sim.ecu.last.idle_target_rpm
    s = sweep.summary
    ref = ctx.config.reference_ratings.get("power_kw")
    r.runs = [sweep, tr]
    r.checks = [
        Check("no DTCs on a healthy engine", not sweep.events["dtcs"] and not tr.events["dtcs"],
              f"sweep {sweep.events['dtcs']}, transient {tr.events['dtcs']}"),
        Check("no significant knock retard", (s.get("max_knock_retard_deg") or 0) <= 1.0,
              f"max {s.get('max_knock_retard_deg')}°"),
        Check("EGT below modelled limit", s["max_egt_c"] < ctx.config.limits.egt_limit_c, f"{s['max_egt_c']} °C"),
        Check("idle settles within ±50 rpm of target", abs(sum(late) / len(late) - target) < 50,
              f"mean {sum(late) / len(late):.0f} rpm vs target {target:.0f}"),
        Check("idle lambda ≈ 1 (closed loop)", abs(sum(late_l) / len(late_l) - 1.0) < 0.03,
              f"λ {sum(late_l) / len(late_l):.3f}"),
        Check("tip-in reaches 90 % torque within 2 s", (tr.metrics.get("t90_s") or 99) < 2.0,
              f"t90 {tr.metrics.get('t90_s')} s"),
    ]
    if ref and not ctx.quick:
        r.checks.append(Check("peak power within ±10 % of the published reference (calibration target, not "
                              "validation)", abs(s["peak_power_kw"] - ref) / ref < 0.10,
                              f"{s['peak_power_kw']} kW vs {ref} kW", "expectation"))
    r.metrics = {"peak_power_kw": s["peak_power_kw"], "peak_torque_nm": s["peak_torque_nm"],
                 "idle_rpm": round(sum(late) / len(late), 1), "t90_s": tr.metrics.get("t90_s")}
    r.findings = analyze_run(sweep, ctx.config) + analyze_run(tr, ctx.config)
    return r


def _tuned_scenario(ctx: Context, sid: str, name: str, boost: float, spark: float, torque: float) -> ScenarioResult:
    r = ScenarioResult(sid, name, f"Synthetic tune: boost target +{boost:g} kPa (≥2000 rpm, high pedal), base "
                                  f"spark +{spark:g}° at ≥100 % load, torque limit +{torque:g} %. Compared with A.")
    base = ctx.baseline()
    cal = _tune(ctx.calibration, name.lower(), boost, spark, torque)
    run = ctx.sweep(cal, f"{sid}: {name.lower()} sweep")
    r.runs = [base, run]
    s, b = run.summary, base.summary
    r.metrics = {"peak_power_kw": s["peak_power_kw"], "peak_power_delta_kw": round(s["peak_power_kw"]
                                                                                  - b["peak_power_kw"], 2),
                 "max_knock_retard_deg": s.get("max_knock_retard_deg"), "max_egt_c": s["max_egt_c"],
                 "max_iat_c": s.get("max_iat_c"), "protections": run.events["protections"]}
    r.findings = analyze_comparison(base, run, base_cal_diff(ctx.calibration, cal)) + analyze_run(run, ctx.config)
    return r, run, s


def base_cal_diff(a: Calibration, b: Calibration) -> dict:
    return a.diff(b).to_dict()


def scenario_b(ctx: Context) -> ScenarioResult:
    r, run, s = _tuned_scenario(ctx, "B", "Moderate tune", 15.0, 1.5, 10.0)
    r.checks = [
        Check("more power than baseline", r.metrics["peak_power_delta_kw"] > 0, f"{r.metrics['peak_power_delta_kw']:+} kW"),
        Check("knock retard stays small (≤ 2°)", (s.get("max_knock_retard_deg") or 0) <= 2.0,
              f"{s.get('max_knock_retard_deg')}°"),
        Check("EGT below modelled limit", s["max_egt_c"] < ctx.config.limits.egt_limit_c, f"{s['max_egt_c']} °C"),
        Check("no DTCs", not run.events["dtcs"], str(run.events["dtcs"])),
    ]
    return r


def scenario_c(ctx: Context) -> ScenarioResult:
    r, run, s = _tuned_scenario(ctx, "C", "Aggressive tune", 45.0, 4.0, 30.0)
    protective = (s.get("max_knock_retard_deg") or 0) > 0.5 or "egt_enrichment" in run.events["protections"] or \
        "overboost_protection" in run.events["protections"]
    r.checks = [
        Check("virtual ECU protections respond (knock retard, EGT enrichment or overboost)", protective,
              f"knock retard {s.get('max_knock_retard_deg')}°, protections {run.events['protections']}"),
        Check("analyst raises at least one warning", any(f.severity in ("warning", "critical") for f in r.findings),
              f"{sum(f.severity in ('warning', 'critical') for f in r.findings)} warnings"),
        Check("engine keeps running (no stall, no limp)", "limp_mode" not in run.events["protections"],
              str(run.events["protections"])),
        Check("power gain vs baseline", None, f"{r.metrics['peak_power_delta_kw']:+} kW", "observation"),
    ]
    return r


def scenario_d(ctx: Context) -> ScenarioResult:
    r = ScenarioResult("D", "Heat stress", "Three back-to-back full-load pulls at 40 °C ambient with 30 % cooling "
                                           "airflow and no cool-down.")
    test = DynoTest(kind="repeat", repeats=3, cooldown_s=0.5, ambient_temp_c=40.0, fan_airflow=0.3,
                    **ctx.sweep_args())
    result = run_dyno(ctx.config, ctx.calibration, test, cancel=ctx.cancel)
    rep = result.repeatability
    r.runs = result.runs
    iat = [x.summary.get("max_iat_c") for x in result.runs]
    knock = max((x.summary.get("max_knock_retard_deg") or 0) for x in result.runs)
    coolant = [x.summary.get("max_coolant_c") or 0.0 for x in result.runs]
    protections = {p for x in result.runs for p in x.events["protections"]}
    base = ctx.baseline().summary["peak_power_kw"]
    r.metrics = {"peak_power_kw": rep["peak_power_kw"]["values"], "max_iat_c": iat, "max_knock_retard_deg": knock,
                 "loss_vs_25c_baseline_pct": round((rep["peak_power_kw"]["values"][-1] - base) / base * 100, 2),
                 "start_charge_loop_c": rep["start_charge_loop_c"]}
    r.checks = [
        Check("charge-air temperature rises from pull to pull", iat[-1] > iat[0], f"{iat} °C"),
        Check("last pull makes less power than the 25 °C baseline", rep["peak_power_kw"]["values"][-1] < base,
              f"{rep['peak_power_kw']['values'][-1]} vs {base} kW"),
        Check("coolant overheat protection engages once coolant passes 110 °C",
              max(coolant) <= 110.0 or "coolant_torque_limit" in protections,
              f"max coolant {max(coolant)} °C, protections {sorted(protections)}"),
        Check("coolant vs modelled limit", None, f"{coolant} °C (limit {ctx.config.limits.coolant_limit_c} °C)",
              "observation"),
        Check("knock retard / protections", None, f"max knock retard {knock}°", "observation"),
    ]
    r.findings = analyze_repeatability(rep) + analyze_run(result.runs[-1], ctx.config)
    return r


def _fault_case(ctx: Context, sensor: str, kind: str, magnitude: float = 0.0) -> dict:
    sim = Simulation(ctx.config, ctx.calibration.copy(), seed=5)
    sim.warm_up()
    sim.start_engine()
    sim.run(1.0)
    sim.set_load(DynoLoad(target_rpm=3000.0))
    sim.driver.pedal = 60.0
    sim.run(2.0)
    before = sim.run(0.01).plant.brake_torque
    sim.sensors.inject(sensor, kind, magnitude)
    fr = sim.run(4.0)
    return {"sensor": sensor, "fault": kind, "dtcs": list(fr.ecu.dtcs), "mil": fr.ecu.mil, "limp": fr.ecu.limp,
            "protections": list(fr.ecu.protections), "rpm": round(fr.plant.rpm), "running": fr.ecu.state == "RUNNING",
            "torque_before_nm": round(before, 1), "torque_after_nm": round(fr.plant.brake_torque, 1),
            "lambda": round(fr.plant.lambda_value, 3)}


def scenario_e(ctx: Context) -> ScenarioResult:
    r = ScenarioResult("E", "Sensor failures", "Faults injected at 3000 rpm / 60 % pedal: MAP open circuit, IAT "
                                               "short to ground, knock sensor open circuit, coolant sensor open "
                                               "circuit, wideband offset.")
    cases = [("map", "open_circuit", 0.0, "P0107", True), ("iat", "short_ground", 0.0, "P0112", False),
             ("knock", "open_circuit", 0.0, "P0327", False), ("ect", "open_circuit", 0.0, "P0118", False)]
    results = []
    for i, (sensor, kind, mag, code, limp) in enumerate(cases):
        ctx.progress(0.1 + 0.2 * i, f"{sensor} {kind}")
        res = _fault_case(ctx, sensor, kind, mag)
        results.append(res)
        r.checks.append(Check(f"{sensor} {kind} → {code}", code in res["dtcs"] and res["mil"],
                              f"DTCs {res['dtcs']}, MIL {res['mil']}"))
        r.checks.append(Check(f"{sensor} {kind}: engine keeps running" + (" in limp mode" if limp else ""),
                              res["running"] and res["limp"] == limp,
                              f"running {res['running']}, limp {res['limp']}, protections {res['protections']}"))
    lam = _fault_case(ctx, "lambda", "offset", 0.06)
    results.append(lam)
    r.checks.append(Check("wideband offset: closed loop follows the wrong reading (no DTC within 4 s)", None,
                          f"actual λ {lam['lambda']}, DTCs {lam['dtcs']}", "observation"))
    r.metrics = {"cases": results}
    for res in results:
        for code in res["dtcs"]:
            r.findings.append(Finding(f"e_{res['sensor']}_{code}", f"{code} after {res['sensor']} {res['fault']}",
                                      "warning", "diagnostics", f"Injected fault produced {code}.", []))
    return r


def scenario_f(ctx: Context) -> ScenarioResult:
    r = ScenarioResult("F", "Interrupted ECU update", "Virtual ECU programmed with a moderate tune while the link "
                                                      "drops, power is lost mid-write and a flash cell fails; then a "
                                                      "clean flash.")
    dev = VirtualEcuDevice(ctx.config.id)
    base_img = build_image(ctx.calibration)
    dev.factory_program(base_img)
    base_sha = verify_image(base_img).payload_sha256
    image = build_image(_tune(ctx.calibration, "moderate", 15.0, 1.5, 10.0))
    new_sha = verify_image(image).payload_sha256
    steps = []

    rep = Programmer(LoopbackLink(dev, LinkFaults(disconnect_after_frames=10))).flash(image)
    r.checks.append(Check("link drop: flash fails, ECU still runs the old calibration",
                          not rep.success and dev.running_sha == base_sha, rep.error or ""))
    steps.append(rep.to_dict())
    dev.faults = DeviceFaults(power_loss_stage="write", power_loss_block=4)
    rep = Programmer(LoopbackLink(dev)).flash(image)
    boot = dev.boot()
    r.checks.append(Check("power loss mid-write: after power cycle the ECU boots the old calibration",
                          not rep.success and boot["mode"] == "application" and dev.running_sha == base_sha,
                          f"boot bank {boot['bank']}, mode {boot['mode']}"))
    steps.append(rep.to_dict())
    dev.faults = DeviceFaults(power_loss_stage="activate")
    rep = Programmer(LoopbackLink(dev)).flash(image)
    boot = dev.boot()
    r.checks.append(Check("power loss during activation: redundant boot block keeps the old calibration",
                          not rep.success and dev.running_sha == base_sha, f"boot bank {boot['bank']}"))
    steps.append(rep.to_dict())
    dev.faults = DeviceFaults(bitflip_block=2)
    rep = Programmer(LoopbackLink(dev)).flash(image)
    r.checks.append(Check("flash-cell bit flip: verification catches it, nothing activated",
                          not rep.success and "VERIFY" in (rep.error or "") and dev.running_sha == base_sha,
                          rep.error or ""))
    steps.append(rep.to_dict())
    rep = Programmer(LoopbackLink(dev, LinkFaults(corrupt_rate=0.05), seed=4)).flash(image)
    r.checks.append(Check("clean flash over a noisy link succeeds and runs the new image",
                          rep.success and dev.running_sha == new_sha, f"retries {rep.retries}"))
    steps.append(rep.to_dict())
    rb = Programmer(LoopbackLink(dev)).rollback()
    r.checks.append(Check("rollback returns to the previous calibration", rb.success and dev.running_sha == base_sha,
                          rb.error or f"bank {dev.running_bank}"))
    r.metrics = {"attempts": [{"operation": s["operation"], "success": s["success"], "error": s["error"],
                               "retries": s["retries"]} for s in steps]}
    return r


def scenario_g(ctx: Context) -> ScenarioResult:
    n = 3 if ctx.quick else 5
    r = ScenarioResult("G", "Repeated runs / thermal repeatability", f"{n} back-to-back pulls at 25 °C with a 2 s "
                                                                     f"cool-down at 1500 rpm between pulls.")
    test = DynoTest(kind="repeat", repeats=n, cooldown_s=2.0, **ctx.sweep_args())
    result = run_dyno(ctx.config, ctx.calibration, test, cancel=ctx.cancel)
    rep = result.repeatability
    r.runs = result.runs
    r.metrics = {"repeatability": rep}
    r.checks = [
        Check("peak power coefficient of variation < 3 %", rep["peak_power_kw"]["cv_pct"] < 3.0,
              f"CV {rep['peak_power_kw']['cv_pct']} %"),
        Check("no DTCs across the series", all(not x.events["dtcs"] for x in result.runs),
              str([x.events["dtcs"] for x in result.runs])),
        Check("heat-soak trend", None, f"first→last {rep['power_change_first_to_last_pct']:+.2f} %", "observation"),
    ]
    r.findings = analyze_repeatability(rep)
    return r


SCENARIOS: dict[str, tuple[str, Callable[[Context], ScenarioResult]]] = {
    "A": ("Stock calibration baseline", scenario_a),
    "B": ("Moderate tune", scenario_b),
    "C": ("Aggressive tune", scenario_c),
    "D": ("Heat stress", scenario_d),
    "E": ("Sensor failures", scenario_e),
    "F": ("Interrupted ECU update", scenario_f),
    "G": ("Repeated runs / thermal", scenario_g),
}


def run_scenarios(config: EngineConfig, calibration: Calibration, ids: list[str] | None = None, quick: bool = False,
                  progress: Progress | None = None, cancel: Callable[[], bool] | None = None) -> list[ScenarioResult]:
    ids = ids or list(SCENARIOS)
    unknown = [i for i in ids if i not in SCENARIOS]
    if unknown:
        raise ValueError(f"Unknown scenario(s): {unknown}")
    ctx = Context(config, calibration, quick, progress or (lambda f, m: None), cancel or (lambda: False))
    out = []
    for n, sid in enumerate(ids):
        ctx.progress(n / len(ids), f"scenario {sid}")
        t0 = time.perf_counter()
        res = SCENARIOS[sid][1](ctx)
        res.duration_s = time.perf_counter() - t0
        out.append(res)
    ctx.progress(1.0, "done")
    return out
