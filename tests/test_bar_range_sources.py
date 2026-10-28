"""Sources in the Process recording window: only the folder's tracks, all checked (the server
decides silence), at least one required; an unchecked track is not uploaded, not recognized and not
mixed into the shared mp3. Process… on an unprocessed and on the last recording is the same
window."""

from __future__ import annotations

import array
import types
import wave
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_bar import _node_pipeline, _Notes, _wav
from test_bar_headless import _Out

from mnema.bar.headless import Channel, ProtocolPlatform
from mnema.bar.range_pick import RangeAnswer, RangeRequest, SourceChoice
from mnema.bar.runtime import BarRuntime
from mnema.bar.state import MenuEnv, Phase, menu

MIC = SourceChoice(label="mic", title="Микрофон")
SYSTEM = SourceChoice(label="system", title="Системный звук")


def _quiet(path: Path, frames: int = 16000 * 120) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(array.array("h", [0] * frames).tobytes())


class _Window(_Notes):
    """The Process recording window (the GUI boundary): records requests, answers with answer()."""

    def __init__(self, answer) -> None:
        super().__init__()
        self.answer = answer
        self.requests: list[RangeRequest] = []

    def ask_range(self, req, on_done) -> None:
        self.requests.append(req)
        on_done(self.answer(req))

    def trash(self, path) -> None: ...


def _process(folder: Path) -> str:
    """Process… on the card of recording folder in its root's feed."""
    from mnema.bar.recordings import library

    [card] = [c for c in library(folder.parent).cards if c.path == folder]
    return f"record:{card.id}:process"


def _whole(req: RangeRequest, sources: list[str] | None = None) -> RangeAnswer:
    chosen = sources if sources is not None else [s.label for s in req.sources if s.on]
    return RangeAnswer(start="", end="", title=req.name, sources=chosen or None)


@pytest.fixture
def notes(monkeypatch, tmp_path) -> Path:
    root = tmp_path / "notes"
    root.mkdir()
    monkeypatch.setenv("MNEMA_LIVE_DIR", str(root))
    monkeypatch.setattr("mnema.bar.runtime._system_available", lambda: False)
    return root


def test_request_lists_folder_tracks_all_checked(tmp_path, notes) -> None:
    """The window has only the folder's tracks, a silent one checked too: the server decides
    silence."""
    both, mic_only, silent = notes / "Созвон", notes / "Диктовка", notes / "Тишина"
    _wav(both / "mic.wav", frames=16000 * 120)
    _quiet(both / "system.wav")
    _wav(mic_only / "mic.wav", frames=16000 * 120)
    _quiet(silent / "mic.wav")
    _quiet(silent / "system.wav")
    window = _Window(lambda req: None)
    rt = BarRuntime(platform=window, prefs_file=tmp_path / "bar.json")
    for folder in (both, mic_only, silent):
        rt.handle(_process(folder))
    assert [r.sources for r in window.requests] == [[MIC, SYSTEM], [MIC], [MIC, SYSTEM]]
    assert rt.state.phase == Phase.IDLE  # Cancel: nothing is processed


def test_answer_without_sources_is_invalid(tmp_path, notes) -> None:
    """No tracks means no answer: the window does not give one, the protocol treats it as cancel,
    and tracks missing from the folder are not processed."""
    with pytest.raises(ValidationError):
        RangeAnswer(start="", end="", sources=[])
    assert RangeAnswer(start="", end="").sources is None  # the window did not ask: all tracks

    out = _Out()
    platform = ProtocolPlatform(Channel(out))
    got: list = []
    platform.ask_range(RangeRequest(length=60.0, sources=[MIC, SYSTEM]), got.append)
    [ask] = out.rest()
    assert [(c.key, c.checked) for c in ask.ask.sources] == [("mic", True), ("system", True)]
    assert ask.ask.sources_label == "Источники"
    platform.answered(ask.id, {"start": "", "end": "", "sources": []})
    platform.ask_range(RangeRequest(length=60.0, sources=[MIC, SYSTEM]), got.append)
    [again] = out.rest()
    platform.answered(again.id, {"start": "", "end": "", "sources": ["system"]})
    assert got == [None, RangeAnswer(start="", end="", sources=["system"])]

    folder = notes / "Созвон"
    _wav(folder / "mic.wav")
    window = _Window(lambda req: _whole(req, ["system"]))
    rt = BarRuntime(platform=window, prefs_file=tmp_path / "bar.json")
    rt.handle(_process(folder))
    assert rt.state.error == "Не выбран ни один источник" and rt.state.phase == Phase.IDLE


def test_one_chosen_track_is_neither_uploaded_nor_recognized(monkeypatch, tmp_path, notes):
    """Only the microphone is checked: system.wav is not sent to the server, not recognized and not
    mixed into recording.mp3, but stays in the folder; a failed reprocessing keeps the old text."""
    from mnema.client import MnemaClient
    from mnema.core import audio, base
    from mnema.live import setup

    _node_pipeline(monkeypatch, tmp_path)
    monkeypatch.setattr(base, "STANDALONE", False)  # Linux: the connected server processes
    monkeypatch.setenv("MNEMA_RUNNER", "http://node:8123")
    folder = notes / "Созвон"
    _wav(folder / "mic.wav", frames=16000 * 120)
    _wav(folder / "system.wav", frames=16000 * 120)
    uploaded: list[str] = []
    recognized: list[str] = []
    mixed: list[list[str]] = []
    real_upload = MnemaClient.upload_resumable

    def upload(self, path, **kw):
        uploaded.append(Path(path).name)
        return real_upload(self, path, **kw)

    def dispatch(op, src, params, **kw):
        recognized.append(Path(src).name)
        return {"text": "Итоги.", "segments": [{"start": 0.0, "end": 1.0, "text": "Итоги."}]}

    monkeypatch.setattr(MnemaClient, "upload_resumable", upload)
    monkeypatch.setattr(setup, "dispatch", dispatch)
    monkeypatch.setattr(
        audio, "mix", lambda inputs, dst, **kw: mixed.append([p.name for p in inputs]) or dst
    )
    window = _Window(lambda req: _whole(req, ["mic"]))
    rt = BarRuntime(platform=window, prefs_file=tmp_path / "bar.json")
    rt.handle(_process(folder))
    rt.join(timeout=20)
    assert [s.label for s in window.requests[0].sources] == ["mic", "system"]
    assert rt.state.error == "" and uploaded == ["mic.wav"] and recognized == ["mic.wav"]
    assert mixed == [] and (folder / "system.wav").exists()
    text = (folder / "transcript.txt").read_text(encoding="utf-8")
    assert "mic: Итоги." in text and "system" not in text

    def broken(op, src, params, **kw):
        raise RuntimeError("распознавание упало")

    monkeypatch.setattr(setup, "dispatch", broken)
    rt.handle(_process(folder))
    rt.join(timeout=20)
    assert (folder / "transcript.txt").read_text(encoding="utf-8") == text


def test_recover_opens_the_window_for_the_folder(monkeypatch, tmp_path, notes) -> None:
    """Process… on an unprocessed recording and on the last recording without text opens the window
    for that folder; Cancel processes nothing, an answer processes with the choice."""
    folder = notes / "2026-10-01_10-05"
    _wav(folder / "mic.wav")
    seen: list[tuple] = []

    def fake_recover(base, config, connection, **kw):
        seen.append((base, kw["tracks"]))
        from mnema.live.models import LiveResult

        return LiveResult(), ""

    monkeypatch.setattr("mnema.live.recording_client.recover_on_node", fake_recover)
    answers: list = [None, _whole]
    window = _Window(lambda req: answers[0] and answers[0](req))
    rt = BarRuntime(platform=window, prefs_file=tmp_path / "bar.json")
    [unfinished] = [i for i in menu(rt.state, rt.prefs, MenuEnv()) if "Не обработана" in i.title]
    process = next(c for c in unfinished.children if c.title == "Обработать…")
    assert (process.key, process.title) == (f"recover:{folder}", "Обработать…")
    rt.handle(process.key)
    assert [r.length for r in window.requests] == [1.0] and seen == []
    answers.pop(0)
    rt.handle(process.key)
    rt.join(timeout=10)
    assert seen == [(folder, ["mic"])]

    rt.state.last_result = folder  # the last recording without text: the same window
    last = next(i for i in menu(rt.state, rt.prefs, MenuEnv()) if i.title.startswith("Последняя"))
    again = next(c for c in last.children if c.key == f"recover:{folder}")
    assert again.title == "Обработать…"
    rt.handle(again.key)
    rt.join(timeout=10)
    assert len(window.requests) == 3 and seen[-1] == (folder, ["mic"])


def test_linux_zenity_fallback_asks_sources(monkeypatch) -> None:
    """Linux without GTK: after the title, a checklist of tracks (the silent one unchecked); none
    checked — cancel; a single track — no question."""
    import subprocess
    import threading

    from mnema.bar import app_linux

    monkeypatch.setattr(threading.Thread, "start", lambda self: self.run())
    replies: list = []
    seen: list[list[str]] = []

    def fake_run(cmd, **k):
        seen.append(cmd)
        code, out = replies.pop(0)
        return types.SimpleNamespace(returncode=code, stdout=out, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    platform = app_linux.LinuxPlatform()
    got: list = []
    req = RangeRequest(length=60.0, sources=[MIC, SYSTEM.model_copy(update={"on": False})])
    fields = [(0, "0:00:00\n"), (0, "0:01:00\n"), (0, "Дейли\n")]
    replies[:] = [(1, ""), *fields, (0, "mic\n")]
    platform.ask_range(req, got.append)
    assert got[-1].sources == ["mic"]
    checklist = seen[-1]
    assert "--checklist" in checklist and checklist[-6:] == [
        "TRUE", "mic", "Микрофон", "FALSE", "system", "Системный звук",
    ]  # fmt: skip
    replies[:] = [(1, ""), *fields, (0, "\n")]
    platform.ask_range(req, got.append)
    assert got[-1] is None
    replies[:] = [(1, ""), *fields]
    platform.ask_range(RangeRequest(length=60.0, sources=[MIC]), got.append)
    assert got[-1].sources == ["mic"] and not any("--checklist" in c for c in seen[-3:])


def test_process_window_state_reads_in_english() -> None:
    """The Process recording window's own texts follow the interface language: choices, the
    selection caption and language names, with no Russian left in English."""
    from mnema import i18n
    from mnema.bar import range_pick
    from mnema.bar.range_pick import LANGS, RangePick, language_names

    with i18n.using("en"):
        part = RangePick(length=600.0, start=60.0, end=300.0)
        texts = [
            range_pick.TITLE,
            range_pick.NAME_PLACEHOLDER,
            range_pick.REPLACE_LABEL,
            range_pick.SOURCES_LABEL,
            RangePick(length=600.0).caption(),
            part.caption(),
            language_names({"de", "ru", "en"}),
            *(title for _key, title in LANGS),
        ]
        assert language_names({"de", "ru"}) == "Russian and German"
        assert part.caption() == "Selected 0:04:00"
    assert not [t for t in texts if any("а" <= ch.lower() <= "я" or ch in "ёЁ" for ch in t)]
    assert language_names({"de", "ru"}) == "Русский и немецкий"
    assert range_pick.TITLE == "Обработать запись"
