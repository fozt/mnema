"""HTTP contract of the shared file library: errors, tokens and re-reads."""

import json
import os
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mnema.server import create_app

FIXTURE = json.loads((Path(__file__).parent / "fixtures/library-v1.json").read_text())
ID = FIXTURE["create"]["id"]
BASE = f"/api/library/records/{ID}"


@pytest.fixture
def node(tmp_path, monkeypatch):
    monkeypatch.setenv("MNEMA_LIBRARY", "true")
    (tmp_path / "library").mkdir()  # the installer creates the folder
    app = create_app(tmp_path)
    return app, TestClient(app)


def served(app, manifest: dict) -> dict:
    """The fixture manifest as this server tells it: with the owner's library number."""
    return {**manifest, "library": app.state.devices.user("owner").library}


def create(client):
    result = client.post("/api/library/records", json=FIXTURE["create"])
    assert result.status_code == 200, result.text
    return result.json()


def test_shared_fixture_roundtrip_and_lost_response_readback(node):
    app, client = node
    record = create(client)
    assert record == FIXTURE["manifest"]["records"][0]
    assert client.get("/api/library").json() == served(app, FIXTURE["manifest"])
    for name, body in FIXTURE["create"]["files"].items():
        response = client.get(f"{BASE}/files/{name}")
        assert response.content == body["text"].encode()
        assert response.headers["X-Mnema-Mtime-Ns"] == str(body["mtime_ns"])
        assert response.headers["ETag"] == '"' + record["files"][name]["sha256"] + '"'
        assert (app.state.library.root / record["path"] / name).read_text() == body["text"]
    response = client.put(
        f"{BASE}/files/summary.txt",
        content="Исправленный пересказ".encode(),
        headers={
            "Content-Type": "text/plain",
            "X-Mnema-Mtime-Ns": "123456789",
            "If-Match": record["files"]["summary.txt"]["sha256"],
        },
    )
    assert response.status_code == 200
    # The answer is lost: the next GET confirms the write, and the original time is kept.
    found = client.get(f"{BASE}/files/summary.txt")
    assert found.text == "Исправленный пересказ"
    assert found.headers["X-Mnema-Mtime-Ns"] == "123456789"
    assert client.post("/api/library/records", json=FIXTURE["create"]).status_code == 409
    assert len(client.get("/api/library").json()["records"]) == 1


def test_file_preconditions_and_move_delete_routes(node):
    app, client = node
    record = create(client)
    path = f"{BASE}/files/summary.txt"
    assert (
        client.put(
            path, content=b"stale", headers={"Content-Type": "text/plain", "X-Mnema-Mtime-Ns": "8"}
        ).status_code
        == 409
    )
    assert (
        client.put(
            path,
            content=b"stale",
            headers={"Content-Type": "text/plain", "X-Mnema-Mtime-Ns": "8", "If-None-Match": "*"},
        ).status_code
        == 409
    )
    assert client.delete(path, headers={"If-Match": '"wrong"'}).status_code == 409
    assert (
        client.delete(
            path, headers={"If-Match": record["files"]["summary.txt"]["sha256"]}
        ).status_code
        == 204
    )
    assert client.get(path).status_code == 404
    assert (
        client.put(
            path,
            content=b"new",
            headers={"Content-Type": "text/plain", "X-Mnema-Mtime-Ns": "9", "If-None-Match": "*"},
        ).status_code
        == 200
    )
    moved = client.patch(BASE, json={"expected_path": record["path"], "path": "Другой/Звонок"})
    assert moved.status_code == 200
    assert (
        client.patch(
            BASE, json={"expected_path": record["path"], "path": "Отставшее имя"}
        ).status_code
        == 409
    )
    assert client.delete(BASE, headers={"If-Match": record["tag"]}).status_code == 409
    assert client.put("/api/library/sections", params={"path": "Пусто"}).status_code == 204
    assert client.delete("/api/library/sections", params={"path": "Другой"}).status_code == 409
    assert client.delete(BASE, headers={"If-Match": moved.json()["tag"]}).status_code == 204
    assert client.delete(BASE, headers={"If-Match": moved.json()["tag"]}).status_code == 204
    assert client.delete("/api/library/sections", params={"path": "Другой"}).status_code == 204
    assert client.delete("/api/library/sections", params={"path": "Пусто"}).status_code == 204
    assert client.get("/api/library").json() == {
        "records": [],
        "sections": ["Работа"],
        "library": app.state.devices.user("owner").library,
    }


@pytest.mark.parametrize("library", ["false", None])  # unset: the library is off
def test_explicitly_disabled_library_keeps_processing_api(tmp_path, monkeypatch, library):
    if library is None:
        monkeypatch.delenv("MNEMA_LIBRARY", raising=False)
    else:
        monkeypatch.setenv("MNEMA_LIBRARY", library)
    client = TestClient(create_app(tmp_path))
    assert client.get("/api/capabilities").status_code == 200
    assert client.get("/api/library").status_code == 404
    assert client.post("/api/library/records", json=FIXTURE["create"]).status_code == 404
    assert not (tmp_path / "library").exists()


def test_creation_io_failure_keeps_id_and_allows_completion(node, monkeypatch):
    app, client = node
    real_replace = os.replace

    def interrupted(source, destination):
        if Path(destination).name == "transcript.txt":
            raise OSError("disk full")
        return real_replace(source, destination)

    with monkeypatch.context() as fault:
        fault.setattr(os, "replace", interrupted)
        assert client.post("/api/library/records", json=FIXTURE["create"]).status_code == 503
    partial = client.get("/api/library").json()["records"]
    assert len(partial) == 1 and partial[0]["id"] == ID
    assert list(partial[0]["files"]) == [".mnema-meta.json"]
    assert client.post("/api/library/records", json=FIXTURE["create"]).status_code == 409
    for name in ("transcript.txt", "summary.txt"):
        body = FIXTURE["create"]["files"][name]
        result = client.put(
            f"{BASE}/files/{name}",
            content=body["text"].encode(),
            headers={
                "Content-Type": "text/plain",
                "X-Mnema-Mtime-Ns": str(body["mtime_ns"]),
                "If-None-Match": "*",
            },
        )
        assert result.status_code == 200
    assert client.get("/api/library").json() == served(app, FIXTURE["manifest"])


@pytest.mark.parametrize("change", ["invalid-id", "no-meta", "audio", "path", "wrong-meta-id"])
def test_invalid_creation_does_not_mutate_library(node, change):
    app, client = node
    body = json.loads(json.dumps(FIXTURE["create"]))
    if change == "invalid-id":
        body["id"] = "arbitrary"
    elif change == "no-meta":
        del body["files"][".mnema-meta.json"]
    elif change == "audio":
        body["files"]["audio.wav"] = {"text": "no", "mtime_ns": 1}
    elif change == "path":
        body["path"] = "../outside"
    else:
        body["files"][".mnema-meta.json"]["text"] = '{"id":"e4b45222-14d0-4ae5-a581-8c746c5f305f"}'
    assert client.post("/api/library/records", json=body).status_code in (409, 422)
    assert list(app.state.library.root.iterdir()) == []


@pytest.mark.parametrize("mtime", [str(2**63), str(-(2**63) - 1), "not-an-int"])
def test_invalid_file_time_does_not_overwrite(node, mtime):
    _, client = node
    record = create(client)
    path = f"{BASE}/files/summary.txt"
    response = client.put(
        path,
        content=b"bad",
        headers={
            "Content-Type": "text/plain",
            "X-Mnema-Mtime-Ns": mtime,
            "If-Match": record["files"]["summary.txt"]["sha256"],
        },
    )
    assert response.status_code == 422
    assert client.get(path).text == FIXTURE["create"]["files"]["summary.txt"]["text"]


def test_empty_text_can_be_synchronized_and_bad_meta_is_rejected(node):
    _, client = node
    record = create(client)
    response = client.put(
        f"{BASE}/files/summary.txt",
        content=b"",
        headers={
            "Content-Type": "text/plain",
            "X-Mnema-Mtime-Ns": "123",
            "If-Match": record["files"]["summary.txt"]["sha256"],
        },
    )
    assert response.status_code == 200
    assert client.get(f"{BASE}/files/summary.txt").content == b""
    response = client.put(
        f"{BASE}/files/.mnema-meta.json",
        content=b"bad json",
        headers={
            "Content-Type": "text/plain",
            "X-Mnema-Mtime-Ns": "123",
            "If-Match": record["files"][".mnema-meta.json"]["sha256"],
        },
    )
    assert response.status_code == 409
    assert client.get("/api/library").json()["records"][0]["id"] == ID


NODE = "node-token"
NOW = 1_800_000_000.0
FILE_HEADERS = {"Content-Type": "text/plain", "X-Mnema-Mtime-Ns": "8", "If-None-Match": "*"}


@pytest.fixture
def shared(tmp_path, monkeypatch):
    """A node with the owner's library, user anna (library folder made) and a device each."""
    monkeypatch.setenv("MNEMA_LIBRARY", "true")
    monkeypatch.setenv("MNEMA_TOKEN", NODE)
    (tmp_path / "library").mkdir()
    (tmp_path / "users" / "anna" / "library").mkdir(parents=True)
    app = create_app(tmp_path)
    app.state.devices.add_user("anna", now=NOW)
    tokens = {}
    for user in ("owner", "anna"):
        code, _ = app.state.devices.create_invite(user=user, now=NOW)
        _, tokens[user] = app.state.devices.claim(code, user, now=NOW, library=True)
    clients = {
        user: TestClient(app, headers={"Authorization": f"Bearer {token}"})
        for user, token in tokens.items()
    }
    clients["node"] = TestClient(app, headers={"Authorization": f"Bearer {NODE}"})
    return app, clients


def test_libraries_of_users_do_not_meet(shared):
    app, clients = shared
    record = create(clients["owner"])
    assert clients["owner"].get("/api/library").json()["records"] == [record]
    assert clients["anna"].get("/api/library").json()["records"] == []
    name = "transcript.txt"
    assert clients["anna"].get(f"{BASE}/files/{name}").status_code == 404
    put = clients["anna"].put(f"{BASE}/files/{name}", content=b"x", headers=FILE_HEADERS)
    assert put.status_code == 404
    move = clients["anna"].patch(BASE, json={"expected_path": record["path"], "path": "Чужое"})
    assert move.status_code == 404
    gone = clients["anna"].delete(f"{BASE}/files/{name}", headers={"If-Match": '"x"'})
    assert gone.status_code == 404
    # Deleting a record the library does not have is the same as an unknown id.
    assert clients["anna"].delete(BASE, headers={"If-Match": record["tag"]}).status_code == 204
    assert clients["owner"].get(f"{BASE}/files/{name}").status_code == 200
    assert not any((app.state.root / "users" / "anna" / "library").iterdir())


def test_section_is_made_only_for_the_asking_user(shared):
    _, clients = shared
    assert (
        clients["anna"].put("/api/library/sections", params={"path": "Работа"}).status_code == 204
    )
    assert clients["anna"].get("/api/library").json()["sections"] == ["Работа"]
    assert clients["owner"].get("/api/library").json()["sections"] == []


def test_same_id_lives_in_both_libraries(shared):
    _, clients = shared
    own = create(clients["owner"])
    other = create(clients["anna"])
    assert other == own
    for client in (clients["owner"], clients["anna"]):
        assert [r["id"] for r in client.get("/api/library").json()["records"]] == [ID]


def test_node_token_reads_the_owners_library(shared):
    _, clients = shared
    record = create(clients["owner"])
    manifest = clients["node"].get("/api/library").json()
    assert manifest["records"] == [record]
    assert manifest["library"] == clients["owner"].get("/api/library").json()["library"]


def test_manifest_library_number_tells_users_apart_and_stays(shared):
    app, clients = shared
    first = {
        user: clients[user].get("/api/library").json()["library"] for user in ("owner", "anna")
    }
    assert first["owner"] == app.state.devices.user("owner").library
    assert first["anna"] == app.state.devices.user("anna").library
    assert first["owner"] != first["anna"]
    again = {
        user: clients[user].get("/api/library").json()["library"] for user in ("owner", "anna")
    }
    assert again == first


def test_library_without_folder_is_unavailable(shared):
    app, clients = shared
    (app.state.root / "users" / "anna" / "library").rmdir()
    response = clients["anna"].get("/api/library")
    assert response.status_code == 503
    assert response.json() == {"detail": "Библиотека недоступна"}
    assert clients["owner"].get("/api/library").status_code == 200


def test_first_requests_of_a_user_share_one_library_object(shared):
    app, _ = shared
    barrier = threading.Barrier(8)
    found = []

    def ask():
        barrier.wait()
        found.append(app.state.libraries.get("anna"))

    threads = [threading.Thread(target=ask) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert found[0] is not None and all(item is found[0] for item in found)
    assert app.state.libraries.get("owner") is app.state.library


def test_users_have_no_libraries_when_the_library_is_off(tmp_path, monkeypatch):
    monkeypatch.delenv("MNEMA_LIBRARY", raising=False)
    app = create_app(tmp_path)
    assert app.state.library is None
    assert app.state.libraries.get("owner") is None
    assert app.state.libraries.get("anna") is None
