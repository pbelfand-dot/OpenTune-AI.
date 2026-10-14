import { useCallback, useEffect, useRef, useState } from "react";
import { api, Job, LiveFrame } from "./api";

/** Load data from the API; `reload()` refetches. */
export function useApi<T>(path: string | null, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const reload = useCallback(async () => {
    if (!path) return;
    setLoading(true);
    try {
      setData(await api.get<T>(path));
      setError(null);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, ...deps]);
  useEffect(() => {
    reload();
  }, [reload]);
  return { data, error, loading, reload, setData };
}

/** Poll a background job until it finishes. */
export function useJob(onDone?: (job: Job) => void) {
  const [job, setJob] = useState<Job | null>(null);
  const timer = useRef<number | null>(null);
  const doneRef = useRef(onDone);
  doneRef.current = onDone;
  const stop = () => {
    if (timer.current) window.clearInterval(timer.current);
    timer.current = null;
  };
  const start = useCallback((j: Job) => {
    stop();
    setJob(j);
    timer.current = window.setInterval(async () => {
      try {
        const next = await api.get<Job>(`/api/jobs/${j.id}`);
        setJob(next);
        if (!["queued", "running"].includes(next.status)) {
          stop();
          doneRef.current?.(next);
        }
      } catch {
        stop();
      }
    }, 250);
  }, []);
  const cancel = useCallback(async () => {
    if (job) await api.del(`/api/jobs/${job.id}`);
  }, [job]);
  useEffect(() => stop, []);
  const running = !!job && ["queued", "running"].includes(job.status);
  return { job, start, cancel, running, setJob };
}

/** WebSocket live stream: latest frame, a rolling history and status messages. */
export function useLive(historySeconds = 30) {
  const [frame, setFrame] = useState<LiveFrame | null>(null);
  const [status, setStatus] = useState<any>(null);
  const [connected, setConnected] = useState(false);
  const [events, setEvents] = useState<{ t: number; text: string }[]>([]);
  const history = useRef<LiveFrame[]>([]);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let ws: WebSocket | null = null;
    let closed = false;
    let retry: number | undefined;
    const connect = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${location.host}/ws/live`);
      ws.onopen = () => setConnected(true);
      ws.onclose = () => {
        setConnected(false);
        if (!closed) retry = window.setTimeout(connect, 1500);
      };
      ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data);
        if (msg.type === "frame") {
          const f = msg as LiveFrame;
          setFrame(f);
          const h = history.current;
          h.push(f);
          const cutoff = f.t - historySeconds;
          while (h.length && h[0].t < cutoff) h.shift();
          if (h.length > 1 && h[h.length - 2].t > f.t) history.current = [f]; // session restarted
          setTick((x) => (x + 1) % 1_000_000);
        } else if (msg.type === "status") {
          setStatus(msg);
          if (!msg.running) {
            setFrame(null);
            history.current = [];
          }
          if (msg.events) setEvents(msg.events);
        } else if (msg.type === "event") {
          setEvents((e) => [...e.slice(-49), { t: msg.t, text: msg.text }]);
        }
      };
    };
    connect();
    return () => {
      closed = true;
      if (retry) window.clearTimeout(retry);
      ws?.close();
    };
  }, [historySeconds]);
  return { frame, status, connected, events, history: history.current, tick };
}

export function useInterval(fn: () => void, ms: number | null) {
  const ref = useRef(fn);
  ref.current = fn;
  useEffect(() => {
    if (ms === null) return;
    const id = window.setInterval(() => ref.current(), ms);
    return () => window.clearInterval(id);
  }, [ms]);
}
