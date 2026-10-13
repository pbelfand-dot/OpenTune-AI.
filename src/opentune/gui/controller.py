"""Telemetry polling on a worker thread, plus a helper to run blocking jobs off the UI thread."""

from __future__ import annotations

import time
import traceback
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Qt, QThread, QThreadPool, QTimer, Signal, Slot

from opentune.core.models import Sample
from opentune.datalog.logger import DataLogger
from opentune.telemetry.source import TelemetrySource


class _PollWorker(QObject):
    opened = Signal(list)
    sample = Signal(object)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, source: TelemetrySource, interval_ms: int) -> None:
        super().__init__()
        self.source = source
        self.interval_ms = interval_ms
        self._timer: QTimer | None = None

    @Slot()
    def start(self) -> None:
        try:
            self.source.open()
        except Exception as exc:  # adapter/port errors must surface in the UI
            self.failed.emit(f"Connection failed: {exc}")
            self.finished.emit()
            return
        self.opened.emit(self.source.channels())
        self._timer = QTimer()
        self._timer.timeout.connect(self._poll)
        self._timer.start(self.interval_ms)

    @Slot()
    def stop(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        try:
            self.source.close()
        except Exception:  # pragma: no cover - best effort on shutdown
            pass
        self.finished.emit()

    def _poll(self) -> None:
        try:
            self.sample.emit(self.source.read())
        except Exception as exc:
            if self._timer is not None:
                self._timer.stop()
            self.failed.emit(f"Telemetry error: {exc}")


class TelemetryController(QObject):
    """Owns the active telemetry source and its worker thread; also handles datalogging."""

    sample_received = Signal(object)
    connected = Signal(object)  # TelemetrySource
    disconnected = Signal()
    error = Signal(str)
    logging_changed = Signal(bool, str)
    _stop_requested = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.source: TelemetrySource | None = None
        self.channels: list[str] = []
        self.latest: Sample | None = None
        self._thread: QThread | None = None
        self._worker: _PollWorker | None = None
        self._logger: DataLogger | None = None
        self._rate_window: list[float] = []

    # ------------------------------------------------------------------ connection
    @property
    def is_connected(self) -> bool:
        return self.source is not None and self._thread is not None

    def connect_source(self, source: TelemetrySource, rate_hz: float = 20.0) -> None:
        self.disconnect_source()
        self.source = source
        self.latest = None
        thread = QThread()
        worker = _PollWorker(source, max(10, int(1000 / max(rate_hz, 1.0))))
        worker.moveToThread(thread)
        thread.started.connect(worker.start)
        worker.opened.connect(self._on_opened)
        worker.sample.connect(self._on_sample)
        worker.failed.connect(self._on_failed)
        # QThread.quit is thread-safe; call it directly from the worker thread so a UI thread
        # blocked in wait() during shutdown cannot deadlock on a queued quit.
        worker.finished.connect(thread.quit, Qt.ConnectionType.DirectConnection)
        self._stop_requested.connect(worker.stop)
        self._thread, self._worker = thread, worker
        thread.start()

    def disconnect_source(self) -> None:
        self.stop_logging()
        if self._thread is not None and self._worker is not None:
            if self._thread.isRunning():
                self._stop_requested.emit()
                self._thread.wait(5000)
            try:
                self._stop_requested.disconnect(self._worker.stop)
            except (RuntimeError, TypeError):  # pragma: no cover
                pass
            self._worker.deleteLater()
            self._thread.deleteLater()
        was_connected = self.source is not None
        self._thread = self._worker = None
        self.source = None
        self.channels = []
        if was_connected:
            self.disconnected.emit()

    def _on_opened(self, channels: list) -> None:
        self.channels = list(channels)
        self.connected.emit(self.source)

    def _on_failed(self, message: str) -> None:
        self.error.emit(message)

    def _on_sample(self, sample: Sample) -> None:
        self.latest = sample
        now = time.monotonic()
        self._rate_window.append(now)
        self._rate_window = [t for t in self._rate_window if now - t <= 2.0]
        if self._logger is not None:
            try:
                self._logger.write(sample)
            except (OSError, ValueError) as exc:
                self.error.emit(f"Logging stopped: {exc}")
                self.stop_logging()
        self.sample_received.emit(sample)

    @property
    def sample_rate(self) -> float:
        return len(self._rate_window) / 2.0

    # ------------------------------------------------------------------ logging
    @property
    def is_logging(self) -> bool:
        return self._logger is not None

    @property
    def log_rows(self) -> int:
        return self._logger.rows if self._logger else 0

    def start_logging(self, path: Path, notes: str = "") -> Path:
        if self.source is None:
            raise RuntimeError("Connect to a data source before logging")
        self.stop_logging()
        channels = self.channels or self.source.channels()
        self._logger = DataLogger(path, channels, self.source.profile.code, self.source.label, notes)
        self.logging_changed.emit(True, str(path))
        return path

    def stop_logging(self) -> Path | None:
        if self._logger is None:
            return None
        path = self._logger.path
        self._logger.close()
        self._logger = None
        self.logging_changed.emit(False, str(path))
        return path


class _JobSignals(QObject):
    done = Signal(object)
    failed = Signal(str)
    progress = Signal(float, str)


class _Job(QRunnable):
    def __init__(self, fn: Callable, signals: _JobSignals) -> None:
        super().__init__()
        self.fn = fn
        self.signals = signals

    def run(self) -> None:
        try:
            result = self.fn(self.signals.progress.emit)
        except Exception as exc:
            traceback.print_exc()
            self.signals.failed.emit(str(exc) or exc.__class__.__name__)
        else:
            self.signals.done.emit(result)


_active_jobs: set[_JobSignals] = set()


def run_async(fn: Callable[[Callable[[float, str], None]], object],
              on_done: Callable[[object], None] | None = None,
              on_error: Callable[[str], None] | None = None,
              on_progress: Callable[[float, str], None] | None = None) -> _JobSignals:
    """Run ``fn(progress_emit)`` on the global thread pool; callbacks run on the UI thread."""
    signals = _JobSignals()
    _active_jobs.add(signals)

    def finish(_=None) -> None:
        _active_jobs.discard(signals)

    if on_done:
        signals.done.connect(on_done)
    if on_error:
        signals.failed.connect(on_error)
    if on_progress:
        signals.progress.connect(on_progress)
    signals.done.connect(finish)
    signals.failed.connect(finish)
    QThreadPool.globalInstance().start(_Job(fn, signals))
    return signals
