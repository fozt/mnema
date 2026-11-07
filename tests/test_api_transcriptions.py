"""POST /api/v1/audio/transcriptions: the OpenAI contract over the transcribe operation.

The boundary is HTTP (TestClient, and real uvicorn where a socket or the openai package matters);
core.transcribe and core.probe are the engine and ffprobe."""

import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import httpx
import openai
import pytest
import uvicorn
from fastapi.testclient import TestClient

from mnema import core
from mnema.api import transcriptions
from mnema.core import asr_engine, backends
from mnema.core.backends import ParakeetBackend
from mnema.core.base import MediaError, noop
from mnema.core.models import Segment, Transcript, Word
from mnema.server import create_app
from mnema.users import current_user

NODE = "node-token"
URL = "/api/v1/audio/transcriptions"
TRANSCRIPT = Transcript(
    text="Привет. Как дела?",
    segments=[
        Segment(
            start=0.0,
            end=1.25,
            text="Привет.",
            words=[Word(text="Привет.", start=0.0, end=1.25)],
        ),
        Segment(
            start=3661.5,
            end=3662.0,
            text="Как дела?",
            words=[
                Word(text="Как", start=3661.5, end=3661.7),
                Word(text="дела?", start=3661.8, end=3662.0),
            ],
        ),
    ],
    language="ru",
)
AUDIO = {"file": ("talk.mp3", b"audio bytes", "audio/mpeg")}


@pytest.fixture
def node(tmp_path, monkeypatch):
    """A node with a token, two devices, a stub engine and a stub ffprobe. hold makes the engine
    wait for release; calls are what the engine was given."""
    monkeypatch.setenv("MNEMA_TOKEN", NODE)
    monkeypatch.setenv("MNEMA_JOB_WORKERS", "1")
    monkeypatch.setenv("MNEMA_JOB_QUEUE_SIZE", "1")
    monkeypatch.setattr(transcriptions, "POLL_S", 0.01)
    audio = SimpleNamespace(duration=3662.0, has_audio=True)
    monkeypatch.setattr(core, "probe", lambda path: audio)
    state = SimpleNamespace(
        calls=[],
        users=[],
        hold=threading.Event(),
        started=threading.Event(),
        release=threading.Event(),
        stopped=threading.Event(),
        failure=None,
    )

    def transcribe(path, *, on, **params):
        state.calls.append(params)
        state.users.append(current_user())
        assert Path(path).read_bytes() == b"audio bytes"
        state.started.set()
        while state.hold.is_set() and not state.release.wait(0.005):
            try:
                on(None, "")
            except MediaError:
                state.stopped.set()
                raise
        if state.failure is not None:
            raise state.failure
        return TRANSCRIPT

    monkeypatch.setattr(core, "transcribe", transcribe)
    app = create_app(tmp_path)
    state.app, state.root, state.audio = app, tmp_path, audio
    state.client = TestClient(app)
    state.h = {"node": {"Authorization": f"Bearer {NODE}"}}
    for name in ("service", "laptop"):
        code, _ = app.state.devices.create_invite(now=time.time())
        claimed = app.state.devices.claim(code, name, now=time.time())
        assert claimed is not None
        state.h[name] = {"Authorization": f"Bearer {claimed[1]}"}
        state.__dict__[f"{name}_token"] = claimed[1]
        state.__dict__[f"{name}_id"] = claimed[0]
    yield state
    state.release.set()


def post(n, *, who="service", files=None, **data):
    data.setdefault("model", "mnema")
    return n.client.post(URL, data=data, files=files or AUDIO, headers=n.h[who])


def leftovers(n) -> list[str]:
    """What the request left in uploads/, waiting out the job thread's own cleanup."""
    uploads = n.root / "uploads"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and uploads.is_dir() and any(uploads.iterdir()):
        time.sleep(0.005)
    return sorted(p.name for p in uploads.iterdir()) if uploads.is_dir() else []


def jobs(n) -> int:
    return n.app.state.store._con.execute("SELECT count(*) FROM jobs").fetchone()[0]


def over(check, what: str) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.005)
    pytest.fail(what)


def refusal(response, status: int, code: str) -> dict:
    assert response.status_code == status, response.text
    error = response.json()["error"]
    assert error["code"] == code
    assert error["message"] and error["type"]
    return error


def test_json_is_the_default_format(node):
    response = post(node)
    assert response.status_code == 200
    assert response.json() == {"text": "Привет. Как дела?"}


def test_text_format(node):
    response = post(node, response_format="text")
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text == "Привет. Как дела?"


def test_srt_format(node):
    assert post(node, response_format="srt").text == (
        "1\n00:00:00,000 --> 00:00:01,250\nПривет.\n\n2\n01:01:01,500 --> 01:01:02,000\nКак дела?\n"
    )


def test_vtt_format(node):
    assert post(node, response_format="vtt").text == (
        "WEBVTT\n\n00:00:00.000 --> 00:00:01.250\nПривет.\n\n"
        "01:01:01.500 --> 01:01:02.000\nКак дела?\n"
    )


def test_verbose_json_has_segments_by_default(node):
    answer = post(node, response_format="verbose_json").json()
    assert answer["task"] == "transcribe"
    assert answer["language"] == "ru"
    assert answer["duration"] == 3662.0
    assert answer["text"] == TRANSCRIPT.text
    assert [(s["id"], s["start"], s["end"], s["text"]) for s in answer["segments"]] == [
        (0, 0.0, 1.25, "Привет."),
        (1, 3661.5, 3662.0, "Как дела?"),
    ]
    assert "words" not in answer


def test_verbose_json_gives_only_the_asked_granularity(node):
    answer = node.client.post(
        URL,
        data={
            "model": "mnema",
            "response_format": "verbose_json",
            "timestamp_granularities[]": "word",
        },
        files=AUDIO,
        headers=node.h["service"],
    ).json()
    assert answer["words"][1] == {"word": "Как", "start": 3661.5, "end": 3661.7}
    assert "segments" not in answer


def test_verbose_json_gives_both_granularities(node):
    answer = node.client.post(
        URL,
        data={
            "model": "mnema",
            "response_format": "verbose_json",
            "timestamp_granularities[]": ["segment", "word"],
        },
        files=AUDIO,
        headers=node.h["service"],
    ).json()
    assert len(answer["segments"]) == 2
    assert len(answer["words"]) == 3


@pytest.mark.parametrize(
    ("fields", "status", "code", "param"),
    [
        ({"response_format": "diarized_json"}, 400, "unsupported_value", "response_format"),
        ({"stream": "true"}, 400, "unsupported_value", "stream"),
        (
            {"response_format": "json", "timestamp_granularities[]": "word"},
            400,
            None,
            "timestamp_granularities",
        ),
        (
            {"response_format": "verbose_json", "timestamp_granularities[]": "letter"},
            400,
            "unsupported_value",
            "timestamp_granularities",
        ),
        ({"model": "gpt-4o-transcribe"}, 404, "model_not_found", "model"),
        ({"model": "openai/gpt-4o-transcribe"}, 404, "model_not_found", "model"),
        ({"model": "auto"}, 404, "model_not_found", "model"),
        ({"language": "ja"}, 400, "unsupported_language", "language"),
    ],
)
def test_a_refused_request_queues_nothing_and_leaves_no_upload(node, fields, status, code, param):
    error = refusal(post(node, **fields), status, code)
    assert error["param"] == param
    assert node.calls == []
    assert jobs(node) == 0
    assert leftovers(node) == []


def test_the_model_and_the_file_are_required(node):
    response = node.client.post(URL, files=AUDIO, headers=node.h["service"])
    assert refusal(response, 400, "missing_required_parameter")["param"] == "model"
    response = node.client.post(URL, data={"model": "mnema"}, headers=node.h["service"])
    assert refusal(response, 400, "missing_required_parameter")["param"] == "file"


def test_a_file_name_the_server_cannot_keep_is_refused(node):
    response = post(node, files={"file": ("x" * 300 + ".mp3", b"audio bytes")})
    assert refusal(response, 400, "invalid_value")["param"] == "file"
    assert leftovers(node) == []


def test_whisper_1_is_parakeet_and_cloud_is_never_chosen(node):
    assert post(node, model="whisper-1").status_code == 200
    assert post(node, model="mnema").status_code == 200
    assert [call["backend"] for call in node.calls] == ["parakeet", "parakeet"]


def test_language_reaches_the_engine_and_the_prompt_does_not(node):
    assert post(node, language="EN", prompt="Mnema, Parakeet").status_code == 200
    assert node.calls == [{"backend": "parakeet", "lang": "en"}]


@pytest.mark.parametrize("token", [None, "wrong"])
def test_a_bad_token_gets_an_openai_401(node, token):
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    response = node.client.post(URL, data={"model": "mnema"}, files=AUDIO, headers=headers)
    refusal(response, 401, "invalid_api_key")
    assert node.calls == []


def test_a_revoked_device_is_refused(node):
    assert post(node).status_code == 200
    node.app.state.devices.revoke(node.service_id)
    refusal(post(node), 401, "invalid_api_key")


def test_the_job_runs_as_the_device_user_and_others_do_not_see_it(node):
    code, _ = node.app.state.devices.create_invite(now=time.time(), user=_user(node, "anna"))
    claimed = node.app.state.devices.claim(code, "anna-service", now=time.time(), library=True)
    assert claimed is not None
    node.h["anna"] = {"Authorization": f"Bearer {claimed[1]}"}
    node.hold.set()
    with ThreadPoolExecutor(1) as pool:
        running = pool.submit(post, node, who="anna")
        assert node.started.wait(5)
        job_id = _running_job(node)
        assert node.client.get(f"/api/jobs/{job_id}", headers=node.h["anna"]).status_code == 200
        for other in ("service", "laptop"):
            client = node.client
            assert client.get(f"/api/jobs/{job_id}", headers=node.h[other]).status_code == 404
            stop = client.post(f"/api/jobs/{job_id}/cancel", headers=node.h[other])
            assert stop.status_code == 404
        node.release.set()
        assert running.result(5).json() == {"text": TRANSCRIPT.text}
    assert node.users == ["anna"]


def _user(node, name: str) -> str:
    node.app.state.devices.add_user(name, now=time.time())
    return name


def _running_job(node) -> str:
    return node.app.state.store._con.execute("SELECT id FROM jobs").fetchone()[0]


def test_nothing_stays_on_the_server_after_the_answer(node):
    assert post(node).status_code == 200
    assert leftovers(node) == []
    assert jobs(node) == 0


def test_a_failed_recognition_is_a_server_error_and_cleans_up(node):
    node.failure = MediaError("The engine is not installed")
    response = post(node)
    assert response.status_code == 500
    error = response.json()["error"]
    assert (error["type"], error["code"]) == ("server_error", "transcription_failed")
    assert error["message"] == "The engine is not installed"
    assert leftovers(node) == []
    assert jobs(node) == 0


def test_a_file_without_audio_is_refused_before_the_queue(node, monkeypatch):
    node.audio.has_audio = False
    refusal(post(node), 400, "invalid_file_format")

    def unreadable(path):
        raise MediaError("not media")

    monkeypatch.setattr(core, "probe", unreadable)
    refusal(post(node), 400, "invalid_file_format")
    assert node.calls == []
    assert leftovers(node) == []


def test_a_full_queue_is_429_with_retry_after_and_leaves_no_upload(node):
    node.hold.set()
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(post, node)
        assert node.started.wait(5)
        second = pool.submit(post, node, who="service")
        over(lambda: jobs(node) == 2, "the second request did not queue")
        response = post(node)
        node.release.set()
        assert first.result(5).status_code == 200
        assert second.result(5).status_code == 200
    error = refusal(response, 429, "rate_limit_exceeded")
    assert error["type"] == "rate_limit_error"
    assert int(response.headers["retry-after"]) > 0
    assert leftovers(node) == []


def test_a_timeout_cancels_the_job(node, monkeypatch):
    monkeypatch.setattr(transcriptions, "TIMEOUT_S", 0.0)
    node.hold.set()
    response = post(node)
    assert response.status_code == 504
    assert response.json()["error"]["code"] == "timeout"
    assert node.stopped.wait(5)
    assert leftovers(node) == []
    over(lambda: jobs(node) == 0, "the job record stayed")


def test_a_request_over_the_limit_is_refused_by_content_length(node, monkeypatch):
    monkeypatch.setattr(transcriptions, "MAX_REQUEST_BYTES", 10)
    refusal(post(node), 413, "file_too_large")
    assert node.calls == []


def test_the_limit_holds_for_a_body_without_content_length(node, monkeypatch):
    monkeypatch.setattr(transcriptions, "MAX_REQUEST_BYTES", 2048)
    boundary = "mnema"
    head = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.mp3"\r\n\r\n'
    ).encode()

    def body():
        yield head
        for _ in range(16):
            yield b"x" * 1024

    response = node.client.post(
        URL,
        content=body(),
        headers={
            **node.h["service"],
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    refusal(response, 413, "file_too_large")
    assert leftovers(node) == []


def test_a_broken_form_is_an_openai_400(node):
    response = node.client.post(
        URL,
        content=b"not a form",
        headers={**node.h["service"], "Content-Type": "multipart/form-data"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


def test_parakeet_keeps_the_word_times(monkeypatch):
    """The recognizer hears 0.3 s of silence before the speech: the answer's times do not."""
    words = [Word(text="Hello", start=0.1, end=0.5), Word(text="there.", start=0.8, end=1.3)]

    class Recognizer:
        def words(self, wav, *, on=noop):
            return words

    monkeypatch.setattr(asr_engine, "local_recognizer", Recognizer)
    monkeypatch.setattr(backends, "extract_audio", lambda src, dst, **options: dst)
    transcript = ParakeetBackend().run(Path("talk.wav"), lang="en")
    assert [(w.text, w.start, w.end) for w in transcript.words] == [
        ("Hello", 0.0, pytest.approx(0.2)),
        ("there.", pytest.approx(0.5), pytest.approx(1.0)),
    ]
    assert transcript.text == "Hello there."


@pytest.fixture
def served(node):
    """The same node behind real uvicorn: real sockets, real disconnects."""
    server = uvicorn.Server(uvicorn.Config(node.app, host="127.0.0.1", port=0, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    over(lambda: server.started, "uvicorn did not start")
    node.port = server.servers[0].sockets[0].getsockname()[1]
    yield node
    node.release.set()
    server.should_exit = True
    thread.join(10)


def sdk(n, token=None, **options):
    return openai.OpenAI(
        base_url=f"http://127.0.0.1:{n.port}/api/v1",
        api_key=token or n.service_token,
        max_retries=0,
        **options,
    )


def test_the_openai_package_reads_every_format(served):
    client = sdk(served)
    file = ("talk.mp3", b"audio bytes")
    assert client.audio.transcriptions.create(model="whisper-1", file=file).text == TRANSCRIPT.text
    for answer in ("text", "srt", "vtt"):
        text = client.audio.transcriptions.create(
            model="mnema", file=file, response_format=answer, language="ru", prompt="Mnema"
        )
        assert "Как дела?" in text
    verbose = client.audio.transcriptions.create(
        model="mnema",
        file=file,
        response_format="verbose_json",
        timestamp_granularities=["word", "segment"],
    )
    assert verbose.duration == 3662.0
    assert verbose.segments[1].text == "Как дела?"
    assert verbose.words[2].word == "дела?"


def test_the_openai_package_gets_its_own_errors(served):
    file = ("talk.mp3", b"audio bytes")
    with pytest.raises(openai.AuthenticationError) as denied:
        sdk(served, token="wrong").audio.transcriptions.create(model="mnema", file=file)
    assert denied.value.code == "invalid_api_key"
    with pytest.raises(openai.NotFoundError) as unknown:
        sdk(served).audio.transcriptions.create(model="gpt-4o-transcribe", file=file)
    assert unknown.value.code == "model_not_found"
    assert unknown.value.param == "model"


def test_a_client_that_leaves_cancels_the_job(served):
    served.hold.set()
    boundary = "mnema"
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="model"\r\n\r\nmnema\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.mp3"\r\n\r\n'
        f"audio bytes\r\n--{boundary}--\r\n"
    ).encode()
    head = (
        f"POST {URL} HTTP/1.1\r\nHost: node\r\nAuthorization: Bearer {served.service_token}\r\n"
        f"Content-Type: multipart/form-data; boundary={boundary}\r\n"
        f"Content-Length: {len(body)}\r\n\r\n"
    ).encode()
    with socket.create_connection(("127.0.0.1", served.port)) as connection:
        connection.sendall(head + body)
        assert served.started.wait(5)
    assert served.stopped.wait(5)
    assert leftovers(served) == []
    over(lambda: jobs(served) == 0, "the job record stayed")


def test_a_job_waiting_in_the_queue_goes_with_its_client(served):
    served.hold.set()
    file = ("talk.mp3", b"audio bytes")
    with ThreadPoolExecutor(1) as pool:
        first = pool.submit(sdk(served).audio.transcriptions.create, model="mnema", file=file)
        assert served.started.wait(5)
        client = sdk(served, timeout=httpx.Timeout(0.2))
        with pytest.raises(openai.APITimeoutError):
            client.audio.transcriptions.create(model="mnema", file=file)
        over(lambda: jobs(served) == 1, "the waiting job stayed in the queue")
        served.release.set()
        assert first.result(5).text == TRANSCRIPT.text
    assert len(served.calls) == 1
