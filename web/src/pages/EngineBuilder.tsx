import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { Card, Disclaimer, Num, Plot, Prov, useAction } from "../components/ui";
import { useLab } from "../context";
import { useApi } from "../hooks";

interface Spec {
  path: string; label: string; unit: string; scale: number; minimum: number; maximum: number; group: string;
  description: string; kind: "float" | "int" | "bool" | "choice"; choices: string[]; step: number | null;
}

const GROUP_LABEL: Record<string, string> = {
  geometry: "Geometry", valvetrain: "Valvetrain & VE", intake: "Intake & charge cooling", turbo: "Turbocharger",
  exhaust: "Exhaust", fuel_system: "Fuel system", combustion: "Combustion & knock", friction: "Friction",
  thermal: "Thermal", mechanical: "Mechanical", limits: "Modelled limits",
};

function get(cfg: any, path: string) {
  const [s, k] = path.split(".");
  return cfg?.[s]?.[k];
}

export default function EngineBuilder() {
  const { project, refreshProjects, go } = useLab();
  const schema = useApi<Spec[]>("/api/engines/schema");
  const presets = useApi<any[]>("/api/engines/presets");
  const [cfg, setCfg] = useState<any>(project?.config);
  const [group, setGroup] = useState("geometry");
  const [problems, setProblems] = useState<string[]>([]);
  const [preview, setPreview] = useState<any>(null);
  const [boost, setBoost] = useState(200);
  const { run, busy } = useAction();
  useEffect(() => setCfg(project?.config), [project?.id, project?.updated_utc]);

  const groups = useMemo(() => Array.from(new Set((schema.data ?? []).map((s) => s.group))), [schema.data]);
  const dirty = JSON.stringify(cfg) !== JSON.stringify(project?.config);
  const setVal = (path: string, v: any) => {
    const [s, k] = path.split(".");
    setCfg((c: any) => ({ ...c, [s]: { ...c[s], [k]: v } }));
  };
  const validate = () => run(async () => {
    const r = await api.post("/api/engines/validate", { config: cfg });
    setProblems(r.problems);
    return r;
  });
  const doPreview = () => run(async () => setPreview(await api.post("/api/engines/preview", { config: cfg, boost_kpa: boost })));
  const save = () => run(async () => {
    await api.patch(`/api/projects/${project!.id}`, { config: cfg });
    await refreshProjects();
  }, "Engine configuration saved to the project");
  const regenerate = () => run(async () => {
    await api.post(`/api/projects/${project!.id}/regenerate-baseline`);
    await refreshProjects();
  }, "New synthetic baseline generated (flash it to use it in the virtual ECU)");
  const newFrom = () => run(async () => {
    const name = prompt("Name for the new project", `${cfg.name} (custom)`);
    if (!name) return;
    await api.post("/api/projects", { name, preset_id: project!.preset_id, config: cfg });
    await refreshProjects();
    go("library");
  }, "Project created");

  const prov = cfg?.provenance ?? {};
  const pts = preview?.points ?? [];
  return (
    <div className="grid">
      <Disclaimer>Engine presets are synthetic models built around a few documented specifications. Parameters without a source are engineering assumptions. This is not a BMW factory simulation.</Disclaimer>
      <div className="grid g3">
        <Card title="Engine" className="span2" actions={<>
          {dirty && <span className="chip warn">unsaved changes</span>}
          <button onClick={validate} disabled={busy}>Validate</button>
          <button className="primary" onClick={save} disabled={busy || !dirty}>Save to project</button>
        </>}>
          <div className="row" style={{ marginBottom: ".6rem" }}>
            <b>{cfg?.name}</b><small className="muted">{cfg?.description}</small>
          </div>
          <div className="tabs">
            {groups.map((g) => <button key={g} className={g === group ? "active" : ""} onClick={() => setGroup(g)}>{GROUP_LABEL[g] ?? g}</button>)}
          </div>
          <table className="t">
            <thead><tr><th>Parameter</th><th>Value</th><th>Unit</th><th>Range</th><th>Provenance</th></tr></thead>
            <tbody>
              {(schema.data ?? []).filter((s) => s.group === group).map((s) => {
                const raw = get(cfg, s.path);
                const p = prov[s.path];
                const kind = p?.provenance ?? "assumption";
                return (
                  <tr key={s.path} title={s.description}>
                    <td>{s.label}<br /><small className="muted mono">{s.path}</small></td>
                    <td>
                      {s.kind === "bool" ? <input type="checkbox" checked={!!raw} onChange={(e) => setVal(s.path, e.target.checked)} /> :
                        s.kind === "choice" ? <select value={raw} onChange={(e) => setVal(s.path, e.target.value)}>{s.choices.map((c) => <option key={c}>{c}</option>)}</select> :
                          Array.isArray(raw) ? <input value={raw.join(",")} onChange={(e) => setVal(s.path, e.target.value.split(",").map((x) => parseInt(x, 10)).filter((x) => !Number.isNaN(x)))} /> :
                            <Num value={raw === undefined ? null : +(raw * s.scale).toPrecision(6)} step={s.step ?? undefined}
                              onChange={(v) => setVal(s.path, s.kind === "int" ? Math.round(v / s.scale) : v / s.scale)} />}
                    </td>
                    <td>{s.unit}</td>
                    <td className="muted">{s.kind === "float" || s.kind === "int" ? `${s.minimum} … ${s.maximum}` : ""}</td>
                    <td><Prov kind={kind} />{p?.source && <div><small className="muted">{p.source}{p.note ? ` — ${p.note}` : ""}</small></div>}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {problems.length > 0 && <div className="col" style={{ marginTop: ".5rem" }}>{problems.map((p) => <span key={p} className="chip bad">{p}</span>)}</div>}
        </Card>
        <div className="col">
          <Card title="Presets">
            <div className="col">
              {(presets.data ?? []).map((p) => (
                <div key={p.id} className="card tight">
                  <b>{p.name}</b>
                  <div><small className="muted">Sanity target: {p.reference_ratings?.label} {p.reference_ratings?.power_kw} kW / {p.reference_ratings?.torque_nm} N·m</small></div>
                  <div><small className="muted">{p.documented_parameters.length} documented parameters; all others are assumptions.</small></div>
                  <button className="small" onClick={() => run(async () => setCfg(await api.get(`/api/engines/presets/${p.id}`)), "Preset loaded into the editor (not saved)")}>Load into editor</button>
                </div>
              ))}
            </div>
          </Card>
          <Card title="After changing the engine">
            <p className="muted" style={{ marginTop: 0 }}>The synthetic baseline calibration was generated from the previous configuration. Regenerate it so the VE, boost and torque-model tables match the new hardware, then flash it.</p>
            <div className="row">
              <button onClick={regenerate} disabled={busy || dirty} title={dirty ? "Save first" : ""}>Regenerate baseline</button>
              <button onClick={newFrom} disabled={busy}>New project from this config</button>
            </div>
          </Card>
        </div>
      </div>
      <Card title="Steady-state full-load preview (no calibration)" actions={<>
        <label className="f inline">Manifold pressure target <Num value={boost} step={5} min={80} max={320} onChange={setBoost} /> kPa</label>
        <button className="primary" onClick={doPreview} disabled={busy}>Compute</button>
      </>}>
        {pts.length ? (<>
          <Plot height={300} data={[
            { x: pts.map((p: any) => p.rpm), y: pts.map((p: any) => p.torque_nm), name: "torque N·m", type: "scatter", mode: "lines+markers", line: { color: "#4cc9f0" },
              marker: { color: pts.map((p: any) => (p.feasible ? "#4cc9f0" : "#ff4d6d")) } },
            { x: pts.map((p: any) => p.rpm), y: pts.map((p: any) => p.power_kw), name: "power kW", yaxis: "y2", type: "scatter", mode: "lines", line: { color: "#f72585", dash: "dash" } },
            { x: pts.map((p: any) => p.rpm), y: pts.map((p: any) => p.egt_c), name: "EGT °C", yaxis: "y2", type: "scatter", mode: "lines", line: { color: "#ffb703", dash: "dot" } },
          ]} layout={{ xaxis: { title: { text: "rpm" } }, yaxis: { title: { text: "N·m" } }, yaxis2: { overlaying: "y", side: "right", title: { text: "kW / °C" } } }} />
          <small className="muted">{preview.note} Red markers: turbo cannot reach the target.</small>
        </>) : <small className="muted">Compute a preview to see how parameter changes affect the model’s full-load capability.</small>}
      </Card>
    </div>
  );
}
