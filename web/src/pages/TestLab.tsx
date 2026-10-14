import { Fragment, useState } from "react";
import { api, fmt } from "../api";
import { Card, Disclaimer, JobBar, Severity, useAction } from "../components/ui";
import { useLab } from "../context";
import { useApi, useJob } from "../hooks";

export default function TestLab() {
  const { project, go } = useLab();
  const list = useApi<any>("/api/scenarios");
  const [ids, setIds] = useState<string[]>(["A", "B", "C", "D", "E", "F", "G"]);
  const [quick, setQuick] = useState(false);
  const [calSource, setCalSource] = useState("working");
  const [results, setResults] = useState<any[] | null>(null);
  const { run, busy } = useAction();
  const job = useJob((j) => j.result && setResults(j.result.results));
  const start = () => run(async () => { setResults(null); job.start(await api.post("/api/scenarios/run", { project_id: project!.id, ids, quick, calibration: { source: calSource } })); });
  return (
    <div className="grid">
      <Disclaimer>{list.data?.disclaimer}</Disclaimer>
      <Card title="Scenarios" actions={<>
        <label className="f inline">Calibration <select value={calSource} onChange={(e) => setCalSource(e.target.value)}><option value="working">working copy</option><option value="active">active version</option><option value="baseline">baseline</option><option value="flashed">version in the virtual ECU</option></select></label>
        <label className="f inline"><input type="checkbox" checked={quick} onChange={(e) => setQuick(e.target.checked)} />quick mode (shorter sweeps)</label>
        <button className="primary" onClick={start} disabled={busy || job.running || !ids.length}>Run {ids.length} scenario{ids.length === 1 ? "" : "s"}</button></>}>
        <div className="grid g4">
          {(list.data?.scenarios ?? []).map((s: any) => (
            <label key={s.id} className="card tight" style={{ cursor: "pointer" }}>
              <div className="row"><input type="checkbox" checked={ids.includes(s.id)} onChange={() => setIds((x) => x.includes(s.id) ? x.filter((y) => y !== s.id) : [...x, s.id].sort())} />
                <b>{s.id}</b> {s.name}</div>
            </label>))}
        </div>
        <div style={{ marginTop: ".6rem" }}><JobBar job={job.job} onCancel={job.cancel} /></div>
        <small className="muted">A full run of all seven takes roughly half a minute of compute; dyno runs are stored in the project.</small>
      </Card>
      {results && <div className="row">{results.map((r) => <span key={r.id} className={`chip ${r.passed ? "ok" : "bad"}`}>{r.id} {r.passed ? "PASS" : "FAIL"}</span>)}</div>}
      {results?.map((r) => (
        <Card key={r.id} title={`${r.id}. ${r.name}`} actions={<><span className={`chip ${r.passed ? "ok" : "bad"}`}>{r.passed ? "all expectations met" : "expectation failed"}</span><small className="muted">{fmt(r.duration_s, 1)} s</small>
          {r.run_ids.length > 0 && <button className="small" onClick={() => go("dyno", { run: r.run_ids[r.run_ids.length - 1] })}>Open run in dyno →</button>}</>}>
          <p className="muted" style={{ marginTop: 0 }}>{r.description}</p>
          <table className="t"><thead><tr><th style={{ width: 30 }} /><th>Check</th><th>Detail</th></tr></thead>
            <tbody>{r.checks.map((c: any, i: number) => <tr key={i}>
              <td className={c.passed === true ? "check-ok" : c.passed === false ? "check-bad" : "check-obs"}>{c.passed === true ? "✔" : c.passed === false ? "✘" : "•"}</td>
              <td>{c.name}{c.kind === "observation" && <small className="muted"> (observation)</small>}</td><td className="mono" style={{ fontSize: ".78rem" }}>{c.detail}</td></tr>)}</tbody></table>
          {Object.keys(r.metrics).length > 0 && <details style={{ marginTop: ".4rem" }}><summary className="muted">metrics</summary>
            <dl className="kv">{Object.entries(r.metrics).map(([k, v]: any) => <Fragment key={k}><dt>{k}</dt><dd className="mono" style={{ fontSize: ".78rem" }}>{typeof v === "object" ? JSON.stringify(v) : String(v)}</dd></Fragment>)}</dl></details>}
          {r.findings.length > 0 && <div className="col" style={{ marginTop: ".5rem" }}>{r.findings.slice(0, 6).map((f: any) => <div key={f.id} className={`finding ${f.severity}`}><Severity s={f.severity} /> <b>{f.title}</b> — {f.summary}</div>)}</div>}
        </Card>))}
    </div>
  );
}
