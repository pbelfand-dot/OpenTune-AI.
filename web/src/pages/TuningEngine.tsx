import { Fragment, useEffect, useMemo, useState } from "react";
import { api, fmt } from "../api";
import { Card, Disclaimer, Field, JobBar, Num, Plot, Stat, useAction } from "../components/ui";
import { useLab } from "../context";
import { useApi, useJob } from "../hooks";

const KEY_OUTPUTS = ["top_power_kw", "mean_power_kw", "mean_torque_nm", "part_load_bsfc_g_kwh", "margin:egt_c",
  "margin:turbo_krpm", "margin:peak_pressure_bar", "margin:torque_nm", "margin:min_lambda", "knock:adverse"];
const PHASE_COLOR: Record<string, string> = { baseline: "#8b95a7", probe: "#a0a8b8", repair: "#ffb703", search: "#4cc9f0", validation: "#c77dff" };

export default function TuningEngine() {
  const { project, go } = useLab();
  const pid = project!.id;
  const [tab, setTab] = useState<"session" | "knowledge" | "benchmark">("session");
  const meta = useApi<any>(`/api/tuning/meta?project_id=${pid}`, [pid]);
  const sessions = useApi<any[]>(`/api/tuning/sessions?project_id=${pid}`, [pid]);
  const [sid, setSid] = useState<string | null>(null);
  useEffect(() => { if (!sid && sessions.data?.length) setSid(sessions.data.find((s) => s.status === "done")?.id ?? null); }, [sessions.data]); // eslint-disable-line
  return (
    <div className="grid">
      <Disclaimer>The tuning engine experiments on the simulator: it proposes calibration changes, predicts their effect,
        tests them on a battery of simulated conditions (including deliberately wrong engine models), and learns from
        every result and mistake. Its output is a recommendation for further simulation — never a calibration for a
        vehicle, and never proof of safety.</Disclaimer>
      <div className="row">
        {(["session", "knowledge", "benchmark"] as const).map((t) => (
          <button key={t} className={tab === t ? "primary" : ""} onClick={() => setTab(t)}>
            {t === "session" ? "Tuning sessions" : t === "knowledge" ? "What it has learned" : "Benchmark & training"}</button>))}
        <span className="spacer" />
        <span className="chip">tuner v{meta.data?.tuner_version ?? "…"}</span>
        <span className="chip bad" title="Always false">ready for vehicle: no</span>
      </div>
      {tab === "session" && meta.data && <SessionTab meta={meta.data} pid={pid} sessions={sessions} sid={sid} setSid={setSid} go={go} />}
      {tab === "knowledge" && <KnowledgeTab pid={pid} />}
      {tab === "benchmark" && <BenchmarkTab />}
    </div>
  );
}

// ---------------------------------------------------------------------------------------- sessions
function SessionTab({ meta, pid, sessions, sid, setSid, go }: any) {
  const [goal, setGoal] = useState("max_power");
  const [budget, setBudget] = useState(12);
  const [calSource, setCalSource] = useState("working");
  const [actions, setActions] = useState<string[]>(meta.default_actions);
  const [adv, setAdv] = useState({ safety_k: 2, tolerance_pp: 0.5, min_gain: 0.1, stress_weight: 0.25, validation_fidelity: "standard", validate: true });
  const { run, busy } = useAction();
  const job = useJob((j) => { if (j.result?.session_id) { setSid(j.result.session_id); sessions.reload(); } });
  const start = () => run(async () => job.start(await api.post("/api/tuning/sessions", {
    project_id: pid, calibration: { source: calSource },
    request: { goal, budget, actions, ...adv },
  })));
  return (
    <>
      <div className="grid g3">
        <Card title="New session" className="span2" actions={<button className="primary" onClick={start} disabled={busy || job.running || !actions.length}>Start tuning session</button>}>
          <div className="row" style={{ flexWrap: "wrap", gap: ".8rem" }}>
            <Field label="Goal"><select value={goal} onChange={(e) => setGoal(e.target.value)}>
              {meta.goals.map((g: any) => <option key={g.id} value={g.id}>{g.label}</option>)}</select></Field>
            <Field label="Search budget (simulations)"><Num value={budget} onChange={setBudget} min={0} max={60} step={1} width="6rem" /></Field>
            <Field label="Start from"><select value={calSource} onChange={(e) => setCalSource(e.target.value)}>
              <option value="working">working copy</option><option value="active">active version</option>
              <option value="baseline">baseline</option><option value="flashed">version in the virtual ECU</option></select></Field>
            <Field label="Validation battery"><select value={adv.validation_fidelity} onChange={(e) => setAdv({ ...adv, validation_fidelity: e.target.value })}>
              {Object.keys(meta.fidelities).map((f) => <option key={f}>{f}</option>)}</select></Field>
          </div>
          <div className="muted" style={{ margin: ".6rem 0 .3rem" }}>Allowed actions (bounded, composite edits of torque limit + boost cap, ignition and lambda tables):</div>
          <div className="grid g4">{meta.actions.map((a: any) => (
            <label key={a.id} className="card tight" title={a.description} style={{ cursor: "pointer" }}>
              <div className="row"><input type="checkbox" checked={actions.includes(a.id)} onChange={() => setActions((x) => x.includes(a.id) ? x.filter((y) => y !== a.id) : [...x, a.id])} />
                <span>{a.label}<br /><small className="muted">step {a.step} {a.unit}, {a.lower}…+{a.upper} {a.unit}</small></span></div></label>))}</div>
          <details style={{ marginTop: ".5rem" }}><summary className="muted">Safety and search settings</summary>
            <div className="row" style={{ flexWrap: "wrap", gap: ".8rem", marginTop: ".4rem" }}>
              <Field label="Safety factor k (mean − kσ)"><Num value={adv.safety_k} onChange={(v) => setAdv({ ...adv, safety_k: v })} min={0} max={5} width="5rem" /></Field>
              <Field label="Tolerance on tight margins (pp)"><Num value={adv.tolerance_pp} onChange={(v) => setAdv({ ...adv, tolerance_pp: v })} min={0} max={5} width="5rem" /></Field>
              <Field label="Minimum gain"><Num value={adv.min_gain} onChange={(v) => setAdv({ ...adv, min_gain: v })} min={0} width="5rem" /></Field>
              <Field label="Stress penalty weight"><Num value={adv.stress_weight} onChange={(v) => setAdv({ ...adv, stress_weight: v })} min={0} width="5rem" /></Field>
              <label className="f inline"><input type="checkbox" checked={adv.validate} onChange={(e) => setAdv({ ...adv, validate: e.target.checked })} />validate the result</label>
            </div></details>
          <div style={{ marginTop: ".6rem" }}><JobBar job={job.job} onCancel={job.cancel} /></div>
        </Card>
        <Card title="Recent sessions">
          <div className="scroll" style={{ maxHeight: 330 }}>
            <table className="t"><tbody>{(sessions.data ?? []).map((s: any) => (
              <tr key={s.id} className={s.id === sid ? "sel" : ""} style={{ cursor: "pointer" }} onClick={() => s.status === "done" && setSid(s.id)}>
                <td>{s.goal}<br /><small className="muted">{s.created_utc.replace("T", " ").slice(0, 16)}</small></td>
                <td>{s.status !== "done" ? <span className="chip warn">{s.status}</span> : s.summary?.validated
                  ? <span className="chip ok">validated {fmt(s.summary.objective_validated, 2)}</span>
                  : <span className="chip">{s.summary?.changes?.length ? "not validated" : "no change"}</span>}</td>
                <td className="num">{s.summary?.evaluations_total ?? "–"} sims</td></tr>))}</tbody></table>
            {!sessions.data?.length && <div className="muted">No sessions yet for this project.</div>}
          </div>
        </Card>
      </div>
      {sid && <SessionReport sid={sid} pid={pid} go={go} />}
    </>
  );
}

function SessionReport({ sid, pid, go }: { sid: string; pid: string; go: any }) {
  const rep = useApi<any>(`/api/tuning/sessions/${sid}`, [sid]);
  const { run, busy } = useAction();
  const r = rep.data;
  const objChart = useMemo(() => {
    if (!r) return [];
    const steps = r.steps;
    const groups: Record<string, any[]> = {};
    for (const s of steps) {
      const k = s.phase === "search" || s.phase === "repair" ? `${s.phase === "repair" ? "repair " : ""}${s.accepted ? "accepted" : "rejected"}` : s.phase;
      (groups[k] ||= []).push(s);
    }
    const color: Record<string, string> = { ...PHASE_COLOR, accepted: "#52d273", rejected: "#ff4d6d", "repair accepted": "#ffb703", "repair rejected": "#9d4edd" };
    const traces: any[] = Object.entries(groups).map(([k, ss]) => ({
      type: "scatter", mode: "markers", name: k, x: ss.map((s) => s.index), y: ss.map((s) => s.objective),
      marker: { size: 9, color: color[k] }, text: ss.map((s) => s.description), hovertemplate: "%{text}<br>objective %{y:.3f}<extra></extra>",
    }));
    const pred = steps.filter((s: any) => s.predicted && (s.phase === "search" || s.phase === "repair"));
    traces.push({ type: "scatter", mode: "markers", name: "predicted ± 2σ", x: pred.map((s: any) => s.index + 0.15),
      y: pred.map((s: any) => s.predicted.objective[0]), marker: { symbol: "diamond-open", size: 8, color: "#ffb703" },
      error_y: { type: "data", array: pred.map((s: any) => 2 * s.predicted.objective[1]), color: "#ffb703", thickness: 1 } });
    return traces;
  }, [r]);
  if (!r) return rep.error ? <div className="chip bad">{rep.error}</div> : <div className="muted">Loading report…</div>;
  const s = r.summary;
  const apply = () => run(async () => { await api.post(`/api/projects/${pid}/tuning/apply`, { session_id: sid }); }, "Applied to the working copy (undo in ECU Tuning)");
  const curves = r.curves;
  const marginKeys = Object.keys(r.baseline.margins_pct);
  return (
    <>
      <div className="grid g4">
        <Stat label="Validated objective" value={s.validated ? s.objective_validated : "not validated"} digits={2} tone={s.validated ? "#52d273" : "#ffb703"} />
        <Stat label="Simulations used" value={s.evaluations_total} digits={0} />
        <Stat label="Probes skipped (known)" value={s.probes_skipped} digits={0} />
        <Stat label="Mistakes logged" value={s.mistakes} digits={0} tone={s.constraint_surprises ? "#ff4d6d" : undefined} />
      </div>
      <Card title={`Session ${r.id} — ${r.goal.label} on ${r.engine.name}`} actions={<>
        <span className="chip">{r.stop_reason.length > 70 ? r.stop_reason.slice(0, 70) + "…" : r.stop_reason}</span>
        <button className="primary" disabled={busy || !r.final.changes.length} onClick={apply} title="Writes the four tuned tables into the working copy (undoable)">Apply to working copy</button>
        <button className="small" onClick={() => go("dyno")}>Verify on the dyno →</button></>}>
        <div className="grid g2">
          <div>
            <h4 style={{ margin: ".2rem 0" }}>Diagnosis</h4>
            {r.diagnosis.map((d: any, i: number) => <div key={i} className="finding info"><b>{d.finding}</b><br /><small className="muted">{d.implication}</small></div>)}
          </div>
          <div>
            <h4 style={{ margin: ".2rem 0" }}>Recommendation (simulation only)</h4>
            {r.final.changes.length ? <ul style={{ margin: 0 }}>{r.final.changes.map((c: string) => <li key={c} className="mono">{c}</li>)}</ul>
              : <div className="muted">No change recommended — the baseline is kept.</div>}
            {r.repair && <div className={`finding ${r.repair.complete ? "notice" : "critical"}`} style={{ marginTop: ".4rem" }}>
              <b>{r.repair.complete ? "Starting calibration repaired" : "Starting calibration exceeds limits — repair incomplete"}</b>: limit excess {fmt(r.repair.start_excess, 2)} → {fmt(r.repair.best_excess, 2)}
              {!r.repair.complete && <><br /><small>Best partial repair (not recommended): {r.repair.best_changes.join("; ") || "–"}</small></>}</div>}
            <dl className="kv" style={{ marginTop: ".5rem" }}>{Object.entries(r.final.metrics_pct).map(([k, v]: any) =>
              <Fragment key={k}><dt>{k}</dt><dd className="mono" style={{ color: v > 0.05 ? "#52d273" : v < -0.05 ? "#ff4d6d" : undefined }}>{v > 0 ? "+" : ""}{fmt(v, 2)} %</dd></Fragment>)}</dl>
            {r.validation && <div className={`chip ${r.validation.passed ? "ok" : "bad"}`} style={{ whiteSpace: "normal" }}>
              Validation ({r.validation.fidelity} battery): {r.validation.passed ? `passed, objective ${fmt(r.validation.objective, 2)}` : r.validation.note ?? "failed"}</div>}
          </div>
        </div>
      </Card>
      <div className="grid g2">
        <Card title="Experiments: measured vs predicted objective">
          <Plot data={objChart} height={300} layout={{ xaxis: { title: "experiment #" }, yaxis: { title: "objective (weighted %)" } }} />
        </Card>
        <Card title="Full-load curve, nominal condition (search battery)">
          <Plot height={300} data={[
            { type: "scatter", mode: "lines", name: "baseline torque", x: curves.baseline.map((p: any) => p.rpm), y: curves.baseline.map((p: any) => p.torque_nm), line: { color: "#8b95a7" } },
            { type: "scatter", mode: "lines", name: "recommended torque", x: curves.final.map((p: any) => p.rpm), y: curves.final.map((p: any) => p.torque_nm), line: { color: "#4cc9f0" } },
            { type: "scatter", mode: "lines", name: "baseline power", yaxis: "y2", x: curves.baseline.map((p: any) => p.rpm), y: curves.baseline.map((p: any) => p.power_kw), line: { color: "#8b95a7", dash: "dot" } },
            { type: "scatter", mode: "lines", name: "recommended power", yaxis: "y2", x: curves.final.map((p: any) => p.rpm), y: curves.final.map((p: any) => p.power_kw), line: { color: "#52d273", dash: "dot" } },
          ]} layout={{ xaxis: { title: "rpm" }, yaxis: { title: "N·m" }, yaxis2: { title: "kW", overlaying: "y", side: "right" } }} />
        </Card>
      </div>
      <div className="grid g2">
        <Card title="Mechanical & thermal stress margins (worst condition)">
          <table className="t"><thead><tr><th>Stress</th><th className="num">baseline</th><th className="num">recommended</th><th className="num">floor</th><th className="num">limit</th></tr></thead>
            <tbody>{marginKeys.map((k) => {
              const m = r.final.score.margins_pct[k];
              return <tr key={k}><td>{k}</td><td className="num">{fmt(r.baseline.margins_pct[k], 2)} %</td>
                <td className="num" style={{ color: m < 0 ? "#ff4d6d" : m < (r.floors[k] ?? 0) ? "#ffb703" : undefined }}>{fmt(m, 2)} %</td>
                <td className="num">{fmt(r.floors[k], 2)} %</td><td className="num muted">{limitOf(r.limits, k)}</td></tr>;
            })}
              <tr><td>knock retard (nominal / adverse)</td><td className="num">{fmt(r.baseline.knock.nominal_deg, 1)} / {fmt(r.baseline.knock.adverse_deg, 1)}°</td>
                <td className="num">{fmt(r.final.score.knock.nominal_deg, 1)} / {fmt(r.final.score.knock.adverse_deg, 1)}°</td><td /><td className="num muted">{r.limits.max_knock_retard_nominal_deg} / {r.limits.max_knock_retard_adverse_deg}°</td></tr>
              <tr><td>charge-air temperature (indicator)</td><td className="num">{fmt(r.baseline.stress.iat_c, 1)} °C</td><td className="num">{fmt(r.final.score.stress.iat_c, 1)} °C</td><td /><td className="num muted">{r.limits.max_iat_c ?? "none"}</td></tr>
            </tbody></table>
          <small className="muted">Margins are distance to the limit in % (higher is safer). Limits are assumptions (provenance: {r.limits.provenance}), not component ratings. Conditions: {Object.keys(r.battery.conditions).join(", ")}.</small>
        </Card>
        <Card title="Model and learning">
          <dl className="kv">
            <dt>prediction coverage (±2σ)</dt><dd>{fmt((r.model.calibration.coverage_2sigma ?? 0) * 100, 0)} % (raw model {fmt((r.model.calibration.coverage_2sigma_raw ?? 0) * 100, 0)} %) over {r.model.calibration.n} predictions</dd>
            <dt>knowledge before</dt><dd>{r.learning.kb_trials_before} stored experiments ({r.learning.prior_trials_used} used as prior)</dd>
            <dt>probed</dt><dd>{r.learning.probed.join(", ") || "none"}</dd>
            <dt>skipped (already known)</dt><dd>{r.learning.probes_skipped.join(", ") || "none"}</dd>
          </dl>
          <details><summary className="muted">prior source per action</summary>
            <dl className="kv">{Object.entries(r.model.prior_sources).map(([a, src]: any) => <Fragment key={a}><dt>{a}</dt><dd>{src}</dd></Fragment>)}</dl></details>
          <small className="muted">{r.model.calibration.note}</small>
        </Card>
      </div>
      <Card title="Every experiment">
        <div className="scroll" style={{ maxHeight: 420 }}>
          <table className="t"><thead><tr><th>#</th><th>phase</th><th>change</th><th className="num">predicted</th><th className="num">measured</th><th>outcome</th><th>why</th></tr></thead>
            <tbody>{r.steps.map((st: any) => <tr key={st.index}>
              <td>{st.index}</td><td><span className="chip" style={{ borderColor: PHASE_COLOR[st.phase], color: PHASE_COLOR[st.phase] }}>{st.phase}</span></td>
              <td className="mono" style={{ fontSize: ".78rem" }}>{st.description}</td>
              <td className="num">{st.predicted ? `${fmt(st.predicted.objective[0], 2)} ± ${fmt(st.predicted.objective[1], 2)}` : "–"}</td>
              <td className="num">{fmt(st.objective, 2)}</td>
              <td>{st.phase === "baseline" ? "" : st.accepted ? <span className="chip ok">accepted</span> : st.passed ? <span className="chip">not kept</span> : <span className="chip bad">failed</span>}</td>
              <td style={{ fontSize: ".78rem" }}>{st.rationale}{st.mistakes.length > 0 && <div style={{ color: "#ffb703" }}>{st.mistakes.join(" · ")}</div>}</td></tr>)}</tbody></table>
        </div>
      </Card>
      <Card title="Read before using this result">
        <ul style={{ margin: 0 }}>{r.disclaimer.map((d: string) => <li key={d}>{d}</li>)}</ul>
      </Card>
    </>
  );
}

function limitOf(limits: any, key: string): string {
  const map: Record<string, string> = { peak_pressure_bar: "max_peak_pressure_bar", torque_nm: "max_torque_nm", turbo_krpm: "max_turbo_krpm",
    egt_c: "max_egt_c", compressor_out_c: "max_compressor_out_c", coolant_c: "max_coolant_c", min_lambda: "min_lambda", iat_c: "max_iat_c" };
  const v = limits[map[key]];
  return v === null || v === undefined ? "none" : String(v);
}

// ---------------------------------------------------------------------------------------- knowledge
function KnowledgeTab({ pid }: { pid: string }) {
  const k = useApi<any>(`/api/tuning/knowledge?project_id=${pid}`, [pid]);
  const mistakes = useApi<any[]>("/api/tuning/mistakes?limit=60");
  const { run, busy } = useAction();
  const d = k.data;
  const reset = () => { if (window.confirm("Delete every stored tuning experiment, session, mistake and benchmark?")) run(async () => { await api.post("/api/tuning/knowledge/reset", { confirm: true }); k.reload(); mistakes.reload(); }, "Knowledge base cleared"); };
  if (!d) return <div className="muted">Loading…</div>;
  return (
    <>
      <div className="grid g4">
        <Stat label="Stored experiments" value={d.trials} digits={0} />
        <Stat label="Sessions" value={d.sessions} digits={0} />
        <Stat label="Mistakes" value={d.mistakes_by_kind.reduce((a: number, m: any) => a + m.n, 0)} digits={0} />
        <Stat label="Engine model" value={d.engine_key} />
      </div>
      <Card title="Learned sensitivities: predicted effect of one action step from the baseline" actions={<button className="small danger" disabled={busy} onClick={reset}>Reset knowledge</button>}>
        <div className="scroll">
          <table className="t"><thead><tr><th>action (one step)</th><th>source</th>{KEY_OUTPUTS.map((o) => <th key={o} className="num" style={{ fontSize: ".72rem" }}>{o.replace("margin:", "Δmargin ").replace("knock:", "Δknock ")} <small className="muted">{d.units[o]}</small></th>)}</tr></thead>
            <tbody>{Object.entries(d.sensitivities).map(([a, outs]: any) => <tr key={a}>
              <td>{d.actions[a].label} <small className="muted">({d.actions[a].step} {d.actions[a].unit})</small></td>
              <td><small className="muted">{d.sources[a]}</small></td>
              {KEY_OUTPUTS.map((o) => { const [m, sd] = outs[o]; const strong = Math.abs(m) > 2 * sd;
                return <td key={o} className="num mono" style={{ fontSize: ".75rem", color: !strong ? "#8b95a7" : m > 0 === !o.startsWith("knock") && !o.includes("bsfc") ? "#52d273" : "#ff8fa3" }}>{m >= 0 ? "+" : ""}{fmt(m, 2)}<small className="muted"> ±{fmt(sd, 2)}</small></td>; })}
            </tr>)}</tbody></table>
        </div>
        <small className="muted">{d.sensitivity_note} Grey: not distinguishable from zero (|mean| ≤ 2σ). Green/red: good/bad for the engine (more output or more margin is green).</small>
      </Card>
      <div className="grid g2">
        <Card title="Caution factors (learned from prediction errors)">
          <dl className="kv">{Object.entries(d.caution).filter(([, v]: any) => v > 1.0).map(([o, v]: any) => <Fragment key={o}><dt>{o}</dt><dd>σ × {fmt(v, 2)}</dd></Fragment>)}</dl>
          {Object.values(d.caution).every((v: any) => v <= 1.0) && <div className="muted">All 1.0 — the model has not been over-confident so far (or has too little history).</div>}
          <small className="muted">When measured effects fall outside the predicted error bars more often than they should, predicted uncertainty for that output is inflated, so the safety screen becomes stricter.</small>
        </Card>
        <Card title="Recent mistakes">
          <div className="scroll" style={{ maxHeight: 300 }}>
            {(mistakes.data ?? []).map((m, i) => <div key={i} className={`finding ${m.kind === "constraint_surprise" || m.kind === "validation_failure" ? "warning" : "info"}`}>
              <b>{m.kind}</b> <small className="muted">{m.engine_key} · {m.actions.join(", ")}</small><br /><small>{m.detail}</small></div>)}
            {!mistakes.data?.length && <div className="muted">No mistakes recorded.</div>}
          </div>
        </Card>
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------------------- benchmark
function BenchmarkTab() {
  const hist = useApi<any[]>("/api/tuning/benchmarks");
  const [suite, setSuite] = useState("quick");
  const [learn, setLearn] = useState(false);
  const [rounds, setRounds] = useState(1);
  const { run, busy } = useAction();
  const job = useJob(() => hist.reload());
  const bench = () => run(async () => job.start(await api.post("/api/tuning/benchmark", { suite, learn })));
  const train = () => run(async () => job.start(await api.post("/api/tuning/train", { rounds })));
  const rows = [...(hist.data ?? [])].reverse();
  const latest = hist.data?.[0];
  return (
    <>
      <Card title="Benchmark and training" actions={<>
        <Field label="Suite" inline><select value={suite} onChange={(e) => setSuite(e.target.value)}><option>smoke</option><option>quick</option><option>standard</option></select></Field>
        <label className="f inline"><input type="checkbox" checked={learn} onChange={(e) => setLearn(e.target.checked)} />store experiments</label>
        <button className="primary" disabled={busy || job.running} onClick={bench}>Run benchmark</button>
        <Field label="Rounds" inline><Num value={rounds} onChange={setRounds} min={1} max={10} step={1} width="4rem" /></Field>
        <button disabled={busy || job.running} onClick={train}>Train on curriculum</button></>}>
        <p className="muted" style={{ marginTop: 0 }}>The benchmark runs fixed tuning tasks — including <b>held-out engines</b> (presets with perturbed model
          parameters and fuel) that training never sees — and scores validated gain, simulations used, mistakes and prediction honesty.
          Training runs the curriculum on the nominal presets and stores every experiment. A quick benchmark takes about 1–2 minutes; one training round 2–3 minutes.</p>
        <JobBar job={job.job} onCancel={job.cancel} />
      </Card>
      {rows.length > 0 && <Card title="Tuner performance over time">
        <Plot height={300} data={[
          { type: "scatter", mode: "lines+markers", name: "gain per 100 simulations", x: rows.map((b) => b.kb_trials_before), y: rows.map((b) => b.aggregate.gain_per_100_evaluations), marker: { color: "#52d273" } },
          { type: "scatter", mode: "lines+markers", name: "mean simulations per task", yaxis: "y2", x: rows.map((b) => b.kb_trials_before), y: rows.map((b) => b.aggregate.mean_evaluations), marker: { color: "#4cc9f0" } },
        ]} layout={{ xaxis: { title: "stored experiments before the benchmark" }, yaxis: { title: "gain / 100 sims" }, yaxis2: { title: "sims / task", overlaying: "y", side: "right" } }} />
        <table className="t"><thead><tr><th>when</th><th>suite</th><th className="num">experiments known</th><th className="num">mean gain</th><th className="num">held-out gain</th><th className="num">validated</th><th className="num">sims/task</th><th className="num">surprises</th><th className="num">validation failures</th><th className="num">±2σ coverage</th></tr></thead>
          <tbody>{(hist.data ?? []).map((b) => <tr key={b.id}><td>{b.created_utc.replace("T", " ").slice(0, 16)}</td><td>{b.suite}{b.learn ? " (learning)" : ""}</td>
            <td className="num">{b.kb_trials_before}</td><td className="num">{fmt(b.aggregate.mean_validated_gain, 2)}</td><td className="num">{fmt(b.aggregate.mean_validated_gain_held_out, 2)}</td>
            <td className="num">{fmt(b.aggregate.validated_rate * 100, 0)} %</td><td className="num">{fmt(b.aggregate.mean_evaluations, 1)}</td><td className="num">{b.aggregate.constraint_surprises}</td>
            <td className="num">{b.aggregate.validation_failures}</td><td className="num">{fmt((b.aggregate.mean_coverage_2sigma ?? 0) * 100, 0)} %</td></tr>)}</tbody></table>
      </Card>}
      {latest && <Card title={`Latest benchmark: ${latest.suite}`}>
        <table className="t"><thead><tr><th>engine</th><th>goal</th><th className="num">validated gain</th><th className="num">sims</th><th className="num">probes skipped</th><th className="num">surprises</th><th>recommended changes</th></tr></thead>
          <tbody>{latest.results.map((x: any, i: number) => <tr key={i}><td>{x.task.engine_key}{x.task.held_out && <span className="chip acc" style={{ marginLeft: 4 }}>held-out</span>}</td>
            <td>{x.task.goal}</td><td className="num">{fmt(x.validated_gain, 2)}</td><td className="num">{x.evaluations}</td><td className="num">{x.probes_skipped}</td><td className="num">{x.constraint_surprises}</td>
            <td style={{ fontSize: ".78rem" }}>{x.changes.join("; ") || <span className="muted">none</span>}</td></tr>)}</tbody></table>
      </Card>}
    </>
  );
}
