"""Compact numeric readout tile."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget

from opentune.gui import theme


class ValueTile(QFrame):
    def __init__(self, title: str, unit: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "card")
        self.setMinimumWidth(120)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 10)
        layout.setSpacing(2)
        self.title_label = QLabel(title.upper())
        self.title_label.setProperty("role", "muted")
        self.title_label.setStyleSheet("font-size: 10px; letter-spacing: 1px;")
        self.value_label = QLabel("–")
        self.value_label.setStyleSheet("font-size: 22px; font-weight: 600;")
        self.unit_label = QLabel(unit)
        self.unit_label.setProperty("role", "muted")
        self.unit_label.setStyleSheet("font-size: 11px;")
        for w in (self.title_label, self.value_label, self.unit_label):
            w.setAlignment(Qt.AlignmentFlag.AlignLeft)
            layout.addWidget(w)
        self._state = "normal"

    @property
    def text(self) -> str:
        return self.value_label.text()

    @property
    def state(self) -> str:
        return self._state

    def set_unit(self, unit: str) -> None:
        self.unit_label.setText(unit)

    def set_text(self, text: str, state: str = "normal") -> None:
        self.value_label.setText(text)
        if state != self._state:
            self._state = state
            color = {"warn": theme.WARN, "critical": theme.CRIT}.get(state, theme.TEXT)
            self.value_label.setStyleSheet(f"font-size: 22px; font-weight: 600; color: {color};")
