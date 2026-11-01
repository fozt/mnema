"""The recording's video with its sound (Mac): screen.mov and the WAV tracks become video.mp4."""

from __future__ import annotations

from pathlib import Path

from ..core.base import Progress, noop
from . import meta


def finish(base: Path, run: meta.Run, on: Progress = noop) -> None:
    """After the recording stopped, before processing: one video.mp4 in the folder."""
