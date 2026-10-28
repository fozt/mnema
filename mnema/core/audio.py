"""Audio track extraction and mixing."""

from __future__ import annotations

from pathlib import Path

from ..i18n import _
from .base import MediaError, Progress, noop, run, tool


def extract_audio(
    src: Path, dst: Path, *, compressed: bool = False, pad_start_ms: int = 0, on: Progress = noop
) -> Path:
    """Extract mono 16 kHz audio; compressed=True gives 32k mp3 (for upload to an API).

    pad_start_ms prepends silence: voice messages start with speech right away, and ASR
    otherwise swallows the first fraction of a second."""
    on(None, _("Extracting audio…"))
    codec = ["-c:a", "libmp3lame", "-b:a", "32k"] if compressed else ["-c:a", "pcm_s16le"]
    pad = ["-af", f"adelay={pad_start_ms}:all=1"] if pad_start_ms else []
    run(
        [
            tool("ffmpeg"),
            "-y",
            "-i",
            str(src),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            *pad,
            *codec,
            str(dst),
        ],
        on=on,
    )
    return dst


def mix(
    inputs: list[Path], dst: Path, *, on: Progress = noop, args: list[str] | None = None
) -> Path:
    """Mix synchronous tracks (shared t=0) into one track with ffmpeg amix.

    normalize=0 keeps every track at full volume (amix would divide by the input count);
    duration=longest follows the longest track. The output format comes from dst's suffix.
    """
    if len(inputs) < 2:
        raise MediaError(_("Mixing needs at least 2 tracks."))
    on(None, _("Mixing tracks…"))
    ins: list[str] = []
    for f in inputs:
        ins += ["-i", str(f)]
    run(
        [
            tool("ffmpeg"),
            "-y",
            *ins,
            "-filter_complex",
            f"amix=inputs={len(inputs)}:duration=longest:normalize=0",
            *(args or []),
            str(dst),
        ],  # fmt: skip
        on=on,
    )
    return dst
