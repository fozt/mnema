"""Full walk of the client files, independent of the UI feed and the server storage."""

from __future__ import annotations

import json
import logging
import os
import stat
import threading
import time
from collections.abc import Collection
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, Field

from ..i18n import _
from ..library.disk import (
    LibraryError,
    inside,
    listing,
    migrate_summary,
    nested,
    read_file,
    read_record,
    replace_file,
    safe_path,
)
from ..library.layout import TEXTS, Kind, hidden, kind
from ..library.models import FileName, Manifest, Stamp, recording_id
from ..live.naming import started_iso

logger = logging.getLogger(__name__)
FILES_LOCK = threading.RLock()


class SyncError(LibraryError):
    """A sync error for the user; file contents and secrets never get into the text. Library read
    errors (LibraryError) are the same as the server's."""

    def __init__(self, message: str) -> None:
        super().__init__(409, message)


class Scan(BaseModel):
    manifest: Manifest = Field(default_factory=Manifest)
    complete: bool = True
    blocked_paths: list[str] = Field(default_factory=list)
    missing_root: bool = False  # a missing path, as opposed to a read error; not saved
    # Recordings inside a recording and in a section subfolder: the scan is incomplete, and the
    # user is told where each one is. readable: no other folder failed to be read.
    nested: list[str] = Field(default_factory=list)
    deep: list[str] = Field(default_factory=list)
    readable: bool = True


def stamp(path: Path) -> Stamp | None:
    try:
        return read_file(path)[1]
    except FileNotFoundError:
        return None


def write_file(path: Path, data: bytes, mtime_ns: int, expected: Stamp | None) -> None:
    """Checks the file again after waiting on the network and replaces only that file."""
    with FILES_LOCK:
        if stamp(path) != expected:
            raise SyncError(_("The local file has already changed; sync will run again"))
        replace_file(path, data, mtime_ns)


def ensure_identity(folder: Path) -> str:
    with FILES_LOCK:
        path = folder / FileName.META
        expected = stamp(path)
        data = json.loads(read_file(path)[0]) if expected else {}
        if not isinstance(data, dict):
            raise SyncError(_("Could not read the recording meta"))
        before = dict(data)
        if "id" in data:
            recording_id(data["id"])
        else:
            data["id"] = str(uuid4())
        if "started" not in data and (started := started_iso(folder)) is not None:
            data["started"] = started
        if data != before:
            write_file(
                path,
                (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode(),
                time.time_ns(),
                expected,
            )
        return data["id"]


def renew_identity(folder: Path) -> str:
    """A copied folder becomes a new recording: a new id, the rest of meta as is."""
    with FILES_LOCK:
        path = folder / FileName.META
        raw, expected = read_file(path)
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise SyncError(_("Could not read the recording meta"))
        data["id"] = str(uuid4())
        write_file(
            path,
            (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode(),
            time.time_ns(),
            expected,
        )
        return data["id"]


def created_ns(folder: Path) -> int:
    """When the folder appeared: a copy does not keep file ctimes, so the original's are older."""
    with os.scandir(folder) as entries:
        times = [
            e.stat(follow_symlinks=False).st_ctime_ns
            for e in entries
            if e.is_file(follow_symlinks=False)
        ]
    return min(times, default=folder.stat().st_ctime_ns)


def scan(
    root: Path,
    busy: set[str],
    *,
    sections: Collection[str] = (),
    records: Collection[str] = (),
    real: Path | None = None,
) -> Scan:
    """The root is not created; a known root deletion is decided by the engine from the checkpoint.

    sections: sections of the last sync; without meta they stay sections (layout.kind).
    records: recording paths of the last sync; such a folder without recording files is not a
    deletion.
    real: the root resolved when the sync started; a different one means the root was swapped for
    a link.
    """
    result = Scan()
    root = root.absolute()
    base = real or root.resolve()
    seen: set[Path] = set()

    def inner(folders: list[Path]) -> str | None:
        """The walk does not see a recording in a subfolder; without this, its id looks deleted."""
        found = nested(folders, base)
        return found.relative_to(root).as_posix() if found else None

    def visit(folder: Path, depth: int) -> None:
        relative = folder.relative_to(root).as_posix()
        try:
            resolved = safe_path(root, relative, base).resolve()
            if relative in busy or str(folder) in busy:
                result.blocked_paths.append(relative)
                return
            if resolved in seen:
                raise SyncError(_("The same folder appears twice"))
            seen.add(resolved)
            files, folders = listing(folder)
            found = kind(
                depth, files, [p.name for p in folders], known=depth == 1 and relative in sections
            )
            if found is Kind.SECTION:
                result.manifest.sections.append(relative)
                for child in folders:
                    visit(child, 2)
                return
            if found is Kind.OTHER:
                if relative in records:
                    raise SyncError(_("The recording folder has none of its files"))
                if moved := inner(folders):
                    result.deep.append(moved)
                    result.complete = False
                    result.blocked_paths.append(relative)
                return  # an empty or unrelated folder in a section: no recording, no reason to wait
            if FileName.META in files:
                raw = read_file(inside(safe_path(root, relative, base) / FileName.META, base))[0]
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise SyncError(_("Cannot read the recording meta"))
                if data.get("id") in busy:
                    result.blocked_paths.append(relative)
                    return
            if child := inner(folders):
                result.nested.append(child)
                result.complete = False
                result.blocked_paths.append(relative)
                return
            files = migrate_summary(folder, files)
            if files & TEXTS or FileName.META in files:
                # Even a meta-only recording may be a download in progress; it must not be lost.
                for name in FileName:
                    inside(folder / name, base)
                ensure_identity(folder)
                result.manifest.records.append(read_record(root, relative, base))
            else:
                result.blocked_paths.append(relative)  # audio without a text is not published
        except (OSError, ValueError, KeyError, TypeError, LibraryError) as error:
            result.complete = result.readable = False
            result.blocked_paths.append(relative)
            logger.warning("Sync postponed: could not read the folder %s: %s", relative, error)

    with FILES_LOCK:
        try:
            root.lstat()  # a broken link exists; it is not a vanished library
        except FileNotFoundError:
            result.complete, result.missing_root = False, True
            return result
        except OSError:
            result.complete = result.readable = False
            result.blocked_paths.append(".")
            return result
        try:
            if root.resolve() != base:
                raise SyncError(_("The Mnema folder was replaced with a link"))
            for folder in sorted(root.iterdir()):
                if not hidden(folder.name) and stat.S_ISDIR(folder.stat().st_mode):
                    visit(folder, 1)
        except (OSError, LibraryError):
            result.complete = result.readable = False
            result.blocked_paths.append(".")
    return result
