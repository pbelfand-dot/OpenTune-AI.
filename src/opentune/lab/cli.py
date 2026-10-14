"""``opentune-lab`` — Virtual Engine Lab command line (headless, cloud-friendly).

    opentune-lab serve [--port 8765] [--open]      browser UI + API on 127.0.0.1
    opentune-lab presets                             list engine presets
    opentune-lab simulate --engine b58-inspired      closed-loop run, prints a telemetry table
    opentune-lab dyno --engine b58-inspired --kind sweep --out results/
    opentune-lab selftest                            end-to-end check (no network, no display)

Everything here is simulated. Nothing talks to vehicle hardware.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def cmd_presets(args) -> int:
    from opentune.lab.physics.presets import PRESETS

    for pid, factory in PRESETS.items():
        cfg = factory()
        ref = cfg.reference_ratings
        print(f"{pid:14s} {cfg.name}")
        print(f"{'':14s} sanity target: {ref.get('label')} {ref.get('power_kw')} kW / {ref.get('torque_nm')} N·m "
              f"({ref.get('source')})")
    return 0


def cmd_simulate(args) -> int:
    from opentune.lab.calibration.defaults import build_baseline
    from opentune.lab.physics.presets import get_preset
    from opentune.lab.sim import DynoLoad, Simulation

    cfg = get_preset(args.engine)
    sim = Simulation(cfg, build_baseline(cfg), seed=args.seed)
    sim.warm_up()
    if not sim.start_engine():
        print("engine did not start", file=sys.stderr)
        return 1
    if args.dyno_rpm:
        sim.set_load(DynoLoad(target_rpm=args.dyno_rpm))
    sim.driver.pedal = args.pedal
    cols = ("time", "rpm", "brake_torque", "p_im", "p_boost", "lambda_value", "spark_advance", "t_im",
            "t_coolant", "turbo_rpm", "ecu_mode")
    print("SIMULATED — synthetic model, not a measurement")
    print("  ".join(f"{c:>12s}" for c in cols))

    def show(fr):
        d = fr.flat()
        vals = []
        for c in cols:
            v = d[c]
            if c in ("p_im", "p_boost"):
                v = v / 1000.0
            elif c in ("t_im", "t_coolant"):
                v = v - 273.15
            vals.append(f"{v:12.2f}" if isinstance(v, float) else f"{str(v):>12s}")
        print("  ".join(vals))

    sim.run(args.seconds, every=args.every, callback=show)
    fr = sim.run(0.001)
    if fr.ecu.dtcs:
        print("DTCs:", ", ".join(fr.ecu.dtcs))
    return 0


def cmd_dyno(args) -> int:
    from opentune.lab.calibration.defaults import build_baseline
    from opentune.lab.calibration.io import import_json
    from opentune.lab.dyno import (
        DRIVETRAIN_EXAMPLES,
        DynoTest,
        curve_to_csv,
        estimate_uncertainty,
        run_dyno,
    )
    from opentune.lab.physics.presets import get_preset

    cfg = get_preset(args.engine)
    cal = import_json(Path(args.calibration).read_text()) if args.calibration else build_baseline(cfg)
    test = DynoTest(kind=args.kind, pedal=args.pedal, rpm_start=args.rpm_start, rpm_end=args.rpm_end,
                    ramp_rpm_s=args.ramp, correction=args.correction,
                    drivetrain=DRIVETRAIN_EXAMPLES[args.drivetrain] if args.drivetrain else None,
                    steady_points=[tuple(map(float, p.split(":"))) for p in args.point or []], repeats=args.repeats)
    t0 = time.time()
    result = run_dyno(cfg, cal, test)
    print(f"SIMULATED dyno ({args.kind}) — {cfg.name} — {time.time() - t0:.1f} s compute")
    out = Path(args.out) if args.out else None
    if out:
        out.mkdir(parents=True, exist_ok=True)
    for run in result.runs:
        if args.uncertainty and run.kind in ("sweep", "partial", "steady"):
            run.uncertainty = estimate_uncertainty(cfg, run, samples=args.uncertainty).to_dict()
        s = run.summary
        print(f"  {run.label}: peak {s.get('peak_torque_nm')} N·m @ {s.get('peak_torque_rpm')} rpm, "
              f"{s.get('peak_power_kw')} kW ({s.get('peak_power_hp')} hp) @ {s.get('peak_power_rpm')} rpm")
        if run.uncertainty:
            u = run.uncertainty
            print(f"    model-uncertainty band (5–95 %): {u['peak_power_p05_kw']}–{u['peak_power_p95_kw']} kW")
        if run.metrics:
            print(f"    metrics: {json.dumps(run.metrics)}")
        if run.events.get("dtcs"):
            print(f"    DTCs: {', '.join(run.events['dtcs'])}")
        if out:
            (out / f"{run.id}_curve.csv").write_text(curve_to_csv(run))
            (out / f"{run.id}.json").write_text(json.dumps(run.to_dict(), indent=1))
    if result.repeatability:
        print("  repeatability:", json.dumps(result.repeatability["peak_power_kw"]))
    if out:
        print(f"  written to {out}")
    return 0


def cmd_serve(args) -> int:
    from opentune.lab.api.app import serve

    serve(args.host, args.port, args.data_dir, args.open)
    return 0


def cmd_selftest(args) -> int:
    import tempfile

    from opentune.ecu.safety import REAL_ECU_WRITE_ENABLED
    from opentune.lab.calibration.defaults import build_baseline
    from opentune.lab.dyno import DynoTest, run_dyno
    from opentune.lab.physics.presets import PRESETS, get_preset

    ok = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        ok &= bool(cond)
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")

    print("OpenTune Virtual Engine Lab self-test (simulation only)")
    check("real ECU writes disabled", REAL_ECU_WRITE_ENABLED is False)
    for pid in PRESETS:
        cfg = get_preset(pid)
        cal = build_baseline(cfg)
        check(f"{pid}: baseline calibration valid", not cal.validate())
        run = run_dyno(cfg, cal, DynoTest(rpm_start=2000, rpm_end=4500, ramp_rpm_s=600, settle_s=1.0)).runs[0]
        check(f"{pid}: dyno sweep", run.summary["peak_torque_nm"] > 100, f"{run.summary['peak_torque_nm']} N·m")
        check(f"{pid}: no DTCs on a healthy sweep", not run.events["dtcs"], ",".join(run.events["dtcs"]))
    try:
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # starlette TestClient deprecation notice
            from fastapi.testclient import TestClient

        from opentune.lab.api.app import create_app

        with TestClient(create_app(tempfile.mkdtemp())) as client:
            check("API health", client.get("/api/health").json().get("ok") is True)
            check("API reports no hardware access", client.get("/api/system").json()["safety"][
                "real_ecu_write_enabled"] is False)
    except ImportError as exc:
        check("API (fastapi installed)", False, str(exc))
    print("SELFTEST PASSED" if ok else "SELFTEST FAILED")
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="opentune-lab", description="OpenTune Virtual Engine Lab (simulation only)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="start the local browser UI and API")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--data-dir")
    s.add_argument("--open", action="store_true", help="open the browser")
    s.set_defaults(func=cmd_serve)
    sub.add_parser("presets", help="list engine presets").set_defaults(func=cmd_presets)
    sm = sub.add_parser("simulate", help="headless closed-loop simulation")
    sm.add_argument("--engine", default="b58-inspired")
    sm.add_argument("--seconds", type=float, default=10.0)
    sm.add_argument("--every", type=float, default=0.5)
    sm.add_argument("--pedal", type=float, default=0.0)
    sm.add_argument("--dyno-rpm", type=float, default=0.0, help="hold this speed with a dyno load")
    sm.add_argument("--seed", type=int, default=1)
    sm.set_defaults(func=cmd_simulate)
    d = sub.add_parser("dyno", help="run a virtual dyno test")
    d.add_argument("--engine", default="b58-inspired")
    d.add_argument("--calibration", help="OpenTune calibration JSON (default: synthetic baseline)")
    d.add_argument("--kind", default="sweep", choices=("sweep", "partial", "steady", "transient", "repeat"))
    d.add_argument("--pedal", type=float, default=100.0)
    d.add_argument("--rpm-start", type=float, default=1500.0)
    d.add_argument("--rpm-end", type=float, default=None)
    d.add_argument("--ramp", type=float, default=300.0)
    d.add_argument("--repeats", type=int, default=3)
    d.add_argument("--point", action="append", help="steady point rpm:pedal (repeatable)")
    d.add_argument("--correction", default="none", choices=("none", "sae_j1349", "din_70020", "iso_1585_si"))
    d.add_argument("--drivetrain", choices=("flat_10", "flat_15", "mixed_rwd_auto", "mixed_awd_auto"))
    d.add_argument("--uncertainty", type=int, default=0, help="Monte Carlo samples (0 = off)")
    d.add_argument("--out", help="directory for CSV/JSON results")
    d.set_defaults(func=cmd_dyno)
    sub.add_parser("selftest", help="end-to-end check").set_defaults(func=cmd_selftest)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
