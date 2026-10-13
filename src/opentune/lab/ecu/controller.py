"""Virtual engine controller (VECU) with a torque-based control structure.

Architecture follows the publicly described torque-structure concept used by
modern gasoline engine management (driver torque demand → limits → air-charge
request → throttle/boost; spark and fuel from maps with corrections), similar in
spirit to the "simplified Bosch EMS" torque structure documented for
MathWorks' SI Core Engine. It is NOT BMW DME firmware and uses only the
calibration it is given plus decoded sensor signals — never plant internals.

Rates: one call to :meth:`VirtualEcu.step` per ECU task period (default 5 ms).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

from opentune.lab.calibration.tables import Calibration
from opentune.lab.ecu.pid import PID
from opentune.lab.ecu.sensors import SENSOR_SPECS, V_VALID_MAX, V_VALID_MIN, SensorReading
from opentune.lab.physics import formulas as f
from opentune.lab.physics.constants import GAMMA_AIR, R_AIR
from opentune.lab.physics.plant import PlantInputs, flow_function_regularized
from opentune.obd.dtc import describe

RHO_REF = 101325.0 / (R_AIR * 298.15)
STATES = ("OFF", "READY", "CRANKING", "RUNNING", "STALLED", "SHUTDOWN")


@dataclass
class DtcEntry:
    code: str
    description: str
    status: str  # "pending" | "stored"
    first_seen: float
    freeze_frame: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class EcuOutputs:
    time: float = 0.0
    state: str = "OFF"
    mode: str = "off"
    fuel_enable: bool = False
    starter: bool = False
    injector_pw_ms: float = 0.0
    fuel_mass_mg: float = 0.0  # per cylinder per cycle
    spark_advance: float = 0.0
    spark_base: float = 0.0
    throttle_cmd: float = 0.0
    boost_target_kpa: float = 0.0
    wastegate_cmd: float = 100.0
    idle_target_rpm: float = 0.0
    idle_torque_adder: float = 0.0
    torque_driver: float = 0.0
    torque_limit: float = 0.0
    torque_request: float = 0.0
    torque_estimate: float = 0.0
    air_request_mg: float = 0.0  # per cylinder per cycle
    air_estimate_mg: float = 0.0
    map_request_kpa: float = 0.0
    load_pct: float = 0.0
    lambda_target: float = 1.0
    lambda_measured: float = 1.0
    stft_pct: float = 0.0
    ltft_pct: float = 0.0
    closed_loop: bool = False
    knock_retard: float = 0.0
    knock_level: float = 0.0
    vvt_intake_target: float = 0.0
    vvt_exhaust_target: float = 0.0
    rail_target_bar: float = 0.0
    protections: list[str] = field(default_factory=list)
    limp: bool = False
    mil: bool = False
    dtcs: list[str] = field(default_factory=list)
    torque_limited_by: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class VirtualEcu:
    def __init__(self, calibration: Calibration, period: float = 0.005) -> None:
        self.cal = calibration
        self.period = period
        self.state = "OFF"
        self.time = 0.0
        self.dtcs: dict[str, DtcEntry] = {}
        self._fault_timers: dict[str, float] = {}
        self.knock_retard = 0.0
        self.stft = 0.0
        self.ltft = 0.0
        self.overrun_cut = False
        self.rev_cut = False
        self.overboost_timer = 0.0
        self.overboost_lockout = 0.0
        self.crank_timer = 0.0
        self.run_time = 0.0
        self._last_rpm = 0.0
        self._crank_lost = False
        self.last = EcuOutputs()
        self.reload_calibration(calibration)

    # ------------------------------------------------------------------ calibration
    def reload_calibration(self, calibration: Calibration) -> None:
        self.cal = calibration
        s = calibration.scalar
        self.vd = s("displacement_l") / 1000.0
        self.n_cyl = int(round(s("cylinders")))
        self.afr = s("stoich_afr")
        self.lhv = s("fuel_lhv_mj") * 1e6
        self.idle_pid = PID(s("idle_kp"), s("idle_ki"), s("idle_kd"), -60.0, 80.0)
        # Wastegate: more boost error → close the wastegate, so the gains are negative on "% open".
        self.boost_pid = PID(-s("boost_kp"), -s("boost_ki"), -s("boost_kd"), 0.0, 100.0, derivative_tau=0.05)
        self.throttle_pid = PID(s("throttle_kp"), s("throttle_ki"), 0.0, s("throttle_min_pos"), 100.0)
        self.lambda_pid = PID(s("lambda_kp"), s("lambda_ki"), 0.0, -s("stft_limit_pct"), s("stft_limit_pct"))

    # ------------------------------------------------------------------ DTC handling
    def _monitor(self, code: str, condition: bool, dt: float, frame: dict, confirm_s: float = 1.0,
                 pending_s: float | None = None) -> None:
        if condition:
            t = self._fault_timers.get(code, 0.0) + dt
            self._fault_timers[code] = t
            entry = self.dtcs.get(code)
            if entry is None and t >= (min(0.2, confirm_s) if pending_s is None else pending_s):
                self.dtcs[code] = DtcEntry(code, describe(code).description, "pending", self.time, dict(frame))
            elif entry is not None and entry.status == "pending" and t >= confirm_s:
                entry.status = "stored"
        else:
            self._fault_timers[code] = 0.0

    def active(self, code: str) -> bool:
        return self._fault_timers.get(code, 0.0) > 0.0

    def clear_dtcs(self) -> None:
        self.dtcs.clear()
        self._fault_timers.clear()

    # ------------------------------------------------------------------ helpers
    def _volts_ok(self, reading: SensorReading | None) -> tuple[bool, bool]:
        """(low_fault, high_fault) for an analogue reading."""
        if reading is None or reading.volts is None:
            return False, False
        return reading.volts < V_VALID_MIN, reading.volts > V_VALID_MAX

    def _air_per_cyl(self, rpm: float, map_kpa: float, iat_c: float) -> float:
        """Speed-density air mass per cylinder per cycle (kg) using the VE table."""
        ve = self.cal.lookup("ve", rpm, map_kpa) / 100.0
        return ve * map_kpa * 1000.0 * (self.vd / self.n_cyl) / (R_AIR * (iat_c + 273.15))

    def _required_map(self, rpm: float, air_per_cyl: float, iat_c: float, guess_kpa: float) -> float:
        p = max(guess_kpa, 20.0)
        for _ in range(3):
            ve = self.cal.lookup("ve", rpm, p) / 100.0
            p = air_per_cyl * R_AIR * (iat_c + 273.15) / (ve * self.vd / self.n_cyl) / 1000.0
            p = min(max(p, 15.0), 320.0)
        return p

    def _load_pct(self, air_per_cyl: float) -> float:
        return 100.0 * air_per_cyl / (RHO_REF * self.vd / self.n_cyl)

    def _torque_from_air(self, rpm: float, air_per_cyl: float, lam: float, map_kpa: float, baro_kpa: float) -> float:
        """ECU torque model: inner (indicated) torque from air charge minus friction and pumping."""
        energy_per_air = self.lhv / (self.afr * lam) * min(1.0, lam)
        eff = self.cal.lookup("torque_eff", rpm, self._load_pct(air_per_cyl))
        imep = air_per_cyl * self.n_cyl * energy_per_air * eff / self.vd
        fmep = self.cal.lookup("friction_est", rpm) * 1e5
        pmep = self.cal.lookup("pmep_est", rpm, map_kpa) * 1e5
        return f.torque_from_mep(imep - fmep - pmep, self.vd)

    def _air_from_torque(self, rpm: float, torque: float, lam: float, baro_kpa: float, map_kpa: float) -> float:
        """Inverse torque model: air charge per cylinder (kg) needed for a brake torque request."""
        fmep = self.cal.lookup("friction_est", rpm) * 1e5
        energy_per_air = self.lhv / (self.afr * lam) * min(1.0, lam)
        air = 0.0
        p_guess = map_kpa
        for _ in range(3):
            pmep = self.cal.lookup("pmep_est", rpm, p_guess) * 1e5
            imep = f.mep_from_torque(torque, self.vd) + fmep + pmep
            eff = self.cal.lookup("torque_eff", rpm, self._load_pct(air) if air > 0 else 50.0)
            air = max(0.0, imep * self.vd / (eff * energy_per_air * self.n_cyl))
            p_guess = self._required_map(rpm, air, 40.0, p_guess) if air > 0 else p_guess
        return air

    def _throttle_for_flow(self, mass_flow: float, p_up_kpa: float, t_up_c: float, p_down_kpa: float) -> float:
        """Inverse throttle model → throttle opening (%) for a required mass flow."""
        s = self.cal.scalar
        p_up = p_up_kpa * 1000.0
        pr = p_down_kpa / max(p_up_kpa, 1.0)
        if pr >= 0.985:
            return 100.0
        psi = flow_function_regularized(pr, GAMMA_AIR)
        area = mass_flow / (s("throttle_cd") * p_up / math.sqrt(R_AIR * (t_up_c + 273.15)) * psi)
        d = s("throttle_diameter_mm") / 1000.0
        closed = math.radians(s("throttle_closed_deg"))
        full = math.pi * d * d / 4.0
        x = min(max(1.0 - area / full, 0.0), 1.0) * math.cos(closed)
        angle = math.acos(x)
        return min(100.0, max(0.0, (angle - closed) / (math.pi / 2.0 - closed) * 100.0))

    def _throttle_flow(self, throttle_pct: float, p_up_kpa: float, t_up_c: float, p_down_kpa: float) -> float:
        """Forward throttle model (same geometry as :meth:`_throttle_for_flow`) → mass flow (kg/s)."""
        s = self.cal.scalar
        d = s("throttle_diameter_mm") / 1000.0
        closed = math.radians(s("throttle_closed_deg"))
        angle = closed + min(max(throttle_pct, 0.0), 100.0) / 100.0 * (math.pi / 2.0 - closed)
        area = math.pi * d * d / 4.0 * (1.0 - math.cos(angle) / math.cos(closed))
        psi = flow_function_regularized(min(p_down_kpa / max(p_up_kpa, 1.0), 1.0), GAMMA_AIR)
        return s("throttle_cd") * area * p_up_kpa * 1000.0 / math.sqrt(R_AIR * (t_up_c + 273.15)) * psi

    def _alpha_n_map(self, rpm: float, tps: float, baro_kpa: float, iat_c: float) -> float:
        """Manifold pressure where throttle flow = speed-density engine flow (MAP-sensor fallback)."""
        if rpm < 50.0:
            return baro_kpa
        n = rpm / 60.0
        lo, hi = 10.0, baro_kpa
        for _ in range(30):
            mid = 0.5 * (lo + hi)
            engine = self._air_per_cyl(rpm, mid, iat_c) * self.n_cyl * n / 2.0
            if self._throttle_flow(tps, baro_kpa, iat_c, mid) > engine:
                lo = mid
            else:
                hi = mid
        return 0.5 * (lo + hi)

    # ------------------------------------------------------------------ main task
    def step(self, readings: dict[str, SensorReading], ignition: bool, start_request: bool,
             dt: float | None = None) -> EcuOutputs:
        dt = self.period if dt is None else dt
        self.time += dt
        cal = self.cal
        s = cal.scalar
        out = EcuOutputs(time=self.time)
        protections: list[str] = []

        def val(sid: str, default: float) -> float:
            r = readings.get(sid)
            return r.value if r is not None else default

        rpm = max(0.0, val("crank", 0.0))
        baro_kpa = 101.3

        # ---- sensor diagnostics and substitute values
        frame = {"rpm": round(rpm), "map_kpa": round(val("map", 0.0), 1), "ect_c": round(val("ect", 0.0), 1),
                 "tps_pct": round(val("tps", 0.0), 1)}
        lo, hi = self._volts_ok(readings.get("map"))
        self._monitor("P0107", lo, dt, frame)
        self._monitor("P0108", hi, dt, frame)
        map_fault = lo or hi
        lo, hi = self._volts_ok(readings.get("iat"))
        self._monitor("P0112", lo, dt, frame)
        self._monitor("P0113", hi, dt, frame)
        iat_fault = lo or hi
        lo, hi = self._volts_ok(readings.get("ect"))
        self._monitor("P0117", lo, dt, frame)
        self._monitor("P0118", hi, dt, frame)
        ect_fault = lo or hi
        lo, hi = self._volts_ok(readings.get("tps"))
        self._monitor("P0122", lo, dt, frame)
        self._monitor("P0123", hi, dt, frame)
        tps_fault = lo or hi
        lam_lo, lam_hi = self._volts_ok(readings.get("lambda"))
        lambda_fault_now = lam_lo or lam_hi
        boost_fault = any(self._volts_ok(readings.get("boost")))
        pedal_fault = any(self._volts_ok(readings.get("pedal")))
        knock_r = readings.get("knock")
        knock_fault = ignition and rpm > 600 and knock_r is not None and knock_r.value <= 0.005
        self._monitor("P0327", knock_fault, dt, frame, confirm_s=2.0)
        # Crank signal lost while the engine was turning: the speed reading collapses to zero in
        # one task period, which a decelerating engine cannot do. Latched until a signal returns.
        if rpm <= 0.0 and self._last_rpm > 300.0 and ignition:
            self._crank_lost = True
        elif rpm > 0.0:
            self._crank_lost = False
        self._last_rpm = rpm
        self._monitor("P0335", self._crank_lost, dt, frame, confirm_s=0.0)

        iat_c = 40.0 if iat_fault else val("iat", 25.0)
        ect_c = 90.0 if ect_fault else val("ect", 25.0)
        map_kpa = val("map", baro_kpa)
        boost_kpa = baro_kpa if boost_fault else val("boost", baro_kpa)
        tps = val("tps", 0.0)
        pedal = 0.0 if pedal_fault else min(max(val("pedal", 0.0), 0.0), 100.0)
        if map_fault:
            # Alpha-N substitute: manifold pressure at which throttle flow equals engine intake
            # flow for the measured throttle angle and speed (limp mode keeps boost off).
            map_kpa = self._alpha_n_map(rpm, tps, baro_kpa, iat_c)
            protections.append("substitute:map")
        if iat_fault:
            protections.append("substitute:iat")
        if ect_fault:
            protections.append("substitute:ect")
        limp = map_fault or tps_fault or pedal_fault or self.active("P0335")
        if limp:
            protections.append("limp_mode")

        # ---- state machine
        if not ignition:
            self.state = "OFF" if rpm < 1.0 else "SHUTDOWN"
        elif self.state in ("OFF", "SHUTDOWN"):
            self.state = "READY"
        if self.state == "READY" and start_request:
            self.state = "CRANKING"
            self.crank_timer = 0.0
        if self.state == "CRANKING":
            self.crank_timer += dt
            if rpm >= s("start_rpm"):
                self.state = "RUNNING"
                self.run_time = 0.0
            elif self.crank_timer > 6.0 or not start_request and rpm < 50.0:
                self.state = "READY"
        if self.state == "RUNNING" and rpm < s("stall_rpm"):
            self.state = "STALLED"
        if self.state == "STALLED" and rpm < 1.0:
            self.state = "READY"
        out.state = self.state
        out.starter = self.state == "CRANKING"

        # ---- warm-up and idle targets
        idle_target = cal.lookup("idle_target", ect_c)
        out.idle_target_rpm = idle_target

        if self.state not in ("RUNNING", "CRANKING"):
            out.mode = "off" if self.state in ("OFF", "SHUTDOWN") else self.state.lower()
            out.throttle_cmd = s("throttle_idle_pos") if ignition else 0.0
            out.wastegate_cmd = 100.0
            out.rail_target_bar = 5.0
            self.idle_pid.reset()
            self.boost_pid.reset()
            self.throttle_pid.reset()
            self._finish(out, protections, frame, limp)
            return out

        # ---- cranking strategy
        if self.state == "CRANKING":
            out.mode = "cranking"
            out.fuel_enable = rpm > 80.0 and not self.active("P0335")
            fuel_mg = s("crank_fuel_mg") * (1.0 + cal.lookup("warmup_enrich", ect_c) / 100.0)
            out.fuel_mass_mg = fuel_mg
            out.rail_target_bar = cal.lookup("rail_target", rpm, 10.0)
            q = s("injector_flow_gs") / 1000.0 * math.sqrt(max(val("rail_pressure", 50.0), 5.0)
                                                          / s("injector_ref_bar"))
            out.injector_pw_ms = (fuel_mg * 1e-6 / q + s("injector_dead_time_ms") / 1000.0) * 1000.0 if q > 0 else 0
            out.spark_advance = s("crank_spark_deg")
            out.throttle_cmd = s("throttle_idle_pos") * 1.5
            out.wastegate_cmd = 100.0
            self._finish(out, protections, frame, limp)
            return out

        # ---- RUNNING: torque structure
        rev_limit = s("rev_limit_rpm")
        if rpm > rev_limit:
            self.rev_cut = True
        elif rpm < rev_limit - s("rev_limit_hyst_rpm"):
            self.rev_cut = False
        if self.rev_cut:
            protections.append("rev_limiter")
        self._monitor("P0219", rpm > rev_limit + 300.0, dt, frame, confirm_s=0.2)

        torque_cap = cal.lookup("torque_limit", rpm)
        driver = cal.lookup("pedal_map", rpm, pedal) / 100.0 * torque_cap
        limit = torque_cap
        limited_by = ""
        coolant_factor = cal.lookup("coolant_torque_limit", ect_c) / 100.0
        if coolant_factor < 0.999:
            protections.append("coolant_torque_limit")
            limited_by = "coolant temperature"
        limit *= coolant_factor
        if limp:
            limit = min(limit, s("limp_torque_nm"))
            limited_by = "limp mode"
        if self.overboost_lockout > 0.0:
            self.overboost_lockout -= dt
            limit = min(limit, 0.5 * torque_cap)
            protections.append("overboost_protection")
            limited_by = "overboost protection"

        governing = pedal < 1.0  # idle speed governor active whenever the pedal is released
        idle_mode = governing and rpm < idle_target + 500.0
        self.run_time += dt
        overrun = pedal < 1.0 and rpm > idle_target + s("overrun_cut_rpm_above_idle") and self.run_time > 3.0
        if overrun and ect_c > 40.0:
            self.overrun_cut = True
        elif pedal >= 1.0 or rpm < idle_target + s("overrun_resume_rpm_above_idle"):
            self.overrun_cut = False

        # idle governor: torque request from PID on engine speed whenever the pedal is released
        if governing:
            # A speed governor must be able to ask for *negative* brake torque (less air than
            # friction + pumping need), otherwise the engine settles wherever zero brake torque
            # happens to fall. Floor: most of the friction + pumping torque (keep some combustion).
            # The floor is also the PID's output limit, so anti-windup acts on the real limit.
            loss_nm = f.torque_from_mep((self.cal.lookup("friction_est", rpm)
                                         + self.cal.lookup("pmep_est", rpm, map_kpa)) * 1e5, self.vd)
            self.idle_pid.out_min = -0.8 * loss_nm
            self.idle_pid.out_max = max(self.idle_pid.out_min, min(80.0, limit))
            idle_adder = self.idle_pid.update(idle_target, rpm, dt, feedforward=s("idle_torque_ff_nm"))
            torque_request = idle_adder
        else:
            self.idle_pid.reset(self.idle_pid.integrator * 0.98)
            idle_adder = 0.0
            torque_request = min(driver, limit)
        if driver > limit and not governing:
            limited_by = limited_by or "torque limit table"

        # ---- air charge request → MAP request
        lam_target = cal.lookup("lambda_target", rpm, max(self.last.load_pct, 10.0))
        air_req = self._air_from_torque(rpm, torque_request, max(lam_target, 0.75), baro_kpa, map_kpa)
        air_req_cyl = air_req
        map_req = self._required_map(rpm, air_req_cyl, iat_c, map_kpa)
        out.map_request_kpa = map_req
        out.air_request_mg = air_req_cyl * 1e6

        # ---- boost target and wastegate (PID + feed-forward map)
        boost_cap = cal.lookup("boost_max", rpm, pedal)
        if self.active("P0327"):
            boost_cap = baro_kpa + 0.8 * (boost_cap - baro_kpa)
            protections.append("knock_sensor_fault_boost_limit")
        boost_target = min(max(map_req + s("boost_reserve_kpa"), baro_kpa), boost_cap)
        if limp or boost_fault or self.overboost_lockout > 0.0:
            boost_target = baro_kpa
        if boost_target <= baro_kpa + 8.0:
            wastegate = 100.0
            self.boost_pid.reset()
        else:
            ff = cal.lookup("wastegate_ff", rpm, boost_target)
            wastegate = self.boost_pid.update(boost_target, boost_kpa, dt, feedforward=ff)
        out.boost_target_kpa = boost_target
        out.wastegate_cmd = wastegate
        # overboost / underboost monitoring
        if boost_kpa > boost_target + s("overboost_margin_kpa") and boost_target > baro_kpa + 8.0:
            self.overboost_timer += dt
        else:
            self.overboost_timer = 0.0
        overboost = self.overboost_timer > s("overboost_time_s")
        self._monitor("P0234", overboost, dt, frame, confirm_s=0.0)
        if overboost:
            self.overboost_lockout = 5.0
        self._monitor("P0299", rpm > s("underboost_enable_rpm") and boost_target > baro_kpa + 30.0
                      and boost_kpa < boost_target - 30.0 and wastegate <= 1.0, dt, frame, confirm_s=5.0,
                      pending_s=2.5)

        # ---- throttle: feed-forward from inverse throttle model + PI on MAP
        p_up = max(boost_kpa, baro_kpa * 0.9)
        n = rpm / 60.0
        if tps_fault:
            throttle = s("throttle_limp_pos")
        elif map_req >= 0.97 * p_up:
            throttle = 100.0
            self.throttle_pid.reset()
        else:
            flow_req = air_req_cyl * self.n_cyl * n / 2.0
            ff = self._throttle_for_flow(flow_req, p_up, iat_c, map_req)
            throttle = self.throttle_pid.update(map_req, map_kpa, dt, feedforward=ff)
        out.throttle_cmd = throttle

        # ---- fuel: speed-density air estimate, lambda target, corrections, closed loop
        air_est = self._air_per_cyl(rpm, map_kpa, iat_c)
        load_pct = 100.0 * air_est / (RHO_REF * self.vd / self.n_cyl)
        out.air_estimate_mg = air_est * 1e6
        out.load_pct = load_pct
        lam_target = cal.lookup("lambda_target", rpm, load_pct)
        egt = val("egt", 0.0)
        if egt > 0.0:
            cap = cal.lookup("egt_protect_lambda", egt)
            if cap < lam_target:
                lam_target = cap
                protections.append("egt_enrichment")
        lambda_fault = lambda_fault_now and self.state == "RUNNING"
        self._monitor("P0131", lambda_fault and lam_lo, dt, frame)
        self._monitor("P0132", lambda_fault and lam_hi, dt, frame)
        lam_meas = val("lambda", 1.0)
        closed_loop = (not lambda_fault and ect_c >= s("cl_min_coolant_c") and abs(lam_target - 1.0) < 0.02
                       and not self.overrun_cut and load_pct > 8.0)
        if closed_loop:
            self.stft = self.lambda_pid.update(lam_target, lam_meas, dt) * -1.0  # lean → add fuel
            if abs(self.stft) > 2.0:  # long-term trim learns the persistent part of the short-term trim
                self.ltft += math.copysign(s("ltft_rate_pct_s") * dt, self.stft)
            self.ltft = min(max(self.ltft, -s("stft_limit_pct")), s("stft_limit_pct"))
        else:
            self.lambda_pid.reset()
            self.stft = 0.0
        trim = self.stft + self.ltft
        trim_limit = s("fuel_trim_dtc_pct")
        self._monitor("P0171", closed_loop and trim > trim_limit, dt, frame, confirm_s=10.0, pending_s=5.0)
        self._monitor("P0172", closed_loop and trim < -trim_limit, dt, frame, confirm_s=10.0, pending_s=5.0)
        if lambda_fault:
            lam_target -= 0.03
            protections.append("open_loop_fuel")
        warmup = cal.lookup("warmup_enrich", ect_c) / 100.0
        fuel_mass = air_est / (self.afr * lam_target) * (1.0 + (self.stft + self.ltft) / 100.0) * (1.0 + warmup)
        fuel_enable = not (self.overrun_cut or self.rev_cut or self.active("P0335") or overboost)
        rail_target = cal.lookup("rail_target", rpm, load_pct)
        rail = max(val("rail_pressure", rail_target), 5.0)
        q = s("injector_flow_gs") / 1000.0 * math.sqrt(rail / s("injector_ref_bar"))
        pw = fuel_mass / q + s("injector_dead_time_ms") / 1000.0 if q > 0 else 0.0
        out.fuel_enable = fuel_enable
        out.fuel_mass_mg = fuel_mass * 1e6
        out.injector_pw_ms = pw * 1000.0 if fuel_enable else 0.0
        out.rail_target_bar = rail_target
        out.lambda_target = lam_target
        out.lambda_measured = lam_meas
        out.stft_pct = self.stft
        out.ltft_pct = self.ltft
        out.closed_loop = closed_loop

        # ---- ignition: base map + corrections + knock control + idle spark
        base = cal.lookup("ign_base", rpm, load_pct)
        spark = base + cal.lookup("iat_spark_corr", iat_c)
        knock_level = knock_r.value if knock_r is not None else 0.0
        if self.active("P0327"):
            self.knock_retard = s("knock_sensor_fault_retard_deg")
            protections.append("knock_sensor_fault_retard")
        elif knock_level > s("knock_threshold"):
            self.knock_retard = min(self.knock_retard + cal.lookup("knock_retard_step", rpm) * dt / 0.05,
                                    s("knock_max_retard_deg"))
        else:
            self.knock_retard = max(0.0, self.knock_retard - s("knock_recovery_deg_s") * dt)
        if self.knock_retard > 0.5:
            protections.append("knock_retard")
        spark -= self.knock_retard
        if idle_mode:
            spark = spark - s("idle_spark_reserve_deg") + s("idle_spark_gain") * (idle_target - rpm) / 100.0
        spark = min(max(spark, s("spark_min_deg")), s("spark_max_deg"))
        out.spark_base = base
        out.spark_advance = spark
        out.knock_retard = self.knock_retard
        out.knock_level = knock_level

        # ---- valvetrain targets (parked on cam sensor fault)
        cam_fault_i = readings.get("cam_intake") is not None and readings["cam_intake"].fault in (
            "open_circuit", "short_ground")
        self._monitor("P0340", cam_fault_i, dt, frame)
        if self.active("P0340") or limp:
            out.vvt_intake_target = out.vvt_exhaust_target = 0.0
        else:
            out.vvt_intake_target = cal.lookup("vvt_intake", rpm, load_pct)
            out.vvt_exhaust_target = cal.lookup("vvt_exhaust", rpm, load_pct)
            cam_meas = val("cam_intake", out.vvt_intake_target)
            self._monitor("P0016", abs(cam_meas - out.vvt_intake_target) > 25.0 and rpm > 1200, dt, frame,
                          confirm_s=3.0)

        # ---- torque bookkeeping
        out.torque_driver = driver
        out.torque_limit = limit
        out.torque_request = torque_request
        out.torque_estimate = self._torque_from_air(rpm, air_est, lam_target, map_kpa, baro_kpa) \
            if fuel_enable else 0.0
        out.idle_torque_adder = idle_adder if governing else 0.0
        out.torque_limited_by = limited_by
        out.mode = ("rev_limit" if self.rev_cut else "overrun_cut" if self.overrun_cut else "idle" if idle_mode
                    else "wot" if pedal > 90.0 else "part_load")
        self._finish(out, protections, frame, limp)
        return out

    def _finish(self, out: EcuOutputs, protections: list[str], frame: dict, limp: bool) -> None:
        out.protections = protections
        out.limp = limp
        out.dtcs = sorted(self.dtcs)
        out.mil = any(e.status == "stored" for e in self.dtcs.values())
        self.last = out

    # ------------------------------------------------------------------ plant interface
    @staticmethod
    def to_plant_inputs(out: EcuOutputs, ignition: bool) -> PlantInputs:
        return PlantInputs(
            throttle_cmd=out.throttle_cmd,
            wastegate_cmd=out.wastegate_cmd,
            injector_pw=out.injector_pw_ms / 1000.0,
            rail_pressure_cmd=out.rail_target_bar * 1e5,
            spark_advance=out.spark_advance,
            intake_cam_cmd=out.vvt_intake_target,
            exhaust_cam_cmd=out.vvt_exhaust_target,
            fuel_enable=out.fuel_enable,
            ignition_on=ignition,
            starter=out.starter,
        )

    def dtc_report(self) -> list[dict]:
        return [e.to_dict() for e in sorted(self.dtcs.values(), key=lambda e: e.code)]


def sensor_list() -> list[dict]:
    return [{"id": s.id, "name": s.name, "unit": s.unit, "analogue": s.encode is not None,
             "dtc_low": s.dtc_low, "dtc_high": s.dtc_high} for s in SENSOR_SPECS.values()]
