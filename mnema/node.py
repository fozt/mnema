"""Where to process recordings, and this computer's server address for invitations.

A Mac processes itself or on the connected server, as chosen in settings; other systems use
the MNEMA_RUNNER server. The address for clients is node_url, one rule for the server
installer and mnema invite.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import platform
import re
import subprocess
from collections.abc import Callable, Iterable

from .connection import Connection

_TAILSCALE = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))
_BRIDGES = ("docker", "br-", "veth", "virbr", "vboxnet", "vmnet")  # container and VM networks


SERVER = "server"  # bar.json "where": the Mac processes on the server (bar.state.Where.SERVER)


def saved_where() -> str | None:
    """Where the Mac processes, as the app saved it (bar.json "where"), or None. Read without
    the app's modules: the CLI and MCP need the rule too."""
    from .config import bar_prefs_file

    try:
        data = json.loads(bar_prefs_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    where = data.get("where") if isinstance(data, dict) else None
    return where if isinstance(where, str) else None


def selected_processing(
    explicit: str | Connection | None = None, *, local: bool = False, where: str | None = None
) -> Connection | None:
    """Where to process, one rule for the app, the CLI and MCP; None is this computer.
    CLI flags win: --local is here, --runner the given server. A Mac uses the MNEMA_RUNNER
    server when settings say "On the server" (where; None reads bar.json), else itself;
    MNEMA_RUNNER also serves it for syncing recordings. Others use the MNEMA_RUNNER server, or
    this computer without it."""
    from .config import Settings
    from .core import base

    if local:
        return None
    if isinstance(explicit, Connection):
        return explicit
    settings = Settings()
    if explicit:
        return Connection(url=explicit, token=settings.token or None)
    if not settings.runner:
        return None
    if base.STANDALONE and (saved_where() if where is None else where) != SERVER:
        return None
    return Connection(url=settings.runner, token=settings.token or None)


_IP_LINE = re.compile(r"^\d+:\s+(\S+?)(?:@\S+)?\s+inet6?\s+([0-9A-Fa-f.:]+)")
_IFCONFIG_HEAD = re.compile(r"^(\S+?):\s")
_IFCONFIG_ADDR = re.compile(r"^\s+inet6?\s+([0-9A-Fa-f.:]+)")


def parse_interfaces(text: str) -> list[tuple[str, str]]:
    """(interface, address) pairs from `ip -o addr` (Linux) or `ifconfig` (macOS)."""
    found: list[tuple[str, str]] = []
    name = ""
    for line in text.splitlines():
        if m := _IP_LINE.match(line):
            found.append((m[1], m[2]))
        elif m := _IFCONFIG_HEAD.match(line):
            name = m[1]
        elif (m := _IFCONFIG_ADDR.match(line)) and name:
            found.append((name, m[1]))
    return found


def interfaces() -> list[tuple[str, str]]:
    """This machine's interface addresses; empty when nothing can tell."""
    command = ["ifconfig"] if platform.system() == "Darwin" else ["ip", "-o", "addr", "show"]
    try:
        out = subprocess.run(
            command, capture_output=True, text=True, timeout=2, stdin=subprocess.DEVNULL
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return parse_interfaces(out)


def node_address(found: Iterable[tuple[str, str]] | None = None) -> str | None:
    """This computer's network address for clients, one rule for the installer and mnema
    invite, computed on every call: Tailscale (100.64.0.0/10, fd7a:115c:a1e0::/48), visible from
    any network of the VPN, IPv4 first; else the first public IPv4 (a VPS's private networks are
    not reachable from a phone); else the first private IPv4 (a home network). Never loopback,
    link-local or container bridges. IPv6 comes in brackets, as in a URL; nothing found — None."""
    addresses: list[tuple[str, ipaddress.IPv4Address | ipaddress.IPv6Address]] = []
    for name, raw in interfaces() if found is None else found:
        with contextlib.suppress(ValueError):
            addresses.append((name, ipaddress.ip_address(raw)))
    tailscale = [a for _, a in addresses if any(a in net for net in _TAILSCALE)]
    for a in sorted(tailscale, key=lambda a: a.version):
        return f"[{a}]" if a.version == 6 else str(a)
    ipv4 = [
        a
        for name, a in addresses
        if a.version == 4 and not (a.is_loopback or a.is_link_local or name.startswith(_BRIDGES))
    ]
    for a in [a for a in ipv4 if a.is_global] + [a for a in ipv4 if a.is_private]:
        return str(a)
    return None


def node_url(port: int, address: Callable[[], str | None] = node_address) -> str | None:
    """This computer's server address in an invitation, one rule for the installer and mnema
    invite: the explicit --url (install_setup.saved_url), else the current network address
    (address — node_address) and port; None when there is none."""
    from .install_setup import saved_url

    if (url := saved_url()) is not None:
        return url
    found = address()
    return f"http://{found}:{port}" if found else None
