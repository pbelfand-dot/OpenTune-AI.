"""Calibration library manager and OTSIM map editor."""

from __future__ import annotations

import copy

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from opentune.calibration.library import CalibrationLibraryError
from opentune.calibration.simcal import (
    CalibrationFormatError,
    CalTable,
    SimCalibration,
    default_calibration,
)
from opentune.engines import ENGINES, get_profile
from opentune.gui import theme
from opentune.gui.context import AppContext

_DECIMALS = {"bar": 3, "deg": 1, "lambda": 3, "rpm": 0}


def _heat(value: float, lo: float, hi: float) -> QColor:
    frac = 0.5 if hi <= lo else (value - lo) / (hi - lo)
    frac = max(0.0, min(1.0, frac))
    # blue -> teal -> amber -> red
    stops = [(0.0, (28, 70, 140)), (0.4, (32, 140, 140)), (0.75, (190, 140, 30)), (1.0, (190, 50, 45))]
    for (f0, c0), (f1, c1) in zip(stops, stops[1:], strict=False):
        if frac <= f1:
            t = (frac - f0) / (f1 - f0)
            return QColor(*(int(a + (b - a) * t) for a, b in zip(c0, c1, strict=False)))
    return QColor(*stops[-1][1])


class TableEditor(QTableWidget):
    def __init__(self, table: CalTable, parent: QWidget | None = None) -> None:
        rows, cols = table.shape
        super().__init__(rows, cols, parent)
        self.cal_table = table
        self.decimals = _DECIMALS.get(table.unit, 2)
        self.setHorizontalHeaderLabels([f"{v:g}" for v in table.col_axis])
        self.setVerticalHeaderLabels([f"{v:g}" for v in table.row_axis])
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        for r in range(rows):
            for c in range(cols):
                self.setItem(r, c, QTableWidgetItem(f"{table.values[r][c]:.{self.decimals}f}"))
        self._recolor()
        self.itemChanged.connect(self._item_changed)

    def _recolor(self) -> None:
        flat = [v for row in self.cal_table.values for v in row]
        lo, hi = min(flat), max(flat)
        self.blockSignals(True)
        for r in range(self.rowCount()):
            for c in range(self.columnCount()):
                item = self.item(r, c)
                item.setBackground(_heat(self.cal_table.values[r][c], lo, hi))
                item.setForeground(QColor("#ffffff"))
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.blockSignals(False)

    def _item_changed(self, item: QTableWidgetItem) -> None:
        try:
            value = float(item.text())
        except ValueError:
            value = self.cal_table.values[item.row()][item.column()]
        self.cal_table.values[item.row()][item.column()] = value
        self.blockSignals(True)
        item.setText(f"{value:.{self.decimals}f}")
        self.blockSignals(False)
        self._recolor()

    def scale_selection(self, factor: float = 1.0, offset: float = 0.0) -> None:
        for item in self.selectedItems():
            r, c = item.row(), item.column()
            self.cal_table.values[r][c] = self.cal_table.values[r][c] * factor + offset
            self.blockSignals(True)
            item.setText(f"{self.cal_table.values[r][c]:.{self.decimals}f}")
            self.blockSignals(False)
        self._recolor()


class MapEditorDialog(QDialog):
    """Edit the fictional OTSIM simulator calibration tables."""

    def __init__(self, calibration: SimCalibration, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Map editor — {title}")
        self.resize(900, 560)
        self.calibration = copy.deepcopy(calibration)
        lay = QVBoxLayout(self)
        banner = QLabel("Simulator calibration (fictional OTSIM format). Changes affect only the simulated "
                        "engine — useful for learning how boost, ignition and fuelling maps interact.")
        banner.setProperty("role", "banner-info")
        banner.setWordWrap(True)
        lay.addWidget(banner)
        self.tabs = QTabWidget()
        self.editors: dict[str, TableEditor] = {}
        for name, table in self.calibration.tables.items():
            editor = TableEditor(table)
            self.editors[name] = editor
            label = f"{name} ({table.unit}; rows: {table.row_label}, cols: {table.col_label})"
            self.tabs.addTab(editor, label if table.shape != (1, 1) else f"{name} ({table.unit})")
        lay.addWidget(self.tabs, 1)
        tools = QHBoxLayout()
        for text, factor, offset in (("+5%", 1.05, 0.0), ("−5%", 0.95, 0.0), ("+0.1", 1.0, 0.1), ("−0.1", 1.0, -0.1)):
            btn = QPushButton(f"Selection {text}")
            btn.clicked.connect(lambda _=False, f=factor, o=offset: self.current_editor().scale_selection(f, o))
            tools.addWidget(btn)
        tools.addStretch(1)
        lay.addLayout(tools)
        self.problems = QLabel("")
        self.problems.setStyleSheet(f"color: {theme.WARN};")
        self.problems.setWordWrap(True)
        lay.addWidget(self.problems)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Save as new version")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def current_editor(self) -> TableEditor:
        return self.tabs.currentWidget()  # type: ignore[return-value]

    def _accept(self) -> None:
        problems = self.calibration.validate()
        if problems:
            self.problems.setText("Cannot save: " + "; ".join(problems))
            return
        self.accept()


class CalibrationsPage(QWidget):
    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        title = QLabel("Calibration Files")
        title.setProperty("role", "title")
        sub = QLabel("Content-addressed library with SHA-256 integrity checks, version lineage, comparison "
                     "and export. Real ECU binaries are stored as opaque files — never parsed or modified.")
        sub.setWordWrap(True)
        sub.setProperty("role", "subtitle")
        lay.addWidget(title)
        lay.addWidget(sub)

        bar = QHBoxLayout()
        self.engine_combo = QComboBox()
        for code in ENGINES:
            self.engine_combo.addItem(code, code)
        self.new_btn = QPushButton("New simulator stock")
        self.import_btn = QPushButton("Import file…")
        self.edit_btn = QPushButton("Edit maps…")
        self.edit_btn.setProperty("role", "primary")
        self.compare_btn = QPushButton("Compare 2")
        self.verify_btn = QPushButton("Verify")
        self.export_btn = QPushButton("Export…")
        self.rename_btn = QPushButton("Rename…")
        self.delete_btn = QPushButton("Delete")
        bar.addWidget(QLabel("Engine"))
        bar.addWidget(self.engine_combo)
        for b in (self.new_btn, self.import_btn, self.edit_btn, self.compare_btn, self.verify_btn,
                  self.export_btn, self.rename_btn, self.delete_btn):
            bar.addWidget(b)
        bar.addStretch(1)
        lay.addLayout(bar)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["Name", "Engine", "Format", "Origin", "Version", "SHA-256", "Imported"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.itemSelectionChanged.connect(self._show_details)
        lay.addWidget(self.table, 2)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(170)
        lay.addWidget(self.details)

        self.new_btn.clicked.connect(self.create_stock)
        self.import_btn.clicked.connect(self.import_file)
        self.edit_btn.clicked.connect(self.edit_maps)
        self.compare_btn.clicked.connect(self.compare)
        self.verify_btn.clicked.connect(self.verify)
        self.export_btn.clicked.connect(self.export)
        self.rename_btn.clicked.connect(self.rename)
        self.delete_btn.clicked.connect(self.delete)
        ctx.library_changed.connect(self.refresh)
        self.engine_combo.setCurrentIndex(max(0, self.engine_combo.findData(ctx.settings.engine)))
        self.refresh()

    # ------------------------------------------------------------------ helpers
    def refresh(self) -> None:
        self.table.setRowCount(0)
        for rec in self.ctx.library.list():
            r = self.table.rowCount()
            self.table.insertRow(r)
            values = (rec.name, rec.engine, rec.format, rec.origin, rec.software_version or "–",
                      rec.short_hash, rec.imported_utc[:19].replace("T", " "))
            for c, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, rec.id)
                self.table.setItem(r, c, item)

    def selected_ids(self) -> list[str]:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return [self.table.item(r, 0).data(Qt.ItemDataRole.UserRole) for r in rows]

    def _one(self) -> str | None:
        ids = self.selected_ids()
        if len(ids) != 1:
            QMessageBox.information(self, "Select one", "Select exactly one calibration.")
            return None
        return ids[0]

    def _show_details(self) -> None:
        ids = self.selected_ids()
        if len(ids) != 1:
            self.details.setPlainText(f"{len(ids)} selected." if ids else "")
            return
        rec = self.ctx.library.get(ids[0])
        lines = [f"{k}: {v}" for k, v in vars(rec).items()]
        if rec.format == "otsim":
            try:
                cal = self.ctx.library.load_simcal(rec.id)
                lines.append("tables: " + ", ".join(f"{t.name} {t.shape[0]}x{t.shape[1]}" for t in cal.tables.values()))
            except (CalibrationFormatError, CalibrationLibraryError) as exc:
                lines.append(f"ERROR: {exc}")
        self.details.setPlainText("\n".join(lines))

    # ------------------------------------------------------------------ actions
    def create_stock(self) -> None:
        engine = self.engine_combo.currentData()
        rec = self.ctx.library.add_simcal(default_calibration(get_profile(engine)), f"{engine} simulator stock")
        self.ctx.library_changed.emit()
        self.details.setPlainText(f"Created {rec.name} ({rec.id}).")

    def import_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import calibration file", "",
                                              "Calibration files (*.bin *.otsim *.ori *.mod);;All files (*)")
        if not path:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Import calibration")
        form = QFormLayout(dialog)
        name = QLineEdit()
        engine = QComboBox()
        for code in ENGINES:
            engine.addItem(code, code)
        engine.setCurrentIndex(self.engine_combo.currentIndex())
        version = QLineEdit()
        notes = QLineEdit()
        form.addRow("Name", name)
        form.addRow("Engine", engine)
        form.addRow("Software version", version)
        form.addRow("Notes", notes)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(dialog.accept)
        box.rejected.connect(dialog.reject)
        form.addRow(box)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            rec = self.ctx.library.import_file(path, name.text(), engine.currentData(),
                                               software_version=version.text(), notes=notes.text())
        except (CalibrationLibraryError, CalibrationFormatError, OSError) as exc:
            QMessageBox.critical(self, "Import failed", str(exc))
            return
        self.ctx.library_changed.emit()
        self.details.setPlainText(f"Imported {rec.name}: sha256 {rec.sha256}")

    def edit_maps(self) -> None:
        cal_id = self._one()
        if not cal_id:
            return
        rec = self.ctx.library.get(cal_id)
        if rec.format != "otsim":
            QMessageBox.information(
                self, "Not editable",
                "Only OpenTune simulator calibrations (OTSIM) can be edited in v0.1. Real ECU binaries are "
                "stored and compared but never interpreted — OpenTune does not ship map definitions.")
            return
        dialog = MapEditorDialog(self.ctx.library.load_simcal(cal_id), rec.name, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        new_name, ok = QInputDialog.getText(self, "Save calibration", "Name for the new version:",
                                            text=f"{rec.name} (modified)")
        if not ok:
            return
        cal = dialog.calibration
        cal.version = f"SIM-{cal.engine}-MOD{len(self.ctx.library.list()) + 1:03d}"[:16]
        try:
            new = self.ctx.library.add_simcal(cal, new_name, origin="simulated-modified", parent_id=rec.id)
        except (CalibrationLibraryError, CalibrationFormatError) as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            return
        self.ctx.library_changed.emit()
        self.details.setPlainText(f"Saved {new.name} ({new.id}), derived from {rec.id}. Flash it on the "
                                  "ECU Programming page to drive with it in the simulator.")

    def compare(self) -> None:
        ids = self.selected_ids()
        if len(ids) != 2:
            QMessageBox.information(self, "Compare", "Select exactly two calibrations (Cmd/Ctrl-click).")
            return
        diff = self.ctx.library.compare(ids[0], ids[1])
        lines = [f"Sizes: {diff.size_a} vs {diff.size_b} bytes",
                 "Identical." if diff.identical else
                 f"{diff.changed_bytes} byte(s) differ in {len(diff.regions)} region(s):"]
        lines += [f"  0x{r.start:06X}–0x{r.end:06X}  ({r.length} B)" for r in diff.regions[:200]]
        a, b = (self.ctx.library.get(i) for i in ids)
        if a.format == b.format == "otsim":
            ca, cb = self.ctx.library.load_simcal(a.id), self.ctx.library.load_simcal(b.id)
            for name, ta in ca.tables.items():
                tb = cb.tables.get(name)
                if tb is None or tb.shape != ta.shape:
                    lines.append(f"table {name}: structure differs")
                    continue
                deltas = [vb - va for ra, rb in zip(ta.values, tb.values, strict=False)
                          for va, vb in zip(ra, rb, strict=False)]
                changed = [d for d in deltas if abs(d) > 1e-9]
                if changed:
                    lines.append(f"table {name}: {len(changed)} cell(s) changed, Δ {min(changed):+.3f} … "
                                 f"{max(changed):+.3f} {ta.unit}")
        self.details.setPlainText("\n".join(lines))

    def verify(self) -> None:
        results = [f"{self.ctx.library.get(i).name}: {'OK' if self.ctx.library.verify(i) else 'FAILED'}"
                   for i in self.selected_ids()]
        self.details.setPlainText("\n".join(results) or "Select calibrations to verify.")

    def export(self) -> None:
        cal_id = self._one()
        if not cal_id:
            return
        rec = self.ctx.library.get(cal_id)
        suffix = ".otsim" if rec.format == "otsim" else ".bin"
        path, _ = QFileDialog.getSaveFileName(self, "Export calibration", f"{rec.name}{suffix}")
        if path:
            self.ctx.library.export(cal_id, path)
            self.details.setPlainText(f"Exported to {path}")

    def rename(self) -> None:
        cal_id = self._one()
        if not cal_id:
            return
        rec = self.ctx.library.get(cal_id)
        name, ok = QInputDialog.getText(self, "Rename", "Name:", text=rec.name)
        if ok and name.strip():
            self.ctx.library.update(cal_id, name=name.strip())
            self.ctx.library_changed.emit()

    def delete(self) -> None:
        ids = self.selected_ids()
        if not ids:
            return
        if QMessageBox.question(self, "Delete", f"Delete {len(ids)} calibration(s) from the library?") \
                != QMessageBox.StandardButton.Yes:
            return
        for cal_id in ids:
            self.ctx.library.remove(cal_id)
        self.ctx.library_changed.emit()
