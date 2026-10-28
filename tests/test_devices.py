"""Invitations and device tokens: one-time exchange, hashes on disk, access to the node API."""

import json
import logging
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from mnema.connection import parse_connection
from mnema.core.base import MediaError
from mnema.devices import DeviceStore
from mnema.server import create_app

NOW = 1_800_000_000.0
NODE = "node-token"
REFUSED = "Приглашение недействительно: попросите новое на сервере"


def _store(root: Path) -> DeviceStore:
    return DeviceStore(root / ".mnema" / "devices.db")


@pytest.fixture
def node(tmp_path, monkeypatch):
    """A node with MNEMA_TOKEN: /api/* only with a token."""
    monkeypatch.setenv("MNEMA_TOKEN", NODE)
    app = create_app(tmp_path)
    return tmp_path, TestClient(app)


def _claim(client: TestClient, code: str, name: str = "Pixel 8") -> httpx.Response:
    return client.post("/api/devices/claim", json={"invite": code, "name": name})


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_invite_is_exchanged_for_device_token_only_once(node, caplog):
    caplog.set_level(logging.DEBUG)
    root, client = node
    code, _ = _store(root).create_invite(now=time.time())
    first = _claim(client, code)
    assert first.status_code == 200
    body = first.json()
    assert body["device"]["name"] == "Pixel 8" and body["device"]["id"]
    assert body["token"] and body["token"] != code
    again = _claim(client, code)
    assert again.status_code == 403 and again.json() == {"detail": REFUSED}
    assert code not in again.text
    assert code not in caplog.text and body["token"] not in caplog.text
    assert [d.id for d in _store(root).devices()] == [body["device"]["id"]]


def test_expired_and_unknown_invites_get_the_same_refusal(node):
    root, client = node
    expired, _ = _store(root).create_invite(1, now=time.time() - 3601)
    for code in (expired, "unknown-code", ""):
        response = _claim(client, code)
        assert response.status_code == 403 and response.json() == {"detail": REFUSED}
    assert _store(root).devices() == []


@pytest.mark.parametrize(
    "content",
    [
        b"not json",
        b"[]",
        json.dumps({"name": "x"}).encode(),
        json.dumps({"invite": 5, "name": "x"}).encode(),
        json.dumps({"invite": "a", "name": 5}).encode(),
        json.dumps({"invite": "a" * 10_000}).encode(),
    ],
    ids=["text", "list", "no-invite", "invite-number", "name-number", "huge"],
)
def test_malformed_claim_is_refused_without_echo(node, content):
    _, client = node
    response = client.post(
        "/api/devices/claim", content=content, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 403 and response.json() == {"detail": REFUSED}


def test_device_name_is_cleaned(tmp_path):
    store = _store(tmp_path)
    raw = "  Pixel\n8\x1b[31m ‮" + "я" * 60
    store.claim(store.create_invite(now=NOW)[0], raw, now=NOW)
    store.claim(store.create_invite(now=NOW)[0], " \t\n", now=NOW + 1)
    names = [d.name for d in store.devices()]
    assert names[0].startswith("Pixel 8[31m") and len(names[0]) == 40
    assert not any(ord(c) < 32 or c == "‮" for c in names[0])
    assert names[1] == "Устройство"


def test_invite_lifetime_is_bounded(tmp_path):
    store = _store(tmp_path)
    assert store.create_invite(now=NOW)[1] == NOW + 6 * 3600
    assert store.create_invite(72, now=NOW)[1] == NOW + 72 * 3600
    for hours in (0, -1, 72.5, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            store.create_invite(hours, now=NOW)
    code, expires = store.create_invite(0.5, now=NOW)
    assert store.claim(code, "x", now=expires) is None


def test_parallel_claims_of_one_invite_give_exactly_one_device(tmp_path):
    """Eight clients with one code at once: exactly one gets a device."""
    code, _ = _store(tmp_path).create_invite(now=NOW)
    barrier = threading.Barrier(8)
    results: list = []

    def claim(i: int) -> None:
        store = _store(tmp_path)
        barrier.wait()
        results.append(store.claim(code, f"Клиент {i}", now=NOW + 1))

    threads = [threading.Thread(target=claim, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) == 8
    assert len([r for r in results if r is not None]) == 1
    assert len(_store(tmp_path).devices()) == 1


def test_database_keeps_only_hashes_and_is_private(tmp_path):
    store = _store(tmp_path)
    code, _ = store.create_invite(now=NOW)
    other, _ = store.create_invite(now=NOW)
    claimed = store.claim(code, "ПК", now=NOW)
    assert claimed is not None
    _, token = claimed
    db = tmp_path / ".mnema" / "devices.db"
    assert db.stat().st_mode & 0o777 == 0o600
    stored = b"".join(p.read_bytes() for p in db.parent.glob("devices.db*"))
    for secret in (code, other, token):
        assert secret.encode() not in stored
    assert store.verify(token, now=NOW) == claimed[0]


def test_device_token_opens_api_and_revoke_closes_it_at_once(node):
    root, client = node
    code, _ = _store(root).create_invite(now=time.time())
    body = _claim(client, code).json()
    headers = _bearer(body["token"])
    assert client.get("/api/capabilities", headers=headers).status_code == 200
    assert _store(root).revoke(body["device"]["id"]) is True
    assert client.get("/api/capabilities", headers=headers).status_code == 401
    assert _store(root).devices()[0].revoked is True
    assert _store(root).revoke("missing") is False


def test_without_token_only_claim_is_reachable(node):
    """Every /api/* route without a token is 401; only POST /api/devices/claim is open."""
    _, client = node
    routes = [
        (method, route.path)
        for route in client.app.routes
        if isinstance(route, APIRoute) and route.path.startswith("/api/")
        for method in route.methods
    ]
    assert ("POST", "/api/devices/claim") in routes
    for method, path in routes:
        if (method, path) == ("POST", "/api/devices/claim"):
            continue
        url = path.replace("{", "").replace("}", "")
        assert client.request(method, url).status_code == 401, (method, path)
    for method in ("GET", "PUT", "DELETE"):
        assert client.request(method, "/api/devices/claim").status_code == 401
    assert client.post("/api/devices/claim/", json={}).status_code == 401
    assert _claim(client, "nope").status_code == 403


def test_last_seen_is_written_at_most_once_a_minute(tmp_path):
    store = _store(tmp_path)
    claimed = store.claim(store.create_invite(now=NOW)[0], "ПК", now=NOW)
    assert claimed is not None
    device_id, token = claimed
    assert store.verify(token, now=NOW + 10) == device_id
    assert store.devices()[0].last_seen == NOW + 10
    store.verify(token, now=NOW + 40)
    assert store.devices()[0].last_seen == NOW + 10
    store.verify(token, now=NOW + 71)
    assert store.devices()[0].last_seen == NOW + 71
    assert store.verify("other", now=NOW) is None and store.verify("", now=NOW) is None


def test_client_claims_invite_without_bearer(node):
    from mnema.client import MnemaClient

    root, client = node
    code, _ = _store(root).create_invite(now=time.time())
    seen: list[httpx.Request] = []

    def forward(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        response = client.request(
            request.method,
            request.url.path,
            content=request.content,
            headers={"Content-Type": request.headers.get("content-type", "")},
        )
        return httpx.Response(response.status_code, content=response.content)

    remote = MnemaClient("http://node", token="old", transport=httpx.MockTransport(forward))
    token = remote.claim_invite(code, "ПК")
    assert "authorization" not in seen[0].headers
    assert client.get("/api/capabilities", headers=_bearer(token)).status_code == 200
    with pytest.raises(MediaError) as error:
        remote.claim_invite(code, "ПК")
    assert REFUSED in str(error.value) and code not in str(error.value)


def _cli(*argv: str) -> None:
    from mnema.cli.parser import build_parser

    args = build_parser().parse_args(list(argv))
    args.func(args)


def _printed_invite(out: str) -> str:
    return next(line for line in out.splitlines() if line.startswith("http"))


def _node_dir(path: Path) -> Path:
    """A folder where mnema serve has run: it has .mnema."""
    (path / ".mnema").mkdir(parents=True, exist_ok=True)
    return path


def test_invite_command_prints_a_string_the_client_accepts(tmp_path, capsys):
    _node_dir(tmp_path)
    _cli("invite", "--dir", str(tmp_path), "--url", "http://node.example:8000", "--hours", "2")
    out = capsys.readouterr().out
    assert "Приглашение для одного устройства, действует до " in out
    parsed = parse_connection(_printed_invite(out))
    assert parsed.url == "http://node.example:8000" and parsed.token is None
    assert parsed.invite is not None
    assert _store(tmp_path).claim(parsed.invite, "ПК", now=time.time()) is not None


def test_invite_leads_to_the_address_the_server_has_now(tmp_path, monkeypatch, capsys):
    """A server outside the image, without --url: this computer's current network address and
    the server port, so after a network change the invitation leads to the new address; no
    address needs --url, and an explicit address leads the next invitations too."""
    from mnema import node

    root = _node_dir(tmp_path / "mnema-server")
    network = [("eth0", "192.168.1.5")]
    monkeypatch.setattr(node, "interfaces", lambda: network)
    _cli("invite")
    parsed = parse_connection(_printed_invite(capsys.readouterr().out))
    assert parsed.url == "http://192.168.1.5:8000"
    assert _store(root).claim(parsed.invite, "ПК", now=time.time()) is not None

    network[:] = [("eth0", "192.168.7.20"), ("tailscale0", "100.64.0.7")]
    _cli("invite")
    assert (
        parse_connection(_printed_invite(capsys.readouterr().out)).url == "http://100.64.0.7:8000"
    )

    network.clear()
    with pytest.raises(SystemExit):
        _cli("invite")
    _cli("invite", "--url", "http://vps.example:43117")
    assert parse_connection(_printed_invite(capsys.readouterr().out)).url == (
        "http://vps.example:43117"
    )
    network[:] = [("eth0", "192.168.1.5")]
    _cli("invite")
    assert parse_connection(_printed_invite(capsys.readouterr().out)).url == (
        "http://vps.example:43117"
    )


def test_invite_for_a_server_in_another_folder_needs_its_own_address(tmp_path, monkeypatch, capsys):
    """A server in another folder (--dir) is not this computer's server: its address is not
    known, --url is needed, and it does not become this computer's server address."""
    from mnema import node

    _node_dir(tmp_path / "mnema-server")
    data = _node_dir(tmp_path / "data")
    monkeypatch.setattr(node, "interfaces", lambda: [("eth0", "203.0.113.11")])
    with pytest.raises(SystemExit):
        _cli("invite", "--dir", str(data))
    _cli("invite", "--dir", str(data), "--url", "http://gpu.example:8000")
    parsed = parse_connection(_printed_invite(capsys.readouterr().out))
    assert parsed.url == "http://gpu.example:8000"
    assert _store(data).claim(parsed.invite, "ПК", now=time.time()) is not None
    _cli("invite")
    assert parse_connection(_printed_invite(capsys.readouterr().out)).url == (
        "http://203.0.113.11:8000"
    )


@pytest.mark.parametrize(
    "argv",
    [
        ("--url", "http://host#token=secret"),
        ("--url", "ftp://host"),
        ("--hours", "0"),
        ("--hours", "100"),
    ],
)
def test_invite_command_rejects_bad_options(tmp_path, argv, capsys):
    _node_dir(tmp_path)
    with pytest.raises(SystemExit):
        _cli("invite", "--dir", str(tmp_path), "--url", "http://host:8000", *argv)
    out = capsys.readouterr().out
    assert "secret" not in out and "#invite=" not in out


@pytest.mark.parametrize("command", [("invite", "--url", "http://host:8000"), ("devices",)])
def test_node_commands_need_a_folder_where_the_node_runs(tmp_path, command):
    """A typo in --dir is refused rather than creating an empty database the node never sees."""
    plain = tmp_path / "plain"
    plain.mkdir()
    for folder in (tmp_path / "missing", plain):
        with pytest.raises(SystemExit):
            _cli(*command, "--dir", str(folder))
    assert not (tmp_path / "missing").exists() and list(plain.iterdir()) == []


def test_devices_command_lists_and_revokes(tmp_path, capsys):
    store = _store(tmp_path)
    claimed = store.claim(store.create_invite(now=NOW)[0], "Pixel 8", now=NOW)
    assert claimed is not None
    device_id, token = claimed
    _cli("devices", "--dir", str(tmp_path))
    listing = capsys.readouterr().out
    assert "Pixel 8" in listing and device_id in listing and token not in listing
    _cli("devices", "revoke", device_id, "--dir", str(tmp_path))
    assert "Pixel 8" in capsys.readouterr().out
    assert store.verify(token, now=NOW) is None
    with pytest.raises(SystemExit):
        _cli("devices", "revoke", "missing", "--dir", str(tmp_path))
    with pytest.raises(SystemExit):
        _cli("devices", "revoke", "--dir", str(tmp_path))
