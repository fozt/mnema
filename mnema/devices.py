"""Node invitations and device tokens: the database keeps only sha256 of codes and tokens.

An invitation is single-use and short-lived; the client exchanges it for its own device token
(`POST /api/devices/claim`). Invitations are issued and devices revoked only on the node itself
(`mnema invite`, `mnema devices`, `mnema users`), straight in the database, without HTTP. Every
invitation and device belongs to a user (`mnema/users.py`); a database from before users is the
owner's.
"""

from __future__ import annotations

import hashlib
import math
import os
import secrets
import sqlite3
import time
import unicodedata
from contextlib import closing
from pathlib import Path

from pydantic import BaseModel

from .connection import format_invite, parse_connection
from .i18n import _
from .i18n.fmt_server import invite_until
from .users import OWNER, Caller, no_user, valid_name

INVITE_HOURS = 6.0
MAX_INVITE_HOURS = 72.0
DEFAULT_NAME = "Device"  # stored in the claiming client's language
_NAME_LIMIT = 40
_SEEN_EVERY_S = 60.0
_SCHEMA = """
CREATE TABLE IF NOT EXISTS invites (
    hash TEXT PRIMARY KEY, created REAL NOT NULL, expires REAL NOT NULL,
    used_at REAL, device_id TEXT
);
CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
    created REAL NOT NULL, last_seen REAL, revoked INTEGER NOT NULL DEFAULT 0
);
"""


_VERSION = 1


class User(BaseModel):
    name: str
    library: str  # the number of the user's library: sync tells libraries on one address apart
    created: float
    removed: bool = False


class Device(BaseModel):
    id: str
    name: str
    created: float
    last_seen: float | None = None
    revoked: bool = False
    user: str = OWNER


class OutdatedClient(Exception):
    """An invitation of a user other than the owner, claimed by an app that knows no library
    numbers: it would mix the libraries, so the invitation stays unused."""


def devices_db(root: Path) -> Path:
    """The device database of the node with folder `root` (serve --dir), next to jobs.db."""
    return root / ".mnema" / "devices.db"


def device_name(raw: str) -> str:
    """The name for the device list: one line without control characters, up to 40 chars."""
    kept = "".join(
        " " if c.isspace() else c
        for c in raw
        if c.isspace() or not unicodedata.category(c).startswith("C")
    )
    return " ".join(kept.split())[:_NAME_LIMIT].strip() or _(DEFAULT_NAME)


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8", "surrogatepass")).hexdigest()


def _version(con: sqlite3.Connection) -> int:
    return con.execute("PRAGMA user_version").fetchone()[0]


def _upgrade(con: sqlite3.Connection) -> None:
    """v0 → v1 inside the caller's transaction: users, and everything so far is the owner's."""
    for table in ("invites", "devices"):
        columns = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
        if "user" not in columns:
            con.execute(f"ALTER TABLE {table} ADD COLUMN user TEXT NOT NULL DEFAULT '{OWNER}'")
    con.execute(
        "CREATE TABLE IF NOT EXISTS users (name TEXT PRIMARY KEY, library TEXT NOT NULL UNIQUE, "
        "created REAL NOT NULL, removed INTEGER NOT NULL DEFAULT 0)"
    )
    con.execute(
        "INSERT OR IGNORE INTO users (name, library, created, removed) VALUES (?, ?, ?, 0)",
        (OWNER, secrets.token_hex(8), time.time()),
    )
    con.execute(f"PRAGMA user_version = {_VERSION}")


def _check_hours(hours: float) -> None:
    if not (math.isfinite(hours) and 0 < hours <= MAX_INVITE_HOURS):
        raise ValueError(
            _(
                "Invitation lifetime: more than 0 and at most {hours} h.",
                hours=f"{MAX_INVITE_HOURS:g}",
            )
        )


def _active(con: sqlite3.Connection, user: str) -> bool:
    return (
        con.execute("SELECT 1 FROM users WHERE name = ? AND removed = 0", (user,)).fetchone()
        is not None
    )


class DeviceStore:
    """Invitations and devices in SQLite; a connection per call, one database for node and CLI."""

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        os.close(os.open(db_path, os.O_RDWR | os.O_CREAT, 0o600))
        if db_path.stat().st_mode & 0o777 != 0o600:
            db_path.chmod(0o600)
        with closing(self._connect()) as con:
            con.executescript(_SCHEMA)
            if _version(con) < _VERSION:
                con.execute("BEGIN IMMEDIATE")
                if _version(con) < _VERSION:  # another server may have upgraded it meanwhile
                    _upgrade(con)
                con.execute("COMMIT")

    def _connect(self) -> sqlite3.Connection:
        # autocommit: explicit BEGIN IMMEDIATE where atomicity matters
        return sqlite3.connect(self._path, timeout=10.0, isolation_level=None)

    def create_invite(
        self, hours: float = INVITE_HOURS, *, user: str = OWNER, now: float
    ) -> tuple[str, float]:
        """A new invitation of a device of `user`: (code, expires_at). Only the code's hash stays
        in the database; a user the server does not have is a ValueError."""
        _check_hours(hours)
        code = secrets.token_urlsafe(24)
        expires = now + hours * 3600
        with closing(self._connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            if not _active(con, user):
                raise ValueError(no_user(user))
            con.execute("DELETE FROM invites WHERE expires <= ?", (now,))
            con.execute(
                "INSERT INTO invites (hash, created, expires, user) VALUES (?, ?, ?, ?)",
                (_digest(code), now, expires, user),
            )
            con.execute("COMMIT")
        return code, expires

    def claim(
        self, code: str, name: str, *, now: float, library: bool = False
    ) -> tuple[str, str] | None:
        """Exchange an invitation for a device of its user: (id, token), or None when it is
        unknown, expired, used or its user was removed.

        `library` — the app knows library numbers; without it an invitation of a user other
        than the owner raises OutdatedClient and stays unused. Marking the invitation and adding
        the device are one transaction: of simultaneous exchanges of one code exactly one
        succeeds."""
        if not code:
            return None
        device_id = secrets.token_hex(8)
        token = secrets.token_urlsafe(32)
        # without COMMIT, closing the connection rolls the whole transaction back
        with closing(self._connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT invites.user FROM invites JOIN users ON users.name = invites.user "
                "WHERE hash = ? AND used_at IS NULL AND expires > ? AND users.removed = 0",
                (_digest(code), now),
            ).fetchone()
            if row is None:
                return None
            (user,) = row
            if user != OWNER and not library:
                raise OutdatedClient
            con.execute(
                "UPDATE invites SET used_at = ?, device_id = ? WHERE hash = ?",
                (now, device_id, _digest(code)),
            )
            con.execute(
                "INSERT INTO devices (id, name, token_hash, created, user) VALUES (?, ?, ?, ?, ?)",
                (device_id, device_name(name), _digest(token), now, user),
            )
            con.execute("COMMIT")
        return device_id, token

    def verify(self, token: str, *, now: float) -> Caller | None:
        """The non-revoked device with this token and its user, or None; a removed user's
        devices are refused."""
        if not token:
            return None
        with closing(self._connect()) as con:
            row = con.execute(
                "SELECT devices.id, devices.last_seen, devices.user FROM devices "
                "JOIN users ON users.name = devices.user "
                "WHERE devices.token_hash = ? AND devices.revoked = 0 AND users.removed = 0",
                (_digest(token),),
            ).fetchone()
            if row is None:
                return None
            device_id, last_seen, user = row
            if last_seen is None or now - last_seen >= _SEEN_EVERY_S:
                try:
                    con.execute("UPDATE devices SET last_seen = ? WHERE id = ?", (now, device_id))
                except sqlite3.OperationalError:
                    pass  # the database is busy: last_seen waits for the next request
        return Caller(device=device_id, user=user)

    def devices(self, user: str | None = None) -> list[Device]:
        """Every device, revoked too; with `user` — only that user's."""
        query = "SELECT id, name, created, last_seen, revoked, user FROM devices"
        params: tuple[str, ...] = ()
        if user is not None:
            query, params = query + " WHERE user = ?", (user,)
        with closing(self._connect()) as con:
            rows = con.execute(query + " ORDER BY created, id", params).fetchall()
        return [
            Device(id=i, name=n, created=c, last_seen=seen, revoked=bool(r), user=u)
            for i, n, c, seen, r, u in rows
        ]

    def users(self) -> list[User]:
        """The server's users, the owner first; removed ones are not listed."""
        with closing(self._connect()) as con:
            rows = con.execute(
                "SELECT name, library, created, removed FROM users WHERE removed = 0 "
                "ORDER BY name != ?, created, name",
                (OWNER,),
            ).fetchall()
        return [User(name=n, library=lib, created=c, removed=bool(r)) for n, lib, c, r in rows]

    def user(self, name: str) -> User | None:
        """The user with this name, a removed one too."""
        with closing(self._connect()) as con:
            row = con.execute(
                "SELECT name, library, created, removed FROM users WHERE name = ?", (name,)
            ).fetchone()
        if row is None:
            return None
        n, lib, c, r = row
        return User(name=n, library=lib, created=c, removed=bool(r))

    def add_user(self, name: str, *, now: float) -> tuple[User, bool]:
        """A new user, or a removed one back with the same library: (user, came back).
        A user who is there already is a ValueError."""
        name = valid_name(name)
        with closing(self._connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT removed FROM users WHERE name = ?", (name,)).fetchone()
            if row is not None and not row[0]:
                raise ValueError(name)
            if row is None:
                con.execute(
                    "INSERT INTO users (name, library, created) VALUES (?, ?, ?)",
                    (name, secrets.token_hex(8), now),
                )
            else:
                con.execute("UPDATE users SET removed = 0 WHERE name = ?", (name,))
            con.execute("COMMIT")
        user = self.user(name)
        assert user is not None
        return user, row is not None

    def remove_user(self, name: str) -> bool:
        """Remove a user: their devices are revoked and unused invitations deleted at once; the
        recordings stay on disk. False when there is no such user; the owner is a ValueError."""
        if name == OWNER:
            raise ValueError(name)
        with closing(self._connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            removed = con.execute(
                "UPDATE users SET removed = 1 WHERE name = ? AND removed = 0", (name,)
            ).rowcount
            if removed != 1:
                return False
            con.execute("UPDATE devices SET revoked = 1 WHERE user = ?", (name,))
            con.execute("DELETE FROM invites WHERE user = ? AND used_at IS NULL", (name,))
            con.execute("COMMIT")
        return True

    def revoke(self, device_id: str) -> bool:
        """Revoke a device: its token stops working from the next request."""
        with closing(self._connect()) as con:
            changed = con.execute(
                "UPDATE devices SET revoked = 1 WHERE id = ?", (device_id,)
            ).rowcount
        return changed == 1


def check_invite(url: str, hours: float) -> str:
    """The server address for an invitation of this lifetime, or a ValueError: checked before
    anything is changed."""
    address = parse_connection(url)
    if address.token is not None or address.invite is not None:
        raise ValueError(_("Give the server address without a token or an invitation"))
    _check_hours(hours)
    return address.url


def issue_invite(
    root: Path, url: str, *, user: str = OWNER, hours: float = INVITE_HOURS, now: float
) -> str:
    """An invitation of a device of `user` to the node with folder `root` at `url`: the expiry
    line and the string for the client."""
    address = check_invite(url, hours)
    code, expires = DeviceStore(devices_db(root)).create_invite(hours, user=user, now=now)
    invite = format_invite(address, code)
    if user == OWNER:
        return _(
            "Invitation for one device, valid until {until}:\n{invite}",
            until=invite_until(expires),
            invite=invite,
        )
    return _(
        "Invitation for one device of {user}, valid until {until}:\n{invite}",
        user=user,
        until=invite_until(expires),
        invite=invite,
    )
