"""Closed-loop simulation: engine plant ↔ sensors ↔ virtual ECU ↔ load.

The plant integrates at ``plant_dt`` (default 1 ms); the ECU task runs every
``ecu_period`` (default 5 ms) on sampled sensor signals; actuator commands are
held between ECU tasks (zero-order hold), as on a real controller.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from opentune.lab.calibration.tables import Calibration
from opentune.lab.ecu.controller import EcuOutputs, VirtualEcu
from opentune.lab.ecu.sensors import SensorBank
from opentune.lab.physics.config import EngineConfig
from opentune.lab.physics.plant import Ambient, EnginePlant, PlantInputs, PlantOutputs
from opentune.lab.sim.loads import FreeRevLoad, Load

TELEMETRY_FIELDS = (
    "time", "rpm", "brake_torque", "brake_power", "p_im", "p_boost", "p_em", "t_im", "t_coolant", "t_oil",
    "t_exhaust_gas", "t_egt_sensor", "t_compressor_out", "turbo_rpm", "lambda_value", "spark_advance",
    "knock_intensity", "m_cylinder", "m_fuel", "fuel_rate_lph", "bsfc_g_kwh", "throttle_pos", "wastegate_pos",
    "intake_cam", "exhaust_cam", "rail_pressure", "imep", "pmep", "fmep", "bmep", "relative_load",
    "volumetric_efficiency", "peak_pressure_est", "compressor_pr", "compressor_eta", "mbt_advance",
    "knock_limit_advance", "t_charge_loop", "surge",
)


@dataclass
class DriverInputs:
    ignition: bool = False
    start: bool = False
    pedal: float = 0.0  # %


@dataclass
class Frame:
    """One telemetry frame: plant truth, ECU outputs, sensor readings."""

    plant: PlantOutputs
    ecu: EcuOutputs
    sensors: dict
    load_torque: float
    vehicle_speed_kmh: float

    def flat(self) -> dict:
        d = {k: getattr(self.plant, k) for k in TELEMETRY_FIELDS}
        d["load_torque"] = self.load_torque
        d["vehicle_speed_kmh"] = self.vehicle_speed_kmh
        e = self.ecu
        d.update({
            "ecu_state": e.state, "ecu_mode": e.mode, "torque_request": e.torque_request,
            "torque_limit": e.torque_limit, "torque_estimate": e.torque_estimate, "boost_target_kpa":
            e.boost_target_kpa, "wastegate_cmd": e.wastegate_cmd, "throttle_cmd": e.throttle_cmd,
            "lambda_target": e.lambda_target, "lambda_measured": e.lambda_measured, "stft_pct": e.stft_pct,
            "ltft_pct": e.ltft_pct, "knock_retard": e.knock_retard, "load_pct": e.load_pct,
            "idle_target_rpm": e.idle_target_rpm, "injector_pw_ms": e.injector_pw_ms, "mil": e.mil,
            "limp": e.limp, "protections": list(e.protections), "dtcs": list(e.dtcs),
            "map_request_kpa": e.map_request_kpa,
        })
        return d


@dataclass
class Simulation:
    config: EngineConfig
    calibration: Calibration
    load: Load = field(default_factory=FreeRevLoad)
    ambient: Ambient = field(default_factory=Ambient)
    seed: int = 1
    plant_dt: float = 0.001
    ecu_period: float = 0.005

    def __post_init__(self) -> None:
        self.plant = EnginePlant(self.config, self.ambient, seed=self.seed)
        self.sensors = SensorBank(seed=self.seed + 101)
        self.ecu = VirtualEcu(self.calibration, self.ecu_period)
        self.driver = DriverInputs()
        self.time = 0.0
        self._ecu_timer = 0.0
        self._inputs = PlantInputs()
        self._last_out: PlantOutputs | None = None
        self._last_ecu = EcuOutputs()
        self._last_readings: dict = {}
        self._load_torque = 0.0

    # ------------------------------------------------------------------ control
    def set_calibration(self, calibration: Calibration) -> None:
        self.calibration = calibration
        self.ecu.reload_calibration(calibration)

    def set_load(self, load: Load) -> None:
        self.load = load

    def warm_up(self, coolant_c: float = 90.0) -> None:
        """Start from a warm, stopped engine (thermal state only)."""
        st = self.plant.state
        st.t_coolant = coolant_c + 273.15
        st.t_oil = coolant_c + 5.0 + 273.15

    def _truth(self, out: PlantOutputs) -> dict[str, float]:
        return {
            "map": out.p_im / 1000.0, "boost": out.p_boost / 1000.0, "iat": out.t_im - 273.15,
            "ect": out.t_coolant - 273.15, "oil_temp": out.t_oil - 273.15, "tps": out.throttle_pos * 100.0,
            "pedal": self.driver.pedal, "lambda": out.lambda_value, "rail_pressure": out.rail_pressure / 1e5,
            "knock": out.knock_intensity, "crank": out.rpm, "cam_intake": out.intake_cam,
            "cam_exhaust": out.exhaust_cam, "egt": out.t_egt_sensor - 273.15,
            "vss": self.load.vehicle_speed_kmh(out.omega), "battery": out.battery_voltage,
        }

    def step(self) -> Frame:
        """Advance one plant step (``plant_dt``)."""
        dt = self.plant_dt
        self._ecu_timer += dt
        if self._ecu_timer >= self.ecu_period - 1e-12 or self._last_out is None:
            self._ecu_timer = 0.0
            if self._last_out is not None:
                readings = self.sensors.sample(self._truth(self._last_out), self.ecu_period)
            else:
                readings = {}
            self._last_readings = readings
            self._last_ecu = self.ecu.step(readings, self.driver.ignition, self.driver.start, self.ecu_period)
            self._inputs = VirtualEcu.to_plant_inputs(self._last_ecu, self.driver.ignition)
        self.plant.ambient.airflow = self.load.airflow()
        self._load_torque = self.load.torque(self._last_out, self.plant.state.omega, dt)
        out = self.plant.step(self._inputs, self._load_torque, dt)
        self._last_out = out
        self.time += dt
        return Frame(out, self._last_ecu, self._last_readings, self._load_torque,
                     self.load.vehicle_speed_kmh(out.omega))

    def run(self, seconds: float, every: float | None = None, callback=None) -> Frame:
        """Run for ``seconds``; optionally call ``callback(frame)`` every ``every`` seconds."""
        steps = int(round(seconds / self.plant_dt))
        stride = max(1, int(round((every or seconds) / self.plant_dt)))
        frame = None
        for i in range(steps):
            frame = self.step()
            if callback is not None and (i + 1) % stride == 0:
                callback(frame)
        assert frame is not None
        return frame

    def start_engine(self, timeout: float = 4.0) -> bool:
        """Turn the key: ignition on, crank until the ECU reports RUNNING (or timeout)."""
        self.driver.ignition = True
        self.driver.start = True
        t = 0.0
        while t < timeout:
            frame = self.step()
            t += self.plant_dt
            if frame.ecu.state == "RUNNING" and frame.plant.rpm > 500:
                self.driver.start = False
                return True
        self.driver.start = False
        return False

    def stop_engine(self) -> None:
        self.driver.ignition = False
        self.driver.start = False
