"""Processing in the app (BarRuntime): a Mac processes itself or on the server, as chosen in
the app; Linux on the connected server, without one the recording is kept as audio.

External boundaries: audio sources and the session, recognition (setup.dispatch), ffmpeg,
litellm; server HTTP is httpx.MockTransport.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_bar import _patch_record, _wav

import mnema.client as client_mod
from mnema.bar.recordings import library
from mnema.bar.runtime import BarRuntime
from mnema.core import base
from mnema.i18n import set_language
from mnema.live import meta
from mnema.live.setup import PROCESSING_MARK, unfinished_sessions

SERVER = "http://100.64.0.9:8123"
NO_SERVER = "Сервер не подключён. Запись сохранена, обработайте позже"
MAC_NO_MODEL = "Не настроена модель. Нажмите «Выбрать модель…» в настройках Mnema"


class _App:
    """The app: notifications and the title window (closed without an answer)."""

    def __init__(self) -> None:
        self.notes: list[tuple[str, str]] = []

    def notify(self, title: str, message: str, path: Path | None) -> None:
        self.notes.append((title, message))

    def ask_title(self, current: str | None, on_done: Callable[[str | None], None], **kw) -> None:
        on_done(None)

    def open(self, path: Any) -> None: ...

    def trash(self, path: Any) -> None: ...


@pytest.fixture
def home(monkeypatch, tmp_path) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MNEMA_LIVE_DIR", str(tmp_path / "notes"))
    return tmp_path


def _server(monkeypatch, handler: Callable[[httpx.Request], httpx.Response]) -> None:
    """The connected Linux server: MockTransport."""
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        client_mod.MnemaClient,
        "_make_async_client",
        lambda self: httpx.AsyncClient(base_url=self._base_url, transport=transport),
    )
    monkeypatch.setattr(client_mod.MnemaClient, "health", lambda *a, **kw: {"ok": True})
    monkeypatch.setenv("MNEMA_RUNNER", SERVER)


def _transcribed(home: Path, name: str = "2026-10-22_13-05") -> Path:
    folder = home / "notes" / name
    folder.mkdir(parents=True)
    (folder / "transcript.txt").write_text("mic: обсудили релиз.\n", encoding="utf-8")
    return folder


def test_mac_records_and_processes_here_without_a_server(monkeypatch, home) -> None:
    """A Mac without a model and without a server processes the recording right here, with no
    requests and no notice; there is a transcript, a remark says where to pick a model; no
    terminal commands in notifications or remarks."""
    captured: dict = {}
    _patch_record(monkeypatch, home, captured)
    monkeypatch.delenv("MNEMA_MODEL")
    sent: list[str] = []
    monkeypatch.setattr(
        client_mod.MnemaClient, "_make_async_client", lambda self: sent.append(self._base_url)
    )
    app = _App()
    rt = BarRuntime(platform=app, prefs_file=home / "bar.json")
    rt.handle("start")
    assert captured["started"].wait(timeout=10)
    assert rt.state.notice == ""
    rt.handle("stop")
    rt.join(timeout=30)
    assert rt.state.error == "" and sent == []
    assert app.notes[-1][0] == "Запись обработана"
    assert rt.state.last_result is not None and rt.state.last_result.name == "transcript.txt"
    [remark] = rt.state.last_warnings
    assert remark.cause == "Не настроена модель"
    assert remark.hint.startswith("Задайте модель в настройках Mnema на Mac.")
    run = meta.last(rt.state.last_result.parent)
    assert run is not None and run.settings["server"] is None
    texts = [f"{t} {m}" for t, m in app.notes] + [r.text for r in rt.state.last_warnings]
    assert not [t for t in texts if "mnema " in t or "./install" in t]


def test_linux_without_server_keeps_audio_for_later(monkeypatch, home) -> None:
    """Linux without a server: an "only the audio will be saved" notice, after recording
    "Server not connected…"; the recording is unprocessed and can be processed later."""
    import mnema.live.session as session_mod
    from mnema.live.models import Track

    captured: dict = {}
    _patch_record(monkeypatch, home, captured)
    recording = session_mod.LiveSession

    class Silent(recording):  # recording without live text, as in the app
        def run(self):
            super().run()
            return [Track(label="mic", committed=[])]

    monkeypatch.setattr(session_mod, "LiveSession", Silent)
    monkeypatch.setattr(base, "STANDALONE", False)
    monkeypatch.setenv("MNEMA_RUNNER", "")
    monkeypatch.setattr("mnema.bar.runtime._system_available", lambda: False)
    rt = BarRuntime(platform=_App(), prefs_file=home / "bar.json")
    rt.handle("start")
    assert captured["started"].wait(timeout=10)
    assert rt.state.notice == "Сервер не подключён, сохранится только звук"
    folder = rt._prepared.base
    rt.handle("stop")
    rt.join(timeout=30)
    assert rt.state.error == NO_SERVER
    [card] = library(home / "notes").cards
    assert card.state is meta.RecordState.UNPROCESSED and card.path == folder
    assert unfinished_sessions(home / "notes") == [folder]


@pytest.mark.parametrize(
    ("standalone", "error"),
    [(True, MAC_NO_MODEL), (False, "Сервер не подключён")],
    ids=["mac-without-model", "linux-without-server"],
)
def test_summary_that_cannot_start_says_why_and_keeps_the_record(
    monkeypatch, home, standalone, error
) -> None:
    monkeypatch.setattr(base, "STANDALONE", standalone)
    monkeypatch.setenv("MNEMA_RUNNER", "")
    monkeypatch.delenv("MNEMA_MODEL")
    folder = _transcribed(home)
    rt = BarRuntime(platform=_App(), prefs_file=home / "bar.json")
    rt.handle(f"summarize:{folder}")
    rt.join(timeout=10)
    assert rt.state.error == error and rt.state.unreachable == {}
    assert not (folder / PROCESSING_MARK).exists() and not (folder / "summary.txt").exists()
    assert meta.last(folder) is None


def test_server_name_and_llm_from_capabilities(monkeypatch, home) -> None:
    """The server name, whether it summarizes and its version come from its capabilities."""
    real = client_mod.MnemaClient
    caps = {"ok": True, "llm": True, "name": "  ПК Ubuntu  ", "version": "9.1.0"}
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=caps))
    monkeypatch.setattr(
        client_mod,
        "MnemaClient",
        lambda url, *, token=None: real(url, token=token, transport=transport),
    )
    monkeypatch.setenv("MNEMA_RUNNER", SERVER)
    rt = BarRuntime(platform=_App(), prefs_file=home / "bar.json")
    for t in threading.enumerate():
        if t.name == "mnema-bar-ping":
            t.join(timeout=10)
    env = rt.menu_env()
    assert (env.server_ok, env.server_name, env.server_llm) == (True, "ПК Ubuntu", True)
    assert env.server_version == "9.1.0"


def test_linux_summary_goes_to_the_server_model(monkeypatch, home) -> None:
    """Summarizing on Linux: the server writes the title and summary with its model (the summary
    language from the app settings); this machine has no model."""
    jobs: list[dict] = []
    result = {
        "folder": "2026-10-22_Созвон о релизе",
        "files": {"summary.txt": "Демо — к пятнице\n"},
        "run": meta.Run(kind="summary", status="ok").model_dump(mode="json"),
    }

    def server(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/uploads" and request.method == "POST":
            assert json.loads(request.content)["name"] == "transcript.txt"
            return httpx.Response(200, json={"id": "a1"})
        if path == "/api/uploads/a1" and request.method == "PUT":
            assert request.content == "mic: обсудили релиз.\n".encode()
            return httpx.Response(200, json={"path": "uploads/a1/transcript.txt"})
        if path == "/api/jobs":
            jobs.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "j1"})
        return httpx.Response(200, json={"status": "done", "result": result})

    _server(monkeypatch, server)
    monkeypatch.delenv("MNEMA_MODEL")
    folder = _transcribed(home)
    app = _App()
    rt = BarRuntime(platform=app, prefs_file=home / "bar.json")
    rt.prefs.summary_lang = "en"
    rt.handle(f"summarize:{folder}")
    rt.join(timeout=10)
    [job] = jobs
    assert (job["op"], job["path"], job["params"]["summary_lang"]) == (
        "recording_summary",
        "uploads/a1/transcript.txt",
        "en",
    )
    named = home / "notes" / "2026-10-22_Созвон о релизе"
    assert app.notes[-1] == ("Пересказ готов", "Созвон о релизе · 22.10")
    assert "Демо — к пятнице" in (named / "summary.txt").read_text(encoding="utf-8")


@pytest.mark.parametrize("standalone", [False, True], ids=["linux", "mac"])
def test_stopped_summary_leaves_the_record_as_it_was(monkeypatch, home, standalone) -> None:
    """Stopping processing during "Summarize": the server job is cancelled, on a Mac the model's
    answer is dropped; the recording stays processed, as on the phone, not "Not processed",
    and the history has no stopped summary."""
    monkeypatch.setattr(base, "STANDALONE", standalone)
    cancelled: list[str] = []
    app = _App()
    rt = BarRuntime(platform=app, prefs_file=home / "bar.json")

    def server(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/uploads" and request.method == "POST":
            return httpx.Response(200, json={"id": "a1"})
        if path == "/api/uploads/a1":
            return httpx.Response(200, json={"path": "uploads/a1/transcript.txt"})
        if path == "/api/jobs":
            return httpx.Response(200, json={"id": "j1"})
        if path == "/api/jobs/j1/cancel":
            cancelled.append(path)
            return httpx.Response(200, json={"ok": True})
        rt.handle(f"record:{rt.state.jobs[0].id}:stop")
        return httpx.Response(200, json={"status": "running", "progress": 0.5, "message": ""})

    if standalone:
        import types

        import litellm

        def completion(**kw):  # pressed while the model answered: the answer is not needed
            rt.handle(f"record:{rt.state.jobs[0].id}:stop")
            answer = {"title": "Другое", "about": "Итог.", "summary": ["Пункт"]}
            delta = types.SimpleNamespace(content=json.dumps(answer, ensure_ascii=False))
            choice = types.SimpleNamespace(delta=delta, finish_reason="stop")
            return iter([types.SimpleNamespace(choices=[choice])])

        monkeypatch.setattr(litellm, "completion", completion)
    else:
        _server(monkeypatch, server)
    root = home / "notes"
    folder = root / "2026-10-22_Созвон о релизе"
    folder.mkdir(parents=True)
    (folder / "mic.wav").write_bytes(b"\0" * 32044)
    (folder / "transcript.txt").write_text("mic: обсудили релиз.\n", encoding="utf-8")
    meta.append(folder, meta.Run(status="ok"))
    rt.handle(f"summarize:{folder}")
    rt.join(timeout=10)

    assert cancelled == ([] if standalone else ["/api/jobs/j1/cancel"])
    runs = json.loads((folder / meta.META_FILE).read_text(encoding="utf-8"))["runs"]
    assert [r["status"] for r in runs] == ["ok"]
    [card] = library(root).cards
    assert card.state is meta.RecordState.PROCESSED
    assert unfinished_sessions(root) == [] and rt.state.error == ""
    assert sorted(p.name for p in folder.iterdir()) == [meta.META_FILE, "mic.wav", "transcript.txt"]


def test_mac_processes_even_when_the_server_is_down(monkeypatch, home) -> None:
    """A Mac never shows an "unavailable" card: it recognizes here whatever the server; with the
    server silent the transcript is saved without a summary, a remark names the server and the
    card offers "Summarize"."""
    captured: dict = {}
    _patch_record(monkeypatch, home, captured)
    monkeypatch.setenv("MNEMA_RUNNER", SERVER)
    monkeypatch.setattr(
        client_mod.MnemaClient, "health", lambda *a, **kw: None
    )  # the server is down
    folder = home / "notes" / "2026-10-22_13-05"
    _wav(folder / "mic.wav")
    rt = BarRuntime(platform=_App(), prefs_file=home / "bar.json")
    rt.recover(folder)
    rt.join(timeout=30)
    assert rt.state.unreachable == {} and rt.state.error == ""
    assert rt.state.last_result is not None and rt.state.last_result.name == "transcript.txt"
    assert not (folder / "summary.txt").exists() and not (folder / PROCESSING_MARK).exists()
    [remark] = rt.state.last_warnings
    assert remark.cause == "Пересказ не составлен: 100.64.0.9:8123 недоступен"
    assert remark.hint == "Нажмите «Составить пересказ» в меню записи"
    assert meta.last(folder).notes == [remark]
    [card] = library(home / "notes").cards
    assert card.state is meta.RecordState.PROCESSED and card.has_text and not card.has_summary


def _spied_server(monkeypatch, home: Path) -> list[httpx.Request]:
    """The connected server is the real recording HTTP API in this process; the test sees its
    requests."""
    from mnema.server import create_app

    seen: list[httpx.Request] = []
    inner = httpx.ASGITransport(app=create_app(home / "server"))

    class Spy(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return await inner.handle_async_request(request)

    monkeypatch.setattr(
        client_mod.MnemaClient,
        "_make_async_client",
        lambda self: httpx.AsyncClient(
            base_url=self._base_url, headers=self._headers, transport=Spy()
        ),
    )
    monkeypatch.setenv("MNEMA_RUNNER", SERVER)
    return seen


def test_mac_on_the_server_processes_there(monkeypatch, home) -> None:
    """A Mac set to "On the server" sends the recording to the connected server over its HTTP
    contract, as on Linux; its host goes to the history and the no-model remark names the
    server ("On this Mac": test_mac_records_and_processes_here_without_a_server)."""
    from mnema.config import config_dir

    captured: dict = {}
    _patch_record(monkeypatch, home, captured)
    seen = _spied_server(monkeypatch, home)
    monkeypatch.delenv("MNEMA_MODEL")  # no model here or on the server
    config_dir().mkdir(parents=True, exist_ok=True)
    (config_dir() / "node.json").write_text(json.dumps({"name": "Дом"}), encoding="utf-8")
    (home / "bar.json").write_text(json.dumps({"recognition": "server"}), encoding="utf-8")
    app = _App()
    rt = BarRuntime(platform=app, prefs_file=home / "bar.json")
    rt.handle("start")
    assert captured["started"].wait(timeout=10)
    rt.handle("stop")
    rt.join(timeout=30)

    assert rt.state.error == "" and app.notes[-1][0] == "Запись обработана"
    assert rt.state.last_result is not None
    run = meta.last(rt.state.last_result.parent)
    assert run is not None and run.status == "ok"
    [remark] = rt.state.last_warnings
    jobs = [r for r in seen if r.url.path == "/api/jobs" and r.method == "POST"]
    assert run.settings["server"] == "100.64.0.9:8123"
    assert [json.loads(r.content)["op"] for r in jobs] == ["recording"]
    assert remark.hint.startswith("Задайте модель в настройках Mnema на Mac.")


def test_mac_on_server_down_waits_for_it_and_does_not_fall_back(monkeypatch, home) -> None:
    """With "On the server" chosen and the server silent: a "<server> is unavailable" card with
    "Retry", the audio is saved; this Mac does not process it by itself."""
    import mnema.live.setup as setup_mod

    captured: dict = {}
    _patch_record(monkeypatch, home, captured)
    monkeypatch.setenv("MNEMA_RUNNER", SERVER)
    monkeypatch.setattr(client_mod.MnemaClient, "health", lambda *a, **kw: None)
    monkeypatch.setattr("mnema.bar.runtime._system_available", lambda: False)
    monkeypatch.setattr(
        setup_mod, "dispatch", lambda *a, **k: pytest.fail("no recognition on this Mac")
    )
    (home / "bar.json").write_text(json.dumps({"recognition": "server"}), encoding="utf-8")
    app = _App()
    rt = BarRuntime(platform=app, prefs_file=home / "bar.json")
    rt.handle("start")
    assert captured["started"].wait(timeout=10)
    assert rt.state.notice == "100.64.0.9:8123 недоступен, сохранится только звук"
    folder = rt._prepared.base
    rt.handle("stop")
    rt.join(timeout=30)
    assert app.notes[-1] == ("100.64.0.9:8123 недоступен", "Запись сохранена, обработайте позже")
    assert rt.state.unreachable == {folder: "100.64.0.9:8123"}


def test_unreachable_server_texts_are_english_in_english(monkeypatch, home) -> None:
    """The English interface: the notice and the notification about a silent server."""
    set_language("en")
    captured: dict = {}
    _patch_record(monkeypatch, home, captured)
    monkeypatch.setenv("MNEMA_RUNNER", SERVER)
    monkeypatch.setattr(client_mod.MnemaClient, "health", lambda *a, **kw: None)
    monkeypatch.setattr("mnema.bar.runtime._system_available", lambda: False)
    (home / "bar.json").write_text(json.dumps({"recognition": "server"}), encoding="utf-8")
    app = _App()
    rt = BarRuntime(platform=app, prefs_file=home / "bar.json")
    rt.handle("start")
    assert captured["started"].wait(timeout=10)
    assert rt.state.notice == "100.64.0.9:8123 is unavailable, only the audio will be saved"
    rt.handle("stop")
    rt.join(timeout=30)
    assert app.notes[-1] == (
        "100.64.0.9:8123 is unavailable",
        "The recording is saved; process it later",
    )


def test_linux_without_server_texts_are_english_in_english(monkeypatch, home) -> None:
    set_language("en")
    monkeypatch.setattr(base, "STANDALONE", False)
    monkeypatch.setenv("MNEMA_RUNNER", "")
    rt = BarRuntime(platform=_App(), prefs_file=home / "bar.json")
    assert rt._startup_notice(None) == "Server not connected, only the audio will be saved"
    rt.handle(f"summarize:{_transcribed(home)}")
    rt.join(timeout=10)
    assert rt.state.error == "Server not connected"


@pytest.mark.parametrize("place", ["cloud", "mac"])
def test_cloud_recognition_with_a_server_goes_to_the_server(monkeypatch, home, place) -> None:
    """In the cloud with a connected server: the server recognizes with the chosen cloud model;
    On this Mac a cloud model saved earlier works as Automatic here, and only the summary goes to
    the server."""
    captured: dict = {}
    _patch_record(monkeypatch, home, captured)
    jobs: list[dict] = []

    def server(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/uploads" and request.method == "POST":
            return httpx.Response(200, json={"id": f"u{len(jobs)}"})
        if request.url.path.startswith("/api/uploads/"):
            return httpx.Response(200, json={"path": request.url.path})
        if request.url.path == "/api/jobs":
            jobs.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "j1"})
        return httpx.Response(200, json={"status": "error", "error": "Нет ключа OpenAI"})

    _server(monkeypatch, server)
    model = "openai/gpt-4o-mini-transcribe"
    saved = {"recognition": place, "asr": model}
    (home / "bar.json").write_text(json.dumps(saved), encoding="utf-8")
    rt = BarRuntime(platform=_App(), prefs_file=home / "bar.json")
    assert rt.prefs.asr == (model if place == "cloud" else "auto")
    rt.handle("start")
    assert captured["started"].wait(timeout=10)
    rt.handle("stop")
    rt.join(timeout=30)
    if place == "cloud":
        assert [(job["op"], job["params"]["asr"]) for job in jobs] == [("recording", model)]
    else:
        assert [job["op"] for job in jobs] == ["recording_summary"]
    assert json.loads((home / "bar.json").read_text(encoding="utf-8"))["asr"] == rt.prefs.asr


def _summaries(monkeypatch) -> None:
    """The user's model on the server answers the title and the summary (litellm)."""
    import types

    import litellm

    answer = {"title": "Созвон о релизе", "about": "Обсудили релиз.", "summary": ["Демо"]}

    def completion(**kw):
        delta = types.SimpleNamespace(content=json.dumps(answer, ensure_ascii=False))
        choice = types.SimpleNamespace(delta=delta, finish_reason="stop")
        return iter([types.SimpleNamespace(choices=[choice])])

    monkeypatch.setattr(litellm, "completion", completion)


def test_mac_recognizes_here_and_the_server_writes_the_summary(monkeypatch, home) -> None:
    """On this Mac with a connected server: the Mac recognizes the audio itself (no recording job
    on the server), then the server writes the title and the summary with the user's model from
    the transcript; the result is applied to the folder."""
    captured: dict = {}
    _patch_record(monkeypatch, home, captured)
    seen = _spied_server(monkeypatch, home)
    _summaries(monkeypatch)
    app = _App()
    rt = BarRuntime(platform=app, prefs_file=home / "bar.json")
    assert rt.prefs.recognition == "mac" and rt.processing_connection() is None
    rt.handle("start")
    assert captured["started"].wait(timeout=10)
    rt.handle("stop")
    rt.join(timeout=30)

    assert rt.state.error == "" and rt.state.unreachable == {}
    jobs = [json.loads(r.content) for r in seen if r.url.path == "/api/jobs"]
    assert [job["op"] for job in jobs] == ["recording_summary"]
    folder = next((home / "notes").iterdir())
    assert folder.name.endswith("_Созвон о релизе")
    assert "Обсудили релиз." in (folder / "summary.txt").read_text(encoding="utf-8")
    runs = json.loads((folder / meta.META_FILE).read_text(encoding="utf-8"))["runs"]
    assert [(r["kind"], r["status"], r["settings"].get("server")) for r in runs] == [
        ("process", "ok", None),
        ("summary", "ok", "100.64.0.9:8123"),
    ]
    assert rt.state.last_summary == folder / "summary.txt"


def test_mac_on_server_summary_without_server_model_names_the_server(monkeypatch, home) -> None:
    """Summarizing with "On the server" and no model on the server: the server's refusal talks
    about the server, not the Mac settings; the recording as it was."""
    monkeypatch.setattr(base, "STANDALONE", True)
    _spied_server(monkeypatch, home)
    monkeypatch.setattr(client_mod.MnemaClient, "health", lambda *a, **kw: {"ok": True})
    monkeypatch.delenv("MNEMA_MODEL")
    (home / "bar.json").write_text(json.dumps({"recognition": "server"}), encoding="utf-8")
    folder = _transcribed(home)
    rt = BarRuntime(platform=_App(), prefs_file=home / "bar.json")
    rt.handle(f"summarize:{folder}")
    rt.join(timeout=10)
    assert rt.state.error == "Не настроена модель. Выберите её в настройках Mnema на Mac"
    assert not (folder / PROCESSING_MARK).exists() and meta.last(folder) is None
