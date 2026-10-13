"""Illustrative engine sound synthesis (not a recording and not an acoustic model).

Additive synthesis driven by a speed/load/turbo-speed trace:

* Firing frequency of a four-stroke engine: f = rpm / 60 × cylinders / 2. The
  sound is built from that frequency's harmonics plus small half-order
  components (cycle-to-cycle variation), with amplitude rising with load.
* Load-dependent filtered noise stands in for combustion and intake roughness.
* A turbo "whistle" follows compressor blade-pass frequency
  (turbo rpm / 60 × blade count), scaled into a comfortable range. The blade
  count and the scaling are artistic assumptions.

The presets are artistic choices. Nothing here claims to reproduce how any
real engine sounds.
"""

from __future__ import annotations

import io
import math
import wave
from dataclasses import asdict, dataclass

import numpy as np


@dataclass(frozen=True)
class SoundPreset:
    id: str
    name: str
    description: str
    harmonics: tuple[float, ...]  # amplitudes of 1×, 2×, 3× … firing frequency
    half_order: float  # amplitude of half-order (cycle-to-cycle) components
    noise: float  # combustion/intake noise level
    lowpass_hz: float  # one-pole low-pass (exhaust muffling)
    turbo: float  # turbo whistle level
    turbo_blades: int = 6  # assumption
    turbo_pitch: float = 0.18  # scale blade-pass frequency into a comfortable range (artistic)


PRESETS: dict[str, SoundPreset] = {p.id: p for p in (
    SoundPreset("exhaust", "Exhaust (outside)", "Strong firing-order fundamental, muffled top end.",
                (1.0, 0.55, 0.3, 0.18, 0.1), 0.12, 0.10, 1800.0, 0.05),
    SoundPreset("interior", "Interior", "Softer, more filtered; low frequencies dominate.",
                (1.0, 0.35, 0.12, 0.05), 0.05, 0.05, 900.0, 0.03),
    SoundPreset("intake", "Intake / engine bay", "Brighter harmonics, more induction noise and turbo whistle.",
                (0.7, 0.6, 0.45, 0.35, 0.25, 0.15), 0.08, 0.22, 5000.0, 0.14),
    SoundPreset("turbo", "Turbo focus", "Exaggerated turbo whistle for demonstrations.",
                (0.6, 0.3, 0.15), 0.05, 0.08, 2500.0, 0.45),
)}


def firing_frequency(rpm: float, cylinders: int) -> float:
    return rpm / 60.0 * cylinders / 2.0


def synthesize(t: list[float], rpm: list[float], load: list[float], turbo_rpm: list[float] | None, cylinders: int,
               preset: str = "exhaust", sample_rate: int = 22050, seed: int = 1) -> np.ndarray:
    """Mono float32 audio in [-1, 1] following the given trace (linearly interpolated)."""
    if preset not in PRESETS:
        raise KeyError(f"Unknown sound preset {preset!r}")
    if len(t) < 2 or t[-1] <= t[0]:
        raise ValueError("Trace needs at least two increasing time points")
    if t[-1] - t[0] > 120.0:
        raise ValueError("At most 120 s of audio")
    p = PRESETS[preset]
    n = int((t[-1] - t[0]) * sample_rate)
    ts = t[0] + np.arange(n) / sample_rate
    r = np.interp(ts, t, rpm)
    ld = np.clip(np.interp(ts, t, load), 0.0, 1.0)
    f0 = np.maximum(r, 0.0) / 60.0 * cylinders / 2.0
    phase = 2.0 * np.pi * np.cumsum(f0) / sample_rate
    amp = 0.25 + 0.75 * ld
    out = np.zeros(n)
    nyq = sample_rate / 2.0
    for k, a in enumerate(p.harmonics, start=1):
        mask = (k * f0) < nyq
        out += np.where(mask, a * np.sin(k * phase), 0.0)
    if p.half_order:
        out += p.half_order * np.sin(0.5 * phase) * (0.5 + 0.5 * ld)
        out += 0.5 * p.half_order * np.sin(1.5 * phase + 0.7)
    out *= amp
    rng = np.random.default_rng(seed)
    noise = rng.standard_normal(n) * p.noise * (0.2 + ld)
    out += noise
    if turbo_rpm is not None and p.turbo:
        tr = np.maximum(np.interp(ts, t, turbo_rpm), 0.0)
        ft = tr / 60.0 * p.turbo_blades * p.turbo_pitch
        tphase = 2.0 * np.pi * np.cumsum(ft) / sample_rate
        level = p.turbo * np.clip((tr - 40000.0) / 120000.0, 0.0, 1.0) * (0.3 + 0.7 * ld)
        out += np.where(ft < nyq, level * np.sin(tphase), 0.0)
    # one-pole low-pass: y[i] = y[i-1] + α (x[i] − y[i-1])
    alpha = 1.0 - math.exp(-2.0 * math.pi * p.lowpass_hz / sample_rate)
    from scipy.signal import lfilter

    out = lfilter([alpha], [1.0, alpha - 1.0], out)
    peak = float(np.max(np.abs(out))) or 1.0
    fade = min(n, int(0.02 * sample_rate))
    env = np.ones(n)
    env[:fade] = np.linspace(0.0, 1.0, fade)
    env[-fade:] = np.linspace(1.0, 0.0, fade)
    return (out / peak * 0.85 * env).astype(np.float32)


def to_wav(samples: np.ndarray, sample_rate: int = 22050) -> bytes:
    pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def rev_trace(idle: float = 750.0, redline: float = 6500.0, seconds: float = 6.0) -> tuple[list, list, list, list]:
    """Synthetic free-rev: idle → redline under load → lift-off back to idle."""
    t = np.linspace(0.0, seconds, 200)
    up = seconds * 0.55
    rise = np.clip((t - 0.5) / (up - 0.5), 0.0, 1.0) ** 0.8
    fall = np.clip((t - up) / (seconds - up), 0.0, 1.0)
    rpm = np.where(t < 0.5, idle, np.where(t < up, idle + (redline - idle) * rise, redline - (redline - idle) * fall))
    load = np.where((t > 0.5) & (t < up), 1.0, 0.1)
    turbo = np.where(load > 0.5, 40000 + 140000 * np.clip((rpm - 1500) / 3000, 0, 1), 30000)
    return t.tolist(), rpm.tolist(), load.tolist(), turbo.tolist()


def presets_list() -> list[dict]:
    return [asdict(p) for p in PRESETS.values()]
