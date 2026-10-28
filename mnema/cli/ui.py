"""Terminal output: summaries, progress bars, streaming render."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table
from rich.text import Text

from .. import core as mc
from ..i18n import _

ACCENT = "#a06bff"
console = Console(highlight=False)
# Progress goes to stderr: the mlx backend redirects stdout process-wide to capture its
# verbose output, so a bar on stdout would freeze until the end.
err_console = Console(stderr=True, highlight=False)


def banner() -> None:
    # No boxes anywhere in the CLI: a path copied from a box carried border fragments.
    console.print(Text("◆  M N E M A  ◆", style=f"bold {ACCENT}"))
    console.print(Text(_("meeting recordings: transcripts and summaries"), style="dim"))


def info_panel(info: mc.MediaInfo) -> RenderableType:
    """What is being processed, as key/value rows. The audio codec is left out: recognition
    converts everything to 16 kHz WAV anyway."""
    t = Table.grid(padding=(0, 2))
    t.add_column(style="dim", justify="right")
    t.add_column(style="bold")
    t.add_row(_("file"), Text(info.path.name))  # Text: yt-dlp names carry [brackets]
    if info.duration:
        t.add_row(_("duration"), mc.fmt_duration(info.duration))
    t.add_row(_("size"), mc.fmt_size(info.size))
    if info.has_video:
        res = f"{info.width}×{info.height}" if info.width else _("video")
        fps = f" · {info.fps:.0f} fps" if info.fps else ""
        t.add_row(_("video"), f"{info.video_codec} {res}{fps}")
    return Group(Text(_("input"), style="dim"), t)


class _ProgressState(BaseModel):
    """Operation state shared by the worker thread and the progress render."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    frac: float | None = None
    msg: str = ""
    result: Any = None
    error: BaseException | None = None
    done: bool = False


class _EtaColumn(TimeRemainingColumn):
    """ETA; a dash until the engine reports a fraction (total=None), not a blank after "left"."""

    def render(self, task) -> Text:
        text = super().render(task)
        return text if text.plain.strip() else Text("—", style="progress.remaining")


def _progress_columns(label: str) -> tuple:
    """Bar columns: spinner, label, bar, percent, time left and elapsed time."""
    return (
        SpinnerColumn(style=ACCENT),
        TextColumn(f"[bold]{label}"),
        BarColumn(bar_width=None, complete_style=ACCENT, finished_style=ACCENT),
        TaskProgressColumn(),
        TextColumn(f"[dim]{_('left')}[/]"),
        _EtaColumn(),
        TextColumn("[dim]·[/]"),
        TimeElapsedColumn(),
    )


def _advance(progress: Progress, task, completed: float, total: float) -> None:
    """Move the bar and unfreeze the timer when a new phase starts after 100%.

    rich freezes elapsed time for good once a task reaches total (finished_time).
    """
    if completed < total and progress.tasks:
        progress.tasks[0].finished_time = None
    progress.update(task, completed=completed)


def run_with_progress(label: str, func, *, total: float | None = None, live_preview: bool = False):
    """Run func(on=callback) in a thread while the progress bar reads its state; re-raises."""
    state = _ProgressState()
    lock = threading.Lock()

    def on(fraction: float | None, message: str) -> None:
        with lock:
            if fraction is not None:
                state.frac = fraction
            if message:
                state.msg = message

    def work() -> None:
        try:
            state.result = func(on=on)
        except BaseException as exc:  # noqa: BLE001
            state.error = exc
        finally:
            state.done = True

    progress = Progress(*_progress_columns(label), console=err_console, expand=True)
    task = progress.add_task(label, total=total)
    thread = threading.Thread(target=work, daemon=True)
    thread.start()

    def render():
        if not live_preview or not state.msg:
            return progress
        return Group(progress, Text(state.msg[:300], style="dim italic"))

    with Live(render(), console=err_console, refresh_per_second=12) as live:
        while not state.done:
            with lock:
                frac = state.frac
            if frac is not None:
                # Fractions are normalized; with total=None the scale is 0..1.
                eff_total = total or 1.0
                if progress.tasks and progress.tasks[0].total is None:
                    progress.update(task, total=eff_total)
                _advance(progress, task, min(frac, 1.0) * eff_total, eff_total)
            else:
                progress.update(task)
            live.update(render())
            time.sleep(0.08)
        if state.error is None:
            # The last frame is complete even with total=None: a frozen spinner in the
            # scrollback looks alive next to the following bar.
            eff_total = total or progress.tasks[0].total or 1.0
            progress.update(task, total=eff_total, completed=eff_total)
        state.msg = ""  # the last frame is the bar alone
        live.update(render())
    thread.join()

    if state.error is not None:
        raise state.error
    return state.result


def run_streaming(label: str, func, *, total: float | None = None, live_preview: bool = False):
    """Run func(on=callback) in the main thread; on() itself moves the bar.

    mlx streams segments from the main thread without competing for the GIL. A heartbeat
    thread repaints the spinner about 10 times a second while there are no segments yet
    (audio extraction, weight loading), so 0% does not look stuck.
    """
    progress = Progress(
        *_progress_columns(label),
        console=err_console,
        expand=True,
        auto_refresh=False,
    )
    # Starts indeterminate: whisperX reports no fractions, so the bar pulses instead of
    # sitting at 0%.
    task = progress.add_task(label, total=None)
    msg = {"text": ""}
    paint_lock = threading.Lock()
    stop = threading.Event()

    def render():
        if not live_preview or not msg["text"]:
            return progress
        return Group(progress, Text(msg["text"][:300], style="dim italic"))

    with Live(render(), console=err_console, auto_refresh=False) as live:

        def paint() -> None:
            with paint_lock:
                live.update(render())
                live.refresh()

        def beat() -> None:
            while not stop.is_set():
                paint()
                time.sleep(0.1)

        heartbeat = threading.Thread(target=beat, daemon=True)
        heartbeat.start()

        def on(fraction: float | None, message: str) -> None:
            if fraction is not None:
                # Fractions are 0..1; with total=None the scale is 0..1 and rich estimates
                # the ETA from the pace. The first fraction makes the bar determinate.
                eff_total = total or 1.0
                if progress.tasks[0].total is None:
                    progress.update(task, total=eff_total)
                _advance(progress, task, min(fraction, 1.0) * eff_total, eff_total)
            if message:
                msg["text"] = message
            paint()

        try:
            result = func(on=on)
        finally:
            stop.set()
            heartbeat.join(timeout=1)
        done_total = total or 1
        progress.update(task, total=done_total, completed=done_total)
        msg["text"] = ""  # the last frame is the bar alone
        paint()
    return result


def die(message: str) -> None:
    # Text, not markup: file names from yt-dlp carry [id], which rich would eat as tags.
    console.print(Text(_("Error: "), style="bold red") + Text(message), soft_wrap=True)
    sys.exit(1)


def _result_lines(title: str, path: Path, detail: str) -> None:
    """Result: title, the path as one logical line (soft_wrap keeps it copyable), details."""
    console.print(title)
    console.print(Text(str(path), style="bold"), soft_wrap=True)
    if detail:
        console.print(Text(detail, style="dim"), soft_wrap=True)


def done_panel(path: Path, detail: str) -> None:
    _result_lines(f"[bold {ACCENT}]{_('✓ done')}[/]", path, detail)


def warn_panel(path: Path, title: str, detail: str) -> None:
    """Yellow result: finished with a caveat, such as a recording without a transcript."""
    _result_lines(f"[bold yellow]{title}[/]", path, detail)
