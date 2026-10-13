"""Datalog recording and log browser."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from opentune.datalog import list_logs, make_log_name, summarize_log
from opentune.gui.context import AppContext


class LogsPage(QWidget):
    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        title = QLabel("Data Logging")
        title.setProperty("role", "title")
        sub = QLabel("Record every live channel to CSV (opens in Excel, Numbers or pandas).")
        sub.setProperty("role", "subtitle")
        lay.addWidget(title)
        lay.addWidget(sub)

        rec = QHBoxLayout()
        self.record_btn = QPushButton("● Start recording")
        self.record_btn.setCheckable(True)
        self.record_btn.setMinimumWidth(170)
        self.record_btn.toggled.connect(self.toggle_recording)
        self.notes_edit = QLineEdit()
        self.notes_edit.setPlaceholderText("Notes for this log (e.g. 3rd gear pull, 93 octane, 25 °C)")
        rec.addWidget(self.record_btn)
        rec.addWidget(self.notes_edit, 1)
        lay.addLayout(rec)
        self.rec_status = QLabel("Not recording.")
        self.rec_status.setProperty("role", "muted")
        lay.addWidget(self.rec_status)

        bar = QHBoxLayout()
        section = QLabel("Saved logs")
        section.setProperty("role", "section")
        bar.addWidget(section)
        bar.addStretch(1)
        self.refresh_btn = QPushButton("Refresh")
        self.analyze_btn = QPushButton("Analyze selected")
        self.analyze_btn.setProperty("role", "primary")
        self.folder_btn = QPushButton("Open folder")
        self.delete_btn = QPushButton("Delete")
        for b in (self.refresh_btn, self.analyze_btn, self.folder_btn, self.delete_btn):
            bar.addWidget(b)
        lay.addLayout(bar)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["File", "Engine", "Duration", "Samples", "Modified"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.doubleClicked.connect(self.analyze_selected)
        lay.addWidget(self.table, 1)

        self.refresh_btn.clicked.connect(self.refresh)
        self.analyze_btn.clicked.connect(self.analyze_selected)
        self.folder_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.ctx.paths.logs))))
        self.delete_btn.clicked.connect(self.delete_selected)
        ctx.controller.logging_changed.connect(self._logging_changed)
        ctx.logs_changed.connect(self.refresh)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(500)
        self.refresh()

    # ------------------------------------------------------------------ recording
    def toggle_recording(self, on: bool) -> None:
        controller = self.ctx.controller
        if on:
            if controller.source is None:
                self.record_btn.setChecked(False)
                QMessageBox.warning(self, "Not connected", "Connect to a data source first.")
                return
            name = make_log_name(controller.source.profile.code, controller.source.label, datetime.now())
            controller.start_logging(self.ctx.paths.logs / name, self.notes_edit.text())
        else:
            controller.stop_logging()

    def _logging_changed(self, active: bool, path: str) -> None:
        self.record_btn.blockSignals(True)
        self.record_btn.setChecked(active)
        self.record_btn.blockSignals(False)
        self.record_btn.setText("■ Stop recording" if active else "● Start recording")
        if not active:
            self.rec_status.setText(f"Saved {Path(path).name}")
            self.ctx.logs_changed.emit()

    def _tick(self) -> None:
        c = self.ctx.controller
        if c.is_logging:
            self.rec_status.setText(f"Recording… {c.log_rows} samples at {c.sample_rate:.0f} Hz")

    # ------------------------------------------------------------------ browser
    def refresh(self) -> None:
        self.table.setRowCount(0)
        for path in list_logs(self.ctx.paths.logs):
            try:
                info = summarize_log(path)
                engine, duration, count = info.engine, f"{info.duration_s:.1f} s", str(info.sample_count)
            except ValueError:
                engine, duration, count = "?", "unreadable", "0"
            r = self.table.rowCount()
            self.table.insertRow(r)
            item = QTableWidgetItem(path.name)
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            self.table.setItem(r, 0, item)
            modified = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            for c, text in enumerate((engine, duration, count, modified), start=1):
                self.table.setItem(r, c, QTableWidgetItem(text))

    def selected_path(self) -> str | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        return self.table.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole)

    def analyze_selected(self) -> None:
        path = self.selected_path()
        if path:
            self.ctx.analyze_requested.emit(path)

    def delete_selected(self) -> None:
        path = self.selected_path()
        if not path:
            return
        if QMessageBox.question(self, "Delete log", f"Delete {Path(path).name}?") == QMessageBox.StandardButton.Yes:
            Path(path).unlink(missing_ok=True)
            self.refresh()
