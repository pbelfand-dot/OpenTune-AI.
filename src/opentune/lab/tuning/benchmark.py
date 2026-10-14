"""Tuner benchmark and training curriculum.

How do we know the tuning engine is getting better rather than just different? A fixed set of tasks is
run against the current knowledge base and scored the same way every time:

* **validated gain** — objective improvement that survived the validation battery (0 if none did).
  When the starting calibration already broke a limit on that engine, the tuner must repair it first and
  the validated "gain" is usually *negative*: the honest cost of getting back within limits,
* **evaluations** — simulator runs used (fewer is better: knowledge should replace experiments),
* **mistakes** — constraint surprises (predicted safe, failed), validation failures, prediction misses,
* **prediction coverage** — share of measured effects inside the model's 2σ band (≈ 0.95 is honest).

Benchmark tasks include **held-out engines**: copies of the presets whose assumption parameters are
perturbed (seeded, ~1σ of the spreads used by the dyno uncertainty estimate) and whose fuel can differ.
The training curriculum never uses them, so their scores show how knowledge transfers to an engine the
tuner has not seen — the situation a real engine (which never matches the model exactly) would be in.

By default a benchmark does not write to the knowledge base (``learn=False``), so runs are comparable.
Results are stored with the knowledge-base size, giving a history of tuner performance over time.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass

from opentune.lab.calibration.defaults import build_baseline
from opentune.lab.dyno.uncertainty import UNCERTAIN_PARAMETERS
from opentune.lab.physics.config import EngineConfig
from opentune.lab.physics.presets import get_preset
from opentune.lab.tuning.knowledge import KnowledgeBase
from opentune.lab.tuning.session import TUNER_VERSION, TuningRequest, TuningSession


@dataclass(frozen=True)
class Task:
    engine: str  # preset id
    goal: str
    variant: int | None = None  # held-out perturbation seed (None = nominal preset)
    octane_delta: float = 0.0  # fuel change (AKI) for this task
    budget: int = 10

    @property
    def engine_key(self) -> str:
        key = self.engine
        if self.variant is not None:
            key += f"~h{self.variant}"
        if self.octane_delta:
            key += f"~aki{self.octane_delta:+g}"
        return key

    @property
    def held_out(self) -> bool:
        return self.variant is not None or bool(self.octane_delta)

    def to_dict(self) -> dict:
        return {**asdict(self), "engine_key": self.engine_key, "held_out": self.held_out}


SUITES: dict[str, tuple[Task, ...]] = {
    "smoke": (Task("b48-inspired", "midrange_torque", budget=3),),
    "quick": (Task("b58-inspired", "max_power"),
              Task("b48-inspired", "midrange_torque"),
              Task("b58-inspired", "balanced", variant=1),
              Task("b48-inspired", "max_power", variant=2)),
    "standard": (Task("b58-inspired", "max_power"),
                 Task("b58-inspired", "efficiency"),
                 Task("b48-inspired", "midrange_torque"),
                 Task("b48-inspired", "power_band"),
                 Task("b58-inspired", "balanced", variant=1),
                 Task("b48-inspired", "max_power", variant=2),
                 Task("b58-inspired", "max_power", variant=3, octane_delta=-3.0),
                 Task("b48-inspired", "efficiency", variant=4)),
}

# Training curriculum: nominal presets only (never the held-out variants)
CURRICULUM: tuple[Task, ...] = (
    Task("b58-inspired", "max_power", budget=12),
    Task("b48-inspired", "max_power", budget=12),
    Task("b58-inspired", "midrange_torque", budget=12),
    Task("b48-inspired", "midrange_torque", budget=12),
    Task("b58-inspired", "efficiency", budget=8),
    Task("b48-inspired", "balanced", budget=12),
)


def variant_config(config: EngineConfig, seed: int, octane_delta: float = 0.0) -> EngineConfig:
    """A held-out engine: assumption parameters perturbed by seeded N(0, σ) draws (clipped to ±2σ)."""
    cfg = config.copy()
    rng = random.Random(1000 + seed)
    for p in UNCERTAIN_PARAMETERS:
        if cfg.provenance_of(p.path).value != "assumption":
            continue  # documented values stay
        z = max(-2.0, min(2.0, rng.gauss(0.0, 1.0)))
        cur = cfg.get(p.path)
        cfg.set(p.path, cur + z * p.sigma if p.absolute else cur * (1.0 + z * p.sigma))
    cfg.combustion.octane_aki += octane_delta
    cfg.id = f"{config.id}~h{seed}" + (f"~aki{octane_delta:+g}" if octane_delta else "")
    cfg.name = f"{config.name} (held-out variant {seed}{f', fuel {octane_delta:+g} AKI' if octane_delta else ''})"
    return cfg


def task_engine(task: Task) -> EngineConfig:
    cfg = get_preset(task.engine)
    if task.held_out:
        cfg = variant_config(cfg, task.variant or 0, task.octane_delta)
    return cfg


def run_task(task: Task, kb: KnowledgeBase, learn: bool, fidelity: str = "quick",
             cancel: Callable[[], bool] | None = None) -> dict:
    cfg = task_engine(task)
    baseline = build_baseline(cfg)  # each engine gets its own generated baseline calibration
    req = TuningRequest(goal=task.goal, budget=task.budget, fidelity=fidelity, learn=learn)
    t0 = time.perf_counter()
    report = TuningSession(cfg, baseline, req, kb, engine_key=task.engine_key, cancel=cancel).run()
    s = report["summary"]
    calib = report["model"]["calibration"]
    return {
        "task": task.to_dict(), "session_id": report["id"] if learn else None,
        "validated_gain": s["objective_validated"] or 0.0, "search_gain": s["objective_search"],
        "validated": s["validated"], "evaluations": s["evaluations_total"],
        "probes_skipped": s["probes_skipped"], "constraint_surprises": s["constraint_surprises"],
        "validation_failures": sum(1 for m in report["mistakes"] if m["kind"] == "validation_failure"),
        "prediction_misses": sum(1 for m in report["mistakes"] if m["kind"] == "prediction_miss"),
        "coverage_2sigma": calib["coverage_2sigma"], "changes": s["changes"],
        "unsafe_baseline": bool(s["baseline_violations"]), "repaired": s["repaired"],
        "seconds": round(time.perf_counter() - t0, 2),
    }


def aggregate(results: list[dict]) -> dict:
    n = len(results)
    if not n:
        return {}
    cov = [r["coverage_2sigma"] for r in results if r["coverage_2sigma"] is not None]
    held = [r for r in results if r["task"]["held_out"]]
    evals = sum(r["evaluations"] for r in results)
    gain = sum(r["validated_gain"] for r in results)
    return {
        "tasks": n, "mean_validated_gain": round(gain / n, 4),
        "mean_validated_gain_held_out": round(sum(r["validated_gain"] for r in held) / len(held), 4) if held else None,
        "validated_rate": round(sum(1 for r in results if r["validated"]) / n, 3),
        "mean_evaluations": round(evals / n, 2), "gain_per_100_evaluations": round(100.0 * gain / max(1, evals), 4),
        "constraint_surprises": sum(r["constraint_surprises"] for r in results),
        "validation_failures": sum(r["validation_failures"] for r in results),
        "prediction_misses": sum(r["prediction_misses"] for r in results),
        "mean_coverage_2sigma": round(sum(cov) / len(cov), 3) if cov else None,
        "unsafe_baselines": sum(1 for r in results if r.get("unsafe_baseline")),
        "repaired": sum(1 for r in results if r.get("repaired")),
        "seconds": round(sum(r["seconds"] for r in results), 1),
    }


def run_benchmark(kb: KnowledgeBase, suite: str = "quick", learn: bool = False,
                  progress: Callable[[float, str], None] | None = None,
                  cancel: Callable[[], bool] | None = None) -> dict:
    if suite not in SUITES:
        raise ValueError(f"Unknown suite {suite!r}; options: {', '.join(SUITES)}")
    before = kb.trial_count()
    results = []
    tasks = SUITES[suite]
    for i, task in enumerate(tasks):
        if progress:
            progress(i / len(tasks), f"{task.engine_key}: {task.goal}")
        results.append(run_task(task, kb, learn, cancel=cancel))
    agg = aggregate(results)
    bid = kb.save_benchmark(suite, TUNER_VERSION, before, learn, results, agg)
    return {"id": bid, "suite": suite, "tuner_version": TUNER_VERSION, "kb_trials_before": before, "learn": learn,
            "results": results, "aggregate": agg,
            "note": "Scores describe the tuner on synthetic engine models; they say nothing about real engines."}


def train(kb: KnowledgeBase, rounds: int = 1, tasks: tuple[Task, ...] = CURRICULUM,
          progress: Callable[[float, str], None] | None = None, cancel: Callable[[], bool] | None = None) -> dict:
    """Run the training curriculum (nominal presets only), storing every experiment in the knowledge base."""
    if not 1 <= rounds <= 20:
        raise ValueError("rounds must be 1–20")
    out = []
    total = rounds * len(tasks)
    for r in range(rounds):
        for i, task in enumerate(tasks):
            if progress:
                progress((r * len(tasks) + i) / total, f"round {r + 1}: {task.engine_key} {task.goal}")
            out.append({"round": r + 1, **run_task(task, kb, learn=True, cancel=cancel)})
    return {"sessions": out, "trials_after": kb.trial_count(),
            "aggregate_by_round": {r + 1: aggregate([o for o in out if o["round"] == r + 1]) for r in range(rounds)}}
