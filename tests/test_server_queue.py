"""The queue and temporary inputs: HTTP contracts without real engines or network."""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from mnema import ops
from mnema.server import create_app


class Params(BaseModel):
    index: int = 0


def app_with(root, fn, *, output=ops.OutputKind.JSON):
    app = create_app(root)
    app.state.operations = {
        "fake": ops.OpSpec(
            name="fake", fn=fn, params=Params, input_kind=ops.InputKind.MEDIA, output_kind=output
        )
    }
    return app


def submit(client, path, index=0, key=None):
    body = {"op": "fake", "path": str(path), "params": {"index": index}}
    if key is not None:
        body["key"] = key
    return client.post("/api/jobs", json=body)


def finished(client, job_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "error"):
            return job
        time.sleep(0.005)
    pytest.fail(f"job {job_id} did not finish")


@pytest.fixture(autouse=True)
def queue_limits(monkeypatch):
    monkeypatch.setenv("MNEMA_JOB_WORKERS", "1")
    monkeypatch.setenv("MNEMA_JOB_QUEUE_SIZE", "2")


def test_queue_is_fifo_bounded_and_pending_has_no_start_time(tmp_path):
    started, release = threading.Event(), threading.Event()
    order = []

    def op(path, params, on):
        order.append(params["index"])
        if params["index"] == 0:
            started.set()
            assert release.wait(5)
        return {"index": params["index"]}

    app = app_with(tmp_path, op)
    client = TestClient(app)
    ids = []
    try:
        ids.append(submit(client, tmp_path / "a", 0).json()["id"])
        assert started.wait(3)
        for index in (1, 2):
            ids.append(submit(client, tmp_path / "a", index).json()["id"])
        assert submit(client, tmp_path / "a", 3).status_code == 429
        assert order == [0]
        for job_id in ids[1:]:
            job = client.get(f"/api/jobs/{job_id}").json()
            assert job["status"] == "pending"
            assert job["message"] == "В очереди"
            stored = app.state.store.get(job_id)
            assert stored.started_at is None and stored.finished_at is None
    finally:
        release.set()
        for job_id in ids:
            assert finished(client, job_id)["status"] == "done"
    assert order == [0, 1, 2]


def test_simultaneous_submissions_cannot_overfill_queue(tmp_path):
    started, release = threading.Event(), threading.Event()

    def op(path, params, on):
        started.set()
        assert release.wait(5)
        return {}

    app = app_with(tmp_path, op)
    client = TestClient(app)
    ids = [submit(client, tmp_path / "a").json()["id"]]
    try:
        assert started.wait(3)
        keys = [f"k{n}" for n in range(8)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda key: submit(client, tmp_path / "a", key=key), keys))
        ids += [r.json()["id"] for r in results if r.status_code == 200]
        assert [r.status_code for r in results].count(200) == 2
        assert [r.status_code for r in results].count(429) == 6
        stored = {key: app.state.store.find_by_key(key) for key in keys}
        accepted = {
            key: r.json()["id"]
            for key, r in zip(keys, results, strict=True)
            if r.status_code == 200
        }
        assert {key: job.id for key, job in stored.items() if job} == accepted
    finally:
        release.set()
        for job_id in ids:
            finished(client, job_id)


def test_configured_workers_run_in_parallel_without_pending_slots(tmp_path, monkeypatch):
    monkeypatch.setenv("MNEMA_JOB_WORKERS", "2")
    monkeypatch.setenv("MNEMA_JOB_QUEUE_SIZE", "0")
    entered = [threading.Event(), threading.Event()]
    release = threading.Event()

    def op(path, params, on):
        entered[params["index"]].set()
        assert release.wait(5)
        return {}

    client = TestClient(app_with(tmp_path, op))
    ids = []
    try:
        ids = [submit(client, tmp_path / "a", i).json()["id"] for i in range(2)]
        assert all(event.wait(3) for event in entered)
        assert submit(client, tmp_path / "a", 0).status_code == 429
    finally:
        release.set()
        for job_id in ids:
            finished(client, job_id)


def test_cancel_queued_job_frees_slot_and_never_invokes_operation(tmp_path):
    started, release = threading.Event(), threading.Event()
    executed = []

    def op(path, params, on):
        executed.append(params["index"])
        if params["index"] == 0:
            started.set()
            assert release.wait(5)
        return {}

    app = app_with(tmp_path, op)
    client = TestClient(app)
    ids = [submit(client, tmp_path / "a", 0).json()["id"]]
    try:
        assert started.wait(3)
        upload = client.post("/api/upload", files={"file": ("x.mp3", b"x")}).json()["path"]
        cancelled = submit(client, upload, 1).json()["id"]
        ids.append(submit(client, tmp_path / "a", 2).json()["id"])
        assert submit(client, tmp_path / "a", 3).status_code == 429
        assert client.post(f"/api/jobs/{cancelled}/cancel").json()["ok"] is True
        job = client.get(f"/api/jobs/{cancelled}").json()
        assert job["status"] == "error" and "отменена" in job["error"]
        stored = app.state.store.get(cancelled)
        assert stored.started_at is None and stored.finished_at is not None
        assert not Path(upload).parent.exists()
        assert cancelled not in app.state.cancels
        ids.append(submit(client, tmp_path / "a", 3).json()["id"])
    finally:
        release.set()
        for job_id in ids:
            finished(client, job_id)
    assert executed == [0, 2, 3]


def test_upload_has_one_consumer_and_sweep_keeps_active_and_queued_inputs(tmp_path):
    started, release = threading.Event(), threading.Event()
    read = []

    def op(path, params, on):
        if params["index"] == 0:
            started.set()
            assert release.wait(5)
        read.append(path.read_bytes())
        return {}

    client = TestClient(app_with(tmp_path, op))
    paths = [
        Path(client.post("/api/upload", files={"file": ("x", body)}).json()["path"])
        for body in (b"first", b"second", b"orphan")
    ]
    ids = []
    try:
        ids.append(submit(client, paths[0], 0).json()["id"])
        assert started.wait(3)
        ids.append(submit(client, paths[1], 1).json()["id"])
        for path in paths[:2]:
            assert submit(client, path).status_code == 409
        old = time.time() - 2 * 86400
        for path in paths:
            os.utime(path.parent, (old, old))
        assert client.post("/api/upload", files={"file": ("fresh", b"new")}).status_code == 200
        assert paths[0].exists() and paths[1].exists()
        assert not paths[2].parent.exists()
    finally:
        release.set()
        for job_id in ids:
            assert finished(client, job_id)["status"] == "done"
    assert read == [b"first", b"second"]
    assert submit(client, paths[0]).status_code == 404


def test_startup_sweeps_only_old_upload_directories(tmp_path):
    stale = tmp_path / "uploads/old"
    stale.mkdir(parents=True)
    (stale / "recording").write_bytes(b"x")
    fresh = tmp_path / "uploads/fresh"
    fresh.mkdir()
    recording = tmp_path / "recordings/call"
    recording.mkdir(parents=True)
    old = time.time() - 2 * 86400
    for folder in (stale, recording):
        os.utime(folder, (old, old))
    create_app(tmp_path)
    assert not stale.exists()
    assert fresh.exists() and recording.exists()


def test_failed_upload_removes_partial_file(tmp_path, monkeypatch):
    import mnema.api.files as files

    def broken_copy(source, dest):
        dest.write(b"partial")
        raise OSError("disk write failed")

    monkeypatch.setattr(files.shutil, "copyfileobj", broken_copy)
    client = TestClient(create_app(tmp_path), raise_server_exceptions=False)
    assert client.post("/api/upload", files={"file": ("x", b"audio")}).status_code == 500
    assert list((tmp_path / "uploads").iterdir()) == []


def test_sweep_keeps_an_upload_still_being_written_and_refreshes_its_ttl(tmp_path, monkeypatch):
    import mnema.api.files as files

    started, release = threading.Event(), threading.Event()
    real_copy = files.shutil.copyfileobj
    first = True

    def slow_copy(source, dest):
        nonlocal first
        if first:
            first = False
            started.set()
            assert release.wait(5)
        real_copy(source, dest)

    monkeypatch.setattr(files.shutil, "copyfileobj", slow_copy)
    client = TestClient(create_app(tmp_path))
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.post, "/api/upload", files={"file": ("slow", b"recording")})
        try:
            assert started.wait(3)
            folder = next((tmp_path / "uploads").iterdir())
            old = time.time() - 2 * 86400
            os.utime(folder, (old, old))
            assert client.post("/api/upload", files={"file": ("quick", b"x")}).status_code == 200
            assert folder.exists()
        finally:
            release.set()
        response = pending.result(timeout=3)
    assert response.status_code == 200
    assert Path(response.json()["path"]).read_bytes() == b"recording"
    assert folder.stat().st_mtime > old + 86400


def test_relocation_failure_of_uploaded_output_is_error_not_missing_success(tmp_path, monkeypatch):
    import mnema.api.jobs as jobs

    def op(path, params, on):
        result = path.with_suffix(".result")
        result.write_bytes(b"result")
        return {"path": str(result)}

    def broken_move(*args):
        raise OSError("output disk failed")

    monkeypatch.setattr(jobs.shutil, "move", broken_move)
    client = TestClient(app_with(tmp_path, op, output=ops.OutputKind.FILE))
    path = client.post("/api/upload", files={"file": ("input.mp3", b"audio")}).json()["path"]
    job_id = submit(client, path).json()["id"]
    job = finished(client, job_id)
    assert job["status"] == "error" and job["result"] is None
    assert "Не удалось сохранить файл результата" in job["error"]


def test_short_job_does_not_wait_for_long_one_and_goes_first() -> None:
    """A voice message never waits for an hour-long recording: its own slot beyond workers;
    short jobs go first in the shared queue, and the short slot never takes a long job."""
    from mnema.job_queue import JobQueue

    q = JobQueue(workers=1, pending=8)
    release_long = threading.Event()
    order: list[str] = []

    def job(name: str, gate: threading.Event | None = None):
        def run() -> None:
            if gate is not None:
                gate.wait(5)
            order.append(name)

        return run

    release_short = threading.Event()
    q.submit("long", job("long", release_long), lambda: None)
    q.submit("short1", job("short1", release_short), lambda: None, short=True)  # its own slot
    q.submit("long2", job("long2"), lambda: None)
    q.submit("short2", job("short2"), lambda: None, short=True)
    release_short.set()
    deadline = time.monotonic() + 5
    while "short2" not in order and time.monotonic() < deadline:
        time.sleep(0.01)
    assert order == ["short1", "short2"]  # both short ones while the long one still runs
    release_long.set()
    while len(order) < 4 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert order == ["short1", "short2", "long", "long2"]
