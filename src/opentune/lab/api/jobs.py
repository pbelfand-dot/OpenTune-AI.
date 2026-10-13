"""Background jobs (dyno runs, uncertainty, optimisation, test suites) with progress and cancel."""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

JobFn = Callable[[Callable[[float, str], None], Callable[[], bool]], Any]


class JobCancelled(Exception):
    pass


@dataclass
class Job:
    id: str
    kind: str
    project_id: str | None
    status: str = "queued"  # queued | running | done | failed | cancelled
    progress: float = 0.0
    message: str = ""
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    result: Any = None
    error: str | None = None
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)

    def to_dict(self, include_result: bool = True) -> dict:
        d = {"id": self.id, "kind": self.kind, "project_id": self.project_id, "status": self.status,
             "progress": round(self.progress, 4), "message": self.message, "created": self.created,
             "started": self.started, "finished": self.finished, "error": self.error}
        if include_result:
            d["result"] = self.result
        return d


class JobManager:
    def __init__(self, workers: int = 2, keep: int = 200) -> None:
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="lab-job")
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._keep = keep

    def submit(self, kind: str, fn: JobFn, project_id: str | None = None) -> Job:
        job = Job(uuid.uuid4().hex[:12], kind, project_id)
        with self._lock:
            self._jobs[job.id] = job
            if len(self._jobs) > self._keep:
                for jid in [j.id for j in sorted(self._jobs.values(), key=lambda j: j.created)
                            if j.status in ("done", "failed", "cancelled")][: len(self._jobs) - self._keep]:
                    self._jobs.pop(jid, None)

        def progress(fraction: float, message: str = "") -> None:
            job.progress = max(job.progress, min(1.0, fraction))
            job.message = message

        def cancelled() -> bool:
            return job._cancel.is_set()

        def runner() -> None:
            if job._cancel.is_set():
                job.status, job.finished = "cancelled", time.time()
                return
            job.status, job.started = "running", time.time()
            try:
                job.result = fn(progress, cancelled)
                job.status, job.progress = "done", 1.0
            except Exception as exc:  # noqa: BLE001 - reported to the client
                if job._cancel.is_set() or type(exc).__name__ in ("DynoCancelled", "JobCancelled"):
                    job.status = "cancelled"
                else:
                    job.status = "failed"
                    job.error = f"{type(exc).__name__}: {exc}"
                    job.message = traceback.format_exc(limit=3)
            finally:
                job.finished = time.time()

        self._pool.submit(runner)
        return job

    def get(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(f"Unknown job {job_id}")
        return job

    def list(self, project_id: str | None = None) -> list[Job]:
        with self._lock:
            jobs = list(self._jobs.values())
        if project_id:
            jobs = [j for j in jobs if j.project_id == project_id]
        return sorted(jobs, key=lambda j: -j.created)

    def cancel(self, job_id: str) -> Job:
        job = self.get(job_id)
        job._cancel.set()
        return job

    def wait(self, job_id: str, timeout: float = 60.0) -> Job:
        """Block until a job finishes (used by tests and the CLI)."""
        job = self.get(job_id)
        end = time.time() + timeout
        while job.status in ("queued", "running") and time.time() < end:
            time.sleep(0.02)
        return job

    def shutdown(self) -> None:
        for job in self.list():
            job._cancel.set()
        self._pool.shutdown(wait=False, cancel_futures=True)
