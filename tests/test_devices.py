"""Invitations and device tokens: one-time exchange, hashes on disk, access to the node API."""

import hashlib
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from mnema.connection import parse_connection
from mnema.core.base import MediaError
from mnema.devices import DeviceStore, OutdatedClient, User
from mnema.server import create_app
from mnema.users import Caller

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
    assert store.verify(token, now=NOW) == Caller(device=claimed[0], user="owner")


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
    assert store.verify(token, now=NOW + 10) == Caller(device=device_id, user="owner")
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


_V0_SCHEMA = """
CREATE TABLE invites (
    hash TEXT PRIMARY KEY, created REAL NOT NULL, expires REAL NOT NULL,
    used_at REAL, device_id TEXT
);
CREATE TABLE devices (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
    created REAL NOT NULL, last_seen REAL, revoked INTEGER NOT NULL DEFAULT 0
);
"""


def _sha(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _old_database(root: Path) -> Path:
    """devices.db of a server before users: one device and one unused invitation."""
    db = root / ".mnema" / "devices.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.executescript(_V0_SCHEMA)
    con.execute(
        "INSERT INTO devices (id, name, token_hash, created) VALUES (?, ?, ?, ?)",
        ("old-device", "Pixel", _sha("old-token"), NOW),
    )
    con.execute(
        "INSERT INTO invites (hash, created, expires) VALUES (?, ?, ?)",
        (_sha("old-code"), NOW, NOW + 3600),
    )
    con.commit()
    con.close()
    db.chmod(0o600)
    return db


def _version(db: Path) -> int:
    con = sqlite3.connect(db)
    try:
        return con.execute("PRAGMA user_version").fetchone()[0]
    finally:
        con.close()


def test_an_old_database_belongs_to_the_owner_with_the_same_tokens(tmp_path):
    db = _old_database(tmp_path)
    store = DeviceStore(db)
    assert store.verify("old-token", now=NOW) == Caller(device="old-device", user="owner")
    assert _version(db) == 1
    [owner] = store.users()
    assert owner.name == "owner" and owner.library and not owner.removed
    claimed = store.claim("old-code", "ПК", now=NOW + 1)
    assert claimed is not None
    assert store.verify(claimed[1], now=NOW + 1) == Caller(device=claimed[0], user="owner")
    assert DeviceStore(db).users() == [owner]
    assert {d.user for d in store.devices()} == {"owner"}


@pytest.mark.parametrize("old", [True, False], ids=["old", "fresh"])
def test_two_servers_opening_one_database_at_once(tmp_path, old):
    db = _old_database(tmp_path) if old else tmp_path / ".mnema" / "devices.db"
    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def open_store() -> None:
        barrier.wait()
        try:
            DeviceStore(db)
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=open_store) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert _version(db) == 1
    assert [u.name for u in DeviceStore(db).users()] == ["owner"]


def test_an_invitation_of_a_user_gives_a_device_of_that_user(tmp_path):
    store = _store(tmp_path)
    anna, returned = store.add_user("anna", now=NOW)
    owner = store.user("owner")
    assert owner is not None
    assert (anna.name, returned) == ("anna", False) and anna.library not in ("", owner.library)
    code, _ = store.create_invite(user="anna", now=NOW)
    with pytest.raises(OutdatedClient):
        store.claim(code, "Pixel", now=NOW)
    claimed = store.claim(code, "Pixel", now=NOW, library=True)
    assert claimed is not None
    assert store.verify(claimed[1], now=NOW) == Caller(device=claimed[0], user="anna")
    assert [(d.id, d.user) for d in store.devices("anna")] == [(claimed[0], "anna")]
    assert store.devices("owner") == []
    assert store.claim(store.create_invite(now=NOW)[0], "Mac", now=NOW) is not None
    assert [u.name for u in store.users()] == ["owner", "anna"]
    with pytest.raises(ValueError):
        store.add_user("anna", now=NOW)
    with pytest.raises(ValueError):
        store.create_invite(user="bob", now=NOW)


def test_a_removed_user_loses_devices_and_invitations_and_comes_back_with_the_library(tmp_path):
    store = _store(tmp_path)
    anna, _ = store.add_user("anna", now=NOW)
    claimed = store.claim(
        store.create_invite(user="anna", now=NOW)[0], "Pixel", now=NOW, library=True
    )
    assert claimed is not None
    unused, _ = store.create_invite(user="anna", now=NOW)
    mine = store.claim(store.create_invite(now=NOW)[0], "Mac", now=NOW)
    assert mine is not None

    assert store.remove_user("anna") is True
    assert store.verify(claimed[1], now=NOW) is None
    assert store.claim(unused, "Phone", now=NOW, library=True) is None
    assert store.verify(mine[1], now=NOW) == Caller(device=mine[0], user="owner")
    assert [u.name for u in store.users()] == ["owner"]
    assert store.user("anna") == User(name="anna", library=anna.library, created=NOW, removed=True)
    assert store.remove_user("anna") is False and store.remove_user("bob") is False
    with pytest.raises(ValueError):
        store.create_invite(user="anna", now=NOW)
    with pytest.raises(ValueError):
        store.remove_user("owner")

    back, returned = store.add_user("anna", now=NOW + 5)
    assert returned and back.library == anna.library and not back.removed
    assert store.verify(claimed[1], now=NOW) is None
    again = store.claim(
        store.create_invite(user="anna", now=NOW)[0], "Pixel", now=NOW, library=True
    )
    assert again is not None and store.verify(again[1], now=NOW) is not None


def test_an_invitation_of_a_user_needs_an_app_that_knows_libraries(node):
    root, client = node
    store = _store(root)
    store.add_user("anna", now=NOW)
    code, _ = store.create_invite(user="anna", now=time.time())
    old = _claim(client, code)
    assert old.status_code == 403
    assert old.json() == {"detail": "Обновите приложение, чтобы подключиться по этому приглашению"}
    new = client.post("/api/devices/claim", json={"invite": code, "name": "Pixel", "library": True})
    assert new.status_code == 200
    assert client.get("/api/capabilities", headers=_bearer(new.json()["token"])).status_code == 200
    again = client.post("/api/devices/claim", json={"invite": code, "name": "X", "library": True})
    assert again.status_code == 403 and again.json() == {"detail": REFUSED}
    mine = store.create_invite(now=time.time())[0]
    assert _claim(client, mine).status_code == 200


URL = "http://node.example:8000"


@pytest.fixture
def server(tmp_path, monkeypatch):
    """A server folder with a server token: users need one."""
    monkeypatch.setenv("MNEMA_TOKEN", NODE)
    return _node_dir(tmp_path)


def _said(out: str) -> str:
    return " ".join(out.split())


def test_users_add_invites_a_device_of_the_new_user(server, capsys):
    _cli("users", "add", "Anna", "--dir", str(server), "--url", URL, "--hours", "2")
    out = capsys.readouterr().out
    assert "Приглашение для одного устройства пользователя anna, действует до " in out
    library = server / "users" / "anna" / "library"
    assert library.is_dir() and library.stat().st_mode & 0o777 == 0o700
    parsed = parse_connection(_printed_invite(out))
    assert parsed.url == URL and parsed.invite is not None
    claimed = _store(server).claim(parsed.invite, "Pixel", now=time.time(), library=True)
    assert claimed is not None
    assert _store(server).verify(claimed[1], now=time.time()) == Caller(
        device=claimed[0], user="anna"
    )

    with pytest.raises(SystemExit):
        _cli("users", "add", "anna", "--dir", str(server), "--url", URL)
    assert "Пользователь anna уже есть. Приглашение: mnema invite --user anna" in _said(
        capsys.readouterr().out
    )
    _cli("invite", "--user", "anna", "--dir", str(server), "--url", URL)
    code = parse_connection(_printed_invite(capsys.readouterr().out)).invite or ""
    second = _store(server).claim(code, "Mac", now=time.time(), library=True)
    assert second is not None
    assert _store(server).verify(second[1], now=time.time()).user == "anna"


def test_a_returned_user_finds_her_recordings(server, capsys):
    _cli("users", "add", "anna", "--dir", str(server), "--url", URL)
    record = server / "users" / "anna" / "library" / "2026-10-31_Talk"
    record.mkdir()
    (record / ".mnema-meta.json").write_text('{"id": "e4b45222-14d0-4ae5-a581-8c746c5f305f"}')
    (record / "transcript.txt").write_text("Привет\n")
    _cli("users", "remove", "anna", "--dir", str(server))
    capsys.readouterr()
    _cli("users", "add", "anna", "--dir", str(server), "--url", URL)
    out = capsys.readouterr().out
    assert "Пользователь anna вернулся с 1 записью" in _said(out)
    assert parse_connection(_printed_invite(out)).invite
    assert (record / "transcript.txt").read_text() == "Привет\n"


def _rows(out: str) -> dict[str, list[str]]:
    return {
        cells[0]: cells[1:]
        for line in out.splitlines()
        if (cells := line.split()) and cells[0] in ("owner", "anna", "bob")
    }


def test_users_lists_people_with_their_devices_and_remove_hides_them(server, capsys):
    store = _store(server)
    store.add_user("anna", now=NOW)
    store.add_user("bob", now=NOW)
    for name in ("Pixel", "Mac"):
        store.claim(store.create_invite(user="anna", now=NOW)[0], name, now=NOW, library=True)
    _cli("users", "--dir", str(server))
    assert _rows(capsys.readouterr().out) == {
        "owner": ["0", "общая"], "anna": ["2", "общая"], "bob": ["0", "общая"],
    }  # fmt: skip

    _cli("users", "remove", "anna", "--dir", str(server))
    capsys.readouterr()
    _cli("users", "--dir", str(server))
    assert set(_rows(capsys.readouterr().out)) == {"owner", "bob"}
    for name in ("anna", "nobody"):
        with pytest.raises(SystemExit):
            _cli("users", "remove", name, "--dir", str(server))
        assert f"Нет пользователя {name}. Добавьте: mnema users add {name}" in _said(
            capsys.readouterr().out
        )
    with pytest.raises(SystemExit):
        _cli("users", "remove", "owner", "--dir", str(server))
    assert "Владельца сервера удалить нельзя" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        _cli("invite", "--user", "anna", "--dir", str(server), "--url", URL)
    assert "Нет пользователя anna. Добавьте: mnema users add anna" in _said(capsys.readouterr().out)


def test_a_bad_user_name_is_refused(server, capsys):
    with pytest.raises(SystemExit):
        _cli("users", "add", "анна", "--dir", str(server), "--url", URL)
    assert "латинские буквы" in capsys.readouterr().out
    assert [u.name for u in _store(server).users()] == ["owner"]


def test_users_need_a_server_with_a_token(tmp_path, capsys):
    _node_dir(tmp_path)
    for argv in (("users", "add", "anna"), ("invite", "--user", "anna")):
        with pytest.raises(SystemExit):
            _cli(*argv, "--dir", str(tmp_path), "--url", URL)
        assert "У сервера нет токена: он нужен пользователям" in _said(capsys.readouterr().out)
    assert [u.name for u in _store(tmp_path).users()] == ["owner"]


def test_devices_command_names_the_user_of_each_device(server, capsys):
    store = _store(server)
    store.add_user("anna", now=NOW)
    friend = store.claim(
        store.create_invite(user="anna", now=NOW)[0], "Pixel", now=NOW, library=True
    )
    mine = store.claim(store.create_invite(now=NOW)[0], "Mac", now=NOW)
    assert friend is not None and mine is not None
    _cli("devices", "--dir", str(server))
    lines = capsys.readouterr().out.splitlines()
    assert any("Pixel" in line and "anna" in line and friend[0] in line for line in lines)
    assert any("Mac" in line and "owner" in line and mine[0] in line for line in lines)
