"""Shared server state: project library, calibration working copies, jobs, live session."""

from __future__ import annotations

import threading
from pathlib import Path

from opentune.lab.api.jobs import JobManager
from opentune.lab.api.live import LiveSession
from opentune.lab.calibration.editor import CalibrationEditor
from opentune.lab.storage.library import ProjectLibrary


class LabState:
    def __init__(self, data_dir: Path, job_workers: int = 2) -> None:
        self.root = Path(data_dir) / "lab"
        self.root.mkdir(parents=True, exist_ok=True)
        self.library = ProjectLibrary(self.root / "lab.sqlite")
        self.jobs = JobManager(job_workers)
        self.lock = threading.RLock()
        self._editors: dict[str, tuple[CalibrationEditor, str]] = {}
        self.live: LiveSession | None = None
        self._knowledge = None

    @property
    def knowledge(self):
        """Tuning-engine knowledge base (experiments, mistakes, benchmarks), opened on first use."""
        with self.lock:
            if self._knowledge is None:
                from opentune.lab.tuning.knowledge import KnowledgeBase

                self._knowledge = KnowledgeBase(self.root / "tuning.sqlite")
            return self._knowledge

    # ------------------------------------------------------------------ calibration working copies
    def editor(self, project_id: str) -> CalibrationEditor:
        with self.lock:
            entry = self._editors.get(project_id)
            if entry is None:
                project = self.library.get_project(project_id)
                cal = self.library.calibrations.load(project.active_version_id)
                entry = (CalibrationEditor(cal), project.active_version_id)
                self._editors[project_id] = entry
            return entry[0]

    def editor_version(self, project_id: str) -> str:
        self.editor(project_id)
        return self._editors[project_id][1]

    def checkout(self, project_id: str, version_id: str) -> CalibrationEditor:
        with self.lock:
            cal = self.library.load_calibration(project_id, "version", version_id)
            editor = CalibrationEditor(cal)
            self._editors[project_id] = (editor, version_id)
            return editor

    def set_editor_version(self, project_id: str, version_id: str) -> None:
        with self.lock:
            editor = self.editor(project_id)
            self._editors[project_id] = (editor, version_id)

    def drop_editor(self, project_id: str) -> None:
        with self.lock:
            self._editors.pop(project_id, None)

    def is_dirty(self, project_id: str) -> bool:
        editor = self.editor(project_id)
        saved = self.library.calibrations.info(self.editor_version(project_id))
        return editor.calibration.content_hash() != saved.content_hash

    # ------------------------------------------------------------------ virtual ECU devices
    def device_path(self, project_id: str) -> Path:
        return self.root / "ecu" / f"{project_id}.json"

    def backup_dir(self, project_id: str) -> Path:
        return self.root / "ecu" / "backups" / project_id

    def device(self, project_id: str):
        """The project's virtual ECU (created on first use with the baseline factory-programmed)."""
        import json

        from opentune.lab.flash import VirtualEcuDevice, build_image

        with self.lock:
            path = self.device_path(project_id)
            if path.exists():
                return VirtualEcuDevice.from_dict(json.loads(path.read_text()))
            project = self.library.get_project(project_id)
            info = self.library.calibrations.lineage_baseline(project.calibration_lineage)
            vid = info.id if info else project.active_version_id
            dev = VirtualEcuDevice(project.config.get("id", "custom"))
            dev.factory_program(build_image(self.library.calibrations.load(vid)))
            self.save_device(project_id, dev)
            self.library.update_project(project_id, flashed_version_id=vid)
            return dev

    def save_device(self, project_id: str, dev) -> None:
        import json

        path = self.device_path(project_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(dev.to_dict()))
        tmp.replace(path)

    def version_for_hash(self, project_id: str, content_hash: str) -> str | None:
        project = self.library.get_project(project_id)
        for v in reversed(self.library.calibrations.history(project.calibration_lineage)):
            if v.content_hash == content_hash:
                return v.id
        return None

    # ------------------------------------------------------------------ live session
    def replace_live(self, session: LiveSession | None) -> None:
        with self.lock:
            old, self.live = self.live, session
        if old is not None:
            old.close()
        if session is not None:
            session.start_thread()

    def ensure_demo_projects(self) -> None:
        """First start: create one project per preset so the UI has something to show."""
        if not self.library.list_projects():
            for name, preset in (("B48-inspired (G20 330i-class) demo", "b48-inspired"),
                                 ("B58-inspired (G20 M340i-class) demo", "b58-inspired")):
                p = self.library.create_project(name, preset, notes="Synthetic model; not BMW factory data.")
                self.device(p.id)

    def shutdown(self) -> None:
        self.replace_live(None)
        self.jobs.shutdown()
        if self._knowledge is not None:
            self._knowledge.close()
