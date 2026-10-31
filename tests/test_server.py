# tests/test_server.py
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from mnema import ops
from mnema.core.base import MediaError
from mnema.server import create_app, require_safe_bind


def _client(root: Path) -> TestClient:
    return TestClient(create_app(root))


@pytest.mark.parametrize(
    "extra",
    [
        {"url": "https://example.com/a"},
        {"urls": ["https://example.com/a"]},
        {"cookies": "secret"},
        {"api_key": "secret"},
    ],
)
def test_job_rejects_client_only_input_fields(tmp_path, extra):
    """Even with a valid path, URLs and secrets are refused and no job is created."""
    app = create_app(tmp_path)
    app.state.operations = {"fake": make_spec(lambda p, params, on: {})}
    source = tmp_path / "input.mp3"
    source.write_bytes(b"audio")
    response = TestClient(app).post(
        "/api/jobs", json={"op": "fake", "path": str(source), "key": "k", **extra}
    )
    assert response.status_code == 422
    assert app.state.store.find_by_key("k") is None
    assert source.read_bytes() == b"audio"
    assert not hasattr(app.state, "job_cookies")


@pytest.mark.parametrize("body", [{}, {"path": ""}, {"path": None}, {"url": "https://x/v"}])
def test_job_requires_nonempty_path(tmp_path, body):
    response = _client(tmp_path).post("/api/jobs", json={"op": "transcribe", **body})
    assert response.status_code == 422


def test_cloud_operations_require_server_model(tmp_path, monkeypatch):
    monkeypatch.delenv("MNEMA_MODEL", raising=False)
    app = create_app(tmp_path)
    spec = make_spec(lambda p, params, on: pytest.fail("cloud operation ran on server"))
    app.state.operations = {"cloud": spec.model_copy(update={"requires_model": True})}
    response = TestClient(app).post("/api/jobs", json={"op": "cloud", "path": "a.mp3", "key": "k"})
    assert response.status_code == 400
    assert response.json()["detail"] == "На сервере не настроена модель"
    assert app.state.store.find_by_key("k") is None


class _EmptyParams(BaseModel):
    model_config = {"extra": "forbid"}


def make_spec(fn, params: type[BaseModel] = _EmptyParams) -> ops.OpSpec:
    """An OpSpec wrapper for a stub op in server tests."""
    return ops.OpSpec(
        name="fake",
        fn=fn,
        params=params,
        input_kind=ops.InputKind.MEDIA,
        output_kind=ops.OutputKind.JSON,
    )


def make_file_spec(fn) -> ops.OpSpec:
    """An OpSpec with FILE output (like compress/mp3): the runner moves the result to outputs/."""
    return ops.OpSpec(
        name="fake",
        fn=fn,
        params=_EmptyParams,
        input_kind=ops.InputKind.MEDIA,
        output_kind=ops.OutputKind.FILE,
    )


def test_docs_and_openapi_disabled(tmp_path: Path):
    """The headless API serves no Swagger/ReDoc/OpenAPI: the auth middleware guards only
    /api/*, and a public runner has no reason to publish its schema."""
    client = _client(tmp_path)
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404


@pytest.mark.parametrize(
    "token, header, code",
    [
        ("s3cret", None, 401),
        ("s3cret", "Bearer s3cret", 200),
        ("s3cret", "Bearer wrong", 401),
        (None, None, 200),  # without MNEMA_TOKEN the server is open
    ],
)
def test_auth_blocks_api_without_credentials(tmp_path: Path, monkeypatch, token, header, code):
    if token:
        monkeypatch.setenv("MNEMA_TOKEN", token)
    else:
        monkeypatch.delenv("MNEMA_TOKEN", raising=False)
    client = _client(tmp_path)
    headers = {"Authorization": header} if header else {}
    assert client.get("/api/capabilities", headers=headers).status_code == code


@pytest.mark.parametrize(
    "host, token, refused",
    [
        ("127.0.0.1", None, False),
        ("localhost", None, False),
        ("::1", None, False),
        ("0.0.0.0", None, True),  # the API would be open to everyone
        ("0.0.0.0", "s3cret", False),
    ],
)
def test_require_safe_bind_rejects_public_host_without_token(host, token, refused):
    if refused:
        with pytest.raises(MediaError):
            require_safe_bind(host, token)
    else:
        require_safe_bind(host, token)


def _job_output(client: TestClient, data: bytes = b"0123456789") -> Path:
    """A file as a FILE job leaves it: root/outputs/<job-id>/<name>."""
    state = client.app.state  # type: ignore[attr-defined]
    job = state.store.create(op="compress", path="in.mov", params={})
    f = state.root / "outputs" / job.id / "a.mov"
    f.parent.mkdir(parents=True)
    f.write_bytes(data)
    return f


def test_download_serves_job_output(tmp_path: Path):
    """Serves a job result from outputs/; Range → 206 (Starlette)."""
    client = _client(tmp_path)
    f = _job_output(client)

    resp = client.get("/api/download", params={"path": str(f)})
    assert resp.status_code == 200
    assert resp.content == b"0123456789"

    ranged = client.get("/api/download", params={"path": str(f)}, headers={"Range": "bytes=0-3"})
    assert ranged.status_code == 206
    assert ranged.content == b"0123"


def test_download_refuses_everything_but_job_outputs(tmp_path: Path):
    """The node root is a recordings folder: the token must open only job results, not them all."""
    client = _client(tmp_path)
    result = _job_output(client)
    recording = tmp_path / "2026-10-22_Дейли" / "transcript.txt"
    recording.parent.mkdir()
    recording.write_text("секрет")
    upload = tmp_path / "uploads" / ("a" * 32) / "call.m4a"
    upload.parent.mkdir(parents=True)
    upload.write_bytes(b"audio")
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("чужое")
    (result.parent / "link.txt").symlink_to(recording)
    (tmp_path / "outputs" / "escape").symlink_to(outside.parent, target_is_directory=True)
    refused = [
        recording,
        upload,
        tmp_path / ".mnema" / "jobs.db",
        Path("/etc/passwd"),
        tmp_path / "outputs" / ".." / recording.parent.name / "transcript.txt",
        result.parent / "link.txt",
        tmp_path / "outputs" / "escape" / outside.name,
        tmp_path / "outputs",
    ]
    for path in refused:
        resp = client.get("/api/download", params={"path": str(path)})
        assert resp.status_code == 403, path
        assert resp.json()["detail"] == "Скачать можно только результат задачи"


def test_download_404_on_missing_job_output(tmp_path: Path):
    client = _client(tmp_path)
    resp = client.get("/api/download", params={"path": str(tmp_path / "outputs" / "j" / "x.mov")})
    assert resp.status_code == 404


def test_file_output_relocated_out_of_ephemeral_input_dir(tmp_path: Path):
    """A FILE op writes its result next to the input (in uploads/<uuid>/, which cleanup
    removes). The runner must move it to durable outputs/<job-id>/ before cleanup, or
    /api/download gets 404: the result is under outputs/, survives cleanup and downloads; the
    input is gone."""
    updir = tmp_path / "uploads" / "u123"
    updir.mkdir(parents=True)
    src = updir / "in.mov"
    src.write_bytes(b"")
    app = create_app(tmp_path)

    def fake(path, params, on):
        out = path.with_name(path.stem + "_compressed.bin")
        out.write_bytes(b"RESULT")
        on(1.0, "готово")
        return {"path": str(out)}

    app.state.operations = {"fake": make_file_spec(fake)}
    client = TestClient(app)
    job_id = client.post("/api/jobs", json={"op": "fake", "path": str(src), "params": {}}).json()[
        "id"
    ]

    for _ in range(50):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "error"):
            break
        time.sleep(0.02)
    assert job["status"] == "done"

    result_path = Path(job["result"]["path"])
    assert result_path.is_relative_to(tmp_path / "outputs")  # moved to the durable folder
    assert result_path.read_bytes() == b"RESULT"  # survived cleanup
    assert not updir.exists()  # the ephemeral input is gone
    dl = client.get("/api/download", params={"path": str(result_path)})
    assert dl.status_code == 200
    assert dl.content == b"RESULT"


def test_file_output_relocation_failure_keeps_job_done(tmp_path: Path, monkeypatch):
    """A failed move (e.g. a copy error across file systems) must NOT turn a successful FILE
    job into ERROR: delivery is best effort and the op did its work. The status stays DONE and
    the path is the original one (the op wrote under root, so the download still works)."""
    import mnema.api.jobs as jobs_mod

    src = tmp_path / "a.mov"
    src.write_bytes(b"")
    app = create_app(tmp_path)

    def fake(path, params, on):
        out = path.with_name(path.stem + "_compressed.bin")
        out.write_bytes(b"RESULT")
        return {"path": str(out)}

    def boom(*a, **k):
        raise OSError("cross-device move failed")

    monkeypatch.setattr(jobs_mod.shutil, "move", boom)
    app.state.operations = {"fake": make_file_spec(fake)}
    client = TestClient(app)
    job_id = client.post("/api/jobs", json={"op": "fake", "path": str(src), "params": {}}).json()[
        "id"
    ]

    for _ in range(50):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "error"):
            break
        time.sleep(0.02)
    assert job["status"] == "done"  # not ERROR
    assert job["result"]["path"] == str(src.with_name("a_compressed.bin"))  # the path is untouched


def test_job_rejects_path_outside_root(tmp_path: Path):
    app = create_app(tmp_path)
    app.state.operations = {"fake": make_spec(lambda p, params, on: {})}
    client = TestClient(app)
    resp = client.post("/api/jobs", json={"op": "fake", "path": "/etc/passwd", "params": {}})
    assert resp.status_code == 403


def test_job_unknown_op_400(tmp_path: Path):
    (tmp_path / "a.mov").write_bytes(b"")
    client = TestClient(create_app(tmp_path))
    resp = client.post(
        "/api/jobs", json={"op": "nope", "path": str(tmp_path / "a.mov"), "params": {}}
    )
    assert resp.status_code == 400


def test_create_job_invalid_params_returns_422(tmp_path: Path):
    """Invalid parameters (an extra or misspelt key) → 422 before a job is created."""
    (tmp_path / "a.mov").write_bytes(b"")

    class StrictParams(BaseModel):
        model_config = {"extra": "forbid"}
        flag: bool = False

    app = create_app(tmp_path)
    app.state.operations = {
        "fake": make_spec(lambda p, params, on: {}, params=StrictParams),
    }
    client = TestClient(app)
    resp = client.post(
        "/api/jobs",
        json={"op": "fake", "path": str(tmp_path / "a.mov"), "key": "k", "params": {"typo": 1}},
    )
    assert resp.status_code == 422
    assert app.state.store.find_by_key("k") is None


def test_upload_saves_under_unique_uploads_dir(tmp_path: Path):
    """An upload goes to uploads/<uuid>/<basename>; same-name uploads never overwrite each other."""
    client = TestClient(create_app(tmp_path))
    uploads = (tmp_path / "uploads").resolve()

    r1 = client.post("/api/upload", files={"file": ("a.mp3", b"data", "audio/mpeg")})
    assert r1.status_code == 200
    p1 = Path(r1.json()["path"])
    assert p1.is_file() and p1.read_bytes() == b"data"
    assert p1.name == "a.mp3"
    assert p1.parent.parent == uploads  # uploads/<uuid>/a.mp3

    # The same basename with other content gets its own uuid folder; both are kept.
    r2 = client.post("/api/upload", files={"file": ("a.mp3", b"other", "audio/mpeg")})
    p2 = Path(r2.json()["path"])
    assert p2 != p1
    assert p2.parent != p1.parent
    assert p1.read_bytes() == b"data" and p2.read_bytes() == b"other"


def test_upload_dir_removed_after_job(tmp_path: Path):
    """uploads/<uuid>/ is removed once the job ends (nothing piles up)."""
    app = create_app(tmp_path)
    app.state.operations = {"fake": make_spec(lambda p, params, on: {"ok": True})}
    client = TestClient(app)

    server_path = client.post(
        "/api/upload", files={"file": ("a.mp3", b"data", "audio/mpeg")}
    ).json()["path"]
    uuid_dir = Path(server_path).parent

    job_id = client.post(
        "/api/jobs", json={"op": "fake", "path": server_path, "params": {}}
    ).json()["id"]
    for _ in range(50):
        if client.get(f"/api/jobs/{job_id}").json()["status"] in ("done", "error"):
            break
        time.sleep(0.02)

    # the final status is written before the worker's finally cleanup: wait for the cleanup
    for _ in range(100):
        if not uuid_dir.exists():
            break
        time.sleep(0.02)
    assert not uuid_dir.exists()  # the upload's uuid folder is removed in finally
    assert not any((tmp_path / "uploads").iterdir())  # nothing piles up


def test_job_input_outside_uploads_not_deleted(tmp_path: Path):
    """A user's recording outside uploads/ stays after the job: only uploads are cleaned."""
    rec = tmp_path / "meeting.mp3"
    rec.write_bytes(b"data")
    app = create_app(tmp_path)
    app.state.operations = {"fake": make_spec(lambda p, params, on: {"ok": True})}
    client = TestClient(app)

    job_id = client.post("/api/jobs", json={"op": "fake", "path": str(rec), "params": {}}).json()[
        "id"
    ]
    for _ in range(50):
        if client.get(f"/api/jobs/{job_id}").json()["status"] in ("done", "error"):
            break
        time.sleep(0.02)

    assert rec.exists()  # the original recording is untouched


def test_cancel_endpoint_stops_running_job(tmp_path: Path):
    """POST /api/jobs/{id}/cancel: a running job stops at the next progress callback with the
    cancel error; a finished one is a no-op; an unknown one is 404."""
    import threading

    (tmp_path / "a.mov").write_bytes(b"")
    app = create_app(tmp_path)
    started = threading.Event()
    release = threading.Event()

    def slow(path, params, on):
        started.set()
        for i in range(200):
            release.wait(0.01)
            on(i / 200, "работаю…")  # a cooperative cancellation point
        return {"ok": 1}

    app.state.operations = {"fake": make_spec(slow)}
    client = TestClient(app)
    job_id = client.post(
        "/api/jobs", json={"op": "fake", "path": str(tmp_path / "a.mov"), "params": {}}
    ).json()["id"]

    assert started.wait(5)
    resp = client.post(f"/api/jobs/{job_id}/cancel")
    assert resp.status_code == 200 and resp.json()["ok"] is True
    release.set()

    for _ in range(100):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "error"):
            break
        time.sleep(0.02)
    assert job["status"] == "error"
    assert "отменена" in job["error"]

    # cancelling a finished job again is a no-op
    again = client.post(f"/api/jobs/{job_id}/cancel").json()
    assert again["ok"] is False

    assert client.post("/api/jobs/nope/cancel").status_code == 404


_CYRILLIC = re.compile("[А-Яа-яЁё]")
_EN = {"Accept-Language": "en-US,en;q=0.9"}


def _finished_job(client: TestClient, job_id: str, headers: dict | None = None) -> dict:
    for _ in range(200):
        job = client.get(f"/api/jobs/{job_id}", headers=headers).json()
        if job["status"] in ("done", "error"):
            return job
        time.sleep(0.01)
    pytest.fail("the job did not finish")


def test_server_answers_in_english_for_an_english_client(tmp_path, monkeypatch):
    """Refusals, job progress and errors, and the default node name follow Accept-Language."""
    monkeypatch.delenv("MNEMA_MODEL", raising=False)
    monkeypatch.setenv("MNEMA_LIBRARY", "true")
    (tmp_path / "library").mkdir()
    app = create_app(tmp_path)

    def failing(p, params, on):
        raise RuntimeError("boom")

    app.state.operations = {
        "fake": make_spec(failing),
        "cloud": make_spec(lambda p, params, on: {}).model_copy(update={"requires_model": True}),
    }
    client = TestClient(app)
    source = tmp_path / "input.mp3"
    source.write_bytes(b"audio")
    texts = [
        client.post("/api/jobs", json={"op": "nope", "path": str(source)}, headers=_EN),
        client.post("/api/jobs", json={"op": "cloud", "path": str(source)}, headers=_EN),
        client.get("/api/jobs/missing", headers=_EN),
        client.get("/api/uploads/" + "0" * 32, headers=_EN),
        client.get("/api/download", params={"path": str(source)}, headers=_EN),
    ]
    details = [response.json()["detail"] for response in texts]
    assert details == [
        "Unknown operation: nope",
        "No model is configured on the server",
        "Job not found",
        "Upload not found",
        "Only a job result can be downloaded",
    ]
    job_id = client.post("/api/jobs", json={"op": "fake", "path": str(source)}, headers=_EN).json()[
        "id"
    ]
    job = _finished_job(client, job_id)  # polled without a header: the job keeps its language
    assert job["error"] == "Processing failed on the server. Details are in the server log"
    assert client.get("/api/capabilities", headers=_EN).json()["name"] == "Server"
    assert client.get("/api/node/settings", headers=_EN).json()["name"] == "Server"
    rendered = [*details, job["error"], job["message"]]
    assert not [text for text in rendered if _CYRILLIC.search(text)]


def test_restart_closes_each_job_in_its_own_language(tmp_path):
    from mnema.store import SqliteJobStore

    store = SqliteJobStore(tmp_path / "jobs.db")
    english = store.create(op="x", path="a", params={}, lang="en")
    russian = store.create(op="x", path="a", params={}, lang="ru")
    from mnema.server import RESTARTED

    assert SqliteJobStore(tmp_path / "jobs.db").sweep_running(RESTARTED) == 2
    fresh = SqliteJobStore(tmp_path / "jobs.db")
    assert (fresh.get(english.id) or pytest.fail()).error == RESTARTED
    assert (fresh.get(russian.id) or pytest.fail()).error == (
        "Сервер перезапустился во время задачи. Запустите её снова"
    )


def test_client_errors_and_invitation_render_in_english(tmp_path):
    import httpx

    from mnema.client import MnemaClient, ServerError, UploadMissing
    from mnema.devices import issue_invite
    from mnema.i18n import using

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502 if "502" in request.url.path else 500, text="oops")

    client = MnemaClient("http://node", transport=httpx.MockTransport(handler))
    with using("en"):
        errors = []
        for path in ("/api/node/502", "/api/node/500"):
            with pytest.raises(ServerError) as caught:
                client._run(client._node_call("GET", path))
            errors.append(str(caught.value))
        errors.append(str(UploadMissing(None)))
        invite = issue_invite(tmp_path, "http://10.0.0.2:8000", now=time.time())
    assert errors == [
        "The server is restarting or unavailable",
        "The server returned error 500",
        "The upload is no longer available on the server",
    ]
    assert invite.startswith("Invitation for one device, valid until ")
    assert not _CYRILLIC.search(" ".join([*errors, invite]))
