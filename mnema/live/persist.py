"""Session artifacts in the folder `base`: transcript.txt (with source labels), summary.txt and
optionally translation.md. The `translate` command reads the transcript as is via
ops._transcript_text.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Sequence
from pathlib import Path

from ..core.models import Segment, Transcript
from .models import LiveResult, Track


def _transcript(tracks: list[Track], language: str | None) -> Transcript:
    segments: list[Segment] = sorted(
        (s for t in tracks for s in t.committed), key=lambda s: s.start
    )
    text = " ".join(s.text.strip() for s in segments)
    return Transcript(text=text, segments=segments, language=language)


def dialogue_text(tracks: list[Track], language: str | None) -> str:
    """All tracks interleaved by time, with a speaker label at every change of turn: the
    transcript.txt form."""
    return _transcript(tracks, language).with_speakers(timed=True)


def _umask() -> int:
    current = os.umask(0)
    os.umask(current)
    return current


def _atomic_bytes(path: Path, content: bytes) -> None:
    """A failed write never truncates the old file; the temporary file is on the same disk."""
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temporary = Path(name)
    try:
        # mkstemp creates 0600; the transcript must stay readable like an ordinary file
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o666 & ~_umask()
        os.chmod(temporary, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_transcript(
    tracks: list[Track],
    base: Path,
    language: str | None,
) -> Path | None:
    """Replace transcript.txt atomically."""
    body = dialogue_text(tracks, language)
    if not body.strip():
        return None
    txt = base / "transcript.txt"
    content = (body + "\n").encode("utf-8")
    try:
        previous = txt.read_bytes()
    except FileNotFoundError:
        previous = b""
    if previous == content:
        return txt
    _atomic_bytes(txt, content)
    return txt


def write_summary(base: Path, about: str, points: list[str]) -> Path:
    """summary.txt: the gist of the call, then the points. The recording's title is the folder
    name and is not in the file."""
    path = base / "summary.txt"
    lead = f"{about}\n\n" if about else ""
    path.write_text(lead + "\n".join(f"- {p}" for p in points) + "\n", encoding="utf-8")
    return path


def save(
    tracks: list[Track],
    *,
    base: Path,
    recordings: Sequence[Path] = (),
    translation: str | None = None,
    language: str | None = None,
    keep_transcript: bool = False,
) -> LiveResult:
    """Write transcript.txt (and translation.md) into `base`; return a LiveResult with the paths.

    keep_transcript: this processing already wrote transcript.txt right after recognition, so it
    is not rewritten (it may have been edited while the summary was being made).

    An empty transcript (recognition failed or did not run) creates no file
    (transcript_txt=None): an empty transcript.txt looks like a finished transcript and breaks
    `translate` downstream.
    """
    base.mkdir(parents=True, exist_ok=True)
    early = base / "transcript.txt"
    txt = early if keep_transcript and early.exists() else write_transcript(tracks, base, language)

    translation_md: Path | None = None
    if translation:
        translation_md = base / "translation.md"
        translation_md.write_text(translation.strip() + "\n", encoding="utf-8")

    return LiveResult(
        tracks=tracks,
        recordings=list(recordings),
        transcript_txt=txt,
        translation_md=translation_md,
    )
