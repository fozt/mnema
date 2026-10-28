"""Temporary uploads: one consumer each, active files protected, forgotten ones swept by TTL."""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from pydantic import BaseModel

from ..i18n import _

MARKER = ".partial.json"  # next to a chunked upload until all its bytes have arrived
MAX_UPLOAD_SIZE = 20 * 1024**3
_UPLOAD_ID = re.compile(r"[0-9a-f]{32}")


class UploadBusy(Exception):
    pass


class UploadUnfinished(UploadBusy):
    pass


class OffsetConflict(Exception):
    """The chunk is refused: the node expects another offset or is taking another chunk."""

    def __init__(self, message: str, offset: int):
        super().__init__(message)
        self.offset = offset


class Part(BaseModel):
    """A chunked upload: how many bytes the node has; path once all have arrived."""

    id: str
    name: str
    size: int
    offset: int
    path: str | None = None


def target(folder: Path, raw: str) -> Path | None:
    """The upload's file in its folder: basename only; None when the name is not allowed."""
    name = Path(raw).name
    try:
        if name == MARKER or len(name.encode()) > 255:
            return None
        base, dest = folder.resolve(), (folder / name).resolve()
    except (OSError, ValueError):  # NUL or lone surrogates in the name
        return None
    if not dest.is_relative_to(base) or dest == base:
        return None
    return dest


class Uploads:
    def __init__(self, root: Path):
        self.root = root / "uploads"
        self._lock = threading.RLock()
        self._busy: set[Path] = set()

    def directory(self, path: str | Path) -> Path | None:
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(self.root):
            return None
        parts = resolved.relative_to(self.root).parts
        return self.root / parts[0] if len(parts) >= 2 else None

    def claim(self, path: str) -> None:
        """An upload is single-use: a second job cannot delete the first one's input."""
        folder = self.directory(path)
        if folder is None:
            return
        with self._lock:
            if (folder / MARKER).exists():
                part = self._part(folder)
                if part is None or part.path is None:
                    raise UploadUnfinished(_("The upload is not finished"))
            if folder in self._busy:
                raise UploadBusy(
                    _("The upload is already used by another job; upload the file again.")
                )
            if not Path(path).is_file():
                raise FileNotFoundError(
                    _("The uploaded file is already deleted; upload the file again.")
                )
            self._busy.add(folder)

    def writing(self, folder: Path) -> None:
        with self._lock:
            self._busy.add(folder)
            try:
                folder.mkdir(parents=True)
            except Exception:
                self._busy.discard(folder)
                raise

    def written(self, folder: Path, *, success: bool) -> None:
        with self._lock:
            try:
                if not success:
                    shutil.rmtree(folder, ignore_errors=True)
                else:
                    os.utime(folder, None)  # the TTL starts when a long upload ends
            finally:
                self._busy.discard(folder)

    def release(self, path: str, *, delete: bool = True) -> None:
        folder = self.directory(path)
        if folder is not None:
            with self._lock:
                if delete:
                    shutil.rmtree(folder, ignore_errors=True)
                self._busy.discard(folder)

    def sweep(self, *, ttl_days: int = 1) -> None:
        """Called on start and on a new upload; uploads being written or used by jobs stay."""
        cutoff = time.time() - ttl_days * 86400
        with self._lock:
            if not self.root.is_dir():
                return
            for folder in self.root.iterdir():
                try:
                    if (
                        folder not in self._busy
                        and not folder.is_symlink()
                        and folder.is_dir()
                        and folder.stat().st_mtime < cutoff
                    ):
                        shutil.rmtree(folder, ignore_errors=True)
                except OSError:
                    continue

    def start(self, name: str, size: int) -> Part:
        """A new chunked upload: an empty uploads/<id>/<name> and a marker with name and size."""
        self.sweep()
        folder = self.root / uuid.uuid4().hex
        dest = target(folder, name)
        if dest is None:
            raise ValueError("invalid upload name")
        with self._lock:
            folder.mkdir(parents=True)
            try:
                (folder / MARKER).write_text(json.dumps({"name": dest.name, "size": size}))
                dest.touch()
            except Exception:
                shutil.rmtree(folder, ignore_errors=True)
                raise
        return Part(id=folder.name, name=dest.name, size=size, offset=0)

    def part(self, upload_id: str) -> Part | None:
        folder = self._folder(upload_id)
        if folder is None:
            return None
        with self._lock:
            return self._part(folder)

    def begin_chunk(self, upload_id: str, offset: int) -> Part:
        """Holds the upload for one chunk; the offset is the file length on disk."""
        folder = self._folder(upload_id)
        with self._lock:
            part = self._part(folder) if folder is not None else None
            if folder is None or part is None:
                raise LookupError(upload_id)
            if folder in self._busy:
                raise OffsetConflict(_("The upload is busy with another request"), part.offset)
            if offset != part.offset:
                raise OffsetConflict(
                    _("The server expects the chunk at offset {offset}", offset=part.offset),
                    part.offset,
                )
            self._busy.add(folder)
            return part

    def end_chunk(self, upload_id: str) -> Part | None:
        folder = self.root / upload_id
        with self._lock:
            try:
                os.utime(folder, None)  # the TTL counts from the last chunk
            except OSError:
                pass
            self._busy.discard(folder)
            return self._part(folder)

    def _folder(self, upload_id: str) -> Path | None:
        return self.root / upload_id if _UPLOAD_ID.fullmatch(upload_id) else None

    def _part(self, folder: Path) -> Part | None:
        """State under the lock; with all bytes in and no chunk going, the marker is removed."""
        try:
            marker = json.loads((folder / MARKER).read_text())
        except FileNotFoundError:
            return self._finished(folder)
        except (OSError, ValueError):
            return None
        try:
            name, size = str(marker["name"]), int(marker["size"])
            file = folder / name
            offset = file.stat().st_size
        except (KeyError, TypeError, ValueError, OSError):
            return None
        if offset == size and folder not in self._busy:
            (folder / MARKER).unlink(missing_ok=True)
            return Part(id=folder.name, name=name, size=size, offset=offset, path=str(file))
        return Part(id=folder.name, name=name, size=size, offset=offset)

    @staticmethod
    def _finished(folder: Path) -> Part | None:
        try:
            files = [p for p in folder.iterdir() if p.is_file()]
            if len(files) != 1:
                return None
            size = files[0].stat().st_size
        except OSError:
            return None
        return Part(id=folder.name, name=files[0].name, size=size, offset=size, path=str(files[0]))
