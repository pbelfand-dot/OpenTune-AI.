import { useEffect, useMemo, useState } from "react";
import { api, fmt } from "../api";
import { Num, Plot, useAction } from "./ui";

type Sel = { r0: number; c0: number; r1: number; c1: number } | null;

function heat(t: number) {
  // perceptual-ish dark→cyan→yellow→magenta ramp
  const stops = [[30, 60, 110], [40, 170, 200], [190, 220, 90], [250, 160, 40], [240, 60, 120]];
  const x = Math.min(0.9999, Math.max(0, t)) * (stops.length - 1);
  const i = Math.floor(x), f = x - i;
  const c = stops[i].map((v, k) => Math.round(v + (stops[i + 1][k] - v) * f));
  return `rgb(${c[0]},${c[1]},${c[2]})`;
}

export default function TableEditor({ projectId, tableId, onChanged }: {
  projectId: string; tableId: string; onChanged: () => void;
}) {
  const [t, setT] = useState<any>(null);
  const [sel, setSel] = useState<Sel>(null);
  const [dragging, setDragging] = useState(false);
  const [editing, setEditing] = useState<[number, number] | null>(null);
  const [editText, setEditText] = useState("");
  const [op, setOp] = useState("add");
  const [amount, setAmount] = useState(1);
  const [showDelta, setShowDelta] = useState(false);
  const [slice, setSlice] = useState(0);
  const [view3d, setView3d] = useState(true);
  const { run, busy } = useAction();
  const base = `/api/projects/${projectId}/calibration`;

  const load = async () => setT(await api.get(`${base}/tables/${tableId}`));
  useEffect(() => { setSel(null); setSlice(0); load(); /* eslint-disable-next-line */ }, [tableId, projectId]);

  // normalise to 2-D rows × cols for display (1-D → one row; 3-D → slice of the first axis)
  const grid = useMemo(() => {
    if (!t) return null;
    const nd = t.axes.length;
    const pick = (v: any) => (nd === 1 ? [v] : nd === 2 ? v : v[slice]);
    const rowsAxis = nd === 1 ? null : t.axes[nd - 2];
    const colsAxis = t.axes[nd - 1];
    return {
      nd, rowsAxis, colsAxis, values: pick(t.values) as number[][],
      delta: t.delta ? (pick(t.delta) as number[][]) : null,
      baseline: t.baseline_values ? (pick(t.baseline_values) as number[][]) : null,
    };
  }, [t, slice]);

  if (!t || !grid) return <div className="muted">Loading table…</div>;
  const flat = grid.values.flat();
  const lo = Math.min(...flat), hi = Math.max(...flat);
  const index = (r: number, c: number) => (grid.nd === 1 ? [c] : grid.nd === 2 ? [r, c] : [slice, r, c]);
  const inSel = (r: number, c: number) => sel && r >= Math.min(sel.r0, sel.r1) && r <= Math.max(sel.r0, sel.r1)
    && c >= Math.min(sel.c0, sel.c1) && c <= Math.max(sel.c0, sel.c1);
  const region = () => {
    if (!sel) return null;
    const rr = [Math.min(sel.r0, sel.r1), Math.max(sel.r0, sel.r1) + 1];
    const cc = [Math.min(sel.c0, sel.c1), Math.max(sel.c0, sel.c1) + 1];
    if (grid.nd === 1) return [cc];
    if (grid.nd === 2) return [rr, cc];
    return [[slice, slice + 1], rr, cc];
  };
  const edit = async (body: any, ok?: string) => {
    const r = await run(() => api.post(`${base}/tables/${tableId}/edit`, body), ok);
    if (r) { setT(r); onChanged(); }
  };
  const commitCell = async () => {
    if (!editing) return;
    const v = parseFloat(editText);
    setEditing(null);
    if (!Number.isNaN(v)) await edit({ op: "set_cell", index: index(editing[0], editing[1]), value: v });
  };
  const axisEdit = async (axisIdx: number, point: number, old: number) => {
    const s = prompt(`New breakpoint value (old ${old})`, String(old));
    if (s === null) return;
    const v = parseFloat(s);
    if (!Number.isNaN(v)) await edit({ op: "axis", axis: axisIdx, point, value: v, resample: true }, "Axis changed; data re-interpolated");
  };
  const undo = async () => { if (await run(() => api.post(`${base}/undo`))) { await load(); onChanged(); } };
  const redo = async () => { if (await run(() => api.post(`${base}/redo`))) { await load(); onChanged(); } };
  const nd = grid.nd;
  const rowAxisIdx = nd - 2, colAxisIdx = nd - 1;

  const plot = nd === 1 ? (
    <Plot height={300} data={[
      { x: grid.colsAxis.values, y: grid.values[0], name: "current", type: "scatter", mode: "lines+markers", line: { color: "#4cc9f0" } },
      ...(grid.baseline ? [{ x: grid.colsAxis.values, y: grid.baseline[0], name: "baseline", type: "scatter", mode: "lines", line: { color: "#8b95a7", dash: "dot" } }] : []),
    ]} layout={{ xaxis: { title: { text: `${grid.colsAxis.label} (${grid.colsAxis.unit})` } }, yaxis: { title: { text: t.unit } } }} />
  ) : view3d ? (
    <Plot height={420} data={[
      { type: "surface", x: grid.colsAxis.values, y: grid.rowsAxis.values, z: grid.values, colorscale: "Viridis", name: "current", showscale: false },
      ...(grid.baseline && grid.delta?.flat().some((d) => Math.abs(d) > 1e-9) ? [{ type: "surface", x: grid.colsAxis.values, y: grid.rowsAxis.values, z: grid.baseline, opacity: 0.35, showscale: false, colorscale: [[0, "#aaa"], [1, "#fff"]], name: "baseline" }] : []),
    ]} layout={{ margin: { l: 0, r: 0, t: 10, b: 0 }, scene: {
      xaxis: { title: { text: grid.colsAxis.label }, gridcolor: "#333", color: "#aaa" },
      yaxis: { title: { text: grid.rowsAxis.label }, gridcolor: "#333", color: "#aaa" },
      zaxis: { title: { text: t.unit }, gridcolor: "#333", color: "#aaa" } } }} />
  ) : (
    <Plot height={360} data={[{ type: "heatmap", x: grid.colsAxis.values, y: grid.rowsAxis.values, z: showDelta && grid.delta ? grid.delta : grid.values, colorscale: "Viridis" }]}
      layout={{ xaxis: { title: { text: grid.colsAxis.label } }, yaxis: { title: { text: grid.rowsAxis.label } } }} />
  );

  return (
    <div className="col">
      <div className="row">
        <b>{t.name}</b><span className="chip">{t.unit}</span><span className="chip">{t.axes.map((a: any) => a.values.length).join(" × ")}</span>
        <span className="chip warn" title="Synthetic values generated from the engine model">{t.provenance}</span>
        <small className="muted">{t.description}</small>
      </div>
      <div className="row">
        <button onClick={undo} disabled={busy || !t.can_undo}>↶ Undo</button>
        <button onClick={redo} disabled={busy || !t.can_redo}>↷ Redo</button>
        <span className="muted">|</span>
        <select value={op} onChange={(e) => setOp(e.target.value)}>
          <option value="set">set =</option><option value="add">add +</option><option value="multiply">multiply ×</option>
          <option value="percent">percent %</option><option value="smooth">smooth</option><option value="interpolate">interpolate corners</option>
        </select>
        {!["smooth", "interpolate"].includes(op) && <Num value={amount} onChange={setAmount} step={0.5} />}
        <button className="primary" disabled={busy} onClick={() => edit({ op: "region", operation: op, amount, region: region() })}>
          Apply to {sel ? "selection" : "whole table"}</button>
        {sel && <button className="ghost small" onClick={() => setSel(null)}>clear selection</button>}
        <span className="spacer" />
        <label className="f inline"><input type="checkbox" checked={showDelta} onChange={(e) => setShowDelta(e.target.checked)} />Δ vs baseline</label>
        {nd >= 2 && <label className="f inline"><input type="checkbox" checked={view3d} onChange={(e) => setView3d(e.target.checked)} />3D</label>}
        {nd === 3 && <label className="f inline">{t.axes[0].label}
          <select value={slice} onChange={(e) => setSlice(Number(e.target.value))}>{t.axes[0].values.map((v: number, i: number) => <option key={i} value={i}>{v}</option>)}</select></label>}
        <a className="btn small" href={`${base}/tables/${tableId}/csv`}>CSV</a>
      </div>
      <small className="muted">Drag to select a region · double-click a cell to type a value · click an axis value to move the breakpoint (data is re-interpolated) · values outside {t.minimum}…{t.maximum} {t.unit} are rejected.</small>
      <div className="cal-grid" onMouseLeave={() => setDragging(false)} onMouseUp={() => setDragging(false)}>
        <table>
          <thead><tr>
            <th title={nd > 1 ? `${grid.rowsAxis.label} ↓ / ${grid.colsAxis.label} →` : grid.colsAxis.label}>
              {nd > 1 ? `${grid.rowsAxis.name}\\${grid.colsAxis.name}` : grid.colsAxis.name}</th>
            {grid.colsAxis.values.map((v: number, c: number) => <th key={c} onClick={() => axisEdit(colAxisIdx, c, v)} title={`${grid.colsAxis.label} (${grid.colsAxis.unit}) — click to edit`}>{fmt(v, grid.colsAxis.decimals ?? 0)}</th>)}
          </tr></thead>
          <tbody>
            {grid.values.map((row: number[], r: number) => (
              <tr key={r}>
                <th onClick={() => nd > 1 && axisEdit(rowAxisIdx, r, grid.rowsAxis.values[r])} title={nd > 1 ? `${grid.rowsAxis.label} (${grid.rowsAxis.unit}) — click to edit` : ""}>
                  {nd > 1 ? fmt(grid.rowsAxis.values[r], grid.rowsAxis.decimals ?? 0) : t.unit}</th>
                {row.map((v: number, c: number) => {
                  const d = grid.delta?.[r]?.[c] ?? 0;
                  const shown = showDelta && grid.delta ? d : v;
                  const tt = showDelta && grid.delta ? (d === 0 ? 0.5 : d > 0 ? 0.85 : 0.15) : (hi > lo ? (v - lo) / (hi - lo) : 0.5);
                  const isEdit = editing && editing[0] === r && editing[1] === c;
                  return (
                    <td key={c} className={`${inSel(r, c) ? "sel" : ""} ${Math.abs(d) > 1e-9 ? "changed" : ""}`}
                      style={{ background: heat(tt) }}
                      title={`${fmt(v, t.decimals)} ${t.unit}${grid.baseline ? ` (baseline ${fmt(grid.baseline[r][c], t.decimals)}, Δ ${fmt(d, t.decimals)})` : ""}`}
                      onMouseDown={(e) => {
                        if (e.shiftKey && sel) setSel({ ...sel, r1: r, c1: c });
                        else { setSel({ r0: r, c0: c, r1: r, c1: c }); setDragging(true); }
                      }}
                      onMouseEnter={() => dragging && sel && setSel({ ...sel, r1: r, c1: c })}
                      onDoubleClick={() => { setEditing([r, c]); setEditText(String(v)); }}>
                      {isEdit ? <input autoFocus value={editText} onChange={(e) => setEditText(e.target.value)}
                        onBlur={commitCell} onKeyDown={(e) => { if (e.key === "Enter") commitCell(); if (e.key === "Escape") setEditing(null); }} />
                        : fmt(shown, t.decimals)}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {plot}
    </div>
  );
}
