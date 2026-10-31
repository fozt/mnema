"""Server users: each has own devices and own library; the owner keeps the server's old ones.

Users live in devices.db (`mnema/devices.py`); here are the name rules, the user of the current
context (a request, a job) and where a user's data lies on the server.
"""

from __future__ import annotations

import contextlib
import contextvars
import re
from collections.abc import Iterator
from pathlib import Path

from pydantic import BaseModel

from .core.base import MediaError
from .i18n import _

OWNER = "owner"
_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
_user: contextvars.ContextVar[str | None] = contextvars.ContextVar("mnema_user", default=None)
_strict = False


class Caller(BaseModel):
    """Who asks the server: the device (or the node token) and its user."""

    device: str
    user: str


def valid_name(raw: str) -> str:
    """A user name as stored: lowercase Latin letters, digits, - and _, up to 32 chars."""
    name = raw.lower()
    if name == OWNER or not _NAME.fullmatch(name):
        raise MediaError(_("User name: Latin letters, digits, - and _, up to 32, for example anna"))
    return name


def no_user(name: str) -> str:
    """The refusal for a user the server does not have (unknown or removed)."""
    from .node import server_command

    return _(
        "No user {name}. Add one: {command}",
        name=name,
        command=server_command(f"users add {name}"),
    )


@contextlib.contextmanager
def as_user(name: str | None) -> Iterator[None]:
    """The user of the current context (a server request, a job thread)."""
    token = _user.set(name)
    try:
        yield
    finally:
        _user.reset(token)


def current_user() -> str | None:
    """The user of this context; None outside the server (CLI, Mac, MCP)."""
    return _user.get()


def set_strict(on: bool) -> None:
    """The server mode: `serve` turns it on, and then every model use needs a user."""
    global _strict
    _strict = on


def strict() -> bool:
    return _strict


def user_library(root: Path, name: str) -> Path:
    """The library of a user of the server with folder `root`: the owner's is the old one."""
    return root / "library" if name == OWNER else root / "users" / name / "library"


def user_env_file(name: str) -> Path:
    """The file of the user's own model and keys, next to the server's shared .env."""
    from .config import config_dir

    return config_dir() / "users" / f"{name}.env"
