import { createContext, ReactNode, useCallback, useContext, useEffect, useState } from "react";
import { api, Project } from "./api";

interface Ctx {
  projects: Project[];
  project: Project | null;
  setProjectId: (id: string) => void;
  refreshProjects: () => Promise<void>;
  page: string;
  go: (page: string, params?: Record<string, string>) => void;
  params: Record<string, string>;
  system: any;
}

const LabCtx = createContext<Ctx>(null as any);
export const useLab = () => useContext(LabCtx);

function parseHash(): { page: string; params: Record<string, string> } {
  const h = location.hash.replace(/^#\/?/, "");
  const [page, query] = h.split("?");
  const params: Record<string, string> = {};
  new URLSearchParams(query || "").forEach((v, k) => (params[k] = v));
  return { page: page || "dashboard", params };
}

export function LabProvider({ children }: { children: ReactNode }) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectIdState] = useState<string | null>(() => localStorage.getItem("ot.project"));
  const [{ page, params }, setRoute] = useState(parseHash);
  const [system, setSystem] = useState<any>(null);

  const refreshProjects = useCallback(async () => {
    const ps = await api.get<Project[]>("/api/projects");
    setProjects(ps);
    setProjectIdState((cur) => (cur && ps.some((p) => p.id === cur) ? cur : ps[0]?.id ?? null));
  }, []);
  useEffect(() => {
    refreshProjects();
    api.get("/api/system").then(setSystem).catch(() => {});
    const onHash = () => setRoute(parseHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, [refreshProjects]);

  const setProjectId = (id: string) => {
    setProjectIdState(id);
    try { localStorage.setItem("ot.project", id); } catch { /* private mode */ }
  };
  const go = (p: string, prm?: Record<string, string>) => {
    const q = prm ? "?" + new URLSearchParams(prm).toString() : "";
    location.hash = `/${p}${q}`;
  };
  const project = projects.find((p) => p.id === projectId) ?? null;
  return (
    <LabCtx.Provider value={{ projects, project, setProjectId, refreshProjects, page, go, params, system }}>
      {children}
    </LabCtx.Provider>
  );
}
