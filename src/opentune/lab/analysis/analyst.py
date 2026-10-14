"""AI engineering analyst: rule-based findings with explicit evidence categories, optional local LLM.

Every statement the analyst makes is tagged with one of the five provenance
categories:

* **measured**    — verified real-world measurement (the lab has none yet, so this never appears
                    unless the user attaches measured data in a later version)
* **documented**  — a publicly documented specification with a source (e.g. a press-release rating)
* **simulated**   — computed by the simulation (dyno runs, steady-state model, scenario results)
* **assumption**  — engineering assumption or synthetic calibration value
* **speculative** — explanation not established by the data (including all LLM text)

The rule-based analyst works without any model. If a local model server
(Ollama or an OpenAI-compatible server on localhost) is configured, it writes a
narrative from the findings; that narrative is always labelled *speculative*.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from opentune.analysis.local_model import LocalModelClient, LocalModelConfig, LocalModelError
from opentune.lab.dyno.runs import DynoRun
from opentune.lab.physics.config import EngineConfig
from opentune.lab.provenance import Provenance
from opentune.obd.dtc import describe

SEVERITIES = ("info", "notice", "warning", "critical")

SYSTEM_PROMPT = (
    "You are an engine-simulation analyst inside the OpenTune Virtual Engine Lab. You explain results of a "
    "SYNTHETIC engine model and a FICTIONAL virtual ECU. Rules: (1) Use only the facts provided. (2) Prefix every "
    "sentence with one tag: [documented], [simulated], [assumption] or [speculative]; never claim [measured] — "
    "there is no measured data. (3) Never present simulated results as predictions for a real engine or vehicle. "
    "(4) Do not give calibration values, map changes, memory addresses, checksums, security-access or immobiliser "
    "information for real ECUs, and do not advise on disabling emissions equipment. (5) Suggest only further "
    "simulation experiments. (6) Under 250 words."
)


@dataclass
class Evidence:
    kind: str  # Provenance value
    text: str
    value: float | None = None
    unit: str = ""
    source: str = ""


@dataclass
class Finding:
    id: str
    title: str
    severity: str
    category: str
    summary: str
    evidence: list[Evidence] = field(default_factory=list)
    suggestion: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AnalysisResult:
    findings: list[Finding]
    narrative: str | None
    narrative_provenance: str
    model_status: str
    disclaimer: str = ("Simulation analysis of a synthetic model. Nothing here is a measurement or a prediction for "
                       "a real engine, and nothing here is a calibration for a vehicle.")

    def to_dict(self) -> dict:
        return {"findings": [f.to_dict() for f in self.findings], "narrative": self.narrative,
                "narrative_provenance": self.narrative_provenance, "model_status": self.model_status,
                "disclaimer": self.disclaimer, "counts": {s: sum(f.severity == s for f in self.findings)
                                                          for s in SEVERITIES}}


S = Provenance.SIMULATED.value
D = Provenance.DOCUMENTED.value
A = Provenance.ASSUMPTION.value
SP = Provenance.SPECULATIVE.value


def _ev(kind: str, text: str, value: float | None = None, unit: str = "", source: str = "") -> Evidence:
    return Evidence(kind, text, None if value is None else round(float(value), 3), unit, source)


def analyze_run(run: DynoRun, config: EngineConfig) -> list[Finding]:
    """Findings for one dyno run (sweep/partial/repeat/steady/transient)."""
    out: list[Finding] = []
    s = run.summary or {}
    lim = config.limits
    tag = f"run {run.label}"
    curve = run.curve or []

    if s.get("peak_power_kw") and config.reference_ratings.get("power_kw") and run.kind in ("sweep", "repeat"):
        ref = config.reference_ratings
        delta = (s["peak_power_kw"] - ref["power_kw"]) / ref["power_kw"] * 100.0
        out.append(Finding(
            "rating_comparison", "Peak power vs published reference rating", "info", "performance",
            f"Simulated peak {s['peak_power_kw']:.0f} kW vs the published {ref['power_kw']:.0f} kW "
            f"({delta:+.1f} %). The model was tuned towards this rating, so agreement is not validation.",
            [_ev(S, f"Peak power in {tag}", s["peak_power_kw"], "kW"),
             _ev(D, ref.get("label", "reference rating"), ref["power_kw"], "kW", ref.get("source", "")),
             _ev(A, "Model coefficients were chosen to land near this rating")]))

    kr = s.get("max_knock_retard_deg") or 0.0
    if kr > 0.5:
        at = max(curve, key=lambda p: p.get("knock_retard_deg") or 0.0) if curve else {}
        out.append(Finding(
            "knock_retard", "Knock control retarded the spark", "warning" if kr < 4 else "critical", "knock",
            f"The virtual ECU pulled up to {kr:.1f}° of spark" + (f", most around {at.get('rpm', 0):.0f} rpm" if at
                                                                  else "") + ".",
            [_ev(S, "Maximum knock retard", kr, "°"),
             _ev(A, "Knock onset comes from an empirical knock-limit model (pressure, IAT, coolant, lambda, octane "
                    "terms), not from measured knock data")],
            "In simulation: reduce boost or spark in the affected region, or lower charge temperature, then re-run."))

    egt = s.get("max_egt_c")
    if egt is not None and egt > lim.egt_limit_c - 30:
        out.append(Finding(
            "egt_high", "Exhaust gas temperature near the modelled limit",
            "critical" if egt > lim.egt_limit_c else "warning", "thermal",
            f"Pre-turbine EGT reached {egt:.0f} °C against a modelled limit of {lim.egt_limit_c:.0f} °C.",
            [_ev(S, "Maximum EGT", egt, "°C"), _ev(A, "EGT limit is a model assumption", lim.egt_limit_c, "°C")],
            "Check the lambda target and spark at high speed in simulation; EGT protection enrichment should engage."))
    if "egt_enrichment" in run.events.get("protections", []):
        out.append(Finding("egt_protection", "EGT protection enriched the mixture", "notice", "thermal",
                           "The virtual ECU's EGT protection table lowered the lambda target during the run.",
                           [_ev(S, "Protection 'egt_enrichment' observed")]))

    iat = s.get("max_iat_c")
    if iat is not None and iat > lim.iat_limit_c:
        out.append(Finding("iat_high", "Charge air temperature high (heat soak)", "warning", "thermal",
                           f"Charge temperature reached {iat:.0f} °C (modelled limit {lim.iat_limit_c:.0f} °C); "
                           "this raises knock tendency in the model.",
                           [_ev(S, "Maximum charge-air temperature", iat, "°C"),
                            _ev(A, "Charge-cooler effectiveness and loop heat capacity are assumptions")],
                           "Compare with a cooler-ambient run or a cool-down period between pulls."))

    if curve and run.kind in ("sweep", "repeat", "partial"):
        rich_needed = [p for p in curve if (p.get("map_kpa") or 0) > 170 and (p.get("lambda") or 0) > 0.95]
        if rich_needed:
            out.append(Finding("lean_high_load", "Near-stoichiometric mixture at high load", "warning", "fuel",
                               f"{len(rich_needed)} high-load points ran at λ > 0.95.",
                               [_ev(S, "Points above 170 kPa with λ > 0.95", len(rich_needed)),
                                _ev(A, "The model raises exhaust temperature and knock tendency as λ → 1")],
                               "Inspect the lambda_target table at high load in simulation."))
        under = [p for p in curve if p["rpm"] > 2500 and p.get("boost_target_kpa") and p.get("boost_kpa")
                 and p["boost_kpa"] < p["boost_target_kpa"] - 15]
        if under:
            out.append(Finding("underboost", "Boost below target", "notice", "boost",
                               f"Boost was more than 15 kPa below target at {len(under)} speeds above 2500 rpm "
                               f"(first at {under[0]['rpm']:.0f} rpm).",
                               [_ev(S, "Largest shortfall", max(p['boost_target_kpa'] - p['boost_kpa'] for p in under),
                                    "kPa"), _ev(A, "Turbo maps (compressor/turbine) are synthetic")],
                               "Compare boost_max against what the turbo model can reach (steady-state preview)."))
        tq_lim = [p for p in curve if p.get("torque_limit_nm") and p.get("torque_nm")
                  and p["torque_nm"] > 0.97 * p["torque_limit_nm"]]
        if len(tq_lim) > 3:
            out.append(Finding("torque_limited", "Output limited by the torque-limit table", "info", "calibration",
                               f"At {len(tq_lim)} speeds the engine delivered ≥97 % of the ECU torque limit, so "
                               "the calibration (not the hardware model) set the output there.",
                               [_ev(S, "Points at the torque limit", len(tq_lim))]))
    turbo = s.get("max_turbo_krpm")
    if turbo and turbo * 1000 > 0.95 * lim.turbo_speed_limit_rpm:
        out.append(Finding("turbo_speed", "Turbo speed near the modelled limit", "warning", "boost",
                           f"Turbo speed reached {turbo:.0f} krpm (limit {lim.turbo_speed_limit_rpm / 1000:.0f}).",
                           [_ev(S, "Maximum turbo speed", turbo, "krpm"), _ev(A, "Turbo speed limit is assumed")]))
    pp = s.get("max_peak_pressure_bar")
    if pp and pp > lim.peak_pressure_limit_bar - 10:
        out.append(Finding("peak_pressure", "Estimated peak cylinder pressure near the limit", "warning", "combustion",
                           f"Estimated peak pressure {pp:.0f} bar (limit {lim.peak_pressure_limit_bar:.0f} bar).",
                           [_ev(S, "Peak pressure estimate", pp, "bar"),
                            _ev(A, "Peak pressure comes from a correlation fitted to the single-zone cycle model")]))
    for code in run.events.get("dtcs", []):
        out.append(Finding(f"dtc_{code}", f"DTC {code} set during the run", "warning", "diagnostics",
                           f"{code}: {describe(code).description}.",
                           [_ev(S, "DTC stored by the virtual ECU"),
                            _ev(D, "Generic SAE J2012 code description", source="SAE J2012 (generic codes)")],
                           "Open Engine Diagnostics to see the freeze frame; check injected faults."))
    if run.kind == "transient" and run.metrics:
        m = run.metrics
        out.append(Finding("transient", "Tip-in response", "info", "control",
                           f"Torque reached 90 % of its final value after {m.get('t90_s')} s; boost settled "
                           f"after {m.get('boost_t90_s')} s.",
                           [_ev(S, "t90 torque", m.get("t90_s"), "s"),
                            _ev(A, "Turbo inertia and wastegate dynamics are assumed values")]))
    unc = run.uncertainty
    if unc:
        width = (unc["peak_power_p95_kw"] - unc["peak_power_p05_kw"]) / max(unc["peak_power_p50_kw"], 1e-6) * 100
        sens = unc.get("sensitivity") or []
        top = ", ".join(x["path"] for x in sens[:3])
        out.append(Finding("model_uncertainty", "Model uncertainty band", "notice" if width > 8 else "info", "model",
                           f"5–95 % band of peak power spans {width:.1f} % of the median"
                           + (f"; most sensitive assumptions: {top}." if top else "."),
                           [_ev(S, "Monte Carlo over assumption parameters", width, "%"),
                            _ev(A, "Parameter spreads are themselves assumptions")],
                           "Measured data for the top-ranked parameters would narrow the band most."))
    return out


def analyze_repeatability(rep: dict) -> list[Finding]:
    if not rep:
        return []
    p = rep["peak_power_kw"]
    sev = "notice" if abs(rep.get("power_change_first_to_last_pct", 0)) > 1.5 else "info"
    return [Finding("repeatability", "Run-to-run variation (heat soak)", sev, "thermal",
                    f"Peak power {p['values']} kW: change first→last {rep['power_change_first_to_last_pct']:+.2f} %, "
                    f"CV {p['cv_pct']:.2f} %. Charge-loop temperature at the start of each pull: "
                    f"{rep.get('start_charge_loop_c')} °C.",
                    [_ev(S, "Back-to-back pulls on the virtual dyno"),
                     _ev(A, "Charge-cooler loop capacity and heat-rejection coefficients are assumptions")],
                    "Add a cool-down between pulls or lower ambient temperature to separate heat soak from "
                    "calibration effects.")]


def analyze_comparison(base: DynoRun, other: DynoRun, cal_diff: dict | None = None) -> list[Finding]:
    dp = other.summary.get("peak_power_kw", 0) - base.summary.get("peak_power_kw", 0)
    dt = other.summary.get("peak_torque_nm", 0) - base.summary.get("peak_torque_nm", 0)
    same_conditions = (base.test.get("ambient_temp_c"), base.test.get("ambient_pressure_kpa"), base.test.get("seed"),
                       base.engine_id) == (other.test.get("ambient_temp_c"), other.test.get("ambient_pressure_kpa"),
                                           other.test.get("seed"), other.engine_id)
    ev = [_ev(S, "Peak power change", dp, "kW"), _ev(S, "Peak torque change", dt, "N·m")]
    changed = []
    if cal_diff:
        changed = [k for k, v in cal_diff.get("tables", {}).items() if v.get("changed_cells")] + \
            list(cal_diff.get("scalars", {}).keys())
    if same_conditions and other.calibration_hash != base.calibration_hash:
        ev.append(_ev(S, "Controlled comparison: same engine, ambient and seed; only the calibration differs, so "
                         "within the model the difference is caused by the calibration change"))
    elif not same_conditions:
        ev.append(_ev(SP, "Conditions differ (engine, ambient or seed), so attributing the difference to the "
                          "calibration alone is speculative"))
    if changed:
        ev.append(_ev(S, "Changed calibration items: " + ", ".join(changed[:8])))
    return [Finding("comparison", f"{other.label} vs {base.label}", "info", "performance",
                    f"Peak power {dp:+.1f} kW, peak torque {dt:+.1f} N·m.", ev)]


def analyze_calibration_diff(diff: dict) -> list[Finding]:
    out = []
    tables = diff.get("tables", {})
    for tid, label, threshold, unit in (("boost_max", "boost target", 25.0, "kPa"),
                                        ("ign_base", "base spark", 3.0, "°"),
                                        ("torque_limit", "torque limit", 50.0, "N·m")):
        t = tables.get(tid)
        if t and t.get("changed_cells") and t.get("max_abs_delta") is not None and t["max_abs_delta"] >= threshold:
            out.append(Finding(f"cal_{tid}", f"Large change to {label}", "notice", "calibration",
                               f"{t['changed_cells']} cells changed, up to {t['max_abs_delta']:.1f} {unit}.",
                               [_ev(S, f"Diff against the project's baseline ({tid})", t["max_abs_delta"], unit),
                                _ev(A, "Baseline tables are synthetic (generated from the model)")],
                               "Run a sweep and a heat-stress repeat with this change before going further."))
    return out


def model_client(config: dict | None) -> LocalModelClient | None:
    if not config or not config.get("enabled"):
        return None
    return LocalModelClient(LocalModelConfig(backend=config.get("backend", "ollama"),
                                             base_url=config.get("url", "http://127.0.0.1:11434"),
                                             model=config.get("model", "llama3.2"),
                                             timeout_s=float(config.get("timeout_s", 60.0))))


def _findings_text(findings: list[Finding]) -> str:
    lines = []
    for f in findings:
        lines.append(f"- [{f.severity}] {f.title}: {f.summary}")
        for e in f.evidence:
            lines.append(f"    ({e.kind}) {e.text}" + (f" = {e.value} {e.unit}" if e.value is not None else ""))
    return "\n".join(lines) or "- no findings"


def narrate(findings: list[Finding], client: LocalModelClient | None, question: str | None = None,
            context: str = "") -> tuple[str | None, str]:
    """LLM narrative (speculative) or ``None`` with a status message."""
    if client is None:
        return None, "No local model configured — rule-based findings only."
    prompt = ("Context:\n" + (context or "(none)") + "\n\nRule-based findings:\n" + _findings_text(findings) +
              ("\n\nUser question: " + question if question else
               "\n\nWrite a short engineering summary of these simulation findings."))
    try:
        return client.generate(prompt, system=SYSTEM_PROMPT), f"Local model {client.config.model} responded."
    except LocalModelError as exc:
        return None, f"Local model unavailable: {exc}. Rule-based findings shown."


KEYWORDS = {"knock": ("knock",), "temperature": ("thermal",), "heat": ("thermal",), "egt": ("thermal",),
            "boost": ("boost",), "turbo": ("boost",), "fuel": ("fuel",), "lambda": ("fuel",), "afr": ("fuel",),
            "dtc": ("diagnostics",), "code": ("diagnostics",), "fault": ("diagnostics",), "power": ("performance",),
            "torque": ("performance", "calibration"), "uncertain": ("model",), "accura": ("model",),
            "valid": ("model",), "control": ("control",), "pid": ("control",), "repeat": ("thermal",)}


def answer_without_model(question: str, findings: list[Finding]) -> str:
    q = question.lower()
    cats = {c for k, cs in KEYWORDS.items() if k in q for c in cs}
    rel = [f for f in findings if f.category in cats] if cats else findings
    if any(w in q for w in ("real car", "my car", "vehicle", "flash my", "safe to")):
        return ("[assumption] The lab only simulates a synthetic engine with a fictional ECU; it cannot tell you "
                "what is safe for a real vehicle, and its calibrations must not be used on one.")
    if not rel:
        return "No rule-based finding matches that question. Configure a local model for free-form answers."
    return "\n".join(f"[{f.evidence[0].kind if f.evidence else S}] {f.title}: {f.summary}" for f in rel[:6])
