// Thin typed wrapper around the lab's REST API (same origin).

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  let data: any = text;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    /* plain text */
  }
  if (!res.ok) {
    let msg = typeof data === "object" && data && "detail" in data ? data.detail : text;
    if (Array.isArray(msg)) msg = msg.map((d: any) => `${(d.loc || []).slice(1).join(".")}: ${d.msg}`).join("; ");
    throw new ApiError(res.status, String(msg || res.statusText));
  }
  return data as T;
}

export const api = {
  get: <T = any>(p: string) => request<T>("GET", p),
  post: <T = any>(p: string, b: unknown = {}) => request<T>("POST", p, b),
  put: <T = any>(p: string, b: unknown) => request<T>("PUT", p, b),
  patch: <T = any>(p: string, b: unknown) => request<T>("PATCH", p, b),
  del: <T = any>(p: string) => request<T>("DELETE", p),
};

export interface Project {
  id: string;
  name: string;
  preset_id: string;
  config: any;
  notes: string;
  engine_name: string;
  engine_id: string;
  calibration_lineage: string;
  active_version_id: string;
  flashed_version_id: string | null;
  active_version: VersionInfo;
  flashed_version: VersionInfo | null;
  flashed_unknown: boolean;
  live: boolean;
  created_utc: string;
  updated_utc: string;
}

export interface VersionInfo {
  id: string;
  lineage: string;
  name: string;
  engine_id: string;
  version: number;
  parent_id: string | null;
  created_utc: string;
  note: string;
  is_baseline: boolean;
  content_hash: string;
  active?: boolean;
  flashed?: boolean;
  checked_out?: boolean;
}

export interface Job {
  id: string;
  kind: string;
  status: "queued" | "running" | "done" | "failed" | "cancelled";
  progress: number;
  message: string;
  error: string | null;
  result: any;
}

export interface LiveFrame {
  type: "frame";
  t: number;
  ch: Record<string, number | null>;
  ecu: { state: string; mode: string; mil: boolean; limp: boolean; dtcs: string[]; protections: string[];
         closed_loop: boolean; torque_limited_by: string };
  driver: { ignition: boolean; pedal: number; starting: boolean };
  load: string;
}

export function fmt(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  return v.toFixed(digits);
}

export const PROVENANCE: Record<string, { label: string; color: string }> = {
  measured: { label: "Measured", color: "#2ec4b6" },
  documented: { label: "Documented", color: "#4cc9f0" },
  simulated: { label: "Simulated", color: "#b5e48c" },
  assumption: { label: "Assumption", color: "#ffb703" },
  speculative: { label: "Speculative", color: "#f72585" },
  "rule-based": { label: "Rule-based", color: "#9d4edd" },
};
