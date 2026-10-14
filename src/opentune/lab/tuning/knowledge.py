"""Experience store and learned response model.

Every calibration experiment the tuning engine runs in the simulator is stored: the action, the measured
effect on performance, efficiency and every stress margin, what the model had predicted, and whether the
change was accepted. That store is what makes the tuner improve:

* **Response model.** For each output (a metric's % change, a stress margin's change in percentage points,
  knock-retard change in degrees) a Bayesian linear model ``Δy ≈ Σ_a w_a · x_a`` over the action units
  ``x``. Its prior comes from (in order of preference) earlier experiments on the same engine, on the
  same engine family, on any engine, and finally a small *engineering prior* (directional rules of thumb,
  labelled as assumptions, with wide uncertainty). Predictions carry a standard deviation.
* **Learning from mistakes.** Prediction errors are tracked as z-scores per output. If the model has been
  over-confident (actual results outside its error bars more often than expected), predicted uncertainty
  is inflated by a *caution factor* — so the safety screen gets stricter where the model has been wrong.
  Actions that produced constraint surprises start later sessions with a smaller trust region.
* **Fewer experiments over time.** An action is only re-measured ("probed") at the start of a session when
  the model's uncertainty about it is still large. Knowledge from earlier sessions removes probes.

Everything in the store is simulated. It describes the synthetic model, not real engines.
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from opentune.lab.tuning.actions import ACTIONS
from opentune.lab.tuning.evaluator import STRESS_KEYS, Scorecard

METRIC_OUTPUTS: tuple[str, ...] = ("top_power_kw", "mean_power_kw", "mean_torque_nm", "part_load_bsfc_g_kwh")
MARGIN_OUTPUTS: tuple[str, ...] = tuple(f"margin:{k}" for k in STRESS_KEYS)
KNOCK_OUTPUTS: tuple[str, ...] = ("knock:nominal", "knock:adverse")
OUTPUTS: tuple[str, ...] = METRIC_OUTPUTS + MARGIN_OUTPUTS + KNOCK_OUTPUTS


def output_unit(key: str) -> str:
    return "%" if key in METRIC_OUTPUTS else "pp" if key.startswith("margin:") else "°"


# Irreducible scatter of each output in the deterministic simulator (bin edges, thermal history of a
# sweep). Chosen from repeated small-step probes on the presets; an assumption for other engines.
NOISE_FLOOR = {**{k: 0.05 for k in METRIC_OUTPUTS}, **{k: 0.10 for k in MARGIN_OUTPUTS},
               **{k: 0.08 for k in KNOCK_OUTPUTS}}
# Prior standard deviation of a linear coefficient (per action unit) and of a curvature coefficient
# (per action unit²) when nothing is known
PRIOR_SCALE = {**{k: 2.0 for k in METRIC_OUTPUTS}, **{k: 2.0 for k in MARGIN_OUTPUTS},
               **{k: 0.8 for k in KNOCK_OUTPUTS}}
CURVATURE_SCALE = {k: 0.3 * v for k, v in PRIOR_SCALE.items()}
# A one-unit step is "known well enough" (no probe needed) when its predicted change has a model
# standard deviation below this
PROBE_THRESHOLD = {**{k: 0.4 for k in METRIC_OUTPUTS}, **{k: 0.6 for k in MARGIN_OUTPUTS},
                   **{k: 0.4 for k in KNOCK_OUTPUTS}}
LOCALITY_UNITS = 3.0  # experiments further than this (action units) from the current point count less


def engineering_rules(kind: str, zone: str) -> dict[str, float]:
    """Directional rules of thumb per action unit (assumptions; deliberately weak priors).

    torque (+3 %): more torque/power in its zone; less torque/pressure/turbo/charge-temperature margin.
    spark (+1°): a little more torque, less peak-pressure margin, more knock control when hot.
    lambda (+0.02, leaner): a little less power, less exhaust-temperature margin, more min-lambda margin.
    part_spark (+1°): part-load BSFC usually improves while below MBT.
    """
    if kind == "torque":
        r = {"margin:torque_nm": -1.5, "margin:peak_pressure_bar": -0.5, "margin:turbo_krpm": -0.5,
             "margin:iat_c": -0.5, "margin:egt_c": -0.2}
        r.update({"top_power_kw": 1.0, "mean_power_kw": 0.5} if zone == "high" else
                 {"mean_torque_nm": 1.0, "mean_power_kw": 0.8})
        return r
    if kind == "spark":
        return {"mean_power_kw": 0.3, "mean_torque_nm": 0.3 if zone != "high" else 0.0,
                "top_power_kw": 0.3 if zone == "high" else 0.0, "margin:peak_pressure_bar": -1.0,
                "knock:adverse": 0.5}
    if kind == "lambda":
        return {"mean_power_kw": -0.2, "margin:egt_c": -0.3, "margin:min_lambda": 1.0}
    if kind == "part_spark":
        return {"part_load_bsfc_g_kwh": -0.3}
    return {}


def scorecard_outputs(score: Scorecard) -> dict[str, float]:
    """Absolute values the changes are computed from."""
    out = {k: float(score.metrics.get(k, 0.0)) for k in METRIC_OUTPUTS}
    out.update({f"margin:{k}": float(score.margins_pct.get(k, 0.0)) for k in STRESS_KEYS})
    out["knock:nominal"] = float(score.knock.get("nominal_deg", 0.0))
    out["knock:adverse"] = float(score.knock.get("adverse_deg", 0.0))
    return out


def deltas(result: Scorecard, reference: Scorecard) -> dict[str, float]:
    """Effect of a change: metrics in % of the reference, margins in percentage points, knock in degrees."""
    a, b = scorecard_outputs(result), scorecard_outputs(reference)
    d = {}
    for k in OUTPUTS:
        if k in METRIC_OUTPUTS:
            d[k] = (a[k] / b[k] - 1.0) * 100.0 if b[k] else 0.0
        else:
            d[k] = a[k] - b[k]
    return {k: round(v, 5) for k, v in d.items()}


def offsets_to_units(offsets: dict[str, float]) -> dict[str, float]:
    return {a: v / ACTIONS[a].step for a, v in offsets.items() if a in ACTIONS}


# ---------------------------------------------------------------------------------------- response model
@dataclass
class Prior:
    mean: dict[str, np.ndarray]  # output -> (2p,) coefficient means: p slopes, then p curvatures
    cov: dict[str, np.ndarray]  # output -> (2p, 2p) covariance (slope/curvature of one action correlated)
    source: dict[str, str]  # action -> "engine (n trials)" | "family …" | "all engines …" | "engineering prior"
    trials_used: int = 0
    lo: np.ndarray | None = None  # explored offsets (action units) behind this prior
    hi: np.ndarray | None = None


class ResponseModel:
    """Quadratic response surface per output, fitted by Bayesian linear regression.

    ``y(o) − y(baseline) ≈ Σ_a (w_a · o_a + q_a · o_a²)`` where ``o`` is the calibration's offset from the
    baseline in action units. The curvature terms let the model find optima (e.g. the lambda that gives
    the most power) instead of extrapolating a slope. Experiments far from the current point are
    down-weighted so the surface stays local. Predictions are *changes* between two calibrations, anchored
    on a measured one, so model bias at the anchor cancels.
    """

    def __init__(self, actions: list[str], prior: Prior, caution: dict[str, float] | None = None) -> None:
        self.actions = list(actions)
        self.prior = prior
        self.caution = {k: 1.0 for k in OUTPUTS} | (caution or {})
        self.O: list[np.ndarray] = []
        self.Y: list[dict[str, float]] = []
        self.center = np.zeros(len(self.actions))
        self._post: dict[str, tuple[np.ndarray, np.ndarray]] | None = None

    def vec(self, units: dict[str, float]) -> np.ndarray:
        return np.array([float(units.get(a, 0.0)) for a in self.actions])

    @staticmethod
    def features(o: np.ndarray) -> np.ndarray:
        return np.concatenate([o, o * o])

    def data_range(self) -> tuple[np.ndarray, np.ndarray]:
        """Explored offsets per action (session data and the prior's data, always including 0)."""
        lo = np.minimum(0.0, self.prior.lo if self.prior.lo is not None else 0.0)
        hi = np.maximum(0.0, self.prior.hi if self.prior.hi is not None else 0.0)
        if self.O:
            pts = np.array(self.O)
            lo, hi = np.minimum(lo, pts.min(axis=0)), np.maximum(hi, pts.max(axis=0))
        return lo, hi

    def predict_features(self, o: np.ndarray) -> np.ndarray:
        """Features for prediction: inside the explored range the quadratic; outside it the square is
        continued along its tangent at the edge, so the surface is extrapolated *linearly*. A fitted
        parabola must not "turn around" beyond the data (e.g. predict that cutting even more torque heats
        the exhaust again) — the classic failure of quadratic response surfaces."""
        lo, hi = self.data_range()
        edge = np.clip(o, lo, hi)
        sq = edge * edge + 2.0 * edge * (o - edge)
        return np.concatenate([o, sq])

    def add(self, units: dict[str, float], delta_vs_baseline: dict[str, float]) -> None:
        self.O.append(self.vec(units))
        self.Y.append(dict(delta_vs_baseline))
        self._post = None

    def set_center(self, units: dict[str, float]) -> None:
        self.center = self.vec(units)
        self._post = None

    @property
    def n(self) -> int:
        return len(self.O)

    def _posterior(self) -> dict[str, tuple[np.ndarray, np.ndarray]]:
        if self._post is not None:
            return self._post
        post = {}
        q = 2 * len(self.actions)
        if self.O:
            pts = np.array(self.O)
            F = np.array([self.features(o) for o in pts])
            d2 = ((pts - self.center) ** 2).sum(axis=1)
            W = np.exp(-d2 / (2.0 * LOCALITY_UNITS**2))
        else:
            F, W = np.zeros((0, q)), np.zeros(0)
        for k in OUTPUTS:
            m0 = self.prior.mean[k]
            noise = NOISE_FLOOR[k]
            prec = np.linalg.inv(self.prior.cov[k] + 1e-12 * np.eye(q))
            rhs = prec @ m0
            if len(F):
                y = np.array([row.get(k, 0.0) for row in self.Y])
                prec = prec + (F.T * W) @ F / noise**2
                rhs = rhs + (F.T * W) @ y / noise**2
            cov = np.linalg.inv(prec)
            post[k] = (cov @ rhs, cov)
        self._post = post
        return post

    def predict(self, new: dict[str, float], ref: dict[str, float] | None = None,
                raw: bool = False) -> dict[str, tuple[float, float]]:
        """Predicted (mean, std) change of each output going from calibration ``ref`` to ``new`` (both as
        offsets from the baseline in action units). ``raw`` leaves out the caution factor."""
        d = self.predict_features(self.vec(new)) - self.predict_features(self.vec(ref or {}))
        out = {}
        for k, (m, cov) in self._posterior().items():
            mean = float(d @ m)
            std = math.sqrt(max(0.0, float(d @ cov @ d)) + NOISE_FLOOR[k] ** 2)
            out[k] = (mean, std if raw else std * self.caution.get(k, 1.0))
        return out

    def coefficient(self, action: str, output: str, curvature: bool = False) -> tuple[float, float]:
        i = self.actions.index(action) + (len(self.actions) if curvature else 0)
        m, cov = self._posterior()[output]
        return float(m[i]), float(math.sqrt(cov[i, i]))

    def uncertain(self, new: dict[str, float], outputs: list[str], ref: dict[str, float] | None = None) -> list[str]:
        """Outputs whose predicted change from ``ref`` to ``new`` is still too uncertain (model uncertainty
        only, without simulator scatter) — the question a probe answers: "do I know what this step does?"
        """
        d = self.predict_features(self.vec(new)) - self.predict_features(self.vec(ref or {}))
        post = self._posterior()
        return [k for k in outputs if math.sqrt(max(0.0, float(d @ post[k][1] @ d))) > PROBE_THRESHOLD[k]]

    def coefficients(self) -> dict:
        return {a: {k: {"slope": [round(v, 4) for v in self.coefficient(a, k)],
                        "curvature": [round(v, 4) for v in self.coefficient(a, k, True)]} for k in OUTPUTS}
                for a in self.actions}


def engineering_prior(actions: list[str]) -> Prior:
    p = len(actions)
    mean = {k: np.zeros(2 * p) for k in OUTPUTS}
    cov = {k: np.diag(np.concatenate([np.full(p, PRIOR_SCALE[k]), np.full(p, CURVATURE_SCALE[k])]) ** 2)
           for k in OUTPUTS}
    for i, a in enumerate(actions):
        for k, v in engineering_rules(ACTIONS[a].kind, ACTIONS[a].zone).items():
            mean[k][i] = v
    return Prior(mean, cov, {a: "engineering prior" for a in actions})


# ---------------------------------------------------------------------------------------- store
SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY, created_utc TEXT, engine_id TEXT, engine_key TEXT, family TEXT, goal TEXT,
    status TEXT, request_json TEXT, summary_json TEXT, report_json TEXT, project_id TEXT
);
CREATE TABLE IF NOT EXISTS trials (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, idx INTEGER, engine_key TEXT, family TEXT,
    phase TEXT, step_json TEXT, offsets_json TEXT, delta_json TEXT, predicted_json TEXT,
    objective REAL, accepted INTEGER, passed INTEGER, reasons_json TEXT, fidelity TEXT, created_utc TEXT
);
CREATE INDEX IF NOT EXISTS trials_engine ON trials(engine_key);
CREATE INDEX IF NOT EXISTS trials_family ON trials(family);
CREATE TABLE IF NOT EXISTS mistakes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, trial_idx INTEGER, engine_key TEXT, kind TEXT,
    actions_json TEXT, output TEXT, detail TEXT, z REAL, created_utc TEXT
);
CREATE TABLE IF NOT EXISTS benchmarks (
    id TEXT PRIMARY KEY, created_utc TEXT, suite TEXT, tuner_version TEXT, kb_trials_before INTEGER,
    learn INTEGER, results_json TEXT, aggregate_json TEXT
);
"""


def _utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def engine_family(engine_key: str) -> str:
    return engine_key.split("~", 1)[0]


class KnowledgeBase:
    """SQLite store of sessions, trials, mistakes and benchmark results (``:memory:`` for tests)."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------ writing
    def new_session(self, engine_id: str, engine_key: str, goal: str, request: dict,
                    project_id: str | None = None) -> str:
        sid = uuid.uuid4().hex[:12]
        with self._lock:
            self._db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                             (sid, _utc(), engine_id, engine_key, engine_family(engine_key), goal, "running",
                              json.dumps(request), None, None, project_id))
            self._db.commit()
        return sid

    def finish_session(self, sid: str, status: str, summary: dict, report: dict) -> None:
        with self._lock:
            self._db.execute("UPDATE sessions SET status=?, summary_json=?, report_json=? WHERE id=?",
                             (status, json.dumps(summary), json.dumps(report), sid))
            self._db.commit()

    def add_trial(self, sid: str, idx: int, engine_key: str, phase: str, step: dict, offsets: dict,
                  delta: dict, predicted: dict | None, objective: float, accepted: bool, passed: bool,
                  reasons: list[str], fidelity: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO trials (session_id, idx, engine_key, family, phase, step_json, offsets_json, "
                "delta_json, predicted_json, objective, accepted, passed, reasons_json, fidelity, created_utc) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sid, idx, engine_key, engine_family(engine_key), phase, json.dumps(step), json.dumps(offsets),
                 json.dumps(delta), json.dumps(predicted) if predicted else None, objective, int(accepted),
                 int(passed), json.dumps(reasons), fidelity, _utc()))
            self._db.commit()

    def add_mistake(self, sid: str, trial_idx: int, engine_key: str, kind: str, actions: list[str],
                    output: str, detail: str, z: float | None = None) -> None:
        with self._lock:
            self._db.execute("INSERT INTO mistakes (session_id, trial_idx, engine_key, kind, actions_json, "
                             "output, detail, z, created_utc) VALUES (?,?,?,?,?,?,?,?,?)",
                             (sid, trial_idx, engine_key, kind, json.dumps(actions), output, detail, z, _utc()))
            self._db.commit()

    def save_benchmark(self, suite: str, version: str, kb_trials_before: int, learn: bool, results: list[dict],
                       aggregate: dict) -> str:
        bid = uuid.uuid4().hex[:12]
        with self._lock:
            self._db.execute("INSERT INTO benchmarks VALUES (?,?,?,?,?,?,?,?)",
                             (bid, _utc(), suite, version, kb_trials_before, int(learn), json.dumps(results),
                              json.dumps(aggregate)))
            self._db.commit()
        return bid

    def reset(self) -> None:
        """Delete everything (sessions, experiments, mistakes, benchmarks)."""
        with self._lock:
            for table in ("sessions", "trials", "mistakes", "benchmarks"):
                self._db.execute(f"DELETE FROM {table}")
            self._db.commit()

    # ------------------------------------------------------------------ reading
    def _rows(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def trial_count(self, engine_key: str | None = None) -> int:
        if engine_key is None:
            return int(self._rows("SELECT COUNT(*) FROM trials")[0][0])
        return int(self._rows("SELECT COUNT(*) FROM trials WHERE engine_key=?", (engine_key,))[0][0])

    def trials(self, where: str = "", args: tuple = (), limit: int = 5000) -> list[dict]:
        rows = self._rows(f"SELECT * FROM trials {where} ORDER BY id DESC LIMIT ?", (*args, limit))
        out = []
        for r in rows:
            out.append({"session_id": r["session_id"], "idx": r["idx"], "engine_key": r["engine_key"],
                        "family": r["family"], "phase": r["phase"], "step": json.loads(r["step_json"]),
                        "offsets": json.loads(r["offsets_json"]), "delta": json.loads(r["delta_json"]),
                        "predicted": json.loads(r["predicted_json"]) if r["predicted_json"] else None,
                        "objective": r["objective"], "accepted": bool(r["accepted"]), "passed": bool(r["passed"]),
                        "reasons": json.loads(r["reasons_json"]), "fidelity": r["fidelity"],
                        "created_utc": r["created_utc"]})
        return out

    def sessions(self, limit: int = 100) -> list[dict]:
        rows = self._rows("SELECT id, created_utc, engine_id, engine_key, goal, status, summary_json, project_id "
                          "FROM sessions ORDER BY created_utc DESC, rowid DESC LIMIT ?", (limit,))
        return [{"id": r["id"], "created_utc": r["created_utc"], "engine_id": r["engine_id"],
                 "engine_key": r["engine_key"], "goal": r["goal"], "status": r["status"],
                 "project_id": r["project_id"],
                 "summary": json.loads(r["summary_json"]) if r["summary_json"] else None} for r in rows]

    def session_report(self, sid: str) -> dict | None:
        rows = self._rows("SELECT report_json FROM sessions WHERE id=?", (sid,))
        if not rows or not rows[0]["report_json"]:
            return None
        return json.loads(rows[0]["report_json"])

    def mistakes(self, limit: int = 200, engine_key: str | None = None) -> list[dict]:
        where, args = ("WHERE engine_key=?", (engine_key,)) if engine_key else ("", ())
        rows = self._rows(f"SELECT * FROM mistakes {where} ORDER BY id DESC LIMIT ?", (*args, limit))
        return [{"session_id": r["session_id"], "trial_idx": r["trial_idx"], "engine_key": r["engine_key"],
                 "kind": r["kind"], "actions": json.loads(r["actions_json"]), "output": r["output"],
                 "detail": r["detail"], "z": r["z"], "created_utc": r["created_utc"]} for r in rows]

    def benchmarks(self, limit: int = 50) -> list[dict]:
        rows = self._rows("SELECT * FROM benchmarks ORDER BY created_utc DESC, rowid DESC LIMIT ?", (limit,))
        return [{"id": r["id"], "created_utc": r["created_utc"], "suite": r["suite"],
                 "tuner_version": r["tuner_version"], "kb_trials_before": r["kb_trials_before"],
                 "learn": bool(r["learn"]), "results": json.loads(r["results_json"]),
                 "aggregate": json.loads(r["aggregate_json"])} for r in rows]

    # ------------------------------------------------------------------ learning
    def prior(self, actions: list[str], engine_key: str, fidelity: str = "quick") -> Prior:
        """Prior for a new session: engine → family → all engines → engineering prior, per action.

        Only probe/search/repair experiments at the same battery fidelity are used (the outputs of different
        batteries are not the same quantities).
        """
        family = engine_family(engine_key)
        base = engineering_prior(actions)
        mean = {k: v.copy() for k, v in base.mean.items()}
        cov = {k: v.copy() for k, v in base.cov.items()}
        source = dict(base.source)
        used = 0
        lo, hi = np.zeros(len(actions)), np.zeros(len(actions))
        # (scope, filter, transfer std = rel·|m| + abs)
        common = "phase IN ('probe', 'search', 'repair') AND fidelity=?"
        levels = [("all engines", f"WHERE {common}", (fidelity,), 0.5, 0.3),
                  ("family", f"WHERE {common} AND family=?", (fidelity, family), 0.25, 0.15),
                  ("engine", f"WHERE {common} AND engine_key=?", (fidelity, engine_key), 0.10, 0.0)]
        for scope, where, args, rel, ab in levels:  # later (more specific) levels override earlier ones
            trials = self.trials(where, args)
            if not trials:
                continue
            fit = ResponseModel(actions, engineering_prior(actions))
            counts = dict.fromkeys(actions, 0)
            for t in trials:
                units = offsets_to_units(t["offsets"])
                moved = [a for a, u in units.items() if abs(u) > 1e-9]
                if not moved or any(a not in actions for a in moved):
                    continue  # rows with actions outside this session's set would be mis-attributed
                fit.add(units, t["delta"])
                for a in moved:
                    counts[a] += 1
            used = max(used, fit.n)
            if fit.n:
                pts = np.array(fit.O)
                lo, hi = np.minimum(lo, pts.min(axis=0)), np.maximum(hi, pts.max(axis=0))
            post = fit._posterior()
            p = len(actions)
            for i, a in enumerate(actions):
                if counts[a] < 1:
                    continue
                source[a] = f"{scope} ({counts[a]} trials)"
                idx = [i, i + p]
                for k in OUTPUTS:
                    m, c = post[k]
                    # keep this action's slope/curvature block (they are strongly correlated when the data
                    # only pins their sum); drop cross-action terms; widen for transfer to a new session
                    block = c[np.ix_(idx, idx)].copy()
                    for b, j in enumerate(idx):
                        scale = PRIOR_SCALE[k] if j < p else CURVATURE_SCALE[k]
                        block[b, b] += (rel * abs(m[j]) + ab * scale / 2.0) ** 2
                        mean[k][j] = m[j]
                    cov[k][np.ix_(idx, idx)] = block
                    for j in idx:  # remove stale cross-action covariance from a less specific level
                        others = [o for o in range(2 * p) if o not in idx]
                        cov[k][j, others] = 0.0
                        cov[k][others, j] = 0.0
        return Prior(mean, cov, source, used, lo, hi)

    def caution(self, window: int = 100) -> dict[str, float]:
        """Caution factor per output: RMS of past raw prediction z-scores (clipped to 1…4)."""
        rows = self.trials("WHERE predicted_json IS NOT NULL", (), window)
        zs: dict[str, list[float]] = {k: [] for k in OUTPUTS}
        for t in rows:
            for k, z in (t["predicted"] or {}).get("z_raw", {}).items():
                if k in zs:
                    zs[k].append(min(25.0, z * z))
        return {k: (min(4.0, max(1.0, math.sqrt(sum(v) / len(v)))) if len(v) >= 3 else 1.0) for k, v in zs.items()}

    def surprises(self, engine_key: str | None = None) -> dict[str, int]:
        """Number of constraint surprises per action (predicted safe, failed)."""
        counts: dict[str, int] = {}
        for m in self.mistakes(1000, engine_key):
            if m["kind"] in ("constraint_surprise", "validation_failure"):
                for a in m["actions"]:
                    counts[a] = counts.get(a, 0) + 1
        return counts

    def summary(self) -> dict:
        n_trials = self.trial_count()
        engines = [dict(r) for r in self._rows("SELECT engine_key, COUNT(*) AS trials, SUM(accepted) AS accepted "
                                               "FROM trials GROUP BY engine_key ORDER BY engine_key")]
        kinds = [dict(r) for r in self._rows("SELECT kind, COUNT(*) AS n FROM mistakes GROUP BY kind")]
        n_sessions = int(self._rows("SELECT COUNT(*) FROM sessions")[0][0])
        return {"trials": n_trials, "sessions": n_sessions, "engines": engines, "mistakes_by_kind": kinds,
                "caution": {k: round(v, 3) for k, v in self.caution().items()},
                "surprises_by_action": self.surprises(),
                "note": "Knowledge about the synthetic engine models only — not about real engines."}
