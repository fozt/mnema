"""The installer and the server swap script: real shell scripts, apt, uv and swap faked."""

import http.server
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from dotenv import dotenv_values

from mnema.connection import parse_connection
from mnema.devices import DeviceStore
from mnema.sync.state import checkpoint_path

REPO = Path(__file__).resolve().parents[1]
COMMAND = r"""
import json, os, pathlib, shutil, subprocess, sys
name, args = pathlib.Path(sys.argv[0]).name, sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps([name, *args]) + "\n")
if (name in os.environ.get("FAKE_FAIL", "").split(",")
        and not (name == "swapon" and "--show=NAME" in args)):
    sys.exit(1)
def install(name):
    target = pathlib.Path(os.environ["FAKE_BIN"]) / name
    if not target.exists():
        target.symlink_to(os.environ["FAKE_COMMAND"])
if name == "id":
    print(os.environ.get("FAKE_UID", "1000"))
elif name == "uname":
    print(os.environ.get("FAKE_OS", "Linux") if args == ["-s"] else "x86_64")
elif name == "sudo":
    if args[0] == "bash":
        assert pathlib.Path(args[1]).name == "setup-swap.sh"
    else:
        sys.exit(subprocess.call(args))
elif name == "apt-get":
    if "install" in args:
        for package in ("ffmpeg", "git", "curl"):
            if package in args:
                install(package)
elif name == "uv" and args[:1] == ["sync"]:
    # uv sync creates the project environment: UV_PROJECT_ENVIRONMENT, else .venv here
    target = os.environ.get("UV_PROJECT_ENVIRONMENT")
    with open(os.environ["FAKE_LOG"], "a") as log:
        log.write(json.dumps(["uv-env", target]) + "\n")
    target = pathlib.Path(target or ".venv")
    if not target.exists():
        shutil.copytree(os.environ["FAKE_VENV"], target, symlinks=True)
elif name == "uv" and args[:1] == ["run"]:  # a script on uv's Python: this interpreter
    sys.exit(subprocess.call([sys.executable, *args[args.index("--") + 2 :]]))
elif name == "mnema-bar":
    with open(os.environ["FAKE_LOG"], "a") as log:
        log.write(json.dumps(
            ["mnema-bar-at", sys.argv[0], os.environ.get("UV_PROJECT_ENVIRONMENT")]
        ) + "\n")
elif name == "curl":
    pathlib.Path(args[args.index("-o") + 1]).write_text(
        'mkdir -p "$UV_INSTALL_DIR"\n'
        'cp "$FAKE_COMMAND" "$UV_INSTALL_DIR/uv"\n'
        'chmod 755 "$UV_INSTALL_DIR/uv"\n'
    )
elif name == "findmnt":
    print(os.environ.get("FAKE_FS", "ext4"))
elif name == "df":
    print("Filesystem 1024-blocks Used Available Capacity Mounted on")
    print("/dev/mock 104857600 1024 " + os.environ.get("FAKE_FREE", "98566144") + " 1% /")
elif name == "dd":
    target = next(a[3:] for a in args if a.startswith("of="))
    pathlib.Path(target).write_bytes(b"swap-data")
elif name == "swapon":
    state = pathlib.Path(os.environ["FAKE_ACTIVE"])
    if "--show=NAME" in args:
        print(state.read_text(), end="")
    else:
        state.write_text(args[0] + "\n")
elif name == "swapoff":
    state = pathlib.Path(os.environ["FAKE_ACTIVE"])
    assert state.read_text().strip() == args[0]
    state.write_text("")
elif name == "mv":
    shutil.move(args[-2], args[-1])
elif name in ("ip", "ifconfig"):
    print("1: lo    inet 127.0.0.1/8 scope host lo")
    print("2: eth0    inet 203.0.113.7/24 brd 203.0.113.255 scope global eth0")
    print("3: docker0    inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0")
"""


@pytest.fixture
def system(tmp_path):
    """PATH holds only allowed tools; dangerous commands are always faked."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in (
        "bash",
        "sh",
        "env",
        "dirname",
        "mktemp",
        "rm",
        "awk",
        "cp",
        "chmod",
        "mkdir",
        "grep",
        "sed",
        "cut",
        "tail",
        "touch",
        "cat",
        "install",
    ):
        (bindir / name).symlink_to(shutil.which(name))
    command = tmp_path / "fake-command"
    command.write_text(f"#!{sys.executable}\n{COMMAND}")
    command.chmod(0o755)
    for name in (
        "id",
        "uname",
        "sudo",
        "dpkg",
        "apt-get",
        "uv",
        "ffmpeg",
        "git",
        "curl",
        "swapon",
        "swapoff",
        "mkswap",
        "findmnt",
        "flock",
        "df",
        "dd",
        "mv",
        "systemctl",
        "ip",
        "ifconfig",
        "xcode-select",
    ):
        (bindir / name).symlink_to(command)
    log = tmp_path / "calls.jsonl"
    active = tmp_path / "active"
    active.write_text("")
    user_home = tmp_path / "home"
    user_home.mkdir()
    env = {
        **os.environ,
        "PATH": str(bindir),
        "HOME": str(user_home),
        "XDG_CONFIG_HOME": str(user_home / ".config"),
        "TMPDIR": str(tmp_path),
        "FAKE_LOG": str(log),
        "FAKE_BIN": str(bindir),
        "FAKE_COMMAND": str(command),
        "FAKE_ACTIVE": str(active),
    }
    for name in (
        "BASH_ENV",
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "XDG_DATA_HOME",
        "UV_PROJECT_ENVIRONMENT",
    ):
        env.pop(name, None)
    units = tmp_path / "systemd"
    units.mkdir()
    env["MNEMA_SYSTEMD_DIR"] = str(units)
    template = tmp_path / "venv-template/bin"  # what the fake uv sync creates
    template.mkdir(parents=True)
    (template / "python").write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
    (template / "python").chmod(0o755)
    (template / "mnema-bar").symlink_to(command)
    env["FAKE_VENV"] = str(template.parent)
    boundary = tmp_path / "boundary"
    boundary.mkdir()
    (boundary / "sitecustomize.py").write_text("""
import json, os, socket
class Listener:
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def getsockname(self): return ("0.0.0.0", int(os.environ.get("FAKE_PORT", "43117")))
def create_server(address):
    with open(os.environ["FAKE_LOG"], "a") as log:
        log.write(json.dumps(["bind", *address]) + "\\n")
    return Listener()
socket.create_server = create_server
""")
    env["PYTHONPATH"] = str(boundary)
    return tmp_path, env


def calls(system):
    log = Path(system[1]["FAKE_LOG"])
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def installer(system, *args):
    root, env = system
    shutil.copy(REPO / "install.sh", root / "install.sh")
    skip = shutil.ignore_patterns("__pycache__", "macos")
    shutil.copytree(REPO / "mnema", root / "mnema", ignore=skip, dirs_exist_ok=True)
    return subprocess.run(
        ["bash", str(root / "install.sh"), *args], env=env, capture_output=True, text=True
    )


def swap(system, size="auto", ram_kib=12345678):
    root, env = system
    fstab = root / "fstab"
    if not fstab.exists():
        fstab.write_text("# keep comment\nUUID=root / ext4 defaults 0 1\n")
    meminfo = root / "meminfo"
    meminfo.write_text(f"MemTotal: {ram_kib} kB\n")
    return subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; setup_swap "$2" "$3" "$4" "$5" "$6"',
            "test",
            str(REPO / "scripts/setup-swap.sh"),
            size,
            str(root / "swapfile"),
            str(fstab),
            str(root / "lock"),
            str(meminfo),
        ],
        env=env,
        capture_output=True,
        text=True,
    )


def env_file(system) -> Path:
    return Path(system[1]["HOME"]) / ".config/mnema/.env"


def bar_json(system) -> Path:
    """The installed app's bar.json: the same settings directory as .env."""
    return env_file(system).with_name("bar.json")


REFUSED = "Приглашение недействительно: попросите новое на сервере"


@contextmanager
def node(system, status=200):
    """A server on 127.0.0.1 for the installer: POST /api/devices/claim gives a token or refuses."""

    class Claim(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            ok = {"token": "device-token", "device": {"id": "d1"}}
            body = json.dumps(ok if status == 200 else {"detail": REFUSED}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    system[1]["NO_PROXY"] = system[1]["no_proxy"] = "127.0.0.1"
    server = http.server.HTTPServer(("127.0.0.1", 0), Claim)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()


def printed_invite(stdout: str) -> str:
    return next(line for line in stdout.splitlines() if line.startswith("http://"))


def node_devices(home: Path) -> DeviceStore:
    return DeviceStore(home / "mnema-server/.mnema/devices.db")


def test_server_creates_token_once_and_runs_as_a_service(system):
    """./install.sh server: engines without a desktop, a token made once and kept on reinstall,
    the mnema service listening with it; at the end an invitation for one device, never the
    node token."""
    first = installer(system, "server")
    assert first.returncode == 0, first.stderr
    commands = calls(system)
    assert ["uv", "sync", "--locked", "--python", "3.12", "--no-dev",
            "--extra", "full", "--extra", "live"] in commands  # fmt: skip
    config = env_file(system).parent
    # the library flag is in the service's environment, not the shared .env
    assert "MNEMA_LIBRARY" not in dotenv_values(config / ".env")
    assert "MNEMA_ASR_THREADS" not in dotenv_values(config / ".env")  # no --threads: the default
    server_env = config / "server.env"
    token = dotenv_values(server_env)["MNEMA_TOKEN"]
    assert dotenv_values(server_env)["MNEMA_LIBRARY"] == "true"
    assert token is not None and len(token) >= 40 and server_env.stat().st_mode & 0o777 == 0o600
    unit = (system[0] / "systemd/mnema.service").read_text()
    assert '"serve" "--host" "0.0.0.0"' in unit
    assert "Restart=on-failure" in unit and token not in unit
    assert f"EnvironmentFile={server_env}\n" in unit
    assert "User=1000\n" in unit
    assert f"WorkingDirectory={system[0]}\n" in unit
    assert ["systemctl", "enable", "mnema"] in commands
    assert token not in first.stdout and "#token=" not in first.stdout
    first_invite = printed_invite(first.stdout)
    assert first_invite.startswith("http://203.0.113.7:43117#invite=")
    assert "Приглашение для одного устройства, действует до " in first.stdout
    assert not any("menubar" in c or "libportaudio2" in c for c in commands)
    system[1]["FAKE_PORT"] = "45000"
    again = installer(system, "server")
    assert again.returncode == 0 and token not in again.stdout
    second_invite = printed_invite(again.stdout)
    assert second_invite.startswith("http://203.0.113.7:43117#invite=")
    assert second_invite != first_invite
    assert dotenv_values(server_env)["MNEMA_TOKEN"] == token
    assert server_env.read_text().count("MNEMA_TOKEN=") == 1
    assert server_env.read_text().count("MNEMA_LIBRARY=") == 1
    assert len([c for c in calls(system) if c[0] == "bind"]) == 1
    assert (config / "server.json").stat().st_mode & 0o777 == 0o600


def test_server_swap_only_when_asked(system):
    done = installer(system, "server", "--swap-size", "8G")
    assert done.returncode == 0, done.stderr
    assert any(c[:2] == ["sudo", "bash"] and c[-1] == "8" for c in calls(system))


def client_sync(*extras: str) -> list[str]:
    """The client's uv sync: the locked set without the dev tools, with these extras."""
    options = [arg for extra in extras for arg in ("--extra", extra)]
    return ["uv", "sync", "--locked", "--python", "3.12", "--no-dev", *options]


# a Mac processes itself with the engines it ships (no GigaAM), Linux only sends recordings
MAC_SYNC = client_sync("menubar", "mcp", "parakeet", "whisper")
LINUX_SYNC = client_sync("menubar", "mcp")
SERVER_SYNC = client_sync("full", "live")


@pytest.mark.parametrize(
    "args", [(), ("bogus",), ("client", "--bogus"), ("server", "http://server#invite=x"),
             ("server", "--swap-size", "0G"), ("server", "--swap-size", "4"),
             ("server", "--swap-size", "65G"), ("client", "--threads"),
             ("client", "--threads", "8"), ("server", "--threads", "all"),
             ("server", "--threads", "1.5")],
)  # fmt: skip
def test_invalid_options_do_not_touch_system(system, args):
    assert installer(system, *args).returncode != 0
    assert calls(system) == []
    assert not env_file(system).parent.exists()  # nothing is written before the arguments parse


@pytest.mark.parametrize(("os_name", "sync"), [("Darwin", MAC_SYNC), ("Linux", LINUX_SYNC)])
def test_client_installs_what_its_system_processes_with(system, os_name, sync):
    """./install.sh client: a Mac processes itself (the app and the engines), a Linux client sends
    recordings to the server (no engines); neither gets the dev tools."""
    system[1]["FAKE_OS"] = os_name
    done = installer(system, "client")
    assert done.returncode == 0, done.stderr
    assert [c for c in calls(system) if c[:2] == ["uv", "sync"]] == [sync]
    assert "MNEMA_ASR_THREADS" not in dotenv_values(env_file(system))


def app_env(system) -> Path:
    """The app's own environment: outside the checkout, so `uv sync` there never strips it."""
    return Path(system[1]["HOME"]) / ".local/share/mnema/app/.venv"


def synced_into(system) -> list[str | None]:
    """UV_PROJECT_ENVIRONMENT of every uv sync."""
    return [c[1] for c in calls(system) if c[0] == "uv-env"]


@pytest.mark.parametrize("os_name", ["Darwin", "Linux"])
def test_client_installs_into_its_own_environment_outside_the_checkout(system, os_name):
    """The app is synced into its own environment and runs from it; the checkout's .venv, the
    developer's, stays as it was."""
    root, env = system
    env["FAKE_OS"] = os_name
    (root / ".venv").mkdir()
    done = installer(system, "client")
    assert done.returncode == 0, done.stderr
    assert synced_into(system) == [str(app_env(system))]
    assert list((root / ".venv").iterdir()) == []
    if os_name == "Darwin":  # Mnema.app is built by the environment's own mnema-bar
        assert ["mnema-bar-at", str(app_env(system) / "bin/mnema-bar"), None] in calls(system)


def test_client_environment_follows_xdg_data_home(system):
    root, env = system
    env["XDG_DATA_HOME"] = str(root / "data home")
    assert installer(system, "client").returncode == 0
    assert synced_into(system) == [str(root / "data home/mnema/app/.venv")]


@pytest.mark.parametrize("order", [("server", "client"), ("client", "server")])
def test_client_and_server_from_one_folder_keep_their_own_environments(system, order):
    """A Linux client next to a server service of the same folder: each mode syncs only its own
    environment, so neither strips the other's packages. The server ignores an exported
    UV_PROJECT_ENVIRONMENT: its unit runs the checkout's .venv."""
    root, env = system
    env["UV_PROJECT_ENVIRONMENT"] = str(root / "elsewhere")
    for mode in order:
        assert installer(system, mode).returncode == 0
    into = {"server": str(root / ".venv"), "client": str(app_env(system))}
    assert synced_into(system) == [into[mode] for mode in order]
    syncs = [c for c in calls(system) if c[:2] == ["uv", "sync"]]
    sets = {"server": SERVER_SYNC, "client": LINUX_SYNC}
    assert syncs == [sets[mode] for mode in order]
    assert not (root / "elsewhere").exists()


@pytest.mark.parametrize(
    ("lang", "text"),
    [
        ({"LANG": "en_US.UTF-8"}, "--threads is only for the server install"),
        ({"LANG": "ru_RU.UTF-8"}, "--threads задаётся только при установке сервера"),
        ({"LC_ALL": "ru_RU.UTF-8", "LANG": "en_US.UTF-8"}, "--threads задаётся только"),
        ({"LC_ALL": "C", "LANG": "ru_RU.UTF-8"}, "--threads is only for the server install"),
    ],
)
def test_installer_speaks_the_language_of_the_locale(system, lang, text):
    _root, env = system
    for name in ("MNEMA_UI_LANG", "LC_ALL", "LC_MESSAGES", "LANG"):
        env.pop(name, None)
    env.update(lang)
    done = installer(system, "client", "--threads", "8")
    assert done.returncode != 0
    assert text in done.stdout


def test_mac_client_without_command_line_tools_stops_before_installing(system):
    """Mnema.app is built with the Command Line Tools: without them the install doesn't start."""
    system[1]["FAKE_OS"] = "Darwin"
    system[1]["FAKE_FAIL"] = "xcode-select"
    done = installer(system, "client")
    assert done.returncode != 0
    assert "Установите Command Line Tools: xcode-select --install" in done.stdout
    assert [c[0] for c in calls(system)] == ["uname", "xcode-select"]
    assert not env_file(system).parent.exists()


@pytest.mark.parametrize("mode", [("server",)])
def test_threads_are_chosen_at_install_and_manual_edit_survives_reinstall(system, mode):
    """The machine that transcribes picks its threads: the flag writes them to ~/.config/mnema/.env,
    and a reinstall without it keeps a manual edit."""
    assert installer(system, *mode, "--threads", "auto").returncode == 0
    assert dotenv_values(env_file(system))["MNEMA_ASR_THREADS"] == "auto"
    config = env_file(system)
    config.write_text(config.read_text().replace("'auto'", "6"))
    assert installer(system, *mode).returncode == 0
    assert dotenv_values(config)["MNEMA_ASR_THREADS"] == "6"
    assert installer(system, *mode, "--threads", "12").returncode == 0
    assert dotenv_values(config)["MNEMA_ASR_THREADS"] == "12"
    assert config.read_text().count("MNEMA_ASR_THREADS") == 1


@pytest.mark.parametrize("os_name", ["Darwin", "Linux"])
def test_client_install_preserves_existing_local_server_configuration(system, os_name):
    system[1]["FAKE_OS"] = os_name
    config = env_file(system)
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("MNEMA_ASR_THREADS=6\nMNEMA_MODEL=gemini/test\nMNEMA_API_KEY=private\n")
    config.with_name("server.json").write_text('{"port": 43117}\n')
    config.with_name("server.env").write_text('MNEMA_TOKEN="node-token"\n')
    files = (".env", "server.json", "server.env")
    before = [config.with_name(name).read_bytes() for name in files]
    done = installer(system, "client")
    assert done.returncode == 0, done.stderr
    assert [config.with_name(name).read_bytes() for name in files] == before


def test_server_installed_with_its_address_invites_to_it_from_now_on(system):
    """--url is the address clients reach the server at (a domain, a forwarded port): the
    installer's invitation and later ones lead there, after a reinstall too."""
    first = installer(system, "server", "--url", "http://mnema.example.org:8443")
    assert first.returncode == 0, first.stderr
    assert parse_connection(printed_invite(first.stdout)).url == "http://mnema.example.org:8443"
    again = installer(system, "server")
    assert again.returncode == 0, again.stderr
    assert parse_connection(printed_invite(again.stdout)).url == "http://mnema.example.org:8443"


class _Node:
    """A server behind MockTransport: POST /api/devices/claim gives a token or a 403."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.claims: list[dict] = []

    def __call__(self, request):
        assert (request.method, request.url.path) == ("POST", "/api/devices/claim")
        assert "authorization" not in request.headers
        self.claims.append(json.loads(request.content))
        if self.status != 200:
            return httpx.Response(self.status, json={"detail": "что угодно от узла"})
        return httpx.Response(200, json={"token": "device-token", "device": {"id": "d1"}})


def test_client_joins_a_node_by_invite(monkeypatch, tmp_path):
    """The invitation is exchanged for a device token named after the machine; .env gets the
    address and the token, never the invitation code."""
    from mnema.install_setup import configure_client

    monkeypatch.setenv("HOME", str(tmp_path))
    node = _Node()
    configure_client(
        "http://203.0.113.7:43117#invite=one-time-code",
        transport=httpx.MockTransport(node),
    )
    assert node.claims == [{"invite": "one-time-code", "name": socket.gethostname()}]
    env = tmp_path / ".config/mnema/.env"
    saved = dotenv_values(env)
    assert (saved["MNEMA_RUNNER"], saved["MNEMA_TOKEN"]) == (
        "http://203.0.113.7:43117",
        "device-token",
    )
    assert "one-time-code" not in env.read_text() and env.stat().st_mode & 0o777 == 0o600


def test_invite_exchange_trusts_the_ca_saved_in_env(monkeypatch, tmp_path):
    """MNEMA_CA only in the settings .env: the invitation exchange already trusts that CA."""
    from mnema.install_setup import configure_client

    monkeypatch.setenv("MNEMA_CA", "")
    monkeypatch.delenv("MNEMA_CA")
    ca = tmp_path / "node-ca.pem"
    env = tmp_path / ".config/mnema/.env"
    env.parent.mkdir(parents=True)
    env.write_text(f"MNEMA_CA={ca}\n")
    trusted = []
    real = httpx.AsyncClient

    def client(*args, **kwargs):
        trusted.append(kwargs.get("verify"))
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    configure_client(
        "https://node.example:43117#invite=one-time-code", transport=httpx.MockTransport(_Node())
    )
    assert trusted == [str(ca)]


@pytest.mark.parametrize(
    ("status", "text"),
    [
        (403, REFUSED),
        (502, "Сервер не отвечает. Проверьте адрес и запущен ли сервер"),
        (404, "По этому адресу нет сервера Mnema"),
    ],
)
def test_client_refused_invite_changes_nothing(monkeypatch, tmp_path, status, text):
    """A refusal gives a clear text of our own, not the server's; the settings are untouched."""
    from mnema.install_setup import configure_client

    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(ValueError) as refused:
        configure_client(
            "http://203.0.113.7:43117#invite=one-time-code",
            transport=httpx.MockTransport(_Node(status)),
        )
    assert str(refused.value) == text
    assert not (tmp_path / ".config/mnema").exists()


def test_client_takes_an_invite_not_a_token_line(monkeypatch, tmp_path):
    """A token line instead of an invitation: a clear text, the settings untouched."""
    from mnema.install_setup import configure_client

    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(ValueError, match="^Вставьте приглашение с сервера$"):
        configure_client("http://203.0.113.7:43117#token=node-token")
    assert not (tmp_path / ".config/mnema").exists()


def test_client_reinstalled_with_the_same_address_keeps_the_device_token(system):
    """Reinstalling with the connected server's address keeps the device token; another server
    never gets it."""
    for name in ("MNEMA_RUNNER", "MNEMA_TOKEN"):  # the installer has them only in .env
        system[1].pop(name, None)
    with node(system) as url:
        assert installer(system, "client", f"{url}#invite=one-time-code").returncode == 0
    done = installer(system, "client", url)
    assert done.returncode == 0, done.stderr
    assert dotenv_values(env_file(system))["MNEMA_TOKEN"] == "device-token"
    done = installer(system, "client", "http://198.51.100.9:8000")
    assert done.returncode == 0, done.stderr
    saved = dotenv_values(env_file(system))
    assert (saved["MNEMA_RUNNER"], saved["MNEMA_TOKEN"]) == ("http://198.51.100.9:8000", "")


def test_client_install_with_a_refused_invite_fails_clearly(system):
    """./install.sh client '<invitation>' refused: a clear text and a nonzero exit, no invitation
    code in the output, no server in the settings."""
    with node(system, 403) as url:
        done = installer(system, "client", f"{url}#invite=one-time-code")
    assert done.returncode != 0
    assert REFUSED in done.stdout + done.stderr
    assert "one-time-code" not in done.stdout + done.stderr
    assert not env_file(system).exists() or "MNEMA_RUNNER" not in env_file(system).read_text()


def test_server_token_is_independent_and_client_preserves_other_settings(system):
    config = env_file(system)
    config.parent.mkdir(parents=True)
    config.write_text("# keep\nMNEMA_TOKEN='secret-$HOME-#-value'\nMNEMA_ASR_THREADS=6\n")
    done = installer(system, "server")
    assert done.returncode == 0, done.stderr
    token = dotenv_values(config.with_name("server.env"))["MNEMA_TOKEN"]
    assert token != "secret-$HOME-#-value" and token not in done.stdout
    assert installer(system, "server").returncode == 0
    assert dotenv_values(config.with_name("server.env"))["MNEMA_TOKEN"] == token
    assert config.read_text().startswith("# keep\nMNEMA_TOKEN=")
    with node(system) as url:
        assert installer(system, "client", f"{url}#invite=one-time-code").returncode == 0
    assert dotenv_values(config)["MNEMA_TOKEN"] == "device-token"
    assert dotenv_values(config)["MNEMA_ASR_THREADS"] == "6"
    assert dotenv_values(config.with_name("server.env"))["MNEMA_TOKEN"] == token


def test_service_failure_is_not_reported_as_success(system):
    system[1]["FAKE_FAIL"] = "systemctl"
    done = installer(system, "server")
    assert done.returncode != 0
    assert "Приглашение" not in done.stdout and "#invite=" not in done.stdout
    assert not (Path(system[1]["HOME"]) / "mnema-server/.mnema/devices.db").exists()


def test_systemd_quotes_special_home_and_repository_paths(system):
    root, env = system
    home = root / 'home space % $ "quote"'
    home.mkdir()
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"))
    repo = root / 'repo space % $ "quote"'
    repo.mkdir()
    done = installer((repo, env), "server")
    assert done.returncode == 0, done.stderr
    unit = (root / "systemd/mnema.service").read_text()
    assert 'repo space %% $$ \\"quote\\"' in unit
    assert 'Environment="HOME=' in unit and 'home space %% $ \\"quote\\"' in unit
    assert f"WorkingDirectory={str(repo).replace('%', '%%')}\n" in unit
    assert f"EnvironmentFile={str(home).replace('%', '%%')}/.config/mnema/server.env\n" in unit
    command = next(
        line for line in done.stdout.splitlines() if line.startswith("Новое приглашение:")
    )
    arguments = shlex.split(command.removeprefix("Новое приглашение: "))
    assert arguments == [str(repo / ".venv/bin/mnema"), "invite"]
    assert '/.venv/bin/mnema" "serve" ' in unit  # the console script exists in a release too
    invite = parse_connection(printed_invite(done.stdout)).invite
    assert invite and node_devices(home).claim(invite, "ПК", now=time.time()) is not None


@pytest.mark.parametrize("connection", [(), ("http://host:43117",)])
def test_client_reinstall_keeps_the_app_settings(system, connection):
    """A reinstall, with a connection string or without, keeps the app settings."""
    chosen = bar_json(system)
    chosen.parent.mkdir(parents=True)
    chosen.write_text(json.dumps({"where": "server", "hotkey": "ctrl+opt+R"}))
    before = chosen.read_bytes()
    assert installer(system, "client", *connection).returncode == 0
    assert chosen.read_bytes() == before


@pytest.mark.parametrize(
    ("standalone", "connection", "told"),
    [(False, "", True), (False, "http://203.0.113.7:43117", False), (True, "", False)],
)
def test_client_without_a_server_is_told_how_to_connect(
    monkeypatch, capsys, tmp_path, standalone, connection, told
):
    """A Linux client without a server only records: the installer says where to paste the
    invitation. A connected client or a Mac that processes itself hears nothing."""
    from mnema import install_setup
    from mnema.core import base

    monkeypatch.setattr(base, "STANDALONE", standalone)
    argv = ["install_setup", "client", "--repo", str(tmp_path), "--connection", connection]
    monkeypatch.setattr(sys, "argv", argv)
    install_setup.main()
    assert ("Сервер не подключён, сохранится только звук" in capsys.readouterr().out) is told


def test_client_env_write_failure_preserves_original_and_cleans_temporary(monkeypatch, tmp_path):
    from mnema.install_setup import configure_client

    monkeypatch.setenv("HOME", str(tmp_path))
    config = tmp_path / ".config/mnema"
    config.mkdir(parents=True)
    original = "# untouched\nMNEMA_RUNNER='https://original'\nMNEMA_TOKEN='original'\n"
    (config / ".env").write_text(original)

    def fail(*args):
        raise OSError("disk failure")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError, match="disk failure"):
        configure_client("http://other:43117")
    assert (config / ".env").read_text() == original
    assert list(config.iterdir()) == [config / ".env"]


def test_swap_installs_once_preserves_fstab_and_uses_private_permissions(system):
    root, env = system
    done = swap(system)
    assert done.returncode == 0, done.stderr
    assert (root / "swapfile").stat().st_mode & 0o777 == 0o600
    fstab = (root / "fstab").read_text()
    assert fstab.startswith("# keep comment\nUUID=root / ext4 defaults 0 1\n")
    assert fstab.count(f"{root}/swapfile none swap sw 0 0") == 1
    assert any(c[0] == "dd" and "count=4096" in c for c in calls(system))
    assert swap(system).returncode == 0
    assert (root / "fstab").read_text() == fstab
    assert len([c for c in calls(system) if c[0] == "mkswap"]) == 1
    assert not any(c[0] == "swapoff" for c in calls(system))
    assert Path(env["FAKE_ACTIVE"]).read_text().strip() == str(root / "swapfile")


@pytest.mark.parametrize(
    "size,ram_kib,count",
    [("auto", 1572864, 2048), ("8", 12345678, 8192), ("4G", 12345678, 4096)],
)  # fmt: skip
def test_swap_respects_memory_default_and_explicit_size(system, size, ram_kib, count):
    root, _env = system
    (root / "fstab").write_text("# root filesystem\n")
    (root / "fstab").chmod(0o640)
    assert swap(system, size, ram_kib).returncode == 0
    assert any(c[0] == "dd" and f"count={count}" in c for c in calls(system))
    assert (root / "fstab").stat().st_mode & 0o777 == 0o640


@pytest.mark.parametrize("size", ["0G", "65G", "4M", "4GG", "1.5G", "4g"])
def test_invalid_swap_size_has_no_system_effects(system, size):
    done = swap(system, size)
    assert done.returncode != 0 and "4G" in done.stdout
    assert calls(system) == []


@pytest.mark.parametrize("existing", ["active", "fstab", "file"])
def test_swap_never_overwrites_existing_state(system, existing):
    root, env = system
    original = "UUID=root / ext4 defaults 0 1\n"
    if existing == "active":
        Path(env["FAKE_ACTIVE"]).write_text("/dev/existing\n")
    elif existing == "fstab":
        original += "UUID=swap none swap sw 0 0\n"
    else:
        (root / "swapfile").write_text("existing file")
    (root / "fstab").write_text(original)
    done = swap(system)
    assert done.returncode == (1 if existing == "file" else 0)
    assert (root / "fstab").read_text() == original
    assert not any(c[0] in ("dd", "mkswap", "swapoff") for c in calls(system))
    if existing == "file":
        assert (root / "swapfile").read_text() == "existing file"


@pytest.mark.parametrize("setting,value", [("FAKE_FS", "btrfs"), ("FAKE_FREE", "1048576")])
def test_swap_rejects_unsupported_disk_before_allocating(system, setting, value):
    root, env = system
    env[setting] = value
    assert swap(system).returncode != 0
    assert not (root / "swapfile").exists()
    assert not any(c[0] == "dd" for c in calls(system))


@pytest.mark.parametrize("failure", ["dd", "mkswap", "swapon", "mv"])
def test_swap_rolls_back_own_partial_file_and_preserves_fstab(system, failure):
    root, env = system
    env["FAKE_FAIL"] = failure
    original = "# original\nUUID=root / ext4 defaults 0 1\n"
    (root / "fstab").write_text(original)
    done = swap(system)
    assert done.returncode != 0, done.stdout + done.stderr
    assert not (root / "swapfile").exists()
    assert (root / "fstab").read_text() == original
    assert not list(root.glob("fstab.mnema.*"))
    assert Path(env["FAKE_ACTIVE"]).read_text() == ""
    stopped = [c for c in calls(system) if c[0] == "swapoff"]
    assert stopped == ([["swapoff", str(root / "swapfile")]] if failure == "mv" else [])


def test_swap_keeps_own_active_file_when_rollback_cannot_disable_it(system):
    root, env = system
    env["FAKE_FAIL"] = "mv,swapoff"
    done = swap(system)
    assert done.returncode != 0
    assert (root / "swapfile").exists()
    assert Path(env["FAKE_ACTIVE"]).read_text().strip() == str(root / "swapfile")
    assert "файл сохранён" in done.stderr
    assert str(root / "swapfile") not in (root / "fstab").read_text()


@pytest.mark.parametrize("source", ["default", "saved", "environment"])
def test_client_connection_initializes_new_library_only(tmp_path, monkeypatch, source):
    from mnema.install_setup import configure_client

    monkeypatch.setenv("HOME", str(tmp_path))
    # the installer moves the .env line into the environment: gone after the test
    monkeypatch.setenv("MNEMA_LIVE_DIR", "")
    monkeypatch.delenv("MNEMA_LIVE_DIR")
    config = tmp_path / ".config/mnema"
    config.mkdir(parents=True)
    root = tmp_path / ("mnema" if source == "default" else source)
    if source == "saved":
        (config / ".env").write_text(f"MNEMA_LIVE_DIR='{root}'\n")
    elif source == "environment":
        (config / ".env").write_text("MNEMA_LIVE_DIR='/unused'\n")
        monkeypatch.setenv("MNEMA_LIVE_DIR", str(root))

    configure_client("https://node.example")
    assert root.is_dir()
    transcript = root / "Meeting" / "transcript.txt"
    transcript.parent.mkdir()
    transcript.write_bytes(b"already recorded")
    configure_client("https://node.example")
    assert transcript.read_bytes() == b"already recorded"
    transcript.unlink()
    transcript.parent.rmdir()
    root.rmdir()
    checkpoint = checkpoint_path()
    checkpoint.write_text("broken checkpoint")
    configure_client("https://node.example")
    assert not root.exists()
    assert checkpoint.read_text() == "broken checkpoint"


def test_xdg_config_home_is_the_one_settings_dir(tmp_path, monkeypatch):
    """With XDG_CONFIG_HOME set the installer writes the connection where the app and sync read
    it, and sees sync.json of a past sync: a lost library is not recreated."""
    from mnema.config import Settings, load_env
    from mnema.install_setup import configure_client
    from mnema.sync.state import Checkpoint, save

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("MNEMA_LIVE_DIR", str(tmp_path / "mnema"))
    save(checkpoint_path(), Checkpoint(server="http://old:8000"))  # the app has synced before
    configure_client(
        "http://203.0.113.7:43117#invite=one-time-code", transport=httpx.MockTransport(_Node())
    )
    assert not (tmp_path / "mnema").exists()
    monkeypatch.delenv("MNEMA_RUNNER")  # a new app process reads the settings again
    monkeypatch.delenv("MNEMA_TOKEN")
    load_env()
    assert (Settings().runner, Settings().token) == ("http://203.0.113.7:43117", "device-token")
    assert not (tmp_path / "home").exists()


@pytest.mark.parametrize("args", [("server",), ("client", "http://203.0.113.7:43117")])
def test_install_links_the_mnema_command(system, args):
    """Both installs point ~/.local/bin/mnema at the command of their environment: the server
    at the checkout's .venv, the app at its own; a reinstall moves the link."""
    link = Path(system[1]["HOME"]) / ".local/bin/mnema"
    link.parent.mkdir(parents=True)
    link.symlink_to(system[0] / "old/.venv/bin/mnema")
    done = installer(system, *args)
    assert done.returncode == 0, done.stderr
    venv = system[0] / ".venv" if args[0] == "server" else app_env(system)
    assert Path(os.readlink(link)) == venv / "bin/mnema"


def test_shared_texts_are_read_in_the_language_of_the_moment():
    """Other modules import these texts by name inside their functions: each read is in the
    language of that moment (a server request, the app)."""
    from mnema import install_setup
    from mnema.i18n import using

    with using("en"):
        assert install_setup.NEW_INVITE == "A new invitation from the server is needed"
    with using("ru"):
        from mnema.install_setup import NEW_INVITE, NO_SERVER_RECORD

        assert NEW_INVITE == "Нужно новое приглашение с сервера"
        assert NO_SERVER_RECORD == "Сервер не подключён, сохранится только звук"
    with pytest.raises(AttributeError):
        install_setup.NO_SUCH_TEXT  # noqa: B018
