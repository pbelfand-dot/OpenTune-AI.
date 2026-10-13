"""Dark theme palette and Qt stylesheet."""

from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication

BG = "#0e1014"
PANEL = "#151820"
CARD = "#1b1f29"
CARD_HI = "#232837"
BORDER = "#2a3040"
TEXT = "#e6e9ef"
MUTED = "#8a93a6"
ACCENT = "#3fa9f5"
ACCENT_DARK = "#1c6fae"
M_RED = "#e4572e"
OK = "#3ddc84"
WARN = "#ffb020"
CRIT = "#ff4d4f"

SERIES_COLORS = ("#3fa9f5", "#e4572e", "#3ddc84", "#ffb020", "#b36bff", "#2ed6c9")

STYLESHEET = f"""
QWidget {{
    background-color: {BG};
    color: {TEXT};
    font-size: 13px;
}}
QMainWindow, QDialog {{ background-color: {BG}; }}
QLabel {{ background: transparent; }}
QLabel[role="title"] {{ font-size: 22px; font-weight: 600; }}
QLabel[role="subtitle"] {{ color: {MUTED}; font-size: 12px; }}
QLabel[role="section"] {{ font-size: 15px; font-weight: 600; padding-top: 6px; }}
QLabel[role="muted"] {{ color: {MUTED}; }}
QLabel[role="banner-danger"] {{
    background-color: #3a1416; color: #ffd7d7; border: 1px solid {CRIT};
    border-radius: 8px; padding: 10px 12px; font-weight: 600;
}}
QLabel[role="banner-info"] {{
    background-color: #10283a; color: #d6ecff; border: 1px solid {ACCENT_DARK};
    border-radius: 8px; padding: 10px 12px;
}}
QLabel[role="badge-danger"] {{
    background-color: #3a1416; color: #ffb3b3; border: 1px solid {CRIT};
    border-radius: 9px; padding: 2px 10px; font-weight: 600; font-size: 11px;
}}
QLabel[role="badge-ok"] {{
    background-color: #12301f; color: #b6f5cf; border: 1px solid {OK};
    border-radius: 9px; padding: 2px 10px; font-weight: 600; font-size: 11px;
}}
QFrame[role="card"] {{
    background-color: {CARD}; border: 1px solid {BORDER}; border-radius: 10px;
}}
QFrame[role="card"] QLabel {{ background: transparent; }}
QListWidget#nav {{
    background-color: {PANEL}; border: none; border-right: 1px solid {BORDER};
    padding-top: 8px; outline: 0;
}}
QListWidget#nav::item {{ padding: 11px 16px; border-radius: 8px; margin: 2px 8px; color: {MUTED}; }}
QListWidget#nav::item:selected {{ background-color: {CARD_HI}; color: {TEXT}; }}
QListWidget#nav::item:hover:!selected {{ background-color: {CARD}; }}
QPushButton {{
    background-color: {CARD_HI}; border: 1px solid {BORDER}; border-radius: 7px;
    padding: 7px 14px; color: {TEXT};
}}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:pressed {{ background-color: {BORDER}; }}
QPushButton:disabled {{ color: #5b6375; border-color: #222734; }}
QPushButton[role="primary"] {{ background-color: {ACCENT_DARK}; border-color: {ACCENT}; font-weight: 600; }}
QPushButton[role="danger"] {{ background-color: #5a1d1f; border-color: {CRIT}; font-weight: 600; }}
QPushButton:checked {{ background-color: {M_RED}; border-color: {M_RED}; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTextBrowser, QTextEdit {{
    background-color: {PANEL}; border: 1px solid {BORDER}; border-radius: 6px; padding: 5px 8px;
    selection-background-color: {ACCENT_DARK};
}}
QComboBox QAbstractItemView {{ background-color: {PANEL}; border: 1px solid {BORDER}; }}
QTableWidget, QTableView, QTreeWidget {{
    background-color: {PANEL}; alternate-background-color: {CARD}; border: 1px solid {BORDER};
    border-radius: 6px; gridline-color: {BORDER}; selection-background-color: {ACCENT_DARK};
}}
QHeaderView::section {{
    background-color: {CARD}; color: {MUTED}; border: none; border-bottom: 1px solid {BORDER};
    padding: 6px; font-weight: 600;
}}
QProgressBar {{
    background-color: {PANEL}; border: 1px solid {BORDER}; border-radius: 6px; text-align: center; height: 20px;
}}
QProgressBar::chunk {{ background-color: {ACCENT}; border-radius: 5px; }}
QSlider::groove:horizontal {{ height: 6px; background: {BORDER}; border-radius: 3px; }}
QSlider::handle:horizontal {{ background: {ACCENT}; width: 16px; margin: -6px 0; border-radius: 8px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT_DARK}; border-radius: 3px; }}
QCheckBox, QRadioButton {{ background: transparent; spacing: 8px; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 15px; height: 15px; border: 1px solid #4a5468; background-color: {PANEL};
}}
QCheckBox::indicator {{ border-radius: 4px; }}
QRadioButton::indicator {{ border-radius: 8px; }}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background-color: {ACCENT}; border-color: {ACCENT};
}}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color: {ACCENT}; }}
QCheckBox:disabled, QRadioButton:disabled {{ color: #5b6375; }}
QGroupBox {{
    border: 1px solid {BORDER}; border-radius: 8px; margin-top: 14px; padding-top: 10px;
    background-color: {CARD};
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 4px; color: {MUTED}; }}
QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 6px; }}
QTabBar::tab {{ background: {CARD}; color: {MUTED}; padding: 7px 14px; border-top-left-radius: 6px;
               border-top-right-radius: 6px; margin-right: 2px; }}
QTabBar::tab:selected {{ background: {CARD_HI}; color: {TEXT}; }}
QStatusBar {{ background-color: {PANEL}; border-top: 1px solid {BORDER}; color: {MUTED}; }}
QScrollArea {{ border: none; }}
QScrollBar:vertical {{ background: {PANEL}; width: 10px; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 5px; min-height: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QToolTip {{ background-color: {CARD_HI}; color: {TEXT}; border: 1px solid {BORDER}; }}
"""


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(BG))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Base, QColor(PANEL))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(CARD))
    palette.setColor(QPalette.ColorRole.Text, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Button, QColor(CARD_HI))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(ACCENT_DARK))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(CARD_HI))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(TEXT))
    app.setPalette(palette)
    app.setStyleSheet(STYLESHEET)
    font = QFont()
    font.setFamilies(["SF Pro Text", "Helvetica Neue", "Inter", "Segoe UI", "Arial"])
    font.setPointSize(13 if font.pointSize() < 13 else font.pointSize())
    app.setFont(font)
