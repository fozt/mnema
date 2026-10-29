"""Installer settings of the app, and the start of the server image: atomic private files."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from .connection import parse_connection
from .devices import issue_invite
from .i18n import _

DEFAULT_PORT = 8000  # serve without --port; the server image's port inside the container

# Texts other modules import by name (`from .install_setup import NEW_INVITE`): each read
# returns the text in the reader's language at that moment, see __getattr__.
_TEXTS = {
    "INVITE_REFUSED": "The invitation is not valid: ask the server for a new one",
    "NO_ANSWER": "The server isn't answering. Check the address and that the server is running",
    "NOT_MNEMA": "There is no Mnema server at this address",
    "NEW_INVITE": "A new invitation from the server is needed",
    "PASTE_INVITE": "Paste the invitation from the server",
    "NOT_CONNECTED": "Server not connected",
    # the start-of-recording notice: nothing will process the recording
    "AUDIO_ONLY": "only the audio will be saved",
    "NO_SERVER_RECORD": "Server not connected, only the audio will be saved",
}


def __getattr__(name: str) -> str:
    try:
        text = _TEXTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    return _(text)


def _command(venv: Path) -> Path:
    return venv / "bin" / "mnema"


def cli_link() -> Path:
    """The mnema command on PATH."""
    return Path.home() / ".local/bin/mnema"


def link_cli(venv: Path) -> bool:
    """Point ~/.local/bin/mnema at the mnema command of the environment ``venv``, replacing the
    previous link atomically. A file that is not a link is someone else's, a link to the command
    of the server in Docker (./install.sh server) is the server's: both are left alone, False."""
    link = cli_link()
    server = Path.home() / "mnema-server/mnema"
    if link.exists() and (not link.is_symlink() or link.resolve() == server.resolve()):
        return False
    link.parent.mkdir(parents=True, exist_ok=True)
    draft = link.with_name(f".{link.name}.{os.getpid()}")
    draft.unlink(missing_ok=True)
    draft.symlink_to(_command(venv).absolute())
    os.replace(draft, link)
    return True


def _link_or_say(venv: Path) -> None:
    """Without the link the installation still works, by the command's full path."""
    command = _command(venv)
    try:
        linked = link_cli(venv)
    except OSError:
        print(_("Couldn't create {link}. Run Mnema as {command}", link=cli_link(), command=command))
        return
    if not linked:
        print(
            _(
                "{link} is taken by another file. Run Mnema as {command}",
                link=cli_link(),
                command=command,
            )
        )


def _config_dir() -> Path:
    from .config import config_dir

    return config_dir()


def _config() -> Path:
    """The settings directory, created private (700)."""
    path = _config_dir()
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def _write(path: Path, text: str) -> None:
    """The new file is private from the start and atomically replaces the old one."""
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def exchange_invite(url: str, code: str, *, transport: Any = None) -> str:
    """Exchange a server invitation for this device's token, named after the machine. A failure
    is a ValueError with our own text, never the server's, and without the code or the token."""
    import httpx

    from .client import MnemaClient
    from .core.base import MediaError

    client = MnemaClient(url, transport=transport)
    try:
        token = client.claim_invite(code, socket.gethostname())
    except MediaError as e:  # 403 is a refusal, other codes a ServerError with the status
        status = getattr(e, "status", None)
        if status is None:
            raise ValueError(_(_TEXTS["INVITE_REFUSED"])) from None
        raise ValueError(_(_TEXTS["NO_ANSWER" if status >= 500 else "NOT_MNEMA"])) from None
    except httpx.HTTPError:
        raise ValueError(_(_TEXTS["NO_ANSWER"])) from None
    except (KeyError, TypeError, ValueError):  # not JSON, or no token
        raise ValueError(_(_TEXTS["NOT_MNEMA"])) from None
    if not isinstance(token, str) or not token or any(not 33 <= ord(c) <= 126 for c in token):
        raise ValueError(_(_TEXTS["NOT_MNEMA"]))
    return token


def connect(url: str, token: str) -> None:
    """Connect this device to a server, for the installer and the Server window alike: the
    address and device token go to ~/.config/mnema/.env, and a new library (no sync checkpoint
    yet) starts with this server. Where to process is not decided here
    (node.selected_processing)."""
    from .config import library_root, set_env
    from .sync.state import checkpoint_path, initialize_library

    set_env({"MNEMA_RUNNER": url, "MNEMA_TOKEN": token})
    initialize_library(library_root(), checkpoint_path())


def configure_client(connection: str, *, transport: Any = None) -> None:
    """The app: a server invitation (#invite=, exchanged before any setting is written, so a
    refusal changes nothing) or a server address; the address of the connected server keeps the
    saved device token. Without a connection string the connection stays as it is."""
    from .config import Settings, load_env

    parsed = parse_connection(connection) if connection else None
    if parsed and parsed.token is not None:
        raise ValueError(_(_TEXTS["PASTE_INVITE"]))
    token = ""
    if parsed and parsed.invite:
        load_env()  # MNEMA_CA and timeouts from .env apply to the exchange already
        token = exchange_invite(parsed.url, parsed.invite, transport=transport)
    _config()
    load_env()  # the library root from .env, the same as the app's
    if parsed is None:
        return
    saved = Settings()
    if not parsed.invite and parsed.url == saved.runner:
        token = saved.token or ""  # a reinstall keeps the device token
    connect(parsed.url, token)


def server_root() -> Path:
    """The server folder (jobs, devices, uploads, library) of `mnema invite` and `mnema
    devices`: /data in the server image (MNEMA_SERVER_DIR), else ~/mnema-server."""
    from .config import Settings

    folder = Settings().server_dir
    return Path(folder) if folder else Path.home() / "mnema-server"


def _server_state() -> dict | None:
    """server.json of this computer's server: the invitation address given with --url."""
    state = _config_dir() / "server.json"
    if not state.exists():
        return None
    data = json.loads(state.read_text())
    if not isinstance(data, dict):
        raise ValueError(_("invalid server.json: fix the file"))
    return data


def saved_url() -> str | None:
    """The invitation address set explicitly (`mnema invite --url`); None when unset or
    server.json is unreadable."""
    try:
        data = _server_state()
    except (OSError, ValueError):
        return None
    url = data.get("url") if data else None
    return url if isinstance(url, str) and url else None


def save_url(url: str) -> str:
    """Save an explicit invitation address in server.json (a detected one is never stored) and
    return it as invitations will carry it."""
    parsed = parse_connection(url)
    if parsed.token is not None or parsed.invite is not None:
        raise ValueError(_("Give the server address without a token or an invitation"))
    data = _server_state() or {}
    if data.get("url") != parsed.url:
        data["url"] = parsed.url
        _write(_config() / "server.json", json.dumps(data) + "\n")
    return parsed.url


def server_token() -> str | None:
    """This server's token: MNEMA_TOKEN of the process environment, else server.env.
    MNEMA_TOKEN in ~/.config/mnema/.env is this device's token for another server and never
    this server's."""
    from .config import process_env

    token = process_env("MNEMA_TOKEN")
    if token is None:
        env = _config_dir() / "server.env"
        token = dotenv_values(env, interpolate=False).get("MNEMA_TOKEN") if env.is_file() else None
    return token or None


def ensure_server_token() -> str:
    """The node token in server.env (600), made once so devices' connections stay valid."""
    env = _config() / "server.env"
    token = (dotenv_values(env, interpolate=False) if env.exists() else {}).get("MNEMA_TOKEN")
    if not token:
        token = secrets.token_urlsafe(32)
        _write(env, f'MNEMA_TOKEN="{token}"\n')
    return token


def start_container() -> None:
    """The server image's start: the node token and the library root on the first start, then
    `mnema serve` on DEFAULT_PORT. Until a device joins, every start logs an invitation, or how
    to make one when no address is known (node.node_url)."""
    from .devices import DeviceStore, devices_db
    from .node import node_url

    os.umask(0o077)  # every file of the server is private
    ensure_server_token()
    root = server_root()
    # serve never creates a lost library root: it would look empty, as if everything was deleted
    (root / "library").mkdir(parents=True, exist_ok=True)
    if not any(not d.revoked for d in DeviceStore(devices_db(root)).devices()):
        if address := node_url(DEFAULT_PORT):
            print(issue_invite(root, address, now=time.time()), flush=True)
        else:
            hint = "mnema invite --url " + _("http://ADDRESS:PORT")
            print(_("New invitation: {command}", command=hint), flush=True)
    launcher = str(_command(Path(sys.prefix)))
    os.execv(launcher, [launcher, "serve", "--host", "0.0.0.0", "--dir", str(root)])


def container_healthy() -> bool:
    """The image's healthcheck: this server answers GET /api/capabilities as Mnema with its
    token."""
    from .client import MnemaClient
    from .connection import Answer

    url = f"http://127.0.0.1:{DEFAULT_PORT}"
    answer, _caps = MnemaClient(url, token=server_token()).probe(timeout=5.0)
    return answer is Answer.MNEMA


def host_defaults() -> str:
    """For ./install.sh server, run in the image on the host's network: a free port of the host
    and its network address (node.node_address, may be empty), one line."""
    from .node import node_address

    with socket.create_server(("0.0.0.0", 0)) as listener:
        port = listener.getsockname()[1]
    return f"{port} {node_address() or ''}".strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["client", "container", "health", "host"])
    # client: the app's environment; by default the one running this
    parser.add_argument("--venv", type=Path, default=Path(sys.prefix))
    parser.add_argument("--connection", default="")
    args = parser.parse_args()
    try:
        if args.mode == "client":
            configure_client(args.connection)
            _link_or_say(args.venv)
            from .config import Settings
            from .core import base

            if not base.STANDALONE and not Settings().runner:  # a Linux client only records
                print(
                    _(
                        "Server not connected, only the audio will be saved. Paste the "
                        "invitation from the server in the app settings: “Server”. Or install "
                        "again: ./install.sh client 'INVITATION'"
                    )
                )
        elif args.mode == "container":
            start_container()
        elif args.mode == "health":
            raise SystemExit(0 if container_healthy() else 1)
        else:
            print(host_defaults())
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, _("Couldn't set up Mnema: {error}", error=exc) + "\n")


if __name__ == "__main__":
    main()
