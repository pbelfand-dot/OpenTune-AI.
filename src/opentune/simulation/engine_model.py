"""Simplified physics model of a turbocharged BMW engine in a rear-drive car.

The goal is plausible, deterministic behaviour for UI development, logging,
analysis and tests — not engineering accuracy. Boost, ignition, lambda and rev
limit come from an OTSIM calibration (:mod:`opentune.calibration.simcal`), so
"tuning" the simulated ECU changes the simulated engine's behaviour.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from opentune.calibration.simcal import SimCalibration, default_calibration
from opentune.core.models import Sample
from opentune.engines.profile import EngineProfile
from opentune.simulation.scenarios import DriverInput, Scenario, make_scenario

R_AIR = 287.05  # J/(kg K)
GASOLINE_DENSITY_G_PER_L = 745.0
STOICH_AFR = 14.7
G = 9.81
AIR_DENSITY_AMBIENT = 1.2

FAULTS: dict[str, str] = {
    "boost_leak": "Boost leak (underboost)",
    "wastegate_stuck": "Stuck wastegate (overboost)",
    "overheat": "Cooling failure (overheat)",
    "lean": "Vacuum leak (lean)",
    "misfire": "Cylinder misfire",
    "knock": "Low-octane fuel (knock)",
    "low_battery": "Weak charging (low voltage)",
}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _approach(current: float, target: float, dt: float, tau: float) -> float:
    """First-order lag toward ``target`` with time constant ``tau`` seconds."""
    if tau <= 0:
        return target
    return current + (target - current) * (1.0 - math.exp(-dt / tau))


@dataclass
class EngineState:
    time: float = 0.0
    engine_running: bool = True
    rpm: float = 0.0
    speed_ms: float = 0.0
    gear: int = 1
    pedal: float = 0.0
    brake: float = 0.0
    throttle: float = 0.0
    boost_bar: float = -0.65
    boost_target_bar: float = -0.65
    boost_target_filtered: float = -0.65  # target with expected spool lag (for diagnostics)
    baro_kpa: float = 101.3
    ambient_c: float = 22.0
    coolant_c: float = 88.0
    oil_c: float = 95.0
    iat_c: float = 30.0
    timing_deg: float = 12.0
    lambda_cmd: float = 1.0
    stft: float = 0.0
    ltft: float = 1.5
    maf_gs: float = 0.0
    rail_bar: float = 50.0
    fuel_rate_lh: float = 0.0
    fuel_level_pct: float = 62.0
    battery_v: float = 14.2
    torque_nm: float = 0.0
    load_pct: float = 0.0
    run_time_s: float = 0.0
    rev_limit: float = 7000.0
    rev_limiter_active: bool = False
    knock_retard_deg: float = 0.0
    shift_cooldown: float = 0.0
    load_avg: float = 0.0
    misfire_cylinder: int | None = None

    @property
    def speed_kmh(self) -> float:
        return self.speed_ms * 3.6

    @property
    def map_kpa(self) -> float:
        return self.baro_kpa + self.boost_bar * 100.0


@dataclass
class SimulatorConfig:
    seed: int = 1234
    ambient_c: float = 22.0
    baro_kpa: float = 101.3
    warm_start: bool = True
    noise: bool = True


class EngineSimulator:
    """Steps the engine/vehicle model forward in time."""

    def __init__(
        self,
        profile: EngineProfile,
        scenario: Scenario | str = "idle",
        calibration: SimCalibration | None = None,
        config: SimulatorConfig | None = None,
    ) -> None:
        self.profile = profile
        self.config = config or SimulatorConfig()
        self.rng = random.Random(self.config.seed)
        self.calibration = calibration or default_calibration(profile)
        self.faults: set[str] = set()
        self.state = self._initial_state()
        self.scenario: Scenario = make_scenario(scenario) if isinstance(scenario, str) else scenario
        self.scenario.reset(self.rng)
        self.last_input = DriverInput()

    # ------------------------------------------------------------------ setup
    def _initial_state(self) -> EngineState:
        cfg = self.config
        warm = cfg.warm_start
        state = EngineState(
            rpm=self.profile.sim.idle_rpm,
            baro_kpa=cfg.baro_kpa,
            ambient_c=cfg.ambient_c,
            coolant_c=88.0 if warm else cfg.ambient_c,
            oil_c=95.0 if warm else cfg.ambient_c,
            iat_c=cfg.ambient_c + 8.0,
        )
        state.rev_limit = self.calibration.rev_limit
        return state

    def set_scenario(self, scenario: Scenario | str) -> None:
        self.scenario = make_scenario(scenario) if isinstance(scenario, str) else scenario
        self.scenario.reset(self.rng)

    def load_calibration(self, calibration: SimCalibration) -> None:
        problems = calibration.validate()
        if problems:
            raise ValueError("Calibration rejected by simulator: " + "; ".join(problems))
        self.calibration = calibration
        self.state.rev_limit = calibration.rev_limit

    def inject_fault(self, fault: str) -> None:
        if fault not in FAULTS:
            raise KeyError(f"Unknown fault {fault!r}. Options: {', '.join(FAULTS)}")
        self.faults.add(fault)
        if fault == "misfire":
            self.state.misfire_cylinder = min(3, self.profile.cylinders)

    def clear_fault(self, fault: str) -> None:
        self.faults.discard(fault)
        if fault == "misfire":
            self.state.misfire_cylinder = None

    def clear_faults(self) -> None:
        for fault in list(self.faults):
            self.clear_fault(fault)

    def set_engine_running(self, running: bool) -> None:
        self.state.engine_running = running
        if not running:
            self.state.rpm = 0.0
            self.state.speed_ms = 0.0
            self.state.torque_nm = 0.0
            self.state.boost_bar = 0.0
            self.state.maf_gs = 0.0

    # ------------------------------------------------------------------ helpers
    def _noise(self, scale: float) -> float:
        return self.rng.gauss(0.0, scale) if self.config.noise else 0.0

    def _coupled_rpm(self, gear: int, speed_ms: float) -> float:
        s = self.profile.sim
        wheel_rps = speed_ms / (2.0 * math.pi * s.wheel_radius_m)
        return wheel_rps * s.gear_ratios[gear - 1] * s.final_drive * 60.0

    def _full_load_map(self, rpm: float) -> float:
        return self.state.baro_kpa + self.calibration.table("boost_target").lookup(rpm, 100.0) * 100.0

    # ------------------------------------------------------------------ step
    MAX_SUBSTEP_S = 0.02

    def step(self, dt: float) -> Sample:
        """Advance the model by ``dt`` seconds and return the resulting sample."""
        if dt <= 0:
            raise ValueError("dt must be positive")
        substeps = max(1, math.ceil(dt / self.MAX_SUBSTEP_S - 1e-9))
        for _ in range(substeps):
            self._step_once(dt / substeps)
        return self.sample()

    def _step_once(self, dt: float) -> None:
        st = self.state
        sim = self.profile.sim
        st.time += dt

        inp = self.scenario.control(st.time, st) if st.engine_running else DriverInput(brake=1.0)
        self.last_input = inp
        st.pedal = _clamp(inp.pedal, 0.0, 100.0)
        st.brake = _clamp(inp.brake, 0.0, 1.0)

        if not st.engine_running:
            self._step_engine_off(dt)
            return

        st.run_time_s += dt
        cal = self.calibration

        # --- throttle (drive-by-wire follows pedal with a small lag)
        st.throttle = _approach(st.throttle, st.pedal, dt, 0.06)

        # --- boost target and turbo dynamics
        st.boost_target_bar = cal.table("boost_target").lookup(st.rpm, st.pedal)
        st.boost_target_filtered = _approach(st.boost_target_filtered, st.boost_target_bar, dt, 0.8)
        target = st.boost_target_bar
        # Exhaust energy limits how much boost the turbo can make at low rpm.
        capability = sim.peak_boost_bar * 1.6 * _clamp(
            (st.rpm - sim.spool_start_rpm * 0.8) / max(1.0, sim.full_spool_rpm - sim.spool_start_rpm * 0.8), 0.0, 1.0)
        achievable = min(target, capability) if target > 0 else target
        if "boost_leak" in self.faults and achievable > 0:
            achievable *= 0.6
        if "wastegate_stuck" in self.faults and target > 0.2:
            achievable = target + 0.35
        if st.rev_limiter_active:
            achievable = min(achievable, 0.2)
        tau = 0.12 if achievable < st.boost_bar else 0.25 + 0.9 * _clamp((2500.0 - st.rpm) / 2500.0, 0.0, 1.0)
        st.boost_bar = _approach(st.boost_bar, achievable, dt, tau) + self._noise(0.004)
        st.boost_bar = _clamp(st.boost_bar, -0.80, 3.0)

        # --- load and torque
        map_kpa = st.map_kpa
        map_idle = 30.0
        full_map = max(self._full_load_map(st.rpm), st.baro_kpa)
        torque_frac = _clamp((map_kpa - map_idle) / max(10.0, full_map - map_idle), 0.0, 1.15)
        st.load_pct = _clamp(100.0 * map_kpa / (st.baro_kpa + sim.peak_boost_bar * 100.0), 0.0, 100.0)
        st.load_avg = _approach(st.load_avg, st.load_pct, dt, 20.0)

        torque = self.profile.max_torque_at(max(st.rpm, sim.idle_rpm)) * torque_frac
        torque *= 1.0 - 0.025 * st.knock_retard_deg
        if st.misfire_cylinder is not None:
            torque *= 1.0 - 1.0 / self.profile.cylinders
        if st.pedal < 1.0:
            torque = -25.0 - 0.004 * st.rpm  # engine braking / pumping losses
        st.rev_limiter_active = st.rpm >= st.rev_limit
        if st.rev_limiter_active:
            torque = -40.0
        st.torque_nm = torque

        # --- drivetrain: torque converter + automatic gearbox
        st.shift_cooldown = max(0.0, st.shift_cooldown - dt)
        self._shift_logic(inp)
        ratio = sim.gear_ratios[st.gear - 1] * sim.final_drive
        coupled = self._coupled_rpm(st.gear, st.speed_ms)
        stall_rpm = sim.idle_rpm + (st.pedal / 100.0) * 1700.0
        slip = _clamp((stall_rpm - coupled) / max(stall_rpm, 1.0), 0.0, 1.0)
        multiplication = 1.0 + 0.9 * slip
        wheel_torque = st.torque_nm * ratio * sim.drivetrain_efficiency * multiplication
        if st.torque_nm < 0 and st.speed_ms < 2.0:
            wheel_torque = 0.0  # torque converter decouples engine braking at walking pace
        drive_force = wheel_torque / sim.wheel_radius_m
        v = st.speed_ms
        drag = 0.5 * AIR_DENSITY_AMBIENT * sim.drag_area_m2 * v * v
        rolling = sim.rolling_resistance * sim.vehicle_mass_kg * G if v > 0.05 else 0.0
        brake_force = st.brake * 0.95 * sim.vehicle_mass_kg * G if v > 0.0 else 0.0
        net = drive_force - drag - rolling - brake_force
        if v <= 0.0 and net < 0.0:
            net = 0.0
        accel = net / (sim.vehicle_mass_kg * 1.06)
        st.speed_ms = max(0.0, v + accel * dt)

        target_rpm = max(self._coupled_rpm(st.gear, st.speed_ms), stall_rpm * slip + sim.idle_rpm * (1 - slip),
                         sim.idle_rpm)
        if st.rev_limiter_active:
            target_rpm = min(target_rpm, st.rev_limit)
        st.rpm = _approach(st.rpm, target_rpm, dt, 0.08)
        if st.misfire_cylinder is not None:
            st.rpm += self._noise(25.0)
        else:
            st.rpm += self._noise(3.0)
        st.rpm = _clamp(st.rpm, 0.0, st.rev_limit + 80.0)

        # --- thermal model
        speed_kmh = st.speed_kmh
        coolant_target = 92.0 + 14.0 * st.load_avg / 100.0
        if "overheat" in self.faults:
            coolant_target = 128.0
        st.coolant_c = _approach(st.coolant_c, coolant_target, dt, 40.0 if "overheat" in self.faults else 120.0)
        oil_target = st.coolant_c + 6.0 + 30.0 * st.load_avg / 100.0
        st.oil_c = _approach(st.oil_c, oil_target, dt, 240.0)
        soak = 16.0 * (1.0 - _clamp(speed_kmh / 40.0, 0.0, 1.0))
        iat_target = st.ambient_c + 5.0 + 10.0 * max(0.0, st.boost_bar) + soak
        st.iat_c = _approach(st.iat_c, iat_target, dt, 8.0) + self._noise(0.05)

        # --- ignition timing (with IAT correction and knock control)
        base = cal.table("ignition_base").lookup(st.rpm, st.load_pct)
        iat_corr = -0.3 * max(0.0, st.iat_c - 40.0)
        if "knock" in self.faults and st.load_pct > 65.0 and self.rng.random() < dt * 2.0:
            st.knock_retard_deg = min(8.0, st.knock_retard_deg + self.rng.uniform(2.0, 4.0))
        st.knock_retard_deg = _approach(st.knock_retard_deg, 0.0, dt, 1.5)
        idle_wander = self._noise(1.2) if st.pedal < 1.0 and speed_kmh < 3.0 else self._noise(0.2)
        st.timing_deg = _clamp(base + iat_corr - st.knock_retard_deg + idle_wander, -20.0, 60.0)

        # --- fuelling
        st.lambda_cmd = cal.table("lambda_target").lookup(st.rpm, st.load_pct)
        lean = "lean" in self.faults
        stft_target = (18.0 if lean else 0.0) + self._noise(1.5)
        st.stft = _clamp(_approach(st.stft, stft_target, dt, 0.5), -25.0, 25.0)
        st.ltft = _clamp(_approach(st.ltft, 12.0 if lean else 1.5, dt, 30.0), -25.0, 25.0)
        density = map_kpa * 1000.0 / (R_AIR * (st.iat_c + 273.15))
        disp_m3 = self.profile.displacement_cc / 1e6
        st.maf_gs = max(0.0, density * disp_m3 * (st.rpm / 120.0) * sim.volumetric_efficiency * 1000.0)
        if st.rev_limiter_active or st.pedal < 1.0 and st.rpm > sim.idle_rpm + 300:
            fuel_gs = 0.0  # overrun / limiter fuel cut
        else:
            fuel_gs = st.maf_gs / (STOICH_AFR * max(0.7, st.lambda_cmd))
        st.fuel_rate_lh = fuel_gs * 3600.0 / GASOLINE_DENSITY_G_PER_L
        used_l = st.fuel_rate_lh * dt / 3600.0
        st.fuel_level_pct = max(0.0, st.fuel_level_pct - 100.0 * used_l / sim.fuel_tank_l)
        rail_target = 50.0 + (sim.max_rail_pressure_bar - 50.0) * (_clamp(st.load_pct / 100.0, 0.0, 1.0) ** 1.5)
        st.rail_bar = _clamp(_approach(st.rail_bar, rail_target, dt, 0.2) + self._noise(1.0),
                             0.0, sim.max_rail_pressure_bar)

        # --- electrical
        charge = 11.6 if "low_battery" in self.faults else 14.2
        st.battery_v = _approach(st.battery_v, charge, dt, 3.0) + self._noise(0.02)

    def _step_engine_off(self, dt: float) -> None:
        st = self.state
        st.rpm = 0.0
        st.speed_ms = 0.0
        st.throttle = _approach(st.throttle, 0.0, dt, 0.1)
        st.boost_bar = _approach(st.boost_bar, 0.0, dt, 0.5)
        st.boost_target_bar = 0.0
        st.maf_gs = 0.0
        st.fuel_rate_lh = 0.0
        st.torque_nm = 0.0
        st.load_pct = 0.0
        st.rail_bar = _approach(st.rail_bar, 5.0, dt, 20.0)
        st.coolant_c = _approach(st.coolant_c, st.ambient_c, dt, 1800.0)
        st.oil_c = _approach(st.oil_c, st.ambient_c, dt, 2400.0)
        st.iat_c = _approach(st.iat_c, st.ambient_c + 10.0, dt, 120.0)
        resting = 11.9 if "low_battery" in self.faults else 12.6
        st.battery_v = _approach(st.battery_v, resting, dt, 5.0)

    def _shift_logic(self, inp: DriverInput) -> None:
        st = self.state
        sim = self.profile.sim
        top_gear = len(sim.gear_ratios)
        if st.speed_ms < 1.0 and inp.hold_gear is None:
            st.gear = 1
            return
        if inp.hold_gear is not None:
            st.gear = int(_clamp(inp.hold_gear, 1, top_gear))
            return
        if st.shift_cooldown > 0:
            return
        up_rpm = 1700.0 + (st.pedal / 100.0) ** 1.2 * (st.rev_limit - 250.0 - 1700.0)
        down_rpm = 1100.0 + (st.pedal / 100.0) * 2200.0
        coupled = self._coupled_rpm(st.gear, st.speed_ms)
        if coupled > up_rpm and st.gear < top_gear:
            st.gear += 1
            st.shift_cooldown = 0.35
        elif coupled < down_rpm and st.gear > 1:
            lower = self._coupled_rpm(st.gear - 1, st.speed_ms)
            if lower < st.rev_limit - 600.0:
                st.gear -= 1
                st.shift_cooldown = 0.35

    # ------------------------------------------------------------------ output
    def sample(self) -> Sample:
        st = self.state
        values = {
            "rpm": st.rpm,
            "vehicle_speed": st.speed_kmh,
            "boost": st.boost_bar,
            "boost_target": st.boost_target_bar,
            "map_kpa": st.map_kpa,
            "baro_kpa": st.baro_kpa,
            "throttle": st.throttle,
            "pedal": st.pedal,
            "engine_load": st.load_pct,
            "coolant_temp": st.coolant_c,
            "oil_temp": st.oil_c,
            "intake_air_temp": st.iat_c,
            "ambient_temp": st.ambient_c,
            "timing_advance": st.timing_deg,
            "maf": st.maf_gs,
            "lambda_cmd": st.lambda_cmd,
            "stft": st.stft,
            "ltft": st.ltft,
            "fuel_rail_pressure": max(0.0, st.rail_bar),
            "fuel_rate": st.fuel_rate_lh,
            "fuel_level": st.fuel_level_pct,
            "battery_voltage": st.battery_v,
            "gear": float(st.gear),
            "torque_est": max(0.0, st.torque_nm),
            "run_time": st.run_time_s,
        }
        return Sample(timestamp=st.time, values=values)


def run_simulation(
    profile: EngineProfile,
    scenario: str = "wot_pull",
    duration_s: float = 30.0,
    rate_hz: float = 20.0,
    faults: tuple[str, ...] = (),
    seed: int = 1234,
    calibration: SimCalibration | None = None,
) -> list[Sample]:
    """Run the simulator headless and return samples at ``rate_hz``."""
    sim = EngineSimulator(profile, scenario, calibration=calibration, config=SimulatorConfig(seed=seed))
    for fault in faults:
        sim.inject_fault(fault)
    dt = 1.0 / rate_hz
    steps = int(round(duration_s * rate_hz))
    return [sim.step(dt) for _ in range(steps)]
