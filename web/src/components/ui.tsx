import { createContext, ReactNode, useCallback, useContext, useEffect, useRef, useState } from "react";
import { fmt, Job, PROVENANCE } from "../api";

declare const Plotly: any;

// ------------------------------------------------------------------ Plotly
const DARK_LAYOUT = {
  paper_bgcolor: "rgba(0,0,0,0)",
  plot_bgcolor: "rgba(0,0,0,0)",
  font: { color: "#d8dee9", size: 11 },
  margin: { l: 55, r: 55, t: 30, b: 40 },
  xaxis: { gridcolor: "#232a38", zerolinecolor: "#2c3546" },
  yaxis: { gridcolor: "#232a38", zerolinecolor: "#2c3546" },
  legend: { orientation: "h", y: -0.2 },
};

export function Plot({ data, layout = {}, height = 320, className = "" }: {
  data: any[]; layout?: any; height?: number; className?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!ref.current || typeof Plotly === "undefined") return;
    const merged = {
      ...DARK_LAYOUT, ...layout, height,
      xaxis: { ...DARK_LAYOUT.xaxis, ...(layout.xaxis || {}) },
      yaxis: { ...DARK_LAYOUT.yaxis, ...(layout.yaxis || {}) },
      ...(layout.yaxis2 ? { yaxis2: { gridcolor: "#232a38", ...layout.yaxis2 } } : {}),
    };
    Plotly.react(ref.current, data, merged, { responsive: true, displaylogo: false });
  }, [data, layout, height]);
  useEffect(() => () => {
    if (ref.current && typeof Plotly !== "undefined") Plotly.purge(ref.current);
  }, []);
  if (typeof Plotly === "undefined")
    return <div className="muted">Charts unavailable: plotly.js not loaded (install the <code>lab</code> extra).</div>;
  return <div ref={ref} className={`plot ${className}`} style={{ height }} />;
}

// ------------------------------------------------------------------ gauge
export function Gauge({ label, value, min, max, unit, warn, danger, digits = 0 }: {
  label: string; value: number | null | undefined; min: number; max: number; unit: string;
  warn?: number; danger?: number; digits?: number;
}) {
  const v = value ?? min;
  const frac = Math.min(1, Math.max(0, (v - min) / (max - min)));
  const a0 = Math.PI * 0.75, a1 = Math.PI * 2.25;
  const ang = a0 + (a1 - a0) * frac;
  const R = 52, cx = 64, cy = 64;
  const arc = (from: number, to: number) => {
    const x0 = cx + R * Math.cos(from), y0 = cy + R * Math.sin(from);
    const x1 = cx + R * Math.cos(to), y1 = cy + R * Math.sin(to);
    return `M ${x0} ${y0} A ${R} ${R} 0 ${to - from > Math.PI ? 1 : 0} 1 ${x1} ${y1}`;
  };
  const color = danger !== undefined && v >= danger ? "#ff4d6d" : warn !== undefined && v >= warn ? "#ffb703" : "#4cc9f0";
  return (
    <svg viewBox="0 0 128 112" className="gauge" style={{ width: "100%", maxWidth: 190 }}>
      <path d={arc(a0, a1)} stroke="#222a39" strokeWidth={10} fill="none" strokeLinecap="round" />
      {frac > 0.001 && <path d={arc(a0, ang)} stroke={color} strokeWidth={10} fill="none" strokeLinecap="round" />}
      <text x={64} y={62} textAnchor="middle" fontSize={20} fontWeight={600}>{fmt(value, digits)}</text>
      <text x={64} y={78} textAnchor="middle" fontSize={9} style={{ fill: "#8b95a7" }}>{unit}</text>
      <text x={64} y={104} textAnchor="middle" fontSize={10} style={{ fill: "#8b95a7" }}>{label}</text>
    </svg>
  );
}

export function Stat({ label, value, unit, digits = 1, tone }: {
  label: string; value: number | string | null | undefined; unit?: string; digits?: number; tone?: string;
}) {
  const shown = typeof value === "number" ? fmt(value, digits) : value ?? "–";
  return (
    <div className="stat">
      <div className="l">{label}</div>
      <div className="v" style={tone ? { color: tone } : undefined}>{shown}{unit && <span className="u">{unit}</span>}</div>
    </div>
  );
}

export function Card({ title, actions, children, className = "" }: {
  title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string;
}) {
  return (
    <section className={`card ${className}`}>
      {(title || actions) && (
        <div className="card-h">{title && <h3>{title}</h3>}<span className="spacer" />{actions}</div>
      )}
      {children}
    </section>
  );
}

export function Prov({ kind }: { kind: string }) {
  const p = PROVENANCE[kind] || { label: kind, color: "#888" };
  return <span className="chip" style={{ borderColor: p.color, color: p.color }} title={`Evidence category: ${p.label}`}>
    <span className="dot" style={{ background: p.color }} />{p.label}</span>;
}

export function JobBar({ job, onCancel }: { job: Job | null; onCancel?: () => void }) {
  if (!job) return null;
  const running = ["queued", "running"].includes(job.status);
  return (
    <div className="col" style={{ gap: ".3rem" }}>
      <div className="row">
        <span className={`chip ${job.status === "done" ? "ok" : job.status === "failed" ? "bad" : "acc"}`}>{job.kind}: {job.status}</span>
        <small className="muted">{job.message?.split("\n")[0]}</small>
        <span className="spacer" />
        {running && onCancel && <button className="small danger" onClick={onCancel}>Cancel</button>}
      </div>
      {running && <div className="progress"><div style={{ width: `${Math.round(job.progress * 100)}%` }} /></div>}
      {job.error && <div className="chip bad" style={{ whiteSpace: "normal" }}>{job.error}</div>}
    </div>
  );
}

export function Field({ label, children, inline = false, title }: { label: string; children: ReactNode; inline?: boolean; title?: string }) {
  return <label className={`f ${inline ? "inline" : ""}`} title={title}>{label}{children}</label>;
}

export function Num({ value, onChange, step, min, max, width }: {
  value: number | null | undefined; onChange: (v: number) => void; step?: number; min?: number; max?: number; width?: string;
}) {
  const [text, setText] = useState(value === null || value === undefined ? "" : String(value));
  useEffect(() => setText(value === null || value === undefined ? "" : String(value)), [value]);
  return <input type="number" value={text} step={step ?? "any"} min={min} max={max} style={width ? { width } : undefined}
    onChange={(e) => { setText(e.target.value); const v = parseFloat(e.target.value); if (!Number.isNaN(v)) onChange(v); }} />;
}

// ------------------------------------------------------------------ toasts
type Toast = { id: number; text: string; kind: "ok" | "err" | "info" };
const ToastCtx = createContext<(text: string, kind?: Toast["kind"]) => void>(() => {});
export const useToast = () => useContext(ToastCtx);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const push = useCallback((text: string, kind: Toast["kind"] = "info") => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t.slice(-4), { id, text, kind }]);
    window.setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), kind === "err" ? 7000 : 3500);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="toast-wrap">{toasts.map((t) => <div key={t.id} className={`toast ${t.kind}`}>{t.text}</div>)}</div>
    </ToastCtx.Provider>
  );
}

/** Run an async action and report errors as toasts. */
export function useAction() {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const run = useCallback(async <T,>(fn: () => Promise<T>, ok?: string): Promise<T | undefined> => {
    setBusy(true);
    try {
      const r = await fn();
      if (ok) toast(ok, "ok");
      return r;
    } catch (e: any) {
      toast(e.message || String(e), "err");
      return undefined;
    } finally {
      setBusy(false);
    }
  }, [toast]);
  return { run, busy };
}

export function Disclaimer({ children }: { children?: ReactNode }) {
  return <div className="banner">{children ?? "Simulated results of a synthetic model. Not measurements, not predictions for a real engine, and not calibrations for a vehicle."}</div>;
}

export function Severity({ s }: { s: string }) {
  const cls = s === "critical" ? "bad" : s === "warning" ? "warn" : s === "notice" ? "acc" : "info";
  return <span className={`chip ${cls}`}>{s}</span>;
}

export function Json({ data }: { data: unknown }) {
  return <pre className="json">{JSON.stringify(data, null, 1)}</pre>;
}
