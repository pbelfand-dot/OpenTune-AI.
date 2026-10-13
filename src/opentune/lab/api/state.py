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
            self.library.create_project("B48-inspired (G20 330i-class) demo", "b48-inspired",
                                        notes="Synthetic model; not BMW factory data.")
            self.library.create_project("B58-inspired (G20 M340i-class) demo", "b58-inspired",
                                        notes="Synthetic model; not BMW factory data.")

    def shutdown(self) -> None:
        self.replace_live(None)
        self.jobs.shutdown()
