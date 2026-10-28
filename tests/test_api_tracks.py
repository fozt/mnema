"""Contract of two-track jobs, retries and how long results are kept."""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from mnema import core, ops
from mnema.api import jobs as api_jobs
from mnema.jobs import JobStatus
from mnema.server import create_app


class TrackParams(BaseModel):
    model_config = {"extra": "forbid"}
    tracks: dict[str, str] = Field(default_factory=dict)


@pytest.fixture
def node(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "probe", lambda path: SimpleNamespace(duration=400.0))
    app = create_app(tmp_path)
    app.state.operations["tracks"] = ops.OpSpec(
        name="tracks",
        fn=lambda path, params, on: {"root": str(path), "tracks": params["tracks"]},
        params=TrackParams,
        input_kind=ops.InputKind.TRACKS,
        output_kind=ops.OutputKind.JSON,
    )
    return app, TestClient(app)


def _uploads(client) -> dict[str, str]:
    return {
        label: client.post("/api/upload", files={"file": (f"{label}.wav", b"audio")}).json()["path"]
        for label in ("mic", "system")
    }


def _submit(client, tracks, **extra):
    return client.post("/api/jobs", json={"op": "tracks", "tracks": tracks, **extra})


def _finished(client, job_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "error"):
            return job
        time.sleep(0.01)
    pytest.fail("the job did not finish")


def _gone(tracks):
    deadline = time.monotonic() + 5
    while (
        any(Path(path).parent.exists() for path in tracks.values()) and time.monotonic() < deadline
    ):
        time.sleep(0.01)
    assert all(not Path(path).parent.exists() for path in tracks.values())


def test_two_tracks_use_server_paths_and_are_removed(node, tmp_path):
    app, client = node
    tracks = _uploads(client)
    response = _submit(client, tracks, params={"tracks": {"mic": "/etc/passwd"}})
    assert response.status_code == 200
    job_id = response.json()["id"]
    assert len(job_id) == 32 and int(job_id, 16) >= 0
    job = _finished(client, job_id)
    assert job["status"] == "done" and job["error_kind"] is None
    assert app.state.store.get(job_id).tracks == tracks
    assert job["result"] == {"root": str(tmp_path), "tracks": tracks}
    _gone(tracks)


@pytest.mark.parametrize("problem", ["busy", "missing"])
def test_second_claim_failure_releases_first_without_deleting(node, problem):
    app, client = node
    tracks = _uploads(client)
    if problem == "busy":
        app.state.uploads.claim(tracks["system"])
    else:
        Path(tracks["system"]).unlink()
    response = _submit(client, tracks)
    assert response.status_code == (409 if problem == "busy" else 404)
    assert response.json()["track"] == "system" and response.json()["detail"]
    assert Path(tracks["mic"]).read_bytes() == b"audio"
    retry = _submit(client, {"mic": tracks["mic"]})
    assert retry.status_code == 200
    assert _finished(client, retry.json()["id"])["status"] == "done"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"tracks": {}},
        {"tracks": {"other": "/x"}},
        {"tracks": {"mic": ""}},
        {"path": "/x", "tracks": {"mic": "/x"}},
        {"tracks": {"mic": "/x"}, "key": "x" * 129},
    ],
)
def test_track_request_validation(node, body):
    _, client = node
    response = client.post("/api/jobs", json={"op": "tracks", **body})
    assert response.status_code == 422


def test_track_operation_and_input_must_match(node):
    app, client = node
    tracks = _uploads(client)
    assert client.post("/api/jobs", json={"op": "tracks", "path": tracks["mic"]}).status_code == 422
    assert client.post("/api/jobs", json={"op": "transcribe", "tracks": tracks}).status_code == 422
    assert all(Path(path).exists() for path in tracks.values())


def test_tracks_reject_duplicate_and_escaped_paths(node, tmp_path):
    _, client = node
    tracks = _uploads(client)
    assert _submit(client, {"mic": tracks["mic"], "system": tracks["mic"]}).status_code == 422
    source = Path(tracks["mic"])
    alias = source.with_name("alias.wav")
    alias.symlink_to(source)
    assert _submit(client, {"mic": str(source), "system": str(alias)}).status_code == 422
    recording = tmp_path / "personal.wav"
    recording.write_bytes(b"private")
    link = source.with_name("outside.wav")
    link.symlink_to(recording)
    for path in (str(recording), str(link), str(tmp_path / "uploads" / ".." / "personal.wav")):
        assert _submit(client, {"mic": path}).status_code == 403
    assert _submit(client, {"mic": str(tmp_path / "uploads" / "loose.wav")}).status_code == 403
    assert recording.read_bytes() == b"private"


def test_retry_key_returns_finished_job_before_checking_deleted_uploads(node):
    app, client = node
    calls = []
    app.state.operations["tracks"].fn = lambda path, params, on: calls.append(path) or {"ok": True}
    tracks = _uploads(client)
    job_id = _submit(client, tracks, key="recording-1").json()["id"]
    assert _finished(client, job_id)["status"] == "done"
    _gone(tracks)
    assert _submit(client, tracks, key="recording-1").json() == {"id": job_id}
    assert _submit(client, {"mic": "/not-an-upload"}, key="recording-1").json() == {"id": job_id}
    assert len(calls) == 1


def test_concurrent_retries_enqueue_once(node):
    app, client = node
    app.state.job_queue.pending_limit = 0
    tracks = _uploads(client)
    release = threading.Event()
    calls = []

    def operation(path, params, on):
        calls.append(path)
        assert release.wait(5)
        return {"ok": True}

    app.state.operations["tracks"].fn = operation
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(lambda _: _submit(client, tracks, key="same-key"), range(8)))
        assert all(response.status_code == 200 for response in responses)
        ids = {response.json()["id"] for response in responses}
        assert len(ids) == len(calls) == 1
    finally:
        release.set()
    assert _finished(client, ids.pop())["status"] == "done"


def test_full_queue_keeps_both_uploads_for_retry(node):
    app, client = node
    app.state.job_queue.pending_limit = 0
    release = threading.Event()
    app.state.operations["tracks"].fn = lambda path, params, on: ({"ready": release.wait(5)})
    first = _submit(client, _uploads(client)).json()["id"]
    tracks = _uploads(client)
    try:
        assert _submit(client, tracks).status_code == 429
        assert all(Path(path).read_bytes() == b"audio" for path in tracks.values())
    finally:
        release.set()
    assert _finished(client, first)["status"] == "done"
    retry = _submit(client, tracks)
    assert retry.status_code == 200
    assert _finished(client, retry.json()["id"])["status"] == "done"


def test_symlinked_upload_root_does_not_admit_recordings(node, tmp_path):
    _, client = node
    private = tmp_path / "recordings"
    source = private / "call" / "mic.wav"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"recording")
    (tmp_path / "uploads").symlink_to(private, target_is_directory=True)
    response = _submit(client, {"mic": str(tmp_path / "uploads" / "call" / "mic.wav")})
    assert response.status_code == 403 and source.read_bytes() == b"recording"


@pytest.mark.parametrize("cancelled", [False, True])
def test_failure_and_running_cancellation_release_both_tracks(node, cancelled):
    app, client = node
    started, release = threading.Event(), threading.Event()

    def operation(path, params, on):
        started.set()
        assert release.wait(5)
        on(0.5, "работаю")
        raise RuntimeError("ошибка обработки")

    app.state.operations["tracks"].fn = operation
    tracks = _uploads(client)
    job_id = _submit(client, tracks).json()["id"]
    try:
        assert started.wait(5)
        assert client.delete(f"/api/jobs/{job_id}").status_code == 409
        if cancelled:
            assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 200
    finally:
        release.set()
    job = _finished(client, job_id)
    assert job["status"] == "error"
    assert job["error_kind"] == ("cancelled" if cancelled else "failed")
    _gone(tracks)


def test_pending_cancellation_and_short_lane_use_longest_track(node, monkeypatch):
    app, client = node
    monkeypatch.setattr(
        core,
        "probe",
        lambda path: SimpleNamespace(duration=400 if path.name == "system.wav" else 100),
    )
    started, release = threading.Event(), threading.Event()

    def operation(path, params, on):
        started.set()
        assert release.wait(5)
        return {"ok": True}

    app.state.operations["tracks"].fn = operation
    first = _submit(client, _uploads(client)).json()["id"]
    try:
        assert started.wait(5)
        waiting_tracks = _uploads(client)
        waiting = _submit(client, waiting_tracks).json()["id"]
        assert client.get(f"/api/jobs/{waiting}").json()["status"] == "pending"
        assert client.delete(f"/api/jobs/{waiting}").status_code == 409
        assert client.post(f"/api/jobs/{waiting}/cancel").status_code == 200
        assert _finished(client, waiting)["error_kind"] == "cancelled"
        _gone(waiting_tracks)
        started.clear()
        monkeypatch.setattr(core, "probe", lambda path: SimpleNamespace(duration=200))
        short = _submit(client, _uploads(client)).json()["id"]
        assert started.wait(5)
        assert client.get(f"/api/jobs/{short}").json()["status"] == "running"
    finally:
        release.set()
    assert _finished(client, first)["status"] == "done"
    assert _finished(client, short)["status"] == "done"


def test_delete_finished_job_removes_result_and_key(node, tmp_path):
    app, client = node
    job_id = _submit(client, _uploads(client), key="finished").json()["id"]
    assert _finished(client, job_id)["status"] == "done"
    output = tmp_path / "outputs" / job_id
    output.mkdir(parents=True)
    (output / "result.txt").write_text("result")
    app.state.cancels.add(job_id)
    assert client.delete(f"/api/jobs/{job_id}").json() == {"ok": True}
    assert client.get(f"/api/jobs/{job_id}").status_code == 404
    assert client.delete(f"/api/jobs/{job_id}").status_code == 404
    assert not output.exists() and job_id not in app.state.cancels
    assert app.state.store.find_by_key("finished") is None


def test_delete_job_output_symlink_keeps_target(node, tmp_path):
    app, client = node
    job = app.state.store.create(op="x", path="", params={})
    job.status = JobStatus.DONE
    app.state.store.update(job)
    target = tmp_path / "recordings"
    target.mkdir()
    (target / "recording.wav").write_bytes(b"audio")
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    (outputs / job.id).symlink_to(target, target_is_directory=True)
    assert client.delete(f"/api/jobs/{job.id}").status_code == 200
    assert (target / "recording.wav").read_bytes() == b"audio"


def test_hourly_sweep_removes_old_results_and_idle_uploads(node, tmp_path):
    app, client = node
    now = time.time()

    def old_job():
        job = app.state.store.create(op="x", path="", params={})
        job.status, job.finished_at = JobStatus.DONE, now - 8 * 86400
        app.state.store.update(job)
        output = tmp_path / "outputs" / job.id
        output.mkdir(parents=True)
        (output / "result.txt").write_text("text")
        return job, output

    old, output = old_job()
    uploads = _uploads(client)
    for path in uploads.values():
        os.utime(Path(path).parent, (now - 2 * 86400, now - 2 * 86400))
    app.state.uploads.claim(uploads["mic"])
    app.state.cancels.add(old.id)
    assert client.get(f"/api/jobs/{old.id}").status_code == 404
    assert not output.exists() and old.id not in app.state.cancels
    assert Path(uploads["mic"]).exists() and not Path(uploads["system"]).exists()
    later, later_output = old_job()
    assert client.get(f"/api/jobs/{later.id}").status_code == 200
    assert later_output.exists()
    api_jobs.sweep(app, now + 3601)
    assert app.state.store.get(later.id) is None and not later_output.exists()
