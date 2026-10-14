"""Tuning session: diagnose → probe → propose → predict → test → accept/reject → learn → validate.

The loop a careful calibrator follows, run against the simulator:

1. **Baseline and diagnosis.** Run the test battery on the starting calibration; find the binding
   constraints (tight stress margins, knock activity, component protection) and what limits torque.
2. **Probe** the actions the knowledge base is still unsure about: one small step each, measured, not
   kept. Actions already well known from earlier sessions are not re-measured.
3. **Propose** small steps (single actions and pairs) inside a per-action *trust region*.
4. **Predict** each proposal's effect with the response model, including uncertainty. A proposal is only
   tried if every stress margin is predicted to stay above its floor *with margin to spare*
   (mean − k·σ) and knock control is predicted not to grow.
5. **Test** the best proposal in the simulator and **judge** it against the limits and the baseline.
6. **Accept** it only if it passes and really improves the goal; otherwise reject it and shrink that
   action's trust region. Every outcome updates the model; wrong predictions are logged as mistakes and
   make later predictions more cautious.
7. **Validate** the final candidate on a larger battery (more conditions, model-error corners, tip-in).
   If it fails, walk back to the last accepted step that passes.

The result is a *recommendation for further simulation and, eventually, supervised measurement*. It is
never marked ready for a vehicle.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields

from opentune.lab.calibration.tables import Calibration
from opentune.lab.physics.config import EngineConfig
from opentune.lab.tuning.actions import (
    ACTIONS,
    DEFAULT_ACTIONS,
    apply_offsets,
    clamp_offset,
    describe_offsets,
    describe_step,
)
from opentune.lab.tuning.evaluator import (
    ADVERSE,
    CONDITION_INFO,
    FIDELITIES,
    GOALS,
    STRESS_KEYS,
    BatteryConfig,
    Scorecard,
    StressLimits,
    evaluate,
    judge,
    margin_floor,
    objective,
    objective_from,
)
from opentune.lab.tuning.knowledge import (
    KNOCK_OUTPUTS,
    MARGIN_OUTPUTS,
    METRIC_OUTPUTS,
    KnowledgeBase,
    ResponseModel,
    deltas,
    offsets_to_units,
    output_unit,
    scorecard_outputs,
)

TUNER_VERSION = "1.0"

DISCLAIMER = [
    "Every number in this report comes from a synthetic engine model and a virtual ECU. Nothing was measured "
    "on a real engine.",
    "Passing the simulated test battery means the calibration stayed within *modelled* limits under the "
    "*simulated* conditions. It is not evidence that the calibration is safe for a real vehicle.",
    "The stress limits are assumptions (not OEM component ratings). Real hardware limits, fuel quality, "
    "sensor tolerances, emissions and durability are not represented adequately.",
    "This output is a recommendation for further simulation and, eventually, supervised measurement on a "
    "dynamometer by qualified people. It is not a flashable or road-ready calibration, and OpenTune cannot "
    "write to a vehicle.",
]

Progress = Callable[[float, str], None]
Cancel = Callable[[], bool]


class TuningCancelled(RuntimeError):
    pass


@dataclass
class TuningRequest:
    goal: str = "max_power"
    budget: int = 16  # search evaluations (baseline, probes and validation are counted separately)
    max_probes: int | None = None  # default: as many as needed
    fidelity: str = "quick"
    validation_fidelity: str = "standard"
    actions: list[str] | None = None
    stress_weight: float = 0.25  # objective penalty per % of added stress index
    safety_k: float = 2.0  # predicted margin − k·σ must clear its floor
    tolerance_pp: float = 0.5  # how far a margin already tight in the baseline may move (noise allowance)
    min_gain: float = 0.1  # objective units (weighted % improvement) a proposal must be predicted to add
    risk_aversion: float = 0.5  # rank proposals by mean − this × σ of the predicted objective
    max_radius: float = 3.0  # trust region cap (action units)
    limits: dict = field(default_factory=dict)  # StressLimits overrides
    workers: int = 0
    validate: bool = True
    learn: bool = True  # store experiments in the knowledge base

    def problems(self) -> list[str]:
        p = []
        if self.goal not in GOALS:
            p.append(f"Unknown goal {self.goal!r}; options: {', '.join(GOALS)}")
        if not 0 <= self.budget <= 200:
            p.append("Budget must be 0–200 evaluations")
        for fid in (self.fidelity, self.validation_fidelity):
            if fid not in FIDELITIES:
                p.append(f"Unknown fidelity {fid!r}")
        for a in self.actions or []:
            if a not in ACTIONS:
                p.append(f"Unknown action {a!r}")
        if self.actions is not None and not self.actions:
            p.append("Select at least one action")
        if not 0.0 <= self.safety_k <= 5.0:
            p.append("safety_k must be 0–5")
        if not 0.0 <= self.tolerance_pp <= 5.0:
            p.append("tolerance_pp must be 0–5")
        known = set(StressLimits.__dataclass_fields__)
        for k in self.limits:
            if k not in known:
                p.append(f"Unknown stress limit {k!r}")
        return p

    @classmethod
    def from_dict(cls, d: dict) -> TuningRequest:
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class StepRecord:
    index: int
    phase: str  # baseline | probe | search | validation
    step: dict[str, float]  # action units relative to the incumbent (probe: relative to the baseline)
    offsets: dict[str, float]  # cumulative physical offsets of the evaluated calibration
    description: str
    predicted: dict | None
    objective: float
    delta: dict[str, float]
    passed: bool
    accepted: bool
    reasons: list[str]
    rationale: str
    mistakes: list[str]
    metrics: dict[str, float]
    margins: dict[str, float]
    knock: dict[str, float]
    eval_seconds: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class _Point:
    offsets: dict[str, float]
    score: Scorecard
    objective: float
    step_index: int


def _units_to_offsets(offsets: dict[str, float], step: dict[str, float]) -> tuple[dict[str, float], dict[str, float]]:
    """Apply a step (action units) to cumulative offsets; returns (new offsets, the step actually taken)."""
    new = dict(offsets)
    taken = {}
    for a, u in step.items():
        spec = ACTIONS[a]
        old = new.get(a, 0.0)
        val = clamp_offset(a, old + u * spec.step)
        real = (val - old) / spec.step
        if abs(real) > 1e-9:
            taken[a] = round(real, 6)
            new[a] = round(val, 6)
    return {a: v for a, v in new.items() if abs(v) > 1e-12}, taken


# ---------------------------------------------------------------------------------------- diagnosis
def diagnose(score: Scorecard, goal: str) -> list[dict]:
    """Plain-language findings about what limits this calibration (evidence from the scorecard)."""
    out: list[dict] = []
    tight = sorted(((m, k) for k, m in score.margins_pct.items() if m < 10.0))
    for m, k in tight:
        worst = max(score.stress_by_condition, key=lambda c, k=k: (score.stress_by_condition[c][k]
                                                                    if STRESS_KEYS[k][1] == "max"
                                                                    else -score.stress_by_condition[c][k]))
        out.append({"finding": f"{k} margin is {m:.1f} % (worst in '{worst}': {CONDITION_INFO.get(worst, worst)})",
                    "implication": "changes that raise it are constrained" if m >= 0 else "already beyond the limit",
                    "evidence": "simulated"})
    kn, ka = score.knock["nominal_deg"], score.knock["adverse_deg"]
    out.append({"finding": f"knock control: {kn:.1f}° nominal, {ka:.1f}° in adverse conditions",
                "implication": ("little spark headroom at full load" if ka > 1.5 else
                                "some spark headroom may exist at full load"),
                "evidence": "simulated"})
    egt_conds = [c for c, ev in score.events.items() if "egt_enrichment" in ev.get("protections", [])]
    if egt_conds:
        out.append({"finding": f"exhaust-temperature protection enrichment active in: {', '.join(egt_conds)}",
                    "implication": "high-rpm, high-load changes that heat the exhaust will be absorbed by "
                                   "protection (and cost power) or rejected",
                    "evidence": "simulated"})
    curve = score.curves.get("nominal", [])
    air_limited = [p["rpm"] for p in curve if p.get("torque_limit_nm") and p.get("torque_nm") is not None
                   and p["torque_nm"] < 0.95 * p["torque_limit_nm"]]
    if curve:
        if air_limited:
            out.append({"finding": f"torque below the torque-limit table from {min(air_limited):.0f} to "
                                   f"{max(air_limited):.0f} rpm (air/boost-limited there)",
                        "implication": "more torque there needs more boost; the torque table alone does nothing",
                        "evidence": "simulated"})
        else:
            out.append({"finding": "torque follows the torque-limit table over the whole sweep",
                        "implication": "torque-limit changes act directly", "evidence": "simulated"})
    out.append({"finding": f"goal: {GOALS[goal]['label']}", "implication": "weights: " + ", ".join(
        f"{k} {w:+g}" for k, w in GOALS[goal]["weights"].items()), "evidence": "request"})
    return out


# ---------------------------------------------------------------------------------------- session
class TuningSession:
    def __init__(self, config: EngineConfig, baseline: Calibration, request: TuningRequest | None = None,
                 kb: KnowledgeBase | None = None, engine_key: str | None = None, project_id: str | None = None,
                 progress: Progress | None = None, cancel: Cancel | None = None,
                 battery: BatteryConfig | None = None, validation_battery: BatteryConfig | None = None) -> None:
        """``battery``/``validation_battery`` override the fidelity presets (tests, custom experiments)."""
        self.config = config
        self.baseline_cal = baseline
        self.req = request or TuningRequest()
        problems = self.req.problems()
        if problems:
            raise ValueError("; ".join(problems))
        self.kb = kb or KnowledgeBase(":memory:")
        # where this session's experiments are written: the knowledge base, or a scratch store when
        # learning is off (benchmarks), so the knowledge base is only read
        self.store = self.kb if self.req.learn else KnowledgeBase(":memory:")
        self.engine_key = engine_key or config.id
        self.project_id = project_id
        self._progress = progress
        self._cancel = cancel
        self.actions = list(self.req.actions or DEFAULT_ACTIONS)
        self.limits = StressLimits.from_config(config)
        for k, v in self.req.limits.items():
            setattr(self.limits, k, v if k == "provenance" else float(v))
        self.battery = battery or BatteryConfig.preset(self.req.fidelity)
        if battery is None:
            self.battery.workers = self.req.workers
        self._vbat = validation_battery
        self.steps: list[StepRecord] = []
        self.mistakes: list[dict] = []
        self.z_log: list[float] = []  # prediction errors / model σ (raw)
        self.z_eff_log: list[float] = []  # prediction errors / σ used for decisions (with caution factor)
        self.evals = {"baseline": 0, "probe": 0, "repair": 0, "search": 0, "validation": 0}
        self.sid = ""
        self.base_v: Scorecard | None = None
        self.floors: dict[str, float] = {}
        self.gaps: dict[str, float] = {}
        self.repaired_at: int | None = None
        self.repair_keys: set[str] = set()  # limits broken by the baseline ("knock" for knock control)

    # ------------------------------------------------------------------ helpers
    def _check_cancel(self) -> None:
        if self._cancel is not None and self._cancel():
            raise TuningCancelled("Tuning session cancelled")

    def _report_progress(self, message: str) -> None:
        if self._progress is None:
            return
        n_probe = self.req.max_probes if self.req.max_probes is not None else len(self.actions)
        total = 1 + n_probe + self.req.budget + self.evals["repair"] + (4 if self.req.validate else 0)
        done = sum(self.evals.values())
        self._progress(min(0.99, done / max(1, total)), message)

    def _evaluate(self, offsets: dict[str, float], phase: str, battery: BatteryConfig | None = None) -> Scorecard:
        self._check_cancel()
        cal = apply_offsets(self.baseline_cal, offsets) if offsets else self.baseline_cal
        score = evaluate(self.config, cal, battery or self.battery, self.limits, self._cancel)
        self.evals[phase] += 1
        return score

    def _decision_outputs(self) -> list[str]:
        goal_keys = [k for k in GOALS[self.req.goal]["weights"] if k in METRIC_OUTPUTS]
        return goal_keys + list(MARGIN_OUTPUTS) + list(KNOCK_OUTPUTS)

    def _predict(self, model: ResponseModel, inc: _Point, offsets: dict[str, float]) -> dict:
        """Predicted outcome of moving from the incumbent to ``offsets``: objective (mean, σ), margins, knock,
        and whether it clears the safety screen."""
        new_u, ref_u = offsets_to_units(offsets), offsets_to_units(inc.offsets)
        pred = model.predict(new_u, ref_u)
        raw = model.predict(new_u, ref_u, raw=True)
        base = self.base
        cur = scorecard_outputs(inc.score)
        metrics = dict(inc.score.metrics)
        for k in METRIC_OUTPUTS:
            if k in metrics:
                metrics[k] = cur[k] * (1.0 + pred[k][0] / 100.0)
        margins = {k: cur[f"margin:{k}"] + pred[f"margin:{k}"][0] for k in self.floors}
        util = []
        for k in self.floors:
            direction = STRESS_KEYS[k][1]
            m = margins[k]
            util.append(1.0 - m / 100.0 if direction == "max" else 1.0 / max(1e-3, 1.0 + m / 100.0))
        sidx = max(util)
        obj = objective_from(metrics, sidx, base.metrics, base.stress_index, self.req.goal, self.req.stress_weight)
        # σ of the objective: goal-weighted metric σ in quadrature (+ stress term on the binding margin)
        var = 0.0
        for k, w in GOALS[self.req.goal]["weights"].items():
            if k in pred:
                var += (abs(w) * pred[k][1]) ** 2
        binding = min(self.floors, key=lambda k: margins[k] - self.floors[k])
        var += (self.req.stress_weight * pred[f"margin:{binding}"][1]) ** 2
        # safety screen
        k_sig = self.req.safety_k
        k_rep = min(k_sig, 1.0)  # repair: still screened, but a broken limit is the bigger risk
        blockers, repair_blockers = [], []
        violated = {k for k in self.floors if cur[f"margin:{k}"] < 0.0}
        for key in self.floors:
            floor = self.floors[key]
            mean, std = pred[f"margin:{key}"]
            lower = cur[f"margin:{key}"] + mean - k_sig * std
            if lower < floor:
                blockers.append(f"{key} margin could fall to {lower:.2f} % (floor {floor:.2f} %)")
            if key not in violated and cur[f"margin:{key}"] + mean - k_rep * std < floor:
                repair_blockers.append(f"{key} margin could fall below its floor {floor:.2f} %")
        kn_mean, kn_std = pred["knock:nominal"]
        ka_mean, ka_std = pred["knock:adverse"]
        ka_cap = min(self.limits.max_knock_retard_adverse_deg, base.knock["adverse_deg"] + 1.0)
        knock_repair = self._knock_deficit(inc.score) > 0.0  # knock itself is being repaired
        for k_, out in ((k_sig, blockers), (k_rep, None if knock_repair else repair_blockers)):
            if out is None:
                continue
            if cur["knock:nominal"] + kn_mean + k_ * kn_std > self.limits.max_knock_retard_nominal_deg:
                out.append("knock control could start in nominal conditions")
            if cur["knock:adverse"] + ka_mean + k_ * ka_std > ka_cap:
                out.append(f"adverse knock retard could exceed {ka_cap:.1f}°")
        # repair view: limits already exceeded at the incumbent are what is being repaired, every other
        # margin is still screened (at k_rep). The predicted deficit uses the mean: a repair move that does
        # not help only costs one rejected experiment.
        deficit = self._deficit_of({k: cur[f"margin:{k}"] + pred[f"margin:{k}"][0] for k in self.floors},
                                   cur["knock:nominal"] + kn_mean, cur["knock:adverse"] + ka_mean,
                                   inc.score.violations, clearance=True)
        return {"objective": [round(obj, 4), round(math.sqrt(var), 4)],
                "outputs": {k: [round(m, 4), round(s, 4)] for k, (m, s) in pred.items()},
                "raw": {k: [round(m, 4), round(s, 4)] for k, (m, s) in raw.items()},
                "safe": not blockers, "blockers": blockers,
                "safe_repair": not repair_blockers, "deficit": round(deficit, 4)}

    def _knock_deficit(self, score: Scorecard) -> float:
        return (max(0.0, score.knock["nominal_deg"] - self.limits.max_knock_retard_nominal_deg)
                + max(0.0, score.knock["adverse_deg"] - self.limits.max_knock_retard_adverse_deg))

    def _record(self, phase: str, step: dict, offsets: dict, score: Scorecard, ref: Scorecard,
                predicted: dict | None, accepted: bool, verdict_passed: bool, reasons: list[str], rationale: str,
                mistakes: list[str], battery: BatteryConfig | None = None) -> StepRecord:
        base = self.base if phase != "validation" else self.base_v
        obj = objective(score, base, self.req.goal, self.req.stress_weight)
        rec = StepRecord(len(self.steps), phase, step, dict(offsets),
                         describe_step(step) if phase in ("probe", "search", "repair") else "; ".join(
                             describe_offsets(offsets)) or "baseline",
                         predicted, obj, deltas(score, ref), verdict_passed, accepted, reasons, rationale, mistakes,
                         dict(score.metrics), dict(score.margins_pct), dict(score.knock), score.eval_seconds)
        self.steps.append(rec)
        kb_delta = deltas(score, base) if phase in ("probe", "search", "repair") else rec.delta
        self.store.add_trial(self.sid, rec.index, self.engine_key, phase, step, offsets, kb_delta, predicted, obj,
                             accepted, verdict_passed, reasons, (battery or self.battery).fidelity)
        return rec

    def _mistake(self, idx: int, kind: str, actions: list[str], output: str, detail: str,
                 z: float | None = None) -> str:
        m = {"step": idx, "kind": kind, "actions": actions, "output": output, "detail": detail,
             "z": None if z is None else round(z, 2)}
        self.mistakes.append(m)
        self.store.add_mistake(self.sid, idx, self.engine_key, kind, actions, output, detail, z)
        return f"{kind}: {detail}"

    def _check_predictions(self, idx: int, step: dict, predicted: dict, delta: dict, score: Scorecard) -> list[str]:
        """Compare predicted and measured effects; log large misses on outputs that matter as mistakes.

        Every output's raw z-score is kept (``predicted["z_raw"]``) — the knowledge base turns them into
        caution factors. A miss counts as a *mistake* only for the goal's metrics, margins within 3 pp of
        their floor, and knock control.
        """
        notes = []
        goal_keys = [k for k in GOALS[self.req.goal]["weights"] if k in METRIC_OUTPUTS]
        z_raw = {}
        for k in predicted["raw"]:
            raw_mean, raw_std = predicted["raw"][k]
            if raw_std > 0:
                z_raw[k] = round((delta[k] - raw_mean) / raw_std, 3)
                if k in self._decision_outputs():
                    self.z_log.append(z_raw[k])
                    std = predicted["outputs"][k][1]
                    self.z_eff_log.append((delta[k] - raw_mean) / std if std > 0 else 0.0)
        predicted["z_raw"] = z_raw
        for k in self._decision_outputs():
            if k.startswith("margin:"):
                key = k.split(":", 1)[1]
                if key not in self.floors or score.margins_pct.get(key, 0.0) > self.floors[key] + 3.0:
                    continue
            elif k not in goal_keys and k not in KNOCK_OUTPUTS:
                continue
            mean, std = predicted["outputs"][k]
            z = (delta[k] - mean) / std if std > 0 else 0.0
            if abs(z) > 3.0:
                notes.append(self._mistake(idx, "prediction_miss", list(step), k,
                                           f"{k}: predicted {mean:+.2f} ± {std:.2f} {output_unit(k)}, "
                                           f"measured {delta[k]:+.2f}", z))
        return notes

    # ------------------------------------------------------------------ main loop
    def run(self) -> dict:
        req = self.req
        self.sid = self.store.new_session(self.config.id, self.engine_key, req.goal, req.to_dict(), self.project_id)
        status = "failed"
        try:
            report = self._run()
            status = "done"
            return report
        except TuningCancelled:
            status = "cancelled"
            raise
        finally:
            if status != "done":
                self.store.finish_session(self.sid, status, {}, {})

    def _floors(self, ref: Scorecard | None = None) -> dict[str, float]:
        """Margin floor per stress key for the search battery.

        The usual floor (``margin_floor``) — and, when a validation battery is used, a floor that keeps
        the *projected* validation margin above the validation floor: the search battery is a cheaper proxy,
        so its margin is shifted by the gap measured on the baseline (validation is usually harsher, e.g.
        more heat soak). This removes most validation failures before they happen. ``ref`` is the point the
        "no worse than" rule refers to: the baseline, or the repaired calibration after a repair.
        """
        req, tol = self.limits.required_margin_pct, self.req.tolerance_pp
        ref = ref or self.base
        floors = {k: margin_floor(ref, k, req, tol) for k in self.base.margins_pct}
        self.gaps = {}
        if self.base_v is not None:
            for k in floors:
                gap = self.base.margins_pct[k] - self.base_v.margins_pct[k]
                self.gaps[k] = round(gap, 3)
                floors[k] = max(floors[k], margin_floor(self.base_v, k, req, tol) + gap)
        return {k: round(v, 4) for k, v in floors.items()}

    def _deficit(self, score: Scorecard, clearance: bool = False) -> float:
        """How far a calibration is beyond its limits (0 = within every limit).

        With ``clearance`` the limits broken by the *baseline* count as met only with some room to spare
        (``tolerance_pp`` of margin, 0.3° of knock retard): a repair that just touches a limit sits inside
        the simulator's scatter and would not survive validation.
        """
        return self._deficit_of(score.margins_pct, score.knock["nominal_deg"], score.knock["adverse_deg"],
                                score.violations, clearance)

    def _deficit_of(self, margins: dict[str, float], kn: float, ka: float, violations: list[str],
                    clearance: bool) -> float:
        tol = self.req.tolerance_pp if clearance else 0.0
        kt = 0.3 if clearance else 0.0
        d = sum(max(0.0, (tol if k in self.repair_keys else 0.0) - m) for k, m in margins.items())
        d += max(0.0, kn - self.limits.max_knock_retard_nominal_deg + (kt if "knock" in self.repair_keys else 0.0))
        d += max(0.0, ka - self.limits.max_knock_retard_adverse_deg + (kt if "knock" in self.repair_keys else 0.0))
        d += sum(1.0 for v in violations if v.startswith(("DTCs", "protection")))
        return round(d, 4)

    def _run(self) -> dict:
        req = self.req
        kb_trials = self.kb.trial_count()
        prior = self.kb.prior(self.actions, self.engine_key, self.battery.fidelity)
        caution = self.kb.caution()
        surprises = self.kb.surprises(self.engine_key)
        model = ResponseModel(self.actions, prior, caution)
        radius = {a: max(0.25, 1.0 / (1 + surprises.get(a, 0))) for a in self.actions}
        evaluated: set[tuple] = set()

        def key_of(offsets: dict[str, float]) -> tuple:
            return tuple(sorted((a, round(v, 4)) for a, v in offsets.items() if abs(v) > 1e-9))

        # 1. baseline (search and validation batteries) and diagnosis
        self._report_progress("baseline")
        self.base = self._evaluate({}, "baseline")
        self.base_v = None
        vbat = None
        if req.validate:
            vbat = self._vbat
            if vbat is None:
                vbat = BatteryConfig.preset(req.validation_fidelity)
                vbat.workers = req.workers
            self._report_progress("baseline (validation battery)")
            self.base_v = self._evaluate({}, "baseline", vbat)
        self.floors = self._floors()
        self.repair_keys = {k for k, m in self.base.margins_pct.items() if m < 0.0}
        if self._knock_deficit(self.base) > 0.0:
            self.repair_keys.add("knock")
        self._record("baseline", {}, {}, self.base, self.base, None, True, not self.base.violations,
                     list(self.base.violations), "starting point", [])
        evaluated.add(key_of({}))
        findings = diagnose(self.base, req.goal)
        if self._deficit(self.base) > 0:
            findings.insert(0, {"finding": "the starting calibration exceeds limits on this engine model: "
                                           + "; ".join(self.base.violations[:3]),
                                "implication": "repair first: find the smallest changes that bring every limit "
                                               "back, then improve the goal from there",
                                "evidence": "simulated"})
        if self.base_v is not None:
            shifted = [f"{k} {g:+.1f} pp" for k, g in self.gaps.items() if abs(g) >= 0.5]
            if shifted:
                findings.append({"finding": "the validation battery is harsher than the search battery on: "
                                            + ", ".join(shifted),
                                 "implication": "search floors for these margins are raised by the same amount",
                                 "evidence": "simulated"})
        inc = _Point({}, self.base, 0.0, 0)
        chain = [inc]

        # 2. probes: one step per action in the direction expected to help the goal, unless the model
        #    already knows what that step does (from earlier sessions)
        decision = self._decision_outputs()
        plans = []
        base_deficit = self._deficit(self.base)
        for a in self.actions:
            up = self._predict(model, inc, _units_to_offsets({}, {a: 1.0})[0])
            down = self._predict(model, inc, _units_to_offsets({}, {a: -1.0})[0])
            if base_deficit > 0 and abs(up["deficit"] - down["deficit"]) > 1e-6:
                direction = 1.0 if up["deficit"] < down["deficit"] else -1.0  # measure the repair direction
            else:
                direction = 1.0 if up["objective"][0] >= down["objective"][0] else -1.0
            offsets, taken = _units_to_offsets({}, {a: direction})
            if not taken:
                offsets, taken = _units_to_offsets({}, {a: -direction})
            uncertain = model.uncertain(offsets_to_units(offsets), decision)
            plans.append((a, offsets, taken, uncertain))
        to_probe = [p for p in plans if p[3]]
        skipped = [p[0] for p in plans if not p[3]]
        if req.max_probes is not None:
            skipped += [p[0] for p in to_probe[req.max_probes:]]
            to_probe = to_probe[: req.max_probes]
        for a, offsets, taken, uncertain in to_probe:
            self._check_cancel()
            predicted = self._predict(model, inc, offsets)
            self._report_progress(f"probe {a}")
            score = self._evaluate(offsets, "probe")
            evaluated.add(key_of(offsets))
            delta = deltas(score, self.base)
            verdict = judge(score, self.base, req.tolerance_pp, self.floors)
            notes = self._check_predictions(len(self.steps), taken, predicted, delta, score)
            rationale = (f"measure the response to {ACTIONS[a].label.lower()} (still uncertain: "
                         f"{', '.join(uncertain[:4])}{'…' if len(uncertain) > 4 else ''})")
            self._record("probe", taken, offsets, score, self.base, predicted, False, verdict.passed,
                         verdict.reasons, rationale, notes)
            model.add(offsets_to_units(offsets), delta)

        # 3–6. search
        shrinks = 0
        stop_reason = "budget used"
        repair_budget = round(1.5 * req.budget)  # own budget: repairing is a prerequisite, not the goal
        while True:
            self._check_cancel()
            if self._deficit(inc.score, clearance=True) > 0.0 and self.evals["repair"] < repair_budget:
                repairing = True
            elif self._deficit(inc.score) > 0.0:
                stop_reason = "repair budget used"
                break
            else:
                repairing = False
                if self.repair_keys and self.repaired_at is None:
                    self.repaired_at = inc.step_index
                    self.floors = self._floors(inc.score)  # from now on: no worse than the repaired point
                if self.evals["search"] >= req.budget:
                    break
            cands = [c for c in self._candidates(model, inc, radius) if key_of(c["offsets"]) not in evaluated]
            if repairing:
                inc_def = self._deficit(inc.score, clearance=True)
                for c in cands:
                    c["repair"] = inc_def - c["pred"]["deficit"] + 0.01 * (c["pred"]["objective"][0] - inc.objective)
                best = max((c for c in cands if c["pred"]["safe_repair"]), key=lambda c: c["repair"], default=None)
                hopeless = best is None or best["repair"] < 0.02
            else:
                best = max((c for c in cands if c["pred"]["safe"]), key=lambda c: c["score"], default=None)
                hopeless = best is None or best["score"] < req.min_gain
            if hopeless:
                if max(radius.values()) > 0.25 + 1e-9 and shrinks < 4 and not repairing:
                    radius = {a: max(0.25, r * 0.5) for a, r in radius.items()}
                    shrinks += 1
                    continue
                if repairing and max(radius.values()) < req.max_radius and shrinks < 4:
                    radius = {a: min(req.max_radius, r * 2.0) for a, r in radius.items()}  # repairs need bigger moves
                    shrinks += 1
                    continue
                stop_reason = ("no untried change is predicted to bring every limit back within the trust region"
                               if repairing else
                               "no untried proposal is predicted to improve the goal while keeping every margin "
                               "above its floor (converged within the trust region)")
                break
            shrinks = 0
            offsets, taken, predicted = best["offsets"], best["step"], best["pred"]
            self._report_progress(f"{'repair' if repairing else 'try'} {describe_step(taken)}")
            score = self._evaluate(offsets, "repair" if repairing else "search")
            evaluated.add(key_of(offsets))
            delta = deltas(score, inc.score)
            verdict = judge(score, self.base, req.tolerance_pp, self.floors)
            obj = objective(score, self.base, req.goal, req.stress_weight)
            gain = obj - inc.objective
            idx = len(self.steps)
            notes = self._check_predictions(idx, taken, predicted, delta, score)
            exp_gain = predicted["objective"][0] - inc.objective
            if repairing:
                new_def = self._deficit(score, clearance=True)
                inc_def = self._deficit(inc.score, clearance=True)
                ok_keys = [k for k in self.floors if inc.score.margins_pct[k] >= 0.0]
                new_problems = [f"{k} margin {score.margins_pct[k]:.2f} % below floor {self.floors[k]:.2f} %"
                                for k in ok_keys if score.margins_pct[k] < self.floors[k]]
                new_problems += [v for v in score.violations if v.startswith(("DTCs", "protection"))
                                 and v not in inc.score.violations]
                # must beat the simulator's scatter (~0.1 pp per margin), unless it completes the repair
                accepted = (new_def == 0.0 or new_def < inc_def - 0.05) and not new_problems
                if new_problems:
                    notes.append(self._mistake(idx, "constraint_surprise", list(taken), "verdict",
                                               "repair step broke another limit: " + "; ".join(new_problems[:3])))
                rationale = (f"repair: limit excess {inc_def:.2f} → {new_def:.2f} "
                             f"(predicted {predicted['deficit']:.2f}), objective {gain:+.2f} → "
                             + ("accepted" if accepted else "rejected"))
                self._record("repair", taken, offsets, score, inc.score, predicted, accepted, not new_problems
                             and self._deficit(score) == 0.0, new_problems or list(score.violations), rationale, notes)
                model.add(offsets_to_units(offsets), deltas(score, self.base))
                if accepted:
                    inc = _Point(offsets, score, obj, idx)
                    chain.append(inc)
                    model.set_center(offsets_to_units(offsets))
                    for a in taken:
                        radius[a] = min(req.max_radius, radius[a] * 1.5)
                else:
                    for a in taken:
                        radius[a] = max(0.125, radius[a] * 0.5)
                continue
            accepted = verdict.passed and gain >= req.min_gain / 2.0
            if not verdict.passed:
                notes.append(self._mistake(idx, "constraint_surprise", list(taken), "verdict",
                                           "predicted safe but failed: " + "; ".join(verdict.reasons[:3])))
            elif gain < 0 and exp_gain > req.min_gain:
                notes.append(self._mistake(idx, "regression", list(taken), "objective",
                                           f"predicted {exp_gain:+.2f}, measured {gain:+.2f}"))
            if accepted:
                rationale = (f"predicted {exp_gain:+.2f} ± {predicted['objective'][1]:.2f}, measured {gain:+.2f}; "
                             "all margins above their floors → accepted")
            elif verdict.passed:
                rationale = (f"predicted {exp_gain:+.2f}, measured {gain:+.2f} — below the minimum gain "
                             f"{req.min_gain / 2:.2f} → rejected")
            else:
                rationale = "failed the battery → rejected: " + "; ".join(verdict.reasons[:2])
            self._record("search", taken, offsets, score, inc.score, predicted, accepted, verdict.passed,
                         verdict.reasons, rationale, notes)
            model.add(offsets_to_units(offsets), deltas(score, self.base))
            if accepted:
                inc = _Point(offsets, score, obj, idx)
                chain.append(inc)
                model.set_center(offsets_to_units(offsets))
                for a in taken:
                    radius[a] = min(req.max_radius, radius[a] * 1.5)
            else:
                for a in taken:
                    radius[a] = max(0.125, radius[a] * 0.5)

        # an unfinished repair is never recommended: report how far it got, keep the baseline
        repair = None
        if self._deficit(self.base) > 0:
            best = min(chain, key=lambda c: self._deficit(c.score))
            repair = {"start_excess": self._deficit(self.base), "best_excess": self._deficit(best.score),
                      "complete": self._deficit(inc.score) == 0.0, "best_offsets": best.offsets,
                      "best_changes": describe_offsets(best.offsets),
                      "remaining": list(best.score.violations)}
            if not repair["complete"]:
                stop_reason = ("the starting calibration exceeds limits on this engine model and the allowed actions "
                               "could not bring every limit back (excess "
                               f"{repair['start_excess']:.2f} → {repair['best_excess']:.2f}); nothing is recommended — "
                               "the remaining fix needs changes outside the tuner's remit (protection tables, "
                               "hardware, or limits reviewed by an engineer)")
                chain = chain[:1]
                inc = chain[0]

        # 7. validation on the larger battery
        validation = None
        final = inc
        if req.validate and len(chain) > 1:
            final = chain[0]
            tried = []
            for cand in list(reversed(chain[1:]))[:3]:
                self._report_progress("validation")
                sv = self._evaluate(cand.offsets, "validation", vbat)
                v = judge(sv, self.base_v, req.tolerance_pp)
                obj_v = objective(sv, self.base_v, req.goal, req.stress_weight)
                # a repaired calibration may cost performance: when the baseline itself broke a limit,
                # passing is what counts
                ok = v.passed and (obj_v > 0.0 or bool(self.base_v.violations) or bool(self.base.violations))
                idx = len(self.steps)
                notes = []
                if not v.passed:
                    notes.append(self._mistake(idx, "validation_failure", list(cand.offsets), "verdict",
                                               "passed the search battery but failed validation: "
                                               + "; ".join(v.reasons[:3])))
                self._record("validation", {}, cand.offsets, sv, self.base_v, None, ok, v.passed, v.reasons,
                             "validated on the larger battery" if ok else "did not validate", notes, vbat)
                tried.append({"step": idx, "offsets": cand.offsets, "passed": v.passed, "objective": obj_v,
                              "reasons": v.reasons, "inherited": v.inherited})
                if ok:
                    final = cand
                    validation = {"fidelity": req.validation_fidelity, "passed": True, "objective": obj_v,
                                  "score": sv.to_dict(include_curves=False), "tried": tried,
                                  "baseline_score": self.base_v.to_dict(include_curves=False)}
                    break
            if validation is None:
                validation = {"fidelity": req.validation_fidelity, "passed": False, "objective": 0.0,
                              "tried": tried, "baseline_score": self.base_v.to_dict(include_curves=False),
                              "note": "No candidate passed validation; the recommendation falls back to the baseline."}
        report = self._report(findings, final, chain, validation, stop_reason, model, prior, skipped,
                              [p[0] for p in to_probe], kb_trials, repair)
        return report

    def _candidates(self, model: ResponseModel, inc: _Point, radius: dict[str, float]) -> list[dict]:
        singles = []
        for a in self.actions:
            seen = set()
            for mult in (1.0, -1.0, 0.5, -0.5):
                offsets, taken = _units_to_offsets(inc.offsets, {a: mult * radius[a]})
                if not taken or abs(taken[a]) < 0.1:
                    continue
                key = round(taken[a], 4)
                if key in seen:
                    continue
                seen.add(key)
                singles.append(self._scored(model, inc, offsets, taken))
        # pairs: the most promising singles (safe or not), combined with each other and with "relief"
        # moves that most increase the tightest margins — a trade, e.g. take a little top-end torque to
        # make room for more mid-range torque
        cur = inc.score.margins_pct
        binding = sorted(self.floors, key=lambda k: cur.get(k, 0.0) - self.floors[k])[:2]

        def relief(c: dict) -> float:
            return sum(c["pred"]["outputs"][f"margin:{k}"][0] for k in binding)

        by_score = sorted(singles, key=lambda c: -c["score"])
        pool = ([c for c in by_score if c["pred"]["safe"]][:4] + by_score[:4]
                + [c for c in sorted(singles, key=lambda c: -relief(c)) if relief(c) > 0][:3])
        pairs, seen_pairs = [], set()
        for c1, c2 in itertools.combinations(pool, 2):
            if set(c1["step"]) & set(c2["step"]):
                continue
            offsets, taken = _units_to_offsets(inc.offsets, {**c1["step"], **c2["step"]})
            key = tuple(sorted(taken.items()))
            if len(taken) == 2 and key not in seen_pairs:
                seen_pairs.add(key)
                pairs.append(self._scored(model, inc, offsets, taken))
        return singles + pairs

    def _scored(self, model: ResponseModel, inc: _Point, offsets: dict, step: dict) -> dict:
        pred = self._predict(model, inc, offsets)
        mean, std = pred["objective"]
        return {"offsets": offsets, "step": step, "pred": pred,
                "score": mean - self.req.risk_aversion * std - inc.objective}

    # ------------------------------------------------------------------ report
    def _report(self, findings, final: _Point, chain, validation, stop_reason, model: ResponseModel, prior,
                skipped, probed, kb_trials, repair: dict | None = None) -> dict:
        req = self.req
        base = self.base
        final_cal = apply_offsets(self.baseline_cal, final.offsets,
                                  f"{self.baseline_cal.name} — tuning engine ({req.goal})") if final.offsets \
            else self.baseline_cal.copy()
        fs = final.score
        metrics_pct = {k: round((fs.metrics[k] / base.metrics[k] - 1.0) * 100.0, 3)
                       for k in base.metrics if base.metrics.get(k) and k in fs.metrics}
        margins_pp = {k: round(fs.margins_pct[k] - base.margins_pct[k], 3) for k in base.margins_pct}
        zs = self.z_log
        ze = self.z_eff_log

        def cover(v: list[float]) -> float | None:
            return round(sum(1 for z in v if abs(z) <= 2.0) / len(v), 3) if v else None

        calib = {"n": len(zs), "coverage_2sigma": cover(ze), "coverage_2sigma_raw": cover(zs),
                 "rms_z": round(math.sqrt(sum(z * z for z in zs) / len(zs)), 3) if zs else None,
                 "note": "Share of measured effects inside the predicted ±2σ band: with the caution factor (used for "
                         "decisions) and raw (the model alone). ~95 % means honest error bars; much less means "
                         "over-confident."}
        accepted = [s.index for s in self.steps if s.phase in ("search", "repair") and s.accepted]
        rejected = [s.index for s in self.steps if s.phase in ("search", "repair") and not s.accepted]
        summary = {
            "goal": req.goal, "engine_key": self.engine_key, "objective_search": round(final.objective, 4),
            "objective_validated": validation["objective"] if validation and validation.get("passed") else None,
            "validated": bool(validation and validation.get("passed")),
            "evaluations": dict(self.evals), "evaluations_total": sum(self.evals.values()),
            "accepted_steps": len(accepted), "rejected_steps": len(rejected), "mistakes": len(self.mistakes),
            "constraint_surprises": sum(1 for m in self.mistakes if m["kind"] == "constraint_surprise"),
            "probes_skipped": len(skipped), "changes": describe_offsets(final.offsets),
            "baseline_violations": list(base.violations),
            "repaired": bool(repair and repair["complete"]),
            "final_within_limits": self._deficit(fs) == 0.0,
            "metrics_pct": metrics_pct, "ready_for_vehicle": False,
        }
        report = {
            "id": self.sid, "tuner_version": TUNER_VERSION, "engine": {"id": self.config.id, "name": self.config.name,
                                                                       "key": self.engine_key},
            "project_id": self.project_id,
            "goal": {"id": req.goal, **GOALS[req.goal]}, "request": req.to_dict(), "limits": self.limits.to_dict(),
            "floors": self.floors, "validation_gaps": self.gaps,
            "battery": {"fidelity": self.battery.fidelity, "conditions": {c: CONDITION_INFO[c] for c in self.battery.conditions},
                        "adverse": sorted(ADVERSE & set(self.battery.conditions))},
            "diagnosis": findings, "stop_reason": stop_reason, "summary": summary,
            "baseline": base.to_dict(include_curves=False),
            "final": {"offsets": final.offsets, "changes": describe_offsets(final.offsets),
                      "score": fs.to_dict(include_curves=False), "metrics_pct": metrics_pct, "margins_pp": margins_pp,
                      "calibration_hash": final_cal.content_hash()},
            "validation": validation, "repair": repair,
            "steps": [s.to_dict() for s in self.steps],
            "mistakes": self.mistakes,
            "model": {"coefficients": model.coefficients(), "prior_sources": prior.source,
                      "caution": {k: round(v, 3) for k, v in model.caution.items()}, "calibration": calib,
                      "units": {k: output_unit(k) for k in model.prior.mean}},
            "learning": {"kb_trials_before": kb_trials, "prior_trials_used": prior.trials_used,
                         "probed": probed, "probes_skipped": skipped},
            "curves": {"baseline": base.curves.get("nominal", []), "final": fs.curves.get("nominal", [])},
            "calibration": final_cal.to_dict(),
            "ready_for_vehicle": False,
            "disclaimer": DISCLAIMER,
        }
        self.store.finish_session(self.sid, "done", summary, report)
        return report


def run_tuning(config: EngineConfig, baseline: Calibration, request: TuningRequest | dict | None = None,
               kb: KnowledgeBase | None = None, **kw) -> dict:
    req = request if isinstance(request, TuningRequest) else TuningRequest.from_dict(request or {})
    return TuningSession(config, baseline, req, kb, **kw).run()
