"""The server image (Dockerfile): its start (python -m mnema.install_setup container), the
healthcheck, and what the installer asks the image on the host's network.

External boundaries: the exec into `mnema serve` (os.execv), the network interfaces, an HTTP
server on loopback for the healthcheck."""

import http.server
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest
from dotenv import dotenv_values

from mnema import install_setup, node
from mnema.cli.parser import build_parser
from mnema.connection import parse_connection
from mnema.devices import DeviceStore


@pytest.fixture
def container(tmp_path, monkeypatch):
    """A fresh /data volume with the environment the Dockerfile sets."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("MNEMA_SERVER_DIR", str(tmp_path))
    monkeypatch.setenv("MNEMA_LIBRARY", "true")
    monkeypatch.setenv("MNEMA_CONTAINER", "true")
    monkeypatch.delenv("MNEMA_URL", raising=False)
    monkeypatch.delenv("MNEMA_TOKEN", raising=False)
    # the container's own network: an address no device can reach
    monkeypatch.setattr(node, "interfaces", lambda: [("eth0", "172.18.0.2")])
    started: list[list[str]] = []

    def execv(path, argv):
        assert path == argv[0]
        started.append(list(argv))

    monkeypatch.setattr(os, "execv", execv)
    mask = os.umask(0o022)  # the start sets the process umask
    yield tmp_path, started
    os.umask(mask)


def start(monkeypatch, capsys) -> str:
    monkeypatch.setattr(sys, "argv", ["install_setup", "container"])
    install_setup.main()
    return capsys.readouterr().out


def invite_in(out: str) -> str | None:
    return next((line for line in out.splitlines() if "#invite=" in line), None)


def cli(*argv: str) -> None:
    args = build_parser().parse_args(list(argv))
    args.func(args)


def test_first_start_sets_up_the_server_and_invites_to_mnema_url(container, monkeypatch, capsys):
    data, started = container
    monkeypatch.setenv("MNEMA_URL", "http://203.0.113.5:38837")
    out = start(monkeypatch, capsys)

    server_env = data / "config/mnema/server.env"
    token = dotenv_values(server_env)["MNEMA_TOKEN"]
    assert token and server_env.stat().st_mode & 0o777 == 0o600
    assert (data / "library").stat().st_mode & 0o777 == 0o700
    invite = parse_connection(invite_in(out) or "")
    assert invite.url == "http://203.0.113.5:38837" and invite.invite
    assert token not in out
    mnema = str(Path(sys.prefix) / "bin/mnema")
    assert started == [[mnema, "serve", "--host", "0.0.0.0", "--dir", str(data)]]


def test_restart_keeps_the_token_and_stops_inviting_once_a_device_joined(
    container, monkeypatch, capsys
):
    data, started = container
    monkeypatch.setenv("MNEMA_URL", "http://203.0.113.5:38837")
    first = invite_in(start(monkeypatch, capsys))
    env = data / "config/mnema/server.env"
    token = dotenv_values(env)["MNEMA_TOKEN"]

    # nobody joined: the next start invites again
    again = invite_in(start(monkeypatch, capsys))
    assert again and again != first
    code = parse_connection(again).invite or ""
    assert DeviceStore(data / ".mnema/devices.db").claim(code, "Pixel", now=time.time())

    assert invite_in(start(monkeypatch, capsys)) is None
    assert env.read_text() == f'MNEMA_TOKEN="{token}"\n'
    assert len(started) == 3


def test_the_first_start_invites_the_owner_until_the_owner_has_a_device(
    container, monkeypatch, capsys
):
    """A friend's device is not the owner's: the start still invites the owner."""
    data, _started = container
    monkeypatch.setenv("MNEMA_URL", "http://203.0.113.5:38837")
    owner = parse_connection(invite_in(start(monkeypatch, capsys)) or "").invite or ""
    store = DeviceStore(data / ".mnema/devices.db")
    store.add_user("anna", now=time.time())
    code, _ = store.create_invite(user="anna", now=time.time())
    assert store.claim(code, "Pixel", now=time.time(), library=True)

    assert invite_in(start(monkeypatch, capsys))
    assert store.claim(owner, "Mac", now=time.time())
    assert invite_in(start(monkeypatch, capsys)) is None


def test_the_address_given_to_mnema_invite_wins_and_is_remembered(container, monkeypatch, capsys):
    """Inside a container the network address is the container's own: no invitation leads
    there. MNEMA_URL is the address; one given with mnema invite --url leads every later
    invitation, also after a restart."""
    _data, _started = container
    out = start(monkeypatch, capsys)
    assert invite_in(out) is None
    assert "./mnema.sh invite --url http://АДРЕС:ПОРТ" in out
    with pytest.raises(SystemExit):
        cli("invite")

    monkeypatch.setenv("MNEMA_URL", "http://203.0.113.5:38837")
    cli("invite")
    assert parse_connection(invite_in(capsys.readouterr().out) or "").url == (
        "http://203.0.113.5:38837"
    )
    cli("invite", "--url", "http://mnema.example.org:8443")
    cli("invite")
    out = capsys.readouterr().out.splitlines()
    assert {parse_connection(line).url for line in out if "#invite=" in line} == {
        "http://mnema.example.org:8443"
    }
    assert parse_connection(invite_in(start(monkeypatch, capsys)) or "").url == (
        "http://mnema.example.org:8443"
    )


def test_healthcheck_asks_this_server_with_its_token(container, monkeypatch, capsys):
    data, _started = container
    seen: list[str | None] = []
    status = {"code": 200}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers.get("Authorization"))
            body = json.dumps({"ok": True, "llm": False, "name": "Server"}).encode()
            self.send_response(status["code"] if self.path == "/api/capabilities" else 404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(install_setup, "DEFAULT_PORT", server.server_address[1])
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    try:
        start(monkeypatch, capsys)
        token = dotenv_values(data / "config/mnema/server.env")["MNEMA_TOKEN"]
        assert install_setup.container_healthy()
        assert seen == [f"Bearer {token}"]
        status["code"] = 401
        assert not install_setup.container_healthy()
    finally:
        server.shutdown()
        server.server_close()


def test_the_installer_learns_a_free_port_and_the_hosts_address(monkeypatch, capsys):
    """Run on the host's network: a port nothing listens on and the address by node_address."""
    monkeypatch.setattr(
        node, "interfaces", lambda: [("docker0", "172.17.0.1"), ("eth0", "203.0.113.5")]
    )
    monkeypatch.setattr(sys, "argv", ["install_setup", "host"])
    install_setup.main()
    port, address = capsys.readouterr().out.split()
    assert 0 < int(port) < 65536 and address == "203.0.113.5"

    monkeypatch.setattr(node, "interfaces", lambda: [])
    install_setup.main()
    assert capsys.readouterr().out.split()[0].isdigit()


def test_the_server_names_its_commands_as_run_on_its_machine(container, monkeypatch, capsys):
    """The server's machine has no mnema command: the container's output says ./mnema.sh (the
    folder of install.sh)."""
    start(monkeypatch, capsys)
    cli("devices")
    assert "Новое приглашение: ./mnema.sh invite" in capsys.readouterr().out
