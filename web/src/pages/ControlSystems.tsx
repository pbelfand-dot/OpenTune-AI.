import { Fragment, useEffect, useState } from "react";
import { api, fmt } from "../api";
import { Card, Disclaimer, Field, JobBar, Num, Plot, useAction } from "../components/ui";
import { useLab } from "../context";
import { useApi, useJob } from "../hooks";

export default function ControlSystems() {
  const [tab, setTab] = useState("loop");
  return (
    <div className="grid">
      <Disclaimer>Control experiments on simple plant models and on the full engine simulation. Tuning suggestions use the published SIMC rules (Skogestad 2003). Optimisation results are optima of a synthetic model: always unvalidated and never a calibration for a vehicle.</Disclaimer>
      <div className="tabs">
        {[["loop", "PID loop lab"], ["engine", "Engine-in-the-loop"], ["opt", "Optimisation"]].map(([id, l]) =>
          <button key={id} className={tab === id ? "active" : ""} onClick={() => setTab(id)}>{l}</button>)}
      </div>
      {tab === "loop" && <LoopLab />}
      {tab === "engine" && <EngineLab />}
      {tab === "opt" && <OptimizeLab />}
    </div>
  );
}

function Metrics({ m }: { m: Record<string, any> }) {
  return <dl className="kv">{Object.entries(m).map(([k, v]) => <Fragment key={k}><dt>{k}</dt><dd>{typeof v === "number" ? fmt(v, 3) : v === null ? "–" : String(v)}</dd></Fragment>)}</dl>;
}

function LoopLab() {
  const meta = useApi<any>("/api/control/meta");
  const [plant, setPlant] = useState<any>(null);
  const [ctrl, setCtrl] = useState<any>(null);
  const [exp, setExp] = useState<any>(null);
  const [res, setRes] = useState<any>(null);
  const [tuning, setTuning] = useState<any>(null);
  const { run, busy } = useAction();
  useEffect(() => {
    if (meta.data && !plant) {
      setPlant({ ...meta.data.defaults.plant, gain: 2, tau: 1, delay: 0.2 });
      setCtrl({ ...meta.data.defaults.controller, kp: 1.25, ki: 1.25, out_min: -5, out_max: 5 });
      setExp({ ...meta.data.defaults.experiment, disturbance: 0.2, disturbance_time: 4, duration: 8 });
    }
  }, [meta.data, plant]);
  if (!plant) return <div className="muted">Loading…</div>;
  const simulate = () => run(async () => setRes(await api.post("/api/control/loop", { plant, controller: ctrl, experiment: exp })));
  const simc = () => run(async () => {
    const t = await api.post("/api/control/simc", { plant });
    setTuning(t);
    setCtrl((c: any) => ({ ...c, kp: +t.kp.toFixed(4), ki: +t.ki.toFixed(4), kd: 0 }));
  });
  const P = (k: string, v: any) => setPlant((x: any) => ({ ...x, [k]: v }));
  const C = (k: string, v: any) => setCtrl((x: any) => ({ ...x, [k]: v }));
  const E = (k: string, v: any) => setExp((x: any) => ({ ...x, [k]: v }));
  return (
    <div className="grid g3">
      <Card title="Plant">
        <div className="form">
          <Field label="Kind"><select value={plant.kind} onChange={(e) => P("kind", e.target.value)}>{meta.data.plant_kinds.map((k: string) => <option key={k}>{k}</option>)}</select></Field>
          <Field label="Gain K"><Num value={plant.gain} onChange={(v) => P("gain", v)} /></Field>
          {plant.kind === "fopdt" && <Field label="τ (s)"><Num value={plant.tau} step={0.1} onChange={(v) => P("tau", v)} /></Field>}
          {plant.kind === "sopdt" && <><Field label="ωn (rad/s)"><Num value={plant.wn} onChange={(v) => P("wn", v)} /></Field><Field label="ζ"><Num value={plant.zeta} step={0.05} onChange={(v) => P("zeta", v)} /></Field></>}
          <Field label="Dead time θ (s)"><Num value={plant.delay} step={0.05} onChange={(v) => P("delay", v)} /></Field>
        </div>
        <button style={{ marginTop: ".5rem" }} onClick={simc} disabled={busy || plant.kind === "sopdt"}>SIMC PI suggestion</button>
        {tuning && <p className="muted"><small>{tuning.method}: Kc {fmt(tuning.kp, 3)}, τI {fmt(tuning.ti, 3)} s → ki {fmt(tuning.ki, 3)}</small></p>}
      </Card>
      <Card title="Controller">
        <div className="form">
          {["kp", "ki", "kd", "derivative_tau", "feedforward", "out_min", "out_max", "rate_limit", "sample_time"].map((k) =>
            <Field key={k} label={k}><Num value={ctrl[k]} step={0.05} onChange={(v) => C(k, v)} /></Field>)}
          <Field label="Anti-windup"><input type="checkbox" checked={ctrl.anti_windup} onChange={(e) => C("anti_windup", e.target.checked)} /></Field>
        </div>
      </Card>
      <Card title="Experiment" actions={<button className="primary" onClick={simulate} disabled={busy}>Simulate</button>}>
        <div className="form">
          {["setpoint_initial", "setpoint_final", "step_time", "disturbance", "disturbance_time", "noise_std", "sensor_delay", "duration"].map((k) =>
            <Field key={k} label={k}><Num value={exp[k]} step={0.05} onChange={(v) => E(k, v)} /></Field>)}
        </div>
      </Card>
      {res && <>
        <Card title="Step response" className="span2">
          <Plot height={340} data={[
            { x: res.t, y: res.setpoint, name: "set-point", type: "scatter", mode: "lines", line: { dash: "dot", color: "#8b95a7" } },
            { x: res.t, y: res.measured, name: "measured", type: "scatter", mode: "lines", line: { color: "#3a6f86", width: 1 } },
            { x: res.t, y: res.output, name: "output y", type: "scatter", mode: "lines", line: { color: "#4cc9f0", width: 2 } },
            { x: res.t, y: res.control, name: "control u", yaxis: "y2", type: "scatter", mode: "lines", line: { color: "#f72585" } },
          ]} layout={{ xaxis: { title: { text: "time (s)" } }, yaxis2: { overlaying: "y", side: "right", title: { text: "u" } } }} />
          {res.notes.map((n: string) => <span key={n} className="chip warn">{n}</span>)}
        </Card>
        <Card title="Metrics"><Metrics m={res.metrics} />{Object.keys(res.disturbance_metrics).length > 0 && <><h4 style={{ marginTop: ".6rem" }}>Disturbance</h4><Metrics m={res.disturbance_metrics} /></>}</Card>
        <Card title="Open-loop Bode (L = C·P)" className="span3">
          <div className="row"><span className="chip acc">phase margin {fmt(res.frequency.phase_margin_deg, 1)}°</span><span className="chip acc">gain margin {fmt(res.frequency.gain_margin_db, 1)} dB</span>
            <span className="chip">ωc {fmt(res.frequency.gain_crossover_rad_s, 2)} rad/s</span><small className="muted">{res.frequency.note}</small></div>
          <div className="grid g2">
            <Plot height={260} data={[{ x: res.frequency.w, y: res.frequency.magnitude_db, type: "scatter", mode: "lines", line: { color: "#4cc9f0" }, name: "|L| dB" }]} layout={{ xaxis: { type: "log", title: { text: "ω (rad/s)" } }, yaxis: { title: { text: "dB" } }, shapes: [{ type: "line", xref: "paper", x0: 0, x1: 1, y0: 0, y1: 0, line: { color: "#555", dash: "dot" } }] }} />
            <Plot height={260} data={[{ x: res.frequency.w, y: res.frequency.phase_deg, type: "scatter", mode: "lines", line: { color: "#f72585" }, name: "∠L °" }]} layout={{ xaxis: { type: "log", title: { text: "ω (rad/s)" } }, yaxis: { title: { text: "degrees" } }, shapes: [{ type: "line", xref: "paper", x0: 0, x1: 1, y0: -180, y1: -180, line: { color: "#555", dash: "dot" } }] }} />
          </div>
        </Card>
      </>}
    </div>
  );
}

function EngineLab() {
  const { project } = useLab();
  const meta = useApi<any>("/api/control/meta");
  const cal = useApi<any>(`/api/projects/${project!.id}/calibration`, [project!.id]);
  const [exp, setExp] = useState<any>(null);
  const [gains, setGains] = useState<Record<string, number>>({});
  const [results, setResults] = useState<any[]>([]);
  const { run, busy } = useAction();
  const job = useJob((j) => j.result && setResults((r) => [j.result, ...r].slice(0, 4)));
  useEffect(() => { if (meta.data && !exp) setExp({ ...meta.data.defaults.engine }); }, [meta.data, exp]);
  if (!exp || !cal.data) return <div className="muted">Loading…</div>;
  const scal = Object.fromEntries(cal.data.scalars.map((s: any) => [s.id, s]));
  const start = () => run(async () => job.start(await api.post("/api/control/engine", { project_id: project!.id, experiment: { ...exp, gains } })));
  const applyToCal = (g: Record<string, number>) => run(async () => {
    for (const [k, v] of Object.entries(g)) await api.post(`/api/projects/${project!.id}/calibration/scalars/${k}`, { value: v });
    cal.reload();
  }, "Gains written to the working copy (undoable on the ECU Tuning page)");
  const E = (k: string, v: any) => setExp((x: any) => ({ ...x, [k]: v }));
  const r = results[0];
  return (
    <div className="grid g3">
      <Card title="Experiment" actions={<button className="primary" disabled={busy || job.running} onClick={start}>Run</button>}>
        <div className="form">
          <Field label="Kind"><select value={exp.kind} onChange={(e) => E("kind", e.target.value)}>{meta.data.engine_experiments.map((k: string) => <option key={k}>{k}</option>)}</select></Field>
          {exp.kind !== "idle_disturbance" && <Field label="Dyno rpm"><Num value={exp.rpm} step={250} onChange={(v) => E("rpm", v)} /></Field>}
          {["boost_step", "torque_tracking"].includes(exp.kind) && <><Field label="Pedal from %"><Num value={exp.pedal_from} onChange={(v) => E("pedal_from", v)} /></Field><Field label="Pedal to %"><Num value={exp.pedal_to} onChange={(v) => E("pedal_to", v)} /></Field></>}
          {exp.kind === "idle_disturbance" && <Field label="Load step N·m"><Num value={exp.load_step_nm} onChange={(v) => E("load_step_nm", v)} /></Field>}
          {exp.kind === "wastegate_step" && <><Field label="Wastegate from % open"><Num value={exp.wastegate_from} onChange={(v) => E("wastegate_from", v)} /></Field><Field label="… to % open"><Num value={exp.wastegate_to} onChange={(v) => E("wastegate_to", v)} /></Field></>}
          <Field label="Duration s"><Num value={exp.duration} onChange={(v) => E("duration", v)} /></Field>
        </div>
        <div style={{ marginTop: ".5rem" }}><JobBar job={job.job} /></div>
      </Card>
      <Card title="Gain overrides (this experiment only)" className="span2">
        <table className="t"><thead><tr><th>Scalar</th><th className="num">Calibration</th><th>Override</th><th>Range</th></tr></thead>
          <tbody>{meta.data.gain_scalars.map((k: string) => scal[k] && <tr key={k}><td>{scal[k].name} <small className="mono muted">{k}</small></td>
            <td className="num">{scal[k].value}</td>
            <td><Num value={gains[k] ?? null} onChange={(v) => setGains((g) => ({ ...g, [k]: v }))} /> {gains[k] !== undefined && <button className="small ghost" onClick={() => setGains((g) => { const x = { ...g }; delete x[k]; return x; })}>✕</button>}</td>
            <td className="muted">{scal[k].minimum}…{scal[k].maximum} {scal[k].unit}</td></tr>)}</tbody></table>
      </Card>
      {r && <>
        <Card title={r.description} className="span2">
          <Plot height={360} data={r.kind === "idle_disturbance" ? [
            { x: r.series.map((p: any) => p.t), y: r.series.map((p: any) => p.rpm), name: "rpm", type: "scatter", mode: "lines", line: { color: "#4cc9f0" } },
            { x: r.series.map((p: any) => p.t), y: r.series.map((p: any) => p.idle_target_rpm), name: "idle target", type: "scatter", mode: "lines", line: { dash: "dot", color: "#8b95a7" } },
            { x: r.series.map((p: any) => p.t), y: r.series.map((p: any) => p.spark_deg), name: "spark °", yaxis: "y2", type: "scatter", mode: "lines", line: { color: "#9d4edd" } },
          ] : r.kind === "torque_tracking" ? [
            { x: r.series.map((p: any) => p.t), y: r.series.map((p: any) => p.torque_nm), name: "torque", type: "scatter", mode: "lines", line: { color: "#4cc9f0" } },
            { x: r.series.map((p: any) => p.t), y: r.series.map((p: any) => p.torque_request_nm), name: "request", type: "scatter", mode: "lines", line: { dash: "dot", color: "#8b95a7" } },
            { x: r.series.map((p: any) => p.t), y: r.series.map((p: any) => p.throttle_cmd), name: "throttle %", yaxis: "y2", type: "scatter", mode: "lines", line: { color: "#ffb703" } },
          ] : [
            { x: r.series.map((p: any) => p.t), y: r.series.map((p: any) => p.boost_kpa), name: "boost", type: "scatter", mode: "lines", line: { color: "#f72585" } },
            { x: r.series.map((p: any) => p.t), y: r.series.map((p: any) => p.boost_target_kpa), name: "boost target", type: "scatter", mode: "lines", line: { dash: "dot", color: "#8b95a7" } },
            { x: r.series.map((p: any) => p.t), y: r.series.map((p: any) => p.wastegate_cmd), name: "wastegate % open", yaxis: "y2", type: "scatter", mode: "lines", line: { color: "#b5e48c" } },
          ]} layout={{ xaxis: { title: { text: "time (s)" } }, yaxis2: { overlaying: "y", side: "right" } }} />
        </Card>
        <Card title="Result">
          <Metrics m={r.metrics} />
          {r.identified_model && <><h4 style={{ marginTop: ".6rem" }}>Identified model ({r.identified_model.fit_quality} fit)</h4>
            <Metrics m={{ gain: r.identified_model.gain, tau_s: r.identified_model.tau, delay_s: r.identified_model.delay, rmse: r.identified_model.rmse }} />
            <h4 style={{ marginTop: ".6rem" }}>SIMC suggestion</h4><Metrics m={r.suggested_gains} />
            {(r.notes ?? []).map((n: string) => <p key={n} className="muted"><small>{n}</small></p>)}
            <div className="row"><button className="small" onClick={() => { setGains((g) => ({ ...g, ...r.suggested_gains })); setExp((x: any) => ({ ...x, kind: "boost_step" })); }}>Try in boost_step</button>
              <button className="small" onClick={() => applyToCal(r.suggested_gains)}>Write to working copy</button></div></>}
          {Object.keys(r.gains || {}).length > 0 && <p className="muted"><small>Overrides: {JSON.stringify(r.gains)}</small></p>}
          {results.length > 1 && <><h4 style={{ marginTop: ".6rem" }}>Previous</h4>{results.slice(1).map((x, i) => <div key={i}><small className="muted">{x.kind} {JSON.stringify(x.gains)} → IAE {fmt(x.metrics.iae, 1)}, overshoot {fmt(x.metrics.overshoot_pct, 1)} %</small></div>)}</>}
        </Card>
      </>}
    </div>
  );
}

function OptimizeLab() {
  const { project, go } = useLab();
  const meta = useApi<any>("/api/optimize/meta");
  const [p, setP] = useState<any>(null);
  const [res, setRes] = useState<any>(null);
  const { run, busy } = useAction();
  const job = useJob((j) => j.result && setRes(j.result));
  useEffect(() => { if (meta.data && !p) setP({ ...meta.data.defaults }); }, [meta.data, p]);
  if (!p) return <div className="muted">Loading…</div>;
  const P = (k: string, v: any) => setP((x: any) => ({ ...x, [k]: v }));
  const start = () => run(async () => { setRes(null); job.start(await api.post("/api/optimize", { project_id: project!.id, problem: p })); });
  const apply = () => run(async () => api.post(`/api/projects/${project!.id}/optimize/apply-candidate`, { report_id: res.report_id }), "Candidate applied to the working copy (simulation only) — undo on the ECU Tuning page");
  return (
    <div className="grid g3">
      <Card title="Problem" actions={<button className="primary" onClick={start} disabled={busy || job.running}>Optimise</button>}>
        <div className="form">
          <Field label="Objective"><select value={p.objective} onChange={(e) => P("objective", e.target.value)}>{meta.data.objectives.map((o: string) => <option key={o}>{o}</option>)}</select></Field>
          <Field label="rpm points"><input value={p.rpm_points.join(", ")} onChange={(e) => P("rpm_points", e.target.value.split(",").map(Number).filter((x) => !Number.isNaN(x) && x > 0))} /></Field>
          <Field label="MAP bounds kPa"><div className="row"><Num value={p.map_bounds_kpa[0]} width="4.5rem" onChange={(v) => P("map_bounds_kpa", [v, p.map_bounds_kpa[1]])} /><Num value={p.map_bounds_kpa[1]} width="4.5rem" onChange={(v) => P("map_bounds_kpa", [p.map_bounds_kpa[0], v])} /></div></Field>
          <Field label="λ bounds"><div className="row"><Num value={p.lambda_bounds[0]} width="4.5rem" step={0.01} onChange={(v) => P("lambda_bounds", [v, p.lambda_bounds[1]])} /><Num value={p.lambda_bounds[1]} width="4.5rem" step={0.01} onChange={(v) => P("lambda_bounds", [p.lambda_bounds[0], v])} /></div></Field>
          {["knock_margin_deg", "egt_margin_c", "turbo_speed_margin", "peak_pressure_margin_bar", "wastegate_reserve", "torque_target_fraction", "ambient_temp_c", "robustness_samples"].map((k) =>
            <Field key={k} label={k}><Num value={p[k]} step={0.5} onChange={(v) => P(k, v)} /></Field>)}
          <Field label="max_torque_nm (optional)"><Num value={p.max_torque_nm} onChange={(v) => P("max_torque_nm", v || null)} /></Field>
        </div>
        <div style={{ marginTop: ".5rem" }}><JobBar job={job.job} onCancel={job.cancel} /></div>
        <p className="muted"><small>{meta.data.gate}</small></p>
      </Card>
      {res && <Card title="Result" className="span2" actions={<>
        <span className="chip bad">validation: {res.validation.status}</span><span className="chip bad">ready for vehicle: no</span>
        <button onClick={apply} disabled={busy}>Apply candidate to working copy (simulation)</button>
        <button onClick={() => go("dyno")}>Verify on dyno →</button></>}>
        <div className="banner" style={{ marginBottom: ".5rem" }}>{res.validation.summary}</div>
        <ul>{res.validation.reasons.map((r: string) => <li key={r}><small>{r}</small></li>)}</ul>
        {res.notes.map((n: string) => <p key={n} className="muted"><small>{n}</small></p>)}
        <div className="scroll"><table className="t"><thead><tr><th className="num">rpm</th><th className="num">MAP</th><th className="num">spark</th><th className="num">λ</th><th className="num">torque</th><th className="num">power</th><th className="num">BSFC</th><th className="num">EGT</th><th className="num">turbo</th><th className="num">knock lim.</th><th>active constraints</th><th className="num">P(violation)</th></tr></thead>
          <tbody>{res.points.map((x: any) => <tr key={x.rpm}><td className="num">{x.rpm}</td><td className="num">{fmt(x.map_kpa, 1)}</td><td className="num">{fmt(x.spark_deg, 1)}</td><td className="num">{fmt(x.lambda_value, 3)}</td>
            <td className="num">{fmt(x.torque_nm, 0)}</td><td className="num">{fmt(x.power_kw, 0)}</td><td className="num">{fmt(x.bsfc_g_kwh, 0)}</td><td className="num">{fmt(x.egt_c, 0)}</td><td className="num">{fmt(x.turbo_krpm, 0)}</td><td className="num">{fmt(x.knock_limit_deg, 1)}</td>
            <td>{x.success ? x.active_constraints.join(", ") : <span className="check-bad">{x.message}</span>}</td><td className="num">{x.robustness ? fmt(x.robustness.any_violation_probability, 2) : "–"}</td></tr>)}</tbody></table></div>
        <h4 style={{ marginTop: ".6rem" }}>Required before any real-world use</h4>
        <ul>{res.validation.required_before_any_real_use.map((r: string) => <li key={r}><small>{r}</small></li>)}</ul>
      </Card>}
    </div>
  );
}
