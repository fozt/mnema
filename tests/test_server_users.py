"""Two users of one server in the server mode (`serve`: users.set_strict(True)), end to end.

The boundary is the node's HTTP API and WebSocket (TestClient) with a device of the owner and
one of anna; each has her model in users/<name>.env. External boundaries are mocked:
litellm.completion, the providers' network (providers.TRANSPORT) and the WLK child (stub).
"""

from __future__ import annotations

import logging
import time
import types
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from mnema import users
from mnema.llm import claude, limits, providers
from mnema.llm.config import save_key, save_model
from mnema.server import create_app

NODE = "node-token-" + "n" * 20
# keys without a recognizable prefix: only the user files tell they are keys
OWNER_KEY = "ok" + "o" * 30 + "w1N2"
ANNA_KEY = "ak" + "y" * 30 + "z9Y8"
ANNA_ASR_KEY = "sk-proj-" + "a" * 40 + "r7S6"
TEXT = "mic: обсудили релиз, сроки и кто готовит демо.\nsystem: я беру демо к пятнице.\n" * 10
SUMMARY = '{"title": "Релиз", "other": "собеседник", "summary": "Обсудили релиз.", "points": []}'


@pytest.fixture
def srv(tmp_path, monkeypatch, caplog):
    """A server with a token and a library, in the server mode; devices of the owner and anna."""
    monkeypatch.setenv("MNEMA_TOKEN", NODE)
    monkeypatch.setenv("MNEMA_LIBRARY", "true")
    monkeypatch.setattr(
        providers, "TRANSPORT", httpx.MockTransport(lambda r: httpx.Response(404, json={}))
    )
    monkeypatch.setattr(limits, "_FETCHED", {})
    monkeypatch.setattr(claude.time, "sleep", lambda s: None)
    caplog.set_level(logging.DEBUG)
    users.set_strict(True)
    app = create_app(tmp_path)
    store = app.state.devices
    store.add_user("anna", now=time.time())
    headers = {}
    for user in (users.OWNER, "anna"):
        users.user_library(app.state.root, user).mkdir(parents=True, exist_ok=True)
        code, _ = store.create_invite(user=user, now=time.time())
        claimed = store.claim(code, f"{user} phone", now=time.time(), library=True)
        assert claimed is not None
        headers[user] = {"Authorization": f"Bearer {claimed[1]}"}
    return SimpleNamespace(app=app, client=TestClient(app), h=headers)


def _litellm(monkeypatch, answer) -> list[dict]:
    """litellm at the boundary: answer(kwargs) -> text or an exception. Returns the calls."""
    import litellm

    calls: list[dict] = []

    def completion(**kw):
        calls.append(kw)
        out = answer(kw)
        if isinstance(out, BaseException):
            raise out
        delta = types.SimpleNamespace(content=out)
        choice = types.SimpleNamespace(delta=delta, finish_reason="stop")
        return iter([types.SimpleNamespace(choices=[choice])])

    monkeypatch.setattr(litellm, "completion", completion)
    return calls


def _summary(srv, who: str) -> dict:
    """A recording_summary job of `who` over an uploaded transcript.txt, run to its end by the
    queue's thread."""
    files = {"file": ("transcript.txt", TEXT.encode())}
    upload = srv.client.post("/api/upload", files=files, headers=srv.h[who])
    assert upload.status_code == 200, upload.text
    params = {"started": "2026-10-31T10:00:00+03:00", "name": "2026-10-31_10-00", "lang": "ru"}
    body = {"op": "recording_summary", "path": upload.json()["path"], "params": params}
    queued = srv.client.post("/api/jobs", json=body, headers=srv.h[who])
    assert queued.status_code == 200, queued.text
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = srv.client.get(f"/api/jobs/{queued.json()['id']}", headers=srv.h[who])
        if job.json()["status"] in ("done", "error"):
            return job.json()
        time.sleep(0.01)
    pytest.fail("the summary job did not finish")


def test_each_user_sees_her_own_model(srv):
    save_model("deepseek/deepseek-chat", OWNER_KEY, user=users.OWNER)
    anna = users.user_env_file("anna")
    anna.write_text("MNEMA_MODEL=gemini/gemini-flash-latest\n")  # a model without a key
    caps = {who: srv.client.get("/api/capabilities", headers=srv.h[who]) for who in srv.h}
    node = {who: srv.client.get("/api/node/settings", headers=srv.h[who]) for who in srv.h}
    assert {who: answer.json()["llm"] for who, answer in caps.items()} == {
        users.OWNER: True,
        "anna": False,
    }
    assert {who: answer.json()["model"] for who, answer in node.items()} == {
        users.OWNER: "deepseek/deepseek-chat",
        "anna": None,
    }
    save_model("gemini/gemini-flash-latest", ANNA_KEY, user="anna")
    assert srv.client.get("/api/node/settings", headers=srv.h["anna"]).json()["model"] == (
        "gemini/gemini-flash-latest"
    )
    for answer in (*caps.values(), *node.values()):
        assert OWNER_KEY not in answer.text


def test_every_server_answer_works_in_the_server_mode_with_the_user_keys(
    srv, monkeypatch, caplog, wlk_process
):
    """No route or queued job of the server mode is left without a user (that would be a
    RuntimeError); each works with the asking user's model and keys, and no answer, remark or
    log line carries a key."""
    save_model("deepseek/deepseek-chat", OWNER_KEY, user=users.OWNER)
    save_model("deepseek/deepseek-chat", ANNA_KEY, user="anna")
    save_key("MNEMA_ASR_API_KEY", ANNA_ASR_KEY, user="anna")
    calls = _litellm(
        monkeypatch, lambda kw: "Hello, Anna" if "Привет" in str(kw["messages"]) else SUMMARY
    )
    c, anna = srv.client, srv.h["anna"]
    answers = [
        c.get("/api/capabilities", headers=anna),
        c.get("/api/node/settings", headers=anna),
        c.get("/api/node/asr", headers=anna),
        c.get("/api/node/asr", headers=srv.h[users.OWNER]),
        c.get("/api/model", headers=anna),
        c.post(
            "/api/translate/phrase",
            json={"text": "Привет, Аня", "target_lang": "en"},
            headers=anna,
        ),
        c.get("/api/library", headers=anna),
    ]
    assert [a.status_code for a in answers] == [200] * len(answers), [a.text for a in answers]
    caps, settings, asr, owner_asr, model, phrase, library = (a.json() for a in answers)
    assert caps["llm"] is True and settings["model"] == "deepseek/deepseek-chat"
    cloud = {"openai/gpt-4o-transcribe", "openai/gpt-4o-mini-transcribe"}
    assert cloud <= {choice["id"] for choice in asr["choices"]}
    assert not cloud & {choice["id"] for choice in owner_asr["choices"]}
    assert model == {
        "model": "deepseek/deepseek-chat",
        "key": "…z9Y8",
        "asr_key": "…r7S6",
        "deepl_key": None,
    }
    assert phrase == {"text": "Hello, Anna"}
    assert library["records"] == []

    job = _summary(srv, "anna")
    assert job["status"] == "done", job
    assert job["result"]["files"]["summary.txt"]
    assert {call["api_key"] for call in calls} == {ANNA_KEY}

    with c.websocket_connect("/api/live?lang=ru", headers=anna) as ws:
        assert ws.receive_json() == {"type": "ready"}
        ws.send_bytes(b"audio")
        ws.send_json({"type": "end"})
        assert [s["text"] for s in ws.receive_json()["committed"]] == ["hello"]
    wlk_process[1]()

    shown = [a.text for a in answers] + [str(job)] + [caplog.text]
    for key in (OWNER_KEY, ANNA_KEY, ANNA_ASR_KEY):
        assert not [text for text in shown if key in text]


def test_a_refusal_of_the_model_names_no_key_of_anyone(srv, monkeypatch, caplog):
    """The provider repeats the key in its refusal: anna's remark, her job and the log hide it,
    and the owner's job runs with the owner's key."""
    save_model("deepseek/deepseek-chat", OWNER_KEY, user=users.OWNER)
    save_model("deepseek/deepseek-chat", ANNA_KEY, user="anna")

    def answer(kw):
        if kw["api_key"] == ANNA_KEY:
            refusal = type("AuthenticationError", (Exception,), {})(f"invalid key: {ANNA_KEY}")
            refusal.status_code = 401  # type: ignore[attr-defined]
            return refusal
        return SUMMARY

    calls = _litellm(monkeypatch, answer)
    refused = _summary(srv, "anna")
    remarks = " ".join(refused["result"]["run"]["remarks"])
    assert "ключ модели не подошёл" in remarks
    assert not refused["result"]["files"].get("summary.txt")
    owners = _summary(srv, users.OWNER)
    assert owners["result"]["files"]["summary.txt"]
    assert {call["api_key"] for call in calls} == {ANNA_KEY, OWNER_KEY}
    shown = [str(refused), str(owners)] + [caplog.text]
    for key in (OWNER_KEY, ANNA_KEY):
        assert not [text for text in shown if key in text]
