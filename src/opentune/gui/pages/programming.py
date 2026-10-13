"""Simulated ECU programming environment."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from opentune.ecu import ProgrammingResult, ProgrammingSession, SimulatedEcu, validate_image
from opentune.ecu.interface import EcuState
from opentune.ecu.safety import REAL_ECU_WRITE_ENABLED
from opentune.gui import theme
from opentune.gui.context import AppContext
from opentune.gui.controller import run_async
from opentune.simulation.vehicle import SimulatedVehicle


class ProgrammingPage(QWidget):
    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.ecu: SimulatedEcu | None = None
        self._standalone_vehicle: SimulatedVehicle | None = None
        self.last_result: ProgrammingResult | None = None
        self.busy = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        title = QLabel("ECU Programming (Simulated)")
        title.setProperty("role", "title")
        lay.addWidget(title)
        banner = QLabel(
            "SIMULATION ONLY — real ECU writing is "
            f"{'ENABLED' if REAL_ECU_WRITE_ENABLED else 'DISABLED'} in OpenTune AI v0.1. "
            "This environment programs an in-memory simulated ECU so flashing workflows "
            "(preflight, backup, transfer, verify, recovery) can be developed with zero risk.")
        banner.setProperty("role", "banner-danger")
        banner.setWordWrap(True)
        lay.addWidget(banner)

        top = QHBoxLayout()
        target_box = QGroupBox("Target")
        tf = QFormLayout(target_box)
        self.target_label = QLabel("–")
        self.software_label = QLabel("–")
        self.state_label = QLabel("–")
        self.voltage_label = QLabel("–")
        self.engine_btn = QPushButton("Switch engine off (ignition on)")
        self.identify_btn = QPushButton("Identify")
        tf.addRow("ECU", self.target_label)
        tf.addRow("Software", self.software_label)
        tf.addRow("State", self.state_label)
        tf.addRow("Supply voltage", self.voltage_label)
        row = QHBoxLayout()
        row.addWidget(self.identify_btn)
        row.addWidget(self.engine_btn)
        tf.addRow(row)
        top.addWidget(target_box, 1)

        image_box = QGroupBox("Image")
        imf = QFormLayout(image_box)
        self.image_combo = QComboBox()
        self.validate_btn = QPushButton("Validate")
        self.validation_label = QLabel("–")
        self.validation_label.setWordWrap(True)
        imf.addRow("Calibration", self.image_combo)
        imf.addRow(self.validate_btn)
        imf.addRow(self.validation_label)
        self.power_loss_check = QCheckBox("Simulate power loss at")
        self.power_loss_spin = QDoubleSpinBox()
        self.power_loss_spin.setRange(1, 99)
        self.power_loss_spin.setValue(50)
        self.power_loss_spin.setSuffix(" %")
        pl = QHBoxLayout()
        pl.addWidget(self.power_loss_check)
        pl.addWidget(self.power_loss_spin)
        imf.addRow(pl)
        self.low_voltage_check = QCheckBox("Simulate weak battery (11.9 V)")
        imf.addRow(self.low_voltage_check)
        top.addWidget(image_box, 1)
        lay.addLayout(top)

        actions = QHBoxLayout()
        self.flash_btn = QPushButton("Program simulated ECU")
        self.flash_btn.setProperty("role", "danger")
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        actions.addWidget(self.flash_btn)
        actions.addWidget(self.cancel_btn)
        actions.addStretch(1)
        lay.addLayout(actions)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        lay.addWidget(self.progress)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        lay.addWidget(self.log, 1)

        self.identify_btn.clicked.connect(self.refresh_target)
        self.engine_btn.clicked.connect(self._toggle_engine)
        self.validate_btn.clicked.connect(self.validate)
        self.flash_btn.clicked.connect(self.flash)
        self.cancel_btn.clicked.connect(self._cancel)
        ctx.library_changed.connect(self.refresh_images)
        ctx.controller.connected.connect(lambda _s: self._reset_target())
        ctx.settings_changed.connect(self.refresh_images)
        self._session: ProgrammingSession | None = None
        self._reset_target()

    # ------------------------------------------------------------------ target
    def vehicle(self) -> SimulatedVehicle:
        source = self.ctx.controller.source
        if source is not None and source.vehicle is not None and source.profile.code == self.ctx.profile.code:
            return source.vehicle
        if self._standalone_vehicle is None or self._standalone_vehicle.profile.code != self.ctx.profile.code:
            self._standalone_vehicle = SimulatedVehicle(self.ctx.profile, "idle")
        return self._standalone_vehicle

    def _reset_target(self) -> None:
        vehicle = self.vehicle()
        self.ecu = SimulatedEcu(vehicle.profile, vehicle)
        self.refresh_images()
        self.refresh_target()

    def refresh_target(self) -> None:
        if self.ecu is None:
            return
        ident = self.ecu.identify()
        self.target_label.setText(ident.target)
        self.software_label.setText(ident.software_version)
        color = theme.CRIT if ident.state is EcuState.RECOVERY else theme.OK
        self.state_label.setText(ident.state.value.upper())
        self.state_label.setStyleSheet(f"color: {color}; font-weight: 600;")
        self.voltage_label.setText(f"{self.ecu.battery_voltage():.2f} V")
        running = self.ecu.engine_running()
        self.engine_btn.setText("Switch engine off (ignition on)" if running else "Start engine")

    def _toggle_engine(self) -> None:
        vehicle = self.vehicle()
        vehicle.set_engine_running(not vehicle.engine_running)
        self.refresh_target()

    def refresh_images(self) -> None:
        current = self.image_combo.currentData()
        self.image_combo.clear()
        for rec in self.ctx.library.list(self.ctx.profile.code):
            if rec.format == "otsim":
                self.image_combo.addItem(f"{rec.name}  [{rec.software_version} · {rec.short_hash}]", rec.id)
        if self.image_combo.count() == 0:
            self.image_combo.addItem("(no simulator calibrations — create one on the Calibrations page)", None)
        idx = self.image_combo.findData(current)
        if idx >= 0:
            self.image_combo.setCurrentIndex(idx)

    # ------------------------------------------------------------------ actions
    def _image(self) -> bytes | None:
        cal_id = self.image_combo.currentData()
        return None if cal_id is None else self.ctx.library.read_bytes(cal_id)

    def validate(self) -> list[str]:
        image = self._image()
        if image is None:
            problems = ["No image selected"]
        else:
            problems = validate_image(image, self.ctx.profile.code)
            if self.ecu is not None:
                session = ProgrammingSession(self.ecu, engine=self.ctx.profile.code)
                problems = session.preflight(image) if not problems else problems
        if problems:
            self.validation_label.setText("✗ " + "\n✗ ".join(problems))
            self.validation_label.setStyleSheet(f"color: {theme.WARN};")
        else:
            self.validation_label.setText("✓ Image valid and preflight checks pass")
            self.validation_label.setStyleSheet(f"color: {theme.OK};")
        return problems

    def flash(self, confirm: bool = True) -> None:
        if self.busy or self.ecu is None:
            return
        image = self._image()
        if image is None:
            QMessageBox.information(self, "No image", "Create or select a simulator calibration first.")
            return
        if confirm and QMessageBox.question(
                self, "Program simulated ECU",
                f"Program '{self.image_combo.currentText()}' into the simulated {self.ctx.profile.code} ECU?\n\n"
                "The current calibration is backed up to the library first.") != QMessageBox.StandardButton.Yes:
            return
        self.ecu.fail_at_fraction = self.power_loss_spin.value() / 100.0 if self.power_loss_check.isChecked() else None
        self.ecu.supply_voltage_override = 11.9 if self.low_voltage_check.isChecked() else None
        self._session = ProgrammingSession(self.ecu, self.ctx.library, self.ctx.profile.code)
        self.busy = True
        self.flash_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.progress.setValue(0)
        self.log.clear()
        session = self._session

        def job(emit):
            return session.run(image, lambda frac, msg: emit(frac, msg))

        run_async(job, self._done, self._error, self._progress)

    def _progress(self, fraction: float, message: str) -> None:
        self.progress.setValue(int(fraction * 100))
        if not (message.startswith("Transferred") and "/" in message):  # skip per-block updates
            self.log.appendPlainText(message)

    def _cancel(self) -> None:
        if self._session is not None:
            self._session.cancel()

    def _finish(self) -> None:
        self.busy = False
        self.flash_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.refresh_target()
        self.ctx.library_changed.emit()

    def _done(self, result: ProgrammingResult) -> None:
        self.last_result = result
        self.log.appendPlainText(("\n✓ " if result.success else "\n✗ ") + result.message)
        if result.backup_id:
            self.log.appendPlainText(f"Backup saved to library: {result.backup_id}")
        if result.success and self.ctx.controller.source is not None and \
                self.ctx.controller.source.vehicle is self.vehicle():
            self.log.appendPlainText("The live simulator now runs this calibration — start the engine and "
                                     "watch the dashboard.")
        self._finish()

    def _error(self, message: str) -> None:
        self.log.appendPlainText(f"\n✗ {message}")
        self._finish()
