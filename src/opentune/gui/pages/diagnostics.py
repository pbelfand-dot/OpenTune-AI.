"""Diagnostic trouble codes, VIN and supported-PID information."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from opentune.gui import theme
from opentune.gui.context import AppContext
from opentune.gui.controller import run_async
from opentune.obd.client import DtcReport
from opentune.obd.pids import PIDS


class DiagnosticsPage(QWidget):
    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.last_report: DtcReport | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        title = QLabel("Diagnostics")
        title.setProperty("role", "title")
        sub = QLabel("Generic OBD-II (SAE J1979) services: stored, pending and permanent DTCs, VIN, "
                     "supported PIDs. Works with the simulator or a real ELM327 adapter.")
        sub.setProperty("role", "subtitle")
        sub.setWordWrap(True)
        lay.addWidget(title)
        lay.addWidget(sub)

        buttons = QHBoxLayout()
        self.read_btn = QPushButton("Read DTCs")
        self.read_btn.setProperty("role", "primary")
        self.clear_btn = QPushButton("Clear DTCs…")
        self.clear_btn.setProperty("role", "danger")
        self.vin_btn = QPushButton("Read VIN")
        self.pids_btn = QPushButton("Supported PIDs")
        for b in (self.read_btn, self.clear_btn, self.vin_btn, self.pids_btn):
            buttons.addWidget(b)
        buttons.addStretch(1)
        self.mil_label = QLabel("MIL: –")
        self.mil_label.setStyleSheet("font-size: 15px; font-weight: 600;")
        buttons.addWidget(self.mil_label)
        lay.addLayout(buttons)

        self.status = QLabel("")
        self.status.setProperty("role", "muted")
        lay.addWidget(self.status)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Type", "Code", "Description", "System"])
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        lay.addWidget(self.table, 2)

        info_title = QLabel("Vehicle information")
        info_title.setProperty("role", "section")
        lay.addWidget(info_title)
        self.info_table = QTableWidget(0, 2)
        self.info_table.setHorizontalHeaderLabels(["Item", "Value"])
        self.info_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.info_table.verticalHeader().setVisible(False)
        self.info_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        lay.addWidget(self.info_table, 1)

        note = QLabel("Manufacturer-specific BMW fault memory is not read in v0.1. Codes without a built-in "
                      "description are shown as generic or manufacturer-specific — never guessed.")
        note.setWordWrap(True)
        note.setProperty("role", "muted")
        lay.addWidget(note)

        self.read_btn.clicked.connect(self.read_dtcs)
        self.clear_btn.clicked.connect(self.clear_dtcs)
        self.vin_btn.clicked.connect(self.read_vin)
        self.pids_btn.clicked.connect(self.read_pids)
        ctx.controller.connected.connect(lambda _s: self._set_enabled(True))
        ctx.controller.disconnected.connect(lambda: self._set_enabled(False))
        self._set_enabled(ctx.controller.source is not None)

    def _set_enabled(self, on: bool) -> None:
        for b in (self.read_btn, self.clear_btn, self.vin_btn, self.pids_btn):
            b.setEnabled(on)
        if not on:
            self.status.setText("Connect to the simulator or an adapter first (Connection page).")
        else:
            self.status.setText("Ready.")

    def _client(self):
        source = self.ctx.controller.source
        if source is None:
            raise RuntimeError("Not connected")
        return source.diagnostics_client()

    def _busy(self, text: str) -> None:
        self.status.setText(text)
        self._set_buttons(False)

    def _set_buttons(self, on: bool) -> None:
        for b in (self.read_btn, self.clear_btn, self.vin_btn, self.pids_btn):
            b.setEnabled(on)

    def _fail(self, message: str) -> None:
        self._set_buttons(True)
        self.status.setText(f"Error: {message}")

    # ------------------------------------------------------------------ actions
    def read_dtcs(self) -> None:
        self._busy("Reading diagnostic trouble codes…")
        run_async(lambda _p: self._client().read_dtcs(), self.show_report, self._fail)

    def show_report(self, report: DtcReport) -> None:
        self.last_report = report
        self._set_buttons(True)
        self.table.setRowCount(0)
        rows = [("Stored", d) for d in report.stored] + [("Pending", d) for d in report.pending] + \
               [("Permanent", d) for d in report.permanent]
        for kind, dtc in rows:
            r = self.table.rowCount()
            self.table.insertRow(r)
            for c, text in enumerate((kind, dtc.code, dtc.description, dtc.system)):
                self.table.setItem(r, c, QTableWidgetItem(text))
        color = theme.WARN if report.mil_on else theme.OK
        self.mil_label.setText(f"MIL: {'ON' if report.mil_on else 'off'}")
        self.mil_label.setStyleSheet(f"font-size: 15px; font-weight: 600; color: {color};")
        self.status.setText(f"{len(report.stored)} stored, {len(report.pending)} pending, "
                            f"{len(report.permanent)} permanent.")

    def clear_dtcs(self) -> None:
        answer = QMessageBox.question(
            self, "Clear DTCs",
            "Clear stored and pending DTCs (OBD Mode 04)?\n\nOn a real car this also resets readiness "
            "monitors and freeze-frame data, which can fail an emissions inspection until the car is "
            "driven through its monitors again. Permanent DTCs cannot be cleared this way.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.perform_clear()

    def perform_clear(self) -> None:
        self._busy("Clearing DTCs…")

        def job(_p):
            client = self._client()
            client.clear_dtcs()
            return client.read_dtcs()

        run_async(job, self.show_report, self._fail)

    def _set_info(self, rows: list[tuple[str, str]]) -> None:
        self.info_table.setRowCount(0)
        for item, value in rows:
            r = self.info_table.rowCount()
            self.info_table.insertRow(r)
            self.info_table.setItem(r, 0, QTableWidgetItem(item))
            self.info_table.setItem(r, 1, QTableWidgetItem(value))

    def read_vin(self) -> None:
        self._busy("Reading VIN…")

        def job(_p):
            client = self._client()
            return [("VIN", client.read_vin() or "not reported"),
                    ("Adapter", client.elm.version or "–"),
                    ("Protocol", str(client.elm.protocol.number if client.elm.protocol else "–")),
                    ("Adapter voltage", f"{client.adapter_voltage() or 0:.1f} V")]

        run_async(job, self._info_done, self._fail)

    def _info_done(self, rows) -> None:
        self._set_buttons(True)
        self._set_info(rows)
        self.status.setText("Done.")

    def read_pids(self) -> None:
        self._busy("Querying supported PIDs…")

        def job(_p):
            supported = sorted(self._client().supported_pids())
            return [(f"PID 0x{pid:02X}", PIDS[pid].name if pid in PIDS else "(range / not decoded)")
                    for pid in supported]

        run_async(job, self._info_done, self._fail)
