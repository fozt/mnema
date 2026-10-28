"""File library without an index or database: the list is always read from plain folders."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import shutil
import stat
import threading
from collections.abc import Iterator
from pathlib import Path

from ..i18n import _
from ..live.naming import free_name
from .disk import (
    LibraryError,
    fsync_dir,
    inside,
    listing,
    meta_id,
    migrate_summary,
    nested,
    read_file,
    read_record,
    replace_file,
    safe_path,
)
from .layout import TEXTS, Kind, hidden, kind
from .models import CreateRecord, FileBody, FileName, Manifest, Record, Stamp

logger = logging.getLogger(__name__)


def _require_identity(data: bytes, id: str) -> None:
    try:
        actual = meta_id(data)
    except ValueError as exc:
        raise LibraryError(409, _("Meta must contain the recording id")) from exc
    if actual != id:
        raise LibraryError(409, _("The id in meta does not match the recording"))


class LibraryFiles:
    def __init__(self, root: Path) -> None:
        # The installer creates the root: a missing folder (an unmounted disk) is not an empty
        # library, or clients would move every recording to the Trash.
        self.root = root.resolve()
        # The library is small: one lock also serializes picking a free name.
        # Checking the previous hash and changing the recording always happen under one lock.
        self._lock = threading.RLock()

    @contextlib.contextmanager
    def _guard(self) -> Iterator[None]:
        with self._lock:
            try:
                if not self.root.is_dir():
                    raise LibraryError(503, _("The library is unavailable"))
                yield
            except LibraryError:
                raise
            except (OSError, ValueError, RuntimeError) as exc:
                raise LibraryError(503, _("Could not read or save the library")) from exc

    def _safe(self, path: Path) -> Path:
        return inside(path, self.root)

    def _path(self, value: str, *, section: bool = False) -> Path:
        return safe_path(self.root, value, self.root, section=section)

    def _read(self, path: Path) -> tuple[bytes, Stamp]:
        return read_file(self._safe(path))

    def _record(self, path: Path) -> Record:
        record = read_record(self.root, path.relative_to(self.root).as_posix(), self.root)
        tag = hashlib.sha256(
            json.dumps(
                [record.path, {k: v.model_dump() for k, v in record.files.items()}],
                sort_keys=True,
            ).encode()
        ).hexdigest()
        return record.model_copy(update={"tag": tag})

    def _scan(self, skipped: list[str] | None = None) -> Manifest:
        """The full list; unrelated folders are skipped, an unreadable recording fails the list."""
        self._safe(self.root)
        result = Manifest()
        ids: set[str] = set()
        seen: set[Path] = set()

        def visit(path: Path, depth: int) -> None:
            relative = path.relative_to(self.root).as_posix()
            try:
                resolved = self._safe(path).resolve()
                if resolved in seen:
                    raise LibraryError(503, _("The same folder appears twice in the library"))
                seen.add(resolved)
                files, folders = listing(path)
                section = kind(depth, files, [p.name for p in folders]) is Kind.SECTION
                if section:
                    result.sections.append(path.name)
                elif FileName.META in files:
                    if nested(folders, self.root):
                        raise LibraryError(
                            503, _("There is another recording inside the recording")
                        )
                    migrate_summary(path, files)
                    record = self._record(path)
                    if record.id in ids:
                        raise LibraryError(503, _("Two recordings in the library have the same id"))
                    ids.add(record.id)
                    result.records.append(record)
                elif files & TEXTS:
                    # Without meta there is no id: skipping it would look like a deletion.
                    raise LibraryError(503, _("The recording has no meta"))
                elif skipped is not None:
                    skipped.append(relative)  # an empty or unrelated folder
            except LibraryError as exc:
                raise LibraryError(
                    exc.status, _("{error}: “{path}”", error=exc, path=relative)
                ) from exc
            except (OSError, ValueError) as exc:
                raise LibraryError(503, _("Could not read “{path}”", path=relative)) from exc
            for child in folders if section else []:
                visit(child, 2)

        for path in sorted(self.root.iterdir()):
            if hidden(path.name):
                continue
            self._safe(path)
            if stat.S_ISDIR(path.stat().st_mode):
                visit(path, 1)
        return result

    def manifest(self) -> Manifest:
        skipped: list[str] = []
        with self._guard():
            try:
                result = self._scan(skipped)
            except LibraryError as exc:
                logger.warning("Library list is incomplete: %s", exc)
                raise LibraryError(
                    503, _("Could not get the full library list: {error}", error=exc)
                ) from exc
        for path in skipped:
            logger.info("Skipped an unrelated folder in the library: %s", path)
        return result

    def _find(self, id: str) -> Record:
        for record in self._scan().records:
            if record.id == id:
                return record
        raise LibraryError(404, _("Recording not found"))

    def _destination(self, path: str) -> Path:
        dest = self._path(path)
        if dest.parent != self.root:
            parent = dest.parent
            if (parent / FileName.META).exists():
                raise LibraryError(409, _("A recording cannot go inside another recording"))
            parent.mkdir(exist_ok=True)
            fsync_dir(self.root)
        return self._safe(free_name(dest))

    def _write(self, path: Path, data: bytes, mtime_ns: int) -> Stamp:
        replace_file(self._safe(path), data, mtime_ns)  # _safe blocks paths outside
        return self._read(path)[1]

    def create(self, id: str, path: str, files: dict[FileName, FileBody]) -> Record:
        with self._guard():
            request = CreateRecord(id=id, path=path, files=files)
            if any(r.id == id for r in self._scan().records):
                raise LibraryError(409, _("A recording with this id already exists"))
            _require_identity(request.files[FileName.META].text.encode(), id)
            dest = self._destination(path)
            dest.mkdir()
            try:
                fsync_dir(dest.parent)
                # Identity first: even without texts the recording is found and completed by PUT.
                for name in (FileName.META, FileName.TRANSCRIPT, FileName.SUMMARY):
                    if body := files.get(name):
                        self._write(dest / name, body.text.encode(), body.mtime_ns)
            finally:
                # Only an empty folder before meta is published; saved data stays.
                with contextlib.suppress(OSError):
                    dest.rmdir()
            return self._record(dest)

    def read(self, id: str, name: FileName) -> tuple[bytes, Stamp]:
        with self._guard():
            record = self._find(id)
            if name not in record.files:
                raise LibraryError(404, _("File not found"))
            return self._read(self._path(record.path) / name)

    def write(
        self, id: str, name: FileName, data: bytes, mtime_ns: int, expected: str | None
    ) -> Stamp:
        with self._guard():
            record = self._find(id)
            stamp = record.files.get(name)
            if (stamp.sha256 if stamp else None) != expected:
                raise LibraryError(409, _("The file has already changed; sync again"))
            if name == FileName.META:
                _require_identity(data, id)
            return self._write(self._path(record.path) / name, data, mtime_ns)

    def remove_file(self, id: str, name: FileName, expected: str) -> None:
        with self._guard():
            if name == FileName.META:
                raise LibraryError(409, _("Meta is deleted only with the recording"))
            record = self._find(id)
            stamp = record.files.get(name)
            if stamp is None or stamp.sha256 != expected:
                raise LibraryError(409, _("The file has already changed; sync again"))
            path = self._safe(self._path(record.path) / name)
            path.unlink()
            fsync_dir(path.parent)

    def move(self, id: str, path: str, expected_path: str) -> Record:
        with self._guard():
            record = self._find(id)
            if record.path != expected_path:
                raise LibraryError(409, _("The folder has already moved; sync again"))
            if record.path == path:
                return record
            source = self._path(record.path)
            dest = self._destination(path)
            source.rename(dest)
            fsync_dir(source.parent)
            fsync_dir(dest.parent)
            return self._record(dest)

    def remove(self, id: str, expected_tag: str) -> None:
        with self._guard():
            try:
                record = self._find(id)
            except LibraryError as exc:
                if exc.status == 404:
                    return
                raise
            if record.tag != expected_tag:
                raise LibraryError(409, _("The recording has already changed; sync again"))
            path = self._path(record.path)
            # rmtree does not follow inner links; a root link is removed itself.
            if path.is_symlink():
                path.unlink()
            else:
                shutil.rmtree(path)
            fsync_dir(path.parent)

    def section(self, path: str, delete: bool = False) -> None:
        with self._guard():
            dest = self._path(path, section=True)
            if delete:
                if not dest.exists():
                    return
                if any(dest.iterdir()):
                    raise LibraryError(409, _("The section is not empty"))
                if dest.is_symlink():
                    dest.unlink()
                else:
                    dest.rmdir()
            else:
                if (dest / FileName.META).exists():
                    raise LibraryError(409, _("A recording already has this name"))
                dest.mkdir(exist_ok=True)
            fsync_dir(self.root)
