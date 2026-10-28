# tests/test_client.py
"""MnemaClient over an ASGI transport, without a real server."""

from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel

from mnema import ops
from mnema.server import create_app


class _EmptyParams(BaseModel):
    model_config = {"extra": "forbid"}


def make_spec(fn) -> ops.OpSpec:
    """OpSpec for a stand-in op."""
    return ops.OpSpec(
        name="fake",
        fn=fn,
        params=_EmptyParams,
        input_kind=ops.InputKind.MEDIA,
        output_kind=ops.OutputKind.JSON,
    )


def make_client(app, base_url: str = "http://test", token: str | None = None):
    """MnemaClient over httpx.ASGITransport: no network connections."""
    from mnema.client import MnemaClient

    transport = httpx.ASGITransport(app=app)
    return MnemaClient(base_url, token=token, transport=transport)


def test_client_upload_reports_progress(tmp_path: Path):
    """upload(on=…) reports the fraction of bytes sent: a large recording without progress
    looks stuck while it is actually uploading."""
    app = create_app(tmp_path)
    src = tmp_path / "rec.mp3"
    src.write_bytes(b"x" * 300_000)  # several httpx chunks

    fractions: list[float] = []

    def on(frac, msg):
        if frac is not None:
            fractions.append(frac)
            assert msg == "Отправляю на сервер…"

    c = make_client(app)
    server_path = c.upload(src, on=on)

    assert Path(server_path).stat().st_size == 300_000  # the whole file arrived
    assert fractions and fractions[-1] == pytest.approx(1.0)  # progress reached the end
    assert fractions == sorted(fractions)  # fractions only grow


def test_client_run_survives_runner_restart_window(tmp_path: Path):
    """Polling survives 502/503 (a proxy or a runner restart) and waits for the result instead
    of failing on the first refusal."""

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/jobs":
            return httpx.Response(200, json={"id": "j1"})
        calls["n"] += 1
        if calls["n"] <= 2:  # the first two polls hit a gateway without the runner
            return httpx.Response(502, text="")
        return httpx.Response(200, json={"id": "j1", "status": "done", "result": {"ok": 1}})

    from mnema.client import MnemaClient

    c = MnemaClient("http://test", transport=httpx.MockTransport(handler))
    messages: list[str] = []
    result = c.run("fake", "/data/x.mp3", {}, on=lambda f, m: messages.append(m), poll_interval=0)

    assert result == {"ok": 1}
    assert any("Сервер недоступен, жду…" in m for m in messages)  # the restart window is announced


def test_client_job_create_waits_out_runner_restart(tmp_path: Path, monkeypatch):
    """A ConnectError on POST /api/jobs (a runner restart during a deploy) is waited out and the
    job is created once the runner is back, so a batch does not fail."""
    import mnema.client as client_mod

    monkeypatch.setattr(client_mod, "_RESTART_POLL_S", 0.0)
    monkeypatch.setattr(client_mod, "_RESTART_WAIT_S", 90.0)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/jobs":
            calls["n"] += 1
            if calls["n"] <= 2:  # the first attempts: the runner is still restarting
                raise httpx.ConnectError("connection refused", request=request)
            return httpx.Response(200, json={"id": "j1"})
        return httpx.Response(200, json={"id": "j1", "status": "done", "result": {"ok": 1}})

    from mnema.client import MnemaClient

    c = MnemaClient("http://test", transport=httpx.MockTransport(handler))
    messages: list[str] = []
    result = c.run("fake", "/data/x.mp3", {}, on=lambda f, m: messages.append(m), poll_interval=0)

    assert result == {"ok": 1}
    assert calls["n"] == 3
    assert any("Сервер недоступен, жду…" in m for m in messages)


def test_client_job_create_does_not_retry_after_send(tmp_path: Path, monkeypatch):
    """A drop after POST /api/jobs was sent (ReadTimeout) is not retried: the request may have
    arrived, and a retry would create a second job."""
    import mnema.client as client_mod

    monkeypatch.setattr(client_mod, "_RESTART_POLL_S", 0.0)
    monkeypatch.setattr(client_mod, "_RESTART_WAIT_S", 90.0)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/jobs":
            calls["n"] += 1
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200, json={})

    from mnema.client import MnemaClient

    c = MnemaClient("http://test", transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.ReadTimeout):
        c.run("fake", "/data/x.mp3", {}, poll_interval=0)
    assert calls["n"] == 1  # exactly one attempt


def test_client_gateway_error_message_is_human(tmp_path: Path):
    """An empty 502 body from a proxy gives a clear reason, not "HTTP 502:"."""
    from mnema.client import MnemaClient
    from mnema.core.base import MediaError

    c = MnemaClient(
        "http://test",
        transport=httpx.MockTransport(lambda r: httpx.Response(502, text="")),
    )
    with pytest.raises(MediaError, match="перезапускается|недоступен"):
        c.asr_choices()
    for answer, text in (
        (
            httpx.Response(422, json={"detail": "На сервере нет ключа Groq"}),
            "На сервере нет ключа Groq",
        ),
        (httpx.Response(500, text="Internal Server Error"), "Сервер ответил ошибкой 500"),
    ):
        c = MnemaClient("http://test", transport=httpx.MockTransport(lambda r, a=answer: a))
        with pytest.raises(MediaError) as caught:
            c.asr_choices()
        assert str(caught.value) == text  # the server's detail as is, without the code and JSON


def test_client_health_reports_reachability_without_waiting(tmp_path: Path):
    """health(): a live runner gives a dict (ok/llm); 404 (not a Mnema server), no answer or
    5xx give None, without retries or waiting for a restart (answer within 2 s)."""
    from mnema.client import MnemaClient

    app = create_app(tmp_path)
    alive = make_client(app).health()
    assert (
        alive and alive["ok"] is True and alive["llm"] is True
    )  # the server's model comes from conftest

    not_mnema = MnemaClient(
        "http://test", transport=httpx.MockTransport(lambda r: httpx.Response(404))
    )
    assert not_mnema.health() is None

    def down(request):
        raise httpx.ConnectError("connection refused", request=request)

    assert MnemaClient("http://test", transport=httpx.MockTransport(down)).health() is None
    unauth = MnemaClient(
        "http://test", transport=httpx.MockTransport(lambda r: httpx.Response(401))
    )
    assert unauth.health() == {"ok": True, "auth": False}  # alive with a wrong token is not down
    broken = MnemaClient(
        "http://test", transport=httpx.MockTransport(lambda r: httpx.Response(502))
    )
    assert broken.health() is None


def test_client_downloads_the_file_result_of_a_remote_job(tmp_path: Path):
    """Like the CLI with --runner (compress, mp3): upload, a FILE job, download of the result."""
    root = tmp_path / "root"
    app = create_app(root)

    def compress(path, params, on):
        out = path.with_name(path.stem + "_compressed.mov")
        out.write_bytes(path.read_bytes()[::-1])
        return {"path": str(out)}

    app.state.operations = {
        "fake": make_spec(compress).model_copy(update={"output_kind": ops.OutputKind.FILE})
    }
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"video-bytes")
    c = make_client(app)
    result = c.run("fake", c.upload(src))

    dest = tmp_path / "out" / "clip_compressed.mov"
    got = c.download(result["path"], dest)

    assert got == dest
    assert dest.read_bytes() == b"setyb-oediv"


def test_client_download_missing_file_raises(tmp_path: Path):
    """download() of a missing result raises MediaError (404)."""
    from mnema.core.base import MediaError

    app = create_app(tmp_path)
    c = make_client(app)
    with pytest.raises(MediaError):
        c.download(str(tmp_path / "outputs" / "j" / "nope.jpg"), tmp_path / "x.jpg")


def test_client_upload_unique_dir_no_collision(tmp_path: Path):
    """Two uploads of same-named files with different content never overwrite each other."""
    app = create_app(tmp_path)
    c = make_client(app)

    a = tmp_path / "a" / "talk.mov"
    a.parent.mkdir()
    a.write_bytes(b"AAAAA")
    b = tmp_path / "b" / "talk.mov"
    b.parent.mkdir()
    b.write_bytes(b"BBBBB")  # same basename, different content

    p1 = Path(c.upload(a))
    p2 = Path(c.upload(b))

    assert p1 != p2 and p1.parent != p2.parent  # different uuid folders
    assert p1.read_bytes() == b"AAAAA" and p2.read_bytes() == b"BBBBB"
    assert p1.parent.parent == (tmp_path / "uploads").resolve()  # uploads/<uuid>/ under root


def test_client_run_error_raises_media_error(tmp_path: Path):
    """run() raises MediaError when the operation failed."""
    from mnema.core.base import MediaError

    app = create_app(tmp_path)

    def bad_op(p, params, on):
        raise RuntimeError("что-то пошло не так")

    app.state.operations = {"bad": make_spec(bad_op)}
    (tmp_path / "a.mp3").write_bytes(b"x")

    c = make_client(app)
    with pytest.raises(MediaError):
        c.run("bad", str(tmp_path / "a.mp3"))


def test_client_run_calls_on_callback(tmp_path: Path):
    """run() calls on() at least once while polling."""
    calls: list[tuple] = []

    def fake_op(p, params, on):
        on(0.5, "в процессе")
        return {"done": True}

    app = create_app(tmp_path)
    app.state.operations = {"prog": make_spec(fake_op)}
    (tmp_path / "a.mp3").write_bytes(b"x")

    def collector(fraction, message):
        calls.append((fraction, message))

    c = make_client(app)
    result = c.run("prog", str(tmp_path / "a.mp3"), on=collector)
    assert result == {"done": True}
    assert len(calls) > 0


@pytest.mark.parametrize("token", ["s3cret", None])
def test_client_bearer_token_passes_auth(tmp_path: Path, monkeypatch, token):
    """MnemaClient(token=...) sends Bearer and gets into a token-protected server; without a
    token the server answers 401 → MediaError."""
    from mnema import node_settings
    from mnema.core.base import MediaError

    monkeypatch.setenv("MNEMA_TOKEN", "s3cret")
    app = create_app(tmp_path)
    c = make_client(app, token=token)
    if token:
        assert c.asr_choices() == node_settings.asr_choices()
    else:
        with pytest.raises(MediaError):
            c.asr_choices()


# ─────────────────────── MNEMA_CA: trusting your own CA ───────────────


@pytest.mark.parametrize(
    "ca, verify", [("/etc/mnema/root.crt", "/etc/mnema/root.crt"), (None, True)]
)
def test_client_uses_mnema_ca_for_verify(monkeypatch, ca, verify):
    """With MNEMA_CA the client uses verify=<path> (trusts that CA); without it the httpx
    default (verify=True); verify=False is never used."""
    from unittest.mock import patch

    from mnema.client import MnemaClient

    if ca:
        monkeypatch.setenv("MNEMA_CA", ca)
    else:
        monkeypatch.delenv("MNEMA_CA", raising=False)
    with patch("httpx.AsyncClient") as mock_ac:
        MnemaClient("https://203.0.113.10")._make_async_client()
    assert mock_ac.call_args.kwargs["verify"] == verify


# ─────────────────────── timeouts and retries on transient drops ──────────────


def test_client_upload_retries_transient_transport_error(tmp_path: Path, monkeypatch):
    """upload() survives one transient drop and still succeeds."""
    from mnema.client import MnemaClient

    app = create_app(tmp_path)
    src = tmp_path / "rec.mp3"
    src.write_bytes(b"audio-bytes")

    transport = httpx.ASGITransport(app=app)
    c = MnemaClient("http://test", transport=transport)
    c._retry_backoff = 0

    real_post = httpx.AsyncClient.post
    state = {"n": 0}

    async def flaky_post(self, *args, **kwargs):
        state["n"] += 1
        if state["n"] == 1:
            raise httpx.WriteError("обрыв на передаче")
        return await real_post(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "post", flaky_post)

    server_path = c.upload(src)
    assert Path(server_path).read_bytes() == b"audio-bytes"
    assert state["n"] == 2


def test_client_download_retries_transient_transport_error(tmp_path: Path, monkeypatch):
    """download() survives one transient drop and still succeeds."""
    from mnema.client import MnemaClient

    root = tmp_path / "root"
    src = root / "outputs" / "job1" / "clip.mp4"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"video-bytes")

    app = create_app(root)
    transport = httpx.ASGITransport(app=app)
    c = MnemaClient("http://test", transport=transport)
    c._retry_backoff = 0

    real_stream = httpx.AsyncClient.stream
    state = {"n": 0}

    def flaky_stream(self, *args, **kwargs):
        state["n"] += 1
        if state["n"] == 1:
            raise httpx.ReadError("обрыв на скачивании")
        return real_stream(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "stream", flaky_stream)

    dest = tmp_path / "out" / "clip.mp4"
    got = c.download(str(src), dest)
    assert got == dest
    assert dest.read_bytes() == b"video-bytes"
    assert state["n"] == 2


def test_client_run_poll_retries_transient_transport_error(tmp_path: Path, monkeypatch):
    """run() survives one transient drop while polling the job status."""
    from mnema.client import MnemaClient

    def fake_op(p, params, on):
        return {"ok": True}

    app = create_app(tmp_path)
    app.state.operations = {"fake": make_spec(fake_op)}
    (tmp_path / "a.mp3").write_bytes(b"x")

    transport = httpx.ASGITransport(app=app)
    c = MnemaClient("http://test", transport=transport)
    c._retry_backoff = 0

    real_get = httpx.AsyncClient.get
    state = {"n": 0}

    async def flaky_get(self, *args, **kwargs):
        state["n"] += 1
        if state["n"] == 1:
            raise httpx.ReadTimeout("обрыв при опросе статуса")
        return await real_get(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "get", flaky_get)

    res = c.run("fake", str(tmp_path / "a.mp3"))
    assert res == {"ok": True}
    assert state["n"] >= 2


def test_cancel_active_job_posts_cancel_with_auth(monkeypatch):
    """cancel_active_job() sends an authorized POST /cancel to the active job and clears it."""
    import mnema.client as client_mod

    sent: dict = {}

    def fake_post(url, headers=None, timeout=None, verify=None):
        sent.update(url=url, headers=headers)

        class R:
            status_code = 200

        return R()

    monkeypatch.setattr(client_mod.httpx, "post", fake_post)
    client_mod._active_job = {
        "base_url": "https://runner",
        "headers": {"Authorization": "Bearer s3cret"},
        "verify": True,
        "job_id": "j42",
    }
    assert client_mod.cancel_active_job() == "j42"
    assert sent["url"] == "https://runner/api/jobs/j42/cancel"
    assert sent["headers"]["Authorization"] == "Bearer s3cret"
    assert client_mod._active_job is None
    assert client_mod.cancel_active_job() is None  # nothing left to cancel the second time


def test_failed_stream_preserves_previous_download_and_removes_partial(tmp_path):
    from mnema.client import MnemaClient

    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"incomplete"
            raise httpx.ReadError("connection lost")

    client = MnemaClient(
        "http://test", transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Broken()))
    )
    client._retry_backoff = 0
    dest = tmp_path / "result.txt"
    dest.write_bytes(b"previous complete result")
    with pytest.raises(httpx.ReadError):
        client.download("/result.txt", dest)
    assert dest.read_bytes() == b"previous complete result"
    assert list(tmp_path.iterdir()) == [dest]


# ─────────────────────── server details ───────────────────────────


def test_keyed_job_recovers_lost_submit_response(monkeypatch):
    """A retry after a lost answer uses the same key and both tracks."""
    import json

    from mnema.client import MnemaClient

    posted = []

    def handle(request):
        posted.append(json.loads(request.content))
        if len(posted) == 1:
            raise httpx.ReadError("response lost", request=request)
        return httpx.Response(200, json={"id": "one-job"})

    client = MnemaClient("http://node", transport=httpx.MockTransport(handle))
    client._retry_backoff = 0
    assert (
        client.submit(
            "recording",
            key="same-key",
            tracks={"mic": "/mic.wav", "system": "/system.wav"},
            params={"asr": "auto"},
        )
        == "one-job"
    )
    assert (
        posted
        == [
            {
                "op": "recording",
                "key": "same-key",
                "tracks": {"mic": "/mic.wav", "system": "/system.wav"},
                "params": {"asr": "auto"},
            }
        ]
        * 2
    )


def test_keyed_track_job_can_be_polled_and_forgotten(tmp_path):
    """The real API: the key outlives the upload; the result is deleted explicitly after reading."""
    from mnema.client import MnemaClient

    app = create_app(tmp_path / "node")
    called = []

    def operation(path, params, on):
        called.append(params["tracks"])
        return {"files": {"transcript.txt": "hello"}}

    app.state.operations = {
        "tracks": ops.OpSpec(
            name="tracks",
            fn=operation,
            params=ops.RecordingParams,
            input_kind=ops.InputKind.TRACKS,
            output_kind=ops.OutputKind.JSON,
        )
    }
    client = MnemaClient("http://node", transport=httpx.ASGITransport(app=app))
    source = tmp_path / "mic.wav"
    source.write_bytes(b"audio")
    tracks = {"mic": client.upload(source)}
    params = {"name": "2026-10-23_12-30", "started": "2026-10-23T12:30:00+03:00"}
    job = client.submit("tracks", key="one", tracks=tracks, params=params)
    out = client.wait_job(job, poll_interval=0.01)
    assert out["status"] == "done" and out["result"]["files"]["transcript.txt"] == "hello"
    assert client.submit("tracks", key="one", tracks=tracks, params=params) == job
    assert len(called) == 1 and client.wait_job(job)["status"] == "done"
    client.cancel(job)  # a finished job does not change
    client.forget(job)
    assert client.wait_job(job) is None
    client.forget(job)  # a retry after a lost answer is safe
    client.cancel(job)


def test_missing_job_and_upload_have_structured_results():
    from mnema.client import MnemaClient, UploadMissing

    client = MnemaClient(
        "http://node",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(404, json={"track": "system", "detail": "gone"})
        ),
    )
    assert client.wait_job("expired") is None
    with pytest.raises(UploadMissing) as missing:
        client.submit("recording", key="one", tracks={"system": "/gone"})
    assert missing.value.track == "system"
