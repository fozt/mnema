"""The Mnema engine for macOS: `python -m mnema.bar --headless`.

Mnema.app (Swift) loads the venv's Python in its own process and talks to the engine over pipes on
descriptors 0 and 1: the engine reads actions, window answers and "panel open", and writes the
panel state, windows, notifications and "quitting" (JSON lines, protocol.py). ONE thread, the
event loop, works with BarRuntime; recording and processing run in BarRuntime's workers and report
changes through on_change.
"""

from __future__ import annotations

import itertools
import logging
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any, TextIO

from pydantic import BaseModel

from ..config import library_root
from ..i18n import _
from . import range_pick  # its labels are read at use time, in the current language
from .panel import panel_model
from .protocol import (
    NEW_SECTION_ASK,
    Ask,
    AskMsg,
    Choice,
    ConfirmAsk,
    FolderAsk,
    FormAsk,
    FormEventMsg,
    FormMsg,
    NotifyMsg,
    QuitMsg,
    RangeAsk,
    SettingsMsg,
    StateMsg,
    TitleAsk,
    decode_incoming,
    encode,
)
from .range_pick import LANGS, RangeAnswer, RangeRequest
from .recordings import LibraryView, library, with_folders
from .runtime import BarRuntime, FormEvent, FormUpdate
from .settings_model import settings_model
from .state import MenuEnv, Phase

logger = logging.getLogger(__name__)

# always allowed: the hotkey, quit (⌘Q; logging out is quit_now, without a question) and the
# processing place in the panel header
_FREE = ("toggle_record", "quit", "quit_now", "choose_server")
_FREE_PREFIXES = ("hotkey:", "hotkey_busy:")  # the hotkey field and its registration answer
_FOLDER_PREFIXES = ("record:",)  # an action on a feed recording: reread the feed
_ENV_EVERY_S = 2.0  # menu environment (server, models), at most this often
_REDRAWS = ("change", "tick")  # events that only redraw the panel


class Channel:
    """The engine's stdout: protocol lines under a lock (the loop and the recording workers write).
    Once the app is gone, writes are dropped silently."""

    def __init__(self, out: TextIO) -> None:
        self._out = out
        self._lock = threading.Lock()
        self.closed = False

    def write(self, line: str) -> None:
        with self._lock:
            if self.closed:
                return
            try:
                self._out.write(line)
                self._out.flush()
            except (OSError, ValueError):  # broken pipe or closed stream
                self.closed = True

    def send(self, msg: BaseModel) -> None:
        self.write(encode(msg))


class ProtocolPlatform:
    """Windows and notifications are messages to the app, answers come through answered(); open,
    clipboard and Trash use macOS itself."""

    def __init__(self, channel: Channel) -> None:
        self.channel = channel
        self.offered: set[str] = set()  # keys the engine has shown
        self._ids = itertools.count(1)
        self._waiting: dict[int, Callable[[Any], None]] = {}
        self._forms: dict[int, Callable[[FormEvent | None], None]] = {}  # open form windows

    def open(self, path: Path) -> None:
        # .txt opens in TextEdit (-e): plain `open` may hand it to Script Editor
        args = ["open", "-e", str(path)] if path.suffix == ".txt" else ["open", str(path)]
        subprocess.Popen(args)

    def trash(self, path: Path) -> None:
        """To the Trash (Finder can put it back), not deleted for good."""
        from Foundation import NSURL, NSFileManager  # ty: ignore[unresolved-import]

        ok, _where, err = NSFileManager.defaultManager().trashItemAtURL_resultingItemURL_error_(
            NSURL.fileURLWithPath_(str(path)), None, None
        )
        if not ok:
            raise OSError(str(err.localizedDescription()) if err is not None else _("no access"))

    def _ask(self, ask: Ask, done: Callable[[Any], None]) -> None:
        n = next(self._ids)
        self._waiting[n] = done
        self.channel.send(AskMsg(id=n, ask=ask))

    def answered(self, n: int, value: Any) -> None:
        done = self._waiting.pop(n, None)
        if done is not None:
            done(value)

    def cancel_all(self) -> None:
        """The app is gone: every open window counts as cancelled."""
        for n in list(self._waiting):
            self.answered(n, None)

    def ask_title(
        self,
        current: str | None,
        on_done: Callable[[str | None], None],
        *,
        cancel: str | None = None,
    ) -> None:
        ask = TitleAsk(
            title=_("Recording title"),
            value=current or "",
            placeholder=range_pick.NAME_PLACEHOLDER,
            ok=_("Save"),
            cancel=cancel or _("Cancel"),
        )
        self._ask(ask, lambda v: on_done(v.strip() if isinstance(v, str) else None))

    def ask_section(self, on_done: Callable[[str | None], None]) -> None:
        self._ask(NEW_SECTION_ASK, lambda v: on_done(v.strip() if isinstance(v, str) else None))

    def confirm(
        self,
        title: str,
        message: str,
        ok: str,
        on_done: Callable[[bool], None],
        *,
        cancel: str | None = None,
    ) -> None:
        ask = ConfirmAsk(title=title, message=message, ok=ok, cancel=cancel or _("Cancel"))
        self._ask(ask, lambda v: on_done(v is True))

    def choose_folder(self, start: Path, on_done: Callable[[Path | None], None]) -> None:
        self._ask(
            FolderAsk(start=str(start), message=_("Choose the recording folder"), ok=_("Choose")),
            lambda v: on_done(Path(v) if isinstance(v, str) and v else None),
        )

    def ask_range(self, req: RangeRequest, on_done: Callable[[RangeAnswer | None], None]) -> None:
        def got(value: Any) -> None:
            try:
                answer = RangeAnswer.model_validate(value)
            except ValueError:  # not a window answer: same as cancel
                answer = None
            on_done(answer)

        asrs = [Choice(key=k, title=t) for k, t in req.asrs]
        sources = [Choice(key=s.label, title=s.title, checked=s.on) for s in req.sources]
        fields = req.model_dump(exclude={"asrs", "sources"})
        texts = {  # every label in the current language, whatever the protocol defaults are
            "title": range_pick.TITLE,
            "start_label": _("Start"),
            "end_label": _("End"),
            "name_label": _("Title"),
            "placeholder": range_pick.NAME_PLACEHOLDER,
            "replace_label": range_pick.REPLACE_LABEL,
            "whole_caption": _("Whole recording selected"),
            "part_caption": _("Selected {}"),  # Swift fills {} with the selected length
            "lang_label": _("Language"),
            "langs": [Choice(key=k, title=t) for k, t in LANGS],
            "asr_label": _("Recognition"),
            "sources_label": range_pick.SOURCES_LABEL,
            "ok": _("Process"),
            "cancel": _("Cancel"),
        }
        self._ask(RangeAsk(**fields, **texts, asrs=asrs, sources=sources), got)

    def open_form(self, ask: FormAsk, on_event: Callable[[FormEvent | None], None]) -> FormUpdate:
        """AskMsg(form), then on_event for each FormEventMsg of the window; update sends FormMsg
        under the same write lock; AnswerMsg(null) — the window was closed."""
        n = next(self._ids)
        self._forms[n] = on_event

        def closed(_value: Any) -> None:
            if self._forms.pop(n, None) is not None:
                on_event(None)

        def update(new: FormAsk | None) -> None:
            if n not in self._forms:
                return  # already closed
            if new is None:  # saved: the engine closes the window and ignores its AnswerMsg
                self._forms.pop(n, None)
                self._waiting.pop(n, None)
            self.channel.send(FormMsg(id=n, ask=new))

        self._waiting[n] = closed
        self.channel.send(AskMsg(id=n, ask=ask))
        return update

    def form_event(self, msg: FormEventMsg) -> None:
        """An action in a form window; an unknown id is ignored (values never go to the log)."""
        on_event = self._forms.get(msg.id)
        if on_event is None:
            logger.warning("event for unknown window %s", msg.id)
            return
        on_event(FormEvent(op=msg.op, values=msg.values, rev=msg.rev))

    def notify(self, title: str, message: str, path: Path | None) -> None:
        key = f"open:{path}" if path is not None else None
        if key is not None:
            self.offered.add(key)  # a click on the notification arrives as this action
        self.channel.send(NotifyMsg(title=title, message=message, open_key=key))

    def open_settings(self, tab: str) -> None:
        self.channel.send(SettingsMsg(tab=tab))


def _keys(node: Any) -> set[str]:
    """Action keys of the panel model (buttons, items, cards), except disabled ones."""
    if isinstance(node, list):
        return set().union(*map(_keys, node))
    if not isinstance(node, dict):
        return set()
    if node.get("enabled") is False:
        return set()
    keys = {v for k, v in node.items() if k in ("key", "open_key") and isinstance(v, str)}
    return keys.union(*map(_keys, node.values()))


class Engine:
    """The event loop: app actions and answers, ticks and BarRuntime changes, one at a time in one
    thread; after each, the panel state (not sent when unchanged)."""

    def __init__(
        self,
        channel: Channel,
        *,
        runtime: Callable[..., BarRuntime] = BarRuntime,
        root: Callable[[], Path] = library_root,
    ) -> None:
        self.channel = channel
        self.platform = ProtocolPlatform(channel)
        self.events: queue.SimpleQueue[tuple[str, Any]] = queue.SimpleQueue()
        self._waiting: set[str] = set()  # redraws queued or held, not yet handled
        self._held: dict[str, tuple[str, Any]] = {}  # redraws put aside for app input; loop only
        self.rt = runtime(on_change=lambda: self._redraw("change"), platform=self.platform)
        self._root = root
        self._open = False  # panel open: faster ticks for live source levels
        self._line = ""  # last state sent
        self._feed: tuple[Any, ...] | None = None  # what the feed depends on; a change rereads it
        self._library = LibraryView()
        self._cards: list[Any] = []  # the feed plus queued folders outside the library
        self._library_at = float("-inf")
        self._env = MenuEnv()
        self._env_at = float("-inf")
        self._env_rev = -1
        self._done = threading.Event()

    def _redraw(self, kind: str) -> None:
        """A tick or a change; while one is still waiting, a second is not queued: a slow redraw
        (a large library) must not pile them up in front of a click, "Quit Mnema" among them."""
        if kind not in self._waiting:
            self._waiting.add(kind)
            self.events.put((kind, None))

    def _next(self) -> tuple[str, Any]:
        """The next event: app input and signals in their order, a waiting redraw after them."""
        block = not self._held
        while True:
            try:
                item = self.events.get(block=block)
            except queue.Empty:
                return self._held.pop(next(iter(self._held)))
            if item[0] not in _REDRAWS:
                return item
            self._held[item[0]] = item
            block = False

    def run(self, stdin: Iterable[str]) -> None:
        self.rt.start_sync()
        self.send_state()  # before reading: the first action must find its keys shown
        threading.Thread(target=self._read, args=(stdin,), daemon=True, name="mnema-stdin").start()
        threading.Thread(target=self._timer, daemon=True, name="mnema-timer").start()
        try:
            while not self.rt.can_exit:
                kind, msg = self._next()
                self._waiting.discard(kind)  # before the redraw: a later change queues again
                try:
                    self._on(kind, msg)
                    if not self.rt.can_exit:  # quitting: the panel closes, nothing to draw
                        self.send_state()
                except Exception:  # noqa: BLE001 - one failed event does not stop the engine
                    logger.exception("engine: event %s", kind)
        finally:
            self._done.set()
            self.rt.close()
        self.channel.send(QuitMsg())

    def _read(self, stdin: Iterable[str]) -> None:
        try:
            for line in stdin:
                if not line.strip():
                    continue
                try:
                    msg = decode_incoming(line)
                except ValueError:  # a window event may hold keys: never log its content
                    shown = "window event" if "form_event" in line else repr(line[:200])
                    logger.warning("malformed line from the app: %s", shown)
                    continue
                self.events.put((msg.type, msg))
        except (OSError, ValueError):
            logger.warning("input from the app broke off", exc_info=True)
        self.events.put(("eof", None))

    def _timer(self) -> None:
        while not self._done.wait(0.1 if self._open else 1.0):
            self._redraw("tick")

    def _allowed(self, key: str) -> bool:
        """Shown by the panel or a notification, the hotkey, quit; "open:" and "reveal:" of an
        existing path — a click on a notification from a previous Mnema run."""
        if key in self.platform.offered or key in _FREE or key.startswith(_FREE_PREFIXES):
            return True
        prefix, _sep, path = key.partition(":")
        return prefix in ("open", "reveal") and Path(path).is_absolute() and Path(path).exists()

    def _on(self, kind: str, msg: Any) -> None:
        rt = self.rt
        if kind == "action":
            key = msg.key
            if self._allowed(key):
                rt.handle(key)
                if key.startswith(_FOLDER_PREFIXES):
                    self._feed = None  # the folder was renamed or taken for processing
            else:
                logger.warning("action the panel never showed: %s", key)
        elif kind == "answer":
            self.platform.answered(msg.id, msg.value)
            self._feed = None
        elif kind == "form_event":
            self.platform.form_event(msg)
        elif kind == "panel":
            self._open = msg.open
            if msg.open:
                self._feed = None  # opened: refresh the feed
        elif kind == "tick":
            rt.tick()
        elif kind == "eof":  # the app closed: cancel windows, save the recording
            self.platform.cancel_all()
            rt.handle("quit_now")
        elif kind == "signal":  # logging out: no question
            rt.handle("quit_now")

    def send_state(self) -> None:
        rt = self.rt
        now = time.monotonic()
        if now - self._env_at >= _ENV_EVERY_S or rt.env_rev != self._env_rev:
            self._env_rev = rt.env_rev  # a window saved or a model was chosen: now, not on a tick
            self._env, self._env_at = rt.menu_env(), now
        jobs = rt.job_folders()
        feed = (
            rt.state.phase,
            rt.state.last_result,
            rt.state.unfinished,
            rt.active_folder,
            rt.library_rev,
            tuple(jobs),
        )
        if feed != self._feed or (self._open and now - self._library_at >= 2):
            self._feed = feed
            # processed and queued recordings stay in place in the feed; only the one being
            # recorded is left out
            self._library = library(
                self._root(), exclude=rt.active_folder, connection=rt.sync_connection()
            )
            self._cards = with_folders(self._library.cards, jobs)
            self._library_at = now
        settings = settings_model(
            rt.prefs, self._env, hotkey_error=rt.hotkey_error, idle=rt.state.phase == Phase.IDLE
        )
        panel = panel_model(
            rt.state,
            rt.prefs,
            self._env,
            self._cards,
            settings,
            sections=self._library.sections,
        )
        line = encode(StateMsg(panel=panel))
        if line == self._line:
            return
        self._line = line
        self.platform.offered |= _keys(panel.model_dump())
        self.channel.write(line)


def _lines(fd: int) -> Iterator[str]:
    """Input lines straight from the descriptor: the reader thread must not hold the sys.stdin
    lock, or quitting with open input fails at interpreter shutdown ("could not acquire lock").
    Closes the descriptor when done."""
    tail = b""
    try:
        while chunk := os.read(fd, 65536):
            *lines, tail = (tail + chunk).split(b"\n")
            yield from (line.decode("utf-8", errors="replace") for line in lines)
    finally:
        os.close(fd)
    if tail:
        yield tail.decode("utf-8", errors="replace")


def run() -> None:
    """stdin and stdout carry only the protocol: library output goes to the log (stderr), and
    child processes get /dev/null as input — ffmpeg without -nostdin reads stdin and would eat the
    app's messages."""
    proto = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
    os.dup2(2, 1)
    inp = os.dup(0)
    null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null, 0)
    os.close(null)
    from .bundle import prepare

    notice = ""
    try:  # the self-contained app: links, login item, its place, cleanup; before the runtime
        notice = prepare()  # reads "Open at login", which the first start turns on
    except Exception:  # noqa: BLE001 - setup never stops the engine
        logger.warning("app bundle setup failed", exc_info=True)
    engine = Engine(Channel(proto))
    if notice:
        engine.rt.state.notice = notice
    signals = (signal.SIGTERM, signal.SIGINT)
    for sig in signals:
        signal.signal(sig, lambda *_args: engine.events.put(("signal", None)))
    try:
        engine.run(_lines(inp))
    finally:
        engine.rt.close()  # sync stops with the app
        for sig in signals:  # the thread returns to the app, where a Python handler cannot run
            signal.signal(sig, signal.SIG_DFL)
        proto.close()  # the app sees end of output: the engine quit


def refuse() -> None:
    """Mnema is already running: say "quitting" at once, the app closes, no second engine."""
    Channel(sys.stdout).send(QuitMsg())
