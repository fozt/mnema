"""Core base types, constants and helpers (no dependencies on other core modules)."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from ..i18n import _
from ..i18n.fmt import _MONTHS

logger = logging.getLogger(__name__)

# The Mac app is standalone (processes on its own or on a server, as chosen in the settings);
# Linux and the rest are server clients. Modules read base.STANDALONE at call time.
STANDALONE = sys.platform == "darwin"

# True while a recording is processed inside the Mac app (live/recording_client.LocalNode):
# remarks and refusals name the place of processing by this, not by the platform.
_HERE: ContextVar[bool] = ContextVar("mnema_processed_here", default=False)


def processed_here() -> bool:
    """Whether this recording is processed on this Mac (LocalNode) rather than on a server."""
    return _HERE.get()


@contextmanager
def processing_here() -> Iterator[None]:
    """Processing inside the block happens on this Mac (LocalNode)."""
    token = _HERE.set(True)
    try:
        yield
    finally:
        _HERE.reset(token)


DEFAULT_LOCAL_MODEL = "mlx-community/whisper-large-v3-turbo"
DEFAULT_WHISPERX_MODEL = "large-v3"

# Audio files: the recordings feed shows them and audio cleanup removes them.
AUDIO_EXT = frozenset({".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma"})
VIDEO_EXT = {".mov", ".mp4", ".mkv", ".m4v", ".avi", ".webm", ".mpg", ".mpeg"}
MEDIA_EXT = AUDIO_EXT | VIDEO_EXT

# on(fraction in 0..1 or None, message): the progress callback shared by the CLI, app and server.
Progress = Callable[["float | None", str], None]


def vram_status() -> str:
    """VRAM snapshot for logs; empty without torch/CUDA. Never imports torch itself."""
    try:
        import sys

        torch = sys.modules.get("torch")
        if torch is None or not torch.cuda.is_available():
            return ""
        free, total = torch.cuda.mem_get_info()
        return f"VRAM free {free >> 20} / total {total >> 20} MiB (used {(total - free) >> 20})"
    except Exception:  # noqa: BLE001 - diagnostics must not break the hot path
        return ""


def noop(fraction: float | None, message: str) -> None:
    pass


def phased(on: Progress, *weights: float) -> list[Progress]:
    """One progress bar over several phases: one callback per phase.

    The 0..1 scale is cut into slices by weight; each callback maps its local 0..1 into its
    slice, so the overall bar grows monotonically. fraction=None only passes the message.
    """
    total = sum(weights) or 1.0
    callbacks: list[Progress] = []
    acc = 0.0
    for w in weights:
        lo, hi = acc / total, (acc + w) / total
        acc += w

        def _make(lo: float, hi: float) -> Progress:
            def _on(fraction: float | None, message: str) -> None:
                if fraction is None:
                    on(None, message)
                else:
                    on(lo + min(max(fraction, 0.0), 1.0) * (hi - lo), message)

            return _on

        callbacks.append(_make(lo, hi))
    return callbacks


# Russian language names: validation keys and model prompts. Not for display (see bar/range_pick).
LANG_NAMES = {
    "ru": "русский",
    "en": "английский",
    "uk": "украинский",
    "de": "немецкий",
    "fr": "французский",
    "es": "испанский",
}
# Russian short month names; display code uses i18n.month_short instead.
MONTHS = _MONTHS["ru"]


class MediaError(RuntimeError):
    """An operation error with a message meant for the user."""


def stop_process(proc: subprocess.Popen) -> None:
    """Stop a child process and reap it before the calling operation returns."""
    if proc.poll() is None:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


# An explicit path to a media tool beats the bundled and the PATH ones.
TOOL_ENV = {"ffmpeg": "MNEMA_FFMPEG", "ffprobe": "MNEMA_FFPROBE"}
MISSING_TOOL = "{program} not found. Install ffmpeg or reinstall Mnema"


def tool(name: str) -> str:
    """Path to ffmpeg or ffprobe: MNEMA_FFMPEG/MNEMA_FFPROBE, then the copy next to this Python
    or in the Mac app bundle (MNEMA_APP_BUNDLE), then PATH."""
    explicit = os.environ.get(TOOL_ENV.get(name, ""), "").strip()
    if explicit:
        return explicit
    places = [Path(sys.executable).parent / name]
    bundle = os.environ.get("MNEMA_APP_BUNDLE", "").strip()
    if bundle:
        places.append(Path(bundle) / "Contents" / "Resources" / "bin" / name)
    for place in places:
        if place.is_file() and os.access(place, os.X_OK):
            return str(place)
    found = shutil.which(name)
    if found:
        return found
    raise MediaError(_(MISSING_TOOL, program=name))


def run(cmd: list[str], *, on: Progress = noop) -> str:
    on(None, "")
    try:
        # Without stdin=DEVNULL ffmpeg reads stdin and eats terminal keys or the bar engine input
        if on is noop:
            result = subprocess.run(
                cmd,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                stdin=subprocess.DEVNULL,
            )
        else:
            with subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8",
                errors="replace", stdin=subprocess.DEVNULL,
            ) as proc:  # fmt: skip
                try:
                    while True:
                        try:
                            stdout, stderr = proc.communicate(timeout=0.2)
                            break
                        except subprocess.TimeoutExpired:
                            on(None, "")
                finally:
                    stop_process(proc)
            result = subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    except FileNotFoundError as e:
        # ffmpeg/ffprobe missing: callers catch MediaError, not FileNotFoundError
        raise MediaError(_(MISSING_TOOL, program=Path(cmd[0]).name)) from e
    if result.returncode != 0:
        raise MediaError(
            _(
                "{command} → exit code {code}\n{output}",
                command=" ".join(cmd[:2]),
                code=result.returncode,
                output=result.stderr.strip()[-800:],
            )
        )
    return result.stdout


STAMP = r"\[\d+(?::\d{2}){1,2}\]"


def fmt_stamp(seconds: float) -> str:
    """Paragraph time from the start of the recording, rounded down to the second."""
    hours, remainder = divmod(int(max(0, seconds)), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def parse_stamp(text: str) -> float | None:
    """A [m:ss] or [h:mm:ss] stamp → seconds; an invalid stamp → None."""
    if not re.fullmatch(STAMP, text):
        return None
    parts = [int(part) for part in text[1:-1].split(":")]
    if any(part >= 60 for part in parts[-2:]):
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + part
    return float(seconds)


def unstamped(text: str) -> str:
    """Remove time stamps at line starts, keeping the text and paragraphs."""
    return re.sub(rf"^{STAMP}[ \t]*", "", text, flags=re.M)


def fmt_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return _("{n} B", n=int(num_bytes))
    size = num_bytes / 1024
    if size < 1024:
        return _("{size} KB", size=f"{size:.1f}")
    size /= 1024
    if size < 1024:
        return _("{size} MB", size=f"{size:.1f}")
    return _("{size} GB", size=f"{size / 1024:.1f}")


def ts_to_sec(ts: str) -> float:
    sec = 0.0
    for part in ts.split(":"):
        sec = sec * 60 + float(part)
    return sec


def speaker_word(lang: str | None) -> str:
    """Speaker label in the transcript's language (content, not interface language).

    lang is a backend code or name: "ru"/"russian"/"Russian" → Russian, anything else → English.
    """
    return "Спикер" if (lang or "").lower().startswith("ru") else "Speaker"
