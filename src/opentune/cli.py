"""OpenTune AI command-line interface.

Every command works without a car: ``simulate``, ``dtc``, ``flash-sim`` and
``selftest`` use the built-in simulator. ``scan`` and ``dtc --port`` talk to a
real ELM327 adapter in read-only mode.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

from opentune import __app_name__, __version__
from opentune.config import AppPaths
from opentune.core.models import CHANNELS, Sample
from opentune.core.units import UnitSystem, convert, display_unit
from opentune.engines import ENGINES, get_profile

LIVE_COLUMNS = {
    "rpm": "rpm", "vehicle_speed": "speed", "gear": "gear", "pedal": "pedal", "boost": "boost",
    "boost_target": "target", "timing_advance": "timing", "intake_air_temp": "IAT",
    "coolant_temp": "coolant", "lambda_cmd": "lambda",
}


def _paths(args) -> AppPaths:
    return AppPaths(Path(args.data_dir)).ensure() if getattr(args, "data_dir", None) else AppPaths.default()


def _column_label(key: str, units: UnitSystem) -> str:
    unit = display_unit(CHANNELS[key].unit, units)
    name = LIVE_COLUMNS.get(key, key)
    return f"{name}({unit})" if unit and unit not in ("rpm", "λ") else name


def _fmt(key: str, value: float | None, units: UnitSystem) -> str:
    if value is None:
        return "–"
    ch = CHANNELS[key]
    return f"{convert(value, ch.unit, units):.{ch.decimals}f}"


# ---------------------------------------------------------------------------- commands
def cmd_info(args) -> int:
    from opentune.ecu.safety import REAL_ECU_WRITE_ENABLED

    print(f"{__app_name__} {__version__}")
    print(f"Real ECU writing: {'ENABLED' if REAL_ECU_WRITE_ENABLED else 'DISABLED (hard-coded)'}")
    print(f"Data directory:   {_paths(args).root}")
    for profile in ENGINES.values():
        print(f"\n{profile.code} — {profile.family_name}")
        print(f"  {profile.displacement_cc} cc, bore x stroke {profile.bore_mm} x {profile.stroke_mm} mm, "
              f"{profile.aspiration}")
        for v in profile.variants:
            print(f"  • {v.applications}: {v.power_kw:.0f} kW / {v.torque_nm:.0f} Nm  [{v.source}]")
    return 0


def cmd_simulate(args) -> int:
    from opentune.datalog import DataLogger
    from opentune.simulation import SimulatedVehicle

    profile = get_profile(args.engine)
    units = UnitSystem(args.units)
    vehicle = SimulatedVehicle(profile, args.scenario)
    for fault in args.fault or []:
        vehicle.inject_fault(fault)
    dt = 1.0 / args.rate
    steps = int(round(args.duration * args.rate))

    client = None
    if args.via_obd:
        from opentune.obd import simulated_client

        client = simulated_client(vehicle, realtime=False)
        channels = client.available_channels()
        print(f"Connected to emulated adapter ({client.elm.version}), {len(channels)} channels over OBD-II")
    else:
        channels = list(CHANNELS)

    logger = None
    if args.log:
        log_path = Path(args.log)
        logger = DataLogger(log_path, channels, profile.code,
                            "simulator-obd" if args.via_obd else "simulator",
                            notes=f"scenario={args.scenario} faults={','.join(args.fault or []) or 'none'}")

    cols = [c for c in LIVE_COLUMNS if c in channels]
    print(f"{'t(s)':>6}" + "".join(f"{_column_label(c, units):>13}" for c in cols))
    print_every = max(1, int(args.rate / args.print_rate)) if args.print_rate > 0 else 0
    try:
        for i in range(steps):
            sample = vehicle.advance(dt)
            if client is not None:
                sample = Sample(sample.timestamp, client.read_channels(channels))
            if logger:
                logger.write(sample)
            if print_every and i % print_every == 0:
                print(f"{sample.timestamp:6.1f}" + "".join(
                    f"{_fmt(c, sample.values.get(c), units):>13}" for c in cols))
            if args.realtime:
                time.sleep(dt)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        if logger:
            logger.close()
            print(f"\nWrote {logger.rows} samples to {logger.path}")
    mem = vehicle.dtc_memory()
    if mem.stored or mem.pending:
        print(f"DTCs — stored: {', '.join(mem.stored) or 'none'}; pending: {', '.join(mem.pending) or 'none'}")
    return 0


def _print_dtcs(report) -> None:
    print(f"MIL (check engine light): {'ON' if report.mil_on else 'off'}")
    for label, items in (("Stored", report.stored), ("Pending", report.pending), ("Permanent", report.permanent)):
        print(f"{label} ({len(items)}):")
        for dtc in items:
            print(f"  {dtc.code}  {dtc.description}")


def cmd_dtc(args) -> int:
    from opentune.obd import ObdClient, SerialTransport, simulated_client

    if args.port:
        client = ObdClient(SerialTransport(args.port, args.baud))
        client.connect()
        print(f"Adapter: {client.elm.version}  protocol {client.elm.protocol}")
    else:
        from opentune.simulation import SimulatedVehicle

        vehicle = SimulatedVehicle(get_profile(args.engine), args.scenario)
        for fault in args.fault or []:
            vehicle.inject_fault(fault)
        vehicle.advance(args.run_seconds)
        client = simulated_client(vehicle, realtime=False)
        print(f"Simulated {args.engine}, ran '{args.scenario}' for {args.run_seconds:.0f} s "
              f"with faults: {', '.join(args.fault or []) or 'none'}")
    try:
        print(f"VIN: {client.read_vin() or 'not reported'}")
        report = client.read_dtcs()
        _print_dtcs(report)
        if args.clear:
            if args.port and not args.yes:
                answer = input("Clear DTCs? This also resets readiness monitors. Type 'yes': ")
                if answer.strip().lower() != "yes":
                    print("Not cleared.")
                    return 0
            client.clear_dtcs()
            print("\nDTCs cleared. Re-reading:")
            _print_dtcs(client.read_dtcs())
    finally:
        client.close()
    return 0


def cmd_scan(args) -> int:
    from opentune.obd import ObdClient, SerialTransport, list_serial_ports

    if args.list_ports:
        ports = list_serial_ports()
        print("\n".join(ports) if ports else "No serial ports found (is pyserial installed?)")
        return 0
    if not args.port:
        print("error: --port is required (use --list-ports to see candidates)", file=sys.stderr)
        return 2
    client = ObdClient(SerialTransport(args.port, args.baud))
    client.connect()
    try:
        print(f"Adapter: {client.elm.version}  protocol {client.elm.protocol}")
        print(f"VIN: {client.read_vin() or 'not reported'}")
        channels = client.available_channels()
        values = client.read_channels(channels)
        units = UnitSystem(args.units)
        for key in channels:
            if key in values:
                ch = CHANNELS[key]
                print(f"  {ch.name:28s} {_fmt(key, values[key], units):>10} {display_unit(ch.unit, units)}")
    finally:
        client.close()
    return 0


def cmd_analyze(args) -> int:
    from opentune.analysis import (
        LocalModelClient,
        LocalModelConfig,
        LocalModelError,
        analyze_samples,
        narrate,
        to_dict,
        to_markdown,
    )
    from opentune.datalog import read_log

    log = read_log(args.log)
    profile = get_profile(args.engine or log.engine)
    report = analyze_samples(log.samples, profile, log.metadata.get("source", ""))
    if args.local_model:
        backend, _, model = args.local_model.partition(":")
        config = LocalModelConfig(backend=backend, model=model or "llama3.2",
                                  base_url=args.model_url or ("http://127.0.0.1:11434" if backend == "ollama"
                                                              else "http://127.0.0.1:8080"))
        try:
            narrate(report, LocalModelClient(config))
        except LocalModelError as exc:
            print(f"(local model unavailable: {exc}) — showing rule-based analysis only\n", file=sys.stderr)
    if args.json:
        print(json.dumps(to_dict(report), indent=2, default=str))
    else:
        print(to_markdown(report))
    if args.output:
        Path(args.output).write_text(to_markdown(report), encoding="utf-8")
    return 0 if not report.findings_by_severity("critical") or not args.fail_on_critical else 1


def cmd_cal(args) -> int:
    from opentune.calibration import CalibrationLibrary, default_calibration

    lib = CalibrationLibrary(_paths(args).calibrations)
    if args.cal_cmd == "list":
        records = lib.list(args.engine)
        if not records:
            print("Library is empty. Try: opentune cal new-stock --engine B58")
        for r in records:
            print(f"{r.id}  {r.engine}  {r.format:6s} {r.size:7d} B  {r.short_hash}  {r.origin:18s} {r.name}")
    elif args.cal_cmd == "new-stock":
        rec = lib.add_simcal(default_calibration(get_profile(args.engine)), f"{args.engine} simulator stock")
        print(f"Created {rec.id}: {rec.name} ({rec.size} bytes)")
    elif args.cal_cmd == "import":
        rec = lib.import_file(args.path, args.name, args.engine, notes=args.notes)
        print(f"Imported {rec.id}: {rec.name} sha256={rec.sha256}")
    elif args.cal_cmd == "show":
        rec = lib.get(args.id)
        for key, value in vars(rec).items():
            print(f"{key:16s} {value}")
        print(f"{'integrity':16s} {'OK' if lib.verify(rec.id) else 'FAILED'}")
        if rec.format == "otsim":
            cal = lib.load_simcal(rec.id)
            for t in cal.tables.values():
                print(f"  table {t.name:14s} {t.shape[0]}x{t.shape[1]} {t.unit}")
    elif args.cal_cmd == "diff":
        diff = lib.compare(args.a, args.b)
        print(f"sizes {diff.size_a} vs {diff.size_b}; {diff.changed_bytes} byte(s) differ in "
              f"{len(diff.regions)} region(s)")
        for region in diff.regions[:50]:
            print(f"  0x{region.start:06X}-0x{region.end:06X} ({region.length} B)")
    elif args.cal_cmd == "export":
        print(f"Exported to {lib.export(args.id, args.dest)}")
    elif args.cal_cmd == "remove":
        lib.remove(args.id)
        print(f"Removed {args.id}")
    return 0


def cmd_flash_sim(args) -> int:
    from opentune.calibration import CalibrationLibrary, default_calibration
    from opentune.ecu import ProgrammingSession, SimulatedEcu
    from opentune.simulation import SimulatedVehicle

    profile = get_profile(args.engine)
    lib = CalibrationLibrary(_paths(args).calibrations)
    vehicle = SimulatedVehicle(profile, "idle")
    vehicle.set_engine_running(False)
    ecu = SimulatedEcu(profile, vehicle)
    if args.power_loss is not None:
        ecu.fail_at_fraction = args.power_loss / 100.0
    if args.voltage is not None:
        ecu.supply_voltage_override = args.voltage
    if args.id:
        image = lib.read_bytes(args.id)
    else:
        cal = default_calibration(profile, version=f"SIM-{profile.code}-DEMO")
        image = cal.to_bytes()
    print(f"Target: {ecu.identify().target}  (software {ecu.identify().software_version})")
    session = ProgrammingSession(ecu, lib)
    last = [-1]

    def progress(fraction: float, message: str) -> None:
        pct = int(fraction * 100)
        if pct // 10 != last[0] // 10 or not message.startswith("Transferred"):
            print(f"[{pct:3d}%] {message}")
            last[0] = pct

    result = session.run(image, progress)
    print(f"\nResult: {'SUCCESS' if result.success else 'FAILED'} — {result.message}")
    print(f"ECU state: {result.ecu_state.value}; software now {ecu.identify().software_version}")
    if result.backup_id:
        print(f"Backup stored in library as {result.backup_id}")
    if not result.success and args.recover:
        print("\nAttempting recovery flash...")
        ecu.fail_at_fraction = None
        result = ProgrammingSession(ecu, lib).run(image, progress)
        print(f"Recovery: {'SUCCESS' if result.success else 'FAILED'} — {result.message}")
    return 0 if result.success else 1


def cmd_selftest(args) -> int:
    """End-to-end pipeline check that needs no car, no GUI and no network."""
    from opentune.analysis import analyze_samples, to_markdown
    from opentune.calibration import CalibrationLibrary, default_calibration
    from opentune.datalog import DataLogger, read_log
    from opentune.ecu import EcuWriteBlockedError, ProgrammingSession, SimulatedEcu, guard_write
    from opentune.obd import simulated_client
    from opentune.simulation import SimulatedVehicle

    ok = True

    def check(label: str, condition: bool) -> None:
        nonlocal ok
        ok &= condition
        print(f"  [{'PASS' if condition else 'FAIL'}] {label}")

    with tempfile.TemporaryDirectory() as tmp:
        for code in ENGINES:
            profile = get_profile(code)
            print(f"{code}:")
            vehicle = SimulatedVehicle(profile, "wot_pull")
            vehicle.inject_fault("boost_leak")
            client = simulated_client(vehicle, realtime=False)
            log_path = Path(tmp) / f"{code}.otlog.csv"
            channels = client.available_channels()
            with DataLogger(log_path, channels, code, "simulator-obd") as logger:
                for _ in range(400):
                    vehicle.advance(0.05)
                    logger.write(Sample(vehicle.sample().timestamp, client.read_channels(channels)))
            log = read_log(log_path)
            check("OBD-II emulator → logger → reader round trip", len(log.samples) == 400)
            report = analyze_samples(log.samples, profile, "simulator-obd")
            check("analysis produced a report", bool(to_markdown(report)))
            dtcs = client.read_dtcs()
            check("underboost DTC P0299 detected", any(d.code == "P0299" for d in dtcs.stored))
            check(f"VIN read ({client.read_vin()})", client.read_vin() is not None)

            lib = CalibrationLibrary(Path(tmp) / f"cal-{code}")
            stock = default_calibration(profile)
            rec = lib.add_simcal(stock, "stock")
            vehicle.set_engine_running(False)
            ecu = SimulatedEcu(profile, vehicle)
            result = ProgrammingSession(ecu, lib).run(lib.read_bytes(rec.id))
            check("simulated ECU programming", result.success)

            class FakeRealEcu:
                is_simulated = True  # lying about it must not help

            try:
                guard_write(FakeRealEcu())
                check("real ECU writes blocked", False)
            except EcuWriteBlockedError:
                check("real ECU writes blocked", True)
    print("\nSELFTEST", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


def cmd_gui(args) -> int:
    try:
        from opentune.gui.app import main as gui_main
    except ImportError as exc:
        print(f"GUI unavailable ({exc}). Install with: pip install PySide6-Essentials", file=sys.stderr)
        return 1
    return gui_main([])


# ---------------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    from opentune.simulation import FAULTS, SCENARIOS

    p = argparse.ArgumentParser(prog="opentune", description=f"{__app_name__} {__version__}")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--data-dir", help="override the data directory (default: per-user app data)")
    sub = p.add_subparsers(dest="command")

    sub.add_parser("gui", help="launch the desktop application").set_defaults(func=cmd_gui)
    sub.add_parser("info", help="version, safety status and engine profiles").set_defaults(func=cmd_info)
    sub.add_parser("selftest", help="end-to-end check without a car").set_defaults(func=cmd_selftest)

    engines = sorted(ENGINES)
    s = sub.add_parser("simulate", help="run the engine simulator (cloud/CLI simulation mode)")
    s.add_argument("--engine", default="B58", type=str.upper, choices=engines)
    s.add_argument("--scenario", default="wot_pull", choices=[k for k in SCENARIOS if k != "manual"])
    s.add_argument("--duration", type=float, default=20.0, help="seconds of simulated time")
    s.add_argument("--rate", type=float, default=20.0, help="samples per second")
    s.add_argument("--print-rate", type=float, default=2.0, help="console lines per second (0 = quiet)")
    s.add_argument("--fault", action="append", choices=sorted(FAULTS), help="inject a fault (repeatable)")
    s.add_argument("--log", help="write a CSV datalog to this path")
    s.add_argument("--via-obd", action="store_true", help="read data through the ELM327 emulator")
    s.add_argument("--realtime", action="store_true", help="pace output in real time")
    s.add_argument("--units", default="metric", choices=[u.value for u in UnitSystem])
    s.set_defaults(func=cmd_simulate)

    d = sub.add_parser("dtc", help="read (and optionally clear) diagnostic trouble codes")
    d.add_argument("--engine", default="B58", type=str.upper, choices=engines)
    d.add_argument("--scenario", default="wot_pull", choices=[k for k in SCENARIOS if k != "manual"])
    d.add_argument("--fault", action="append", choices=sorted(FAULTS))
    d.add_argument("--run-seconds", type=float, default=30.0)
    d.add_argument("--port", help="real ELM327 serial port (omit for the simulator)")
    d.add_argument("--baud", type=int, default=38400)
    d.add_argument("--clear", action="store_true", help="clear DTCs after reading (Mode 04)")
    d.add_argument("--yes", action="store_true", help="do not ask for confirmation when clearing")
    d.set_defaults(func=cmd_dtc)

    sc = sub.add_parser("scan", help="read-only scan of a real car via an ELM327 adapter")
    sc.add_argument("--port")
    sc.add_argument("--baud", type=int, default=38400)
    sc.add_argument("--list-ports", action="store_true")
    sc.add_argument("--units", default="imperial", choices=[u.value for u in UnitSystem])
    sc.set_defaults(func=cmd_scan)

    a = sub.add_parser("analyze", help="analyse a datalog")
    a.add_argument("log")
    a.add_argument("--engine", type=str.upper, choices=engines)
    a.add_argument("--local-model", help="e.g. ollama:llama3.2 or openai-compatible:local-model")
    a.add_argument("--model-url", help="local model server URL (localhost only)")
    a.add_argument("--json", action="store_true")
    a.add_argument("--output", help="also write the Markdown report here")
    a.add_argument("--fail-on-critical", action="store_true", help="exit 1 if critical findings exist")
    a.set_defaults(func=cmd_analyze)

    c = sub.add_parser("cal", help="calibration library")
    csub = c.add_subparsers(dest="cal_cmd", required=True)
    cl = csub.add_parser("list")
    cl.add_argument("--engine", type=str.upper)
    cn = csub.add_parser("new-stock", help="create the simulator's stock OTSIM calibration")
    cn.add_argument("--engine", default="B58", type=str.upper, choices=engines)
    ci = csub.add_parser("import")
    ci.add_argument("path")
    ci.add_argument("--name", default="")
    ci.add_argument("--engine", default="B58", type=str.upper, choices=engines)
    ci.add_argument("--notes", default="")
    cs = csub.add_parser("show")
    cs.add_argument("id")
    cd = csub.add_parser("diff")
    cd.add_argument("a")
    cd.add_argument("b")
    ce = csub.add_parser("export")
    ce.add_argument("id")
    ce.add_argument("dest")
    cr = csub.add_parser("remove")
    cr.add_argument("id")
    c.set_defaults(func=cmd_cal)

    f = sub.add_parser("flash-sim", help="program the SIMULATED ECU (real ECUs are never written)")
    f.add_argument("--engine", default="B58", type=str.upper, choices=engines)
    f.add_argument("--id", help="library calibration id to flash (default: generated demo image)")
    f.add_argument("--power-loss", type=float, help="simulate power loss at this percent of transfer")
    f.add_argument("--voltage", type=float, help="override simulated supply voltage")
    f.add_argument("--recover", action="store_true", help="attempt a recovery flash after a failure")
    f.set_defaults(func=cmd_flash_sim)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return int(args.func(args) or 0)
    except (KeyError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
