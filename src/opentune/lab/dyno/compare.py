"""Comparing dyno runs, CSV export and Plotly figure specifications."""

from __future__ import annotations

import csv
import io
import json

from opentune.lab.dyno.runs import CHANNEL_BY_ID, DynoRun


def _interp(xs: list[float], ys: list[float], x: float) -> float | None:
    if not xs or x < xs[0] or x > xs[-1]:
        return None
    for i in range(1, len(xs)):
        if x <= xs[i]:
            x0, x1 = xs[i - 1], xs[i]
            y0, y1 = ys[i - 1], ys[i]
            return y0 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return ys[-1]


def compare_runs(runs: list[DynoRun], baseline: int = 0, rpm_step: float = 100.0,
                 channels: tuple[str, ...] = ("torque_nm", "power_kw", "boost_kpa", "spark_deg", "lambda", "egt_c",
                                              "iat_c")) -> dict:
    """Interpolate runs onto a common rpm grid (their overlap) and report deltas vs ``baseline``."""
    if len(runs) < 2:
        raise ValueError("Need at least two runs to compare")
    if not 0 <= baseline < len(runs):
        raise ValueError("Baseline index out of range")
    curves = [r.curve for r in runs]
    if any(not c for c in curves):
        raise ValueError("Every run needs an rpm curve (sweep, partial, repeat or steady)")
    lo = max(c[0]["rpm"] for c in curves)
    hi = min(c[-1]["rpm"] for c in curves)
    if hi <= lo:
        raise ValueError("Runs do not share an rpm range")
    grid = []
    x = lo
    while x <= hi + 1e-9:
        grid.append(round(x, 3))
        x += rpm_step
    table: dict[str, list[list[float | None]]] = {}
    for ch in channels:
        table[ch] = []
        for c in curves:
            xs = [p["rpm"] for p in c if p.get(ch) is not None]
            ys = [p[ch] for p in c if p.get(ch) is not None]
            table[ch].append([_interp(xs, ys, g) for g in grid])
    base_t = table["torque_nm"][baseline]
    base_p = table["power_kw"][baseline]
    out_runs = []
    for i, r in enumerate(runs):
        dt = [None if a is None or b is None else round(a - b, 3) for a, b in zip(table["torque_nm"][i], base_t,
                                                                                    strict=False)]
        dp = [None if a is None or b is None else round(a - b, 3) for a, b in zip(table["power_kw"][i], base_p,
                                                                                    strict=False)]
        valid_p = [v for v in table["power_kw"][i] if v is not None]
        valid_bp = [v for v in base_p if v is not None]
        avg_gain = (sum(valid_p) / len(valid_p) - sum(valid_bp) / len(valid_bp)) if valid_p and valid_bp else 0.0
        out_runs.append({
            "id": r.id, "label": r.label, "engine_id": r.engine_id, "calibration_name": r.calibration_name,
            "calibration_hash": r.calibration_hash,
            "peak_torque_nm": r.summary.get("peak_torque_nm"), "peak_power_kw": r.summary.get("peak_power_kw"),
            "peak_torque_delta_nm": round(r.summary.get("peak_torque_nm", 0) - runs[baseline].summary.get(
                "peak_torque_nm", 0), 3),
            "peak_power_delta_kw": round(r.summary.get("peak_power_kw", 0) - runs[baseline].summary.get(
                "peak_power_kw", 0), 3),
            "mean_power_delta_kw": round(avg_gain, 3),
            "torque_delta_nm": dt, "power_delta_kw": dp,
            "max_egt_c": r.summary.get("max_egt_c"), "max_knock_retard_deg": r.summary.get("max_knock_retard_deg"),
            "dtcs": r.events.get("dtcs", []),
        })
    return {"rpm": grid, "baseline": baseline, "channels": {k: v for k, v in table.items()}, "runs": out_runs,
            "note": "All values simulated. Differences between runs are differences of the model, not "
                    "predictions for a real engine."}


def _header(run: DynoRun) -> str:
    """Comment lines (plain text, not CSV-quoted) describing where the data came from."""
    lines = ["# OpenTune Virtual Engine Lab — SIMULATED dyno data (not a measurement)",
             f"# run: {run.label} | id: {run.id} | kind: {run.kind}",
             f"# engine: {run.engine_name} | calibration: {run.calibration_name} | sha256: {run.calibration_hash}",
             f"# correction: {run.correction.get('standard')} | factor: {run.correction.get('factor')} | "
             f"applicable: {run.correction.get('applicable')}"]
    if run.test.get("drivetrain"):
        d = run.test["drivetrain"]
        lines.append(f"# drivetrain loss (stated; {d.get('provenance')}): {d.get('name')}")
    return "".join(ln.replace("\n", " ") + "\n" for ln in lines)


def curve_to_csv(run: DynoRun) -> str:
    buf = io.StringIO()
    buf.write(_header(run))
    w = csv.writer(buf, lineterminator="\n")
    if not run.curve:
        return buf.getvalue()
    keys = list(run.curve[0].keys())
    w.writerow([f"{k} ({CHANNEL_BY_ID[k].unit})" if k in CHANNEL_BY_ID else k for k in keys])
    for p in run.curve:
        w.writerow([p.get(k) for k in keys])
    return buf.getvalue()


def samples_to_csv(run: DynoRun) -> str:
    buf = io.StringIO()
    buf.write(_header(run))
    w = csv.writer(buf, lineterminator="\n")
    if not run.samples:
        return buf.getvalue()
    keys = list(run.samples[0].keys())
    w.writerow([f"{k} ({CHANNEL_BY_ID[k].unit})" if k in CHANNEL_BY_ID else k for k in keys])
    for s in run.samples:
        w.writerow([s.get(k) for k in keys])
    return buf.getvalue()


PALETTE = ("#4cc9f0", "#f72585", "#b5e48c", "#ffb703", "#9d4edd", "#90e0ef", "#fb8500", "#e5e5e5")


def figure_spec(runs: list[DynoRun], left: str = "torque_nm", right: str | None = "power_kw",
                band: dict | None = None, title: str = "Virtual dyno (simulated)") -> dict:
    """Plotly figure JSON (rendered by the browser UI or ``plotly.io``)."""
    data = []
    for i, r in enumerate(runs):
        color = PALETTE[i % len(PALETTE)]
        xs = [p["rpm"] for p in r.curve]
        data.append({"type": "scatter", "mode": "lines", "name": f"{r.label} — {CHANNEL_BY_ID[left].label}",
                     "x": xs, "y": [p.get(left) for p in r.curve], "line": {"color": color, "width": 2}})
        if right:
            data.append({"type": "scatter", "mode": "lines", "name": f"{r.label} — {CHANNEL_BY_ID[right].label}",
                         "x": xs, "y": [p.get(right) for p in r.curve], "yaxis": "y2",
                         "line": {"color": color, "width": 2, "dash": "dash"}})
    if band and left == "torque_nm":
        data.append({"type": "scatter", "mode": "lines", "x": band["rpm"], "y": band["torque_p95"],
                     "line": {"width": 0}, "showlegend": False, "hoverinfo": "skip"})
        data.append({"type": "scatter", "mode": "lines", "x": band["rpm"], "y": band["torque_p05"],
                     "fill": "tonexty", "fillcolor": "rgba(76,201,240,0.18)", "line": {"width": 0},
                     "name": "model uncertainty (5–95 %)"})
    layout = {
        "title": {"text": title}, "paper_bgcolor": "#0f1117", "plot_bgcolor": "#0f1117",
        "font": {"color": "#d8dee9"}, "xaxis": {"title": {"text": "Engine speed (rpm)"}, "gridcolor": "#2a2f3a"},
        "yaxis": {"title": {"text": f"{CHANNEL_BY_ID[left].label} ({CHANNEL_BY_ID[left].unit})"},
                  "gridcolor": "#2a2f3a"},
        "legend": {"orientation": "h"}, "margin": {"l": 60, "r": 60, "t": 50, "b": 50},
        "annotations": [{"text": "SIMULATED — synthetic model, not a measurement", "xref": "paper", "yref": "paper",
                         "x": 0.0, "y": 1.06, "showarrow": False, "font": {"size": 11, "color": "#ffb703"}}],
    }
    if right:
        layout["yaxis2"] = {"title": {"text": f"{CHANNEL_BY_ID[right].label} ({CHANNEL_BY_ID[right].unit})"},
                            "overlaying": "y", "side": "right", "gridcolor": "#2a2f3a"}
    return {"data": data, "layout": layout}


def save_html(runs: list[DynoRun], path: str, **kwargs) -> str:
    """Stand-alone HTML chart (requires the ``plotly`` package; works offline)."""
    import plotly.io as pio

    spec = figure_spec(runs, **kwargs)
    pio.write_html(json.loads(json.dumps(spec)), path, include_plotlyjs=True, full_html=True)
    return path
