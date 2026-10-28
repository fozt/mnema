"""Terminal live view: lines are grouped into speaker turns (like with_speakers in a batch
transcript). Consecutive lines of one speaker grow one paragraph in the live area; a turn goes to
the scrollback (console.print above the live area) as a whole on a speaker change, an inactivity
timeout or a size limit (a huge block does not fit the terminal's live area).

"Line ↔ translation" pairs stay inside the turn: the line shows at once and the translation is
attached when ready (a streaming translator grows it in place, "⟶ …" → "⟶ Hello, how…"); a turn
does not leave while its translations are in flight, so pairs never split. Below the live area is
the live line of each source's unfinished line. Modelled on cli/ui.py::run_streaming.

Two views share one turn lifecycle (_TurnView):
- LiveView: line by line, turns go to the scrollback, live area at the bottom;
- PaneView: two-column full screen (rich alternate screen), original turns on the left and their
  translations on the right, one row per turn. Shows the tail of turns that fits the terminal
  height (the archive is in the file); on exit the whole result is printed to the normal terminal
  so the text stays in the scrollback after Ctrl-C.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from typing import Self

from pydantic import BaseModel, Field
from rich.console import Console, Group, RenderableType
from rich.layout import Layout
from rich.live import Live
from rich.table import Table
from rich.text import Text

from ..cli.ui import ACCENT, err_console
from ..i18n import _

_TURN_TIMEOUT = 30.0  # seconds without new lines → the turn goes to the scrollback
_TURN_MAX_CHARS = 700  # an endless monologue must not outgrow the live area


class _Entry(BaseModel):
    """A line inside a turn: text and translation (finished or growing)."""

    text: str
    translation: str | None = None
    pending: bool = False  # translation in flight: the turn stays in the live area
    partial: str = ""  # growing translation while pending


class _Turn(BaseModel):
    """A speaker turn: consecutive lines of one speaker as one growing block."""

    label: str
    entries: list[_Entry] = Field(default_factory=list)
    closed: bool = False  # speaker change, timeout or size: waits only for its translations
    last_at: float = 0.0  # time of the last line (inactivity timeout)


def _translated_parts(turn: _Turn) -> list[str]:
    """The turn's translations in line order; one in flight shows as a growing tail with "…"."""
    parts: list[str] = []
    for e in turn.entries:
        if e.pending:
            parts.append(f"{e.partial}…" if e.partial else "…")
        elif e.translation:
            parts.append(e.translation)
    return parts


def _turn_block(turn: _Turn) -> Text:
    """Turn block: bright label, the lines as a paragraph, translations dimmed on a new line."""
    t = Text()
    t.append(f"{turn.label}: ", style=f"bold {ACCENT}")
    t.append(" ".join(e.text for e in turn.entries))
    parts = _translated_parts(turn)
    if parts:
        t.append(f"\n  ⟶  {' '.join(parts)}", style="dim")
    return t


def _turn_lines(turn: _Turn, draft: str = "") -> list[Text]:
    """A turn in the live area: one row per line (plus a row for its translation), appended only.

    A joined paragraph would rewrap the whole block on every append and the reader would lose
    their place; the scrollback gets the joined paragraph (_turn_block). draft is the speech
    tail ASR may still rewrite: dimmed, at the end of the block's last row.
    """
    rows: list[Text] = []
    for i, e in enumerate(turn.entries):
        t = Text()
        t.append(f"{turn.label}: " if i == 0 else "  ", style=f"bold {ACCENT}" if i == 0 else "")
        t.append(e.text)
        if draft and i == len(turn.entries) - 1:
            t.append(f" {draft}", style="dim")
        rows.append(t)
        if e.pending or e.translation:
            body = e.translation or ""
            if e.pending:
                body = f"{e.partial}…" if e.partial else "…"
            rows.append(Text(f"  ⟶  {body}", style="dim"))
    return rows


# a frozen live line (the ASR track died silently) is cleared after this many seconds
_LINE_STALE = 15.0


def _live_line(label: str, text: str) -> Text:
    """Live line of the current unfinished line, updated in place."""
    if not text:
        return Text("")
    t = Text()
    t.append(f"{label}⋯ ", style=ACCENT)  # "speaking now" marker
    t.append(text)
    return t


class _TurnView:
    """Base of the live views: hold() adds a line to the current turn with its translation in
    flight; commit() adds a line or completes a pair; partial() grows a translation; set_line()
    sets the live line. Subclasses decide how the screen is drawn (_renderable) and where
    finished turns go (_flush)."""

    _screen = False  # the full-screen subclass turns on rich alternate screen

    def __init__(
        self,
        sources: list[str],
        *,
        translate: bool = False,
        clock: Callable[[], float] = time.monotonic,
        console: Console | None = None,
    ) -> None:
        self._labels = list(sources)
        self._translate = translate
        self._clock = clock
        self._console = console or err_console
        self._lines: dict[str, str] = {s: "" for s in sources}
        self._line_at: dict[str, float] = {}  # last update of the live line
        # source status (warming up, ready, copied) is a separate channel: the session rewrites
        # the live line with the draft on every tick, empty included, so a status would not survive
        self._status: dict[str, str] = {}
        self._status_at: dict[str, float] = {}
        # closed turns waiting for translations, plus the current one (last, if not closed)
        self._turns: list[_Turn] = []
        self._live: Live | None = None
        # Two threads change the view: the session loop (set_line/hold/commit/set_status) and the
        # translation worker (commit/partial). The RLock serializes every change of _turns/_lines/
        # _status (otherwise a double flush, a lost turn or IndexError in _drain). Reentrant:
        # public methods call the internal ones (_turn_for/_drain/_refresh) under the lock.
        self._lock = threading.RLock()

    def _renderable(self) -> RenderableType:
        raise NotImplementedError

    def _flush(self, turn: _Turn) -> None:
        """A finished turn leaves the live area: to the scrollback or to the PaneView store."""
        raise NotImplementedError

    def _expire(self) -> None:
        """A frozen draft (the track died silently) and a stale status are cleared."""
        now = self._clock()
        for lbl in self._labels:
            if now - self._line_at.get(lbl, now) >= _LINE_STALE:
                self._lines[lbl] = ""
            if now - self._status_at.get(lbl, now) >= _LINE_STALE:
                self._status.pop(lbl, None)

    def _open_turn(self) -> _Turn | None:
        """The open (growing) turn: the draft tail is drawn inside its block."""
        if self._turns and not self._turns[-1].closed:
            return self._turns[-1]
        return None

    def _draft_for(self, turn: _Turn | None) -> tuple[str, str] | None:
        """The draft that continues the open turn: (draft label, text) or None.

        Turn and draft labels may differ (a turn may carry a speaker name, a draft carries the
        source name "mic"). A matching label wins; otherwise the only non-empty draft continues
        the only open turn (the usual case: one microphone).
        """
        if turn is None:
            return None
        drafts = {lbl: text for lbl, text in self._lines.items() if text}
        if turn.label in drafts:
            return turn.label, drafts[turn.label]
        if len(drafts) == 1:
            return next(iter(drafts.items()))
        return None

    def _loose_drafts(self, consumed: set[str]) -> list[Text]:
        """Drafts not attached to the open turn (speech has just started, no block yet)."""
        return [
            _live_line(lbl, self._lines[lbl])
            for lbl in self._labels
            if self._lines.get(lbl) and lbl not in consumed
        ]

    def _status_rows(self) -> list[Text]:
        """Source statuses (warming up, ready, copied); live speech hides the status."""
        return [
            _live_line(lbl, self._status[lbl])
            for lbl in self._labels
            if self._status.get(lbl) and not self._lines.get(lbl)
        ]

    def __enter__(self) -> Self:
        self._live = Live(
            self._renderable(),
            console=self._console,
            refresh_per_second=12,
            auto_refresh=False,
            screen=self._screen,
        )
        self._live.start()
        return self

    def __exit__(self, *exc: object) -> None:
        with self._lock:
            if self._live is not None:
                # lines still waiting for a translation are not lost: they leave without it
                for turn in self._turns:
                    turn.closed = True
                    for e in turn.entries:
                        e.pending = False
                        e.partial = ""
                self._drain()
                self._refresh()  # the live area is empty, so stop() does not show stale content
                self._live.stop()
                self._live = None

    def hold(self, label: str, text: str) -> None:
        """A finished line shows at once in the current turn; its translation attaches later."""
        with self._lock:
            turn = self._turn_for(label)
            turn.entries.append(_Entry(text=text, pending=True))
            turn.last_at = self._clock()
            self._lines[label] = ""  # the line is finished: the live line starts over
            self._drain()
            self._refresh()

    def commit(self, label: str, text: str, translation: str | None = None) -> None:
        with self._lock:
            entry = self._find_pending(label, text)
            if entry is not None:  # the translation of a held line arrived
                entry.translation = translation
                entry.pending = False
                entry.partial = ""
                # the live line already shows the next phrase
            else:  # an immediate commit without a translation
                turn = self._turn_for(label)
                turn.entries.append(_Entry(text=text, translation=translation))
                turn.last_at = self._clock()
                self._lines[label] = ""
            self._drain()
            self._refresh()

    def partial(self, label: str, text: str, translation: str) -> None:
        """Growing translation of a held line (translator stream): "…" → "Hello, how…"."""
        with self._lock:
            entry = self._find_pending(label, text)
            if entry is not None:
                entry.partial = translation
                self._refresh()

    def set_line(self, label: str, text: str) -> None:
        with self._lock:
            self._close_stale()  # new speech after a lull: the stale turn leaves
            self._lines[label] = text
            self._line_at[label] = self._clock()
            if text:  # speech started: the status is done and does not come back
                self._status.pop(label, None)
            self._drain()
            self._refresh()

    def set_status(self, label: str, text: str) -> None:
        """Source status in the live area (warming up, ready, copied).

        A separate channel the draft (set_line) does not overwrite: shown while the draft is
        empty, cleared by new speech or when stale."""
        with self._lock:
            self._status[label] = text
            self._status_at[label] = self._clock()
            self._refresh()

    def _turn_for(self, label: str) -> _Turn:
        """The speaker's current turn; a label change, timeout or size closes the previous one."""
        self._close_stale()
        if self._turns and not self._turns[-1].closed:
            last = self._turns[-1]
            grown = sum(len(e.text) for e in last.entries) >= _TURN_MAX_CHARS
            if last.label == label and not grown:
                return last
            last.closed = True  # leaves as a whole once its translations arrive
        turn = _Turn(label=label, last_at=self._clock())
        self._turns.append(turn)
        return turn

    def _close_stale(self) -> None:
        """Inactivity timeout: speech after a long lull starts a new block."""
        if self._turns and not self._turns[-1].closed:
            last = self._turns[-1]
            if self._clock() - last.last_at >= _TURN_TIMEOUT:
                last.closed = True

    def _find_pending(self, label: str, text: str) -> _Entry | None:
        for turn in self._turns:
            if turn.label != label:
                continue
            for e in turn.entries:
                if e.pending and e.text == text:
                    return e
        return None

    def _drain(self) -> None:
        """Closed turns whose translations arrived leave the live area, strictly in order."""
        while self._turns:
            turn = self._turns[0]
            if not turn.closed or any(e.pending for e in turn.entries):
                return
            self._flush(turn)
            self._turns.pop(0)

    def _refresh(self) -> None:
        if self._live is not None:
            self._live.update(self._renderable(), refresh=True)


class LiveView(_TurnView):
    """Line view: turns row by row in the live area, finished ones go to the scrollback."""

    def _renderable(self) -> Group:
        self._expire()
        open_turn = self._open_turn()
        inline = self._draft_for(open_turn)
        consumed = {inline[0]} if inline else set()
        rows: list[Text] = []
        for t in self._turns:
            draft = inline[1] if (inline is not None and t is open_turn) else ""
            rows.extend(_turn_lines(t, draft=draft))
        rows.extend(self._loose_drafts(consumed))
        rows.extend(self._status_rows())
        return Group(*rows)

    def _flush(self, turn: _Turn) -> None:
        if self._live is not None:
            self._live.console.print(_turn_block(turn))


_PANE_GAP = 1  # blank row between turns
_PANE_PAD = 2  # gap between the grid columns


def _pane_col_width(width: int) -> int:
    """Column width for estimating wraps: a bit narrower than the real one (word wrap takes
    more rows than character wrap), better one turn fewer than a cut-off fresh turn."""
    return max(16, (width - 2 * _PANE_PAD) // 2 - 4)


def _pane_cells(turn: _Turn, draft: str = "") -> tuple[Text, Text]:
    """Row cells: label and original on the left (plus the dimmed draft), translations right."""
    left = Text()
    left.append(f"{turn.label}: ", style=f"bold {ACCENT}")
    left.append(" ".join(e.text for e in turn.entries))
    if draft:
        left.append(f" {draft}", style="dim")
    right = Text(" ".join(_translated_parts(turn)), style="dim")
    return left, right


def _cell_lines(text: str, width: int) -> int:
    """Estimated number of rows of a cell wrapped at width."""
    return sum(max(1, math.ceil(len(line) / width)) for line in text.split("\n"))


def visible_turns(turns: list[_Turn], *, height: int, col_width: int) -> list[_Turn]:
    """The tail of turns that fits in height rows (follows the end; the archive is in the file).

    A row is as tall as its taller column, with a gap between rows. The last turn is always
    shown, even if it alone does not fit (it is cut at the bottom, but the latest is visible).
    """
    out: list[_Turn] = []
    used = 0
    for turn in reversed(turns):
        left, right = _pane_cells(turn)
        rows = max(_cell_lines(left.plain, col_width), _cell_lines(right.plain, col_width))
        if out and used + rows + _PANE_GAP > height:
            break
        used += rows + (_PANE_GAP if out else 0)
        out.append(turn)
        if used >= height:
            break
    out.reverse()
    return out


class PaneView(_TurnView):
    """Two-column full-screen view (--translate): original left, translation right, a row per turn.

    Full screen allows rewriting what is shown: a growing translation updates in its cell and
    the turn's lines are appended on the left. Nothing goes to the scrollback during the
    session; on exit the whole result is printed to the normal terminal at once."""

    _screen = True

    def __init__(
        self,
        sources: list[str],
        *,
        translate: bool = False,
        clock: Callable[[], float] = time.monotonic,
        console: Console | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(sources, translate=translate, clock=clock, console=console)
        self._done: list[_Turn] = []  # finished turns: stay on screen and go to the result
        # intro line (translation directions, audio is recording): the alternate screen hides
        # everything printed before it, so the hint has to live inside the screen
        self._hint = hint

    def _flush(self, turn: _Turn) -> None:
        self._done.append(turn)

    def _renderable(self) -> Layout:
        width, height = self._console.size
        self._expire()
        open_turn = self._open_turn()
        inline = self._draft_for(open_turn)
        consumed = {inline[0]} if inline else set()
        loose = self._loose_drafts(consumed)  # drafts without a block go into the grid
        # the bottom holds only statuses and hints; speech is always above
        bottom = [
            *self._status_rows(),
            *([Text(self._hint, style="dim")] if self._hint else []),
            Text(_("Ctrl-T copy text · Ctrl-C stop"), style="dim"),
        ]
        visible = visible_turns(
            [*self._done, *self._turns],
            height=max(1, height - len(bottom) - len(loose) - 1),
            col_width=_pane_col_width(width),
        )
        grid = Table.grid(expand=True, padding=(0, _PANE_PAD))
        grid.add_column(ratio=1)
        grid.add_column(ratio=1)
        for i, turn in enumerate(visible):
            if i:
                grid.add_row("", "")
            draft = inline[1] if (inline is not None and turn is open_turn) else ""
            grid.add_row(*_pane_cells(turn, draft=draft))
        for row in loose:  # speech has just started: no block yet
            grid.add_row(row, Text(""))
        layout = Layout()
        layout.split_column(
            Layout(grid, name="turns", ratio=1),
            Layout(Group(*bottom), name="bottom", size=len(bottom)),
        )
        return layout

    def __exit__(self, *exc: object) -> None:
        was_live = self._live is not None
        super().__exit__(*exc)  # finishes the turns, drains into _done, leaves the alt screen
        if was_live:
            # the result goes to the normal terminal so it stays in the scrollback. Under the
            # lock: if the translation worker's join timed out, it is still alive and appends to
            # _done ("list changed size during iteration" otherwise).
            with self._lock:
                for turn in self._done:
                    self._console.print(_turn_block(turn))
                self._done.clear()
