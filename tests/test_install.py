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
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from dotenv import dotenv_values

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
elif name == "chown":
    pass
elif name == "readlink":  # the host's time zone, or where a link points
    zone = "/usr/share/zoneinfo/Europe/Berlin"
    print(zone if args == ["/etc/localtime"] else os.readlink(args[0]))
elif name == "systemctl":  # a server of 0.3.0 is this unit
    sys.exit(0 if args == ["cat", "mnema.service"] and os.environ.get("FAKE_OLD_SERVICE") else 1)
elif name == "docker":
    project = pathlib.Path(args[2]).parent if args[:2] == ["compose", "-f"] else None
    pulled = pathlib.Path(os.environ["FAKE_BIN"]).parent / "pulled"
    rest = args[3:] if project else args
    def settings():
        lines = (project / ".env").read_text().splitlines()
        return dict(line.split("=", 1) for line in lines)
    if args[:2] == ["compose", "version"]:
        sys.exit(int("compose" in os.environ.get("FAKE_DOCKER_MISSING", "")))
    elif args[:1] == ["info"]:
        missing = os.environ.get("FAKE_DOCKER_MISSING", "")
        if "denied" in missing:
            print("permission denied while trying to connect to the docker API", file=sys.stderr)
        elif "daemon" in missing:
            print("Cannot connect to the Docker daemon", file=sys.stderr)
        sys.exit(int("denied" in missing or "daemon" in missing))
    elif args[:2] == ["image", "inspect"]:  # the image id: a pull brings a new one
        print("sha256:" + ("new" if pulled.exists() else "old"))
    elif args[:1] == ["run"]:  # the image on the host's network
        assert args[1:4] == ["--rm", "--network", "host"]
        assert args[-2:] == ["mnema.install_setup", "host"]
        print(os.environ.get("FAKE_HOST", "43117 203.0.113.7"))
    elif rest[:1] == ["config"]:
        print(settings()["MNEMA_IMAGE"])
    elif args[:1] == ["tag"]:  # back to the previous image
        assert args[1] == "sha256:old"
        pulled.unlink()
    elif rest[:1] == ["up"]:
        assert settings()["MNEMA_PORT"] and (project / "data").is_dir()
        if settings()["MNEMA_PULL"] == "always" and "never" not in rest:  # pull_policy, --pull
            pulled.touch()
        if pulled.exists() and os.environ.get("FAKE_BROKEN_IMAGE"):
            sys.exit(1)  # the new image never becomes healthy
    elif args[:1] == ["exec"]:  # the server's folder and container as ./mnema.sh finds them
        project = pathlib.Path(
            os.environ.get("MNEMA_SERVER_DIR") or pathlib.Path(os.environ["HOME"]) / "mnema-server"
        )
        command = [a for a in args[1:] if a not in ("-i", "-it")]
        assert command[:2] == [settings()["MNEMA_NAME"], "mnema"], command
        attached = any(a in ("-i", "-it") for a in args[1:3])  # without -i stdin stays out
        saved = project / "data/config/mnema/server.json"
        if command[2:4] == ["invite", "--url"]:  # remembered (install_setup.save_url)
            saved.parent.mkdir(parents=True, exist_ok=True)
            saved.write_text(json.dumps({"url": command[4]}))
        if command[2:3] == ["invite"]:  # node.node_url
            url = settings()["MNEMA_URL"]
            if saved.exists():
                url = json.loads(saved.read_text())["url"]
            if not url:
                sys.exit("Give --url")
            print(url + "#invite=one-time-code")
        elif command[2:] == ["--version"]:
            print("mnema 9.9.9")
        elif command[2:] == ["model"]:  # asks for the key
            print("key: " + (sys.stdin.readline().strip() if attached else ""))
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
        "ln",
        "head",
        "find",
        "sort",
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
        "docker",
        "chown",
        "readlink",
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
        "MNEMA_IMAGE",
        "MNEMA_NAME",
        "MNEMA_SERVER_DIR",
    ):
        env.pop(name, None)
    template = tmp_path / "venv-template/bin"  # what the fake uv sync creates
    template.mkdir(parents=True)
    (template / "python").write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
    (template / "python").chmod(0o755)
    (template / "mnema-bar").symlink_to(command)
    env["FAKE_VENV"] = str(template.parent)
    return tmp_path, env


def calls(system):
    log = Path(system[1]["FAKE_LOG"])
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def installer(system, *args):
    root, env = system
    shutil.copy(REPO / "install.sh", root / "install.sh")
    shutil.copy(REPO / "mnema.sh", root / "mnema.sh")
    shutil.copy(REPO / "compose.yaml", root / "compose.yaml")
    skip = shutil.ignore_patterns("__pycache__", "macos")
    shutil.copytree(REPO / "mnema", root / "mnema", ignore=skip, dirs_exist_ok=True)
    shutil.copytree(REPO / "scripts", root / "scripts", ignore=skip, dirs_exist_ok=True)
    return subprocess.run(
        ["bash", str(root / "install.sh"), *args],
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
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


def server_dir(system) -> Path:
    return Path(system[1]["HOME"]) / "mnema-server"


def docker(system) -> list[list[str]]:
    return [c[1:] for c in calls(system) if c[0] == "docker"]


def compose(system, *args: str) -> list[str]:
    return ["compose", "-f", str(server_dir(system) / "compose.yaml"), *args]


def compose_env(system) -> dict[str, str]:
    lines = (server_dir(system) / ".env").read_text().splitlines()
    return dict(line.split("=", 1) for line in lines)


def files_under(path: Path) -> set[str]:
    return {str(p.relative_to(path)) for p in path.rglob("*")}


def test_server_runs_in_docker_and_writes_only_its_folder(system):
    """./install.sh server: the published image, a free port of the host and its address, the
    container from compose.yaml in ~/mnema-server; no packages, no environment, no command on
    PATH, nothing else in the home folder. At the end an invitation from the container."""
    root, env = system
    done = installer(system, "server")
    assert done.returncode == 0, done.stderr
    image = "ghcr.io/fozt/mnema:latest"
    assert docker(system) == [
        ["compose", "version"],
        ["info"],
        ["pull", image],
        ["run", "--rm", "--network", "host", image, "python", "-m", "mnema.install_setup", "host"],
        compose(system, "up", "-d", "--wait"),
        ["exec", "-i", "mnema", "mnema", "model", "--show"],
        ["exec", "-i", "mnema", "mnema", "invite"],
    ]  # fmt: skip
    assert not [c for c in calls(system) if c[0] in ("uv", "apt-get", "sudo", "chown")]
    folder = server_dir(system)
    assert compose_env(system) == {
        "MNEMA_NAME": "mnema",
        "MNEMA_IMAGE": image,
        "MNEMA_PULL": "always",
        "MNEMA_PORT": "43117",
        "MNEMA_URL": "http://203.0.113.7:43117",
        "MNEMA_TZ": "Europe/Berlin",
        "MNEMA_UID": "1000",
        "MNEMA_GID": "1000",
    }
    assert (folder / "compose.yaml").read_text() == (REPO / "compose.yaml").read_text()
    assert oct(folder.stat().st_mode & 0o777) == oct((folder / "data").stat().st_mode & 0o777)
    assert folder.stat().st_mode & 0o777 == 0o700
    assert files_under(Path(env["HOME"])) == {
        "mnema-server", "mnema-server/compose.yaml", "mnema-server/.env", "mnema-server/data",
    }  # fmt: skip
    assert "http://203.0.113.7:43117#invite=one-time-code" in done.stdout
    assert "Сервер принимает подключения на порту 43117." in done.stdout
    assert "Модель можно выбрать позже: ./mnema.sh model" in done.stdout  # no terminal to ask in
    assert f"Команды сервера, в папке {root}:" in done.stdout
    assert "./mnema.sh logs" in done.stdout


def test_reinstall_keeps_the_port_and_the_address_given_with_url(system):
    """The port is picked once: devices keep reaching the same one. --url is the address devices
    reach the server at (a domain, a forwarded port): remembered, later installs keep it, while
    the detected address follows the network."""
    assert installer(system, "server").returncode == 0
    system[1]["FAKE_HOST"] = "45000 203.0.113.8"
    done = installer(system, "server", "--url", "http://mnema.example.org:8443")
    assert done.returncode == 0, done.stderr
    assert "http://mnema.example.org:8443#invite=one-time-code" in done.stdout
    assert "Сервер принимает подключения на порту 43117." in done.stdout
    system[1]["FAKE_HOST"] = "45000 203.0.113.9"
    done = installer(system, "server")
    assert done.returncode == 0, done.stderr
    assert compose_env(system)["MNEMA_PORT"] == "43117"
    assert compose_env(system)["MNEMA_URL"] == "http://203.0.113.9:43117"
    assert "http://mnema.example.org:8443#invite=one-time-code" in done.stdout


def test_server_without_an_address_asks_for_one_and_starts_nothing(system):
    system[1]["FAKE_HOST"] = "43117"
    done = installer(system, "server")
    assert done.returncode != 0
    assert "./install.sh server --url http://АДРЕС:43117" in done.stdout
    assert not [c for c in docker(system) if "up" in c]
    assert not server_dir(system).exists()


def test_server_without_an_address_reinstalls_with_the_remembered_one(system):
    assert installer(system, "server", "--url", "http://mnema.example.org:8443").returncode == 0
    system[1]["FAKE_HOST"] = "45000"
    done = installer(system, "server")
    assert done.returncode == 0, done.stderr
    assert "http://mnema.example.org:8443#invite=one-time-code" in done.stdout


def test_server_of_an_earlier_version_is_stopped_first(system):
    """0.3.0 ran the server as the systemd service mnema: the installer starts no second one."""
    system[1]["FAKE_OLD_SERVICE"] = "1"
    done = installer(system, "server")
    assert done.returncode != 0
    assert "Сначала остановите её: sudo systemctl disable --now mnema" in done.stdout
    assert docker(system)[-1] == ["info"]
    assert not server_dir(system).exists()


@pytest.mark.parametrize(("missing", "text"), [
    ("docker", "Установите Docker Engine с плагином Compose: https://docs.docker.com/engine/install/"),
    ("compose", "Установите Docker Engine с плагином Compose: https://docs.docker.com/engine/install/"),
    ("daemon", "Docker не запущен. Запустите его и установите снова:\n"
     "  sudo systemctl enable --now docker"),
    ("denied", "Добавьте его в группу docker, откройте новую оболочку и установите снова:\n"
     "  sudo usermod -aG docker "),
])  # fmt: skip
def test_server_without_docker_says_what_to_install(system, missing, text):
    root, env = system
    if missing == "docker":
        (root / "bin/docker").unlink()
    env["FAKE_DOCKER_MISSING"] = missing
    done = installer(system, "server")
    assert done.returncode != 0
    assert text in done.stdout
    assert not server_dir(system).exists()
    assert not (Path(env["HOME"]) / ".local").exists()


def test_server_built_here_or_given_as_a_local_image_is_not_pulled(system):
    root, env = system
    done = installer(system, "server", "--build")
    assert done.returncode == 0, done.stderr
    assert ["build", "-t", "mnema:local", str(root)] in docker(system)
    assert not [c for c in docker(system) if c[0] == "pull"]
    assert (compose_env(system)["MNEMA_IMAGE"], compose_env(system)["MNEMA_PULL"]) == (
        "mnema:local",
        "never",
    )

    env["MNEMA_IMAGE"] = "mnema-server:dev"
    assert installer(system, "server").returncode == 0
    assert not [c for c in docker(system) if c[0] == "pull"]
    assert (compose_env(system)["MNEMA_IMAGE"], compose_env(system)["MNEMA_PULL"]) == (
        "mnema-server:dev",
        "never",
    )


def test_root_runs_the_container_as_the_image_user(system):
    """The container never runs as root: as root the data folder goes to the image's user."""
    system[1]["FAKE_UID"] = "0"
    assert installer(system, "server").returncode == 0
    assert (compose_env(system)["MNEMA_UID"], compose_env(system)["MNEMA_GID"]) == ("1000", "1000")
    assert ["chown", "-R", "1000:1000", str(server_dir(system) / "data")] in calls(system)


def test_container_failure_is_not_reported_as_success(system):
    system[1]["FAKE_FAIL"] = "docker"
    done = installer(system, "server")
    assert done.returncode != 0
    assert "#invite=" not in done.stdout


@pytest.mark.parametrize(("os_name", "ok"), [("Linux", True), ("Darwin", False)])
def test_server_swap_only_when_asked_and_only_on_linux(system, os_name, ok):
    system[1]["FAKE_OS"] = os_name
    done = installer(system, "server", "--swap-size", "8G")
    assert (done.returncode == 0) is ok, done.stderr
    assert any(c[:2] == ["sudo", "bash"] and c[-1] == "8" for c in calls(system)) is ok
    assert installer(system, "server").returncode == 0 or not ok
    assert len([c for c in calls(system) if c[:2] == ["sudo", "bash"]]) == int(ok)


# the head of ~/mnema-server/mnema as ./install.sh server of 0.3.1 wrote it
OLD_SERVER_COMMAND = """#!/usr/bin/env bash
DIR=/home/me/mnema-server
# The mnema command on the server's machine. ./install.sh server copies it to ~/mnema-server/mnema,
set -euo pipefail
"""


def host_command(
    system, *args: str, tty: bool = False, given: str | None = None
) -> subprocess.CompletedProcess:
    """./mnema.sh in the folder of install.sh, from a terminal or from a script (given — what is
    piped into it)."""
    root, env = system
    if not (root / "mnema.sh").exists():
        shutil.copy(REPO / "mnema.sh", root / "mnema.sh")
        shutil.copytree(REPO / "scripts", root / "scripts", dirs_exist_ok=True)
    if not tty:
        return subprocess.run(
            [str(root / "mnema.sh"), *args],
            env=env,
            capture_output=True,
            text=True,
            input=given,
            stdin=None if given is not None else subprocess.DEVNULL,
        )
    leader, terminal = os.openpty()
    try:
        done = subprocess.run(
            [str(root / "mnema.sh"), *args],
            env=env,
            stdin=terminal,
            stdout=terminal,
            stderr=subprocess.PIPE,
            text=True,
        )
    finally:
        os.close(terminal)
        os.close(leader)
    return done


@pytest.mark.parametrize("tty", [False, True])
def test_host_command_runs_mnema_in_the_container(system, tty):
    """Every command but update and logs is mnema in the container, interactive from a
    terminal."""
    assert installer(system, "server").returncode == 0
    Path(system[1]["FAKE_LOG"]).write_text("")
    done = host_command(system, "devices", "revoke", "a b", tty=tty)
    assert done.returncode == 0, done.stderr
    flags = ["-it"] if tty else ["-i"]
    assert docker(system) == [["exec", *flags, "mnema", "mnema", "devices", "revoke", "a b"]]


def test_host_command_passes_piped_answers_to_the_container(system):
    """Without a terminal what is piped into ./mnema.sh still reaches the command."""
    assert installer(system, "server").returncode == 0
    done = host_command(system, "model", given="AIza-piped\n")
    assert done.returncode == 0, done.stderr
    assert done.stdout == "key: AIza-piped\n"


def test_host_command_finds_the_server_where_the_installer_put_it(system):
    """MNEMA_SERVER_DIR and MNEMA_NAME of the install: the commands reach that folder and that
    container; logs follow its log."""
    root, env = system
    env["MNEMA_SERVER_DIR"] = str(root / "elsewhere")
    env["MNEMA_NAME"] = "mnema-try"
    done = installer(system, "server", "--url", "http://127.0.0.1:43117")
    assert done.returncode == 0, done.stderr
    assert not (root / "home/mnema-server").exists()
    invite = ["exec", "-i", "mnema-try", "mnema", "invite", "--url", "http://127.0.0.1:43117"]
    assert invite in docker(system)
    del env["MNEMA_NAME"]  # the container's name is the installed one
    Path(env["FAKE_LOG"]).write_text("")
    assert "http://127.0.0.1:43117#invite=" in host_command(system, "invite").stdout
    assert host_command(system, "logs").returncode == 0
    assert docker(system) == [
        ["exec", "-i", "mnema-try", "mnema", "invite"],
        ["logs", "-f", "mnema-try"],
    ]


def test_host_command_without_a_server_says_to_install_it(system):
    done = host_command(system, "invite")
    assert done.returncode != 0
    folder = server_dir(system)
    assert (
        done.stdout
        == f"Сервер Mnema не установлен в {folder}. Установите его: ./install.sh server\n"
    )
    assert docker(system) == []


@pytest.mark.parametrize("built_here", [False, True])
def test_host_update_pulls_the_image_and_recreates_the_container(system, built_here):
    """./mnema.sh update: the newest image (compose pulls the published one, an image built here
    stays), the container recreated and healthy, the previous image removed."""
    root, env = system
    image = "mnema:local" if built_here else "ghcr.io/fozt/mnema:latest"
    assert installer(system, *(["server", "--build"] if built_here else ["server"])).returncode == 0
    Path(env["FAKE_LOG"]).write_text("")
    (root / "pulled").unlink(missing_ok=True)
    done = host_command(system, "update")
    assert done.returncode == 0, done.stderr
    expected = [
        compose(system, "config", "--images"),
        ["image", "inspect", "-f", "{{.Id}}", image],
        compose(system, "up", "-d", "--wait"),
        ["image", "inspect", "-f", "{{.Id}}", image],
        ["exec", "mnema", "mnema", "--version"],
    ]
    if not built_here:
        expected.insert(-1, ["image", "rm", "sha256:old"])
    assert docker(system) == expected
    assert "mnema 9.9.9" in done.stdout
    assert ("не скачивается: сервер перезапускается с ним как есть" in done.stdout) is built_here


def test_host_update_goes_back_when_the_new_image_does_not_start(system):
    """A new image that never becomes healthy: the previous one is tagged back and runs again."""
    root, env = system
    assert installer(system, "server").returncode == 0
    Path(env["FAKE_LOG"]).write_text("")
    (root / "pulled").unlink(missing_ok=True)
    env["FAKE_BROKEN_IMAGE"] = "1"
    done = host_command(system, "update")
    assert done.returncode != 0
    image = "ghcr.io/fozt/mnema:latest"
    assert docker(system) == [
        compose(system, "config", "--images"),
        ["image", "inspect", "-f", "{{.Id}}", image],
        compose(system, "up", "-d", "--wait"),
        ["image", "inspect", "-f", "{{.Id}}", image],
        ["tag", "sha256:old", image],
        compose(system, "up", "-d", "--wait", "--pull", "never"),
    ]
    assert "Новая версия не запустилась. Возвращаю прежнюю." in done.stdout


def client_sync(*extras: str) -> list[str]:
    """The client's uv sync: the locked set without the dev tools, with these extras."""
    options = [arg for extra in extras for arg in ("--extra", extra)]
    return ["uv", "sync", "--locked", "--python", "3.12", "--no-dev", *options]


# a Mac processes itself with the engines it ships (no GigaAM), Linux only sends recordings
MAC_SYNC = client_sync("menubar", "mcp", "parakeet", "whisper")
LINUX_SYNC = client_sync("menubar", "mcp")


@pytest.mark.parametrize(
    "args", [(), ("bogus",), ("client", "--bogus"), ("server", "http://server#invite=x"),
             ("server", "--swap-size", "0G"), ("server", "--swap-size", "4"),
             ("server", "--swap-size", "65G"), ("client", "--threads", "8"),
             ("server", "--threads", "8"), ("client", "--build"),
             ("client", "--url", "http://203.0.113.7:43117"),
             ("server", "--url", "http://host:1#invite=x"), ("server", "--url", "host:1"),
             ("server", "--url", "http://host:$PORT")],
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


@pytest.mark.parametrize(
    ("lang", "text"),
    [
        ({"LANG": "en_US.UTF-8"}, "--swap-size: 1G to 64G"),
        ({"LANG": "ru_RU.UTF-8"}, "--swap-size: от 1G до 64G"),
        ({"LC_ALL": "ru_RU.UTF-8", "LANG": "en_US.UTF-8"}, "--swap-size: от 1G"),
        ({"LC_ALL": "C", "LANG": "ru_RU.UTF-8"}, "--swap-size: 1G to 64G"),
    ],
)
def test_installer_speaks_the_language_of_the_locale(system, lang, text):
    _root, env = system
    for name in ("MNEMA_UI_LANG", "LC_ALL", "LC_MESSAGES", "LANG"):
        env.pop(name, None)
    env.update(lang)
    done = installer(system, "server", "--swap-size", "0G")
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
    assert node.claims == [
        {"invite": "one-time-code", "name": socket.gethostname(), "library": True}
    ]
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
    argv = ["install_setup", "client", "--connection", connection]
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


def test_client_links_the_mnema_command(system):
    """The app points ~/.local/bin/mnema at its environment's command; a reinstall moves the link,
    also one the server of 0.3.1 left."""
    link = Path(system[1]["HOME"]) / ".local/bin/mnema"
    link.parent.mkdir(parents=True)
    link.symlink_to(server_dir(system) / "mnema")
    done = installer(system, "client", "http://203.0.113.7:43117")
    assert done.returncode == 0, done.stderr
    assert Path(os.readlink(link)) == app_env(system) / "bin/mnema"


def test_server_removes_the_command_of_0_3_1(system):
    """The server of 0.3.1 put its command into its folder and linked ~/.local/bin/mnema to it:
    a reinstall removes both, the commands are ./mnema.sh now."""
    link = Path(system[1]["HOME"]) / ".local/bin/mnema"
    link.parent.mkdir(parents=True)
    server_dir(system).mkdir()
    (server_dir(system) / "mnema").write_text(OLD_SERVER_COMMAND)
    link.symlink_to(server_dir(system) / "mnema")
    done = installer(system, "server")
    assert done.returncode == 0, done.stderr
    assert not link.is_symlink()
    assert not (server_dir(system) / "mnema").exists()


def test_server_keeps_other_files_in_its_folder_and_links_into_it(system):
    """Only the command of 0.3.1 and the link to it go: someone's own mnema file in the server's
    folder and a link to another file there stay."""
    link = Path(system[1]["HOME"]) / ".local/bin/mnema"
    link.parent.mkdir(parents=True)
    server_dir(system).mkdir()
    (server_dir(system) / "mnema").write_text("#!/bin/sh\necho mine\n")
    (server_dir(system) / "tool").write_text("#!/bin/sh\n")
    link.symlink_to(server_dir(system) / "tool")
    done = installer(system, "server")
    assert done.returncode == 0, done.stderr
    assert (server_dir(system) / "mnema").read_text() == "#!/bin/sh\necho mine\n"
    assert Path(os.readlink(link)) == server_dir(system) / "tool"


@pytest.mark.parametrize("foreign", ["file", "link"])
def test_server_leaves_a_foreign_mnema_command_alone(system, foreign):
    link = Path(system[1]["HOME"]) / ".local/bin/mnema"
    link.parent.mkdir(parents=True)
    other = system[0] / "other/bin/mnema"
    if foreign == "file":
        link.write_text("#!/bin/sh\n")
    else:
        link.symlink_to(other)
    done = installer(system, "server")
    assert done.returncode == 0, done.stderr
    if foreign == "file":
        assert link.read_text() == "#!/bin/sh\n"
    else:
        assert Path(os.readlink(link)) == other
    assert "#invite=one-time-code" in done.stdout


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
