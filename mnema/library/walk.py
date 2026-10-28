"""Library walk for display and actions: the feed, unprocessed recordings, sections for a new
recording, audio cleanup and the MCP list. Section or recording is layout.kind with the sections of
the last sync (known); folders outside the root, recordings with links pointing outside and
unreadable folders are skipped. The full walk for sync, where a skip is an error rather than a
deletion, is sync.scan and LibraryFiles."""

from __future__ import annotations

import os
from collections.abc import Collection
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from .disk import LibraryError, inside, safe_path
from .layout import Kind, hidden, kind


class Folder(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    path: Path  # under the root as given: the root may be a link
    relative: str  # "Section/Recording" or "Recording"
    section: str  # the recording's section; "" for a root folder
    kind: Kind  # RECORD or SECTION
    files: dict[str, os.stat_result]  # regular files, hidden ones too
    folders: list[str]  # visible folders


def _entries(folder: Path) -> tuple[dict[str, os.stat_result], list[str]]:
    files: dict[str, os.stat_result] = {}
    folders: list[str] = []
    with os.scandir(folder) as entries:
        for e in entries:
            if e.is_dir():
                if not hidden(e.name):
                    folders.append(e.name)
            elif e.is_file():
                files[e.name] = e.stat()
    return files, sorted(folders)


def _read(folder: Path, relative: str, real: Path, known: Collection[str]) -> Folder:
    section, _, name = relative.rpartition("/")
    files, folders = _entries(folder)
    level = 2 if section else 1
    what = kind(level, files, folders, known=level == 1 and name in known)
    if what is Kind.RECORD:
        for file in files:  # a file linking outside: the recording is neither shown nor touched
            inside(folder / file, real)
    return Folder(
        path=folder, relative=relative, section=section, kind=what, files=files, folders=folders
    )


def read_folder(root: Path, relative: str, *, known: Collection[str] = ()) -> Folder:
    """One folder re-read by the walk rule before acting on a recording from a snapshot: Finder may
    have replaced it. A path outside the root or a link pointing outside raises LibraryError."""
    root = root.absolute()
    real = root.resolve()
    return _read(safe_path(root, relative, real), relative, real, known)


def walk(root: Path, *, known: Collection[str] = (), depth: int = 2) -> list[Folder]:
    """Sections and recordings; the root is not created. known: sections of the last sync
    (sync.json), so recording files in an empty section do not change it. depth=1: root folders
    only, without the recordings inside sections."""
    root = root.absolute()
    try:
        real = root.resolve()  # a root link (~/mnema on another disk) is the same library
        queue = [(d.name, "") for d in sorted(root.iterdir()) if not hidden(d.name) and d.is_dir()]
    except OSError:
        return []
    found: list[Folder] = []
    seen: set[Path] = set()
    for name, section in queue:
        relative = f"{section}/{name}" if section else name
        try:
            folder = safe_path(root, relative, real)
            resolved = folder.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            entry = _read(folder, relative, real, known)
        except (OSError, LibraryError):
            continue
        if entry.kind is Kind.OTHER:
            continue
        if entry.kind is Kind.SECTION and depth > 1:
            queue.extend((child, name) for child in entry.folders)
        found.append(entry)
    return found


def recordings(root: Path, *, known: Collection[str] = ()) -> list[dict]:
    """Recordings for MCP: path under the root, the folder and its visible files."""
    return [
        {
            "name": f.relative,
            "path": str(f.path),
            "files": sorted(name for name in f.files if not hidden(name)),
        }
        for f in walk(root, known=known)
        if f.kind is Kind.RECORD
    ]
