"""The node writes summaries and translations with its own model.

Runner HTTP API contract; external boundaries are mocked: litellm.completion, provider network
(providers.TRANSPORT) and ffprobe.
"""

from __future__ import annotations

import threading
import time
import types
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from mnema import config, ops
from mnema.job_queue import JobQueue
from mnema.llm import claude, limits, providers
from mnema.server import create_app

KEY = "sk-ant-api03-" + "k" * 24 + "Q9z1"
GEMINI_KEY = "AIza" + "g" * 31 + "a1B2"
RU_TEXT = "mic: обсудили релиз, сроки и кто готовит демо.\nsystem: я беру демо к пятнице.\n" * 10
STARTED = "2026-10-22T00:30:00+03:00"
NAME = "2026-10-22_00-30"


@pytest.fixture(autouse=True)
def _node_model(monkeypatch):
    """The node's model comes from the process environment; its limits from litellm's table."""
    monkeypatch.setenv("MNEMA_API_KEY", KEY)
    monkeypatch.setattr(
        providers, "TRANSPORT", httpx.MockTransport(lambda r: httpx.Response(404, json={}))
    )
    monkeypatch.setattr(limits, "_FETCHED", {})
    monkeypatch.setattr(claude.time, "sleep", lambda s: None)


def _litellm(monkeypatch, answer) -> list[dict]:
    """litellm at the boundary: answer(kwargs) -> text or an exception. Returns the calls."""
    import litellm

    calls: list[dict] = []

    def completion(**kw):
        calls.append(kw)
        out = answer(kw)
        if isinstance(out, BaseException):
            raise out
        delta = types.SimpleNamespace(content=out)
        choice = types.SimpleNamespace(delta=delta, finish_reason="stop")
        return iter([types.SimpleNamespace(choices=[choice])])

    monkeypatch.setattr(litellm, "completion", completion)
    return calls


def _no_model(monkeypatch) -> None:
    monkeypatch.delenv("MNEMA_MODEL", raising=False)
    monkeypatch.delenv("MNEMA_API_KEY", raising=False)


def _upload(client: TestClient, text: str) -> str:
    files = {"file": ("transcript.txt", text.encode())}
    return client.post("/api/upload", files=files).json()["path"]


def _finished(client: TestClient, job_id: str) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "error"):
            return job
        time.sleep(0.005)
    pytest.fail(f"Job {job_id} did not finish")


def test_model_refusal_is_reported_without_the_key(tmp_path, monkeypatch):
    refusal = type("AuthenticationError", (Exception,), {})(f"invalid x-api-key: {KEY}")
    refusal.status_code = 401  # type: ignore[attr-defined]
    _litellm(monkeypatch, lambda kw: refusal)
    client = TestClient(create_app(tmp_path))
    body = {
        "op": "recording_summary",
        "path": _upload(client, RU_TEXT),
        "params": {"started": STARTED, "name": NAME, "lang": "ru"},
    }
    job_id = client.post("/api/jobs", json=body).json()["id"]
    result = _finished(client, job_id)["result"]
    assert not result["files"].get("summary.md")
    assert "ключ модели не подошёл" in " ".join(result["run"]["remarks"])
    assert KEY not in client.get(f"/api/jobs/{job_id}").text


def test_node_without_model_refuses_translation(tmp_path, monkeypatch):
    _no_model(monkeypatch)
    calls = _litellm(monkeypatch, lambda kw: pytest.fail("no model is configured"))
    app = create_app(tmp_path)
    client = TestClient(app)
    path = _upload(client, RU_TEXT)
    response = client.post("/api/jobs", json={"op": "translate", "path": path})
    assert response.status_code == 400
    assert response.json()["detail"] == "На сервере не настроена модель"
    assert not calls
    assert Path(path).is_file()  # the upload is not claimed and can go to another job


def test_node_without_model_answers_no_model(tmp_path, monkeypatch):
    """From capabilities the client learns whether the node can summarize; the model name is in
    the node settings."""
    monkeypatch.setattr("socket.gethostname", lambda: "studio")
    _no_model(monkeypatch)
    client = TestClient(create_app(tmp_path))
    response = client.get("/api/capabilities")
    assert response.json() == {"ok": True, "llm": False, "name": "studio"}
    assert client.get("/api/node/settings").json()["model"] is None


def test_node_model_without_a_key_is_no_model_in_every_answer(tmp_path, monkeypatch):
    """MNEMA_MODEL without MNEMA_API_KEY means no summary: both capabilities and the server info
    (/api/node/settings) report no model, not just a model name."""
    monkeypatch.delenv("MNEMA_API_KEY")
    client = TestClient(create_app(tmp_path))
    assert client.get("/api/capabilities").json()["llm"] is False
    assert client.get("/api/node/settings").json()["model"] is None


def test_node_model_comes_from_the_node_env_file(tmp_path, monkeypatch):
    """The node's model is lines of its ~/.config/mnema/.env; an edit applies without a
    restart."""
    _no_model(monkeypatch)
    env = config.user_env_file()
    env.parent.mkdir(parents=True, exist_ok=True)
    env.write_text(f"MNEMA_MODEL=gemini/gemini-flash-latest\nMNEMA_API_KEY={GEMINI_KEY}\n")
    client = TestClient(create_app(tmp_path))
    caps, settings = client.get("/api/capabilities"), client.get("/api/node/settings")
    assert caps.json()["llm"] is True
    assert settings.json()["model"] == "gemini/gemini-flash-latest"
    for answer in (caps, settings):
        assert GEMINI_KEY not in answer.text


class _Index(BaseModel):
    index: int = 0


def test_text_job_does_not_wait_for_recordings_or_voice_messages(tmp_path, monkeypatch):
    """The only slot runs a recording and the short slot a voice note: translating finished text
    takes its own lane and skips ffprobe."""
    import mnema.api.jobs as jobs

    monkeypatch.setenv("MNEMA_JOB_WORKERS", "1")
    monkeypatch.setenv("MNEMA_JOB_QUEUE_SIZE", "2")
    probed: list[str] = []

    def probe(path):
        probed.append(Path(path).name)
        return types.SimpleNamespace(duration=20.0 if Path(path).name == "voice.ogg" else 3600.0)

    monkeypatch.setattr(jobs.mc, "probe", probe)
    _litellm(monkeypatch, lambda kw: "Перевод сервера")
    started = {name: threading.Event() for name in ("call.wav", "voice.ogg")}
    release = threading.Event()

    def asr(path, params, on):
        started[path.name].set()
        assert release.wait(5)
        return {}

    app = create_app(tmp_path)
    app.state.operations = {
        **ops.registry(),
        "asr": ops.OpSpec(
            name="asr",
            fn=asr,
            params=_Index,
            input_kind=ops.InputKind.MEDIA,
            output_kind=ops.OutputKind.JSON,
        ),
    }
    client = TestClient(app)
    busy = []
    try:
        for name in ("call.wav", "voice.ogg"):
            (tmp_path / name).write_bytes(b"audio")
            busy.append(client.post("/api/jobs", json={"op": "asr", "path": str(tmp_path / name)}))
            assert started[name].wait(3)
        response = client.post(
            "/api/jobs", json={"op": "translate", "path": _upload(client, RU_TEXT)}
        )
        assert response.status_code == 200, response.text
        job = _finished(client, response.json()["id"])
        assert job["result"] == {"markdown": "Перевод сервера"}
        assert probed == ["call.wav", "voice.ogg"]
    finally:
        release.set()
        for response in busy:
            assert _finished(client, response.json()["id"])["status"] == "done"


def test_text_lane_runs_only_text_jobs_and_has_its_own_room() -> None:
    q = JobQueue(workers=1, pending=1, text_slots=1)
    order: list[str] = []
    gates = {"text1": threading.Event(), "long": threading.Event()}

    def job(name: str):
        def run() -> None:
            if name in gates:
                assert gates[name].wait(5)
            order.append(name)

        return run

    def wait_for(count: int) -> None:
        deadline = time.monotonic() + 5
        while len(order) < count and time.monotonic() < deadline:
            time.sleep(0.005)

    q.submit("text1", job("text1"), lambda: None, text=True)
    q.submit("text2", job("text2"), lambda: None, text=True)  # the text lane is busy, so it waits
    assert not q.has_room(text=True)  # the wait queue is full and the text lane busy
    assert q.has_room()  # the transcription slot is free: text does not use it
    q.submit("long", job("long"), lambda: None)
    gates["long"].set()
    wait_for(1)
    time.sleep(0.05)  # text does not take the freed transcription slot
    assert order == ["long"]
    gates["text1"].set()
    wait_for(3)
    assert order == ["long", "text1", "text2"]
