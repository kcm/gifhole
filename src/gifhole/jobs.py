"""A semi-durable in-process job queue.

Both OCR and page-scraping are too slow to run inside a request: a 40-GIF
scrape with a conversion each would hold the connection open for minutes. Jobs
run on a worker thread and the UI polls for status.

When configured with a database path, jobs are tracked in SQLite so that queued
and interrupted jobs survive server restarts, crashes, or code reloads.
In-memory mode (without a database path) remains supported for hermetic tests.
"""

from __future__ import annotations

import itertools
import json
import logging
import queue
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
    payload: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {**self.__dict__}


class JobQueue:
    """One worker thread draining a FIFO of callables or handler tasks."""

    def __init__(
        self, db_path: Path | str | None = None, keep: int = 40, workers: int = 1
    ) -> None:
        self._queue: queue.Queue[tuple[Job, Any]] = queue.Queue()
        self._jobs: dict[int, Job] = {}
        self._lock = threading.Lock()
        self._keep = keep
        self._db_path = Path(db_path) if db_path else None
        self._db: sqlite3.Connection | None = None
        self._handlers: dict[str, Callable[[Job, dict], Any]] = {}
        self._stopped = False
        if self._db_path:
            self._init_db()
        self._workers = [
            threading.Thread(target=self._run, daemon=True, name=f"gifhole-jobs-{i}")
            for i in range(max(1, workers))
        ]
        for w in self._workers:
            w.start()

    def _init_db(self) -> None:
        assert self._db_path is not None
        self._db = sqlite3.connect(self._db_path, check_same_thread=False, timeout=30.0)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.execute("PRAGMA busy_timeout = 5000")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS job_queue ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "kind TEXT NOT NULL, "
            "label TEXT NOT NULL, "
            "payload TEXT NOT NULL DEFAULT '{}', "
            "status TEXT NOT NULL DEFAULT 'queued', "
            "detail TEXT NOT NULL DEFAULT '', "
            "done INTEGER NOT NULL DEFAULT 0, "
            "total INTEGER NOT NULL DEFAULT 0, "
            "created_at REAL NOT NULL)"
        )
        self._db.commit()
        self._recover_and_load()

    def _row_to_job(self, row: sqlite3.Row) -> Job:
        try:
            payload = json.loads(row["payload"])
        except Exception:
            payload = {}
        return Job(
            id=row["id"],
            kind=row["kind"],
            label=row["label"],
            status=row["status"],
            detail=row["detail"],
            done=row["done"],
            total=row["total"],
            created_at=row["created_at"],
            payload=payload,
        )

    def _recover_and_load(self) -> None:
        if not self._db:
            return
        with self._lock:
            # Any job that was 'running' when the process stopped was interrupted.
            # Reset it to 'queued' so it can run to completion.
            self._db.execute("UPDATE job_queue SET status = 'queued' WHERE status = 'running'")
            self._db.commit()

            # Load recent finished jobs so list_jobs() / UI polls show them immediately.
            recent_rows = self._db.execute(
                "SELECT * FROM job_queue WHERE status IN ('done', 'error', 'cancelled') "
                "ORDER BY created_at DESC LIMIT ?",
                (self._keep,),
            ).fetchall()
            for r in recent_rows:
                job = self._row_to_job(r)
                self._jobs[job.id] = job

            # Enqueue pending jobs in FIFO order
            pending_rows = self._db.execute(
                "SELECT * FROM job_queue WHERE status = 'queued' ORDER BY id ASC"
            ).fetchall()
            for r in pending_rows:
                job = self._row_to_job(r)
                self._jobs[job.id] = job
                self._queue.put((job, None))

    def register_handler(self, kind: str, fn: Callable[[Job, dict], Any]) -> None:
        self._handlers[kind] = fn

    def submit(
        self,
        kind: str,
        label: str,
        fn: Callable[[Job], Any] | None = None,
        payload: dict | None = None,
    ) -> Job:
        """Queue `fn(job)` or a registered handler for `kind`."""
        payload_data = payload or {}
        now = time.time()
        with self._lock:
            if self._db:
                payload_str = json.dumps(payload_data)
                cur = self._db.execute(
                    "INSERT INTO job_queue "
                    "(kind, label, payload, status, detail, done, total, created_at) "
                    "VALUES (?, ?, ?, 'queued', '', 0, 0, ?)",
                    (kind, label, payload_str, now),
                )
                self._db.commit()
                job_id = cur.lastrowid
            else:
                job_id = next(_ids)

            job = Job(
                id=job_id,
                kind=kind,
                label=label,
                status="queued",
                created_at=now,
                payload=payload_data,
            )
            self._jobs[job.id] = job
            self._prune()

        self._queue.put((job, fn))
        return job

    def _sync_job(self, job: Job) -> None:
        if not self._db:
            return
        with self._lock:
            self._db.execute(
                "UPDATE job_queue SET status = ?, detail = ?, done = ?, total = ? WHERE id = ?",
                (job.status, job.detail, job.done, job.total, job.id),
            )
            self._db.commit()

    def _prune(self) -> None:
        finished = sorted(
            (j for j in self._jobs.values() if j.status in ("done", "error", "cancelled")),
            key=lambda j: j.created_at,
        )
        for job in finished[: max(len(finished) - self._keep, 0)]:
            self._jobs.pop(job.id, None)
        if self._db:
            self._db.execute(
                "DELETE FROM job_queue WHERE status IN ('done', 'error', 'cancelled') "
                "AND id NOT IN ("
                "  SELECT id FROM job_queue WHERE status IN ('done', 'error', 'cancelled') "
                "  ORDER BY created_at DESC LIMIT ?"
                ")",
                (self._keep,),
            )
            self._db.commit()

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
            if self._db:
                if kind is None:
                    self._db.execute(
                        "UPDATE job_queue SET status = 'cancelled', detail = 'cancelled' "
                        "WHERE status = 'queued'"
                    )
                else:
                    self._db.execute(
                        "UPDATE job_queue SET status = 'cancelled', detail = 'cancelled' "
                        "WHERE status = 'queued' AND kind = ?",
                        (kind,),
                    )
                self._db.commit()
        return stopped

    def _run(self) -> None:
        while not self._stopped:
            try:
                job, fn = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            # Cancelled while it sat in the queue.
            if job.status == "cancelled":
                self._sync_job(job)
                with self._lock:
                    self._prune()
                self._queue.task_done()
                continue
            job.status = "running"
            self._sync_job(job)
            try:
                if fn is not None:
                    result = fn(job)
                else:
                    # Allow up to 5s for handlers to register during startup
                    deadline = time.time() + 5.0
                    while job.kind not in self._handlers and time.time() < deadline:
                        if self._stopped:
                            return
                        time.sleep(0.02)
                    handler = self._handlers.get(job.kind)
                    if handler is None:
                        raise RuntimeError(f"no handler registered for job kind '{job.kind}'")
                    result = handler(job, job.payload)
                job.status = "done"
                if result:
                    job.detail = str(result)
            except Exception as exc:  # a bad job must not kill the worker
                log.exception("job %s failed", job.id)
                job.status = "error"
                job.detail = str(exc)
            finally:
                self._sync_job(job)
                with self._lock:
                    self._prune()
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

    def close(self) -> None:
        self._stopped = True
        for w in self._workers:
            if w.is_alive():
                w.join(timeout=1.0)
        with self._lock:
            if self._db:
                self._db.close()
                self._db = None
