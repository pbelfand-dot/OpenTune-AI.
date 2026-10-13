"""Circular analog gauge drawn with QPainter."""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from opentune.gui import theme

START_DEG = 225.0  # 7:30 position
SWEEP_DEG = 270.0


class Gauge(QWidget):
    def __init__(self, title: str, unit: str, minimum: float, maximum: float, decimals: int = 0,
                 warn_from: float | None = None, critical_from: float | None = None,
                 major_ticks: int = 8, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.title = title
        self.unit = unit
        self.minimum = minimum
        self.maximum = maximum
        self.decimals = decimals
        self.warn_from = warn_from
        self.critical_from = critical_from
        self.major_ticks = major_ticks
        self.marker: float | None = None  # e.g. boost target
        self._value: float | None = None
        self.setMinimumSize(180, 180)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        return QSize(240, 240)

    @property
    def value(self) -> float | None:
        return self._value

    def set_range(self, minimum: float, maximum: float, unit: str | None = None, decimals: int | None = None,
                  warn_from: float | None = None, critical_from: float | None = None) -> None:
        self.minimum, self.maximum = minimum, maximum
        if unit is not None:
            self.unit = unit
        if decimals is not None:
            self.decimals = decimals
        self.warn_from, self.critical_from = warn_from, critical_from
        self.update()

    def set_value(self, value: float | None, marker: float | None = None) -> None:
        self._value = value
        self.marker = marker
        self.update()

    def _fraction(self, value: float) -> float:
        span = self.maximum - self.minimum
        return 0.0 if span <= 0 else max(0.0, min(1.0, (value - self.minimum) / span))

    def _angle(self, value: float) -> float:
        return START_DEG - SWEEP_DEG * self._fraction(value)

    def _value_color(self) -> QColor:
        v = self._value
        if v is not None and self.critical_from is not None and v >= self.critical_from:
            return QColor(theme.CRIT)
        if v is not None and self.warn_from is not None and v >= self.warn_from:
            return QColor(theme.WARN)
        return QColor(theme.ACCENT)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        side = min(self.width(), self.height()) - 12
        if side <= 20:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx, cy = self.width() / 2.0, self.height() / 2.0 + side * 0.04
        radius = side / 2.0
        ring = max(6.0, side * 0.055)
        rect = QRectF(cx - radius + ring, cy - radius + ring, 2 * (radius - ring), 2 * (radius - ring))

        # background track
        p.setPen(QPen(QColor(theme.BORDER), ring, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
        p.drawArc(rect, int(START_DEG * 16), int(-SWEEP_DEG * 16))

        # warning / critical zones on the track
        for start, color in ((self.warn_from, theme.WARN), (self.critical_from, theme.CRIT)):
            if start is not None and start < self.maximum:
                a0 = self._angle(start)
                pen = QPen(QColor(color), ring * 0.35, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap)
                p.setPen(pen)
                outer = rect.adjusted(-ring * 0.9, -ring * 0.9, ring * 0.9, ring * 0.9)
                p.drawArc(outer, int(a0 * 16), int(-(a0 - (START_DEG - SWEEP_DEG)) * 16))

        # value arc
        if self._value is not None:
            frac = self._fraction(self._value)
            p.setPen(QPen(self._value_color(), ring, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
            p.drawArc(rect, int(START_DEG * 16), int(-SWEEP_DEG * frac * 16))

        # ticks and labels
        tick_font = QFont(self.font())
        tick_font.setPointSizeF(max(7.0, side * 0.045))
        p.setFont(tick_font)
        inner_r = radius - ring * 2.3
        for i in range(self.major_ticks + 1):
            value = self.minimum + (self.maximum - self.minimum) * i / self.major_ticks
            ang = math.radians(self._angle(value))
            c, s = math.cos(ang), -math.sin(ang)
            p.setPen(QPen(QColor(theme.MUTED), 2))
            p.drawLine(QPointF(cx + c * inner_r, cy + s * inner_r),
                       QPointF(cx + c * (inner_r - ring * 0.8), cy + s * (inner_r - ring * 0.8)))
            label_r = inner_r - ring * 2.0
            text = self._tick_label(value)
            box = QRectF(cx + c * label_r - 22, cy + s * label_r - 9, 44, 18)
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, text)

        # target marker
        if self.marker is not None:
            ang = math.radians(self._angle(self.marker))
            c, s = math.cos(ang), -math.sin(ang)
            p.setPen(QPen(QColor(theme.TEXT), 3))
            r0, r1 = radius - ring * 0.2, radius - ring * 1.9
            p.drawLine(QPointF(cx + c * r0, cy + s * r0), QPointF(cx + c * r1, cy + s * r1))

        # value text
        value_font = QFont(self.font())
        value_font.setPointSizeF(max(10.0, side * 0.15))
        value_font.setBold(True)
        text = "–" if self._value is None else f"{self._value:.{self.decimals}f}"
        # Shrink long readouts (e.g. 4-digit rpm) so they stay clear of the tick labels.
        max_width = side * 0.42
        p.setFont(value_font)
        width = p.fontMetrics().horizontalAdvance(text)
        if width > max_width:
            value_font.setPointSizeF(max(8.0, value_font.pointSizeF() * max_width / width))
        p.setFont(value_font)
        p.setPen(QColor(theme.TEXT))
        p.drawText(QRectF(cx - radius, cy - side * 0.13, 2 * radius, side * 0.2),
                   Qt.AlignmentFlag.AlignCenter, text)
        small = QFont(self.font())
        small.setPointSizeF(max(8.0, side * 0.055))
        p.setFont(small)
        p.setPen(QColor(theme.MUTED))
        p.drawText(QRectF(cx - radius, cy + side * 0.08, 2 * radius, side * 0.08),
                   Qt.AlignmentFlag.AlignCenter, self.unit)
        p.drawText(QRectF(cx - radius, cy + side * 0.30, 2 * radius, side * 0.09),
                   Qt.AlignmentFlag.AlignCenter, self.title.upper())
        p.end()

    def _tick_label(self, value: float) -> str:
        span = self.maximum - self.minimum
        if span >= 2000:
            return f"{value / 1000:.0f}"
        if span < 10:
            return f"{value:.1f}".rstrip("0").rstrip(".") if value != 0 else "0"
        return f"{value:.0f}"
