import { Fragment, useEffect, useMemo, useState } from "react";
import { api, fmt } from "../api";
import { Card, Disclaimer, Field, JobBar, Num, Plot, useAction } from "../components/ui";
import { useLab } from "../context";
import { useApi, useJob } from "../hooks";

const COLORS = ["#4cc9f0", "#f72585", "#b5e48c", "#ffb703", "#9d4edd", "#90e0ef", "#fb8500", "#e5e5e5"];

export default function Dyno() {
  const { project, params } = useLab();
  const pid = project!.id;
  const meta = useApi<any>("/api/dyno/meta");
  const runs = useApi<any[]>(`/api/dyno/runs?project_id=${pid}`, [pid]);
  const [test, setTest] = useState<any>(null);
  const [calSource, setCalSource] = useState("working");
  const [unc, setUnc] = useState(0);
  const [selected, setSelected] = useState<string[]>(params.run ? [params.run] : []);
  const [details, setDetails] = useState<Record<string, any>>({});
  const [left, setLeft] = useState("torque_nm");
  const [right, setRight] = useState("power_kw");
  const [band, setBand] = useState(true);
  const [compare, setCompare] = useState<any>(null);
  const [preset, setPreset] = useState("exhaust");
  const { run, busy } = useAction();
  const job = useJob(async (j) => {
    await runs.reload();
    if (j.result?.run_ids) setSelected((s) => Array.from(new Set([...j.result.run_ids, ...s])).slice(0, 6));
  });
  useEffect(() => { if (meta.data && !test) setTest({ ...meta.data.defaults, drivetrain: null }); }, [meta.data, test]);
  useEffect(() => {
    selected.filter((id) => !details[id]).forEach(async (id) => {
      try { const d = await api.get(`/api/dyno/runs/${id}?samples=true`); setDetails((x) => ({ ...x, [id]: d })); } catch { /* deleted */ }
    });
  }, [selected, details]);
  useEffect(() => {
    if (selected.length >= 2) api.post("/api/dyno/compare", { run_ids: selected.slice(0, 8) }).then(setCompare).catch(() => setCompare(null));
    else setCompare(null);
  }, [selected]);

  const t = (k: string, v: any) => setTest((x: any) => ({ ...x, [k]: v }));
  const start = () => run(async () => {
    const body = { ...test };
    if (body.rpm_end === null || body.rpm_end === undefined || body.rpm_end === "") delete body.rpm_end;
    job.start(await api.post("/api/dyno/run", { project_id: pid, test: body, calibration: { source: calSource }, uncertainty_samples: unc }));
  });
  const chans = meta.data?.channels ?? [];
  const label = (id: string) => chans.find((c: any) => c.id === id);
  const shown = selected.map((id) => details[id]).filter(Boolean);
  const plotData = useMemo(() => {
    const out: any[] = [];
    shown.forEach((r: any, i: number) => {
      const c = COLORS[i % COLORS.length];
      if (r.kind === "transient") {
        const s = r.samples || [];
        out.push({ x: s.map((p: any) => p.t), y: s.map((p: any) => p[left]), name: `${r.label} ${left}`, type: "scatter", mode: "lines", line: { color: c } });
        if (right) out.push({ x: s.map((p: any) => p.t), y: s.map((p: any) => p[right]), name: `${r.label} ${right}`, yaxis: "y2", type: "scatter", mode: "lines", line: { color: c, dash: "dash" } });
        return;
      }
      const cv = r.curve || [];
      out.push({ x: cv.map((p: any) => p.rpm), y: cv.map((p: any) => p[left]), name: `${r.label}`, type: "scatter", mode: r.kind === "steady" ? "markers" : "lines", line: { color: c, width: 2 } });
      if (right) out.push({ x: cv.map((p: any) => p.rpm), y: cv.map((p: any) => p[right]), name: `${r.label} (${label(right)?.label ?? right})`, yaxis: "y2", type: "scatter", mode: r.kind === "steady" ? "markers" : "lines", line: { color: c, dash: "dash" }, showlegend: false });
      if (band && r.uncertainty && left === "torque_nm") {
        const u = r.uncertainty;
        out.push({ x: u.rpm, y: u.torque_p95, type: "scatter", mode: "lines", line: { width: 0 }, showlegend: false, hoverinfo: "skip" });
        out.push({ x: u.rpm, y: u.torque_p05, type: "scatter", mode: "lines", fill: "tonexty", fillcolor: "rgba(76,201,240,0.15)", line: { width: 0 }, name: "model uncertainty 5–95 %" });
      }
    });
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shown.length, selected, left, right, band, details]);
  const isTime = shown.some((r: any) => r.kind === "transient");
  const toggle = (id: string) => setSelected((s) => (s.includes(id) ? s.filter((x) => x !== id) : [id, ...s].slice(0, 8)));

  if (!test) return <div className="muted">Loading…</div>;
  return (
    <div className="grid">
      <Disclaimer>Virtual engine dyno: speed-controlled absorber driving the closed-loop simulation. Brake torque is inertia-compensated crank torque. Wheel figures appear only with a stated drivetrain loss. Atmospheric corrections are uncorrected by default and not applicable to boost-controlled engines.</Disclaimer>
      <div className="grid g3">
        <Card title="Test" className="span2" actions={<button className="primary" onClick={start} disabled={busy || job.running}>Run test</button>}>
          <div className="form">
            <Field label="Kind"><select value={test.kind} onChange={(e) => t("kind", e.target.value)}>
              {meta.data.kinds.map((k: string) => <option key={k}>{k}</option>)}</select></Field>
            <Field label="Calibration"><select value={calSource} onChange={(e) => setCalSource(e.target.value)}>
              <option value="working">working copy (unsaved edits included)</option><option value="active">active saved version</option>
              <option value="baseline">baseline</option><option value="flashed">version in the virtual ECU</option></select></Field>
            {["sweep", "partial", "repeat"].includes(test.kind) && <>
              <Field label="Start rpm"><Num value={test.rpm_start} step={100} onChange={(v) => t("rpm_start", v)} /></Field>
              <Field label="End rpm (blank = max − 300)"><Num value={test.rpm_end} step={100} onChange={(v) => t("rpm_end", v)} /></Field>
              <Field label="Ramp rpm/s"><Num value={test.ramp_rpm_s} step={50} onChange={(v) => t("ramp_rpm_s", v)} /></Field>
            </>}
            {test.kind === "partial" && <Field label="Pedal %"><Num value={test.pedal} min={0} max={100} onChange={(v) => t("pedal", v)} /></Field>}
            {test.kind === "repeat" && <>
              <Field label="Repeats"><Num value={test.repeats} min={2} max={10} onChange={(v) => t("repeats", v)} /></Field>
              <Field label="Cool-down s"><Num value={test.cooldown_s} onChange={(v) => t("cooldown_s", v)} /></Field></>}
            {test.kind === "steady" && <Field label="Points rpm:pedal, …"><input value={test.steady_points.map((p: number[]) => p.join(":")).join(", ")}
              onChange={(e) => t("steady_points", e.target.value.split(",").map((s) => s.trim().split(":").map(Number)).filter((p) => p.length === 2 && p.every((x) => !Number.isNaN(x))))} placeholder="2000:30, 3000:60, 4500:100" /></Field>}
            {test.kind === "transient" && <>
              <Field label="Speed rpm"><Num value={test.transient_rpm} step={100} onChange={(v) => t("transient_rpm", v)} /></Field>
              <Field label="Pedal from → to"><div className="row"><Num value={test.transient_from_pedal} width="4rem" onChange={(v) => t("transient_from_pedal", v)} />→<Num value={test.transient_to_pedal} width="4rem" onChange={(v) => t("transient_to_pedal", v)} /></div></Field>
              <Field label="Duration s"><Num value={test.transient_duration_s} onChange={(v) => t("transient_duration_s", v)} /></Field></>}
            <Field label="Ambient °C"><Num value={test.ambient_temp_c} onChange={(v) => t("ambient_temp_c", v)} /></Field>
            <Field label="Ambient kPa"><Num value={test.ambient_pressure_kpa} onChange={(v) => t("ambient_pressure_kpa", v)} /></Field>
            <Field label="Cooling airflow (0–1)"><Num value={test.fan_airflow} step={0.1} onChange={(v) => t("fan_airflow", v)} /></Field>
            <Field label="Correction"><select value={test.correction} onChange={(e) => t("correction", e.target.value)}>
              {meta.data.corrections.map((c: any) => <option key={c.id} value={c.id} title={c.formula}>{c.name}</option>)}</select></Field>
            <Field label="Drivetrain loss (wheel figures)"><select value={test.drivetrain?.id ?? ""} onChange={(e) => t("drivetrain", meta.data.drivetrains.find((d: any) => d.id === e.target.value) ?? null)}>
              <option value="">none — crank only</option>{meta.data.drivetrains.map((d: any) => <option key={d.id} value={d.id}>{d.name}</option>)}</select></Field>
            <Field label="Uncertainty samples (0 = off)"><Num value={unc} min={0} max={500} step={8} onChange={setUnc} /></Field>
            <Field label="Seed"><Num value={test.seed} onChange={(v) => t("seed", v)} /></Field>
          </div>
          <div style={{ marginTop: ".6rem" }}><JobBar job={job.job} onCancel={job.cancel} /></div>
          {test.correction !== "none" && <small className="muted">{meta.data.corrections.find((c: any) => c.id === test.correction)?.formula} — {meta.data.corrections.find((c: any) => c.id === test.correction)?.source_note}</small>}
        </Card>
        <Card title="Runs" actions={<button className="small" onClick={() => runs.reload()}>↻</button>}>
          <div className="scroll" style={{ maxHeight: 330 }}>
            <table className="t"><tbody>{(runs.data ?? []).map((r) => (
              <tr key={r.id}>
                <td><input type="checkbox" checked={selected.includes(r.id)} onChange={() => toggle(r.id)} /></td>
                <td>{r.label}<br /><small className="muted">{r.kind} · {r.created_utc.replace("T", " ").slice(0, 16)}</small></td>
                <td className="num">{fmt(r.summary.peak_power_kw, 0)} kW<br />{fmt(r.summary.peak_torque_nm, 0)} N·m</td>
                <td><button className="small ghost" title="Delete" onClick={() => run(async () => { await api.del(`/api/dyno/runs/${r.id}`); setSelected((s) => s.filter((x) => x !== r.id)); runs.reload(); })}>✕</button></td>
              </tr>))}</tbody></table>
            {!runs.data?.length && <small className="muted">No runs yet.</small>}
          </div>
        </Card>
      </div>
      <Card title={isTime ? "Time series (transient)" : "Curves"} actions={<>
        <Field label="Left" inline><select value={left} onChange={(e) => setLeft(e.target.value)}>{chans.map((c: any) => <option key={c.id} value={c.id}>{c.label} ({c.unit})</option>)}</select></Field>
        <Field label="Right" inline><select value={right} onChange={(e) => setRight(e.target.value)}><option value="">—</option>{chans.map((c: any) => <option key={c.id} value={c.id}>{c.label} ({c.unit})</option>)}</select></Field>
        <label className="f inline"><input type="checkbox" checked={band} onChange={(e) => setBand(e.target.checked)} />uncertainty band</label>
      </>}>
        {shown.length ? <Plot height={420} data={plotData} layout={{
          xaxis: { title: { text: isTime ? "time (s)" : "engine speed (rpm)" } },
          yaxis: { title: { text: `${label(left)?.label} (${label(left)?.unit})` } },
          yaxis2: right ? { title: { text: `${label(right)?.label} (${label(right)?.unit})` }, overlaying: "y", side: "right" } : undefined,
          annotations: [{ text: "SIMULATED — synthetic model", xref: "paper", yref: "paper", x: 0, y: 1.08, showarrow: false, font: { color: "#ffb703", size: 11 } }],
        }} /> : <small className="muted">Run a test or tick runs on the right to plot them. Up to 8 runs can be overlaid.</small>}
      </Card>
      {shown.map((r: any) => <RunSummary key={r.id} r={r} preset={preset} setPreset={setPreset} onUnc={() => run(async () => {
        job.start(await api.post(`/api/dyno/runs/${r.id}/uncertainty`, { samples: 48 }));
        setDetails((d) => { const x = { ...d }; delete x[r.id]; return x; });
      })} />)}
      {compare && <Card title="Comparison (vs first selected run)">
        <table className="t"><thead><tr><th>Run</th><th className="num">Peak torque</th><th className="num">Δ</th><th className="num">Peak power</th><th className="num">Δ</th><th className="num">Mean Δ power</th><th className="num">Max EGT</th><th className="num">Max knock retard</th><th>DTCs</th></tr></thead>
          <tbody>{compare.runs.map((r: any) => <tr key={r.id}><td>{r.label}</td><td className="num">{fmt(r.peak_torque_nm, 1)}</td><td className="num">{fmt(r.peak_torque_delta_nm, 1)}</td>
            <td className="num">{fmt(r.peak_power_kw, 1)}</td><td className="num">{fmt(r.peak_power_delta_kw, 1)}</td><td className="num">{fmt(r.mean_power_delta_kw, 1)}</td>
            <td className="num">{fmt(r.max_egt_c, 0)}</td><td className="num">{fmt(r.max_knock_retard_deg, 1)}</td><td>{r.dtcs.join(", ")}</td></tr>)}</tbody></table>
        <Plot height={260} data={compare.runs.slice(1).map((r: any, i: number) => ({ x: compare.rpm, y: r.torque_delta_nm, name: `${r.label} Δ torque`, type: "scatter", mode: "lines", line: { color: COLORS[(i + 1) % COLORS.length] } }))}
          layout={{ xaxis: { title: { text: "rpm" } }, yaxis: { title: { text: "Δ torque (N·m)" } } }} />
        <small className="muted">{compare.note}</small>
      </Card>}
    </div>
  );
}

function RunSummary({ r, preset, setPreset, onUnc }: { r: any; preset: string; setPreset: (p: string) => void; onUnc: () => void }) {
  const s = r.summary || {};
  return (
    <Card title={`${r.label}`} actions={<>
      <span className="chip">{r.kind}</span><span className="chip mono">cal {r.calibration_hash.slice(0, 8)}</span>
      <a className="btn small" href={`/api/dyno/runs/${r.id}/curve.csv`}>curve CSV</a>
      <a className="btn small" href={`/api/dyno/runs/${r.id}/samples.csv`}>samples CSV</a>
    </>}>
      <div className="grid g3">
        <dl className="kv">
          {s.peak_torque_nm !== undefined && <><dt>Peak torque</dt><dd>{fmt(s.peak_torque_nm, 1)} N·m ({fmt(s.peak_torque_lbft, 1)} lb·ft) @ {s.peak_torque_rpm}</dd>
            <dt>Peak power</dt><dd>{fmt(s.peak_power_kw, 1)} kW · {fmt(s.peak_power_hp, 1)} hp · {fmt(s.peak_power_ps, 1)} PS @ {s.peak_power_rpm}</dd></>}
          {s.peak_power_corr_kw !== undefined && <><dt>Corrected ({r.correction.standard})</dt><dd>{fmt(s.peak_power_corr_kw, 1)} kW (CF {fmt(s.correction_factor, 4)}{r.correction.applicable ? "" : ", not applicable"})</dd></>}
          {s.peak_wheel_power_kw !== undefined && <><dt>Wheel (stated loss)</dt><dd>{fmt(s.peak_wheel_power_kw, 1)} kW · {fmt(s.peak_wheel_power_hp, 1)} hp — {s.drivetrain_loss}</dd></>}
          {s.final_torque_nm !== undefined && <><dt>Final torque</dt><dd>{fmt(s.final_torque_nm, 1)} N·m</dd></>}
        </dl>
        <dl className="kv">
          <dt>Max boost</dt><dd>{fmt(s.max_boost_kpa, 1)} kPa</dd><dt>Max EGT</dt><dd>{fmt(s.max_egt_c, 0)} °C</dd>
          <dt>Max IAT</dt><dd>{fmt(s.max_iat_c, 1)} °C</dd><dt>Min λ</dt><dd>{fmt(s.min_lambda, 3)}</dd>
          <dt>Max knock retard</dt><dd>{fmt(s.max_knock_retard_deg, 1)}°</dd><dt>DTCs / protections</dt><dd>{[...(r.events?.dtcs ?? []), ...(r.events?.protections ?? [])].join(", ") || "none"}</dd>
        </dl>
        <div className="col">
          {r.metrics && Object.keys(r.metrics).length > 0 && <dl className="kv">{Object.entries(r.metrics).filter(([, v]) => typeof v !== "object").map(([k, v]: any) => <Fragment key={k}><dt>{k}</dt><dd>{typeof v === "number" ? fmt(v, 3) : String(v)}</dd></Fragment>)}</dl>}
          {r.uncertainty ? <div className="card tight">Model-uncertainty band (5–95 %): {fmt(r.uncertainty.peak_power_p05_kw, 1)}–{fmt(r.uncertainty.peak_power_p95_kw, 1)} kW
            <br /><small className="muted">Top sensitivities: {(r.uncertainty.sensitivity ?? []).slice(0, 3).map((x: any) => `${x.path} (±${fmt(x.swing_kw / 2, 1)} kW)`).join(", ")}</small>
            <br /><small className="muted">{r.uncertainty.notes?.[0]}</small></div>
            : ["sweep", "partial", "steady", "repeat"].includes(r.kind) && <button className="small" onClick={onUnc}>Estimate model uncertainty</button>}
          <div className="row"><select value={preset} onChange={(e) => setPreset(e.target.value)}>{["exhaust", "interior", "intake", "turbo"].map((p) => <option key={p}>{p}</option>)}</select>
            <audio controls preload="none" src={`/api/dyno/runs/${r.id}/sound.wav?preset=${preset}`} style={{ height: 30, maxWidth: 260 }} /></div>
          <small className="muted">Illustrative sound synthesis, not a recording.</small>
        </div>
      </div>
      {r.kind === "steady" && <div className="scroll"><table className="t"><thead><tr><th className="num">rpm</th><th className="num">pedal</th><th className="num">torque</th><th className="num">σ</th><th className="num">power</th><th className="num">MAP</th><th className="num">λ</th><th className="num">spark</th><th className="num">BSFC</th><th className="num">EGT</th></tr></thead>
        <tbody>{r.curve.map((p: any, i: number) => <tr key={i}><td className="num">{p.rpm}</td><td className="num">{p.pedal_pct}</td><td className="num">{fmt(p.torque_nm, 1)}</td><td className="num">{fmt(p.torque_std_nm, 2)}</td><td className="num">{fmt(p.power_kw, 1)}</td><td className="num">{fmt(p.map_kpa, 1)}</td><td className="num">{fmt(p.lambda, 3)}</td><td className="num">{fmt(p.spark_deg, 1)}</td><td className="num">{fmt(p.bsfc_g_kwh, 0)}</td><td className="num">{fmt(p.egt_c, 0)}</td></tr>)}</tbody></table></div>}
      <small className="muted">{r.limitations?.[0]}</small>
    </Card>
  );
}
