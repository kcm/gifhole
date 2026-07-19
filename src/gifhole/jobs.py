"""A minimal in-process job queue.

Both OCR and page-scraping are too slow to run inside a request: a 40-GIF
scrape with a conversion each would hold the connection open for minutes. Jobs
run on a worker thread and the UI polls for status.

Deliberately not durable. Jobs are lost on restart, which is fine, because every job
is re-derivable from the folder, and the alternative is a scheduler this app
does not need.
"""

from __future__ import annotations

import itertools
import logging
import queue
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

_ids = itertools.count(1)


@dataclass
class Job:
    id: int
    kind: str
    label: str
    status: str = "queued"  # queued | running | done | error | cancelled
    detail: str = ""
    done: int = 0
    total: int = 0
    created_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {**self.__dict__}


class JobQueue:
    """One worker thread draining a FIFO of callables."""

    def __init__(self, keep: int = 40) -> None:
        self._queue: queue.Queue[tuple[Job, object]] = queue.Queue()
        self._jobs: dict[int, Job] = {}
        self._lock = threading.Lock()
        self._keep = keep
        self._worker = threading.Thread(target=self._run, daemon=True, name="gifhole-jobs")
        self._worker.start()

    def submit(self, kind: str, label: str, fn) -> Job:
        """Queue `fn(job)`; it may update `job.done` / `job.total` as it goes."""
        job = Job(id=next(_ids), kind=kind, label=label)
        with self._lock:
            self._jobs[job.id] = job
            self._prune()
        self._queue.put((job, fn))
        return job

    def _prune(self) -> None:
        finished = sorted(
            (j for j in self._jobs.values() if j.status in ("done", "error", "cancelled")),
            key=lambda j: j.created_at,
        )
        for job in finished[: max(len(finished) - self._keep, 0)]:
            self._jobs.pop(job.id, None)

    def cancel(self, kind: str | None = None) -> int:
        """Drop everything still queued, optionally only of one kind.

        The job already running is left alone. It is usually mid-API-call or
        mid-download, and killing a worker thread cleanly is not worth the
        complexity when the useful promise is "stop spending on the other 150",
        not "stop this instant". Marked rather than removed from the queue, so
        the worker skips them and the strip can still show what was cancelled.
        """
        stopped = 0
        with self._lock:
            for job in self._jobs.values():
                if job.status == "queued" and (kind is None or job.kind == kind):
                    job.status = "cancelled"
                    job.detail = "cancelled"
                    stopped += 1
        return stopped

    def _run(self) -> None:
        while True:
            job, fn = self._queue.get()
            # Cancelled while it sat in the queue.
            if job.status == "cancelled":
                self._queue.task_done()
                continue
            job.status = "running"
            try:
                result = fn(job)
                job.status = "done"
                if result:
                    job.detail = str(result)
            except Exception as exc:  # a bad job must not kill the worker
                log.exception("job %s failed", job.id)
                job.status = "error"
                job.detail = str(exc)
            finally:
                self._queue.task_done()

    def list_jobs(self) -> list[Job]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    def get(self, job_id: int) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def active(self) -> int:
        return sum(1 for j in self.list_jobs() if j.status in ("queued", "running"))

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """Block until the queue drains. Tests only; the UI polls instead."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.active() == 0:
                return True
            time.sleep(0.02)
        return False
