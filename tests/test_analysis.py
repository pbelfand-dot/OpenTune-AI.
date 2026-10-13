import io
import json
import urllib.error

import pytest

from opentune.analysis import (
    LocalModelClient,
    LocalModelConfig,
    LocalModelError,
    analyze_samples,
    build_prompt,
    detect_pulls,
    narrate,
    to_dict,
    to_markdown,
)
from opentune.core.models import Sample
from opentune.obd import simulated_client
from opentune.simulation import SimulatedVehicle, run_simulation


def _titles(report):
    return [(f.severity, f.title) for f in report.findings]


@pytest.mark.parametrize("scenario", ["wot_pull", "track", "city", "cruise"])
def test_healthy_engine_has_no_findings(profile, scenario):
    samples = run_simulation(profile, scenario, duration_s=60, rate_hz=20)
    report = analyze_samples(samples, profile, "simulator")
    assert [f for f in report.findings if f.severity != "info"] == [], _titles(report)
    assert report.health_score >= 95


@pytest.mark.parametrize(
    ("fault", "severity", "needle"),
    [
        ("boost_leak", "critical", "Underboost"),
        ("wastegate_stuck", "critical", "Overboost"),
        ("overheat", "critical", "Coolant"),
        ("lean", "critical", "lean"),
        ("knock", "warning", "timing reductions"),
        ("low_battery", "warning", "voltage"),
        ("misfire", "warning", "idle"),
    ],
)
def test_faults_are_detected(profile, fault, severity, needle):
    samples = run_simulation(profile, "wot_pull", duration_s=90, rate_hz=20, faults=(fault,))
    report = analyze_samples(samples, profile, "simulator")
    assert any(f.severity == severity and needle.lower() in f.title.lower() for f in report.findings), \
        _titles(report)
    assert report.health_score < 100


def test_pull_detection_and_summary(b58):
    samples = run_simulation(b58, "wot_pull", duration_s=40, rate_hz=20)
    pulls = detect_pulls(samples)
    assert len(pulls) == 2
    report = analyze_samples(samples, b58)
    first = report.pulls[0]
    assert first.start_rpm < 2500 and first.end_rpm > 6500
    assert first.peak_boost_bar == pytest.approx(1.2, abs=0.05)
    assert first.peak_power_kw == pytest.approx(285, rel=0.1)
    assert "simulator torque model" in first.power_method


def test_obd_only_log_uses_acceleration_estimate(b58):
    vehicle = SimulatedVehicle(b58, "wot_pull")
    client = simulated_client(vehicle, realtime=False)
    channels = client.available_channels()
    samples = []
    for _ in range(300):
        vehicle.advance(0.05)
        samples.append(Sample(vehicle.sample().timestamp, client.read_channels(channels)))
    report = analyze_samples(samples, b58, "simulator-obd")
    assert report.pulls, "pull should be detected from pedal/throttle"
    pull = report.pulls[0]
    assert "acceleration" in pull.power_method
    # Wheel-power estimate from speed with 1 km/h resolution is rough; keep it in a sane band.
    assert 120 < pull.peak_power_kw < 380
    assert all("Underboost" not in f.title for f in report.findings)


def test_map_saturation_note():
    samples = [Sample(i * 0.1, {"map_kpa": 255.0, "baro_kpa": 100.0, "rpm": 5000.0}) for i in range(20)]
    from opentune.engines import B58

    report = analyze_samples(samples, B58, "elm327")
    assert any("saturated" in f.title for f in report.findings)


def test_empty_log(b58):
    report = analyze_samples([], b58)
    assert report.sample_count == 0 and "No samples" in report.summary


def test_markdown_and_dict(b58):
    samples = run_simulation(b58, "wot_pull", duration_s=30, rate_hz=20, faults=("boost_leak",))
    report = analyze_samples(samples, b58, "simulator")
    md = to_markdown(report)
    assert "## Findings" in md and "## Full-throttle pulls" in md and "Underboost" in md
    json.dumps(to_dict(report))  # JSON-serialisable


# ---------------------------------------------------------------------------- local model
class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_opener(responses: dict[str, dict], calls: list):
    def opener(req, timeout):
        calls.append((req.full_url, req.get_method(), json.loads(req.data) if req.data else None))
        for suffix, payload in responses.items():
            if req.full_url.endswith(suffix):
                return FakeResponse(json.dumps(payload).encode())
        raise urllib.error.URLError("connection refused")

    return opener


@pytest.fixture
def report(b58):
    samples = run_simulation(b58, "wot_pull", duration_s=30, rate_hz=10, faults=("boost_leak",))
    return analyze_samples(samples, b58, "simulator")


def test_prompt_contains_facts_and_no_raw_data(report):
    prompt = build_prompt(report)
    assert "B58" in prompt and "Pull 1" in prompt and "CRITICAL" in prompt
    assert len(prompt) < 4000


def test_ollama_backend(report):
    calls = []
    opener = fake_opener({"/api/tags": {"models": [{"name": "llama3.2:latest"}]},
                          "/api/generate": {"response": "Check the charge pipes."}}, calls)
    client = LocalModelClient(LocalModelConfig("ollama", "http://127.0.0.1:11434", "llama3.2"), opener)
    assert client.list_models() == ["llama3.2:latest"]
    assert narrate(report, client) == "Check the charge pipes."
    assert report.narrative == "Check the charge pipes."
    url, method, body = calls[-1]
    assert url.endswith("/api/generate") and method == "POST"
    assert body["stream"] is False and "emissions" in body["system"]
    assert "AI narrative" in to_markdown(report)


def test_openai_compatible_backend(report):
    calls = []
    opener = fake_opener({"/v1/models": {"data": [{"id": "qwen"}]},
                          "/v1/chat/completions": {"choices": [{"message": {"content": "ok"}}]}}, calls)
    client = LocalModelClient(LocalModelConfig("openai-compatible", "http://localhost:8080", "qwen"), opener)
    assert client.list_models() == ["qwen"]
    assert client.generate("hi") == "ok"
    assert calls[-1][2]["messages"][0]["role"] == "system"


@pytest.mark.parametrize("url", ["https://api.example.com", "http://192.168.1.10:11434", "ftp://localhost"])
def test_remote_or_invalid_urls_rejected(url):
    with pytest.raises(LocalModelError):
        LocalModelClient(LocalModelConfig("ollama", url))


def test_remote_allowed_only_when_explicit():
    LocalModelClient(LocalModelConfig("ollama", "http://10.0.0.5:11434", allow_remote=True))


def test_unreachable_and_bad_responses():
    client = LocalModelClient(LocalModelConfig(), fake_opener({}, []))
    with pytest.raises(LocalModelError, match="Cannot reach"):
        client.generate("x")
    client = LocalModelClient(LocalModelConfig(), fake_opener({"/api/generate": {"response": "  "}}, []))
    with pytest.raises(LocalModelError, match="empty"):
        client.generate("x")
    client = LocalModelClient(LocalModelConfig("openai-compatible", "http://127.0.0.1:1"),
                              fake_opener({"/v1/chat/completions": {"nope": 1}}, []))
    with pytest.raises(LocalModelError, match="Unexpected"):
        client.generate("x")
    with pytest.raises(LocalModelError, match="backend"):
        LocalModelClient(LocalModelConfig("cloud-gpt"))
