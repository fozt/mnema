"""Media file probing with ffprobe."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from ..i18n import _
from .base import MediaError, run, tool


class MediaInfo(BaseModel):
    path: Path
    duration: float
    size: int
    audio_codec: str | None
    video_codec: str | None
    width: int | None = None
    height: int | None = None
    fps: float | None = None

    @property
    def has_audio(self) -> bool:
        return bool(self.audio_codec)

    @property
    def has_video(self) -> bool:
        return bool(self.video_codec)


def _ffprobe(path: Path, args: list[str]) -> str:
    return run([tool("ffprobe"), "-v", "error", *args, str(path)]).strip()


def probe(path: Path) -> MediaInfo:
    path = Path(path)
    if not path.is_file():
        raise MediaError(_("File not found: {path}", path=path))

    def field(kind: str, name: str) -> str:
        lines = _ffprobe(
            path,
            [
                "-select_streams",
                kind,
                "-show_entries",
                f"stream={name}",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
            ],
        ).splitlines()
        return lines[0] if lines else ""

    audio_codec = field("a", "codec_name") or None
    video_codec = field("v", "codec_name") or None

    width = height = None
    fps = None
    if video_codec:
        wh = _ffprobe(
            path,
            ["-select_streams", "v", "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0"],
        ).splitlines()
        if wh and "x" in wh[0]:
            try:
                width, height = (int(x) for x in wh[0].split("x")[:2])
            except ValueError:
                pass
        rate = _ffprobe(
            path,
            [
                "-select_streams",
                "v",
                "-show_entries",
                "stream=r_frame_rate",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
            ],
        ).splitlines()
        if rate and "/" in rate[0]:
            num, den = rate[0].split("/")
            fps = float(num) / float(den) if float(den) else None

    dur_raw = _ffprobe(
        path, ["-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1"]
    )
    try:
        duration = float(dur_raw)
    except ValueError:
        duration = 0.0

    return MediaInfo(
        path=path,
        duration=duration,
        size=path.stat().st_size,
        audio_codec=audio_codec,
        video_codec=video_codec,
        width=width,
        height=height,
        fps=fps,
    )
