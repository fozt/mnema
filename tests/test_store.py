"""The SQLite job store: behaviour the contract tests do not express.

Surviving a restart is covered by the repo (test_repo.py) and HTTP client/server contracts
(test_client.py, test_server.py). Here: concurrency under WAL and evicting finished jobs from
the cache.
"""

from pathlib import Path

import pytest


def test_concurrent_readers_writers_no_crash(tmp_path: Path) -> None:
    """Concurrent reads and writes on the shared connection do not fail (WAL)."""
    import threading

    from mnema.store import SqliteJobStore

    js = SqliteJobStore(tmp_path / "jobs.db")
    job = js.create(op="x", path="/p", params={})

    errors: list[Exception] = []
    stop = threading.Event()

    def writer() -> None:
        i = 0
        while not stop.is_set():
            try:
                js.create(op="x", path=f"/p{i}", params={})
                js.update(job)
                i += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
                return

    def reader() -> None:
        while not stop.is_set():
            try:
                js.get(job.id)
                # the cache-miss path: reading an unknown id goes to SQLite
                js.get("missing-id")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
                return

    threads = [threading.Thread(target=writer) for _ in range(2)]
    threads += [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()
    # let the threads run long enough to catch a race
    import time

    time.sleep(1.0)
    stop.set()
    for t in threads:
        t.join()

    assert not errors, f"concurrent access raised errors: {errors[:3]}"


def test_job_store_evicts_terminal_from_cache(tmp_path: Path) -> None:
    """A finished job leaves _cache (otherwise Job.result with whole texts piles up).

    An unfinished one stays cached (run_job changes the live object); get() after eviction
    reads it from SQLite.
    """
    from mnema.jobs import JobStatus
    from mnema.store import SqliteJobStore

    s = SqliteJobStore(tmp_path / "jobs.db")
    job = s.create(op="transcribe", path="/p", params={})
    assert job.id in s._cache

    job.status = JobStatus.RUNNING
    s.update(job)
    assert job.id in s._cache  # still running: keep the live object

    job.status = JobStatus.DONE
    job.result = {"text": "большой результат"}
    s.update(job)
    assert job.id not in s._cache  # finished: evicted from memory
    got = s.get(job.id)  # still readable from SQLite
    assert (
        got is not None
        and got.status == JobStatus.DONE
        and got.result["text"] == "большой результат"
    )


def test_key_and_tracks_survive_restart(tmp_path):
    from mnema.store import SqliteJobStore

    db = tmp_path / "jobs.db"
    store = SqliteJobStore(db)
    tracks = {"mic": "/uploads/one/mic.wav", "system": "/uploads/two/system.wav"}
    job = store.create(op="recording", path="/node", params={}, tracks=tracks, key="retry")
    assert len(job.id) == 32
    assert store.find_by_key("retry") is job
    fresh = SqliteJobStore(db)
    assert fresh.find_by_key("retry").tracks == tracks
    assert fresh.find_by_key("absent") is None


def test_unique_key_is_atomic_across_store_instances(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from mnema.jobs import DuplicateJobKey
    from mnema.store import SqliteJobStore

    stores = [SqliteJobStore(tmp_path / "jobs.db") for _ in range(2)]

    def create(store):
        try:
            return store.create(op="recording", path="/node", params={}, key="same").id, True
        except DuplicateJobKey as exc:
            return exc.job_id, False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, stores))
    assert results[0][0] == results[1][0]
    assert sum(created for _, created in results) == 1


@pytest.mark.parametrize("status", ["pending", "running"])
def test_restart_marks_error_kind_and_finished_time_even_for_cached_job(tmp_path, status):
    import time

    from mnema.jobs import JobStatus
    from mnema.store import SqliteJobStore

    store = SqliteJobStore(tmp_path / "jobs.db")
    job = store.create(op="recording", path="/node", params={})
    job.status = JobStatus(status)
    store.update(job)
    before = time.time()
    assert store.sweep_running("перезапуск") == 1
    restarted = store.get(job.id)
    assert restarted.status == JobStatus.ERROR and restarted.error_kind == "restarted"
    assert before <= restarted.finished_at <= time.time()


def test_retention_uses_finish_time(tmp_path):
    import time

    from mnema.jobs import JobStatus
    from mnema.store import SqliteJobStore

    store = SqliteJobStore(tmp_path / "jobs.db")
    now = time.time()
    cases = [("done", 8), ("error", 8), ("done", 6), ("running", 8), ("pending", 8)]
    jobs = []
    for status, age in cases:
        job = store.create(op="x", path="", params={})
        job.status, job.created_at = JobStatus(status), now - 10 * 86400
        job.finished_at = now - age * 86400 if status in ("done", "error") else None
        store.update(job)
        jobs.append(job)
    assert set(store.sweep_finished(now - 7 * 86400)) == {jobs[0].id, jobs[1].id}
    assert all(store.get(job.id) is not None for job in jobs[2:])
    assert store.delete(jobs[-1].id) is True
    assert store.delete(jobs[-1].id) is False
    assert store.get(jobs[-1].id) is None
    store.update(jobs[-1])
    assert store.get(jobs[-1].id) is None
