"""One connection string: the server address with a token or an invitation in the fragment,
never in the HTTP URL."""

from __future__ import annotations

import ipaddress
import json
import re
from enum import StrEnum
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field

from . import __version__
from .i18n import _


class Connection(BaseModel):
    model_config = ConfigDict(frozen=True)
    url: str
    token: str | None = Field(default=None, repr=False)
    invite: str | None = Field(default=None, repr=False)


def parse_connection(text: str) -> Connection:
    """Parse http(s)://host:port#token=… or #invite=…; a plain address is fine too."""
    text = text.strip()
    if not text or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in text):
        raise ValueError(_("Paste the invitation from the server"))
    if "://" not in text:
        text = "http://" + text
    try:
        parts = urlsplit(text)
        port = parts.port
        host = parts.hostname or ""
        if ":" in host:
            if ipaddress.IPv6Address(host).scope_id is not None:  # a zone (%eth0) is not a URL host
                raise ValueError
            host = f"[{host}]"
        else:
            host = host.encode("idna").decode("ascii").lower()
            if not re.fullmatch(r"[a-z0-9._-]+", host):
                raise ValueError
        if parts.scheme not in ("http", "https") or parts.username is not None or parts.query:
            raise ValueError
        if port == 0 or re.search(r"%(?![0-9a-fA-F]{2})", parts.fragment):
            raise ValueError
        secret: dict[str, str] = {}
        if parts.fragment:
            values = parse_qsl(parts.fragment, keep_blank_values=True, strict_parsing=True)
            if len(values) != 1 or values[0][0] not in ("token", "invite"):
                raise ValueError
            key, value = values[0]
            if not value or any(not 33 <= ord(c) <= 126 for c in value):
                raise ValueError
            secret[key] = value
        authority = host + (f":{port}" if port is not None else "")
        url = urlunsplit((parts.scheme, authority, parts.path.rstrip("/"), "", ""))
    except (ValueError, UnicodeError) as exc:
        raise ValueError(
            _("Invalid invitation. Copy the whole invitation from the server")
        ) from exc
    return Connection(url=url, token=secret.get("token"), invite=secret.get("invite"))


def _plain_address(url: str) -> str:
    connection = parse_connection(url)
    if connection.token is not None or connection.invite is not None:
        raise ValueError(_("Pass the address and the token separately."))
    return connection.url


def format_connection(url: str, token: str | None = None) -> str:
    """Build the string to copy; the token never becomes part of an HTTP request URL."""
    text = _plain_address(url) + ("#" + urlencode({"token": token}) if token else "")
    parse_connection(text)
    return text


def format_invite(url: str, code: str) -> str:
    """The invitation string for one device: the node address and a one-time code."""
    if not code:
        raise ValueError(_("The invitation code is empty."))
    text = _plain_address(url) + "#" + urlencode({"invite": code})
    parse_connection(text)
    return text


def is_mnema(answer: object) -> bool:
    """The GET /api/capabilities body is a Mnema server: ok is true, llm is a boolean."""
    return isinstance(answer, dict) and answer.get("ok") is True and type(answer.get("llm")) is bool


class VersionGap(StrEnum):
    """Which side to update when the app and the server differ in MAJOR.MINOR."""

    APP = "app"  # the server is newer
    SERVER = "server"  # the server is older


_RELEASE = re.compile(r"(\d+)\.(\d+)(?:\.\d+)?", re.ASCII)  # digits as on the phone
_UNVERSIONED = "0.0.0"  # a local build without a release version (the phone's debug default)


def version_gap(server: object, app: str = __version__) -> VersionGap | None:
    """The `version` of GET /api/capabilities against this app; a patch difference is fine,
    a server without a readable version (before 0.3.2) or an unversioned build is not judged."""
    found = _RELEASE.fullmatch(server) if isinstance(server, str) else None
    mine = _RELEASE.fullmatch(app)
    if found is None or mine is None or _UNVERSIONED in (server, app):
        return None
    theirs, ours = tuple(map(int, found.groups()[:2])), tuple(map(int, mine.groups()[:2]))
    if theirs == ours:
        return None
    return VersionGap.APP if theirs > ours else VersionGap.SERVER


class Answer(StrEnum):
    """What an address answered to GET /api/capabilities, as on the phone (RunnerClient.health)."""

    MNEMA = "mnema"  # a Mnema server
    TOKEN = "token"  # 401/403: the server refused the token or expects one
    NOT_MNEMA = "not_mnema"  # 404 or another body: not a Mnema server
    NO_ANSWER = "no_answer"  # no answer or a server error


def answer_of(status: int, body: bytes) -> tuple[Answer, dict]:
    """The GET /api/capabilities answer as an Answer, with the Mnema server's capabilities (else
    empty): one check for the client (MnemaClient.probe, health), the Server window and the
    server update; the phone follows the same fixture tests/fixtures/connection-v1.json."""
    if status in (401, 403):
        return Answer.TOKEN, {}
    if status == 404:
        return Answer.NOT_MNEMA, {}
    if not 200 <= status < 300:
        return Answer.NO_ANSWER, {}
    try:
        data = json.loads(body)
    except ValueError:
        return Answer.NOT_MNEMA, {}
    return (
        (Answer.MNEMA, data)
        if isinstance(data, dict) and is_mnema(data)
        else (Answer.NOT_MNEMA, {})
    )
