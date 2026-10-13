"""Optional local LLM narrative for analysis reports.

Supports two free, self-hosted backends over plain HTTP (stdlib only):

* ``ollama`` — https://ollama.com (``/api/tags``, ``/api/generate``), default port 11434
* ``openai-compatible`` — llama.cpp server, LM Studio, etc. (``/v1/models``,
  ``/v1/chat/completions``)

By default only loopback addresses are allowed, so logs never leave the
machine and no paid cloud service can be used by accident. The rule-based
analysis works without any model.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlparse

from opentune.analysis.analyzer import AnalysisReport

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

SYSTEM_PROMPT = (
    "You are an automotive data analyst for the OpenTune AI datalogging tool. "
    "Explain the provided analysis results in clear, plain language for a car enthusiast. "
    "Prioritise safety: tell the driver to stop hard driving when a finding is critical. "
    "Only use the facts provided; say when data is insufficient. "
    "Do NOT provide ECU calibration values, map changes, memory addresses, security-access "
    "or immobiliser bypass information, or advice on disabling emissions equipment. "
    "Keep the answer under 250 words."
)


class LocalModelError(RuntimeError):
    pass


@dataclass
class LocalModelConfig:
    backend: str = "ollama"
    base_url: str = "http://127.0.0.1:11434"
    model: str = "llama3.2"
    timeout_s: float = 120.0
    allow_remote: bool = False


Opener = Callable[..., object]


class LocalModelClient:
    def __init__(self, config: LocalModelConfig, opener: Opener | None = None) -> None:
        self.config = config
        self._opener = opener or urllib.request.urlopen
        self._check_url()

    def _check_url(self) -> None:
        parsed = urlparse(self.config.base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise LocalModelError(f"Invalid model URL {self.config.base_url!r}")
        if not self.config.allow_remote and parsed.hostname not in LOCAL_HOSTS:
            raise LocalModelError(
                "Only local model servers (localhost) are allowed. OpenTune does not send "
                "your logs to cloud services.")
        if self.config.backend not in ("ollama", "openai-compatible"):
            raise LocalModelError(f"Unknown backend {self.config.backend!r}")

    def _url(self, path: str) -> str:
        return self.config.base_url.rstrip("/") + path

    def _request(self, path: str, payload: dict | None = None) -> dict:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self._url(path), data=data,
                                     headers={"Content-Type": "application/json"},
                                     method="GET" if payload is None else "POST")
        try:
            with self._opener(req, timeout=self.config.timeout_s) as resp:  # type: ignore[attr-defined]
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise LocalModelError(f"Cannot reach local model at {self.config.base_url}: {exc.reason}") from exc
        except (OSError, ValueError) as exc:
            raise LocalModelError(f"Local model request failed: {exc}") from exc

    def list_models(self) -> list[str]:
        if self.config.backend == "ollama":
            data = self._request("/api/tags")
            return [m.get("name", "") for m in data.get("models", [])]
        data = self._request("/v1/models")
        return [m.get("id", "") for m in data.get("data", [])]

    def generate(self, prompt: str, system: str = SYSTEM_PROMPT) -> str:
        if self.config.backend == "ollama":
            data = self._request("/api/generate", {
                "model": self.config.model, "prompt": prompt, "system": system, "stream": False,
            })
            text = data.get("response", "")
        else:
            data = self._request("/v1/chat/completions", {
                "model": self.config.model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                "temperature": 0.2,
            })
            try:
                text = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError) as exc:
                raise LocalModelError("Unexpected response from model server") from exc
        if not text.strip():
            raise LocalModelError("Model returned an empty response")
        return text.strip()


def build_prompt(report: AnalysisReport) -> str:
    lines = [
        f"Engine: BMW {report.engine}. Data source: {report.source or 'unknown'}.",
        f"Log duration {report.duration_s:.0f} s, {report.sample_count} samples, "
        f"health score {report.health_score}/100.",
    ]
    for i, pull in enumerate(report.pulls, 1):
        lines.append(
            f"Pull {i}: {pull.start_rpm:.0f}->{pull.end_rpm:.0f} rpm, peak boost "
            f"{(pull.peak_boost_bar or 0):.2f} bar, min timing {(pull.min_timing_deg or 0):.1f} deg, "
            f"IAT {(pull.iat_start_c or 0):.0f}->{(pull.iat_end_c or 0):.0f} C"
            + (f", peak power ~{pull.peak_power_kw:.0f} kW ({pull.power_method})" if pull.peak_power_kw else ""))
    if report.findings:
        lines.append("Findings:")
        for f in report.findings:
            lines.append(f"- [{f.severity.upper()}] {f.title}: {f.detail}")
    else:
        lines.append("Findings: none.")
    lines.append("Explain what this means for the car and what the owner should do next.")
    return "\n".join(lines)


def narrate(report: AnalysisReport, client: LocalModelClient) -> str:
    report.narrative = client.generate(build_prompt(report))
    return report.narrative
