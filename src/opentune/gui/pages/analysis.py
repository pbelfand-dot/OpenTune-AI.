"""AI performance analysis page (rule engine + optional local model)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from opentune.analysis import (
    AnalysisReport,
    LocalModelClient,
    LocalModelConfig,
    analyze_samples,
    build_prompt,
    to_markdown,
)
from opentune.datalog import list_logs, read_log
from opentune.engines import get_profile
from opentune.gui import theme
from opentune.gui.context import AppContext
from opentune.gui.controller import run_async

_SEV_COLOR = {"critical": theme.CRIT, "warning": theme.WARN, "info": theme.ACCENT}


class AnalysisPage(QWidget):
    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.report: AnalysisReport | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        title = QLabel("AI Performance Analysis")
        title.setProperty("role", "title")
        sub = QLabel("Deterministic rule engine (always on, fully offline) with an optional local LLM "
                     "narrative via Ollama or any OpenAI-compatible local server. Nothing is sent to the cloud.")
        sub.setWordWrap(True)
        sub.setProperty("role", "subtitle")
        lay.addWidget(title)
        lay.addWidget(sub)

        row = QHBoxLayout()
        self.log_combo = QComboBox()
        self.log_combo.setMinimumWidth(380)
        self.browse_btn = QPushButton("Browse…")
        self.run_btn = QPushButton("Run analysis")
        self.run_btn.setProperty("role", "primary")
        self.save_btn = QPushButton("Save report")
        row.addWidget(QLabel("Log"))
        row.addWidget(self.log_combo, 1)
        row.addWidget(self.browse_btn)
        row.addWidget(self.run_btn)
        row.addWidget(self.save_btn)
        lay.addLayout(row)

        self.score = QLabel("Health score: –")
        self.score.setStyleSheet("font-size: 18px; font-weight: 600;")
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        lay.addWidget(self.score)
        lay.addWidget(self.summary)

        split = QSplitter(Qt.Orientation.Horizontal)
        self.findings = QTableWidget(0, 3)
        self.findings.setHorizontalHeaderLabels(["Severity", "Finding", "Time (s)"])
        self.findings.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.findings.verticalHeader().setVisible(False)
        self.findings.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.findings.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.report_view = QTextBrowser()
        self.report_view.setOpenExternalLinks(False)
        split.addWidget(self.findings)
        split.addWidget(self.report_view)
        split.setSizes([420, 600])
        lay.addWidget(split, 1)

        lay.addWidget(self._build_model_box())

        self.browse_btn.clicked.connect(self._browse)
        self.run_btn.clicked.connect(self.run_analysis)
        self.save_btn.clicked.connect(self._save)
        ctx.analyze_requested.connect(self.analyze_path)
        ctx.logs_changed.connect(self.refresh_logs)
        self.refresh_logs()

    def _build_model_box(self) -> QGroupBox:
        s = self.ctx.settings
        box = QGroupBox("Optional local AI model")
        form = QFormLayout(box)
        self.model_enabled = QCheckBox("Generate a plain-language narrative with a local model")
        self.model_enabled.setChecked(s.local_model_enabled)
        self.backend_combo = QComboBox()
        self.backend_combo.addItem("Ollama", "ollama")
        self.backend_combo.addItem("OpenAI-compatible local server (llama.cpp, LM Studio)", "openai-compatible")
        self.backend_combo.setCurrentIndex(max(0, self.backend_combo.findData(s.local_model_backend)))
        self.url_edit = QLineEdit(s.local_model_url)
        self.model_edit = QLineEdit(s.local_model_name)
        buttons = QHBoxLayout()
        self.test_btn = QPushButton("Test connection")
        self.narrate_btn = QPushButton("Generate narrative")
        buttons.addWidget(self.test_btn)
        buttons.addWidget(self.narrate_btn)
        buttons.addStretch(1)
        self.model_status = QLabel("Local-only: URLs must point to localhost.")
        self.model_status.setProperty("role", "muted")
        form.addRow(self.model_enabled)
        form.addRow("Backend", self.backend_combo)
        form.addRow("Server URL", self.url_edit)
        form.addRow("Model", self.model_edit)
        form.addRow(buttons)
        form.addRow(self.model_status)
        self.test_btn.clicked.connect(self._test_model)
        self.narrate_btn.clicked.connect(self._narrate)
        self.model_enabled.toggled.connect(self._store_model_settings)
        self.backend_combo.currentIndexChanged.connect(self._backend_changed)
        self.url_edit.editingFinished.connect(self._store_model_settings)
        self.model_edit.editingFinished.connect(self._store_model_settings)
        return box

    # ------------------------------------------------------------------ logs
    def refresh_logs(self) -> None:
        current = self.log_combo.currentData()
        self.log_combo.clear()
        for path in list_logs(self.ctx.paths.logs):
            self.log_combo.addItem(path.name, str(path))
        if current:
            idx = self.log_combo.findData(current)
            if idx >= 0:
                self.log_combo.setCurrentIndex(idx)

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open datalog", str(self.ctx.paths.logs),
                                              "OpenTune logs (*.otlog.csv *.csv)")
        if path:
            self.analyze_path(path)

    def analyze_path(self, path: str) -> None:
        idx = self.log_combo.findData(path)
        if idx < 0:
            self.log_combo.addItem(Path(path).name, path)
            idx = self.log_combo.count() - 1
        self.log_combo.setCurrentIndex(idx)
        self.run_analysis()

    # ------------------------------------------------------------------ analysis
    def run_analysis(self) -> None:
        path = self.log_combo.currentData()
        if not path:
            self.summary.setText("No log selected. Record one on the Data Logging page first.")
            return
        self.summary.setText("Analysing…")
        self.run_btn.setEnabled(False)

        def job(_p):
            log = read_log(path)
            profile = get_profile(log.engine)
            return analyze_samples(log.samples, profile, log.metadata.get("source", ""))

        run_async(job, self.show_report, self._failed)

    def _failed(self, message: str) -> None:
        self.run_btn.setEnabled(True)
        self.summary.setText(f"Analysis failed: {message}")

    def show_report(self, report: AnalysisReport) -> None:
        self.run_btn.setEnabled(True)
        self.report = report
        color = theme.OK if report.health_score >= 80 else theme.WARN if report.health_score >= 50 else theme.CRIT
        self.score.setText(f"Health score: {report.health_score}/100")
        self.score.setStyleSheet(f"font-size: 18px; font-weight: 600; color: {color};")
        self.summary.setText(report.summary)
        self.findings.setRowCount(0)
        for f in report.findings:
            r = self.findings.rowCount()
            self.findings.insertRow(r)
            sev = QTableWidgetItem(f.severity.upper())
            sev.setForeground(Qt.GlobalColor.white)
            sev.setBackground(QColor(_SEV_COLOR.get(f.severity, theme.MUTED)))
            self.findings.setItem(r, 0, sev)
            self.findings.setItem(r, 1, QTableWidgetItem(f.title))
            self.findings.setItem(r, 2, QTableWidgetItem("" if f.time_s is None else f"{f.time_s:.1f}"))
        self.report_view.setMarkdown(to_markdown(report))
        if self.model_enabled.isChecked():
            self._narrate()

    def _save(self) -> None:
        if self.report is None:
            return
        default = self.ctx.paths.reports / "analysis.md"
        path, _ = QFileDialog.getSaveFileName(self, "Save report", str(default), "Markdown (*.md)")
        if path:
            Path(path).write_text(to_markdown(self.report), encoding="utf-8")
            self.model_status.setText(f"Saved report to {path}")

    # ------------------------------------------------------------------ local model
    def _backend_changed(self) -> None:
        backend = self.backend_combo.currentData()
        if backend == "ollama" and ":8080" in self.url_edit.text():
            self.url_edit.setText("http://127.0.0.1:11434")
        elif backend == "openai-compatible" and ":11434" in self.url_edit.text():
            self.url_edit.setText("http://127.0.0.1:8080")
        self._store_model_settings()

    def _store_model_settings(self) -> None:
        s = self.ctx.settings
        s.local_model_enabled = self.model_enabled.isChecked()
        s.local_model_backend = self.backend_combo.currentData()
        s.local_model_url = self.url_edit.text().strip()
        s.local_model_name = self.model_edit.text().strip()
        self.ctx.save_settings()

    def model_config(self) -> LocalModelConfig:
        return LocalModelConfig(backend=self.backend_combo.currentData(), base_url=self.url_edit.text().strip(),
                                model=self.model_edit.text().strip() or "llama3.2")

    def _test_model(self) -> None:
        self.model_status.setText("Contacting local model server…")
        config = self.model_config()
        run_async(lambda _p: LocalModelClient(config).list_models(),
                  lambda models: self.model_status.setText(
                      "Connected. Models: " + (", ".join(models[:8]) or "none installed")),
                  lambda msg: self.model_status.setText(f"Not available: {msg}"))

    def _narrate(self) -> None:
        if self.report is None:
            self.model_status.setText("Run an analysis first.")
            return
        report = self.report
        config = self.model_config()
        self.model_status.setText("Generating narrative with local model…")

        def job(_p):
            return LocalModelClient(config).generate(build_prompt(report))

        def done(text: str) -> None:
            report.narrative = text
            self.report_view.setMarkdown(to_markdown(report))
            self.model_status.setText("Narrative generated locally.")

        run_async(job, done, lambda msg: self.model_status.setText(
            f"Local model unavailable ({msg}). Rule-based analysis shown."))
