"""Rule-based performance and health analysis of datalogs.

Works fully offline with no model. An optional local LLM
(:mod:`opentune.analysis.local_model`) can turn the structured result into a
plain-language narrative, but every finding here is computed deterministically
so it can be tested and trusted.

Important limitation: generic OBD-II exposes no knock, boost-target or misfire
counters, so findings derived from real-car OBD logs are phrased as
*possible* causes, never diagnoses.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field

from opentune.core.models import CHANNELS, Sample
from opentune.engines.profile import EngineProfile

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}
SPOOL_TAU_S = 0.8  # expected turbo response lag used when comparing boost to target
_PENALTY = {"critical": 25, "warning": 10, "info": 2}


@dataclass
class Finding:
    severity: str  # critical | warning | info
    title: str
    detail: str
    recommendation: str = ""
    channel: str | None = None
    time_s: float | None = None


@dataclass
class ChannelStats:
    minimum: float
    maximum: float
    mean: float


@dataclass
class PullSummary:
    start_s: float
    end_s: float
    start_rpm: float
    end_rpm: float
    peak_boost_bar: float | None
    min_timing_deg: float | None
    iat_start_c: float | None
    iat_end_c: float | None
    min_lambda: float | None
    peak_maf_gs: float | None
    peak_power_kw: float | None
    power_method: str = ""

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


@dataclass
class AnalysisReport:
    engine: str
    source: str
    duration_s: float
    sample_count: int
    stats: dict[str, ChannelStats] = field(default_factory=dict)
    pulls: list[PullSummary] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    health_score: int = 100
    summary: str = ""
    narrative: str | None = None

    def findings_by_severity(self, severity: str) -> list[Finding]:
        return [f for f in self.findings if f.severity == severity]


def _values(samples: list[Sample], key: str) -> list[float]:
    return [s.values[key] for s in samples if key in s.values and not math.isnan(s.values[key])]


def _has(samples: list[Sample], key: str) -> bool:
    return any(key in s.values for s in samples)


def detect_pulls(samples: list[Sample], min_duration_s: float = 1.0, threshold: float = 90.0) -> list[tuple[int, int]]:
    """Index ranges (inclusive) where pedal (or throttle) stays at/above ``threshold`` %."""
    key = "pedal" if _has(samples, "pedal") else "throttle"
    ranges: list[tuple[int, int]] = []
    start: int | None = None
    for i, s in enumerate(samples):
        wot = s.values.get(key, 0.0) >= threshold
        if wot and start is None:
            start = i
        elif not wot and start is not None:
            ranges.append((start, i - 1))
            start = None
    if start is not None:
        ranges.append((start, len(samples) - 1))
    return [(a, b) for a, b in ranges
            if samples[b].timestamp - samples[a].timestamp >= min_duration_s]


def _slope(points: list[tuple[float, float]]) -> float:
    n = len(points)
    if n < 2:
        return 0.0
    mt = sum(p[0] for p in points) / n
    mv = sum(p[1] for p in points) / n
    den = sum((p[0] - mt) ** 2 for p in points)
    return 0.0 if den == 0 else sum((p[0] - mt) * (p[1] - mv) for p in points) / den


def _power_from_acceleration(seg: list[Sample], profile: EngineProfile) -> float | None:
    """Rough road-load power estimate (kW at the wheels) from speed vs time."""
    pts = [(s.timestamp, s.values["vehicle_speed"] / 3.6) for s in seg if "vehicle_speed" in s.values]
    if len(pts) < 5:
        return None
    sim = profile.sim
    best = 0.0
    for i, (t, v) in enumerate(pts):
        window = [p for p in pts[max(0, i - 10): i + 11] if abs(p[0] - t) <= 0.5]
        accel = _slope(window)
        force = sim.vehicle_mass_kg * 1.06 * accel + 0.5 * 1.2 * sim.drag_area_m2 * v * v \
            + sim.rolling_resistance * sim.vehicle_mass_kg * 9.81
        best = max(best, force * v / 1000.0)
    return best or None


def summarize_pull(seg: list[Sample], profile: EngineProfile) -> PullSummary:
    def first(key: str) -> float | None:
        vals = _values(seg[:3], key)
        return vals[0] if vals else None

    def last(key: str) -> float | None:
        vals = _values(seg[-3:], key)
        return vals[-1] if vals else None

    def agg(key: str, fn) -> float | None:
        vals = _values(seg, key)
        return fn(vals) if vals else None

    power, method = None, ""
    if _has(seg, "torque_est"):
        power = max((s.values.get("torque_est", 0.0) * s.values.get("rpm", 0.0) * 2 * math.pi / 60_000.0
                     for s in seg), default=None)
        method = "simulator torque model (crank)"
    elif _has(seg, "vehicle_speed"):
        power = _power_from_acceleration(seg, profile)
        method = "acceleration estimate (wheel, rough)" if power else ""
    return PullSummary(
        start_s=seg[0].timestamp,
        end_s=seg[-1].timestamp,
        start_rpm=first("rpm") or 0.0,
        end_rpm=agg("rpm", max) or 0.0,
        peak_boost_bar=agg("boost", max),
        min_timing_deg=agg("timing_advance", min),
        iat_start_c=first("intake_air_temp"),
        iat_end_c=last("intake_air_temp"),
        min_lambda=agg("lambda_cmd", min),
        peak_maf_gs=agg("maf", max),
        peak_power_kw=power,
        power_method=method,
    )


class Analyzer:
    def __init__(self, profile: EngineProfile) -> None:
        self.profile = profile
        self.t = profile.thresholds

    def analyze(self, samples: list[Sample], source: str = "") -> AnalysisReport:
        report = AnalysisReport(
            engine=self.profile.code,
            source=source,
            duration_s=(samples[-1].timestamp - samples[0].timestamp) if len(samples) > 1 else 0.0,
            sample_count=len(samples),
        )
        if not samples:
            report.summary = "No samples to analyse."
            return report
        for key in CHANNELS:
            vals = _values(samples, key)
            if vals:
                report.stats[key] = ChannelStats(min(vals), max(vals), statistics.fmean(vals))
        pull_ranges = detect_pulls(samples)
        segments = [samples[a:b + 1] for a, b in pull_ranges]
        report.pulls = [summarize_pull(seg, self.profile) for seg in segments]

        simulated = _has(samples, "boost_target")
        f = report.findings
        self._temperatures(samples, segments, f)
        self._boost(samples, segments, simulated, f)
        self._timing(segments, f)
        self._fuel(samples, segments, f)
        self._electrical(samples, f)
        self._idle(samples, f)
        self._rev_limit(samples, f)

        f.sort(key=lambda x: (SEVERITY_ORDER.get(x.severity, 9), x.time_s or 0.0))
        score = 100 - sum(_PENALTY.get(x.severity, 0) for x in f)
        report.health_score = max(0, min(100, score))
        report.summary = self._summary(report)
        return report

    # ------------------------------------------------------------------ rules
    def _threshold_rule(self, samples, key, warn, crit, label, advice, f) -> None:
        peak = max(((s.values[key], s.timestamp) for s in samples if key in s.values), default=None)
        if peak is None:
            return
        value, when = peak
        if value >= crit:
            f.append(Finding("critical", f"{label} reached {value:.0f} °C",
                             f"Peak {label.lower()} {value:.1f} °C exceeds the {crit:.0f} °C critical limit.",
                             advice, key, when))
        elif value >= warn:
            f.append(Finding("warning", f"{label} high ({value:.0f} °C)",
                             f"Peak {label.lower()} {value:.1f} °C exceeds the {warn:.0f} °C warning level.",
                             advice, key, when))

    def _temperatures(self, samples, segments, f) -> None:
        t = self.t
        self._threshold_rule(samples, "coolant_temp", t["coolant_warn"], t["coolant_critical"], "Coolant temperature",
                             "Stop hard driving. Check coolant level, fan, thermostat and water pump.", f)
        self._threshold_rule(samples, "oil_temp", t["oil_warn"], t["oil_critical"], "Oil temperature",
                             "Reduce load and let oil cool. Consider an oil cooler for track use.", f)
        for seg in segments:
            iats = _values(seg, "intake_air_temp")
            if not iats:
                continue
            if max(iats) >= self.t["iat_warn"]:
                f.append(Finding("warning", f"Intake air hot during pull ({max(iats):.0f} °C)",
                                 "High intake temperature reduces knock margin and power.",
                                 "Allow cool-down between pulls; check the charge-air cooler.",
                                 "intake_air_temp", seg[0].timestamp))
            elif max(iats) - iats[0] >= 12.0:
                f.append(Finding("info", f"Intake temperature rose {max(iats) - iats[0]:.0f} °C in one pull",
                                 "Indicates charge-air cooler heat soak.", "", "intake_air_temp", seg[0].timestamp))
            cool = _values(seg, "coolant_temp")
            if cool and min(cool) < 70.0:
                f.append(Finding("warning", "Full throttle before engine was warm",
                                 f"Coolant was {min(cool):.0f} °C during a full-throttle pull.",
                                 "Wait until coolant and oil are at operating temperature.",
                                 "coolant_temp", seg[0].timestamp))

    def _boost(self, samples, segments, simulated: bool, f) -> None:
        if simulated:
            self._boost_vs_target(samples, f)
        maps = _values(samples, "map_kpa")
        if maps and max(maps) >= 254.0 and not simulated:
            f.append(Finding("info", "Manifold pressure sensor PID saturated",
                             "Generic PID 0x0B tops out at 255 kPa absolute (~22 psi boost at sea level); "
                             "higher boost cannot be measured over generic OBD-II.",
                             "Use a manufacturer-specific logging module for boost above this range.", "map_kpa"))

    def _boost_vs_target(self, samples, f) -> None:
        """Compare boost to target, ignoring normal spool-up and lift-off transients.

        The target is low-pass filtered (time constant ``SPOOL_TAU_S``) so a healthy
        turbo catching up after a sudden pedal change is not flagged. A condition must
        persist continuously to count.
        """
        filt = None
        under_run = over_run = 0.0
        under_best = over_best = 0.0
        worst_under = worst_over = 0.0
        under_start = over_start = None
        first_under = first_over = None
        prev_t = None
        for s in samples:
            tgt, act = s.values.get("boost_target"), s.values.get("boost")
            if tgt is None or act is None:
                continue
            dt = 0.0 if prev_t is None else s.timestamp - prev_t
            prev_t = s.timestamp
            filt = tgt if filt is None else filt + (tgt - filt) * (1 - math.exp(-dt / SPOOL_TAU_S))
            ref = min(tgt, filt)
            if ref > 0.3 and ref - act > 0.25:
                under_start = s.timestamp if under_start is None else under_start
                under_run += dt
                worst_under = max(worst_under, ref - act)
                if under_run > under_best:
                    under_best, first_under = under_run, under_start
            else:
                under_run, under_start = 0.0, None
            if tgt > 0.2 and act - max(tgt, filt) > 0.2:
                over_start = s.timestamp if over_start is None else over_start
                over_run += dt
                worst_over = max(worst_over, act - max(tgt, filt))
                if over_run > over_best:
                    over_best, first_over = over_run, over_start
            else:
                over_run, over_start = 0.0, None
        if over_best >= 0.5:
            f.append(Finding("critical", f"Overboost: up to {worst_over:.2f} bar above target",
                             f"Boost exceeded target continuously for {over_best:.1f} s.",
                             "Stop full-throttle driving; inspect wastegate actuator and linkage.",
                             "boost", first_over))
        if under_best >= 1.0:
            sev = "critical" if worst_under > 0.45 else "warning"
            f.append(Finding(sev, f"Underboost: up to {worst_under:.2f} bar below target",
                             f"Boost stayed well below target for {under_best:.1f} s under load.",
                             "Pressure-test the charge pipes and intercooler; check couplers and clamps.",
                             "boost", first_under))

    def _timing(self, segments, f) -> None:
        events = 0
        first_time = None
        last_event = -1e9
        for seg in segments:
            for i in range(1, len(seg)):
                cur, prev = seg[i], seg[i - 1]
                if "timing_advance" not in cur.values or "timing_advance" not in prev.values:
                    continue
                if cur.timestamp - seg[0].timestamp < 0.75:
                    continue  # timing naturally falls as load builds at the start of a pull
                window = [s for s in seg[max(0, i - 6):i] if cur.timestamp - s.timestamp <= 0.3]
                if not window:
                    continue
                peak_before = max(s.values.get("timing_advance", -99) for s in window)
                rpm_rising = cur.values.get("rpm", 0) >= window[0].values.get("rpm", 0)
                load_steady = abs(cur.values.get("engine_load", 0) - window[0].values.get("engine_load", 0)) < 5.0
                if (peak_before - cur.values["timing_advance"] >= 3.0 and rpm_rising and load_steady
                        and cur.timestamp - last_event >= 0.5):
                    events += 1
                    last_event = cur.timestamp
                    first_time = first_time if first_time is not None else cur.timestamp
        if events:
            sev = "warning" if events >= 3 else "info"
            f.append(Finding(sev, f"Sudden timing reductions under load ({events})",
                             "Ignition advance dropped ≥3° within 0.3 s while rpm was rising — a pattern "
                             "consistent with knock control. Generic OBD-II cannot confirm knock directly.",
                             "Use the octane the car requires; check intake temperatures; avoid lugging.",
                             "timing_advance", first_time))

    def _fuel(self, samples, segments, f) -> None:
        running = [s for s in samples if s.values.get("rpm", 0) > 400]
        trims = [s.values["stft"] + s.values["ltft"] for s in running if "stft" in s.values and "ltft" in s.values]
        if len(trims) >= 10:
            mean = statistics.fmean(trims)
            if mean >= 20.0:
                f.append(Finding("critical", f"Engine running very lean (total fuel trim +{mean:.0f}%)",
                                 "The ECU is adding a lot of fuel to reach target — air is getting in "
                                 "unmeasured or fuel delivery is weak.",
                                 "Smoke-test for vacuum/boost leaks; check fuel pressure and the MAF sensor.",
                                 "ltft"))
            elif mean >= 10.0:
                f.append(Finding("warning", f"Lean fuel trims (+{mean:.0f}%)",
                                 "Fuel trims indicate the engine is running lean of target.",
                                 "Check for intake leaks and inspect the MAF sensor.", "ltft"))
            elif mean <= -10.0:
                f.append(Finding("warning", f"Rich fuel trims ({mean:.0f}%)",
                                 "Fuel trims indicate the engine is running rich of target.",
                                 "Check for leaking injectors or a stuck purge valve.", "ltft"))
        for seg in segments:
            hi_load = [s for s in seg if s.values.get("engine_load", 0) >= 90 and "lambda_cmd" in s.values]
            if hi_load and max(s.values["lambda_cmd"] for s in hi_load) > 0.95:
                f.append(Finding("warning", "Lean commanded mixture at full load",
                                 "Commanded lambda stayed above 0.95 at ≥90% load.",
                                 "Review the lambda target table — full-load enrichment protects the engine.",
                                 "lambda_cmd", seg[0].timestamp))
                break

    def _electrical(self, samples, f) -> None:
        running = [s for s in samples if s.values.get("rpm", 0) > 400 and "battery_voltage" in s.values]
        if len(running) >= 5:
            low = min(running, key=lambda s: s.values["battery_voltage"])
            if low.values["battery_voltage"] < self.t["battery_low"]:
                f.append(Finding("warning", f"Low system voltage ({low.values['battery_voltage']:.1f} V)",
                                 "System voltage dropped below normal while the engine was running.",
                                 "Test the battery and charging system.", "battery_voltage", low.timestamp))

    def _idle(self, samples, f) -> None:
        idle = [s.values["rpm"] for s in samples
                if s.values.get("vehicle_speed", 1) < 1 and s.values.get("pedal", s.values.get("throttle", 100)) < 2
                and s.values.get("rpm", 0) > 300]
        if len(idle) >= 20 and statistics.pstdev(idle) > 15.0:
            f.append(Finding("warning", f"Unstable idle (±{statistics.pstdev(idle):.0f} rpm)",
                             "Idle speed fluctuates more than expected — a possible misfire symptom.",
                             "Read DTCs; inspect coils and spark plugs.", "rpm"))

    def _rev_limit(self, samples, f) -> None:
        limit = self.profile.sim.rev_limit_rpm
        hits = [s for s in samples if s.values.get("rpm", 0) >= limit - 30]
        if hits:
            f.append(Finding("info", "Rev limiter reached",
                             f"Engine speed reached the profile's {limit:.0f} rpm limit.", "", "rpm",
                             hits[0].timestamp))

    def _summary(self, report: AnalysisReport) -> str:
        crit = len(report.findings_by_severity("critical"))
        warn = len(report.findings_by_severity("warning"))
        parts = [f"{report.engine} log, {report.duration_s:.0f} s, {report.sample_count} samples."]
        if report.pulls:
            best = max((p.peak_power_kw or 0.0) for p in report.pulls)
            boost = max((p.peak_boost_bar or 0.0) for p in report.pulls)
            parts.append(f"{len(report.pulls)} full-throttle pull(s); peak boost {boost:.2f} bar"
                         + (f", peak power ≈{best:.0f} kW." if best else "."))
        if crit:
            parts.append(f"{crit} critical issue(s) need attention before further hard driving.")
        elif warn:
            parts.append(f"{warn} warning(s) to review.")
        else:
            parts.append("No problems detected by the rule set.")
        parts.append(f"Health score {report.health_score}/100.")
        return " ".join(parts)


def analyze_samples(samples: list[Sample], profile: EngineProfile, source: str = "") -> AnalysisReport:
    return Analyzer(profile).analyze(samples, source)
