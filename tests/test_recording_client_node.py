"""The recording client against a real node (create_app over ASGI): retries lose nothing.

External boundaries: transcription (setup.dispatch), ffmpeg and ffprobe, litellm, provider
network.
"""

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

from mnema import core, node_settings
from mnema.connection import Connection
from mnema.core import audio
from mnema.live import meta, recording_client, setup
from mnema.live.setup import FinalizeCancelled, LiveConfig
from mnema.llm import claude, limits, providers

NODE = Connection(url="http://node:8123")
WORDS = "Обсудили подготовку выпуска и проверку ошибок перед демонстрацией. " * 12


def _answer(title: str) -> str:
    return json.dumps(
        {"title": title, "about": "Готовим выпуск.", "summary": ["[0:00] Проверить ошибки"]},
        ensure_ascii=False,
    )


@pytest.fixture(autouse=True)
def node(monkeypatch, tmp_path):
    from mnema.client import MnemaClient
    from mnema.server import create_app

    find = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda n, *a, **kw: object() if n == "whisperx" else find(n, *a, **kw),
    )
    monkeypatch.setattr(providers, "TRANSPORT", httpx.MockTransport(lambda r: httpx.Response(404)))
    monkeypatch.setattr(limits, "_FETCHED", {})
    monkeypatch.setattr(core, "probe", lambda p: types.SimpleNamespace(duration=2))
    monkeypatch.setattr(audio, "extract_audio", lambda src, dest, **kw: shutil.copyfile(src, dest))
    node_settings.node_file().parent.mkdir(parents=True, exist_ok=True)
    node_settings.node_file().write_text(json.dumps({"name": "Мой узел"}))
    transport = httpx.ASGITransport(app=create_app(tmp_path / "node"))
    monkeypatch.setattr(
        recording_client,
        "MnemaClient",
        lambda url, *, token=None: MnemaClient(url, token=token, transport=transport),
    )


def _asr(monkeypatch, *, failing=()):
    def transcribe(op, path, params, **kw):
        if Path(path).stem in failing:
            raise OSError("provider refused")
        return {
            "text": WORDS,
            "segments": [{"start": 0, "end": 1, "text": WORDS}],
            "language": "ru",
            "engine": "whisperx",
        }

    monkeypatch.setattr(setup, "dispatch", transcribe)


def _model(monkeypatch, answer):
    import litellm

    def completion(**kw):
        choice = types.SimpleNamespace(
            delta=types.SimpleNamespace(content=answer()), finish_reason="stop"
        )
        return iter([types.SimpleNamespace(choices=[choice])])

    monkeypatch.setattr(litellm, "completion", completion)
    monkeypatch.setattr(claude.time, "sleep", lambda s: None)


def _recording(tmp_path, name="2026-10-22_13-05", seconds=1):
    base = tmp_path / "notes" / name
    base.mkdir(parents=True)
    for label in ("mic", "system"):
        with wave.open(str(base / f"{label}.wav"), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(16000)
            out.writeframes(array.array("h", [3000] * 16000 * seconds).tobytes())
    return base


@pytest.mark.parametrize("chosen", [None, "Созвон 1/2"])
def test_title_with_slash_allows_summary_and_reprocessing(monkeypatch, tmp_path, chosen):
    _asr(monkeypatch)
    _model(monkeypatch, lambda: _answer("Обсуждение CI/CD"))
    name = f"2026-10-22_{'Созвон 1 2' if chosen else 'Обсуждение CI CD'}"
    result, _ = recording_client.recover_on_node(
        _recording(tmp_path),
        LiveConfig(mix_audio=False),
        NODE,
        title=chosen,
    )
    assert result.base.name == name
    summary = recording_client.summarize_on_node(result.base, NODE)
    assert meta.last(summary.base).kind == "summary" and meta.last(summary.base).status == "ok"
    again, _ = recording_client.recover_on_node(summary.base, LiveConfig(mix_audio=False), NODE)
    assert meta.last(again.base).status == "ok" and again.base.name == name


def test_failed_track_of_new_recording_keeps_the_recognized_half(monkeypatch, tmp_path):
    _model(monkeypatch, lambda: _answer("Подготовка выпуска"))
    _asr(monkeypatch, failing={"system"})
    result, full = recording_client.recover_on_node(
        _recording(tmp_path), LiveConfig(mix_audio=False), NODE
    )
    text = result.transcript_txt.read_text(encoding="utf-8")
    assert "mic:" in text and "system:" not in text and full == text
    assert result.base.name == "2026-10-22_Подготовка выпуска"
    run = meta.last(result.base)
    assert run.status == "ok" and run.tracks["system"].error and not run.tracks["mic"].error
    assert any(
        "«Системный звук»" in remark.cause
        and remark.effect == "Речи с этой дорожки в расшифровке нет"
        for remark in result.remarks
    )


def test_failed_track_on_reprocessing_keeps_transcript_summary_and_name(monkeypatch, tmp_path):
    _model(monkeypatch, lambda: _answer("Другое название"))
    base = _recording(tmp_path, "2026-10-22_Мой созвон")
    (base / "transcript.txt").write_text("[0:00] mic: прежнее\n\n[0:01] system: прежнее\n")
    (base / "summary.txt").write_text("Мой созвон.\n\nПрежний пересказ.\n")
    _asr(monkeypatch, failing={"system"})
    result, full = recording_client.recover_on_node(
        base, LiveConfig(mix_audio=False), NODE, retitle=True
    )
    assert result.base == base and result.transcript_txt is None and full == ""
    assert (
        base / "transcript.txt"
    ).read_text() == "[0:00] mic: прежнее\n\n[0:01] system: прежнее\n"
    assert (base / "summary.txt").read_text() == "Мой созвон.\n\nПрежний пересказ.\n"
    run = meta.last(base)
    assert run.status == "failed" and run.tracks["system"].error and not run.tracks["mic"].error
    assert any(
        "«Системный звук»" in remark.cause and remark.effect == "Новая расшифровка не сохранена"
        for remark in result.remarks
    )


def test_language_change_waits_for_the_cancelled_job_on_node(monkeypatch, tmp_path):
    _asr(monkeypatch)
    entered, resume = threading.Event(), threading.Event()

    def answer():
        entered.set()
        assert resume.wait(10)
        return _answer("Подготовка выпуска")

    _model(monkeypatch, answer)
    base = _recording(tmp_path)

    def stop(fraction, message):
        if entered.is_set():  # the node waits for the model: another language was chosen
            raise FinalizeCancelled

    with pytest.raises(FinalizeCancelled):
        recording_client.recover_on_node(
            base, LiveConfig(lang="ru", mix_audio=False), NODE, on=stop
        )
    timer = threading.Timer(0.5, resume.set)  # the model answered and the cancel reached the node
    timer.start()
    try:
        result, full = recording_client.recover_on_node(
            base, LiveConfig(lang="en", mix_audio=False), NODE
        )
    finally:
        resume.set()
        timer.cancel()
    run = meta.last(result.base)
    assert run.status == "ok" and run.settings["lang"] == "en" and full


def test_reprocessing_one_track_leaves_no_old_mixed_recording(monkeypatch, tmp_path):
    """A retry with the microphone only: the old recording.mp3 with system audio is gone."""
    _asr(monkeypatch)
    _model(monkeypatch, lambda: _answer("Подготовка выпуска"))
    base = _recording(tmp_path)
    (base / "recording.mp3").write_bytes(b"mic and system")
    result, _ = recording_client.recover_on_node(base, LiveConfig(), NODE, tracks={"mic"})
    assert meta.last(result.base).status == "ok"
    assert not (result.base / "recording.mp3").exists()
    assert all(path.name != "recording.mp3" for path in result.recordings)


def test_part_is_processed_in_its_own_folder(monkeypatch, tmp_path):
    """A part of a recording ("Process recording…" with a start and an end) gets its own folder
    next to it: the client reports which folder it processes and then its new name with the
    part mark; the part's history has the processed range; the full recording is untouched."""
    _asr(monkeypatch)
    _model(monkeypatch, lambda: _answer("Подготовка выпуска"))
    base = _recording(tmp_path, seconds=120)
    files = {path.name: path.read_bytes() for path in base.iterdir()}
    seen: list[Path] = []
    result, _ = recording_client.recover_on_node(
        base,
        LiveConfig(lang="ru", mix_audio=False),
        NODE,
        start=30.0,
        end=90.0,
        title="Дейли",
        on_base=seen.append,
    )
    part = base.with_name(f"{base.name} (0.00.30–0.01.30)")
    named = base.with_name("2026-10-22_Дейли (0.00.30–0.01.30)")
    assert seen == [part, named, named] and result.base == named
    run = meta.last(named)
    assert run.part == {"start": 30.0, "end": 90.0} and run.tracks["mic"].seconds == 60.0
    assert {path.name: path.read_bytes() for path in base.iterdir()} == files


def test_part_keeps_the_manual_name_of_the_recording(monkeypatch, tmp_path):
    from mnema.bar.state import title_of

    _asr(monkeypatch)
    _model(monkeypatch, lambda: _answer("Другое название"))
    base = setup.rename_session(_recording(tmp_path, seconds=120), "Дейли: API-клиент")
    result, _ = recording_client.recover_on_node(
        base, LiveConfig(lang="ru", mix_audio=False), NODE, start=30.0, end=90.0
    )
    assert result.base.name == "2026-10-22_Дейли API-клиент (0.00.30–0.01.30)"
    assert title_of(result.base) == "Дейли API-клиент (0.00.30–0.01.30)"


def test_record_waiting_on_the_node_is_not_moved_to_a_section(monkeypatch, tmp_path):
    """Linux: `mnema finish` waits for the server while the app moves the recording to a
    section: the move is refused as already processing, the recording stays, and processing
    finishes in it."""
    from mnema.bar.recordings import move_record
    from mnema.core.base import MediaError

    _model(monkeypatch, lambda: _answer("Подготовка выпуска"))
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
    folder = _recording(tmp_path)
    (folder.parent / "Работа").mkdir()
    done: list = []
    cli = threading.Thread(
        target=lambda: done.append(
            recording_client.recover_on_node(folder, LiveConfig(mix_audio=False), NODE)
        )
    )
    cli.start()
    try:
        assert started.wait(timeout=10)
        with pytest.raises(MediaError, match="Запись уже обрабатывается"):
            move_record(folder.parent, folder, "Работа")
        assert folder.is_dir() and not any((folder.parent / "Работа").iterdir())
    finally:
        release.set()
        cli.join(timeout=10)
    result, _ = done[0]
    assert result.base.parent == folder.parent and meta.last(result.base).status == "ok"
