import { useState } from "react";
import { api, fmt } from "../api";
import { Card, Disclaimer, Field, Json, useAction } from "../components/ui";
import { useLab } from "../context";
import { useApi } from "../hooks";

export default function Library() {
  const { projects, project, setProjectId, refreshProjects, go } = useLab();
  const presets = useApi<any[]>("/api/engines/presets");
  const [name, setName] = useState("");
  const [preset, setPreset] = useState("b58-inspired");
  const [report, setReport] = useState<any>(null);
  const runs = useApi<any[]>(project ? `/api/projects/${project.id}/runs` : null, [project?.id]);
  const reports = useApi<any[]>(project ? `/api/projects/${project.id}/reports` : null, [project?.id]);
  const versions = useApi<any[]>(project ? `/api/projects/${project.id}/calibration/versions` : null, [project?.id]);
  const { run, busy } = useAction();
  const create = () => run(async () => {
    const p = await api.post("/api/projects", { name: name || `${preset} project`, preset_id: preset });
    await refreshProjects();
    setProjectId(p.id);
    setName("");
  }, "Project created with a synthetic baseline factory-programmed into its virtual ECU");
  return (
    <div className="grid">
      <Disclaimer>Everything is stored locally in one SQLite file in your OpenTune data directory. Nothing is uploaded.</Disclaimer>
      <div className="grid g3">
        <Card title="Projects" className="span2">
          <table className="t"><thead><tr><th>Name</th><th>Engine</th><th className="num">Active</th><th className="num">In ECU</th><th>Updated</th><th /></tr></thead>
            <tbody>{projects.map((p) => <tr key={p.id} style={p.id === project?.id ? { background: "#152031" } : undefined}>
              <td><b>{p.name}</b><br /><small className="muted">{p.notes}</small></td><td>{p.engine_name}</td>
              <td className="num">v{p.active_version.version}</td><td className="num">{p.flashed_version ? `v${p.flashed_version.version}` : "–"}</td>
              <td className="mono">{p.updated_utc.replace("T", " ").slice(0, 16)}</td>
              <td className="row"><button className="small" disabled={p.id === project?.id} onClick={() => setProjectId(p.id)}>Open</button>
                <button className="small" onClick={() => run(async () => { const n = prompt("New name", p.name); if (n) { await api.patch(`/api/projects/${p.id}`, { name: n }); await refreshProjects(); } })}>Rename</button>
                <button className="small danger" onClick={() => run(async () => { if (confirm(`Delete “${p.name}” with all versions, runs and reports?`)) { await api.del(`/api/projects/${p.id}`); await refreshProjects(); } })}>Delete</button></td>
            </tr>)}</tbody></table>
        </Card>
        <Card title="New project">
          <div className="col">
            <Field label="Name"><input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Friend's G20 330i (B48-inspired)" /></Field>
            <Field label="Engine preset"><select value={preset} onChange={(e) => setPreset(e.target.value)}>{(presets.data ?? []).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select></Field>
            <button className="primary" onClick={create} disabled={busy}>Create</button>
            <small className="muted">Creates a synthetic baseline calibration (v1, baseline) and factory-programs it into the project’s virtual ECU.</small>
          </div>
        </Card>
      </div>
      {project && <div className="grid g3">
        <Card title="Calibration versions" actions={<button className="small" onClick={() => go("tuning", { tab: "versions" })}>Manage →</button>}>
          <table className="t"><tbody>{(versions.data ?? []).slice().reverse().map((v) => <tr key={v.id}><td>v{v.version}</td><td>{v.note || <span className="muted">—</span>}</td>
            <td className="row">{v.is_baseline && <span className="chip info">baseline</span>}{v.flashed && <span className="chip ok">ECU</span>}{v.active && <span className="chip acc">active</span>}</td></tr>)}</tbody></table>
        </Card>
        <Card title="Dyno runs" actions={<button className="small" onClick={() => go("dyno")}>Open dyno →</button>}>
          <div className="scroll"><table className="t"><tbody>{(runs.data ?? []).map((r) => <tr key={r.id}><td>{r.label}<br /><small className="muted">{r.kind} · {r.created_utc.slice(0, 16)}</small></td>
            <td className="num">{fmt(r.summary.peak_power_kw, 0)} kW</td><td><button className="small" onClick={() => go("dyno", { run: r.id })}>view</button></td></tr>)}</tbody></table></div>
        </Card>
        <Card title="Reports (flash, scenarios, control, optimisation, analysis)">
          <div className="scroll"><table className="t"><tbody>{(reports.data ?? []).map((r) => <tr key={r.id}><td><span className="chip">{r.kind}</span></td><td>{r.title}<br /><small className="muted">{r.created_utc.slice(0, 16)}</small></td>
            <td><button className="small" onClick={() => run(async () => setReport(await api.get(`/api/projects/reports/${r.id}`)))}>view</button></td></tr>)}</tbody></table></div>
        </Card>
      </div>}
      {report && <Card title={report.title} actions={<><a className="btn small" href={`data:application/json,${encodeURIComponent(JSON.stringify(report, null, 1))}`} download={`${report.kind}_${report.id}.json`}>Download JSON</a><button className="small" onClick={() => setReport(null)}>Close</button></>}>
        <Json data={report.data} />
      </Card>}
    </div>
  );
}
