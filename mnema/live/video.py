"""The recording's screen video: screen.mov from the Mac app plus the sound of the tracks become
one video.mp4 in the recording folder, right after the recording stops."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ..core.base import Progress, noop, tool
from ..core.base import run as run_tool
from ..i18n import _
from . import meta
from . import remarks as _remarks
from .naming import WAV_HEADER
from .record import SCREEN_FILE, VIDEO_FILE

logger = logging.getLogger(__name__)

VIDEOS = (VIDEO_FILE, SCREEN_FILE)  # the recording's video, the assembled one first
_TRACKS = ("mic", "system")
_PART = ".mnema-video.mp4"  # written by ffmpeg, then replaces video.mp4


def video_file(base: Path) -> Path | None:
    """The video to open: video.mp4, or screen.mov when its assembly failed."""
    return next((base / name for name in VIDEOS if (base / name).is_file()), None)


def mux(base: Path, offset: float | None, on: Progress = noop) -> Path:
    """screen.mov + the mix of mic.wav and system.wav (those with sound) → video.mp4.

    The video is copied as is, the sound goes to AAC. offset — seconds from the sound's start
    to the first frame: the video is shifted by -itsoffset, so the sound keeps its own time
    from 0 (the transcript's times refer to it) and nothing of it is cut; adelay would move the
    sound the wrong way. ffmpeg writes a temporary file that then replaces video.mp4, and
    screen.mov goes only after that. A failure raises MediaError and leaves the folder as it
    was."""
    screen = base / SCREEN_FILE
    tracks = [
        path
        for path in (base / f"{label}.wav" for label in _TRACKS)
        if path.is_file() and path.stat().st_size > WAV_HEADER
    ]
    shift = ["-itsoffset", str(round(offset, 3))] if offset and offset > 0 else []
    inputs = [*shift, "-i", str(screen)]
    for track in tracks:
        inputs += ["-i", str(track)]
    if len(tracks) > 1:
        mix = "".join(f"[{i}:a]" for i in range(1, len(tracks) + 1))
        sound = [
            "-filter_complex",
            f"{mix}amix=inputs={len(tracks)}:duration=longest:normalize=0[a]",
            "-map", "0:v", "-map", "[a]",
        ]  # fmt: skip
    else:
        sound = ["-map", "0:v", *(["-map", "1:a"] if tracks else [])]
    codecs = ["-c:v", "copy", "-tag:v", "hvc1"]
    if tracks:
        codecs += ["-c:a", "aac", "-b:a", "96k"]
    part = base / _PART
    on(None, _("Assembling video…"))
    try:
        run_tool(
            [
                tool("ffmpeg"),
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                *inputs,
                *sound,
                *codecs,
                "-movflags",
                "+faststart",
                "-shortest",
                "-f",
                "mp4",
                str(part),
            ]  # fmt: skip
        )
        os.replace(part, base / VIDEO_FILE)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    screen.unlink(missing_ok=True)
    return base / VIDEO_FILE


def finish(base: Path, run: meta.Run, on: Progress = noop) -> None:
    """After the recording: assemble video.mp4 when the screen was recorded. A failure is a
    remark in run (the caller writes it to the history), screen.mov stays; the sound and its
    processing do not depend on it. An empty screen.mov (no frame was written) is removed."""
    screen = base / SCREEN_FILE
    try:
        if not screen.is_file():
            return
        if screen.stat().st_size == 0:
            screen.unlink()
            return
    except OSError:
        return
    offset = run.video.offset if run.video is not None else None
    try:
        mux(base, offset, on)
    except Exception as e:  # noqa: BLE001 - the sound matters more than the video
        logger.warning("%s: video not assembled: %s", base, e)
        reason = (str(e).strip().splitlines() or [type(e).__name__])[-1]
        if run.video is None:
            run.video = meta.VideoRun()
        run.video.error = reason
        note = _remarks.video_mux_failed(reason)
        run.notes.append(note)
        run.remarks.append(note.text)
