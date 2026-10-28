"""The panel's recordings list: recording folders → cards.

Everything comes from the folder's files: the length from WAV, the gist from summary.md, the date
and history from meta. Folders without meta are read by their audio and text files. The cache
tracks every file's changes; this walk is for display only, never for sync deletions.
"""

from __future__ import annotations

import json
import os
import re
import textwrap
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from ..core.base import AUDIO_EXT
from ..i18n import _, _n
from ..i18n import current as ui_language
from ..library.disk import LibraryError, read_file
from ..library.layout import TEXTS, Kind
from ..library.models import FileName, Stamp
from ..library.walk import read_folder, walk
from ..live import meta
from ..live.meta import META_FILE, RecordState
from ..live.naming import display_title, folder_title, free_name, recording_start, wav_seconds
from ..live.setup import PROCESSING_MARK, folder_state
from ..sync.scan import FILES_LOCK, ensure_identity, renew_identity
from ..sync.state import Checkpoint, current, known_sections, texts_confirmed

_PARAGRAPH = re.compile(r"\n\s*\n")


class RecordingCard(BaseModel):
    path: Path
    id: str = ""  # meta.id is assigned before the card is published
    section: str = ""
    title: str | None  # None: the folder is named by its start time
    part: str = ""  # part mark of a time-named folder: " (0.10–1.35)"
    started: datetime
    time_known: bool = True  # False: only the date is known (from the folder name)
    seconds: float  # 0: unknown length (no WAV)
    gist: str  # from summary.md
    has_text: bool
    has_summary: bool
    remarks: list[str]  # processing remarks, full text
    state: RecordState  # meta.record_state
    has_audio: bool = True  # audio files exist
    has_sound: bool = True  # the audio is not empty (meta.has_sound): something to process
    languages: list[str] = Field(default_factory=list)  # of the last processing
    finished: bool = False  # the last processing in history, a stopped one too, reached the end
    processing: bool = False  # .mnema-processing is present: running or interrupted
    texts: dict[FileName, Stamp] = Field(default_factory=dict)  # text stamps, as sync sees them
    cleanable: bool = False  # see cleanable()


class LibraryView(BaseModel):
    cards: list[RecordingCard] = Field(default_factory=list)
    sections: list[str] = Field(default_factory=list)


class CleanupResult(BaseModel):
    removed: int = 0  # recordings whose audio went to the Trash
    errors: list[str] = Field(default_factory=list)


class CleanupPlan(BaseModel):
    records: int = 0
    bytes: int = 0
    reason: str = ""  # records == 0: why there is nothing to delete, as a notice


AFTER_SYNC = "Audio can be deleted after syncing with the server"
ONLY_UNPROCESSED = "Only unprocessed recordings have audio"
ONLY_SILENT = "Only recordings without speech have audio"
NOTHING = "No recordings with audio that can be deleted"


def _no_audio() -> str:
    """Sync does not carry audio, so this machine may have none."""
    from ..core import base

    if base.STANDALONE:
        return _("No recordings on this Mac have audio")
    return _("No recordings on this computer have audio")


_DONE = (RecordState.PROCESSED, RecordState.NO_SPEECH)  # processing reached the end


def _waiting_for_server(card: RecordingCard) -> bool:
    """Everything cleanable() needs except the server confirming the texts."""
    return (
        card.has_audio
        and card.state is RecordState.PROCESSED
        and card.finished
        and not card.processing
    )


def records_count(count: int) -> str:
    """The number of recordings in "Delete audio of {records}?": Russian needs the genitive."""
    return _n("{n} recording", "{n} recordings", count)


def size_label(num_bytes: int) -> str:
    """ "350 MB", "1.2 GB", as on the phone: below ten with one decimal."""
    megabytes = num_bytes / (1024 * 1024)
    value, gigabytes = (megabytes, False) if megabytes < 1024 else (megabytes / 1024, True)
    number = f"{value:.1f}" if value < 10 else str(round(value))
    if ui_language() == "ru":
        number = number.replace(".", ",")
    return _("{size} GB", size=number) if gigabytes else _("{size} MB", size=number)


_CACHE: dict[Path, tuple[tuple, RecordingCard]] = {}


def _gist(summary: str) -> str:
    """The gist from summary.md (as the phone's parseSummaryFile, fixture summary-v1): the first
    paragraph outside "- " points and their continuations."""
    body: list[str] = []
    point = False
    for line in summary.splitlines():
        if line.startswith("- "):
            point = True
        elif point and line.strip():
            continue  # continuation of a point
        else:
            point = False
            body.append(line)
    for block in _PARAGRAPH.split("\n".join(body)):
        block = block.strip()
        if block and not block.startswith(("- ", "* ")):
            return re.sub(r"[,;:]…$", "…", textwrap.shorten(block, 280, placeholder="…"))
    return ""


def _languages(meta: str) -> list[str]:
    """Track languages of the last processing that reached the end (stopped, failed and summary
    runs do not count); empty when the file is missing or malformed."""
    try:
        runs = json.loads(meta)["runs"]
        done = [r for r in runs if r.get("status") == "ok" and r.get("kind") != "summary"]
        tracks = done[-1]["tracks"].values()
        return sorted({t["lang"] for t in tracks if isinstance(t.get("lang"), str)})
    except (ValueError, LookupError, TypeError, AttributeError):
        return []


def _card(folder: Path, files: dict[str, os.stat_result]) -> RecordingCard | None:
    audio = [st for name, st in files.items() if Path(name).suffix.lower() in AUDIO_EXT]
    has_text = "transcript.txt" in files
    has_summary = "summary.md" in files
    if not audio and not has_text and not has_summary:
        return None  # not a recording
    seconds = max(
        (
            wav_seconds(folder / name, st.st_size)
            for name, st in files.items()
            if name.lower().endswith(".wav")
        ),
        default=0.0,
    )
    when, time_known = recording_start(folder, files)

    def read(name: str) -> str:
        return (
            (folder / name).read_text(encoding="utf-8", errors="replace") if name in files else ""
        )

    raw_meta = read(META_FILE)
    try:
        data = json.loads(raw_meta) if raw_meta else {}
        if not isinstance(data, dict):
            data = {}
    except ValueError:
        data = {}
    shown = display_title(folder.name)
    sizes = {name: st.st_size for name, st in files.items()}
    id = data.get("id")  # cleanup_audio compares it with the list snapshot
    texts: dict[FileName, Stamp] = {}
    try:
        for name in sorted(TEXTS):
            if name in files:
                texts[name] = read_file(folder / name)[1]
    except (OSError, ValueError, LibraryError):
        texts = {}  # unreadable, so not confirmed by sync either

    return RecordingCard(
        path=folder,
        id=id if isinstance(id, str) else str(folder),
        title=shown.title,
        part=shown.part,
        started=when,
        time_known=time_known,
        seconds=seconds,
        gist=_gist(read("summary.md")),
        has_text=has_text,
        has_summary=has_summary,
        has_audio=bool(audio),
        has_sound=meta.has_sound(sizes),
        remarks=meta.remarks_of(data),
        state=folder_state(folder, files, data),
        languages=_languages(raw_meta),
        finished=meta.processed(data),
        processing=PROCESSING_MARK in files,
        texts=texts,
    )


def cleanable(card: RecordingCard, checkpoint: Checkpoint, connection: str | None) -> bool:
    """The one rule of "Delete audio of processed recordings" (fixture audio-cleanup-v1): the
    recording has audio, is processed (meta.record_state), its last processing, a stopped one
    too, reached the end (meta.processed) and no new one runs. With a connected server the texts
    must also be confirmed by the last sync with it: without audio the recording lives on them."""
    return _waiting_for_server(card) and (
        not connection or texts_confirmed(checkpoint, connection, card.id, card.texts)
    )


def library(
    root: Path, *, exclude: Path | None = None, connection: str | None = None
) -> LibraryView:
    """All cards and empty sections for the UI; errors are skipped, so this is NOT a sync
    manifest. connection is the connected sync server (None: none); it decides cleanable."""
    with FILES_LOCK:
        return _library(root, exclude=exclude, connection=connection)


def _library(root: Path, *, exclude: Path | None, connection: str | None) -> LibraryView:
    global _CACHE
    result = LibraryView()
    checkpoint = current()
    cache: dict[Path, tuple[tuple, RecordingCard]] = {}
    for found in walk(root, known=set(checkpoint.sections)):
        folder, files = found.path, found.files
        if folder == exclude:
            continue
        if found.kind is Kind.SECTION:
            result.sections.append(found.relative)
            continue
        try:
            stamp = tuple(
                sorted(
                    (name, st.st_mtime_ns, st.st_ctime_ns, st.st_size) for name, st in files.items()
                )
            )
            hit = _CACHE.get(folder)
            card = hit[1] if hit is not None and hit[0] == stamp else _card(folder, files)
            if card is None:
                continue
            id = ensure_identity(folder)
            card = card.model_copy(update={"id": id, "section": found.section})
        except (OSError, ValueError, LibraryError):
            continue
        cache[folder] = (stamp, card)
    _separate_copies(cache)
    _CACHE = cache
    result.cards = sorted(
        (
            card.model_copy(update={"cleanable": cleanable(card, checkpoint, connection)})
            for _stamp, card in cache.values()
        ),
        key=lambda c: c.started.timestamp(),
        reverse=True,
    )
    return result


def with_folders(cards: list[RecordingCard], folders: list[Path]) -> list[RecordingCard]:
    """The list plus queued folders outside the library ("Process recording…" outside the root):
    such a card is read from the folder itself, without a section, in its place by date."""
    known = {c.path.absolute() for c in cards}
    extra: list[RecordingCard] = []
    for folder in folders:
        if folder.absolute() in known:
            continue
        try:
            with FILES_LOCK:
                files = {e.name: e.stat() for e in os.scandir(folder) if e.is_file()}
                card = _card(folder, files)
        except (OSError, ValueError, LibraryError):
            continue
        if card is not None:
            extra.append(card)
            known.add(folder.absolute())
    if not extra:
        return cards
    return sorted([*cards, *extra], key=lambda c: c.started.timestamp(), reverse=True)


def _separate_copies(cache: dict[Path, tuple[tuple, RecordingCard]]) -> None:
    """A Finder copy with the same id is a new recording; the older folder keeps the id."""
    groups: dict[str, list[Path]] = defaultdict(list)
    for folder, (_stamp, card) in cache.items():
        groups[card.id].append(folder)
    for group in groups.values():
        # copying does not keep ctime: the copy's is newer than the original's
        group.sort(key=lambda f: (min((s[2] for s in cache[f][0]), default=0), str(f)))
        for folder in group[1:]:
            stamp, card = cache[folder]
            try:
                cache[folder] = (stamp, card.model_copy(update={"id": renew_identity(folder)}))
            except (OSError, ValueError, LibraryError):
                continue  # duplicate ids stay: an action will not pick one at random


def record_sections(root: Path) -> list[str]:
    """Root sections by the list's rule, where a new recording can go; recordings are not read."""
    with FILES_LOCK:
        known = known_sections()
        return [f.relative for f in walk(root, known=known, depth=1) if f.kind is Kind.SECTION]


def move_record(root: Path, folder: Path, section: str, *, create: bool = False) -> Path:
    """ "Move to section": the recording folder goes to <root>/<section>/ ("" is the root) under
    a free name (naming.free_name: "… (2)"). Nothing inside changes: meta and its id move along,
    so sync sees a move by id, as from Finder. The section must exist (record_sections); create
    is "New section…": the title is cleaned like a folder name (naming.folder_title), a section
    differing only in case is that section, otherwise it is created. Already there: the folder
    stays. An empty title raises ValueError; a missing section or recording, or a name taken
    during the move, OSError; another process of this computer processing it (CLI, MCP: the
    recording_client.alone lock) MediaError. The recording stays in place in every such case."""
    from ..live.recording_client import alone

    root = root.absolute()
    with FILES_LOCK:
        sections = record_sections(root)
        if create:
            name = folder_title(section)
            if not name:
                raise ValueError("empty section title")
            section = next((s for s in sections if s.casefold() == name.casefold()), name)
        elif section and section not in sections:
            raise FileNotFoundError(f"no section {section!r}")
        if not folder.is_dir() or not folder.resolve().is_relative_to(root.resolve()):
            raise FileNotFoundError(f"no recording {folder}")
        parent = root / section if section else root
        new = create and section not in sections
        if not new and folder.parent.resolve() == parent.resolve():
            return folder
        # lock before creating the section: a busy recording neither moves nor makes a section
        with alone(folder):
            if new:
                parent = free_name(parent)  # the name is taken by a recording: "Section (2)"
                parent.mkdir(parents=True)
            target = free_name(parent / folder.name)
            folder.rename(target)  # taken meanwhile: OSError, the folder stays
        return target


def find_recording(root: Path, id: str) -> Path | None:
    matches = [c.path for c in library(root).cards if c.id == id]
    return matches[0] if len(matches) == 1 else None


def cleanup_plan(
    root: Path, *, connection: str | None, busy: Callable[[], set[str]]
) -> CleanupPlan:
    """How many recordings and bytes of audio cleanup_audio would remove now, for the question
    before deleting; with nothing to remove, reason says why."""
    plan = CleanupPlan()
    root = root.absolute()
    cards = library(root, connection=connection).cards
    audio = [c for c in cards if c.has_audio]
    if connection and any(_waiting_for_server(c) for c in cards):
        plan.reason = _(AFTER_SYNC)
    elif not audio:
        plan.reason = _no_audio()
    elif all(c.processing or c.state not in _DONE for c in audio):
        plan.reason = _(ONLY_UNPROCESSED)
    elif all(c.state is RecordState.NO_SPEECH for c in audio):
        plan.reason = _(ONLY_SILENT)
    else:  # mixed reasons, or text without history: a general reason never contradicts a card
        plan.reason = _(NOTHING)
    for card in cards:
        if not card.cleanable:
            continue
        try:
            relative = card.path.relative_to(root).as_posix()
            if {card.id, str(card.path), relative} & busy():
                continue
            sizes = [
                f.stat().st_size
                for f in card.path.iterdir()
                if f.suffix.lower() in AUDIO_EXT and f.is_file()
            ]
        except (OSError, ValueError):
            continue
        plan.records += 1
        plan.bytes += sum(sizes)
    return plan


def cleanup_audio(
    root: Path,
    *,
    connection: str | None,
    busy: Callable[[], set[str]],
    trash: Callable[[Path], None],
) -> CleanupResult:
    """ "Delete audio of processed recordings": the audio of every cleanable() recording goes to
    the Trash, the texts stay. Each folder is read again by the same rule first: it may have been
    replaced in Finder or changed since the snapshot."""
    result = CleanupResult()
    root = root.absolute()
    checkpoint = current()
    for card in library(root, connection=connection).cards:
        if not card.cleanable:
            continue
        try:
            with FILES_LOCK:
                relative = card.path.relative_to(root).as_posix()
                if {card.id, str(card.path), relative} & busy():
                    continue
                found = read_folder(root, relative, known=set(checkpoint.sections))
                fresh = _card(found.path, found.files) if found.kind is Kind.RECORD else None
                if (
                    fresh is None
                    or fresh.id != card.id
                    or not cleanable(fresh, checkpoint, connection)
                ):
                    continue
                audio = [n for n in sorted(found.files) if Path(n).suffix.lower() in AUDIO_EXT]
                for name in audio:
                    trash(found.path / name)
                result.removed += bool(audio)
        except (OSError, ValueError, LibraryError):
            name = card.title or card.path.name
            result.errors.append(_("Could not delete the audio of “{name}”", name=name))
    return result
