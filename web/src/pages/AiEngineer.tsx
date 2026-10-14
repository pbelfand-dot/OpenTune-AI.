import { useEffect, useState } from "react";
import { api, fmt, PROVENANCE } from "../api";
import { Card, Disclaimer, Field, Prov, Severity, useAction } from "../components/ui";
import { useLab } from "../context";
import { useApi } from "../hooks";

export default function AiEngineer() {
  const { project } = useLab();
  const pid = project!.id;
  const runs = useApi<any[]>(`/api/dyno/runs?project_id=${pid}`, [pid]);
  const settings = useApi<any>("/api/ai/settings");
  const status = useApi<any>("/api/ai/status");
  const [sel, setSel] = useState<string[]>([]);
  const [includeCal, setIncludeCal] = useState(true);
  const [useModel, setUseModel] = useState(true);
  const [result, setResult] = useState<any>(null);
  const [chat, setChat] = useState<{ q: string; a: string; prov: string; status: string }[]>([]);
  const [q, setQ] = useState("");
  const [cfg, setCfg] = useState<any>(null);
  const { run, busy } = useAction();
  useEffect(() => { if (runs.data && !sel.length && runs.data.length) setSel([runs.data[0].id]); }, [runs.data]); // eslint-disable-line
  useEffect(() => { if (settings.data) setCfg(settings.data); }, [settings.data]);
  const analyze = () => run(async () => setResult(await api.post("/api/ai/analyze", { project_id: pid, run_ids: sel, include_calibration: includeCal, use_model: useModel })));
  const ask = () => run(async () => {
    const r = await api.post("/api/ai/ask", { project_id: pid, run_ids: sel, include_calibration: includeCal, use_model: useModel, question: q });
    setChat((c) => [...c, { q, a: r.answer, prov: r.provenance, status: r.model_status }]);
    setQ("");
  });
  const saveCfg = () => run(async () => { await api.put("/api/ai/settings", cfg); await status.reload(); }, "Model settings saved");
  return (
    <div className="grid">
      <Disclaimer>The analyst states where every claim comes from. There is no measured data in the lab, so nothing is ever tagged “measured”. Language-model text is always labelled speculative.</Disclaimer>
      <div className="row">{Object.entries(PROVENANCE).map(([k]) => <Prov key={k} kind={k} />)}</div>
      <div className="grid g3">
        <Card title="Context" className="span2" actions={<button className="primary" onClick={analyze} disabled={busy}>Analyse</button>}>
          <div className="scroll" style={{ maxHeight: 220 }}>
            <table className="t"><tbody>{(runs.data ?? []).map((r) => <tr key={r.id}>
              <td><input type="checkbox" checked={sel.includes(r.id)} onChange={() => setSel((s) => s.includes(r.id) ? s.filter((x) => x !== r.id) : [...s, r.id].slice(-8))} /></td>
              <td>{r.label}<br /><small className="muted">{r.kind} · {r.created_utc.slice(0, 16)}</small></td>
              <td className="num">{fmt(r.summary.peak_power_kw, 0)} kW</td></tr>)}</tbody></table>
            {!runs.data?.length && <small className="muted">No dyno runs yet — the analysis will cover the calibration only.</small>}
          </div>
          <div className="row" style={{ marginTop: ".4rem" }}>
            <label className="f inline"><input type="checkbox" checked={includeCal} onChange={(e) => setIncludeCal(e.target.checked)} />include calibration diff vs baseline</label>
            <label className="f inline"><input type="checkbox" checked={useModel} onChange={(e) => setUseModel(e.target.checked)} />use local model if enabled</label>
            <small className="muted">The first two selected runs are compared.</small>
          </div>
        </Card>
        <Card title="Local model (optional)">
          <p className="muted" style={{ marginTop: 0 }}><small>{status.data?.detail}</small></p>
          {cfg && <div className="form">
            <Field label="Enabled"><input type="checkbox" checked={cfg.enabled} onChange={(e) => setCfg({ ...cfg, enabled: e.target.checked })} /></Field>
            <Field label="Backend"><select value={cfg.backend} onChange={(e) => setCfg({ ...cfg, backend: e.target.value })}><option value="ollama">Ollama</option><option value="openai-compatible">OpenAI-compatible (local)</option></select></Field>
            <Field label="URL (localhost only)"><input value={cfg.url} onChange={(e) => setCfg({ ...cfg, url: e.target.value })} /></Field>
            <Field label="Model"><input value={cfg.model} onChange={(e) => setCfg({ ...cfg, model: e.target.value })} /></Field>
          </div>}
          <button style={{ marginTop: ".5rem" }} onClick={saveCfg} disabled={busy}>Save & test</button>
          <p className="muted"><small>Free and self-hosted: e.g. <code>ollama pull llama3.2</code>. Cloud endpoints are rejected so data never leaves the machine.</small></p>
        </Card>
      </div>
      {result && <Card title={`Findings (${result.findings.length})`} actions={<>{Object.entries(result.counts).map(([k, v]: any) => v ? <Severity key={k} s={`${k} ${v}`.split(" ")[0]} /> : null)}<small className="muted">{result.model_status}</small></>}>
        <div className="col">
          {result.findings.map((f: any) => (
            <div key={f.id} className={`finding ${f.severity}`}>
              <div className="row"><Severity s={f.severity} /><b>{f.title}</b><span className="chip">{f.category}</span></div>
              <div>{f.summary}</div>
              {f.evidence.map((e: any, i: number) => <div key={i} className="ev"><Prov kind={e.kind} /> {e.text}{e.value !== null && e.value !== undefined ? `: ${e.value} ${e.unit}` : ""}{e.source && <small className="muted"> — {e.source}</small>}</div>)}
              {f.suggestion && <div className="ev muted">→ {f.suggestion}</div>}
            </div>))}
          {!result.findings.length && <small className="muted">No findings — nothing notable in the selected data.</small>}
        </div>
        {result.narrative && <div className="card tight" style={{ marginTop: ".6rem" }}><Prov kind="speculative" /> <b>Model narrative</b><p style={{ whiteSpace: "pre-wrap" }}>{result.narrative}</p></div>}
        <p className="muted"><small>{result.disclaimer}</small></p>
      </Card>}
      <Card title="Ask the analyst">
        <div className="col">
          {chat.map((c, i) => <div key={i} className="card tight"><b>Q:</b> {c.q}<div style={{ whiteSpace: "pre-wrap", marginTop: ".3rem" }}><Prov kind={c.prov} /> {c.a}</div><small className="muted">{c.status}</small></div>)}
          <div className="row"><input style={{ flex: 1 }} value={q} onChange={(e) => setQ(e.target.value)} placeholder="e.g. Why did knock retard appear above 5000 rpm?" onKeyDown={(e) => e.key === "Enter" && q.length > 1 && ask()} />
            <button className="primary" disabled={busy || q.length < 2} onClick={ask}>Ask</button></div>
          <small className="muted">Without a local model the answer is assembled from the rule-based findings.</small>
        </div>
      </Card>
    </div>
  );
}
