"""Engine physics: formulas, configuration, presets and the mean-value plant model."""

from opentune.lab.physics.config import PARAM_SPECS, EngineConfig, param_schema
from opentune.lab.physics.plant import Ambient, EnginePlant, PlantInputs, PlantOutputs
from opentune.lab.physics.presets import PRESETS, b48_inspired, b58_inspired, get_preset

__all__ = [
    "PARAM_SPECS",
    "PRESETS",
    "Ambient",
    "EngineConfig",
    "EnginePlant",
    "PlantInputs",
    "PlantOutputs",
    "b48_inspired",
    "b58_inspired",
    "get_preset",
    "param_schema",
]
