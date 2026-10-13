"""Live simulation session: runs the closed-loop simulation in real time and streams frames.

One background thread advances the simulation by ``1 / rate_hz`` simulated
seconds per tick (times ``speed``), then sleeps until the next wall-clock tick.
Frames go to every WebSocket subscriber through its asyncio queue; slow
subscribers drop frames instead of slowing the simulation down.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from typing import Any

from opentune.lab.calibration.tables import Calibration
from opentune.lab.dyno.runs import CHANNEL_BY_ID
from opentune.lab.ecu.sensors import FAULT_TYPES, SENSOR_SPECS
from opentune.lab.physics.config import EngineConfig
from opentune.lab.physics.plant import Ambient
from opentune.lab.sim.loads import DynoLoad, FreeRevLoad, VehicleLoad
from opentune.lab.sim.session import Frame, Simulation

LIVE_CHANNELS = (
    "rpm", "torque_nm", "power_kw", "map_kpa", "boost_kpa", "boost_target_kpa", "throttle_pct", "wastegate_pct",
    "turbo_krpm", "lambda", "lambda_target", "spark_deg", "knock_retard_deg", "knock_intensity", "iat_c",
    "coolant_c", "oil_c", "egt_c", "charge_loop_c", "fuel_lph", "bsfc_g_kwh", "ve_pct", "load_pct",
    "peak_pressure_bar", "intake_cam_deg", "exhaust_cam_deg", "torque_request_nm", "torque_limit_nm",
    "absorber_torque_nm", "rail_bar",
)


class LiveError(RuntimeError):
    pass


class LiveSession:
    def __init__(self, project_id: str, config: EngineConfig, calibration: Calibration, calibration_label: str,
                 rate_hz: float = 20.0, speed: float = 1.0, warm: bool = True, ambient_temp_c: float = 25.0,
                 ambient_pressure_kpa: float = 101.325, seed: int = 1) -> None:
        self.project_id = project_id
        self.config = config
        self.rate_hz = rate_hz
        self.speed = speed
        self.calibration_label = calibration_label
        self.calibration_hash = calibration.content_hash()
        ambient = Ambient(pressure=ambient_pressure_kpa * 1000.0, temperature=ambient_temp_c + 273.15)
        self.sim = Simulation(config, calibration.copy(), ambient=ambient, seed=seed)
        if warm:
            self.sim.warm_up(90.0)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        self._sub_lock = threading.Lock()
        self.history: deque[dict] = deque(maxlen=int(rate_hz * 120))
        self.paused = False
        self._starting_since: float | None = None
        self.last_message: dict | None = None
        self.load_kind = "neutral"
        self.events: deque[dict] = deque(maxlen=200)

    # ------------------------------------------------------------------ lifecycle
    def start_thread(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="lab-live", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _loop(self) -> None:
        period = 1.0 / self.rate_hz
        next_t = time.monotonic()
        while not self._stop.is_set():
            if not self.paused:
                self.tick()
            next_t += period
            delay = next_t - time.monotonic()
            if delay < -1.0:  # fell behind (debugger, slow machine): resynchronise
                next_t = time.monotonic()
            elif delay > 0:
                self._stop.wait(delay)

    def tick(self) -> dict:
        """Advance one publish period (also used directly by tests, without the thread)."""
        with self._lock:
            frame = self.sim.run(self.speed / self.rate_hz)
            if self._starting_since is not None:
                if frame.ecu.state == "RUNNING" and frame.plant.rpm > 500.0:
                    self.sim.driver.start = False
                    self._starting_since = None
                    self._event("Engine running")
                elif self.sim.time - self._starting_since > 5.0:
                    self.sim.driver.start = False
                    self._starting_since = None
                    self._event("Start aborted after 5 s of cranking")
            msg = self._message(frame)
        self.history.append(msg)
        self.last_message = msg
        self._publish(msg)
        return msg

    # ------------------------------------------------------------------ messages
    def _message(self, fr: Frame) -> dict:
        ch = {}
        for cid in LIVE_CHANNELS:
            v = CHANNEL_BY_ID[cid].extract(fr)
            ch[cid] = round(float(v), 3) if v == v and abs(v) != float("inf") else None
        e = fr.ecu
        ch.update({"pedal_pct": self.sim.driver.pedal, "vss_kmh": round(fr.vehicle_speed_kmh, 2),
                   "stft_pct": round(e.stft_pct, 2), "ltft_pct": round(e.ltft_pct, 2),
                   "idle_target_rpm": round(e.idle_target_rpm, 1), "injector_pw_ms": round(e.injector_pw_ms, 3)})
        return {
            "type": "frame", "t": round(self.sim.time, 3), "ch": ch,
            "ecu": {"state": e.state, "mode": e.mode, "mil": e.mil, "limp": e.limp, "dtcs": list(e.dtcs),
                    "protections": list(e.protections), "closed_loop": e.closed_loop,
                    "torque_limited_by": e.torque_limited_by},
            "driver": {"ignition": self.sim.driver.ignition, "pedal": self.sim.driver.pedal,
                       "starting": self._starting_since is not None},
            "load": self.load_kind,
        }

    def _event(self, text: str) -> None:
        ev = {"t": round(self.sim.time, 3), "text": text}
        self.events.append(ev)
        self._publish({"type": "event", **ev})

    # ------------------------------------------------------------------ pub/sub
    def subscribe(self, loop: asyncio.AbstractEventLoop, maxsize: int = 64) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        with self._sub_lock:
            self._subscribers.append((loop, q))
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._sub_lock:
            self._subscribers = [(lp, qq) for lp, qq in self._subscribers if qq is not q]

    @staticmethod
    def _put(q: asyncio.Queue, msg: dict) -> None:
        if q.full():
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
        q.put_nowait(msg)

    def _publish(self, msg: dict) -> None:
        with self._sub_lock:
            subs = list(self._subscribers)
        for loop, q in subs:
            try:
                loop.call_soon_threadsafe(self._put, q, msg)
            except RuntimeError:  # loop closed
                self.unsubscribe(q)

    # ------------------------------------------------------------------ controls
    def ignition(self, on: bool) -> None:
        with self._lock:
            self.sim.driver.ignition = on
            if not on:
                self.sim.driver.start = False
                self._starting_since = None
                self.sim.driver.pedal = 0.0
        self._event("Ignition on" if on else "Ignition off")

    def crank(self) -> None:
        with self._lock:
            if self.sim.ecu.state == "RUNNING":
                raise LiveError("Engine is already running")
            self.sim.driver.ignition = True
            self.sim.driver.start = True
            self._starting_since = self.sim.time
        self._event("Cranking")

    def stop_engine(self) -> None:
        self.ignition(False)

    def set_pedal(self, pedal: float) -> None:
        if not 0.0 <= pedal <= 100.0:
            raise LiveError("Pedal must be 0–100 %")
        with self._lock:
            self.sim.driver.pedal = float(pedal)

    def set_load(self, kind: str, target_rpm: float | None = None, gear_ratio: float | None = None) -> None:
        with self._lock:
            if kind == "neutral":
                load = FreeRevLoad()
            elif kind == "dyno":
                rpm = target_rpm if target_rpm is not None else max(self.sim.plant.state.omega * 9.549, 1000.0)
                if not 800.0 <= rpm <= self.config.limits.max_rpm:
                    raise LiveError("Dyno target out of range")
                load = DynoLoad(target_rpm=rpm)
            elif kind == "vehicle":
                load = VehicleLoad(gear_ratio=gear_ratio or 2.143)
                load.speed_ms = self.sim.plant.state.omega / load.ratio * load.wheel_radius_m
            else:
                raise LiveError(f"Unknown load {kind!r}")
            self.sim.set_load(load)
            self.load_kind = kind
        self._event(f"Load: {kind}" + (f" @ {target_rpm:.0f} rpm" if kind == "dyno" and target_rpm else ""))

    def set_dyno_target(self, rpm: float) -> None:
        with self._lock:
            if not isinstance(self.sim.load, DynoLoad):
                raise LiveError("Dyno load not active")
            if not 800.0 <= rpm <= self.config.limits.max_rpm:
                raise LiveError("Dyno target out of range")
            self.sim.load.target_rpm = rpm

    def set_speed(self, speed: float) -> None:
        if not 0.1 <= speed <= 4.0:
            raise LiveError("Simulation speed must be 0.1–4×")
        self.speed = speed

    def inject_fault(self, sensor: str, kind: str, magnitude: float = 0.0) -> None:
        if sensor not in SENSOR_SPECS:
            raise LiveError(f"Unknown sensor {sensor!r}")
        if kind not in FAULT_TYPES:
            raise LiveError(f"Unknown fault {kind!r}")
        with self._lock:
            self.sim.sensors.inject(sensor, kind, magnitude)
        self._event(f"Fault injected: {sensor} {kind}" + (f" ({magnitude:g})" if magnitude else ""))

    def clear_faults(self, sensor: str | None = None) -> None:
        with self._lock:
            self.sim.sensors.clear(sensor)
        self._event(f"Faults cleared: {sensor or 'all'}")

    def clear_dtcs(self) -> None:
        with self._lock:
            self.sim.ecu.clear_dtcs()
        self._event("DTCs cleared")

    def set_ambient(self, temp_c: float, pressure_kpa: float) -> None:
        if not (-30.0 <= temp_c <= 50.0 and 60.0 <= pressure_kpa <= 110.0):
            raise LiveError("Ambient out of range (−30…50 °C, 60–110 kPa)")
        with self._lock:
            self.sim.plant.ambient.temperature = temp_c + 273.15
            self.sim.plant.ambient.pressure = pressure_kpa * 1000.0
        self._event(f"Ambient: {temp_c:.1f} °C, {pressure_kpa:.1f} kPa")

    def load_calibration(self, calibration: Calibration, label: str) -> None:
        """Swap the calibration the virtual ECU runs (called after a simulated flash)."""
        with self._lock:
            self.sim.set_calibration(calibration.copy())
            self.calibration_label = label
            self.calibration_hash = calibration.content_hash()
        self._event(f"Virtual ECU now running: {label}")

    # ------------------------------------------------------------------ queries
    def status(self) -> dict[str, Any]:
        with self._lock:
            ecu = self.sim.ecu
            return {
                "project_id": self.project_id, "engine": self.config.name, "engine_id": self.config.id,
                "calibration": self.calibration_label, "calibration_hash": self.calibration_hash,
                "sim_time": round(self.sim.time, 3), "speed": self.speed, "rate_hz": self.rate_hz,
                "paused": self.paused, "state": ecu.state, "load": self.load_kind,
                "faults": {k: v.to_dict() for k, v in self.sim.sensors.faults.items()},
                "ambient": {"temp_c": round(self.sim.plant.ambient.temperature - 273.15, 2),
                            "pressure_kpa": round(self.sim.plant.ambient.pressure / 1000.0, 3)},
                "events": list(self.events)[-20:],
            }

    def dtc_report(self) -> list[dict]:
        with self._lock:
            return self.sim.ecu.dtc_report()

    def sensors(self) -> list[dict]:
        with self._lock:
            readings = dict(self.sim._last_readings)
        out = []
        for sid, spec in SENSOR_SPECS.items():
            r = readings.get(sid)
            out.append({"id": sid, "name": spec.name, "unit": spec.unit,
                        "volts": round(r.volts, 3) if r is not None and r.volts is not None else None,
                        "value": round(r.value, 3) if r is not None else None,
                        "true_value": round(r.true_value, 3) if r is not None else None,
                        "fault": r.fault if r is not None else None,
                        "dtc_low": spec.dtc_low, "dtc_high": spec.dtc_high})
        return out
