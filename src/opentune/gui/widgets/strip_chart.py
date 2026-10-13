"""Scrolling multi-series strip chart (each series scaled to its own range)."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from opentune.gui import theme


@dataclass
class Series:
    key: str
    label: str
    color: str
    minimum: float
    maximum: float
    decimals: int = 0
    points: deque = field(default_factory=lambda: deque(maxlen=4000))


class StripChart(QWidget):
    def __init__(self, window_s: float = 30.0, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.window_s = window_s
        self.series: dict[str, Series] = {}
        self.setMinimumHeight(170)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def add_series(self, key: str, label: str, minimum: float, maximum: float, decimals: int = 0) -> None:
        color = theme.SERIES_COLORS[len(self.series) % len(theme.SERIES_COLORS)]
        self.series[key] = Series(key, label, color, minimum, maximum, decimals)

    def clear(self) -> None:
        for s in self.series.values():
            s.points.clear()
        self.update()

    def append(self, t: float, values: dict[str, float]) -> None:
        for key, s in self.series.items():
            if key in values:
                s.points.append((t, values[key]))
                while s.points and t - s.points[0][0] > self.window_s * 1.1:
                    s.points.popleft()
        self.update()

    def latest_time(self) -> float | None:
        times = [s.points[-1][0] for s in self.series.values() if s.points]
        return max(times) if times else None

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(theme.PANEL))
        plot = QRectF(self.rect()).adjusted(10, 30, -10, -10)
        p.setPen(QPen(QColor(theme.BORDER), 1))
        for i in range(5):
            y = plot.top() + plot.height() * i / 4
            p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
        for i in range(7):
            x = plot.left() + plot.width() * i / 6
            p.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))

        t_end = self.latest_time()
        font = QFont(self.font())
        font.setPointSizeF(9.5)
        p.setFont(font)
        x_legend = plot.left()
        for s in self.series.values():
            latest = s.points[-1][1] if s.points else None
            text = f"■ {s.label}: {'–' if latest is None else f'{latest:.{s.decimals}f}'}"
            p.setPen(QColor(s.color))
            p.drawText(QPointF(x_legend, 18), text)
            x_legend += p.fontMetrics().horizontalAdvance(text) + 18
            if t_end is None or len(s.points) < 2:
                continue
            span = s.maximum - s.minimum or 1.0
            path = QPainterPath()
            started = False
            for t, v in s.points:
                age = t_end - t
                if age > self.window_s:
                    continue
                x = plot.right() - plot.width() * age / self.window_s
                frac = max(0.0, min(1.0, (v - s.minimum) / span))
                y = plot.bottom() - plot.height() * frac
                if started:
                    path.lineTo(x, y)
                else:
                    path.moveTo(x, y)
                    started = True
            p.setPen(QPen(QColor(s.color), 2))
            p.drawPath(path)
        p.setPen(QColor(theme.MUTED))
        p.drawText(QRectF(plot.right() - 120, plot.bottom() - 18, 116, 16),
                   Qt.AlignmentFlag.AlignRight, f"last {self.window_s:.0f} s")
        p.end()
