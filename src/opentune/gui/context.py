"""Shared application state passed to every page."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from opentune.calibration.library import CalibrationLibrary
from opentune.config import AppPaths, Settings
from opentune.engines import get_profile
from opentune.engines.profile import EngineProfile
from opentune.gui.controller import TelemetryController
from opentune.telemetry.source import create_source


class AppContext(QObject):
    settings_changed = Signal()
    library_changed = Signal()
    logs_changed = Signal()
    analyze_requested = Signal(str)  # log path

    def __init__(self, paths: AppPaths, settings: Settings | None = None,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.paths = paths.ensure()
        self.settings = settings or Settings.load(paths.settings_file)
        self.library = CalibrationLibrary(paths.calibrations)
        self.controller = TelemetryController(self)

    @property
    def profile(self) -> EngineProfile:
        return get_profile(self.settings.engine)

    def save_settings(self) -> None:
        self.settings.save(self.paths.settings_file)
        self.settings_changed.emit()

    def connect(self, scenario: str = "city") -> None:
        s = self.settings
        source = create_source(s.connection, self.profile, s.serial_port, s.serial_baudrate, scenario)
        rate = s.sample_rate_hz if s.connection == "simulator" else min(s.sample_rate_hz, 10.0)
        self.controller.connect_source(source, rate)

    def shutdown(self) -> None:
        self.controller.disconnect_source()
