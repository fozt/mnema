"""The recording's screen video: screen.mov from the Mac app plus the sound of the tracks become
one video.mp4 in the recording folder, as the first step of the recording's processing (here,
wherever the text is made); a screen.mov left by a quit or a crash goes at its next processing."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ..core.base import Progress, noop, tool
from ..core.base import run as run_tool
from ..i18n import _
from ..sync.scan import FILES_LOCK
from . import meta
from . import remarks as _remarks
from .naming import WAV_HEADER
from .record import SCREEN_FILE, VIDEO_FILE

logger = logging.getLogger(__name__)

VIDEOS = (VIDEO_FILE, SCREEN_FILE)  # the recording's video, the assembled one first
ASSEMBLING = "Assembling video…"  # the step of processing, without a share
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
    sound the wrong way. No -shortest: the video stops a moment before the sound (it is stopped
    first), and the file is as long as the sound, the last frame staying on screen. ffmpeg writes
    a temporary file that then replaces video.mp4, and screen.mov goes only after that. A
    failure raises MediaError and leaves the folder as it was."""
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
    on(None, _(ASSEMBLING))
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
                "-f",
                "mp4",
                str(part),
            ],  # fmt: skip
            on=on,  # a stop or a quit raises in on: ffmpeg is killed, not left running
        )
        os.replace(part, base / VIDEO_FILE)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    try:
        screen.unlink(missing_ok=True)
    except OSError:  # video.mp4 is whole: a screen.mov left behind is assembled again later
        logger.warning("%s: %s not removed", base, SCREEN_FILE, exc_info=True)
    return base / VIDEO_FILE


def pending(base: Path) -> bool:
    """The folder has a screen.mov to assemble."""
    return (base / SCREEN_FILE).is_file()


def finish(base: Path, run: meta.Run, on: Progress = noop) -> None:
    """Assemble video.mp4 when the screen was recorded. A failure is a remark in run (the caller
    writes it to the history), screen.mov stays; the sound and its processing do not depend on
    it; a remark of an earlier failed assembly goes. An empty screen.mov (no frame was written)
    and a temporary file left by a crash are removed."""
    screen = base / SCREEN_FILE
    try:
        (base / _PART).unlink(missing_ok=True)
        if not screen.is_file():
            return
        if screen.stat().st_size == 0:
            screen.unlink()
            return
    except OSError:
        return
    if run.video is None:
        run.video = meta.VideoRun()
    earlier = {n.text for n in run.notes if n.code == _remarks.VIDEO_MUX_FAILED}
    run.notes = [n for n in run.notes if n.code != _remarks.VIDEO_MUX_FAILED]
    run.remarks = [r for r in run.remarks if r not in earlier]
    # the capture's own failure stays; one of an earlier assembly does not
    capture = run.video.error if run.video.error in _remarks.VIDEO_REASONS else ""
    run.video.error = capture
    try:
        mux(base, run.video.offset, on)
    except Exception as e:  # noqa: BLE001 - the sound matters more than the video
        logger.warning("%s: video not assembled: %s", base, e)
        reason = (str(e).strip().splitlines() or [type(e).__name__])[-1]
        run.video.error = capture or reason
        note = _remarks.video_mux_failed(reason)
        run.notes.append(note)
        run.remarks.append(note.text)


def assemble(base: Path, run: meta.Run | None = None, on: Progress = noop) -> None:
    """The first step of processing a recording with a screen.mov: finish(), and the history
    keeps the video facts for every later processing (meta.set_video). run — the run of a
    just-finished recording; without it, the facts the recording left in its history. An
    exception raised by on (processing stopped) stops the assembly, screen.mov stays."""
    if run is None:
        kept = meta.recorded_video(base)
        run = meta.Run()
        if kept is not None:
            run.video = meta.VideoRun(offset=kept.offset, error=kept.error)
            run.notes = list(kept.notes)
    finish(base, run, on)
    keep(base, run)


def keep(base: Path, run: meta.Run) -> None:
    """The video facts of run go into the recording's history at once: a processing that comes
    later (or never: the server was down, Mnema quit) does not lose them."""
    if run.video is None:
        return
    notes = [n for n in run.notes if n.code.startswith("video")]
    try:
        with FILES_LOCK:
            meta.set_video(base, meta.RecordedVideo(**run.video.model_dump(), notes=notes))
    except OSError:  # the history is not writable: the recording stays as it is
        logger.warning("the video of %s is not in its history", base, exc_info=True)
