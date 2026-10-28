"""The last sync only: no texts, no deleted ids, no operation log."""

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from ..connection import parse_connection
from ..library.layout import TEXTS
from ..library.models import FileName, Record, Stamp


class Checkpoint(BaseModel):
    server: str = ""
    records: dict[str, Record] = Field(default_factory=dict)
    sections: list[str] = Field(default_factory=list)


def checkpoint_path() -> Path:
    """This computer's sync.json in the settings folder: written by sync and the installer, read
    by the feed, the sections for a new recording and audio cleanup."""
    from ..config import config_dir

    return config_dir() / "sync.json"


def current() -> Checkpoint:
    """This computer's sync.json for the feed and audio cleanup; unreadable means no sync yet."""
    try:
        return load(checkpoint_path())
    except OSError:
        return Checkpoint()


def known_sections() -> set[str]:
    """Sections of the last sync: without meta they stay sections (layout.kind)."""
    return set(current().sections)


def server_key(connection: str) -> str:
    """Whose sync this is: the server address from the connection string, the same for sync
    (LibraryClient) and audio cleanup. ValueError for an invalid string."""
    return parse_connection(connection).url


def texts_confirmed(
    state: Checkpoint, connection: str | None, id: str, files: Mapping[FileName, Stamp]
) -> bool:
    """The texts of recording id (files: their current stamps) match the last sync with the
    server connection, so they stay there without the audio. No server selected, or the last sync
    was with another one: not confirmed."""
    try:
        server = server_key(connection) if connection else None
    except ValueError:
        return False
    known = state.records.get(id)
    if server is None or state.server != server or known is None:
        return False

    def sha(stamps: Mapping[FileName, Stamp], name: FileName) -> str | None:
        return stamps[name].sha256 if name in stamps else None

    return bool(TEXTS & files.keys()) and all(
        sha(files, name) == sha(known.files, name) for name in TEXTS
    )


def load(path: Path) -> Checkpoint:
    try:
        return Checkpoint.model_validate_json(path.read_bytes())
    except (FileNotFoundError, ValidationError, ValueError):
        # No history to compare with means a full sync, never an order to delete files.
        return Checkpoint()


def save(path: Path, state: Checkpoint) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(state.model_dump(mode="json"), file, ensure_ascii=False)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def initialize_library(root: Path, checkpoint: Path) -> None:
    """Only an explicit connection of a new client creates the folder before the first sync.

    Any checkpoint, even a broken file or link, means the library existed before. Its
    disappearance is up to a regular sync, not to reopening the settings.
    """
    from .scan import FILES_LOCK

    with FILES_LOCK:
        try:
            checkpoint.lstat()
        except FileNotFoundError:
            root.mkdir(parents=True, exist_ok=True)
