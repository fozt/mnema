from pathlib import Path

import pytest

from mnema.core.base import MediaError
from mnema.jobs import CANCEL_MESSAGE, JobStatus, run_job, safe_path
from mnema.store import SqliteJobStore

SERVER_FAILURE = "Сбой обработки на сервере. Подробности в журнале сервера"


def test_safe_path_allows_inside_root(tmp_path: Path):
    f = tmp_path / "a.mov"
    f.write_bytes(b"")
    assert safe_path(tmp_path, str(f)) == f.resolve()


def test_safe_path_rejects_outside_root(tmp_path: Path):
    with pytest.raises(MediaError):
        safe_path(tmp_path, "/etc/passwd")
    with pytest.raises(MediaError):
        safe_path(tmp_path, str(tmp_path / ".." / "x"))


def test_run_job_success_keeps_progress_and_result(tmp_path: Path):
    store = SqliteJobStore(tmp_path / "jobs.db")
    job = store.create(op="fake", path="/x", params={})

    def fake_op(path, params, on):
        on(0.5, "половина")
        return {"ok": True}

    run_job(job, fake_op)

    assert job.status is JobStatus.DONE
    assert job.result == {"ok": True}
    assert job.progress == 0.5 and job.message == "половина"
    assert job.error is None


@pytest.mark.parametrize(
    "exc, error",
    [
        (MediaError("сломалось"), "сломалось"),
        # not a MediaError, or an empty text: no exception class, details in the server log
        (RuntimeError("division by zero"), SERVER_FAILURE),
        (MediaError(), SERVER_FAILURE),
    ],
)
def test_run_job_error_is_captured(tmp_path: Path, exc, error):
    store = SqliteJobStore(tmp_path / "jobs.db")
    job = store.create(op="fake", path="/x", params={})

    def boom(path, params, on):
        raise exc

    run_job(job, boom)
    assert job.status is JobStatus.ERROR
    assert job.error == error
    assert job.error_kind == "failed"
    assert job.result is None


def test_sweep_running_closes_zombie_jobs(tmp_path: Path):
    """A runner restart mid-job: pending/running in SQLite becomes an honest ERROR (otherwise
    the client would poll RUNNING forever); finished jobs are left alone."""
    from mnema.jobs import JobStatus

    store = SqliteJobStore(tmp_path / "jobs.db")
    zombie = store.create(op="transcribe", path="/x/a.mp3", params={})
    zombie.status = JobStatus.RUNNING
    store.update(zombie)
    finished = store.create(op="mp3", path="/x/b.mp3", params={})
    finished.status = JobStatus.DONE
    finished.result = {"path": "/x/b.mp3"}
    store.update(finished)

    # a new instance, as the process after a restart
    fresh = SqliteJobStore(tmp_path / "jobs.db")
    assert fresh.sweep_running("перезапуск") == 1

    z = fresh.get(zombie.id)
    assert z is not None and z.status == JobStatus.ERROR and "перезапуск" in (z.error or "")
    d = fresh.get(finished.id)
    assert d is not None and d.status == JobStatus.DONE  # the finished result is intact


def test_server_startup_sweeps_zombies(tmp_path: Path):
    """create_app on an existing database closes the jobs a restart interrupted."""
    from fastapi.testclient import TestClient

    from mnema.jobs import JobStatus
    from mnema.server import create_app

    (tmp_path / ".mnema").mkdir()
    store = SqliteJobStore(tmp_path / ".mnema" / "jobs.db")
    job = store.create(op="transcribe", path="/x/a.mp3", params={})
    job.status = JobStatus.RUNNING
    store.update(job)

    app = create_app(tmp_path)  # a runner restart
    client = TestClient(app)
    got = client.get(f"/api/jobs/{job.id}").json()
    assert got["status"] == "error"
    assert "перезапустился" in got["error"]


@pytest.mark.parametrize(
    ("lang", "error"),
    [("ru", "Задача отменена клиентом."), ("en", "The client cancelled the job.")],
)
def test_cancel_reaches_recording_processing_in_either_language(tmp_path: Path, lang, error):
    """A cancel from run_job stops the recording processing on the node (FinalizeCancelled), not
    a processing failure, and the job error is the cancel text in the job's language."""
    from mnema.i18n import using
    from mnema.jobs import JobErrorKind, JobStatus, run_job
    from mnema.live.node_process import _progress
    from mnema.live.setup import FinalizeCancelled

    store = SqliteJobStore(tmp_path / "jobs.db")
    with using(lang):
        job = store.create(op="recording", path="/x/a.wav", params={})
    stopped = []

    def op(path, params, on):
        report = _progress(on)
        try:
            report(0.5, "")
        except FinalizeCancelled:
            stopped.append(True)
            raise MediaError(CANCEL_MESSAGE) from None
        return {"failed": True}

    run_job(job, op, cancelled=lambda: True)

    assert stopped == [True]
    assert (job.status, job.error, job.error_kind) == (
        JobStatus.ERROR,
        error,
        JobErrorKind.CANCELLED,
    )


def test_job_lifecycle_timestamps(tmp_path):
    """created/started/finished are filled: how long the job waited and how long it ran."""
    from mnema import jobs as jobs_mod

    store = SqliteJobStore(tmp_path / "jobs.db")
    job = store.create(op="transcribe", path="/x/a.mp3", params={})
    assert job.created_at > 0 and job.started_at is None and job.finished_at is None

    jobs_mod.run_job(job, lambda p, params, on: {"ok": 1})
    assert job.started_at is not None and job.finished_at is not None
    assert job.created_at <= job.started_at <= job.finished_at
