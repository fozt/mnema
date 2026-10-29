"""Library files on disk, shared by the server (LibraryFiles) and the client sync (sync.scan):
a recording path inside the root, stable reads and replacement of a file, a folder listing and a
whole record."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import tempfile
from pathlib import Path

from ..i18n import _
from .layout import hidden
from .models import LEGACY_SUMMARY, FileName, Record, Stamp, recording_id

logger = logging.getLogger(__name__)


class LibraryError(Exception):
    """status is the server response: 409 means sync again, 503 means the library cannot be read
    in full."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def inside(path: Path, real: Path) -> Path:
    """real is the root with links resolved: a link pointing outside fails, an inner one passes."""
    if not path.resolve().is_relative_to(real):
        raise LibraryError(409, _("The path leads outside the library"))
    return path


def safe_path(root: Path, value: str, real: Path | None = None, *, section: bool = False) -> Path:
    """ "Section/Recording" or "Recording" (section=True: a section only) under root, never
    outside it."""
    parts = value.split("/")
    if not 1 <= len(parts) <= (1 if section else 2) or any(
        p == "" or hidden(p) or "\0" in p for p in parts
    ):
        raise LibraryError(409, _("Invalid path in the library"))
    return inside(root.joinpath(*parts), real or root.resolve())


def read_file(path: Path) -> tuple[bytes, Stamp]:
    """File bytes and their stamp; a file that changed while being read is a 409."""
    # A FIFO or device file put there by hand must not hang the full walk.
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as file:
        before = os.fstat(file.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise LibraryError(503, _("Found a special file instead of a text file"))
        data = file.read()
        after = os.fstat(file.fileno())
    if (before.st_mtime_ns, before.st_ctime_ns, before.st_size) != (
        after.st_mtime_ns,
        after.st_ctime_ns,
        after.st_size,
    ) or path.stat() != after:
        raise LibraryError(409, _("The file changed while being read; sync again"))
    data.decode("utf-8")
    return data, Stamp(sha256=hashlib.sha256(data).hexdigest(), mtime_ns=after.st_mtime_ns)


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def replace_file(path: Path, data: bytes, mtime_ns: int) -> None:
    """Atomic replacement through a temporary file alongside: same mode, the given mtime.
    An inner link stays a link: the file it points to is replaced."""
    data.decode("utf-8")
    path = path.resolve()
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    fd, name = tempfile.mkstemp(prefix=".mnema-sync-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fchmod(file.fileno(), mode)
            os.utime(temporary, ns=(mtime_ns, mtime_ns))
            os.fsync(file.fileno())
        os.replace(temporary, path)
        fsync_dir(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def listing(folder: Path) -> tuple[set[str], list[Path]]:
    """File names and visible folders; hidden entries other than meta are skipped. A stat failure
    is an error: a skipped folder would look like a deleted recording."""
    files: set[str] = set()
    folders: list[Path] = []
    for path in sorted(folder.iterdir()):
        if hidden(path.name) and path.name != FileName.META:
            continue
        if stat.S_ISDIR(path.stat().st_mode):
            folders.append(path)
        else:
            files.add(path.name)
    return files, folders


def migrate_summary(folder: Path, files: set[str], *, strict: bool = False) -> set[str]:
    """summary.md of earlier versions becomes summary.txt the first time the recording is read;
    files are the folder's file names, returned as they are after the rename. An existing
    summary.txt wins and the old file is left alone. strict (sync, the server library): a failed
    rename raises LibraryError 503, since the recording listed without its summary would look
    as if the summary was deleted."""
    target = folder / FileName.SUMMARY
    if LEGACY_SUMMARY not in files or FileName.SUMMARY in files or target.is_symlink():
        return files
    try:
        (folder / LEGACY_SUMMARY).rename(target)
        fsync_dir(folder)
    except OSError as error:
        logger.warning("Could not rename summary.md in %s: %s", folder, error)
        if strict:
            raise LibraryError(503, _("Couldn't rename summary.md to summary.txt")) from error
        return files
    return files - {LEGACY_SUMMARY} | {FileName.SUMMARY}


def nested(folders: list[Path], real: Path) -> Path | None:
    """A subfolder with meta: a recording inside a recording or in a section subfolder; links
    pointing outside do not count."""
    return next(
        (f for f in folders if f.resolve().is_relative_to(real) and (f / FileName.META).exists()),
        None,
    )


def meta_id(data: bytes) -> str:
    """Recording id from the meta text; ValueError when it is missing or not a UUID."""
    value = json.loads(data)
    if not isinstance(value, dict) or not isinstance(value.get("id"), str):
        raise ValueError("meta has no recording id")
    return recording_id(value["id"])


def read_record(root: Path, relative: str, real: Path | None = None) -> Record:
    """Meta with its id and the stamps of the recording texts; tag is set by the server. No meta:
    FileNotFoundError, no id: ValueError, a broken link instead of a text: an error, not a deleted
    file."""
    real = real or root.resolve()
    folder = safe_path(root, relative, real)
    raw, current = read_file(inside(folder / FileName.META, real))
    id = meta_id(raw)
    files = {FileName.META: current}
    for name in (FileName.TRANSCRIPT, FileName.SUMMARY):
        path = inside(folder / name, real)
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        files[name] = read_file(path)[1]
    return Record(id=id, path=relative, files=files, tag="")
