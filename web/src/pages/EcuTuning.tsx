import { useMemo, useState } from "react";
import { api, fmt } from "../api";
import TableEditor from "../components/TableEditor";
import { Card, Disclaimer, Field, JobBar, Num, useAction } from "../components/ui";
import { useLab } from "../context";
import { useApi, useJob } from "../hooks";

export default function EcuTuning() {
  const { project, refreshProjects, params, go } = useLab();
  const [tab, setTab] = useState(params.tab || "tables");
  const summary = useApi<any>(`/api/projects/${project!.id}/calibration`, [project!.id]);
  const [table, setTable] = useState<string>("ign_base");
  const s = summary.data;
  return (
    <div className="grid">
      <Disclaimer>All tables are synthetic, generated from the engine model for the fictional OTVECU-1 virtual ECU. They are not extracted from any BMW DME and must never be used on a vehicle.</Disclaimer>
      {s && <div className="row">
        <span className="chip acc">working copy (based on v{project!.active_version.version})</span>
        {s.dirty ? <span className="chip warn">unsaved edits</span> : <span className="chip ok">saved</span>}
        {s.changed_vs_baseline ? <span className="chip info">differs from baseline</span> : <span className="chip">identical to baseline</span>}
        {project!.flashed_version_id === s.version_id && !s.dirty ? <span className="chip ok">this version is in the virtual ECU</span> :
          <span className="chip warn">virtual ECU runs v{project!.flashed_version?.version ?? "?"}</span>}
        {s.problems.length > 0 && <span className="chip bad">{s.problems.length} validation problems</span>}
        <span className="mono muted">sha256 {s.content_hash.slice(0, 12)}…</span>
      </div>}
      <div className="tabs">
        {[["tables", "Tables"], ["scalars", "Scalars"], ["versions", "Versions & compare"], ["ecu", "Virtual ECU (flash)"]].map(([id, label]) =>
          <button key={id} className={tab === id ? "active" : ""} onClick={() => setTab(id)}>{label}</button>)}
      </div>
      {tab === "tables" && s && <Tables summary={s} table={table} setTable={setTable} reload={summary.reload} />}
      {tab === "scalars" && s && <Scalars summary={s} reload={summary.reload} />}
      {tab === "versions" && s && <Versions summary={s} reload={summary.reload} refreshProjects={refreshProjects} />}
      {tab === "ecu" && <VirtualEcu reload={summary.reload} refreshProjects={refreshProjects} dirty={!!s?.dirty} goDyno={() => go("dyno")} />}
    </div>
  );
}

function Tables({ summary, table, setTable, reload }: any) {
  const { project } = useLab();
  const byCat = useMemo(() => {
    const m: Record<string, any[]> = {};
    for (const t of summary.tables) (m[t.category] ||= []).push(t);
    return m;
  }, [summary]);
  return (
    <div className="grid" style={{ gridTemplateColumns: "250px 1fr" }}>
      <Card title="Tables">
        <div className="col" style={{ gap: ".4rem" }}>
          {Object.entries(byCat).map(([cat, ts]) => (
            <div key={cat}>
              <h4>{summary.categories[cat] ?? cat}</h4>
              {ts.map((t: any) => (
                <button key={t.id} className={`small ${t.id === table ? "on" : "ghost"}`} style={{ display: "block", width: "100%", textAlign: "left", marginBottom: 2 }}
                  onClick={() => setTable(t.id)} title={t.description}>
                  {t.name} <span className="muted">{t.shape.join("×")}</span>
                  {t.changed_cells > 0 && <span className="chip info" style={{ marginLeft: 4 }}>{t.changed_cells}</span>}
                </button>
              ))}
            </div>
          ))}
        </div>
      </Card>
      <Card><TableEditor projectId={project!.id} tableId={table} onChanged={reload} /></Card>
    </div>
  );
}

function Scalars({ summary, reload }: any) {
  const { project } = useLab();
  const { run } = useAction();
  const [filter, setFilter] = useState("");
  const set = (id: string, value: number) => run(async () => { await api.post(`/api/projects/${project!.id}/calibration/scalars/${id}`, { value }); reload(); });
  return (
    <Card title="Scalar parameters" actions={<input placeholder="filter…" value={filter} onChange={(e) => setFilter(e.target.value)} />}>
      <div className="scroll" style={{ maxHeight: 640 }}>
        <table className="t">
          <thead><tr><th>Category</th><th>Name</th><th>Value</th><th>Unit</th><th>Range</th><th>Baseline</th><th>Description</th></tr></thead>
          <tbody>
            {summary.scalars.filter((x: any) => !filter || (x.name + x.id + x.category).toLowerCase().includes(filter.toLowerCase())).map((x: any) => (
              <tr key={x.id}>
                <td>{summary.categories[x.category] ?? x.category}</td>
                <td>{x.name}<br /><small className="mono muted">{x.id}</small></td>
                <td><Num value={x.value} onChange={(v) => v !== x.value && set(x.id, v)} /></td>
                <td>{x.unit}</td>
                <td className="muted">{x.minimum} … {x.maximum}</td>
                <td className={x.baseline !== x.value ? "check-bad" : "muted"}>{x.baseline}</td>
                <td className="muted">{x.description}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

function Versions({ summary, reload, refreshProjects }: any) {
  const { project } = useLab();
  const pid = project!.id;
  const versions = useApi<any[]>(`/api/projects/${pid}/calibration/versions`, [summary.content_hash]);
  const diff = useApi<any>(`/api/projects/${pid}/calibration/diff`, [summary.content_hash]);
  const [note, setNote] = useState("");
  const [importText, setImportText] = useState("");
  const { run, busy } = useAction();
  const save = () => run(async () => { await api.post(`/api/projects/${pid}/calibration/save`, { note }); setNote(""); await refreshProjects(); reload(); versions.reload(); }, "Version saved");
  const checkout = (vid: string) => run(async () => {
    try { await api.post(`/api/projects/${pid}/calibration/versions/${vid}/checkout`, {}); }
    catch (e: any) {
      if (e.status === 409 && confirm("Discard unsaved changes?")) await api.post(`/api/projects/${pid}/calibration/versions/${vid}/checkout`, { discard_changes: true });
      else throw e;
    }
    await refreshProjects(); reload(); versions.reload();
  }, "Version checked out");
  const setBaseline = (vid: string) => run(async () => { await api.post(`/api/projects/${pid}/calibration/versions/${vid}/baseline`); reload(); versions.reload(); diff.reload(); }, "Baseline changed");
  const doImport = () => run(async () => { await api.post(`/api/projects/${pid}/calibration/import`, { text: importText }); setImportText(""); reload(); }, "Imported into the working copy (save to keep)");
  const changed = Object.entries(diff.data?.tables ?? {}).filter(([, v]: any) => v.changed_cells || v.axes_changed);
  return (
    <div className="grid g2">
      <Card title="Save working copy as a new version">
        <div className="row">
          <input style={{ flex: 1 }} placeholder="What changed and why?" value={note} onChange={(e) => setNote(e.target.value)} />
          <button className="primary" onClick={save} disabled={busy || !summary.dirty}>Save version</button>
        </div>
        <h4 style={{ marginTop: "1rem" }}>Edit history (working copy)</h4>
        <ol className="steps" style={{ maxHeight: 200, overflow: "auto" }}>
          {summary.history.undo.map((h: any, i: number) => <li key={i}>{h.description}</li>)}
        </ol>
        {!summary.history.undo.length && <small className="muted">No edits since the last checkout.</small>}
      </Card>
      <Card title="Differences vs baseline">
        {diff.data?.identical ? <small className="muted">Working copy is identical to the baseline.</small> : (
          <table className="t"><thead><tr><th>Item</th><th className="num">Cells changed</th><th className="num">Max |Δ|</th><th className="num">Mean Δ</th></tr></thead>
            <tbody>
              {changed.map(([id, v]: any) => <tr key={id}><td>{id}</td><td className="num">{v.changed_cells}{v.axes_changed ? " (axes)" : ""}</td><td className="num">{fmt(v.max_abs_delta, 2)}</td><td className="num">{fmt(v.mean_delta, 3)}</td></tr>)}
              {Object.entries(diff.data?.scalars ?? {}).map(([id, v]: any) => <tr key={id}><td>{id}</td><td className="num" colSpan={3}>{v.a} → {v.b}</td></tr>)}
            </tbody></table>
        )}
      </Card>
      <Card title="Version history" className="span2">
        <table className="t"><thead><tr><th>v</th><th>Created (UTC)</th><th>Note</th><th>Hash</th><th>Status</th><th /></tr></thead>
          <tbody>{(versions.data ?? []).slice().reverse().map((v) => (
            <tr key={v.id}>
              <td>{v.version}</td><td className="mono">{v.created_utc}</td><td>{v.note}</td><td className="mono">{v.content_hash.slice(0, 10)}</td>
              <td className="row">{v.is_baseline && <span className="chip info">baseline</span>}{v.active && <span className="chip acc">active</span>}
                {v.flashed && <span className="chip ok">in virtual ECU</span>}{v.checked_out && <span className="chip">checked out</span>}</td>
              <td className="row"><button className="small" disabled={v.checked_out} onClick={() => checkout(v.id)}>Check out</button>
                <button className="small" disabled={v.is_baseline} onClick={() => setBaseline(v.id)}>Make baseline</button></td>
            </tr>))}</tbody></table>
      </Card>
      <Card title="Export">
        <div className="row">
          <a className="btn" href={`/api/projects/${pid}/calibration/export.json`}>OpenTune JSON</a>
          <a className="btn" href={`/api/projects/${pid}/calibration/export.mat`}>MATLAB .mat</a>
        </div>
        <p className="muted">The .mat file stores each table with its breakpoints for use with MATLAB/Simulink lookup-table blocks; MATLAB is not required to create it. Per-table CSV export is on the Tables tab.</p>
      </Card>
      <Card title="Import OpenTune JSON into the working copy">
        <textarea rows={5} value={importText} onChange={(e) => setImportText(e.target.value)} placeholder="Paste an exported OpenTune calibration (same engine and layout)…" />
        <div className="row" style={{ marginTop: ".4rem" }}>
          <input type="file" accept=".json" onChange={async (e) => { const f = e.target.files?.[0]; if (f) setImportText(await f.text()); }} />
          <button onClick={doImport} disabled={busy || !importText}>Import</button>
        </div>
        <small className="muted">The content hash is verified; tampered or foreign-layout files are rejected.</small>
      </Card>
    </div>
  );
}

function VirtualEcu({ reload, refreshProjects, dirty, goDyno }: any) {
  const { project } = useLab();
  const pid = project!.id;
  const ecu = useApi<any>(`/api/projects/${pid}/ecu`, [pid]);
  const meta = useApi<any>("/api/flash/meta");
  const versions = useApi<any[]>(`/api/projects/${pid}/calibration/versions`, [pid]);
  const [opts, setOpts] = useState<any>({ block_size: 512, retries: 3, link_corrupt_rate: 0, link_drop_rate: 0, disconnect_after_frames: null, power_loss_stage: null, power_loss_block: 3, bitflip_block: null });
  const [vid, setVid] = useState<string>("");
  const [report, setReport] = useState<any>(null);
  const [readBack, setReadBack] = useState<any>(null);
  const { run, busy } = useAction();
  const after = async () => { await ecu.reload(); await refreshProjects(); reload(); };
  const job = useJob(async (j) => { setReport(j.result); await after(); });
  const flash = () => run(async () => {
    setReport(null);
    const body: any = { ...opts, version_id: vid || project!.active_version_id };
    if (!body.power_loss_stage) delete body.power_loss_stage;
    job.start(await api.post(`/api/projects/${pid}/ecu/flash`, body));
  });
  const st = ecu.data?.status;
  const o = (k: string, v: any) => setOpts((x: any) => ({ ...x, [k]: v }));
  return (
    <div className="grid g2">
      <Card title="Virtual ECU (OTVECU-SIM)" actions={<button className="small" onClick={() => ecu.reload()}>Refresh</button>}>
        {st && <>
          <dl className="kv">
            <dt>Mode</dt><dd><span className={`chip ${st.mode === "application" ? "ok" : "bad"}`}>{st.mode}</span></dd>
            <dt>Running bank</dt><dd>{st.running_bank ?? "–"}</dd>
            <dt>Calibration in ECU</dt><dd>{ecu.data.flashed_version ? `v${ecu.data.flashed_version.version} — ${ecu.data.flashed_version.note || ecu.data.flashed_version.name}` : "unknown / not a saved version"}</dd>
            <dt>Firmware</dt><dd>{st.firmware.family} app {st.firmware.app_version} · boot {st.firmware.boot_version}</dd>
            <dt>Boot control</dt><dd className="mono">{st.bcb.copies.map((c: any, i: number) => c ? `#${i}: bank ${c.bank} seq ${c.seq}` : `#${i}: invalid`).join(" · ")}</dd>
            <dt>Engine running</dt><dd>{st.engine_running ? "yes — programming will be refused" : "no"}</dd>
          </dl>
          <table className="t" style={{ marginTop: ".5rem" }}><thead><tr><th>Bank</th><th>State</th><th>Detail</th></tr></thead>
            <tbody>{Object.values(st.banks).map((b: any) => <tr key={b.bank}><td>{b.bank}</td>
              <td><span className={`chip ${b.state === "valid" ? "ok" : b.state === "erased" ? "" : "bad"}`}>{b.state}</span></td>
              <td className="mono">{b.header ? `${b.header.engine_id} · schema ${b.header.schema_version} · ${b.image_bytes} B · sha ${b.header.payload_sha256.slice(0, 10)}` : b.reason ?? ""}</td></tr>)}</tbody></table>
          <div className="row" style={{ marginTop: ".6rem" }}>
            <button onClick={() => run(async () => { setReport(await api.post(`/api/projects/${pid}/ecu/rollback`)); await after(); })} disabled={busy}>Roll back to other bank</button>
            <button onClick={() => run(async () => { const r = await api.post(`/api/projects/${pid}/ecu/power-cycle`); await after(); return r; }, "ECU power-cycled")} disabled={busy}>Power cycle</button>
            <button onClick={() => run(async () => setReadBack(await api.post(`/api/projects/${pid}/ecu/read`, {})))} disabled={busy}>Read back calibration</button>
          </div>
          {readBack && <div className="card tight" style={{ marginTop: ".5rem" }}>
            Read {readBack.name} ({readBack.link.frames} frames). {readBack.matches_version ? "Matches a saved version." : "Not a saved version."} {readBack.differs_from_working_copy ? "Differs from the working copy." : "Identical to the working copy."}
            <button className="small" style={{ marginLeft: 8 }} onClick={() => run(async () => { await api.post(`/api/projects/${pid}/ecu/read`, { load_into_working_copy: true }); await after(); }, "Loaded into the working copy")}>Load into working copy</button>
          </div>}
        </>}
        <p className="muted" style={{ marginBottom: 0 }}>{meta.data?.note}</p>
      </Card>
      <Card title="Flash a saved version (simulated)">
        {dirty && <div className="chip warn" style={{ marginBottom: ".5rem" }}>The working copy has unsaved edits — only saved versions can be flashed.</div>}
        <div className="form">
          <Field label="Version"><select value={vid} onChange={(e) => setVid(e.target.value)}>
            <option value="">active (v{project!.active_version.version})</option>
            {(versions.data ?? []).map((v) => <option key={v.id} value={v.id}>v{v.version} {v.note}</option>)}</select></Field>
          <Field label="Block size (bytes)"><select value={opts.block_size} onChange={(e) => o("block_size", Number(e.target.value))}>{(meta.data?.block_sizes ?? [512]).map((b: number) => <option key={b}>{b}</option>)}</select></Field>
          <Field label="Retries per frame"><Num value={opts.retries} min={0} max={10} onChange={(v) => o("retries", v)} /></Field>
        </div>
        <h4 style={{ marginTop: ".8rem" }}>Fault injection</h4>
        <div className="form">
          <Field label="Frame corruption rate" title={meta.data?.faults?.link_corrupt_rate}><Num value={opts.link_corrupt_rate} step={0.05} min={0} max={0.5} onChange={(v) => o("link_corrupt_rate", v)} /></Field>
          <Field label="Lost-response rate" title={meta.data?.faults?.link_drop_rate}><Num value={opts.link_drop_rate} step={0.05} min={0} max={0.5} onChange={(v) => o("link_drop_rate", v)} /></Field>
          <Field label="Disconnect after N frames" title={meta.data?.faults?.disconnect_after_frames}><Num value={opts.disconnect_after_frames} min={1} onChange={(v) => o("disconnect_after_frames", v || null)} /></Field>
          <Field label="Power loss during"><select value={opts.power_loss_stage ?? ""} onChange={(e) => o("power_loss_stage", e.target.value || null)}>
            <option value="">— none —</option><option value="erase">erase</option><option value="write">write</option><option value="activate">activate</option></select></Field>
          {opts.power_loss_stage === "write" && <Field label="…at block"><Num value={opts.power_loss_block} min={0} onChange={(v) => o("power_loss_block", v)} /></Field>}
          <Field label="Flash-cell bit flip at block" title={meta.data?.faults?.bitflip_block}><Num value={opts.bitflip_block} min={0} onChange={(v) => o("bitflip_block", Number.isFinite(v) ? v : null)} /></Field>
        </div>
        <div className="row" style={{ marginTop: ".6rem" }}>
          <button className="primary" onClick={flash} disabled={busy || job.running}>Flash to virtual ECU</button>
          <button className="ghost small" onClick={() => setOpts({ ...opts, link_corrupt_rate: 0, link_drop_rate: 0, disconnect_after_frames: null, power_loss_stage: null, bitflip_block: null })}>clear faults</button>
        </div>
        <div style={{ marginTop: ".5rem" }}><JobBar job={job.job} /></div>
      </Card>
      {report && <Card title={`${report.operation} — ${report.success ? "SUCCESS" : "FAILED"}`} className="span2" actions={report.success && <button className="small primary" onClick={goDyno}>Test it on the virtual dyno →</button>}>
        <small className="muted">{report.label}</small>
        <table className="t" style={{ marginTop: ".4rem" }}><thead><tr><th>Step</th><th>Status</th><th>Detail</th><th className="num">t (sim ms)</th></tr></thead>
          <tbody>{report.steps.map((s: any, i: number) => <tr key={i}><td>{s.step}</td><td><span className={`chip ${s.status === "ok" ? "ok" : s.status === "failed" ? "bad" : ""}`}>{s.status}</span></td><td>{s.detail}</td><td className="num">{fmt(s.t_ms, 1)}</td></tr>)}</tbody></table>
        {report.error && <p className="check-bad">{report.error}</p>}
        {report.advice?.map((a: string, i: number) => <p key={i} className="muted" style={{ margin: ".2rem 0" }}>• {a}</p>)}
        <div className="row"><span className="chip">frames {report.link?.frames}</span><span className="chip">retries {report.retries}</span>
          <span className="chip">corrupted {report.link?.corrupted}</span><span className="chip">simulated {fmt(report.link?.simulated_seconds, 3)} s</span>
          {report.backup && <span className="chip">backup {report.backup.bytes} B</span>}</div>
      </Card>}
      <Card title="Backups (read back before every flash)" className="span2">
        <table className="t"><thead><tr><th>File</th><th>Engine</th><th className="num">Bytes</th><th>Valid</th><th /></tr></thead>
          <tbody>{(ecu.data?.backups ?? []).map((b: any) => <tr key={b.name}><td className="mono">{b.name}</td><td>{b.engine_id}</td><td className="num">{b.bytes}</td>
            <td>{b.valid ? "✔" : "✘"}</td><td><button className="small" disabled={!b.valid || busy} onClick={() => run(async () => { setReport(await api.post(`/api/projects/${pid}/ecu/backups/${b.name}/restore`)); await after(); })}>Restore</button></td></tr>)}</tbody></table>
        {!ecu.data?.backups?.length && <small className="muted">No backups yet — one is created at every flash.</small>}
      </Card>
    </div>
  );
}
