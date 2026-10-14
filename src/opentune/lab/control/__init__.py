"""Control-systems laboratory (generic loops and engine-in-the-loop experiments)."""

from opentune.lab.control.engine_tests import (
    ENGINE_EXPERIMENTS,
    GAIN_SCALARS,
    EngineExperiment,
    run_engine_experiment,
)
from opentune.lab.control.loop import (
    PLANT_KINDS,
    ControllerConfig,
    Experiment,
    LoopResult,
    PlantModel,
    fit_fopdt,
    frequency_response,
    simc_tuning,
    simulate_loop,
    step_metrics,
)

__all__ = [
    "ENGINE_EXPERIMENTS",
    "GAIN_SCALARS",
    "PLANT_KINDS",
    "ControllerConfig",
    "EngineExperiment",
    "Experiment",
    "LoopResult",
    "PlantModel",
    "fit_fopdt",
    "frequency_response",
    "run_engine_experiment",
    "simc_tuning",
    "simulate_loop",
    "step_metrics",
]
