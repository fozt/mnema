"""SQLite persistence: the runner's job store."""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from pathlib import Path

from .i18n import _, current, using
from .jobs import DuplicateJobKey, Job, JobErrorKind, JobStatus


class SqliteJobStore:
    """Jobs in SQLite with an in-memory cache.

    The cache keeps run_job and get() on the same Job object, so status and result changes
    are visible through get() at once. SQLite makes jobs survive a restart: a new store
    instance reads them back from the database.
    """

    def __init__(self, db_path: Path) -> None:
        self._lock = threading.Lock()
        self._con = sqlite3.connect(str(db_path), check_same_thread=False)
        # WAL: readers (status polls) never block the writer (run_job)
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.execute(
            "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL)"
        )
        self._con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS jobs_key ON jobs (json_extract(data, '$.key'))"
        )
        self._con.commit()
        # id → Job: the live objects of this process
        self._cache: dict[str, Job] = {}

    def create(
        self,
        *,
        op: str,
        path: str,
        params: dict,
        tracks: dict[str, str] | None = None,
        key: str | None = None,
        lang: str | None = None,
    ) -> Job:
        job = Job(
            id=uuid.uuid4().hex,
            op=op,
            path=path,
            params=params,
            tracks=tracks,
            key=key,
            lang=lang or current(),
        )
        with self._lock:
            try:
                self._con.execute(
                    "INSERT INTO jobs (id, data) VALUES (?, ?)",
                    (job.id, job.model_dump_json()),
                )
            except sqlite3.IntegrityError as exc:
                self._con.rollback()
                row = self._con.execute(
                    "SELECT id FROM jobs WHERE json_extract(data, '$.key') = ?", (key,)
                ).fetchone()
                if row is not None:
                    raise DuplicateJobKey(row[0]) from exc
                raise
            self._con.commit()
            self._cache[job.id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        # The shared connection, reads included, is locked: one sqlite3.Connection is not
        # thread-safe with check_same_thread=False.
        with self._lock:
            # the cache holds the live object run_job changes
            if job_id in self._cache:
                return self._cache[job_id]
            row = self._con.execute("SELECT data FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        return Job.model_validate_json(row[0])

    def find_by_key(self, key: str) -> Job | None:
        with self._lock:
            row = self._con.execute(
                "SELECT id, data FROM jobs WHERE json_extract(data, '$.key') = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            return self._cache.get(row[0]) or Job.model_validate_json(row[1])

    def delete(self, job_id: str) -> bool:
        with self._lock:
            result = self._con.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            self._con.commit()
            self._cache.pop(job_id, None)
            return result.rowcount > 0

    def sweep_finished(self, before: float) -> list[str]:
        """Delete jobs that finished before the given time."""
        with self._lock:
            rows = self._con.execute(
                "DELETE FROM jobs WHERE json_extract(data, '$.status') IN ('done', 'error') "
                "AND json_extract(data, '$.finished_at') < ? RETURNING id",
                (before,),
            ).fetchall()
            self._con.commit()
            ids = [row[0] for row in rows]
            for job_id in ids:
                self._cache.pop(job_id, None)
            return ids

    def sweep_running(self, error: str) -> int:
        """Unfinished jobs (pending/running) → ERROR, called when the runner starts.

        A process that died mid-job (restart, deploy, OOM) leaves RUNNING in SQLite forever and
        the client would poll it endlessly. ``error`` is the English source text, rendered in
        each job's language. Returns the number of closed jobs."""
        n = 0
        with self._lock:
            rows = self._con.execute("SELECT data FROM jobs").fetchall()
            for (data,) in rows:
                job = Job.model_validate_json(data)
                if job.status in (JobStatus.PENDING, JobStatus.RUNNING):
                    job.status = JobStatus.ERROR
                    with using(job.lang):
                        job.error = _(error)
                    job.error_kind = JobErrorKind.RESTARTED
                    job.finished_at = time.time()
                    self._con.execute(
                        "UPDATE jobs SET data = ? WHERE id = ?", (job.model_dump_json(), job.id)
                    )
                    self._cache.pop(job.id, None)
                    n += 1
            if n:
                self._con.commit()
        return n

    def update(self, job: Job) -> None:
        """Save the changes of an existing job."""
        with self._lock:
            self._con.execute(
                "UPDATE jobs SET data = ? WHERE id = ?",
                (job.model_dump_json(), job.id),
            )
            self._con.commit()
            # A finished job leaves the cache: SQLite is the source of truth for get(), and
            # otherwise the cache grows without bound (Job.result holds whole texts).
            if job.status in (JobStatus.DONE, JobStatus.ERROR):
                self._cache.pop(job.id, None)


__all__ = ["SqliteJobStore"]
