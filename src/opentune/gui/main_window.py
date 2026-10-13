"""Main application window: sidebar navigation + stacked pages + status bar."""

from __future__ import annotations

from PySide6.QtCore import QSize, QTimer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from opentune import __app_name__, __version__
from opentune.ecu.safety import REAL_ECU_WRITE_ENABLED
from opentune.gui.context import AppContext
from opentune.gui.pages.analysis import AnalysisPage
from opentune.gui.pages.calibrations import CalibrationsPage
from opentune.gui.pages.connection import ConnectionPage
from opentune.gui.pages.dashboard import DashboardPage
from opentune.gui.pages.diagnostics import DiagnosticsPage
from opentune.gui.pages.logs import LogsPage
from opentune.gui.pages.programming import ProgrammingPage
from opentune.telemetry.source import TelemetrySource

NAV = (
    ("dashboard", "◉  Dashboard"),
    ("diagnostics", "⚠  Diagnostics"),
    ("logs", "●  Data Logging"),
    ("calibrations", "▤  Calibrations"),
    ("analysis", "✦  AI Analysis"),
    ("programming", "⇪  ECU Programming"),
    ("connection", "⚙  Connection"),
)


class MainWindow(QMainWindow):
    def __init__(self, ctx: AppContext, auto_connect: bool = True, scenario: str = "city") -> None:
        super().__init__()
        self.ctx = ctx
        self.setWindowTitle(f"{__app_name__} {__version__}")
        self.resize(1440, 900)
        self.setMinimumSize(QSize(1100, 720))

        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        side = QWidget()
        side.setFixedWidth(220)
        side_lay = QVBoxLayout(side)
        side_lay.setContentsMargins(0, 0, 0, 0)
        brand = QLabel(f"OpenTune <span style='color:#3fa9f5'>AI</span><br>"
                       f"<span style='font-size:11px;color:#8a93a6'>v{__version__} · BMW B58 / B48</span>")
        brand.setStyleSheet("font-size: 20px; font-weight: 700; padding: 18px 18px 8px 18px;"
                            "background-color: #151820;")
        side_lay.addWidget(brand)
        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        for key, label in NAV:
            item = QListWidgetItem(label)
            item.setData(256, key)
            self.nav.addItem(item)
        side_lay.addWidget(self.nav, 1)
        root.addWidget(side)

        self.stack = QStackedWidget()
        self.pages: dict[str, QWidget] = {
            "dashboard": DashboardPage(ctx),
            "diagnostics": DiagnosticsPage(ctx),
            "logs": LogsPage(ctx),
            "calibrations": CalibrationsPage(ctx),
            "analysis": AnalysisPage(ctx),
            "programming": ProgrammingPage(ctx),
            "connection": ConnectionPage(ctx),
        }
        for key, _ in NAV:
            self.stack.addWidget(self.pages[key])
        root.addWidget(self.stack, 1)
        self.setCentralWidget(central)
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.setCurrentRow(0)

        bar = self.statusBar()
        self.conn_label = QLabel("Not connected")
        self.rate_label = QLabel("")
        self.log_label = QLabel("")
        badge = QLabel("ECU WRITE DISABLED" if not REAL_ECU_WRITE_ENABLED else "ECU WRITE ENABLED")
        badge.setProperty("role", "badge-danger")
        bar.addWidget(self.conn_label)
        bar.addWidget(self.rate_label)
        bar.addWidget(self.log_label)
        bar.addPermanentWidget(badge)

        ctx.controller.connected.connect(self._connected)
        ctx.controller.disconnected.connect(lambda: self.conn_label.setText("Not connected"))
        ctx.controller.error.connect(self._error)
        ctx.controller.logging_changed.connect(
            lambda on, path: self.log_label.setText("● REC" if on else ""))
        ctx.analyze_requested.connect(lambda _p: self.show_page("analysis"))
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)
        if auto_connect:
            QTimer.singleShot(0, lambda: ctx.connect(scenario))

    def show_page(self, key: str) -> None:
        self.nav.setCurrentRow([k for k, _ in NAV].index(key))

    def _connected(self, source: TelemetrySource) -> None:
        self.conn_label.setText(f"● {source.label} · {source.profile.code}")

    def _error(self, message: str) -> None:
        self.statusBar().showMessage(message, 10000)
        if message.startswith(("Connection failed", "Telemetry error")):
            self.ctx.controller.disconnect_source()

    def _tick(self) -> None:
        if self.ctx.controller.is_connected:
            self.rate_label.setText(f"{self.ctx.controller.sample_rate:.0f} Hz")
        else:
            self.rate_label.setText("")

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.ctx.shutdown()
        super().closeEvent(event)
