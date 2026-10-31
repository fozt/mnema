"""The model API of the server: each user sets her model and keys from the app.

The boundary is the node's HTTP API (TestClient and MnemaClient over ASGITransport) with a
device of the owner and one of anna; the providers' network (providers.TRANSPORT) and
litellm.completion are mocked; the settings folder is in the test's tmp (conftest).
"""

from __future__ import annotations

import logging
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from mnema import config, users
from mnema.client import MnemaClient, ServerError
from mnema.llm import providers
from mnema.llm.config import LLMSettings
from mnema.server import create_app
from tests import model_stubs

NODE = "node-token-" + "n" * 20
GOOD = "AIza" + "g" * 31 + "Good"
OTHER = "AIza" + "o" * 31 + "Othr"
BAD = "AIza" + "b" * 31 + "Badd"
OPENAI = "sk-proj-" + "o" * 30 + "x9Q1"
DEEPL = "a1b2c3d4-" + "d" * 26 + ":fx"
DEEPL_PRO = "e5f6a7b8-" + "p" * 26 + "Pro1"


def _device(store, user: str) -> str:
    code, _ = store.create_invite(user=user, now=time.time())
    claimed = store.claim(code, f"{user} mac", now=time.time(), library=True)
    assert claimed is not None
    return claimed[1]


class Network:
    """Gemini and OpenAI model lists, DeepL usage and translation; the requests it saw."""

    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []
        self.deepl_down = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        host, auth = request.url.host, request.headers.get("authorization", "")
        if host == "generativelanguage.googleapis.com":
            if request.headers["x-goog-api-key"] not in (GOOD, OTHER):
                return httpx.Response(400, json={"error": {"message": "API key not valid."}})
            listing = [
                {"name": f"models/{name}", "supportedGenerationMethods": ["generateContent"]}
                for name in model_stubs.GEMINI_MODELS
            ]
            return httpx.Response(200, json={"models": listing})
        if host == "api.openai.com":
            if auth != f"Bearer {OPENAI}":
                return httpx.Response(401, json={"error": {"message": "Incorrect API key"}})
            return httpx.Response(200, json={"data": [{"id": "gpt-4o-transcribe"}]})
        if host in ("api-free.deepl.com", "api.deepl.com"):
            if self.deepl_down:
                return httpx.Response(503)
            if auth not in (f"DeepL-Auth-Key {DEEPL}", f"DeepL-Auth-Key {DEEPL_PRO}"):
                return httpx.Response(403)
            if request.url.path == "/v2/usage":
                return httpx.Response(200, json={"character_count": 0})
            return httpx.Response(200, json={"translations": [{"text": "Hello."}]})
        raise AssertionError(f"unexpected request {request.url}")


@pytest.fixture
def net(monkeypatch) -> Network:
    network = Network()
    monkeypatch.setattr(providers, "TRANSPORT", httpx.MockTransport(network))
    return network


@pytest.fixture
def llm(monkeypatch) -> model_stubs.Litellm:
    return model_stubs.Litellm(monkeypatch)


@pytest.fixture
def srv(tmp_path, monkeypatch):
    """A server in its mode (strict) with a token, a device of the owner and one of anna."""
    monkeypatch.setenv("MNEMA_TOKEN", NODE)
    users.set_strict(True)
    app = create_app(tmp_path / "srv")
    store = app.state.devices
    store.add_user("anna", now=time.time())
    tokens = {users.OWNER: _device(store, users.OWNER), "anna": _device(store, "anna")}

    def client(user: str) -> MnemaClient:
        return MnemaClient(
            "http://test", token=tokens[user], transport=httpx.ASGITransport(app=app)
        )

    http = TestClient(app, headers={"Accept-Language": "ru"})
    return client, http, tokens


def _lines(user: str) -> str:
    path = users.user_env_file(user)
    return path.read_text() if path.exists() else ""


def test_a_user_sets_her_model_from_the_app(srv, net, llm) -> None:
    client, _http, _tokens = srv
    anna = client("anna")
    assert anna.model() == {"model": None, "key": None, "asr_key": None, "deepl_key": None}
    checked = anna.model_key("gemini", GOOD)
    assert checked["recommended"] == "gemini-flash-latest"
    assert {"id": "gemini-flash-latest", "title": "gemini-flash-latest", "free": False} in (
        checked["models"]
    )
    saved = anna.save_model("gemini/gemini-flash-latest", GOOD)
    assert saved == {
        "model": "gemini/gemini-flash-latest", "key": "…Good", "asr_key": None, "deepl_key": None,
    }  # fmt: skip
    assert llm.calls[-1]["api_key"] == GOOD
    assert GOOD in _lines("anna") and not users.user_env_file(users.OWNER).exists()
    assert not config.user_env_file().exists()
    with users.as_user("anna"):
        assert LLMSettings().model == "gemini/gemini-flash-latest"


def test_a_null_key_keeps_the_saved_key_of_the_provider(srv, net, llm) -> None:
    client, _http, _tokens = srv
    anna = client("anna")
    anna.save_model("gemini/gemini-flash-latest", GOOD)
    assert anna.model_key("gemini", None)["recommended"] == "gemini-flash-latest"
    assert anna.save_model("gemini/gemini-pro-latest", None)["key"] == "…Good"
    assert llm.calls[-1]["api_key"] == GOOD
    with pytest.raises(ServerError) as refused:
        anna.save_model("deepseek/deepseek-chat", None)  # no saved DeepSeek key
    assert (refused.value.status, refused.value.retry) == (400, "key")
    assert "gemini/gemini-pro-latest" in _lines("anna")


def test_a_refused_key_or_model_says_which_step_to_go_back_to(srv, net, llm) -> None:
    client, http, tokens = srv
    anna = client("anna")
    with pytest.raises(ServerError) as refused:
        anna.model_key("gemini", BAD)
    assert refused.value.status == 400 and str(refused.value) == "Ключ модели не подошёл"
    llm.answers["gemini/gemini-flash-latest"] = model_stubs.error("API key not valid", 401)
    answer = http.put(
        "/api/model",
        json={"model": "gemini/gemini-flash-latest", "key": OTHER},
        headers={"Authorization": f"Bearer {tokens['anna']}"},
    )
    assert answer.status_code == 400
    assert answer.json() == {
        "detail": "Ключ модели не подошёл: введите другой ключ",
        "retry": "key",
    }
    llm.answers["gemini/gemini-flash-latest"] = model_stubs.error("model not found", 404)
    with pytest.raises(ServerError) as refused:
        anna.save_model("gemini/gemini-flash-latest", OTHER)
    assert refused.value.retry == "model" and str(refused.value).endswith("выберите другую модель")
    assert _lines("anna") == ""


def test_removing_the_model_keeps_the_other_keys(srv, net, llm) -> None:
    client, _http, _tokens = srv
    anna = client("anna")
    anna.save_model("gemini/gemini-flash-latest", GOOD)
    anna.save_deepl_key(DEEPL)
    anna.remove_model()
    assert anna.model() == {"model": None, "key": None, "asr_key": None, "deepl_key": "…d:fx"}


def test_recognition_and_deepl_keys_are_checked_and_removed_with_null(srv, net) -> None:
    client, _http, _tokens = srv
    anna = client("anna")
    assert anna.save_asr_key(OPENAI)["asr_key"] == "…x9Q1"
    with pytest.raises(ServerError) as refused:
        anna.save_asr_key("sk-proj-" + "w" * 30 + "Wrng")
    assert refused.value.status == 400 and str(refused.value) == "Ключ модели не подошёл"
    assert anna.save_deepl_key(DEEPL)["deepl_key"] == "…d:fx"
    assert anna.save_deepl_key(DEEPL_PRO)["deepl_key"] == "…Pro1"
    usage = [r.url for r in net.seen if r.url.path == "/v2/usage"]
    assert [u.host for u in usage] == ["api-free.deepl.com", "api.deepl.com"]
    with pytest.raises(ServerError) as refused:
        anna.save_deepl_key("bad-deepl-key")
    assert str(refused.value) == "DeepL не принял ключ"
    net.deepl_down = True
    with pytest.raises(ServerError) as refused:
        anna.save_deepl_key(DEEPL)
    assert str(refused.value) == "DeepL ответил ошибкой 503"
    assert anna.model()["deepl_key"] == "…Pro1"  # a refused key changes nothing
    assert anna.save_asr_key(None)["asr_key"] is None
    assert anna.save_deepl_key(None)["deepl_key"] is None
    assert OPENAI not in _lines("anna") and DEEPL_PRO not in _lines("anna")


def test_the_owner_device_neither_sees_nor_changes_the_file_of_anna(srv, net, llm) -> None:
    client, http, _tokens = srv
    client("anna").save_model("gemini/gemini-flash-latest", GOOD)
    anna_file = _lines("anna")
    owner = client(users.OWNER)
    assert owner.model()["model"] is None
    owner.save_model("gemini/gemini-pro-latest", OTHER)
    owner.save_deepl_key(DEEPL)
    owner.remove_model()
    assert _lines("anna") == anna_file
    assert client("anna").model()["model"] == "gemini/gemini-flash-latest"
    node = http.get("/api/model", headers={"Authorization": f"Bearer {NODE}"})
    assert node.json()["deepl_key"] == "…d:fx"  # the node token is the owner


def test_keys_from_request_bodies_never_reach_the_log(srv, net, llm, caplog) -> None:
    client, _http, _tokens = srv
    caplog.set_level(logging.DEBUG)
    anna = client("anna")
    llm.answers["gemini/gemini-flash-latest"] = model_stubs.error(f"bad key {OTHER}", 401)
    with pytest.raises(ServerError):
        anna.save_model("gemini/gemini-flash-latest", OTHER)
    anna.save_deepl_key(DEEPL)
    anna.save_asr_key(OPENAI)
    for key in (OTHER, DEEPL, OPENAI):
        logging.getLogger("mnema.test").warning("refused: %s", key)
    assert not any(key in caplog.text for key in (OTHER, DEEPL, OPENAI))
