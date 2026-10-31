"""Live phrase translation: POST /api/translate/phrase on the node and the client RemoteDialog."""

import json
import re
import subprocess
import threading
import time
import types

import httpx
import pytest
from fastapi.testclient import TestClient
from test_live_session import FakeSource, FakeTranscriber, FakeView, _ticking

from mnema.client import MnemaClient
from mnema.core.base import MediaError
from mnema.core.models import Segment
from mnema.devices import DeviceStore, devices_db
from mnema.live.models import LiveChunk
from mnema.live.remote_translate import RemoteDialog
from mnema.live.session import LiveSession
from mnema.live.translate import PinnedDialog
from mnema.server import create_app

PHRASE = {"text": "Hello", "context": ["я: Привет ⟶ Hi"], "target_lang": "ru"}


def _chunk(text: str):
    delta = types.SimpleNamespace(content=text)
    return types.SimpleNamespace(choices=[types.SimpleNamespace(delta=delta)])


@pytest.fixture
def model(monkeypatch):
    """The node model at the litellm boundary: answers "<lang>: <phrase>", FAIL means no balance."""
    import litellm

    calls: list[dict] = []

    def completion(**kw):
        system, text = (m["content"] for m in kw["messages"])
        calls.append({"system": system, "text": text})
        if text == "FAIL":
            raise RuntimeError("Insufficient Balance")
        lang = re.search(r"на язык: (\w+)", system)
        assert lang is not None
        return iter([_chunk(f"{lang.group(1)}: {text}")])

    monkeypatch.setattr(litellm, "completion", completion)
    return calls


def _dialog(tmp_path) -> RemoteDialog:
    app = create_app(tmp_path)
    return RemoteDialog(
        MnemaClient("http://node", transport=httpx.ASGITransport(app=app)), my_lang="ru"
    )


def test_phrase_route_translates_with_client_context_in_request(tmp_path, model, monkeypatch):
    client = TestClient(create_app(tmp_path))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("процесс на фразу"))
    response = client.post("/api/translate/phrase", json=PHRASE)
    assert response.status_code == 200 and response.json() == {"text": "ru: Hello"}
    assert model[0]["text"] == "Hello" and "я: Привет ⟶ Hi" in model[0]["system"]


def test_phrase_route_without_node_model_is_clear_error(tmp_path, model, monkeypatch):
    monkeypatch.delenv("MNEMA_MODEL")
    response = TestClient(create_app(tmp_path)).post("/api/translate/phrase", json=PHRASE)
    assert response.status_code == 400
    assert response.json()["detail"].startswith(
        "Не настроена модель. Выберите её в настройках Mnema на Mac"
    )
    assert not model


def test_phrase_route_reports_model_refusal_in_user_words(tmp_path, model):
    response = TestClient(create_app(tmp_path)).post(
        "/api/translate/phrase", json={**PHRASE, "text": "FAIL"}
    )
    assert response.status_code == 502
    assert response.json() == {"detail": "Перевод не получился: закончились деньги на счёте"}


def test_phrase_route_needs_node_or_device_token(tmp_path, model, monkeypatch):
    monkeypatch.setenv("MNEMA_TOKEN", "secret")
    client = TestClient(create_app(tmp_path))
    store = DeviceStore(devices_db(tmp_path))
    invite, _ = store.create_invite(now=time.time())
    _, device = store.claim(invite, "Mac", now=time.time())
    assert client.post("/api/translate/phrase", json=PHRASE).status_code == 401
    for token in ("secret", device):
        response = client.post(
            "/api/translate/phrase", json=PHRASE, headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200
    assert len(model) == 2


@pytest.mark.parametrize("extra", [{"model": "client/model"}, {"api_key": "client-secret"}])
def test_phrase_route_rejects_client_model_settings(tmp_path, model, extra):
    response = TestClient(create_app(tmp_path)).post(
        "/api/translate/phrase", json={**PHRASE, **extra}
    )
    assert response.status_code == 422 and not model


def test_remote_dialog_sends_last_phrases_with_translations_as_context(tmp_path, model):
    dialog = _dialog(tmp_path)
    mic, system = PinnedDialog(dialog, "я"), PinnedDialog(dialog, "они")
    assert mic.route("Привет")[1].push("Привет") == "en: Привет"
    assert system.route("Hello")[1].push("Hello") == "ru: Hello"
    # my English phrase: label by source, direction by language
    assert mic.route("My English")[1].push("My English") == "ru: My English"
    assert dialog.route("Спасибо")[1].push("Спасибо") == "en: Спасибо"
    assert dialog.route("Bye")[1].push("Bye") == "ru: Bye"
    assert "Контекст" not in model[0]["system"]
    last = model[-1]["system"]
    assert "Привет" not in last
    assert (
        "они: Hello ⟶ ru: Hello\nя: My English ⟶ ru: My English\nя: Спасибо ⟶ en: Спасибо" in last
    )


def test_remote_dialog_refusal_is_clear_and_next_phrase_goes_on(tmp_path, model):
    dialog = _dialog(tmp_path)
    with pytest.raises(MediaError, match="Перевод не получился: закончились деньги на счёте"):
        dialog.route("FAIL")[1].push("FAIL")
    assert dialog.route("Hello")[1].push("Hello") == "ru: Hello"
    assert model[-1]["system"].endswith("Контекст:\nони: FAIL")  # the phrase stayed untranslated


def test_remote_dialog_unreachable_node_never_translates_here(model):
    def offline(request):
        raise httpx.ConnectError("offline", request=request)

    client = MnemaClient("http://offline", transport=httpx.MockTransport(offline))
    with pytest.raises(MediaError, match="Нет связи с сервером"):
        RemoteDialog(client, my_lang="ru").route("Hello")[1].push("Hello")
    assert not model


def test_session_keeps_phrase_order_while_slow_reply_waits():
    second_shown = threading.Event()
    requests: list[dict] = []

    def node(request):
        body = json.loads(request.content)
        requests.append(body)
        if body["text"] == "Hello there.":
            assert second_shown.wait(5)  # slow reply: the next phrase is already shown
        return httpx.Response(200, json={"text": f"<{body['text']}>"})

    class View(FakeView):
        def hold(self, label, text):
            super().hold(label, text)
            if text == "Как дела?":
                second_shown.set()

    chunks = [
        LiveChunk(committed=[Segment(start=0, end=1, text="Hello there.", speaker="mic")]),
        LiveChunk(committed=[Segment(start=1, end=2, text="Как дела?", speaker="mic")]),
    ]
    client = MnemaClient("http://node", transport=httpx.MockTransport(node))
    view = View()
    LiveSession(
        [FakeSource("mic", 2)],
        lambda s: FakeTranscriber(chunks),
        dialog=RemoteDialog(client, my_lang="ru"),
        view=view,
        clock=_ticking(),
    ).run()
    assert view.committed == [
        ("mic", "Hello there.", "<Hello there.>"),
        ("mic", "Как дела?", "<Как дела?>"),
    ]
    assert requests[1] == {
        "text": "Как дела?",
        "context": ["они: Hello there. ⟶ <Hello there.>"],
        "target_lang": "en",
    }


def test_remote_dialog_unreachable_node_error_is_english_in_english_ui(model):
    from mnema.i18n import using

    def offline(request):
        raise httpx.ConnectError("offline", request=request)

    client = MnemaClient("http://offline", transport=httpx.MockTransport(offline))
    with using("en"), pytest.raises(MediaError) as err:
        RemoteDialog(client, my_lang="ru").route("Hello")[1].push("Hello")
    assert str(err.value) == "No connection to the server: the phrase was not translated"
