"""The GUI-less engine (mnema/bar/headless.py): the macOS app runs `python -m mnema.bar
--headless` and talks to it in JSON lines (mnema/bar/protocol.py). Streams stand in for the app:
stdin is a pipe or a string, stdout a queue of parsed messages; open and show in Finder are the
OS boundary (open) and are only recorded."""

from __future__ import annotations

import functools
import io
import json
import logging
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

import httpx
import pytest
from dotenv import dotenv_values
from test_bar import _patch_record, _wav

from mnema.bar.headless import Channel, Engine, ProtocolPlatform
from mnema.bar.protocol import (
    OUTGOING,
    ActionMsg,
    AnswerMsg,
    AskMsg,
    FormAsk,
    FormEventMsg,
    FormField,
    FormMsg,
    FormOp,
    NotifyMsg,
    QuitMsg,
    SettingsMsg,
    StateMsg,
)
from mnema.bar.range_pick import RangeAnswer, RangeRequest
from mnema.bar.runtime import BarRuntime
from mnema.install_setup import INVITE_REFUSED
from mnema.live.models import LiveResult
from mnema.llm import providers


class _Out:
    """The engine's stdout: each line is a protocol message in the queue."""

    def __init__(self) -> None:
        self.q: queue.Queue[Any] = queue.Queue()
        self.lines: list[str] = []  # raw, to check no secret reaches the output

    def write(self, line: str) -> int:
        self.lines.append(line)
        self.q.put(OUTGOING.validate_json(line))
        return len(line)

    def flush(self) -> None: ...

    def until(self, match: Callable[[Any], bool], timeout: float = 10.0) -> Any:
        deadline = time.monotonic() + timeout
        while True:
            msg = self.q.get(timeout=max(deadline - time.monotonic(), 0.01))
            if match(msg):
                return msg

    def rest(self) -> list[Any]:
        msgs = []
        while not self.q.empty():
            msgs.append(self.q.get_nowait())
        return msgs


def _line(msg: Any) -> str:
    return msg.model_dump_json() + "\n"


@pytest.fixture
def notes(monkeypatch, tmp_path) -> Path:
    root = tmp_path / "notes"
    root.mkdir()
    monkeypatch.setenv("MNEMA_LIVE_DIR", str(root))
    return root


@pytest.fixture
def native(monkeypatch) -> list[tuple[str, Path]]:
    calls: list[tuple[str, Path]] = []
    monkeypatch.setattr(ProtocolPlatform, "open", lambda self, p: calls.append(("open", p)))
    return calls


def _engine(tmp_path: Path, root: Path, out: _Out) -> Engine:
    runtime = functools.partial(BarRuntime, prefs_file=tmp_path / "bar.json")
    return Engine(Channel(out), runtime=runtime, root=lambda: root)


def _start(engine: Engine) -> tuple[threading.Thread, TextIO]:
    """The engine in its own thread; returns the pipe the "app" writes to."""
    r, w = os.pipe()
    thread = threading.Thread(
        target=engine.run, args=(os.fdopen(r, encoding="utf-8"),), daemon=True
    )
    thread.start()
    return thread, os.fdopen(w, "w", encoding="utf-8", buffering=1)


def test_only_keys_the_panel_showed_are_handled(tmp_path, notes, caplog) -> None:
    """A panel action works; a key the panel never showed (or a malformed line) is ignored: the app
    cannot rename an arbitrary recording or set anything it likes."""
    hidden = "record:00000000-0000-0000-0000-000000000000:rename"
    stdin = io.StringIO(
        _line(ActionMsg(key="toggle_system"))
        + "не json\n\n"
        + _line(ActionMsg(key="lang:de"))
        + _line(ActionMsg(key=hidden))
    )
    out = _Out()
    engine = _engine(tmp_path, notes, out)
    engine.run(stdin)
    assert engine.rt.prefs.system is False
    assert f"action the panel never showed: {hidden}" in caplog.text
    assert not engine.rt.state.error  # the key never reached the recordings
    msgs = out.rest()
    assert not [m for m in msgs if isinstance(m, AskMsg)]  # no title window
    states = [m for m in msgs if isinstance(m, StateMsg)]
    assert [s.on for s in states[-1].panel.idle.sources] == [True, False]


def test_idle_engine_checks_for_updates_on_timer(monkeypatch, tmp_path, notes):
    from mnema import update

    requests = []

    def manifest(request):
        requests.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "version": "99.0.0",
                "archive": "https://releases.test/archive",
                "sha256": "0" * 64,
            },
        )

    monkeypatch.setenv("MNEMA_UPDATE_URL", "https://releases.test/latest")
    monkeypatch.setattr(update, "TRANSPORT", httpx.MockTransport(manifest))
    out = _Out()
    thread, app = _start(_engine(tmp_path, notes, out))
    try:
        state = out.until(
            lambda m: (
                isinstance(m, StateMsg)
                and any("99.0.0" in notice.text for notice in m.panel.notices)
            )
        )
        assert state.panel.screen == "idle"
        assert requests == ["/latest"]
    finally:
        app.write(_line(ActionMsg(key="quit")))
        app.close()
        thread.join(timeout=10)
    assert not thread.is_alive()


def test_notification_of_an_earlier_run_opens_existing_path(tmp_path, notes, native) -> None:
    """A click on a notification from a previous Mnema run: this run never showed its key, but the
    path exists — open the file or folder; a missing path is ignored."""
    folder = tmp_path / "архив" / "2026-09-27_Дейли"
    folder.mkdir(parents=True)
    txt = folder / "transcript.txt"
    txt.write_text("x", encoding="utf-8")
    keys = [
        f"open:{txt}",
        f"reveal:{folder}",
        f"open:{folder / 'нет.txt'}",
        f"reveal:{tmp_path}/нет",
    ]
    _engine(tmp_path, notes, _Out()).run(
        io.StringIO("".join(_line(ActionMsg(key=k)) for k in keys))
    )
    assert native == [("open", txt), ("open", folder)]


def test_server_in_header_opens_server_settings_any_time(tmp_path, notes) -> None:
    """The server in the panel header always works: a click opens settings on the Server tab even
    when no card showed "Choose server"."""
    out = _Out()
    _engine(tmp_path, notes, out).run(io.StringIO(_line(ActionMsg(key="choose_server"))))
    assert SettingsMsg(tab="server") in out.rest()


def test_rename_card_asks_title_and_renames_folder(tmp_path, notes) -> None:
    """Rename… on a card: the title window, the answer renames the folder and rereads the feed. A
    click that changes no folder does not reread the feed from disk."""
    folder = notes / "2026-09-27_13-05"
    folder.mkdir()
    (folder / "transcript.txt").write_text("x", encoding="utf-8")
    scans: list[Path] = []

    def root() -> Path:
        scans.append(notes)
        return notes

    out = _Out()
    runtime = functools.partial(BarRuntime, prefs_file=tmp_path / "bar.json")
    thread, app = _start(Engine(Channel(out), runtime=runtime, root=root))
    with app:
        [card] = out.until(lambda m: isinstance(m, StateMsg)).panel.cards
        app.write(_line(ActionMsg(key="toggle_system")))
        out.until(lambda m: isinstance(m, StateMsg))
        assert len(scans) == 1
        rename = next(b.key for b in card.menu if b.key.endswith(":rename"))
        app.write(_line(ActionMsg(key=rename)))
        ask = out.until(lambda m: isinstance(m, AskMsg))
        # an untitled recording gets an empty field
        assert ask.ask.kind == "title" and ask.ask.value == ""
        app.write(_line(AnswerMsg(id=ask.id, value="  Дейли  ")))
        state = out.until(lambda m: isinstance(m, StateMsg))
        assert [c.title for c in state.panel.cards] == ["Дейли"]
    thread.join(timeout=10)
    assert not thread.is_alive() and isinstance(out.rest()[-1], QuitMsg)
    assert (notes / "2026-09-27_Дейли" / "transcript.txt").exists() and not folder.exists()


def test_card_process_then_language_correction_while_processing(monkeypatch, tmp_path, notes):
    """Process… on an unprocessed card opens the process window, its language starts at Automatic;
    during processing, Language in the card menu is a correction: the same recording again,
    strictly in the chosen language."""

    folder = notes / "2026-09-30_13-05"
    _wav(folder / "mic.wav")
    seen: list[tuple] = []
    resume = threading.Event()

    def fake_recover(base, config, connection, *, on, on_lang, **kw):
        seen.append((base, config.lang))
        if len(seen) == 1:
            on_lang({"mic": "ru"})
            assert resume.wait(timeout=10)
            on(0.5, "")  # the language was corrected: interrupted here
        return LiveResult(), ""

    def shown(value: str) -> Callable[[Any], bool]:
        """A card of a recording in processing with language value in its menu."""
        return lambda m: (
            isinstance(m, StateMsg)
            and any(
                c.busy and any(b.title == f"Язык: {value}" for b in c.menu) for c in m.panel.cards
            )
        )

    monkeypatch.setattr("mnema.live.recording_client.recover_on_node", fake_recover)
    out = _Out()
    thread, app = _start(_engine(tmp_path, notes, out))
    with app:
        state = out.until(lambda m: isinstance(m, StateMsg))
        [card] = state.panel.cards
        assert not [b for b in card.menu if b.title.startswith("Язык")]  # not processing
        app.write(_line(ActionMsg(key=card.buttons[0].key)))
        ask = out.until(lambda m: isinstance(m, AskMsg))
        assert (ask.ask.kind, ask.ask.lang) == ("range", "auto")
        app.write(_line(AnswerMsg(id=ask.id, value={"start": "", "end": "", "title": ""})))
        busy = out.until(shown("русский"))  # the language comes from speech
        assert busy.panel.screen == "idle"  # the top is Start recording
        app.write(_line(ActionMsg(key=f"record:{card.id}:lang:en")))
        out.until(shown("английский"))
        resume.set()
        out.until(lambda m: isinstance(m, NotifyMsg))
    thread.join(timeout=10)
    assert seen == [(folder, None), (folder, "en")]


def test_protocol_platform_converts_answers_and_cancels() -> None:
    out = _Out()
    platform = ProtocolPlatform(Channel(out))
    got: list[Any] = []
    platform.ask_title("Дейли", got.append)
    platform.confirm("Отменить запись?", "Звук не сохранится.", "Отменить запись", got.append,
                     cancel="Продолжить запись")  # fmt: skip
    platform.choose_folder(Path("/Users/me/mnema"), got.append)
    platform.ask_range(RangeRequest(length=90.0, name="Дейли", replace=True), got.append)
    title, confirm, folder, part = out.rest()
    assert [m.id for m in (title, confirm, folder, part)] == [1, 2, 3, 4]
    assert (title.ask.kind, title.ask.value) == ("title", "Дейли")
    assert (confirm.ask.title, confirm.ask.ok, confirm.ask.cancel) == (
        "Отменить запись?", "Отменить запись", "Продолжить запись"
    )  # fmt: skip
    assert folder.ask.start == "/Users/me/mnema"
    assert (part.ask.length, part.ask.name, part.ask.replace) == (90.0, "Дейли", True)
    assert part.ask.lang == "auto"  # by speech at first
    assert [c.title for c in part.ask.langs] == ["Автоматически", "Русский", "Английский"]
    range_answer = {"start": "0:10:00", "end": "1:00:00", "title": "", "replace": False}
    range_answer["lang"] = "ru"
    platform.answered(part.id, range_answer)
    platform.answered(folder.id, "/Users/me/mnema/2026-09-27_Дейли")
    platform.answered(confirm.id, True)
    platform.answered(title.id, "  Итоги  ")
    platform.answered(title.id, "ещё раз")  # the window was already answered
    assert got == [
        RangeAnswer(start="0:10:00", end="1:00:00", lang="ru"),
        Path("/Users/me/mnema/2026-09-27_Дейли"),
        True,
        "Итоги",
    ]

    got.clear()
    platform.ask_range(RangeRequest(length=90.0), got.append)
    platform.confirm("Удалить запись?", "Запись удалится.", "Удалить", got.append)
    platform.choose_folder(Path("/"), got.append)
    part, confirm, folder = out.rest()
    platform.answered(part.id, {"start": 5})
    platform.answered(confirm.id, "да")
    platform.answered(folder.id, "")
    assert got == [None, False, None]

    got.clear()
    platform.ask_title(None, got.append)
    platform.confirm("Удалить запись?", "Запись удалится.", "Удалить", got.append)
    platform.cancel_all()  # the app closed with windows open
    assert got == [None, False]

    out.rest()
    platform.notify("Запись обработана", "Дейли · 30.09", Path("/Users/me/mnema/Д/transcript.txt"))
    [note] = out.rest()
    assert isinstance(note, NotifyMsg) and note.open_key == "open:/Users/me/mnema/Д/transcript.txt"
    assert note.open_key in platform.offered  # a click on the notification is an allowed action

    platform.open_settings("server")  # "Choose server" on a card
    [opened] = out.rest()
    assert isinstance(opened, SettingsMsg) and opened.tab == "server"


def test_form_event_reaches_window_with_typed_key_hidden_from_repr() -> None:
    """The model window key reaches the form as typed, while the event's repr and str keep it out
    of logs and tracebacks."""
    out = _Out()
    platform = ProtocolPlatform(Channel(out))
    got: list[Any] = []
    ask = FormAsk(title="Модель для пересказа", fields=[FormField(id="key", label="Ключ API")])
    platform.open_form(ask, got.append)
    [opened] = out.rest()
    values = {"key": "sk-or-v1-secret"}
    platform.form_event(FormEventMsg(id=opened.id, rev=1, op=FormOp.SUBMIT, values=values))
    [event] = got
    assert (event.op, event.values, event.rev) == (FormOp.SUBMIT, values, 1)
    assert "sk-or-v1-secret" not in repr(event) and "sk-or-v1-secret" not in str(event)


def test_form_window_over_protocol_never_echoes_the_secret(
    monkeypatch, tmp_path, notes, caplog
) -> None:
    """A form window over the protocol: AskMsg(form) → window events → window state with the event
    number; saved — FormMsg(None), the app's answer to a closed window is ignored. The invite code
    from the event never reaches an outgoing line or the log; an event for an unknown window is
    ignored."""
    for name in ("MNEMA_RUNNER", "MNEMA_TOKEN"):
        monkeypatch.setenv(name, "")  # the window writes the process env; teardown restores it
    secret, token = "s3cret-invite-code", "device-token-value"
    claims: list[httpx.Request] = []

    def node(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/devices/claim":
            claims.append(request)
            if len(claims) == 1:  # the node rejected the first invite
                return httpx.Response(403, json={"detail": "Приглашение недействительно"})
            return httpx.Response(200, json={"token": token})
        return httpx.Response(200, json={"ok": True, "llm": False})

    monkeypatch.setattr(providers, "TRANSPORT", httpx.MockTransport(node))
    caplog.set_level(logging.DEBUG)
    out = _Out()
    thread, app = _start(_engine(tmp_path, notes, out))
    values = {"connection": f"srv:8000#invite={secret}"}
    with app:
        out.until(lambda m: isinstance(m, StateMsg))
        app.write(_line(ActionMsg(key="server_setup")))
        ask = out.until(lambda m: isinstance(m, AskMsg))
        assert (ask.ask.kind, ask.ask.title) == ("form", "Подключение к серверу")
        app.write(_line(FormEventMsg(id=ask.id, rev=1, op=FormOp.SUBMIT, values=values)))
        busy = out.until(lambda m: isinstance(m, FormMsg))
        assert (busy.id, busy.ask.busy, busy.ask.rev) == (ask.id, "Проверяю…", 1)
        failed = out.until(lambda m: isinstance(m, FormMsg))
        assert (failed.ask.error, failed.ask.rev) == (INVITE_REFUSED, 1)
        app.write(_line(FormEventMsg(id=99, rev=1, op=FormOp.SUBMIT, values=values)))
        app.write('{"type": "form_event", "id": 1, "values": {"token": "' + secret + '"}}\n')
        app.write(_line(FormEventMsg(id=ask.id, rev=2, op=FormOp.SUBMIT, values=values)))
        closed = out.until(lambda m: isinstance(m, FormMsg) and m.ask is None)
        assert closed.id == ask.id
        app.write(_line(AnswerMsg(id=ask.id, value=None)))
        app.write(_line(FormEventMsg(id=ask.id, rev=3, op=FormOp.SUBMIT, values=values)))
        app.write(_line(ActionMsg(key="quit")))
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert not [m for m in out.rest() if isinstance(m, FormMsg)]  # a closed window stays silent
    assert dotenv_values(tmp_path / ".config/mnema/.env")["MNEMA_TOKEN"] == token
    assert not [line for line in out.lines if secret in line or token in line]
    assert secret not in caplog.text and token not in caplog.text
    assert "event for unknown window 99" in caplog.text


@pytest.mark.parametrize("quit_action", ["", "quit", "quit_now"], ids=["eof", "quit", "system"])
def test_quit_mid_recording_saves_it_without_processing(monkeypatch, tmp_path, quit_action) -> None:
    """The app closed mid-recording or the user logs out (quit_now) — no question; Quit Mnema asks
    "Quit Mnema?", and Quit stops the recording and saves it unprocessed (Not processed, Process…
    after the next launch), then the engine exits."""
    from mnema.bar.recordings import library
    from mnema.live.meta import RecordState

    captured: dict = {}
    _patch_record(monkeypatch, tmp_path, captured)
    processed: list[Path] = []
    monkeypatch.setattr(
        "mnema.live.recording_client.finalize_on_node",
        lambda prepared, tracks, **kw: processed.append(prepared.base) or (LiveResult(), ""),
    )
    notes = tmp_path / "notes"
    out = _Out()
    thread, app = _start(_engine(tmp_path, notes, out))
    with app:
        out.until(lambda m: isinstance(m, StateMsg))
        app.write(_line(ActionMsg(key="start")))
        rec = out.until(lambda m: isinstance(m, StateMsg) and m.panel.screen == "recording")
        assert captured["started"].wait(timeout=10)
        if quit_action == "quit_now":
            app.write(_line(ActionMsg(key="quit_now")))
            thread.join(timeout=20)
            assert not thread.is_alive()  # logging out: before EOF, no question
        elif quit_action:
            app.write(_line(ActionMsg(key="quit")))
            ask = out.until(lambda m: isinstance(m, AskMsg))
            assert (ask.ask.title, ask.ask.message, ask.ask.ok, ask.ask.cancel) == (
                "Выйти из Mnema?",
                "Запись остановится и сохранится. Обработать её можно после запуска.",
                "Выйти",
                "Не выходить",
            )
            app.write(_line(AnswerMsg(id=ask.id, value=True)))
            thread.join(timeout=20)
            assert not thread.is_alive()  # an explicit quit works before EOF
    thread.join(timeout=20)
    assert not thread.is_alive()
    fields = {
        f.key: f.enabled for t in rec.panel.settings.tabs for s in t.sections for f in s.fields
    }
    assert (fields["toggle_mic"], fields["discreet"]) == (False, True)  # settings follow the phase
    msgs = out.rest()
    assert isinstance(msgs[-1], QuitMsg)
    asks = [m for line in out.lines if isinstance(m := OUTGOING.validate_json(line), AskMsg)]
    assert len(asks) == (1 if quit_action == "quit" else 0)  # no title window
    assert processed == [] and not [m for m in msgs if isinstance(m, NotifyMsg)]
    [card] = library(notes).cards
    assert card.state is RecordState.UNPROCESSED and (card.path / "mic.wav").exists()


_ENGINE = [sys.executable, "-m", "mnema.bar", "--headless"]


def _engine_env(tmp_path: Path) -> dict[str, str]:
    return {
        **os.environ,
        "HOME": str(tmp_path),
        "MNEMA_LIVE_DIR": str(tmp_path / "notes"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
    }


def test_real_process_quits_cleanly_while_input_is_open(tmp_path) -> None:
    """The app asks to quit without closing input: the engine exits with code 0 (a real case: the
    interpreter aborted because of a thread waiting for input)."""
    pipe = subprocess.PIPE
    with subprocess.Popen(
        _ENGINE, stdin=pipe, stdout=pipe, stderr=pipe, text=True, env=_engine_env(tmp_path)
    ) as proc:
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
        try:
            proc.stdin.write(_line(ActionMsg(key="quit")))
            proc.stdin.flush()
            code = proc.wait(timeout=30)  # input is still open
        finally:
            proc.kill()
        msgs = [OUTGOING.validate_json(line) for line in proc.stdout.read().splitlines()]
        assert code == 0, proc.stderr.read()
    assert isinstance(msgs[0], StateMsg) and isinstance(msgs[-1], QuitMsg)


def test_engine_children_do_not_read_the_protocol(tmp_path) -> None:
    """An engine child process never gets the protocol pipe: ffmpeg without -nostdin reads stdin
    and would eat the app's messages (a malformed line, a lost click, a window answer). The child
    here is the OS `open` program (open the transcript): the fake one reads its input."""
    read = tmp_path / "read"
    opener = tmp_path / "bin" / "open"
    opener.parent.mkdir()
    opener.write_text(
        f'#!/bin/sh\ncat > "{read}.part" && mv "{read}.part" "{read}"\n', encoding="utf-8"
    )
    opener.chmod(0o755)
    txt = tmp_path / "notes" / "2026-09-27_Дейли" / "transcript.txt"
    txt.parent.mkdir(parents=True)
    txt.write_text("x", encoding="utf-8")
    env = _engine_env(tmp_path) | {"PATH": f"{opener.parent}{os.pathsep}{os.environ['PATH']}"}
    pipe = subprocess.PIPE
    with subprocess.Popen(
        _ENGINE, stdin=pipe, stdout=pipe, stderr=pipe, text=True, env=env
    ) as proc:
        assert proc.stdin is not None
        try:
            first = OUTGOING.validate_json(proc.stdout.readline())
            proc.stdin.write(_line(ActionMsg(key=first.panel.cards[0].open_key)))
            proc.stdin.flush()
            deadline = time.monotonic() + 30
            while not read.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            proc.stdin.write(_line(ActionMsg(key="quit")))  # reached the engine, not `open`
            proc.stdin.flush()
            code = proc.wait(timeout=30)
        finally:
            proc.kill()
    assert (code, read.read_text(encoding="utf-8")) == (0, "")


def test_first_start_of_the_bundled_app_shows_open_at_login_on(tmp_path) -> None:
    """The first start of the DMG app turns "Open at login" on: Settings show it at once."""
    app = tmp_path / "Applications" / "Mnema.app"
    (app / "Contents" / "Resources" / "bin").mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_bytes(b"")
    env = _engine_env(tmp_path) | {
        "MNEMA_APP_BUNDLE": str(app),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
    }
    pipe = subprocess.PIPE
    with subprocess.Popen(
        _ENGINE, stdin=pipe, stdout=pipe, stderr=pipe, text=True, env=env
    ) as proc:
        assert proc.stdin is not None and proc.stdout is not None
        try:
            first = OUTGOING.validate_json(proc.stdout.readline())
            proc.stdin.write(_line(ActionMsg(key="quit")))
            proc.stdin.flush()
            proc.wait(timeout=30)
        finally:
            proc.kill()
    assert isinstance(first, StateMsg)
    fields = [
        field
        for tab in first.panel.settings.tabs
        for section in tab.sections
        for field in section.fields
        if field.key == "autostart"
    ]
    assert [field.checked for field in fields] == [True]


def test_kill_works_after_the_engine_exits(tmp_path) -> None:
    """The engine quit and the app shows "Mnema stopped": its thread sits in Swift, where a Python
    signal handler cannot run, so SIGTERM ends the process again instead of getting lost."""
    script = (
        "import os, signal, time\n"
        "from mnema.bar import headless\n"
        "headless.run()\n"
        "os.kill(os.getpid(), signal.SIGTERM)\n"
        "time.sleep(10)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        env=_engine_env(tmp_path),
        timeout=120,
    )
    assert proc.returncode == -signal.SIGTERM, proc.stderr


def test_windows_speak_english_in_an_english_interface() -> None:
    """With an English interface every window the engine opens has English labels, whatever the
    protocol defaults are; Russian is only in the user's data."""
    from mnema import i18n

    out = _Out()
    platform = ProtocolPlatform(Channel(out))
    with i18n.using("en"):
        platform.ask_title("Дейли", lambda _v: None)
        platform.confirm("Delete recording?", "It will be removed.", "Delete", lambda _v: None)
        platform.choose_folder(Path("/Users/me/mnema"), lambda _v: None)
        platform.ask_range(RangeRequest(length=90.0, sources=[]), lambda _v: None)
    title, confirm, folder, part = out.rest()
    shown = title.ask.model_dump(exclude={"value"}) | {"cancel": confirm.ask.cancel}
    shown |= folder.ask.model_dump() | {"range": part.ask.model_dump()}
    assert not re.search("[А-Яа-яЁё]", json.dumps(shown, ensure_ascii=False))
    assert (part.ask.title, part.ask.asr_label, part.ask.ok) == (
        "Process recording", "Recognition", "Process"
    )  # fmt: skip
    assert [c.title for c in part.ask.langs] == ["Automatic", "Russian", "English"]
    assert (title.ask.title, confirm.ask.cancel) == ("Recording title", "Cancel")
