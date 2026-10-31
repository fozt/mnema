"""A Mac processes by itself: the same recording pipeline (recording_client.process → _apply),
with the job run as a registry operation in this process (LocalNode), no server. A refusal
before work is the same on the Mac and the server: no work file with a request, the recording
stays as it was.

External boundaries: recognition (setup.dispatch), ffmpeg (audio.extract_audio, audio.mix),
installed engines (importlib.util.find_spec), litellm, the provider network; the server is a
real create_app over ASGI.
"""

from __future__ import annotations

import array
import importlib.util
import json
import shutil
import threading
import types
import wave
from pathlib import Path

import httpx
import pytest

from mnema import node_settings
from mnema.client import MnemaClient
from mnema.connection import Connection
from mnema.core import audio, base
from mnema.core.base import MediaError
from mnema.live import meta, recording_client, setup
from mnema.live.setup import PROCESSING_MARK, FinalizeCancelled, LiveConfig
from mnema.llm import claude, limits, providers

NAME = "2026-10-22_13-05"
WORDS = "Обсудили подготовку выпуска и проверку ошибок перед демонстрацией. " * 12


_ASYNC_CLIENT = MnemaClient._make_async_client


class Crash(BaseException):
    """The app crashed mid-processing (not an exception of the operation)."""


@pytest.fixture
def engines(monkeypatch) -> dict:
    """Installed recognition engines: whisperx is there, the rest as on the machine."""
    installed: dict = {"whisperx": object()}
    find = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda n, *a, **kw: installed[n] if n in installed else find(n, *a, **kw),
    )
    return installed


@pytest.fixture
def sources(monkeypatch, engines) -> list[Path]:
    """A Mac without a server: no network to a server; where the operation reads the audio."""
    monkeypatch.setattr(base, "STANDALONE", True)
    monkeypatch.setenv("MNEMA_RUNNER", "")
    monkeypatch.setattr(providers, "TRANSPORT", httpx.MockTransport(lambda r: httpx.Response(404)))
    monkeypatch.setattr(limits, "_FETCHED", {})
    read: list[Path] = []

    def extract(src, dst, **kw):
        read.append(Path(src))
        return Path(shutil.copyfile(src, dst))

    monkeypatch.setattr(audio, "extract_audio", extract)
    monkeypatch.setattr(audio, "mix", lambda inputs, dst, **kw: dst.write_bytes(b"ID3") and dst)
    monkeypatch.setattr(
        MnemaClient, "_make_async_client", lambda self: pytest.fail("the Mac has no server")
    )
    return read


def _asr(monkeypatch, crash: list[bool] | None = None) -> None:
    def transcribe(op, path, params, **kw):
        if crash and crash.pop():
            raise Crash
        return {
            "text": WORDS,
            "segments": [{"start": 0, "end": 1, "text": WORDS}],
            "language": "ru",
            "engine": "whisperx",
        }

    monkeypatch.setattr(setup, "dispatch", transcribe)


def _model(monkeypatch, title: str = "Подготовка выпуска") -> list[dict]:
    import litellm

    calls: list[dict] = []
    answer = {"title": title, "about": "Готовим выпуск.", "summary": ["[0:00] Проверить ошибки"]}

    def completion(**kw):
        calls.append(kw)
        delta = types.SimpleNamespace(content=json.dumps(answer, ensure_ascii=False))
        choice = types.SimpleNamespace(delta=delta, finish_reason="stop")
        return iter([types.SimpleNamespace(choices=[choice])])

    monkeypatch.setattr(litellm, "completion", completion)
    monkeypatch.setattr(claude.time, "sleep", lambda s: None)
    return calls


def _recording(root: Path, name: str = NAME) -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    for label in ("mic", "system"):
        with wave.open(str(folder / f"{label}.wav"), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(16000)
            out.writeframes(array.array("h", [3000] * 16000).tobytes())
    return folder


def _files(folder: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in folder.iterdir()}


def _server(monkeypatch, tmp_path) -> Connection:
    """A Linux server: a real create_app over ASGI."""
    from mnema.server import create_app

    transport = httpx.ASGITransport(app=create_app(tmp_path / "server"))
    monkeypatch.setattr(MnemaClient, "_make_async_client", _ASYNC_CLIENT)
    monkeypatch.setattr(
        recording_client,
        "MnemaClient",
        lambda url, *, token=None: MnemaClient(url, token=token, transport=transport),
    )
    return Connection(url="http://node:8123")


def test_mac_processes_like_the_server_without_it(monkeypatch, tmp_path, sources) -> None:
    """A recording processed on the Mac has the same files and folder name as after the server;
    no server in the history, no temporary folders next to the recording."""
    _asr(monkeypatch)
    calls = _model(monkeypatch)
    here, there = tmp_path / "mac", tmp_path / "linux"
    result, full = recording_client.recover_on_node(
        _recording(here), LiveConfig(mix_audio=False), None
    )
    assert sorted(sources) == [here / NAME / "mic.wav", here / NAME / "system.wav"]  # in place
    monkeypatch.setattr(base, "STANDALONE", False)
    served, _ = recording_client.recover_on_node(
        _recording(there), LiveConfig(mix_audio=False), _server(monkeypatch, tmp_path)
    )
    assert result.base.name == served.base.name == "2026-10-22_Подготовка выпуска"
    assert sorted(_files(result.base)) == sorted(_files(served.base))
    assert full and "Готовим выпуск." in (result.base / "summary.txt").read_text(encoding="utf-8")
    assert [p.name for p in here.iterdir()] == [result.base.name]
    run = meta.last(result.base)
    assert run is not None and run.status == "ok"
    assert run.settings["server"] is None and meta.last(served.base).settings["server"]
    assert run.settings["node"] == node_settings.machine_name()
    assert len(calls) == 2


def test_mac_summary_has_no_server_and_no_temporary_folder(monkeypatch, tmp_path, sources):
    _model(monkeypatch, "Созвон о релизе")
    folder = tmp_path / "notes" / NAME
    folder.mkdir(parents=True)
    (folder / "transcript.txt").write_text(f"[0:00] mic: {WORDS}\n", encoding="utf-8")
    result = recording_client.summarize_on_node(folder, None, lang="ru")
    assert result.base.name == "2026-10-22_Созвон о релизе" and result.summary_md is not None
    run = meta.last(result.base)
    assert run is not None and (run.kind, run.status) == ("summary", "ok")
    assert run.settings["server"] is None
    assert [p.name for p in (tmp_path / "notes").iterdir()] == [result.base.name]
    assert not (result.base / PROCESSING_MARK).exists()


@pytest.mark.parametrize("step", ["recording", "summary"])
def test_stop_on_mac_leaves_files_and_history(monkeypatch, tmp_path, sources, step) -> None:
    """Stopping processing: the exception from on propagates as is, the recording's files and
    history stay."""
    _asr(monkeypatch)
    calls = _model(monkeypatch)
    folder = _recording(tmp_path / "notes")
    (folder / "transcript.txt").write_text(f"[0:00] mic: {WORDS}\n", encoding="utf-8")
    meta.append(folder, meta.Run(status="ok"))
    before = _files(folder)
    stop = "Распознаю микрофон…" if step == "recording" else "Составляю пересказ и название…"

    def on(fraction, message):
        if message == stop or (step == "summary" and fraction is not None):
            raise FinalizeCancelled

    with pytest.raises(FinalizeCancelled):
        if step == "recording":
            recording_client.recover_on_node(folder, LiveConfig(mix_audio=False), None, on=on)
        else:
            recording_client.summarize_on_node(folder, None, on=on)
    recording_client.drop_request(folder)  # what the app does after a stop
    after = _files(folder)
    runs = [json.loads(f[meta.META_FILE])["runs"] for f in (before, after)]
    assert runs[0] == runs[1] and after.keys() == before.keys()
    assert {k: v for k, v in after.items() if k != meta.META_FILE} == {
        k: v for k, v in before.items() if k != meta.META_FILE
    }
    assert [p.name for p in (tmp_path / "notes").iterdir()] == [NAME]
    assert calls == []


def test_restart_resubmits_and_moved_folder_is_read_from_its_new_place(
    monkeypatch, tmp_path, sources
) -> None:
    """The app crashed mid-processing and the folder moved to a section: the next processing
    submits the job again and reads the audio from the new path."""
    crash = [True]
    _asr(monkeypatch, crash)
    _model(monkeypatch)
    root = tmp_path / "notes"
    folder = _recording(root)
    with pytest.raises(Crash):
        recording_client.recover_on_node(folder, LiveConfig(mix_audio=False), None)
    work = json.loads((folder / PROCESSING_MARK).read_text(encoding="utf-8"))
    assert work["node"] == "" and work["job"]
    moved = root / "Работа" / NAME
    moved.parent.mkdir()
    folder.rename(moved)
    sources.clear()
    result, full = recording_client.recover_on_node(moved, LiveConfig(mix_audio=False), None)
    assert full and result.base.parent == moved.parent
    assert sources and all(p.parent == moved for p in sources)
    run = meta.last(result.base)
    assert run is not None and run.status == "ok" and run.job != work["job"]
    assert not (result.base / PROCESSING_MARK).exists()


def test_folder_in_processing_is_not_processed_again(monkeypatch, tmp_path, sources) -> None:
    """The app processes a recording while `mnema finish` of the same folder runs from a
    terminal: the second is refused, the first one's work file stays, one run in the history."""
    _model(monkeypatch)
    started, release = threading.Event(), threading.Event()

    def transcribe(op, path, params, **kw):
        started.set()
        assert release.wait(timeout=10)
        return {
            "text": WORDS,
            "segments": [{"start": 0, "end": 1, "text": WORDS}],
            "language": "ru",
            "engine": "whisperx",
        }

    monkeypatch.setattr(setup, "dispatch", transcribe)
    folder = _recording(tmp_path / "notes")
    done: list = []
    app = threading.Thread(
        target=lambda: done.append(
            recording_client.recover_on_node(folder, LiveConfig(mix_audio=False), None)
        )
    )
    app.start()
    try:
        assert started.wait(timeout=10)
        work = (folder / PROCESSING_MARK).read_bytes()
        with pytest.raises(MediaError) as busy:
            recording_client.recover_on_node(folder, LiveConfig(lang="en", mix_audio=False), None)
        assert str(busy.value) == "Запись уже обрабатывается"
        assert (folder / PROCESSING_MARK).read_bytes() == work
    finally:
        release.set()
        app.join(timeout=10)
    result, full = done[0]
    assert full and result.base.name == "2026-10-22_Подготовка выпуска"
    runs = json.loads((result.base / meta.META_FILE).read_text(encoding="utf-8"))["runs"]
    assert [run["status"] for run in runs] == ["ok"]


def test_mac_refuses_unavailable_recognition_before_work(monkeypatch, tmp_path, sources, engines):
    """The chosen recognition is not installed: refused before work, the recording as it was."""
    engines["gigaam"] = None
    _asr(monkeypatch)
    folder = _recording(tmp_path / "notes")
    before = _files(folder)
    with pytest.raises(MediaError) as refused:
        recording_client.recover_on_node(folder, LiveConfig(asr="gigaam", mix_audio=False), None)
    assert str(refused.value) == "Выбранное распознавание недоступно. Выберите другое"
    assert not (folder / PROCESSING_MARK).exists() and meta.last(folder) is None
    assert set(_files(folder)) <= {*before, meta.META_FILE}
    assert sources == []


def test_mac_cloud_recognition_without_a_key_is_refused_before_work(monkeypatch, tmp_path, sources):
    """A Mac without a server recognizes in the cloud with its own key; without one the
    recording is refused before work and the audio goes nowhere."""
    monkeypatch.delenv("MNEMA_ASR_API_KEY", raising=False)
    _asr(monkeypatch)
    folder = _recording(tmp_path / "notes")
    config = LiveConfig(asr="openai/gpt-4o-transcribe", mix_audio=False)
    with pytest.raises(MediaError) as refused:
        recording_client.recover_on_node(folder, config, None)
    assert str(refused.value) == (
        "Нет ключа OpenAI для распознавания. Задайте его в настройках Mnema на Mac"
    )
    assert not (folder / PROCESSING_MARK).exists() and meta.last(folder) is None
    assert sources == []


def test_cloud_choice_saved_for_mac_records_with_own_engines(
    monkeypatch, tmp_path, sources
) -> None:
    """The Mac's bar.json has cloud recognition while this Mac processes: the recording uses
    its own engines and the settings show "Automatic"."""
    from mnema.bar.runtime import BarRuntime
    from mnema.bar.settings_model import settings_model

    monkeypatch.setenv("MNEMA_LIVE_DIR", str(tmp_path / "notes"))
    _asr(monkeypatch)
    _model(monkeypatch)
    prefs = tmp_path / "bar.json"
    prefs.write_text(json.dumps({"asr": "openai/gpt-4o-transcribe"}), encoding="utf-8")
    folder = _recording(tmp_path / "notes")
    rt = BarRuntime(prefs_file=prefs)
    rt.recover(folder)
    rt.join(timeout=10)
    assert rt.state.error == ""
    run = meta.last(tmp_path / "notes" / "2026-10-22_Подготовка выпуска")
    assert run is not None and run.status == "ok"
    model = settings_model(rt.prefs, rt.menu_env())
    field = next(
        f for t in model.tabs for s in t.sections for f in s.fields if f.title == "Распознавание"
    )
    assert field.value == "Автоматически"
    assert [c.title for c in field.choices if c.checked] == ["Автоматически"]


@pytest.mark.parametrize(
    ("saved", "asked"),
    [
        ({"asr": "gigaam", "where": "server"}, None),  # "On server" saved, but no server now
        ({}, "gigaam"),  # "Retry" here of a recording asked from the server's list
    ],
)
def test_gigaam_chosen_elsewhere_records_here_with_own_engines(
    monkeypatch, tmp_path, sources, engines, saved, asked
) -> None:
    """GigaAM is not in the Mac app: processing here with it chosen runs Automatic, as a cloud
    choice does, instead of being refused."""
    from mnema.bar.runtime import BarRuntime

    engines["gigaam"] = None
    monkeypatch.setenv("MNEMA_LIVE_DIR", str(tmp_path / "notes"))
    _asr(monkeypatch)
    _model(monkeypatch)
    prefs = tmp_path / "bar.json"
    prefs.write_text(json.dumps(saved), encoding="utf-8")
    folder = _recording(tmp_path / "notes")
    rt = BarRuntime(prefs_file=prefs)
    rt.recover(folder, asr=asked)
    rt.join(timeout=10)
    assert rt.state.error == ""
    run = meta.last(tmp_path / "notes" / "2026-10-22_Подготовка выпуска")
    assert run is not None and run.status == "ok"
    assert run.settings["asr"] == "auto"


def test_server_refusal_drops_the_request(monkeypatch, tmp_path, engines) -> None:
    """The server refused before work (422): the refusal has no commands, there is no work
    file with a request: the recording was not "interrupted", it stays as it was."""
    monkeypatch.setattr(audio, "extract_audio", lambda src, dst, **kw: shutil.copyfile(src, dst))
    engines["gigaam"] = None
    folder = _recording(tmp_path / "notes")
    with pytest.raises(MediaError) as refused:
        recording_client.recover_on_node(
            folder, LiveConfig(asr="gigaam", mix_audio=False), _server(monkeypatch, tmp_path)
        )
    assert str(refused.value) == "Выбранное распознавание недоступно. Выберите другое"
    assert not (folder / PROCESSING_MARK).exists() and meta.last(folder) is None


@pytest.mark.parametrize("where", ["mac", "server"])
def test_refused_part_leaves_only_the_original_recording(
    monkeypatch, tmp_path, engines, where
) -> None:
    """A refusal before work for a part: no part folder is left, the library as it was."""
    monkeypatch.setattr(audio, "extract_audio", lambda src, dst, **kw: shutil.copyfile(src, dst))
    engines["gigaam"] = None
    root = tmp_path / "notes"
    folder = _recording(root)
    before = _files(folder)
    if where == "mac":
        monkeypatch.setattr(base, "STANDALONE", True)
        connection = None
    else:
        connection = _server(monkeypatch, tmp_path)
    with pytest.raises(MediaError) as refused:
        recording_client.recover_on_node(
            folder, LiveConfig(asr="gigaam", mix_audio=False), connection, start=0.2, end=0.8
        )
    assert str(refused.value) == "Выбранное распознавание недоступно. Выберите другое"
    assert [p.name for p in root.iterdir()] == [NAME] and _files(folder) == before


def test_busy_server_keeps_the_request_and_its_uploads(monkeypatch, tmp_path, engines) -> None:
    """A full server queue (429) is not a refusal: the request and uploads stay, a retry does
    not upload the audio again."""
    from mnema.server import create_app

    monkeypatch.setattr(audio, "extract_audio", lambda src, dst, **kw: shutil.copyfile(src, dst))
    _asr(monkeypatch)
    _model(monkeypatch)
    server = httpx.ASGITransport(app=create_app(tmp_path / "server"))
    busy, uploads = [True], []

    async def network(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/jobs" and busy and busy.pop():
            return httpx.Response(429, json={"detail": "Сервер занят: очередь заполнена"})
        if request.method == "POST" and request.url.path == "/api/uploads":
            uploads.append(request.url.path)
        return await server.handle_async_request(request)

    monkeypatch.setattr(MnemaClient, "_make_async_client", _ASYNC_CLIENT)
    monkeypatch.setattr(
        recording_client,
        "MnemaClient",
        lambda url, *, token=None: MnemaClient(
            url, token=token, transport=httpx.MockTransport(network)
        ),
    )
    connection = Connection(url="http://node:8123")
    folder = _recording(tmp_path / "notes")
    config = LiveConfig(asr="whisper", mix_audio=False)
    with pytest.raises(MediaError, match="^Сервер занят: очередь заполнена$"):
        recording_client.recover_on_node(folder, config, connection)
    assert (folder / PROCESSING_MARK).is_file() and len(uploads) == 2
    result, _ = recording_client.recover_on_node(folder, config, connection)
    run = meta.last(result.base)
    assert run is not None and run.status == "ok" and len(uploads) == 2


def test_linux_without_server_keeps_the_recording(tmp_path) -> None:
    """Linux processes only on a server: without one it is refused, the recording intact."""
    folder = _recording(tmp_path / "notes")
    before = _files(folder)
    with pytest.raises(MediaError) as refused:
        recording_client.recover_on_node(folder, LiveConfig(mix_audio=False), None)
    assert str(refused.value) == "Сервер не подключён. Запись сохранена, обработайте позже"
    assert _files(folder) == before
