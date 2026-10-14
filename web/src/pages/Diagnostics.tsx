import { Fragment, useState } from "react";
import { api, fmt } from "../api";
import { Card, Disclaimer, Field, Num, useAction } from "../components/ui";
import { useLab } from "../context";
import { useApi, useInterval, useLive } from "../hooks";

export default function Diagnostics() {
  const { project, go } = useLab();
  const live = useLive(10);
  const meta = useApi<any>("/api/diagnostics/meta");
  const [dtcs, setDtcs] = useState<any[]>([]);
  const [sensors, setSensors] = useState<any[]>([]);
  const [open, setOpen] = useState<string | null>(null);
  const [fault, setFault] = useState({ sensor: "map", kind: "open_circuit", magnitude: 0 });
  const { run, busy } = useAction();
  const mine = live.status?.running && live.status?.project_id === project?.id;
  useInterval(async () => {
    if (!mine) return;
    try {
      setDtcs(await api.get("/api/diagnostics/dtcs"));
      setSensors(await api.get("/api/diagnostics/sensors"));
    } catch { /* session ended */ }
  }, mine ? 500 : null);
  const ecu = live.frame?.ecu;
  if (!mine) return (
    <div className="grid">
      <Disclaimer>Diagnostics act on the live virtual engine. Descriptions are generic SAE J2012 code texts.</Disclaimer>
      <Card title="No live session for this project"><button className="primary" onClick={() => go("dashboard")}>Start one on the Dashboard</button></Card>
    </div>
  );
  return (
    <div className="grid">
      <Disclaimer>Fault injection changes the virtual sensors’ electrical signals; the virtual ECU detects faults from voltages and plausibility, exactly as its software is written — not as any production ECU behaves.</Disclaimer>
      <div className="row">
        <span className={`chip ${ecu?.mil ? "bad" : "ok"}`}>MIL {ecu?.mil ? "ON" : "off"}</span>
        <span className={`chip ${ecu?.limp ? "bad" : "ok"}`}>{ecu?.limp ? "LIMP MODE" : "normal operation"}</span>
        <span className="chip">ECU {ecu?.state} · {ecu?.mode}</span>
        <span className="chip">{fmt(live.frame?.ch.rpm, 0)} rpm</span>
        {(ecu?.protections ?? []).map((p) => <span key={p} className="chip warn">{p}</span>)}
      </div>
      <div className="grid g2">
        <Card title="Diagnostic trouble codes" actions={<button onClick={() => run(() => api.post("/api/diagnostics/dtcs/clear"), "DTCs cleared")} disabled={busy}>Clear DTCs</button>}>
          <table className="t"><thead><tr><th>Code</th><th>Description</th><th>Status</th><th className="num">First seen (s)</th></tr></thead>
            <tbody>{dtcs.map((d) => <Fragment key={d.code}>
              <tr onClick={() => setOpen(open === d.code ? null : d.code)} style={{ cursor: "pointer" }}>
                <td className="mono">{d.code}</td><td>{d.description}</td>
                <td><span className={`chip ${d.status === "stored" ? "bad" : "warn"}`}>{d.status}</span></td><td className="num">{fmt(d.first_seen, 2)}</td></tr>
              {open === d.code && <tr><td colSpan={4}><dl className="kv">{Object.entries(d.freeze_frame).map(([k, v]: any) => <Fragment key={k}><dt>{k}</dt><dd>{String(v)}</dd></Fragment>)}</dl></td></tr>}
            </Fragment>)}</tbody></table>
          {!dtcs.length && <small className="muted">No DTCs. Click a code to see its freeze frame.</small>}
        </Card>
        <Card title="Inject a fault" actions={<button className="ghost" onClick={() => run(() => api.del("/api/diagnostics/faults"), "All faults cleared")}>Clear all faults</button>}>
          <div className="form">
            <Field label="Sensor"><select value={fault.sensor} onChange={(e) => setFault({ ...fault, sensor: e.target.value })}>
              {(meta.data?.sensors ?? []).map((s: any) => <option key={s.id} value={s.id}>{s.name}</option>)}</select></Field>
            <Field label="Fault"><select value={fault.kind} onChange={(e) => setFault({ ...fault, kind: e.target.value })}>
              {(meta.data?.fault_types ?? []).map((k: string) => <option key={k}>{k}</option>)}</select></Field>
            {["offset", "noise", "drift", "delay", "intermittent"].includes(fault.kind) &&
              <Field label={fault.kind === "drift" ? "units/s" : fault.kind === "delay" ? "seconds" : fault.kind === "intermittent" ? "probability" : "magnitude"}>
                <Num value={fault.magnitude} step={0.1} onChange={(v) => setFault({ ...fault, magnitude: v })} /></Field>}
          </div>
          <button className="primary" style={{ marginTop: ".5rem" }} disabled={busy} onClick={() => run(() => api.post("/api/diagnostics/faults", fault), `Injected ${fault.sensor} ${fault.kind}`)}>Inject</button>
          <h4 style={{ marginTop: ".8rem" }}>Active faults</h4>
          {Object.entries(live.status?.faults ?? {}).length ? Object.entries(live.status.faults).map(([s, f]: any) => <span key={s} className="chip bad">{s}: {f.kind}{f.magnitude ? ` ${f.magnitude}` : ""}</span>) : <small className="muted">none (status refreshes when the session status changes)</small>}
        </Card>
      </div>
      <Card title="Sensors (virtual ECU inputs)">
        <table className="t"><thead><tr><th>Sensor</th><th className="num">Signal (V)</th><th className="num">ECU value</th><th className="num">Physical truth</th><th>Unit</th><th>Fault</th><th>DTCs (low/high)</th></tr></thead>
          <tbody>{sensors.map((s) => <tr key={s.id}><td>{s.name}</td>
            <td className={`num ${s.volts !== null && (s.volts < 0.2 || s.volts > 4.8) ? "check-bad" : ""}`}>{s.volts === null ? "digital" : fmt(s.volts, 3)}</td>
            <td className="num">{fmt(s.value, 2)}</td><td className="num muted">{fmt(s.true_value, 2)}</td><td>{s.unit}</td>
            <td>{s.fault && <span className="chip bad">{s.fault}</span>}</td><td className="mono muted">{[s.dtc_low, s.dtc_high].filter(Boolean).join(" / ")}</td></tr>)}</tbody></table>
        <small className="muted">Valid analogue window 0.2–4.8 V. “Physical truth” is shown for teaching; a real ECU only sees the signal.</small>
      </Card>
      <Card title="Codes the virtual ECU can set">
        <div className="row">{(meta.data?.dtcs ?? []).map((d: any) => <span key={d.code} className="chip" title={d.description}><span className="mono">{d.code}</span> {d.description}</span>)}</div>
      </Card>
    </div>
  );
}
