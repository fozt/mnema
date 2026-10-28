"""Recording folder names. A recording's title is its folder name without the leading date.
The rule is shared with the phone (fixtures folder-titles-v1, folder-names-v1)."""

from __future__ import annotations

import os
import re
import stat
import struct
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

from pydantic import BaseModel

from ..core.base import AUDIO_EXT
from . import meta

# re.ASCII: \d is only 0–9, as in Kotlin; "２０２６-…" is not a date
_UNNAMED = re.compile(r"(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})(?: \(\d+\))?", re.ASCII)
_NAMED = re.compile(r"^(\d{4})-(\d{2})-(\d{2})_(.+?)(?: \(\d+\))?$", re.ASCII)
_DATED = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:$|_)", re.ASCII)
PART_LABEL = re.compile(r"( \(\d+(?:\.\d{2}){1,2}–\d+(?:\.\d{2}){1,2}\))(?: \(\d+\))?$", re.ASCII)
_UNSAFE = re.compile(r'[\x00-\x1f\x7f-\x9f/\\:*?"<>|]')
WAV_HEADER = 44  # the WAV header Mnema writes: a file without sound is no longer than this
_WAV_BYTES_PER_SECOND = 32000  # PCM16 mono 16 kHz: the length of a file without a WAV header
_CHUNK_ID = re.compile(rb"[A-Za-z0-9 _]{4}")
_MAX_TITLE = 80  # title characters in a folder name
_MAX_NAME_BYTES = 240  # name bytes: leaves room for " (2)" under the file system limit
NAME_MAX_BYTES = 255  # the file system limit for a folder name


class FolderTitle(BaseModel):
    title: str | None  # None: named by start time, the recording time is shown
    part: str = ""  # part label of a time-named folder: " (0.10–1.35)"


def part_label(name: str) -> str:
    """The part label " (0.10–1.35)" without the "(2)" of a taken name; none — empty string."""
    m = PART_LABEL.search(name)
    return m[1] if m else ""


def _offset(seconds: float) -> str:
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}.{m:02d}" if not sec else f"{h}.{m:02d}.{sec:02d}"


def part_of(start: float, end: float) -> str:
    """The part label from its bounds in recording seconds: "0.10–1.35", with seconds
    "0.10.05–…"."""
    return f"{_offset(start)}–{_offset(end)}"


def _unparted(name: str) -> str:
    while PART_LABEL.search(name):
        name = PART_LABEL.sub("", name)
    return name


def _by_time(name: str) -> re.Match[str] | None:
    return _UNNAMED.fullmatch(_unparted(name))


def display_title(name: str) -> FolderTitle:
    """The title from the folder name with the leading date dropped. A name by start time
    ("2026-10-21_08-35") has no title."""
    if _by_time(name):
        return FolderTitle(title=None, part=part_label(name))
    if (m := _NAMED.match(name)) and m[4].strip():
        return FolderTitle(title=m[4].strip())
    return FolderTitle(title=name)


def is_default_name(name: str | Path) -> bool:
    """A name by start time: processing names such a folder."""
    return display_title(name.name if isinstance(name, Path) else name).title is None


def folder_title(title: str) -> str:
    """The title as part of a folder name: forbidden and control characters become spaces,
    spaces are collapsed, dots and spaces at the edges are trimmed, at most 80 characters."""
    text = re.sub(r"\s+", " ", _UNSAFE.sub(" ", title)).strip(" .")
    return text[:_MAX_TITLE].rstrip(" .")


def _typed(title: str, part: str) -> str:
    """The title as typed: spaces collapsed, a part label typed at the end does not count."""
    text = " ".join(title.split())
    return text.removesuffix(part.strip()).rstrip() if part else text


def chosen_title(text: str | None, part: str = "") -> str | None:
    """The user's own title (processing window, "Set title…", the node's user_title): a part
    label at the end does not count; when nothing is left for a folder name ("???", "...", just
    the label) there is no title and processing names the folder (fixture folder-names-v1,
    own)."""
    typed = _typed(text or "", part)
    return typed if folder_title(typed) else None


def folder_name(start: datetime, title: str, *, part: str = "") -> str:
    """ "2026-10-21_Team standup" with the part label; without a title, the start time
    "2026-10-21_08-35". A label typed at the end of the title is not repeated; the name is at
    most 240 bytes."""
    name = folder_title(_typed(title, part)) or f"{start:%H-%M}"
    day = f"{start:%Y-%m-%d}_"
    while len(f"{day}{name}{part}".encode()) > _MAX_NAME_BYTES and len(name) > 1:
        name = name[:-1].rstrip(" .")
    return f"{day}{name}{part}"


def titled_path(folder: Path, title: str) -> Path:
    """Where to rename a recording folder for a title: the date is the recording start, the part
    label stays, empty means the name by start time, a name taken by another recording gets
    "… (2)"."""
    start, _ = recording_start(folder)
    return free_name(
        folder.with_name(folder_name(start, title, part=part_label(folder.name))), own=folder
    )


def same_folder(path: Path, own: Path | None) -> bool:
    """The same directory, including under a name in a different letter case; links are not
    followed."""
    if own is None:
        return False
    try:
        return os.path.samestat(path.lstat(), own.lstat())
    except OSError:
        return False


def free_name(path: Path, *, own: Path | None = None) -> Path:
    """A name taken by another recording gets "… (2)", "… (3)", at most 255 bytes: folders are
    never merged. Links are not followed; the folder itself (a letter case change) is not a
    taken name."""
    candidate, n = path, 2
    while os.path.lexists(candidate) and not same_folder(candidate, own):
        suffix, stem = f" ({n})", path.name
        while len(f"{stem}{suffix}".encode()) > NAME_MAX_BYTES and len(stem) > 1:
            stem = stem[:-1]
        candidate, n = path.with_name(f"{stem}{suffix}"), n + 1
    return candidate


def _named_start(name: str) -> tuple[datetime, bool] | None:
    """The start from the folder name: a name by time gives date and time (True),
    "2026-09-27_Standup" only the date (False); no leading date — None."""
    try:
        if m := _by_time(name):
            year, month, day, hour, minute = map(int, m.groups())
            return datetime(year, month, day, hour, minute), True
        if m := _DATED.match(name):
            return datetime(int(m[1]), int(m[2]), int(m[3])), False
    except ValueError:  # "2026-13-45_…" is not a date
        pass
    return None


def _meta_start(folder: Path) -> datetime | None:
    started = meta.started(folder)
    try:
        return datetime.fromisoformat(started).replace(tzinfo=None) if started else None
    except ValueError:
        return None


def chunk_follows(f: BinaryIO, at: int, size: int) -> bool:
    """A RIFF chunk starts at byte ``at`` of a file of ``size`` bytes (a tag: the sound before it
    is complete), rather than sound the header does not cover."""
    if at + 8 > size:
        return False
    f.seek(at)
    tail = f.read(8)
    return (
        len(tail) == 8
        and _CHUNK_ID.fullmatch(tail[:4]) is not None
        and struct.unpack("<I", tail[4:])[0] <= size - (at + 8)
    )


def _wav_data(f: BinaryIO, size: int) -> tuple[int, int] | None:
    """Bytes per second and sound bytes of a PCM WAV; None — not a PCM WAV."""
    head = f.read(12)
    if len(head) < 12 or head[:4] != b"RIFF" or head[8:] != b"WAVE":
        return None
    pos, rate = 12, None
    while pos + 8 <= size:
        f.seek(pos)
        chunk = f.read(8)
        if len(chunk) < 8:
            return None
        length = struct.unpack("<I", chunk[4:])[0]
        body = pos + 8
        if chunk[:4] == b"fmt ":
            fmt = f.read(16)
            if length < 16 or len(fmt) < 16:
                return None
            tag, channels, frame_rate, _, _, bits = struct.unpack("<HHIIHH", fmt)
            block = channels * (bits // 8)
            if tag not in (1, 0xFFFE) or frame_rate <= 0 or bits % 8 or block <= 0:
                return None
            rate = (frame_rate * block, block)
        elif chunk[:4] == b"data":
            if rate is None:
                return None
            available = size - body
            # the sound length was never written (the recording broke off): sound to the end
            end = body + length + length % 2
            unfinished = length > available or (end < size and not chunk_follows(f, end, size))
            data = available if unfinished else length
            return rate[0], data - data % rate[1]
        pos = body + length + length % 2
    return None


def wav_seconds(path: Path, size: int | None = None) -> float:
    """Length of a WAV track (fixture recording-start-v1, lengths): from the header's rate,
    channels and sound size; when the sound size was never written (the recording broke off),
    up to the end of the file; not a PCM WAV or unreadable — (size − 44) / 32000. ``size`` is the
    file size when already known."""
    try:
        size = path.stat().st_size if size is None else size
        with path.open("rb") as f:
            found = _wav_data(f, size)
    except (OSError, struct.error):
        found = None
    if found is not None:
        return found[1] / found[0]
    return max((size or 0) - WAV_HEADER, 0) / _WAV_BYTES_PER_SECOND


def _track_start(folder: Path, files: dict[str, os.stat_result] | None) -> datetime | None:
    """From the recording files (hidden ".…" ones do not count): the end of the longest WAV
    track minus its length; else the earliest audio file, else the earliest file."""
    if files is None:
        files = {}
        try:
            paths = list(folder.iterdir())
        except OSError:  # the folder is gone or unreadable: the start comes from the name
            return None
        for path in paths:
            try:
                current = path.lstat()
            except OSError:  # the file vanished during the walk
                continue
            if stat.S_ISREG(current.st_mode):
                files[path.name] = current
    files = {name: st for name, st in files.items() if not name.startswith(".")}
    wavs = [
        (wav_seconds(folder / name, st.st_size), st.st_mtime)
        for name, st in files.items()
        if name.lower().endswith(".wav")
    ]
    if longest := max(wavs, default=None):
        return datetime.fromtimestamp(longest[1] - longest[0])
    audio = [st for name, st in files.items() if Path(name).suffix.lower() in AUDIO_EXT]
    first = min((st.st_mtime for st in audio or files.values()), default=None)
    return datetime.fromtimestamp(first) if first is not None else None


def _start(folder: Path, files: dict[str, os.stat_result] | None) -> tuple[datetime, bool] | None:
    named = _named_start(folder.name)
    if named is not None and named[1]:
        return named
    when = _meta_start(folder) or _track_start(folder, files)
    if when is None:
        return named
    if named is not None and named[0].date() != when.date():
        return datetime.combine(named[0].date(), when.time()), True
    return when, True


def recording_start(
    folder: Path, files: dict[str, os.stat_result] | None = None
) -> tuple[datetime, bool]:
    """The recording start in the time zone where it was recorded; False — only the date is
    known (fixture recording-start-v1). A name by time gives date and time; otherwise
    meta.started or a track, and the leading date of the name wins when the day differs (the
    folder was copied, mtime rewritten, the date fixed in Finder). The folder mtime is never
    used; nothing known — now."""
    return _start(folder, files) or (datetime.now(), False)


def started_iso(folder: Path) -> str | None:
    """meta.started for a recording without it, by the same rule, in this machine's time zone;
    an unknown start time — None: a guess is not written."""
    start = _start(folder, None)
    if start is None or not start[1]:
        return None
    return start[0].astimezone().isoformat(timespec="seconds")
