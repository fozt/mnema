"""The client reads the node's capabilities; the model and key are set only on the server."""

import json
import socket

import pytest
from fastapi.testclient import TestClient

from mnema import config
from mnema.server import create_app


@pytest.fixture
def node_list(monkeypatch):
    """The node's model comes from its ~/.config/mnema/.env lines, not the process environment."""
    monkeypatch.delenv("MNEMA_MODEL", raising=False)
    monkeypatch.delenv("MNEMA_API_KEY", raising=False)


def _client(tmp_path) -> TestClient:
    return TestClient(create_app(tmp_path))


def test_server_name_without_config(tmp_path, node_list, monkeypatch):
    """A server without a recordings library (a manual `mnema serve`) and without a computer
    name shows other devices the default computer name (Russian here: tests run in ru)."""
    monkeypatch.setattr(socket, "gethostname", lambda: "")
    monkeypatch.delenv("MNEMA_LIBRARY", raising=False)
    client = _client(tmp_path)
    body = client.get("/api/node/settings").json()
    assert body["name"] == "Компьютер" == client.get("/api/capabilities").json()["name"]
    assert body["model"] is None
    assert isinstance(body["engines"], bool)


def test_name_is_read_from_the_server_config_without_restart(tmp_path):
    client = _client(tmp_path)
    path = config.user_env_file().with_name("node.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"name": "Домашний"}))
    assert client.get("/api/node/settings").json()["name"] == "Домашний"
    assert client.get("/api/capabilities").json()["name"] == "Домашний"
