"""Illustrative engine sound synthesis (firing-order harmonics, load noise, turbo whistle)."""

from opentune.lab.sound.synth import (
    PRESETS,
    firing_frequency,
    presets_list,
    rev_trace,
    synthesize,
    to_wav,
)

__all__ = ["PRESETS", "firing_frequency", "presets_list", "rev_trace", "synthesize", "to_wav"]
