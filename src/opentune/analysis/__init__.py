"""AI performance analysis: deterministic rules plus an optional local LLM narrative."""

from opentune.analysis.analyzer import (
    AnalysisReport,
    Analyzer,
    Finding,
    PullSummary,
    analyze_samples,
    detect_pulls,
)
from opentune.analysis.local_model import (
    LocalModelClient,
    LocalModelConfig,
    LocalModelError,
    build_prompt,
    narrate,
)
from opentune.analysis.report import to_dict, to_markdown

__all__ = [
    "AnalysisReport",
    "Analyzer",
    "Finding",
    "LocalModelClient",
    "LocalModelConfig",
    "LocalModelError",
    "PullSummary",
    "analyze_samples",
    "build_prompt",
    "detect_pulls",
    "narrate",
    "to_dict",
    "to_markdown",
]
