"""A new recording while processing runs: BarRuntime keeps recording and the processing queue
apart. A real BarRuntime and recording folders; external boundaries are audio sources and the
session (test_bar._patch_record), recognition (setup.dispatch: a gate that holds a track until
the test opens it) and ffmpeg."""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from test_bar import NODE, _patch_record, _wav
from test_bar_sync_runtime import Controller

import mnema.bar.runtime as runtime_mod
import mnema.live.session as session_mod
import mnema.live.setup as setup_mod
from mnema.bar.panel import panel_model
from mnema.bar.recordings import library, with_folders
from mnema.bar.runtime import BarRuntime
from mnema.bar.settings_model import settings_model
from mnema.bar.state import (
    MenuEnv,
    Phase,
    menu,
)
from mnema.core import base
from mnema.i18n import set_language
from mnema.live.meta import META_FILE, RecordState
from mnema.sync.scan import FILES_LOCK


class _Gate:
    """Track recognition: every call waits until it is opened (release by index)."""

    def __init__(self) -> None:
        self.calls: list[dict] = []  # call parameters in order
        self._entered = threading.Semaphore(0)
        self._open: list[threading.Event] = []
        self._lock = threading.Lock()
        self.closed = False

    def __call__(self, op, src, params, *, runner=None, local=False, on=None) -> dict:
        with self._lock:
            self.calls.append(dict(params))
            opened = threading.Event()
            if self.closed:
                opened.set()
            self._open.append(opened)
        self._entered.release()
        opened.wait(timeout=10)
        if on is not None:
            on(0.5, "распознаю…")  # stopped while waiting: interrupted here
        return {"text": "привет", "segments": [{"start": 0.0, "end": 1.0, "text": "привет"}]}

    def entered(self) -> None:
        """Wait until the next track reaches recognition."""
        assert self._entered.acquire(timeout=10)

    def release(self, index: int) -> None:
        self._open[index].set()

    def open_all(self) -> None:
        with self._lock:
            self.closed = True
            for opened in self._open:
                opened.set()


class _App:
    """The app: notifications, the title window (answer), a question (yes), folder choice
    (chosen), the "Process recording" window (ranges), the Trash (trashed)."""

    def __init__(self) -> None:
        self.notes: list[tuple[str, str]] = []
        self.asked: list[str | None] = []
        self.answer: str | None = None
        self.questions: list[tuple[str, str, str, str]] = []
        self.yes = False
        self.chosen: Path | None = None
        self.ranges: list[Any] = []
        self.trashed: list[Path] = []

    def choose_folder(self, start: Path, on_done: Callable[[Path | None], None]) -> None:
        on_done(self.chosen)

    def ask_range(self, req: Any, on_done: Callable[[Any], None]) -> None:
        self.ranges.append(req)

    def notify(self, title: str, message: str, path: Path | None) -> None:
        self.notes.append((title, message))

    def ask_title(self, current, on_done: Callable[[str | None], None], **kw) -> None:
        self.asked.append(current)
        on_done(self.answer)

    def confirm(self, title, message, ok, on_done, *, cancel="Отменить") -> None:
        self.questions.append((title, message, ok, cancel))
        on_done(self.yes)

    def open(self, path: Any) -> None: ...

    def trash(self, path: Any) -> None:
        self.trashed.append(path)


class _OpenWindows(_App):
    """Title windows stay open until the test answers (answer_window)."""

    def __init__(self) -> None:
        super().__init__()
        self.windows: list[tuple[str | None, Callable[[str | None], None]]] = []

    def ask_title(self, current, on_done: Callable[[str | None], None], **kw) -> None:
        self.asked.append(current)
        self.windows.append((current, on_done))

    def answer_window(self, value: str | None) -> None:
        _current, on_done = self.windows.pop(0)
        on_done(value)


@pytest.fixture
def setup(monkeypatch, tmp_path):
    captured: dict = {}
    _patch_record(monkeypatch, tmp_path, captured)
    monkeypatch.setattr("mnema.bar.runtime._system_available", lambda: False)
    monkeypatch.delenv("MNEMA_MODEL")  # no model: a transcript without summary or title
    gate = _Gate()
    monkeypatch.setattr(setup_mod, "dispatch", gate)
    app = _App()
    rt = BarRuntime(platform=app, prefs_file=tmp_path / "bar.json")
    rt.prefs.system = False  # one track, one recognition call per recording
    yield rt, captured, gate, app
    gate.open_all()
    rt.join(timeout=10)


def _until(check: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def _record(rt: BarRuntime, captured: dict, *, stop: bool = True) -> Path:
    """Record with the shortcut: start, wait for the sources, stop; the recording is queued."""
    captured["started"] = threading.Event()
    rt.handle("toggle_record")
    assert rt.state.phase == Phase.RECORDING
    assert captured["started"].wait(timeout=10)
    folder = rt.active_folder
    assert folder is not None
    if stop:
        rt.handle("toggle_record")
        _until(lambda: rt.state.phase == Phase.IDLE and folder in rt.job_folders())
    return folder


def _folder(root: Path, name: str) -> Path:
    folder = root / name
    _wav(folder / "mic.wav")
    return folder


def _ids(rt: BarRuntime) -> list[tuple[str, bool]]:
    return [(job.id, job.running) for job in rt.state.jobs]


def _card(folder: Path):
    [card] = [c for c in library(folder.parent).cards if c.path == folder]
    return card


def _state(folder: Path) -> RecordState:
    return _card(folder).state


def _runs(folder: Path) -> list:
    """The recording's processing history."""
    try:
        return json.loads((folder / META_FILE).read_text(encoding="utf-8")).get("runs", [])
    except FileNotFoundError:
        return []


def _id(folder: Path) -> str:
    [card] = [c for c in library(folder.parent).cards if c.path == folder]
    return card.id


def test_new_recording_starts_while_previous_is_processed(setup, tmp_path) -> None:
    """While the previous recording is recognized, the shortcut starts a new one; on stop the
    panel shows "Start recording" again, and the new one waits behind the previous one and
    starts when it ends."""
    rt, captured, gate, app = setup
    first = _record(rt, captured)
    gate.entered()  # the first one is recognized
    assert rt.state.phase == Phase.IDLE and rt.state.running is not None

    second = _record(rt, captured, stop=False)  # a new recording while it is recognized
    assert rt.state.phase == Phase.RECORDING and second != first
    items = menu(rt.state, rt.prefs, MenuEnv())  # the tray while recording shows processing too
    job = rt.state.running
    assert job is not None and job.folder == first
    assert f"record:{job.id}:stop" in [i.key for i in items]
    rt.handle("toggle_record")
    _until(lambda: rt.state.phase == Phase.IDLE and second in rt.job_folders())

    assert rt.job_folders() == [first, second] and len(gate.calls) == 1
    assert [running for _, running in _ids(rt)] == [True, False]
    assert rt.state.unfinished == []  # queued, so not "Not processed"
    titles = [i.title for i in menu(rt.state, rt.prefs, MenuEnv())]
    assert "В очереди: 1" in titles and "Начать запись" in titles
    gate.release(0)
    gate.entered()  # the second starts when the first ends
    assert rt.job_folders() == [second]
    assert _state(first) is RecordState.PROCESSED
    gate.release(1)
    rt.join(timeout=10)
    assert rt.state.jobs == [] and _state(second) is RecordState.PROCESSED
    assert [t for t, _ in app.notes].count("Запись обработана") == 2


def test_title_window_waits_while_a_new_recording_is_made(setup) -> None:
    """The title window after processing does not open while a new recording is made; it opens
    after processing when nothing is recorded."""
    rt, captured, gate, app = setup
    _record(rt, captured)
    gate.entered()
    _record(rt, captured, stop=False)
    gate.release(0)
    _until(lambda: len(rt.state.jobs) == 0)
    assert app.asked == []  # recording: no question
    rt.handle("toggle_record")
    gate.entered()
    gate.release(1)
    rt.join(timeout=10)
    assert app.asked == [None]


def test_stop_running_keeps_audio_and_unqueue_waiting_keeps_history(setup, tmp_path) -> None:
    """Stopping the running one ("Stop processing") keeps the audio, the history says stopped
    and the queue goes on; "Remove from queue" of a waiting one leaves it as is, no history."""
    rt, _captured, gate, app = setup
    notes = tmp_path / "notes"
    a, b, c = (_folder(notes, f"2026-10-2{i}_10-00") for i in (1, 2, 3))
    for folder in (a, b, c):
        rt.recover(folder)
    gate.entered()
    ida, idb, idc = (_id(f) for f in (a, b, c))
    assert _ids(rt) == [(ida, True), (idb, False), (idc, False)]

    rt.handle(f"record:{idb}:stop")  # the waiting one leaves the queue
    assert _ids(rt) == [(ida, True), (idc, False)]
    rt.handle(f"record:{ida}:stop")  # stop the running one
    gate.release(0)
    gate.entered()  # the next one starts
    assert _ids(rt) == [(idc, True)]
    gate.release(1)
    rt.join(timeout=10)

    assert _state(a) is RecordState.AUDIO_ONLY
    assert _runs(b) == [] and (b / "mic.wav").exists()
    assert _state(c) is RecordState.PROCESSED and (c / "transcript.txt").exists()
    assert ("Обработка остановлена", "Звук сохранён, обработать можно позже") in app.notes


def test_language_from_the_card_restarts_only_that_record(setup, tmp_path) -> None:
    """Picking "Language: English" in the running one's menu redoes it strictly in English;
    the waiting one behind it stays put and goes by speech."""
    rt, _captured, gate, _app = setup
    notes = tmp_path / "notes"
    a, b = _folder(notes, "2026-10-21_10-00"), _folder(notes, "2026-10-22_10-00")
    rt.recover(a)
    rt.recover(b)
    gate.entered()
    ida, idb = _id(a), _id(b)
    rt.handle(f"record:{ida}:lang:en")
    gate.release(0)
    gate.entered()  # the same recording again
    assert gate.calls[1].get("lang") == "en" and _ids(rt) == [(ida, True), (idb, False)]
    gate.release(1)
    gate.entered()
    assert "lang" not in gate.calls[2]  # the waiting one goes by speech
    gate.release(2)
    rt.join(timeout=10)
    assert _state(a) is RecordState.PROCESSED and _state(b) is RecordState.PROCESSED


def test_waiting_record_renamed_and_moved_is_processed_where_it_is(setup, tmp_path) -> None:
    """A waiting one renamed and moved to a section: the task finds it by id and processes it."""
    rt, _captured, gate, app = setup
    notes = tmp_path / "notes"
    a, b = _folder(notes, "2026-10-21_10-00"), _folder(notes, "2026-10-22_10-00")
    (notes / "Работа").mkdir()
    rt.recover(a)
    rt.recover(b)
    gate.entered()
    idb = _id(b)
    app.answer = "Созвон"
    rt.handle(f"record:{idb}:rename")
    rt.handle(f"record:{idb}:move:Работа")
    moved = notes / "Работа" / "2026-10-22_Созвон"
    assert moved.is_dir() and not b.exists() and rt.job_folders()[1] == moved
    gate.release(0)
    gate.entered()
    gate.release(1)
    rt.join(timeout=10)
    assert (moved / "transcript.txt").exists() and _state(moved) is RecordState.PROCESSED


def test_same_record_is_queued_once(setup, tmp_path) -> None:
    rt, _captured, gate, _app = setup
    a = _folder(tmp_path / "notes", "2026-10-21_10-00")
    rt.recover(a)
    gate.entered()
    rt.summarize(a)  # running and waiting: one task
    assert len(rt.state.jobs) == 1 and rt.state.notice == "Запись уже в очереди"


def test_recognition_setting_applies_to_records_that_have_not_started(setup, tmp_path) -> None:
    """Recognition changed during processing: the running one keeps the old one, the waiting one
    gets the new one (a task takes the settings when it starts, not when queued)."""
    rt, captured, gate, _app = setup
    rt.recover(_folder(tmp_path / "notes", "2026-10-21_10-00"))
    gate.entered()
    _record(rt, captured)  # recorded with "Automatic"
    rt.handle("asr:gigaam")
    gate.release(0)
    gate.entered()
    gate.release(1)
    rt.join(timeout=10)
    assert [c.get("backend") for c in gate.calls] == [None, "gigaam"]


def test_quit_with_queue_asks_and_keeps_records_unprocessed(setup, tmp_path) -> None:
    """Quitting Mnema during processing asks; "Cancel" lets everything go on; "Quit" drops the
    queue (the waiting one stays "Not processed") and the app can exit at once."""
    rt, _captured, gate, app = setup
    notes = tmp_path / "notes"
    a, b = _folder(notes, "2026-10-21_10-00"), _folder(notes, "2026-10-22_10-00")
    rt.recover(a)
    rt.recover(b)
    gate.entered()
    rt.handle("quit")
    assert app.questions == [
        (
            "Завершить Mnema?",
            "Обработка прервётся. Обработать записи можно после запуска.",
            "Завершить",
            "Отменить",
        )
    ]
    assert not rt.quit_requested and len(rt.state.jobs) == 2
    app.yes = True
    rt.handle("quit")
    assert rt.can_exit and [j.folder for j in rt.state.jobs] == [a]
    assert _runs(b) == [] and _state(b) is RecordState.UNPROCESSED


def test_quit_while_recording_and_processing_saves_the_recording(setup) -> None:
    """Recording and processing: the question mentions both; "Quit" saves the recording without
    processing and the app can exit once it is closed."""
    rt, captured, gate, app = setup
    _record(rt, captured)
    gate.entered()
    recording = _record(rt, captured, stop=False)
    app.yes = True
    rt.handle("quit")
    assert app.questions[-1][1] == (
        "Запись остановится и сохранится, обработка прервётся. "
        "Обработать записи можно после запуска."
    )
    _until(lambda: rt.can_exit)
    assert recording not in rt.job_folders() and (recording / "mic.wav").exists()
    assert _runs(recording) == []


def test_system_quit_does_not_ask(setup, tmp_path) -> None:
    rt, _captured, gate, app = setup
    rt.recover(_folder(tmp_path / "notes", "2026-10-21_10-00"))
    gate.entered()
    rt.handle("quit_now")
    assert rt.can_exit and app.questions == []


def test_queue_and_quit_questions_are_english_in_english(setup, tmp_path) -> None:
    """The English interface: the queue notice, the discard and quit questions have no Russian."""
    set_language("en")
    rt, captured, gate, app = setup
    a = _folder(tmp_path / "notes", "2026-10-21_10-00")
    rt.recover(a)
    gate.entered()
    rt.summarize(a)
    assert rt.state.notice == "This recording is already queued"
    recording = _record(rt, captured, stop=False)
    rt.handle(f"recover:{recording}")
    assert rt.state.notice == (
        "This recording is still in progress. You can process it after it stops"
    )
    rt.handle("discard")
    rt.handle("quit")
    assert app.questions == [
        (
            "Discard recording?",
            "The audio will not be saved.",
            "Discard recording",
            "Keep recording",
        ),
        (
            "Quit Mnema?",
            "The recording will stop and be saved, and processing will be interrupted. "
            "You can process the recordings after the next launch.",
            "Quit",
            "Cancel",
        ),
    ]
    assert not [t for q in app.questions for t in q if re.search("[А-Яа-яЁё]", t)]
    rt.handle("stop")


def test_panel_shows_processing_and_waiting_cards_under_start(setup, tmp_path) -> None:
    """The panel during processing: "Start recording" on top; the running one has a bar and the
    step in its card, the waiting one "Queued"; a folder outside the library is a card too."""
    rt, _captured, gate, _app = setup
    notes = tmp_path / "notes"
    outside = _folder(tmp_path / "архив", "2026-10-20_10-00")
    (outside / "transcript.txt").write_text("прежняя", encoding="utf-8")  # to process again
    a = _folder(notes, "2026-10-21_10-00")
    rt.recover(outside, lang="ru")
    rt.recover(a)
    gate.entered()
    env = MenuEnv(mac=True)
    cards = with_folders(library(notes).cards, rt.job_folders())
    panel = panel_model(rt.state, rt.prefs, env, cards, settings_model(rt.prefs, env))
    assert panel.screen == "idle" and panel.idle is not None and panel.idle.start.enabled
    waiting, running = panel.cards  # by date, newest first
    job = rt.state.jobs[0]
    assert (running.id, running.busy, running.detail.startswith("Распознаю речь…")) == (
        job.id, True, True,
    )  # fmt: skip
    assert running.progress is not None and 0.0 <= running.progress < 1.0
    # the old transcript is about to be replaced: not offered until the new one (as in the tray)
    assert [b.title for b in running.menu if not b.children] == [
        "Открыть папку", "Задать название…", "Остановить обработку",
    ]  # fmt: skip
    [lang] = [b for b in running.menu if b.children]
    assert lang.title == "Язык: русский"
    assert [c.key for c in lang.children if c.checked] == [f"record:{job.id}:lang:ru"]
    assert (waiting.busy, waiting.detail, waiting.progress) == (False, "В очереди", None)
    assert [b.title for b in waiting.menu] == [
        "Открыть папку", "Переименовать…", "Переместить в раздел", "Убрать из очереди",
        "Удалить запись",
    ]  # fmt: skip
    assert panel.status.icon == "saving"
    tray = {i.key for i in menu(rt.state, rt.prefs, MenuEnv())} | {
        c.key for i in menu(rt.state, rt.prefs, MenuEnv()) for c in i.children
    }
    keys = {b.key for b in running.menu} | {c.key for c in lang.children}
    assert keys - {f"record:{job.id}:folder", f"record:{job.id}:lang"} <= tray
    assert "Открыть расшифровку" not in [i.title for i in menu(rt.state, rt.prefs, MenuEnv())]


def test_unqueued_record_is_unprocessed_again(setup, tmp_path) -> None:
    """Removing from the queue puts it back to "Not processed" (Linux tray) instead of losing it."""
    rt, _captured, gate, _app = setup
    notes = tmp_path / "notes"
    a, b = _folder(notes, "2026-10-21_10-00"), _folder(notes, "2026-10-22_10-00")
    rt.recover(a)
    rt.recover(b)
    gate.entered()
    assert b not in rt.state.unfinished
    rt.handle(f"record:{_id(b)}:stop")
    assert b in rt.state.unfinished and a not in rt.state.unfinished


def test_trash_takes_a_waiting_record_out_of_the_queue(setup, tmp_path) -> None:
    """Deleting a waiting recording: out of the queue and into the Trash; not a running one."""
    rt, _captured, gate, app = setup
    notes = tmp_path / "notes"
    a, b = _folder(notes, "2026-10-21_10-00"), _folder(notes, "2026-10-22_10-00")
    rt.recover(a)
    rt.recover(b)
    gate.entered()
    rt.handle(f"record:{_id(a)}:trash")
    rt.handle(f"record:{_id(b)}:trash")
    assert app.trashed == [b] and rt.job_folders() == [a]


def test_just_recorded_waiting_record_renamed_and_moved(setup, tmp_path) -> None:
    """A just-recorded one renamed and moved to a section while it waits is processed where it
    was moved."""
    rt, captured, gate, app = setup
    notes = tmp_path / "notes"
    rt.recover(_folder(notes, "2026-10-20_10-00"))
    gate.entered()
    recorded = _record(rt, captured)
    (notes / "Работа").mkdir()
    rid = _id(recorded)
    app.answer = "Созвон"
    rt.handle(f"record:{rid}:rename")
    rt.handle(f"record:{rid}:move:Работа")
    [moved] = (notes / "Работа").iterdir()
    assert moved.name.endswith("_Созвон") and not recorded.exists()
    gate.release(0)
    gate.entered()
    gate.release(1)
    rt.join(timeout=10)
    assert (moved / "transcript.txt").exists() and _state(moved) is RecordState.PROCESSED


def test_waiting_and_running_records_are_left_alone(setup, tmp_path, monkeypatch) -> None:
    """Sync leaves the running recording alone (a waiting one is fine: the task finds it by id);
    "Delete audio of processed recordings" and "Not processed" leave both alone."""
    monkeypatch.setattr(runtime_mod, "SyncController", Controller)
    rt, _captured, gate, app = setup
    notes = tmp_path / "notes"
    a, b, c = (_folder(notes, f"2026-10-2{i}_10-00") for i in (1, 2, 3))
    rt.recover(a)
    gate.entered()
    gate.release(0)
    rt.join(timeout=10)
    assert _state(a) is RecordState.PROCESSED  # audio of a processed one can be deleted
    rt.recover(b)
    gate.entered()
    rt.recover(a)  # processed again: waits
    rt.recover(c)  # not processed: waits
    rt.start_sync()
    busy = rt._sync.callbacks["busy"]()
    assert _id(b) in busy and _id(a) not in busy and _id(c) not in busy
    assert rt.state.unfinished == []
    app.yes = True
    rt.handle("cleanup_audio")
    assert app.questions == [] and app.trashed == []  # nothing to delete


def test_down_server_ends_the_job_and_the_queue_goes_on(setup, tmp_path, monkeypatch) -> None:
    """The chosen server did not answer: the recording shows "<server> is unavailable" with
    "Retry", the queue goes on, the next one the same."""
    rt, _captured, _gate, app = setup
    monkeypatch.setattr(base, "STANDALONE", False)  # Linux: the server processes
    monkeypatch.setenv("MNEMA_RUNNER", NODE)
    monkeypatch.setattr("mnema.client.MnemaClient.health", lambda *a, **kw: None)
    notes = tmp_path / "notes"
    a, b = _folder(notes, "2026-10-21_10-00"), _folder(notes, "2026-10-22_10-00")
    rt.recover(a)
    rt.recover(b)
    rt.join(timeout=10)
    assert rt.state.jobs == []
    assert set(rt.state.unreachable) == {a.absolute(), b.absolute()}
    down = ("100.64.0.9:8123 недоступен", "Запись сохранена, обработайте позже")
    assert app.notes == [down, down]


def test_processing_the_folder_being_recorded_says_so(setup) -> None:
    """The "Process recording…" item while recording, with the folder being recorded picked: no part
    window, a notice that the recording is still in progress."""
    rt, captured, _gate, app = setup
    recording = _record(rt, captured, stop=False)
    app.chosen = recording
    rt.handle("process_folder")
    assert app.ranges == [] and rt.state.notice == (
        "Эта запись ещё идёт. Обработать её можно после остановки"
    )
    rt.recover(recording)
    assert recording not in rt.job_folders()


def test_title_windows_wait_for_each_other(monkeypatch, tmp_path) -> None:
    """The previous recording's title window is still open: "Set title…" of the running one
    opens next instead of getting lost; once answered, it gets no window after processing."""
    captured: dict = {}
    _patch_record(monkeypatch, tmp_path, captured)
    monkeypatch.setattr("mnema.bar.runtime._system_available", lambda: False)
    monkeypatch.delenv("MNEMA_MODEL")
    gate = _Gate()
    monkeypatch.setattr(setup_mod, "dispatch", gate)
    app = _OpenWindows()
    rt = BarRuntime(platform=app, prefs_file=tmp_path / "bar.json")
    rt.prefs.system = False
    try:
        first = _record(rt, captured)
        gate.entered()
        second = _record(rt, captured)
        gate.release(0)
        gate.entered()  # the first is done, its title window open; the second runs
        _until(lambda: len(app.windows) == 1)
        sid = _id(second)
        rt.handle(f"record:{sid}:title")
        rt.handle(f"record:{sid}:title")  # the same window only once
        assert len(app.windows) == 1
        app.answer_window(None)  # "Skip" on the first opens the second one's window
        assert len(app.windows) == 1
        app.answer_window("Созвон")
        gate.release(1)
        rt.join(timeout=10)
        assert app.windows == []  # titled by the user: no window after processing
        [named] = [c.path for c in library(tmp_path / "notes").cards if c.id == sid]
        assert named.name.endswith("_Созвон") and first.exists()
    finally:
        gate.open_all()
        rt.join(timeout=10)


def test_rename_while_a_recording_starts_does_not_hang(setup, tmp_path, monkeypatch) -> None:
    """Renaming a waiting recording holds the files lock while a new recording is built (or
    stopped): neither hangs."""
    rt, captured, gate, app = setup
    notes = tmp_path / "notes"
    a, b = _folder(notes, "2026-10-21_10-00"), _folder(notes, "2026-10-22_10-00")
    rt.recover(a)
    rt.recover(b)
    gate.entered()
    idb = _id(b)
    built, go = threading.Event(), threading.Event()
    session = session_mod.LiveSession

    class Slow(session):  # built while another thread holds the files lock
        def __init__(self, *a: Any, **k: Any) -> None:
            super().__init__(*a, **k)
            built.set()
            go.wait(timeout=10)

    monkeypatch.setattr(session_mod, "LiveSession", Slow)
    app.answer = "Созвон"

    def rename() -> None:
        with FILES_LOCK:
            go.set()
            time.sleep(0.3)  # the recording reached the locks
            rt.handle(f"record:{idb}:rename")

    captured["started"] = threading.Event()
    rt.handle("toggle_record")
    assert built.wait(timeout=10)
    worker = threading.Thread(target=rename, daemon=True)
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive(), "hung"
    assert captured["started"].wait(timeout=10)
    rt.handle("toggle_record")
    _until(lambda: rt.state.phase == Phase.IDLE)
    assert (notes / "2026-10-22_Созвон").is_dir()
