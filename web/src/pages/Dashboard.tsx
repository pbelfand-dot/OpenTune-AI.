import { useEffect, useMemo, useRef, useState } from "react";
import { api, fmt } from "../api";
import { Card, Disclaimer, Gauge, Num, Plot, Stat, useAction } from "../components/ui";
import { useLab } from "../context";
import { useApi, useLive } from "../hooks";

function useEngineSound(rpm: number | null | undefined, load: number, cylinders: number, enabled: boolean) {
  // Illustrative only: firing-frequency harmonics, same recipe as the server-side synthesiser.
  const ctx = useRef<AudioContext | null>(null);
  const oscs = useRef<{ o: OscillatorNode; g: GainNode; k: number }[]>([]);
  const master = useRef<GainNode | null>(null);
  useEffect(() => {
    if (!enabled) {
      ctx.current?.close();
      ctx.current = null;
      oscs.current = [];
      return;
    }
    const ac = new AudioContext();
    const m = ac.createGain();
    const lp = ac.createBiquadFilter();
    lp.type = "lowpass";
    lp.frequency.value = 1600;
    m.gain.value = 0.0;
    m.connect(lp).connect(ac.destination);
    const amps = [1, 0.55, 0.3, 0.18];
    oscs.current = amps.map((a, i) => {
      const o = ac.createOscillator();
      const g = ac.createGain();
      o.type = i === 0 ? "sawtooth" : "sine";
      g.gain.value = a * 0.25;
      o.connect(g).connect(m);
      o.start();
      return { o, g, k: i + 1 };
    });
    ctx.current = ac;
    master.current = m;
    return () => { ac.close(); };
  }, [enabled]);
  useEffect(() => {
    const ac = ctx.current;
    if (!ac || !master.current) return;
    const f = Math.max(0, (rpm ?? 0) / 60 * cylinders / 2);
    for (const { o, k } of oscs.current) o.frequency.setTargetAtTime(Math.max(1, f * k), ac.currentTime, 0.03);
    master.current.gain.setTargetAtTime(f > 3 ? 0.08 + 0.17 * Math.min(1, Math.max(0, load)) : 0, ac.currentTime, 0.05);
  }, [rpm, load, cylinders]);
}

const WORKFLOW = [
  ["Choose or create a project (engine preset)", "library"],
  ["Review engine parameters and their provenance", "engine"],
  ["Start the virtual engine and watch live data", "dashboard"],
  ["Open calibration tables in 2D/3D", "tuning"],
  ["Edit tables (cells, regions, axes) with undo/redo", "tuning"],
  ["Save a version and compare it with the baseline", "tuning"],
  ["Flash the version to the virtual ECU (simulated)", "tuning"],
  ["Run the virtual dyno and compare before/after", "dyno"],
  ["Run scenarios, diagnostics and control experiments", "tests"],
  ["Ask the AI engineer for an evidence-tagged analysis", "ai"],
] as const;

export default function Dashboard() {
  const { project, go, refreshProjects } = useLab();
  const live = useLive(30);
  const { run, busy } = useAction();
  const [sound, setSound] = useState(false);
  const [dynoRpm, setDynoRpm] = useState(3000);
  const runs = useApi<any[]>(project ? `/api/projects/${project.id}/runs` : null, [project?.id]);
  const reports = useApi<any[]>(project ? `/api/projects/${project.id}/reports` : null, [project?.id]);
  const versions = useApi<any[]>(project ? `/api/projects/${project.id}/calibration/versions` : null, [project?.id]);
  const f = live.frame;
  const ch = f?.ch ?? {};
  const running = !!live.status?.running;
  const mine = running && live.status?.project_id === project?.id;
  const cyl = project?.config?.geometry?.cylinders ?? 6;
  const maxRpm = project?.config?.limits?.max_rpm ?? 7000;
  useEngineSound(ch.rpm, (ch.torque_nm ?? 0) / 500, cyl, sound && mine);

  const control = (body: any) => run(() => api.post("/api/live/control", body));
  const start = () => run(async () => { await api.post("/api/live/start", { project_id: project!.id }); await refreshProjects(); }, "Virtual engine session started");

  const hist = live.history;
  const chart = useMemo(() => {
    const t = hist.map((h) => h.t);
    const line = (k: string, name: string, axis = "y", dash?: string, color?: string) =>
      ({ x: t, y: hist.map((h) => h.ch[k]), name, yaxis: axis, type: "scatter", mode: "lines", line: { width: 1.6, dash, color } });
    return [line("rpm", "rpm", "y", undefined, "#4cc9f0"), line("boost_kpa", "boost kPa", "y2", undefined, "#f72585"),
      line("boost_target_kpa", "boost target", "y2", "dot", "#f72585"), line("map_kpa", "MAP kPa", "y2", undefined, "#ffb703")];
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [live.tick]);
  const chart2 = useMemo(() => {
    const t = hist.map((h) => h.t);
    const line = (k: string, name: string, axis = "y", dash?: string, color?: string) =>
      ({ x: t, y: hist.map((h) => h.ch[k]), name, yaxis: axis, type: "scatter", mode: "lines", line: { width: 1.6, dash, color } });
    return [line("lambda", "λ", "y", undefined, "#b5e48c"), line("lambda_target", "λ target", "y", "dot", "#b5e48c"),
      line("spark_deg", "spark °", "y2", undefined, "#9d4edd"), line("knock_retard_deg", "knock retard °", "y2", undefined, "#ff4d6d")];
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [live.tick]);

  const done = [
    !!project, !!project, mine || (runs.data?.length ?? 0) > 0, (versions.data?.length ?? 0) > 1,
    (versions.data?.length ?? 0) > 1, (versions.data?.length ?? 0) > 1,
    !!project?.flashed_version && project.flashed_version.version > 1, (runs.data?.length ?? 0) > 1,
    (reports.data ?? []).some((r) => ["scenario", "control"].includes(r.kind)),
    (reports.data ?? []).some((r) => r.kind === "analysis"),
  ];

  return (
    <div className="grid">
      <Disclaimer />
      <Card title="Virtual engine" actions={<>
        <span className={`chip ${live.connected ? "ok" : "bad"}`}>{live.connected ? "stream connected" : "stream offline"}</span>
        {running && <span className="chip acc" title={live.status?.calibration}>{live.status?.engine} · {String(live.status?.calibration).slice(0, 70)}</span>}
      </>}>
        {!mine ? (
          <div className="row">
            <button className="primary" disabled={busy || !project} onClick={start}>Start session for “{project?.name}”</button>
            {running && <small className="muted">Another project’s session is running; starting replaces it.</small>}
            <small className="muted">The session runs exactly what is in this project’s virtual ECU memory.</small>
          </div>
        ) : (
          <div className="col">
            <div className="row">
              <button className={f?.driver.ignition ? "on" : ""} onClick={() => control({ action: f?.driver.ignition ? "ignition_off" : "ignition_on" })}>Ignition {f?.driver.ignition ? "ON" : "OFF"}</button>
              <button className="primary" disabled={f?.ecu.state === "RUNNING"} onClick={() => control({ action: "crank" })}>Crank / start</button>
              <button className="danger" onClick={() => control({ action: "stop_engine" })}>Stop engine</button>
              <span className="chip">ECU {f?.ecu.state ?? "–"}</span>
              <span className="chip">mode {f?.ecu.mode ?? "–"}</span>
              {f?.ecu.closed_loop && <span className="chip ok">λ closed loop</span>}
              {f?.ecu.mil && <span className="chip bad">MIL · {f.ecu.dtcs.join(", ")}</span>}
              {f?.ecu.limp && <span className="chip bad">LIMP</span>}
              {(f?.ecu.protections ?? []).map((p) => <span key={p} className="chip warn">{p}</span>)}
              <span className="spacer" />
              <label className="f inline"><input type="checkbox" checked={sound} onChange={(e) => setSound(e.target.checked)} />Illustrative sound</label>
              <button className="small" onClick={() => run(() => api.post("/api/live/stop"))}>End session</button>
            </div>
            <div className="row">
              <label className="f inline" style={{ minWidth: 340 }}>Pedal {fmt(f?.driver.pedal, 0)} %
                <input type="range" min={0} max={100} step={1} value={f?.driver.pedal ?? 0} style={{ width: 220 }}
                  onChange={(e) => control({ action: "pedal", value: Number(e.target.value) })} />
              </label>
              <label className="f inline">Load
                <select value={f?.load ?? "neutral"} onChange={(e) => control({ action: "load", load: e.target.value, target_rpm: dynoRpm })}>
                  <option value="neutral">Neutral (free rev)</option>
                  <option value="dyno">Dyno (hold speed)</option>
                  <option value="vehicle">Vehicle (one gear)</option>
                </select>
              </label>
              {f?.load === "dyno" && <label className="f inline">Dyno rpm
                <Num value={dynoRpm} step={100} min={800} max={maxRpm} onChange={setDynoRpm} />
                <button className="small" onClick={() => control({ action: "dyno_target", target_rpm: dynoRpm })}>Set</button>
              </label>}
              <label className="f inline">Speed
                <select value={live.status?.speed ?? 1} onChange={(e) => control({ action: "speed", value: Number(e.target.value) })}>
                  {[0.25, 0.5, 1, 2, 4].map((s) => <option key={s} value={s}>{s}×</option>)}
                </select>
              </label>
            </div>
          </div>
        )}
      </Card>

      <div className="grid g4">
        <Card className="tight"><Gauge label="Engine speed" value={ch.rpm} min={0} max={maxRpm} unit="rpm" warn={maxRpm - 600} danger={maxRpm - 200} /></Card>
        <Card className="tight"><Gauge label="Boost (abs)" value={ch.boost_kpa} min={60} max={300} unit="kPa" /></Card>
        <Card className="tight"><Gauge label="Brake torque" value={ch.torque_nm} min={0} max={project?.config?.reference_ratings?.torque_nm ? project.config.reference_ratings.torque_nm * 1.4 : 700} unit="N·m" /></Card>
        <Card className="tight"><Gauge label="Lambda" value={ch.lambda} min={0.7} max={1.3} unit="λ" digits={3} /></Card>
      </div>
      <div className="grid g6">
        <Stat label="Power" value={ch.power_kw} unit="kW" />
        <Stat label="MAP" value={ch.map_kpa} unit="kPa" />
        <Stat label="Throttle" value={ch.throttle_pct} unit="%" />
        <Stat label="Wastegate" value={ch.wastegate_pct} unit="% open" />
        <Stat label="Turbo" value={ch.turbo_krpm} unit="krpm" digits={0} />
        <Stat label="Spark" value={ch.spark_deg} unit="° BTDC" />
        <Stat label="Knock retard" value={ch.knock_retard_deg} unit="°" tone={(ch.knock_retard_deg ?? 0) > 0.5 ? "#ffb703" : undefined} />
        <Stat label="Charge air" value={ch.iat_c} unit="°C" />
        <Stat label="Coolant" value={ch.coolant_c} unit="°C" />
        <Stat label="Oil" value={ch.oil_c} unit="°C" />
        <Stat label="EGT" value={ch.egt_c} unit="°C" digits={0} />
        <Stat label="Fuel" value={ch.fuel_lph} unit="L/h" />
        <Stat label="STFT / LTFT" value={`${fmt(ch.stft_pct)} / ${fmt(ch.ltft_pct)}`} unit="%" />
        <Stat label="Torque req / limit" value={`${fmt(ch.torque_request_nm, 0)} / ${fmt(ch.torque_limit_nm, 0)}`} unit="N·m" />
        <Stat label="Idle target" value={ch.idle_target_rpm} unit="rpm" digits={0} />
        <Stat label="Vehicle speed" value={ch.vss_kmh} unit="km/h" />
        <Stat label="VE" value={ch.ve_pct} unit="%" />
        <Stat label="Peak pressure (est.)" value={ch.peak_pressure_bar} unit="bar" digits={0} />
      </div>
      <div className="grid g2">
        <Card title="Speed and air path (last 30 s)">
          <Plot data={chart} height={280} layout={{ yaxis: { title: { text: "rpm" } }, yaxis2: { title: { text: "kPa" }, overlaying: "y", side: "right" }, xaxis: { title: { text: "simulation time (s)" } } }} />
        </Card>
        <Card title="Fuel and spark (last 30 s)">
          <Plot data={chart2} height={280} layout={{ yaxis: { title: { text: "λ" }, range: [0.65, 1.45] }, yaxis2: { title: { text: "°" }, overlaying: "y", side: "right" }, xaxis: { title: { text: "simulation time (s)" } } }} />
        </Card>
      </div>
      <div className="grid g2">
        <Card title="Workflow (10 steps)">
          <ol className="steps">
            {WORKFLOW.map(([text, page], i) => (
              <li key={i}><span className={done[i] ? "check-ok" : "muted"}>{done[i] ? "✔" : "○"}</span>{" "}
                <a href={`#/${page}`} onClick={(e) => { e.preventDefault(); go(page); }}>{text}</a></li>
            ))}
          </ol>
        </Card>
        <Card title="Events">
          <div className="scroll" style={{ maxHeight: 260 }}>
            <table className="t"><tbody>
              {[...live.events].reverse().map((e, i) => <tr key={i}><td className="num mono">{fmt(e.t, 2)} s</td><td>{e.text}</td></tr>)}
            </tbody></table>
            {!live.events.length && <small className="muted">No events yet.</small>}
          </div>
        </Card>
      </div>
    </div>
  );
}
