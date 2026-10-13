"""Connection and application settings."""

from __future__ import annotations

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from opentune import __version__
from opentune.ecu.safety import REAL_ECU_WRITE_ENABLED
from opentune.engines import ENGINES
from opentune.gui import theme
from opentune.gui.context import AppContext
from opentune.obd.transport import list_serial_ports
from opentune.telemetry.source import TelemetrySource

CONNECTIONS = (
    ("simulator", "Simulator (direct, all channels)"),
    ("simulator-obd", "Simulator through ELM327 emulation (tests the real OBD-II stack)"),
    ("elm327", "ELM327 / OBDLink adapter on a real car (read-only)"),
)


class ConnectionPage(QWidget):
    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        s = ctx.settings
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        title = QLabel("Connection & Settings")
        title.setProperty("role", "title")
        lay.addWidget(title)

        conn = QGroupBox("Vehicle connection")
        form = QFormLayout(conn)
        self.engine_combo = QComboBox()
        for code, profile in ENGINES.items():
            self.engine_combo.addItem(f"{code} — {profile.family_name}", code)
        self.engine_combo.setCurrentIndex(max(0, self.engine_combo.findData(s.engine)))
        form.addRow("Engine", self.engine_combo)
        self.kind_group = QButtonGroup(self)
        kind_box = QVBoxLayout()
        self.kind_buttons: dict[str, QRadioButton] = {}
        for i, (key, label) in enumerate(CONNECTIONS):
            rb = QRadioButton(label)
            self.kind_group.addButton(rb, i)
            self.kind_buttons[key] = rb
            kind_box.addWidget(rb)
        self.kind_buttons.get(s.connection, self.kind_buttons["simulator"]).setChecked(True)
        form.addRow("Source", kind_box)
        port_row = QHBoxLayout()
        self.port_combo = QComboBox()
        self.port_combo.setEditable(True)
        self.port_combo.setMinimumWidth(280)
        self.refresh_ports_btn = QPushButton("Refresh ports")
        port_row.addWidget(self.port_combo, 1)
        port_row.addWidget(self.refresh_ports_btn)
        form.addRow("Serial port", port_row)
        self.baud_combo = QComboBox()
        for baud in (38400, 9600, 115200, 500000):
            self.baud_combo.addItem(str(baud), baud)
        self.baud_combo.setCurrentIndex(max(0, self.baud_combo.findData(s.serial_baudrate)))
        form.addRow("Baud rate", self.baud_combo)
        self.rate_spin = QDoubleSpinBox()
        self.rate_spin.setRange(1, 50)
        self.rate_spin.setValue(s.sample_rate_hz)
        self.rate_spin.setSuffix(" Hz")
        form.addRow("Sample rate", self.rate_spin)
        buttons = QHBoxLayout()
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setProperty("role", "primary")
        self.disconnect_btn = QPushButton("Disconnect")
        buttons.addWidget(self.connect_btn)
        buttons.addWidget(self.disconnect_btn)
        buttons.addStretch(1)
        form.addRow(buttons)
        self.status = QLabel("Not connected")
        self.status.setWordWrap(True)
        form.addRow("Status", self.status)
        lay.addWidget(conn)

        hint = QLabel("macOS: USB adapters appear as /dev/cu.usbserial-* or /dev/cu.SLAB_USBtoUART. "
                      "Bluetooth adapters must be paired first and appear as /dev/cu.<name>. Real-car "
                      "connections are read-only: live data, DTCs and VIN via generic OBD-II.")
        hint.setWordWrap(True)
        hint.setProperty("role", "banner-info")
        lay.addWidget(hint)

        about = QGroupBox("About")
        af = QFormLayout(about)
        af.addRow("Version", QLabel(__version__))
        write = QLabel("DISABLED (hard-coded)" if not REAL_ECU_WRITE_ENABLED else "ENABLED")
        write.setStyleSheet(f"color: {theme.CRIT}; font-weight: 600;")
        af.addRow("Real ECU writing", write)
        data_row = QHBoxLayout()
        data_row.addWidget(QLabel(str(ctx.paths.root)), 1)
        open_btn = QPushButton("Open")
        open_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(ctx.paths.root))))
        data_row.addWidget(open_btn)
        af.addRow("Data folder", data_row)
        af.addRow("Cloud services", QLabel("None required. No account, no subscription."))
        lay.addWidget(about)
        lay.addStretch(1)

        self.refresh_ports_btn.clicked.connect(self.refresh_ports)
        self.connect_btn.clicked.connect(self.connect_clicked)
        self.disconnect_btn.clicked.connect(self.ctx.controller.disconnect_source)
        self.kind_group.idClicked.connect(lambda _i: self._update_enabled())
        ctx.controller.connected.connect(self._connected)
        self._last_error = ""
        ctx.controller.disconnected.connect(self._disconnected)
        ctx.controller.error.connect(self._error)
        self.refresh_ports()
        if s.serial_port:
            self.port_combo.setCurrentText(s.serial_port)
        self._update_enabled()

    def selected_kind(self) -> str:
        for key, rb in self.kind_buttons.items():
            if rb.isChecked():
                return key
        return "simulator"

    def _update_enabled(self) -> None:
        real = self.selected_kind() == "elm327"
        for w in (self.port_combo, self.refresh_ports_btn, self.baud_combo):
            w.setEnabled(real)

    def refresh_ports(self) -> None:
        current = self.port_combo.currentText()
        self.port_combo.clear()
        ports = list_serial_ports()
        self.port_combo.addItems(ports)
        if current:
            self.port_combo.setCurrentText(current)

    def apply_to_settings(self) -> None:
        s = self.ctx.settings
        s.engine = self.engine_combo.currentData()
        s.connection = self.selected_kind()
        s.serial_port = self.port_combo.currentText().strip()
        s.serial_baudrate = int(self.baud_combo.currentData())
        s.sample_rate_hz = float(self.rate_spin.value())
        self.ctx.save_settings()

    def connect_clicked(self) -> None:
        self.apply_to_settings()
        if self.ctx.settings.connection == "elm327" and not self.ctx.settings.serial_port:
            self.status.setText("Choose a serial port for the adapter.")
            return
        self._last_error = ""
        self.status.setText("Connecting…")
        try:
            self.ctx.connect()
        except (ValueError, OSError) as exc:
            self.status.setText(f"Connection failed: {exc}")

    def _connected(self, source: TelemetrySource) -> None:
        self.status.setText(f"Connected: {source.label} · {source.profile.code} · "
                            f"{len(self.ctx.controller.channels)} channels")

    def _error(self, message: str) -> None:
        self._last_error = message
        self.status.setText(message)

    def _disconnected(self) -> None:
        self.status.setText(f"Disconnected — {self._last_error}" if self._last_error else "Disconnected")
