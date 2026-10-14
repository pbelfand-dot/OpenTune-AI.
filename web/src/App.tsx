import type { ComponentType } from "react";
import { useLab } from "./context";
import Dashboard from "./pages/Dashboard";
import EngineBuilder from "./pages/EngineBuilder";
import EcuTuning from "./pages/EcuTuning";
import Dyno from "./pages/Dyno";
import ControlSystems from "./pages/ControlSystems";
import Diagnostics from "./pages/Diagnostics";
import AiEngineer from "./pages/AiEngineer";
import TestLab from "./pages/TestLab";
import Library from "./pages/Library";

const PAGES: { id: string; label: string; ico: string; el: ComponentType }[] = [
  { id: "dashboard", label: "Dashboard", ico: "◉", el: Dashboard },
  { id: "engine", label: "Engine Builder", ico: "⚙", el: EngineBuilder },
  { id: "tuning", label: "ECU Tuning", ico: "▦", el: EcuTuning },
  { id: "dyno", label: "Virtual Dyno", ico: "📈", el: Dyno },
  { id: "control", label: "Control Systems", ico: "∿", el: ControlSystems },
  { id: "diagnostics", label: "Engine Diagnostics", ico: "⚠", el: Diagnostics },
  { id: "ai", label: "AI Engineer", ico: "✦", el: AiEngineer },
  { id: "tests", label: "Test Laboratory", ico: "⚗", el: TestLab },
  { id: "library", label: "Project Library", ico: "▤", el: Library },
];

export default function App() {
  const { page, go, projects, project, setProjectId, system } = useLab();
  const current = PAGES.find((p) => p.id === page) ?? PAGES[0];
  const Page = current.el;
  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <b>OpenTune</b> Virtual Engine Lab
          <small className="muted">Simulation only · no hardware access</small>
        </div>
        <nav className="nav">
          {PAGES.map((p) => (
            <button key={p.id} className={p.id === current.id ? "active" : ""} onClick={() => go(p.id)}>
              <span className="ico">{p.ico}</span>{p.label}
            </button>
          ))}
        </nav>
        <div className="foot">
          {system ? <>Lab {system.lab_version} · Python {system.python}<br />Real ECU writes: <b style={{ color: "#52d273" }}>{system.safety.real_ecu_write_enabled ? "ENABLED" : "disabled"}</b></> : "connecting…"}
        </div>
      </aside>
      <header className="topbar">
        <h2 style={{ margin: 0 }}>{current.label}</h2>
        <span className="spacer" />
        <label className="f inline">Project
          <select value={project?.id ?? ""} onChange={(e) => setProjectId(e.target.value)} style={{ maxWidth: 340 }}>
            {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
        </label>
        {project && <span className="chip acc" title="Calibration version currently in the virtual ECU">
          ECU: {project.flashed_version ? `v${project.flashed_version.version}` : project.flashed_unknown ? "unsaved image" : "–"}
          {" · "}active v{project.active_version.version}</span>}
      </header>
      <main className="main">
        {project || current.id === "library" ? <Page key={current.id + (project?.id ?? "")} /> :
          <div className="muted">Loading projects… If this persists, create one in the Project Library.</div>}
      </main>
    </div>
  );
}
