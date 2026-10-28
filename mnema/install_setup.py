"""Installer settings and the systemd service: atomic files, no shell code interpreted."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shlex
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from .connection import Connection, format_connection, parse_connection
from .devices import issue_invite
from .i18n import _

# The server runs this console script from its checkout or release (`.venv` there); the app
# installed by `./install.sh client` has its own environment, passed as --venv.
LAUNCHER = ".venv/bin/mnema"
DEFAULT_PORT = 8000  # serve without --port and --saved-port

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
    previous link atomically. A file that is not a link is someone else's: left alone, False."""
    link = cli_link()
    if link.exists() and not link.is_symlink():
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


def _scalar(value: str) -> str:
    """User, WorkingDirectory and EnvironmentFile take no quotes or backslash escapes."""
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(_("The installation path contains control characters."))
    if not value or value != value.strip() or value.endswith("\\"):
        raise ValueError(_("The installation path must not end with a space or a backslash."))
    return value.replace("%", "%%")


def _quote(value: str, *, command: bool = False) -> str:
    """systemd lists unquote; % is a specifier, $ a substitution in ExecStart."""
    value = _scalar(value).replace("\\", "\\\\").replace('"', '\\"')
    if command:
        value = value.replace("$", "$$")
    return '"' + value + '"'


def _unit(repo: Path, user: str, command: list[str], env_file: Path | None) -> str:
    from .config import config_dir

    environment = f"EnvironmentFile={_scalar(str(env_file))}\n" if env_file else ""
    # the service reads the same settings directory as the installer
    home = " ".join(
        _quote(f"{k}={v}")
        for k, v in (("HOME", Path.home()), ("XDG_CONFIG_HOME", config_dir().parent))
    )
    return (
        "[Unit]\nDescription=Mnema\nAfter=network-online.target\nWants=network-online.target\n\n"
        f"[Service]\nUser={_scalar(user)}\nEnvironment={home}\n"
        f"{environment}WorkingDirectory={_scalar(str(repo))}\n"
        f"ExecStart={' '.join(_quote(arg, command=True) for arg in command)}\n"
        "Restart=on-failure\nRestartSec=3\nUMask=0077\n\n[Install]\nWantedBy=multi-user.target\n"
    )


def _service(name: str, content: str, directory: Path) -> None:
    prefix = [] if os.geteuid() == 0 else ["sudo"]
    fd, temporary = tempfile.mkstemp(prefix="mnema-unit-")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
        subprocess.run(
            [*prefix, "install", "-m", "644", temporary, str(directory / f"{name}.service")],
            check=True,
        )
        for args in (
            ["daemon-reload"],
            ["enable", name],
            ["restart", name],
            ["is-active", "--quiet", name],
        ):
            subprocess.run([*prefix, "systemctl", *args], check=True)
    finally:
        Path(temporary).unlink(missing_ok=True)


def server_service_path() -> Path:
    return Path(os.environ.get("MNEMA_SYSTEMD_DIR", "/etc/systemd/system")) / "mnema.service"


def _unit_value(content: str, key: str) -> str:
    values = re.findall(rf"^{key}=(.*)$", content, flags=re.MULTILINE)
    if len(values) != 1:
        raise ValueError(_("Couldn't read the installed Mnema service"))
    return values[0]


def _server_command(content: str) -> tuple[Path, str, int]:
    """The installation folder, ExecStart and where the LAUNCHER path ends in it:
    `<folder>/LAUNCHER serve …`."""
    unknown = _("Unknown way of starting the Mnema service")
    command = _unit_value(content, "ExecStart")
    args = shlex.split(command)
    if len(args) < 2 or args[1] != "serve":
        raise ValueError(unknown)
    # The installer escapes literal $ and % for systemd; other arguments are kept as they are.
    launcher = Path(args[0].replace("%%", "%").replace("$$", "$"))
    repo = launcher.parent.parent.parent
    if not launcher.is_absolute() or launcher != repo / LAUNCHER:
        raise ValueError(unknown)
    end = next(re.finditer(r'"(?:[^"\\]|\\.)*"|[^\s"]+', command)).end()
    if shlex.split(command[:end]) != args[:1]:
        raise ValueError(unknown)
    return repo, command, end


@dataclass(frozen=True)
class InstalledServer:
    path: Path
    content: str
    repo: Path
    user: str

    def connection(self) -> Connection:
        _repo, command, _end = _server_command(self.content)
        args = shlex.split(command)[2:]

        if "--saved-port" not in args:
            raise ValueError(_("Unknown way of starting the Mnema service"))

        def option(name: str) -> str:
            values = []
            for index, arg in enumerate(args):
                if arg == name:
                    if index + 1 == len(args):
                        raise ValueError(_("Invalid Mnema service command"))
                    values.append(args[index + 1])
                elif arg.startswith(name + "="):
                    values.append(arg.split("=", 1)[1])
            if len(values) != 1:
                raise ValueError(_("Invalid Mnema service command"))
            return values[0]

        host = option("--host")
        host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
        # the port serve takes when the service starts
        port = serve_port(saved=True)
        env = Path(_unit_value(self.content, "EnvironmentFile").replace("%%", "%"))
        if not env.is_absolute() or not env.is_file():
            raise ValueError(_("The Mnema service environment file is missing"))
        token = dotenv_values(env, interpolate=False).get("MNEMA_TOKEN")
        address = f"http://{'[' + host + ']' if ':' in host else host}:{port}"
        return parse_connection(format_connection(address, token))

    def retarget(self, repo: Path) -> str:
        _repo, command, end = _server_command(self.content)
        _unit_value(self.content, "WorkingDirectory")
        start = _quote(str(repo / LAUNCHER), command=True)
        content = re.sub(
            r"^ExecStart=.*$",
            lambda _match: "ExecStart=" + start + command[end:],
            self.content,
            flags=re.MULTILINE,
        )
        return re.sub(
            r"^WorkingDirectory=.*$",
            lambda _match: "WorkingDirectory=" + _scalar(str(repo)),
            content,
            flags=re.MULTILINE,
        )


def installed_server() -> InstalledServer | None:
    path = server_service_path()
    try:
        content = path.read_text()
    except FileNotFoundError:
        return None
    repo, _command_line, _end = _server_command(content)
    return InstalledServer(path, content, repo, _unit_value(content, "User"))


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
    """Data of this computer's server (jobs, devices, library): shared by `./install.sh server`,
    run-server and mnema invite/devices."""
    return Path.home() / "mnema-server"


def _server_state() -> dict | None:
    """server.json of this computer's server: the port and an explicit invitation address."""
    state = _config_dir() / "server.json"
    if not state.exists():
        return None
    data = json.loads(state.read_text())
    if not isinstance(data, dict):
        raise ValueError(_("invalid server.json: fix the file"))
    return data


def server_port() -> int | None:
    """This computer's server port from server.json, chosen once by the installer; None without
    the file, ValueError for an invalid port."""
    data = _server_state()
    if data is None:
        return None
    port = data.get("port")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError(_("invalid port in server.json: fix the file"))
    return port


def serve_port(explicit: int | None = None, *, saved: bool = False) -> int:
    """The port of `mnema serve`: --port; --saved-port (the service, run-server) reads this
    computer's server port on every start and fails without server.json rather than picking
    another; otherwise DEFAULT_PORT."""
    if explicit:
        return explicit
    if not saved:
        return DEFAULT_PORT
    port = server_port()
    if port is None:
        raise ValueError(_("no ~/.config/mnema/server.json: reinstall the server"))
    return port


def saved_url() -> str | None:
    """The invitation address set explicitly (`mnema invite --url`, `./install.sh server --url`);
    None when unset or server.json is unreadable."""
    try:
        data = _server_state()
    except (OSError, ValueError):
        return None
    url = data.get("url") if data else None
    return url if isinstance(url, str) and url else None


def save_url(url: str) -> str:
    """Save an explicit invitation address in server.json next to the port (a detected address
    is never stored) and return it as invitations will carry it. Not saved when the server is
    not installed (no server.json)."""
    parsed = parse_connection(url)
    if parsed.token is not None or parsed.invite is not None:
        raise ValueError(_("Give the server address without a token or an invitation"))
    data = _server_state()
    if data is not None and data.get("url") != parsed.url:
        data["url"] = parsed.url
        _write(_config_dir() / "server.json", json.dumps(data) + "\n")
    return parsed.url


def server_token() -> str | None:
    """This server's token: MNEMA_TOKEN of the service environment (systemd, run-server), else
    server.env. MNEMA_TOKEN in ~/.config/mnema/.env is this device's token for another server
    and never this server's."""
    from .config import process_env

    token = process_env("MNEMA_TOKEN")
    if token is None:
        env = _config_dir() / "server.env"
        token = dotenv_values(env, interpolate=False).get("MNEMA_TOKEN") if env.is_file() else None
    return token or None


def server_identity() -> tuple[int, str]:
    """Port and token of the `./install.sh server` server, in server.json and server.env (600).
    Missing ones are chosen once, a free port and a new token, so clients' connections stay
    valid."""
    config = _config()
    port = server_port()
    if port is None:
        with socket.create_server(("0.0.0.0", 0)) as listener:
            port = listener.getsockname()[1]
        _write(config / "server.json", json.dumps({"port": port}) + "\n")
    env = config / "server.env"
    saved = dotenv_values(env, interpolate=False) if env.exists() else {}
    token = saved.get("MNEMA_TOKEN") or secrets.token_urlsafe(32)
    format_connection("http://localhost", token)  # the token must be valid for Bearer
    _set_server_env(env, MNEMA_TOKEN=token)
    return port, token


def _set_server_env(env: Path, **values: str) -> None:
    """Set variables in server.env, the service environment (systemd EnvironmentFile,
    run-server): double quotes, no shell expansion; other lines stay."""

    def line(key: str, value: str) -> str:
        return key + '="' + value.replace("\\", "\\\\").replace('"', '\\"') + '"\n'

    old = env.read_text() if env.exists() else ""
    lines, missing = [], dict(values)
    for kept in old.splitlines():
        key = kept.split("=", 1)[0].strip()
        if key not in values:
            lines.append(kept + "\n")
        elif key in missing:  # a repeated variable is dropped
            lines.append(line(key, missing.pop(key)))
    text = "".join(lines) + "".join(line(k, v) for k, v in missing.items())
    if text != old or env.stat().st_mode & 0o777 != 0o600:
        _write(env, text)


def configure_server(
    repo: Path,
    *,
    systemd: Path | None,
    user: str,
    threads: str | None = None,
    url: str | None = None,
) -> None:
    from .config import set_env
    from .node import node_url

    port, _token = server_identity()
    if url:  # the address clients reach the server at, for every invitation
        save_url(url)
    if threads is not None:  # without the flag: the .env line or the Settings default
        set_env({"MNEMA_ASR_THREADS": threads})
    config = _config()
    # the library flag belongs to the service environment, not the shared .env
    _set_server_env(config / "server.env", MNEMA_LIBRARY="true")
    root = server_root()
    # only the installer creates the library root: serve never mistakes a lost one for empty
    (root / "library").mkdir(parents=True, exist_ok=True)
    launcher = str(repo / LAUNCHER)
    # serve reads the port from server.json on every start
    command = [launcher, "serve", "--host", "0.0.0.0", "--saved-port", "--dir", str(root)]
    if systemd is not None:
        _service("mnema", _unit(repo, user, command, config / "server.env"), systemd)
    else:
        python = str(repo / ".venv/bin/python")
        run = [python, "-m", "mnema.install_setup", "run-server", "--repo", str(repo)]
        print(_("No systemd. Start the server like this: {command}", command=shlex.join(run)))
    # the node token is never printed: a new device joins with a one-time invitation
    address = node_url(port)
    if address is None:  # the address is given by hand once and remembered
        url = _("http://ADDRESS:{port}", port=port)
        hint = shlex.join([launcher, "invite", "--url", url])
        print(
            "\n"
            + _(
                "The server's network address wasn't found. Create an invitation with the "
                "address: {command}",
                command=hint,
            )
        )
    else:
        print("\n" + issue_invite(root, address, now=time.time()))
        print(_("New invitation: {command}", command=shlex.join([launcher, "invite"])))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["client", "server", "run-server"])
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    # client: the app's environment; by default the one running this
    parser.add_argument("--venv", type=Path, default=Path(sys.prefix))
    parser.add_argument("--connection", default="")
    parser.add_argument("--systemd", type=Path)
    parser.add_argument("--user", default="")
    parser.add_argument("--threads")
    parser.add_argument("--url")
    args = parser.parse_args()
    try:
        if args.mode == "client":
            if args.threads is not None or args.url is not None:
                parser.error(_("--threads and --url are for server only"))
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
        elif args.mode == "server":
            configure_server(
                args.repo,
                systemd=args.systemd,
                user=args.user,
                threads=args.threads,
                url=args.url,
            )
            _link_or_say(args.repo / ".venv")
        else:
            server_identity()  # port and token before the first start
            # the same environment as the service's EnvironmentFile: token and library
            saved = dotenv_values(_config() / "server.env", interpolate=False)
            os.environ.update({k: v for k, v in saved.items() if v is not None})
            launcher = str(args.repo / LAUNCHER)
            os.execv(
                launcher,
                [
                    launcher,
                    "serve",
                    "--host",
                    "0.0.0.0",
                    "--saved-port",
                    "--dir",
                    str(server_root()),
                ],
            )
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        parser.exit(1, _("Couldn't set up Mnema: {error}", error=exc) + "\n")


if __name__ == "__main__":
    main()
