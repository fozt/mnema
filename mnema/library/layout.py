"""Section or recording: one rule for the server, sync and the feed (fixture library-layout-v1)."""

from collections.abc import Iterable
from enum import StrEnum

from .models import FileName

# Files that mark a folder as a recording; unrelated files (memo.m4a, pdf) do not change the layout.
RECORD_FILES = frozenset(
    {"mic.wav", "system.wav", FileName.TRANSCRIPT, FileName.SUMMARY, FileName.META}
)
TEXTS = frozenset({FileName.TRANSCRIPT, FileName.SUMMARY})


class Kind(StrEnum):
    RECORD = "record"
    SECTION = "section"
    OTHER = "other"  # not part of the library: walks skip it and do not count as incomplete


def hidden(name: str) -> bool:
    """Hidden folders (.obsidian, .git, .Trash-*) are neither sections nor recordings."""
    return name.startswith(".")


def kind(depth: int, files: Iterable[str], folders: Iterable[str], *, known: bool = False) -> Kind:
    """files and folders are the folder's entry names; depth 1 is the library root.

    known: the folder was a section at the last sync (the client's sync.json), so recording files
    dropped into an empty section do not turn it into a recording. Meta wins: that is a moved
    recording.
    """
    names = set(files)
    if FileName.META in names:
        return Kind.RECORD
    if depth == 1 and (known or any(not hidden(name) for name in folders)):
        return Kind.SECTION  # files next to the section's recordings do not make it a recording
    if names & RECORD_FILES:
        return Kind.RECORD
    return Kind.SECTION if depth == 1 else Kind.OTHER
