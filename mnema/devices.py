"""Node invitations and device tokens: the database keeps only sha256 of codes and tokens.

An invitation is single-use and short-lived; the client exchanges it for its own device token
(`POST /api/devices/claim`). Invitations are issued and devices revoked only on the node itself
(`mnema invite`, `mnema devices`), straight in the database, without HTTP.
"""

from __future__ import annotations

import hashlib
import math
import os
import secrets
import sqlite3
import unicodedata
from contextlib import closing
from pathlib import Path

from pydantic import BaseModel

from .connection import format_invite, parse_connection
from .i18n import _
from .i18n.fmt_server import invite_until

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


class Device(BaseModel):
    id: str
    name: str
    created: float
    last_seen: float | None = None
    revoked: bool = False


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

    def _connect(self) -> sqlite3.Connection:
        # autocommit: explicit BEGIN IMMEDIATE where atomicity matters
        return sqlite3.connect(self._path, timeout=10.0, isolation_level=None)

    def create_invite(self, hours: float = INVITE_HOURS, *, now: float) -> tuple[str, float]:
        """A new invitation: (code, expires_at). Only the code's hash stays in the database."""
        if not (math.isfinite(hours) and 0 < hours <= MAX_INVITE_HOURS):
            raise ValueError(
                _(
                    "Invitation lifetime: more than 0 and at most {hours} h.",
                    hours=f"{MAX_INVITE_HOURS:g}",
                )
            )
        code = secrets.token_urlsafe(24)
        expires = now + hours * 3600
        with closing(self._connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("DELETE FROM invites WHERE expires <= ?", (now,))
            con.execute(
                "INSERT INTO invites (hash, created, expires) VALUES (?, ?, ?)",
                (_digest(code), now, expires),
            )
            con.execute("COMMIT")
        return code, expires

    def claim(self, code: str, name: str, *, now: float) -> tuple[str, str] | None:
        """Exchange an invitation for a device: (id, token), or None when it is unknown, expired
        or used.

        Marking the invitation and adding the device are one transaction: of simultaneous
        exchanges of one code exactly one succeeds."""
        if not code:
            return None
        device_id = secrets.token_hex(8)
        token = secrets.token_urlsafe(32)
        # without COMMIT, closing the connection rolls the whole transaction back
        with closing(self._connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            used = con.execute(
                "UPDATE invites SET used_at = ?, device_id = ? "
                "WHERE hash = ? AND used_at IS NULL AND expires > ?",
                (now, device_id, _digest(code), now),
            ).rowcount
            if used != 1:
                return None
            con.execute(
                "INSERT INTO devices (id, name, token_hash, created) VALUES (?, ?, ?, ?)",
                (device_id, device_name(name), _digest(token), now),
            )
            con.execute("COMMIT")
        return device_id, token

    def verify(self, token: str, *, now: float) -> str | None:
        """The id of the non-revoked device with this token, or None."""
        if not token:
            return None
        with closing(self._connect()) as con:
            row = con.execute(
                "SELECT id, last_seen FROM devices WHERE token_hash = ? AND revoked = 0",
                (_digest(token),),
            ).fetchone()
            if row is None:
                return None
            device_id, last_seen = row
            if last_seen is None or now - last_seen >= _SEEN_EVERY_S:
                try:
                    con.execute("UPDATE devices SET last_seen = ? WHERE id = ?", (now, device_id))
                except sqlite3.OperationalError:
                    pass  # the database is busy: last_seen waits for the next request
        return device_id

    def devices(self) -> list[Device]:
        with closing(self._connect()) as con:
            rows = con.execute(
                "SELECT id, name, created, last_seen, revoked FROM devices ORDER BY created, id"
            ).fetchall()
        return [
            Device(id=i, name=n, created=c, last_seen=seen, revoked=bool(r))
            for i, n, c, seen, r in rows
        ]

    def revoke(self, device_id: str) -> bool:
        """Revoke a device: its token stops working from the next request."""
        with closing(self._connect()) as con:
            changed = con.execute(
                "UPDATE devices SET revoked = 1 WHERE id = ?", (device_id,)
            ).rowcount
        return changed == 1


def issue_invite(root: Path, url: str, *, hours: float = INVITE_HOURS, now: float) -> str:
    """An invitation of the node with folder `root` at `url`: the expiry line and the string
    for the client."""
    address = parse_connection(url)
    if address.token is not None or address.invite is not None:
        raise ValueError(_("Give the server address without a token or an invitation"))
    code, expires = DeviceStore(devices_db(root)).create_invite(hours, now=now)
    return _(
        "Invitation for one device, valid until {until}:\n{invite}",
        until=invite_until(expires),
        invite=format_invite(address.url, code),
    )
