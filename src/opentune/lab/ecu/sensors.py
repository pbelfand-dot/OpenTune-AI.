"""Virtual sensors: physical value → electrical signal → (ECU) decoded value.

Each analogue sensor has a transfer function to a 0–5 V signal so circuit
faults (open/short) produce the same out-of-range voltages a real ECU would see.
Transfer functions are typical generic characteristics (assumptions), e.g. a
3-bar MAP sensor spanning 20–300 kPa over 0.5–4.5 V and an NTC thermistor with a
2.49 kΩ pull-up.

Fault injection: ``stuck``, ``open_circuit``, ``short_ground``, ``short_supply``,
``offset``, ``noise``, ``drift``, ``delay``, ``intermittent``.
"""

from __future__ import annotations

import math
import random
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

V_SUPPLY = 5.0
V_VALID_MIN = 0.2
V_VALID_MAX = 4.8
PULL_UP_OHMS = 2490.0
NTC_R25 = 2500.0
NTC_BETA = 3435.0


def linear_encode(lo_value: float, hi_value: float, lo_v: float = 0.5, hi_v: float = 4.5) -> Callable[[float], float]:
    return lambda x: lo_v + (x - lo_value) * (hi_v - lo_v) / (hi_value - lo_value)


def linear_decode(lo_value: float, hi_value: float, lo_v: float = 0.5, hi_v: float = 4.5) -> Callable[[float], float]:
    return lambda v: lo_value + (v - lo_v) * (hi_value - lo_value) / (hi_v - lo_v)


def ntc_encode(t_c: float) -> float:
    t_k = t_c + 273.15
    r = NTC_R25 * math.exp(NTC_BETA * (1.0 / t_k - 1.0 / 298.15))
    return V_SUPPLY * r / (r + PULL_UP_OHMS)


def ntc_decode(volts: float) -> float:
    volts = min(max(volts, 0.01), V_SUPPLY - 0.01)
    r = PULL_UP_OHMS * volts / (V_SUPPLY - volts)
    inv_t = 1.0 / 298.15 + math.log(r / NTC_R25) / NTC_BETA
    return 1.0 / inv_t - 273.15


@dataclass(frozen=True)
class SensorSpec:
    id: str
    name: str
    unit: str
    encode: Callable[[float], float] | None  # physical → volts (None: digital/derived sensor)
    decode: Callable[[float], float] | None  # volts → physical (the ECU side)
    noise: float = 0.0  # 1-σ noise in engineering units
    lag_s: float = 0.0  # first-order sensor time constant
    dtc_low: str | None = None
    dtc_high: str | None = None
    open_circuit_volts: float = 0.05  # where an open circuit pulls the signal
    saturate: tuple[float, float] | None = None  # output saturates at these physical values (not a fault)


SENSOR_SPECS: dict[str, SensorSpec] = {s.id: s for s in (
    SensorSpec("map", "Manifold absolute pressure", "kPa", linear_encode(20, 300), linear_decode(20, 300),
               noise=0.3, lag_s=0.004, dtc_low="P0107", dtc_high="P0108"),
    SensorSpec("boost", "Boost pressure (pre-throttle)", "kPa", linear_encode(20, 300), linear_decode(20, 300),
               noise=0.3, lag_s=0.004),
    SensorSpec("iat", "Intake air temperature", "°C", ntc_encode, ntc_decode, noise=0.05, lag_s=1.5,
               dtc_low="P0112", dtc_high="P0113", open_circuit_volts=4.95),
    SensorSpec("ect", "Engine coolant temperature", "°C", ntc_encode, ntc_decode, noise=0.05, lag_s=2.0,
               dtc_low="P0117", dtc_high="P0118", open_circuit_volts=4.95),
    SensorSpec("oil_temp", "Engine oil temperature", "°C", ntc_encode, ntc_decode, noise=0.05, lag_s=2.0,
               open_circuit_volts=4.95),
    SensorSpec("tps", "Throttle position", "%", linear_encode(0, 100), linear_decode(0, 100), noise=0.05,
               dtc_low="P0122", dtc_high="P0123"),
    SensorSpec("pedal", "Accelerator pedal position", "%", linear_encode(0, 100), linear_decode(0, 100), noise=0.05),
    SensorSpec("lambda", "Wideband lambda (bank 1 sensor 1)", "λ", linear_encode(0.68, 1.36),
               linear_decode(0.68, 1.36), noise=0.003, lag_s=0.08, dtc_low="P0131", dtc_high="P0132",
               saturate=(0.68, 1.36)),
    SensorSpec("rail_pressure", "Fuel rail pressure", "bar", linear_encode(0, 400), linear_decode(0, 400),
               noise=0.5, lag_s=0.01),
    SensorSpec("knock", "Knock sensor (processed intensity)", "", None, None, noise=0.03,
               dtc_low="P0327", dtc_high="P0328"),
    SensorSpec("crank", "Crankshaft speed", "rpm", None, None, noise=1.5, dtc_low="P0335"),
    SensorSpec("cam_intake", "Intake cam position (advance)", "°", None, None, noise=0.2, dtc_low="P0340"),
    SensorSpec("cam_exhaust", "Exhaust cam position (retard)", "°", None, None, noise=0.2),
    SensorSpec("egt", "Exhaust gas temperature (pre-turbine)", "°C", None, None, noise=2.0, lag_s=0.3),
    SensorSpec("vss", "Vehicle speed", "km/h", None, None, noise=0.0),
    SensorSpec("battery", "Battery voltage", "V", None, None, noise=0.02),
)}

FAULT_TYPES = ("stuck", "open_circuit", "short_ground", "short_supply", "offset", "noise", "drift", "delay",
               "intermittent")


@dataclass
class SensorFault:
    kind: str
    magnitude: float = 0.0  # offset/noise (engineering units), drift (units/s), delay (s), intermittent (probability)
    started: float = 0.0

    def to_dict(self) -> dict:
        return {"kind": self.kind, "magnitude": self.magnitude}


@dataclass
class SensorReading:
    id: str
    volts: float | None  # None for digital sensors
    value: float  # what the ECU decodes
    true_value: float  # physical truth (for display and analysis only)
    fault: str | None = None

    def to_dict(self) -> dict:
        return {"id": self.id, "volts": self.volts, "value": self.value, "true_value": self.true_value,
                "fault": self.fault}


@dataclass
class SensorBank:
    seed: int = 7
    faults: dict[str, SensorFault] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.rng = random.Random(self.seed)
        self._filtered: dict[str, float] = {}
        self._stuck: dict[str, float] = {}
        self._history: dict[str, deque] = {}
        self._lambda_history: deque = deque()
        self.time = 0.0

    def inject(self, sensor_id: str, kind: str, magnitude: float = 0.0) -> None:
        if sensor_id not in SENSOR_SPECS:
            raise KeyError(f"Unknown sensor {sensor_id!r}")
        if kind not in FAULT_TYPES:
            raise KeyError(f"Unknown fault {kind!r}. Options: {', '.join(FAULT_TYPES)}")
        self.faults[sensor_id] = SensorFault(kind, magnitude, self.time)
        self._stuck.pop(sensor_id, None)

    def clear(self, sensor_id: str | None = None) -> None:
        if sensor_id is None:
            self.faults.clear()
            self._stuck.clear()
        else:
            self.faults.pop(sensor_id, None)
            self._stuck.pop(sensor_id, None)

    def _lambda_truth(self, lam: float, rpm: float, dt: float) -> float:
        """Exhaust transport delay ≈ two engine revolutions + 30 ms (assumption)."""
        lam = 1.40 if not math.isfinite(lam) else min(lam, 1.40)
        self._lambda_history.append((self.time, lam))
        delay = 0.03 + (120.0 / rpm if rpm > 60.0 else 1.0)
        while len(self._lambda_history) > 1 and self._lambda_history[1][0] <= self.time - delay:
            self._lambda_history.popleft()
        return self._lambda_history[0][1]

    def sample(self, truth: dict[str, float], dt: float) -> dict[str, SensorReading]:
        """Convert physical truth values into sensor readings (with lag, noise and faults)."""
        self.time += dt
        truth = dict(truth)
        truth["lambda"] = self._lambda_truth(truth.get("lambda", 1.0), truth.get("crank", 0.0), dt)
        out: dict[str, SensorReading] = {}
        for sid, spec in SENSOR_SPECS.items():
            if sid not in truth:
                continue
            true_value = truth[sid]
            value = true_value
            if spec.lag_s > 0.0:
                prev = self._filtered.get(sid, true_value)
                value = prev + (true_value - prev) * (1.0 - math.exp(-dt / spec.lag_s))
                self._filtered[sid] = value
            fault = self.faults.get(sid)
            if fault and fault.kind == "delay":
                hist = self._history.setdefault(sid, deque())
                hist.append((self.time, value))
                while len(hist) > 1 and hist[1][0] <= self.time - fault.magnitude:
                    hist.popleft()
                value = hist[0][1]
            if spec.noise:
                value += self.rng.gauss(0.0, spec.noise)
            if fault:
                if fault.kind == "offset":
                    value += fault.magnitude
                elif fault.kind == "noise":
                    value += self.rng.gauss(0.0, fault.magnitude)
                elif fault.kind == "drift":
                    value += fault.magnitude * (self.time - fault.started)
                elif fault.kind == "stuck":
                    value = self._stuck.setdefault(sid, value)
            if sid == "knock":
                value = max(0.0, value) + 0.04  # background vibration floor seen by a healthy sensor
                if fault and fault.kind in ("open_circuit", "short_ground"):
                    value = 0.0
                out[sid] = SensorReading(sid, None, value, true_value, fault.kind if fault else None)
                continue
            if sid == "crank":
                if fault and fault.kind in ("open_circuit", "short_ground", "intermittent") and (
                        fault.kind != "intermittent" or self.rng.random() < fault.magnitude):
                    value = 0.0
                out[sid] = SensorReading(sid, None, max(0.0, value), true_value, fault.kind if fault else None)
                continue
            if spec.encode is None:
                if fault and fault.kind in ("open_circuit", "short_ground"):
                    value = 0.0
                out[sid] = SensorReading(sid, None, value, true_value, fault.kind if fault else None)
                continue
            if spec.saturate is not None:  # e.g. a wideband controller pins its output at the range ends
                value = min(max(value, spec.saturate[0]), spec.saturate[1])
            volts = spec.encode(value)
            if fault:
                if fault.kind == "open_circuit":
                    volts = spec.open_circuit_volts
                elif fault.kind == "short_ground":
                    volts = 0.02
                elif fault.kind == "short_supply":
                    volts = 4.98
                elif fault.kind == "intermittent" and self.rng.random() < max(0.05, fault.magnitude):
                    volts = 0.02
            volts = min(max(volts, 0.0), V_SUPPLY)
            out[sid] = SensorReading(sid, volts, spec.decode(volts), true_value, fault.kind if fault else None)
        return out
