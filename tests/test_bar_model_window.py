"""The "Summary model" window on the Mac (bar/forms.ModelFlow through BarRuntime.handle and window
events): its steps, here or on the server; the key lives only in the window field.

Mocked boundaries: the provider network (providers.TRANSPORT), the server (MnemaClient's
transport) and litellm.completion.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from dotenv import dotenv_values

from mnema.bar.forms import FormEvent
from mnema.bar.protocol import FormAsk, FormFieldKind, FormOp
from mnema.bar.runtime import BarRuntime
from mnema.bar.settings_model import settings_model
from mnema.bar.state import Phase
from mnema.config import user_env_file
from mnema.core import base
from mnema.llm import providers
from tests import model_stubs
from tests.model_stubs import error

GOOD = "AIza" + "g" * 31 + "Good"
BAD = "AIza" + "b" * 31 + "Bad1"
FLASH = "gemini/gemini-flash-latest"


class _Forms:
    """The macOS app with a live form window: every window state goes to a queue."""

    def __init__(self) -> None:
        self.shown: queue.Queue[FormAsk | None] = queue.Queue()
        self.on_event: Callable[[FormEvent | None], None] = lambda ev: None
        self.rev = 0
        self.checked = False  # the last press showed "Checking…"

    def open_form(self, ask: FormAsk, on_event: Callable[[FormEvent | None], None]) -> Any:
        self.on_event = on_event
        self.shown.put(ask)
        return self.shown.put

    def next(self) -> FormAsk | None:
        return self.shown.get(timeout=10)

    def submit(self, **values: str) -> FormAsk | None:
        """Press the main button and get the step's result; a step with a check first shows
        "Checking…" with no error and no cleared fields."""
        self.rev += 1
        self.on_event(FormEvent(op=FormOp.SUBMIT, values=values, rev=self.rev))
        shown = self.next()
        self.checked = shown is not None and shown.busy != ""
        if self.checked:
            assert shown is not None and (shown.busy, shown.error, shown.clear) == (
                "Проверяю…", "", [],
            )  # fmt: skip
            shown = self.next()
        assert shown is None or (shown.busy, shown.rev) == ("", self.rev)
        return shown

    def notify(self, title: str, message: str, path: Any) -> None: ...

    def open(self, path: Any) -> None: ...


def _join() -> None:
    for t in threading.enumerate():
        if t.name == "mnema-bar-form":
            t.join(timeout=10)


@pytest.fixture
def mac(monkeypatch, tmp_path) -> tuple[BarRuntime, _Forms]:
    """A Mac without a model: the window opens on the Provider step."""
    monkeypatch.setattr(base, "STANDALONE", True)
    monkeypatch.delenv("MNEMA_MODEL")
    monkeypatch.delenv("MNEMA_API_KEY")
    platform = _Forms()
    rt = BarRuntime(platform=platform, prefs_file=tmp_path / "bar.json")
    rt.handle("model_setup")
    return rt, platform


def _choices(ask: FormAsk) -> list[tuple[str, str, str, bool]]:
    [field] = ask.fields
    assert field.kind is FormFieldKind.CHOICE
    return [(c.key, c.title, c.detail, c.checked) for c in field.choices]


def test_provider_key_and_model_steps_save_the_checked_model(mac, monkeypatch, caplog) -> None:
    """Provider (no network) → key: an empty key, another provider's key and one with invalid
    characters never reach the provider, a provider refusal clears the field → model: the key's
    list with the recommended one chosen → a check request → MNEMA_MODEL and MNEMA_API_KEY in .env,
    the window closes, the Model row is new. The key is nowhere in the window, settings, bar.json
    or the log."""
    rt, platform = mac
    caplog.set_level(logging.DEBUG)
    sent = model_stubs.gemini(monkeypatch, GOOD)
    stub = model_stubs.Litellm(monkeypatch)
    shown: list[FormAsk] = []

    first = platform.next()
    assert first is not None and (first.title, first.message, first.ok, first.cancel) == (
        "Модель для пересказа", "Модель не настроена", "Далее", "Отменить",
    )  # fmt: skip
    titles = [title for _, title, _, _ in _choices(first)]
    assert titles == ["Gemini", "DeepSeek", "OpenAI", "Claude", "Qwen", "OpenRouter", "Groq"]
    assert [key for key, _, _, checked in _choices(first) if checked] == ["gemini"]

    key_step = platform.submit(provider="gemini")
    assert key_step is not None and sent == [] and not platform.checked  # no "Checking…"
    assert key_step.message == "Где взять ключ Gemini: aistudio.google.com/apikey"
    assert [(f.id, f.label, f.kind, f.placeholder) for f in key_step.fields] == [
        ("key", "Ключ API", FormFieldKind.SECRET, ""),
    ]
    for typed, said in [
        ("", "Нужен ключ"),
        ("sk-or-v1-" + "o" * 30, "Это ключ OpenRouter"),
        ("AIza ключ" + "x" * 20, "В ключе есть недопустимые знаки. Скопируйте его заново"),
        (BAD, "Ключ модели не подошёл"),
    ]:
        refused = platform.submit(key=typed)
        assert platform.checked
        assert refused is not None and (refused.error, refused.clear) == (said, ["key"])
        assert [f.id for f in refused.fields] == ["key"]
        shown.append(refused)
    assert sent == [BAD]

    model_step = platform.submit(key=GOOD)
    assert model_step is not None and model_step.ok == "Проверить и сохранить"
    assert _choices(model_step) == [
        ("gemini-pro-latest", "gemini-pro-latest", "", False),
        ("gemini-flash-latest", "gemini-flash-latest", "", True),
        ("gemini-flash-lite-latest", "gemini-flash-lite-latest", "", False),
    ]
    shown += [first, key_step, model_step]
    assert platform.submit(model="gemini-flash-latest") is None
    [call] = stub.calls
    assert (call["model"], call["api_key"]) == (FLASH, GOOD)
    _join()
    env = dotenv_values(user_env_file())
    assert (env["MNEMA_MODEL"], env["MNEMA_API_KEY"]) == (FLASH, GOOD)
    model_row = settings_model(rt.prefs, rt.menu_env()).tabs[0].sections[1].fields[2]
    assert (model_row.title, model_row.value) == ("Модель", FLASH)
    rt.handle("summary")  # bar.json is written
    for text in (
        *(ask.model_dump_json() for ask in shown),
        settings_model(rt.prefs, rt.menu_env()).model_dump_json(),
        (rt._prefs_file.read_text(encoding="utf-8") if rt._prefs_file else ""),
        caplog.text,
    ):
        assert GOOD not in text and BAD not in text


def test_saved_key_of_the_same_provider_is_offered_and_used(mac, monkeypatch) -> None:
    """The current model's provider has a key: the "Keep …" placeholder, and an empty field means
    that key."""
    from mnema.llm.config import save_model

    rt, platform = mac
    platform.next()
    platform.on_event(None)  # a window without a model was closed
    save_model(FLASH, GOOD)
    rt.handle("model_setup")
    first = platform.next()
    assert first is not None and first.message == f"Сейчас: {FLASH}, ключ …Good"
    sent = model_stubs.gemini(monkeypatch, GOOD)
    key_step = platform.submit(provider="gemini")
    assert key_step is not None and key_step.fields[0].placeholder == "Оставить …Good"
    model_step = platform.submit(key="")
    assert model_step is not None and model_step.fields[0].id == "model" and sent == [GOOD]


@pytest.mark.parametrize(
    ("answer", "step", "said"),
    [
        (error("API key not valid", 401), "key", "Ключ модели не подошёл: введите другой ключ"),
        (
            error("models/gemini-flash-latest is not found", 404),
            "model",
            "Нет доступа к модели: выберите другую модель",
        ),
    ],
    ids=["key", "model"],
)
def test_model_refusal_returns_to_the_step_to_change(mac, monkeypatch, answer, step, said):
    """The check request refused: a key problem goes back to Key with a cleared field, otherwise to
    Model with the model chosen; .env is unchanged."""
    _, platform = mac
    model_stubs.gemini(monkeypatch, GOOD)
    model_stubs.Litellm(monkeypatch).answers[FLASH] = answer
    platform.next()
    platform.submit(provider="gemini")
    platform.submit(key=GOOD)
    refused = platform.submit(model="gemini-flash-latest")
    assert refused is not None and ([f.id for f in refused.fields], refused.error) == ([step], said)
    assert refused.clear == (["key"] if step == "key" else [])
    if step == "model":
        assert [key for key, *_, checked in _choices(refused) if checked] == ["gemini-flash-latest"]
    assert not user_env_file().exists()


def test_provider_without_models_goes_back_to_the_provider(mac, monkeypatch) -> None:
    """The provider accepted the key but gave no models and nothing to recommend: back to
    Provider."""
    _, platform = mac
    monkeypatch.setattr(
        providers,
        "TRANSPORT",
        httpx.MockTransport(lambda request: httpx.Response(200, json={"data": []})),
    )
    platform.next()
    key_step = platform.submit(provider="openai")
    assert key_step is not None
    assert key_step.message == "Где взять ключ OpenAI: platform.openai.com/api-keys"
    back = platform.submit(key="sk-" + "d" * 32)
    assert back is not None and [f.id for f in back.fields] == ["provider"]
    assert back.error == "Провайдер не дал список моделей. Выберите другого провайдера"
    assert [key for key, *_, checked in _choices(back) if checked] == ["openai"]


@pytest.mark.parametrize("busy", [False, True], ids=["answered", "overloaded"])
def test_cancel_during_the_check_keeps_the_env_file(mac, monkeypatch, busy) -> None:
    """Cancel while the model answers: a reply is not saved; a busy model's retry pause stops at
    once with no second request. The window gets nothing more."""
    import litellm

    _, platform = mac
    model_stubs.gemini(monkeypatch, GOOD)
    stub = model_stubs.Litellm(monkeypatch)
    calls: list[str] = []

    def completion(**kw):
        calls.append(kw["model"])
        platform.on_event(None)  # the window closed during the request
        if busy:
            raise error("The model is overloaded", 503)
        return iter([model_stubs.chunk("готово")])

    monkeypatch.setattr(litellm, "completion", completion)
    platform.next()
    platform.submit(provider="gemini")
    platform.submit(key=GOOD)
    platform.rev += 1
    values = {"model": "gemini-flash-latest"}
    platform.on_event(FormEvent(op=FormOp.SUBMIT, values=values, rev=platform.rev))
    assert platform.next() is not None  # "Checking…"
    _join()
    assert calls == [FLASH] and stub.sleeps == []
    assert platform.shown.empty() and not user_env_file().exists()


def test_model_window_only_on_the_mac_and_only_when_idle(monkeypatch, tmp_path) -> None:
    """Linux summarizes with the server's model, so it has no model window; on the Mac it is
    unavailable while recording."""
    platform = _Forms()
    rt = BarRuntime(platform=platform, prefs_file=tmp_path / "bar.json")
    rt.handle("model_setup")
    assert platform.shown.empty()
    monkeypatch.setattr(base, "STANDALONE", True)
    rt.state.phase = Phase.RECORDING
    rt.handle("model_setup")
    assert platform.shown.empty()


def test_model_window_steps_are_what_the_app_fixtures_show(monkeypatch) -> None:
    """The Provider and Key steps with a Gemini model and key …a1B2 match the Fixtures Swift draws
    the window from."""
    from mnema.bar.forms import ModelFlow
    from mnema.bar.protocol import samples
    from mnema.llm.config import save_model

    monkeypatch.delenv("MNEMA_MODEL")
    monkeypatch.delenv("MNEMA_API_KEY")
    save_model(FLASH, "AIza" + "x" * 31 + "a1B2")
    shown = samples()
    flow = ModelFlow()
    assert flow.ask() == shown["out-ask-form-model-provider"].ask  # type: ignore[attr-defined]
    key_step = flow.submit({"provider": "gemini"}, force=False)
    assert key_step == shown["out-ask-form-model-key"].ask  # type: ignore[attr-defined]


SERVER = "http://srv:8123"
OPENAI = "sk-proj-" + "o" * 30 + "Good"
DEEPL = "d" * 32 + ":fx"


class _UserServer:
    """The connected server's /api/model* for the device's user (MockTransport of MnemaClient);
    it checks keys and models itself and keeps them in its state."""

    def __init__(self, monkeypatch) -> None:
        self.state: dict[str, Any] = {
            "model": None,
            "key": None,
            "asr_key": None,
            "deepl_key": None,
        }
        self.requests: list[tuple[str, str, dict | None]] = []
        self.refuse: dict[str, tuple[int, dict]] = {}  # path → the next answer instead
        self.before_answer: Callable[[str, str], None] = lambda method, path: None
        import mnema.client as client_mod

        real, transport = client_mod.MnemaClient, httpx.MockTransport(self._handle)
        monkeypatch.setattr(
            client_mod,
            "MnemaClient",
            lambda url, *, token=None: real(url, token=token, transport=transport),
        )
        monkeypatch.setenv("MNEMA_RUNNER", SERVER)
        monkeypatch.setenv("MNEMA_TOKEN", "device-token")

    def _handle(self, request: httpx.Request) -> httpx.Response:
        import json

        assert request.headers["authorization"] == "Bearer device-token"
        body = json.loads(request.content) if request.content else None
        path = request.url.path
        if path == "/api/capabilities":
            return httpx.Response(200, json={"ok": True, "llm": True})
        if path == "/api/node/asr":
            return httpx.Response(200, json={"choices": [{"id": "auto", "title": "Auto"}]})
        self.requests.append((request.method, path, body))
        self.before_answer(request.method, path)
        if path in self.refuse:
            code, answer = self.refuse.pop(path)
            return httpx.Response(code, json=answer)
        if path == "/api/model/key":
            if body["key"] not in (GOOD, None):
                return httpx.Response(400, json={"detail": "Ключ модели не подошёл"})
            models = [{"id": m, "title": m, "free": False} for m in model_stubs.GEMINI_MODELS]
            return httpx.Response(
                200, json={"models": models, "recommended": "gemini-flash-latest"}
            )
        if path == "/api/model" and request.method == "PUT":
            self.state.update(model=body["model"], key="…" + (body["key"] or GOOD)[-4:])
        elif path in ("/api/model/asr-key", "/api/model/deepl-key"):
            self.state[path.rsplit("/", 1)[1].replace("-", "_")] = "…" + body["key"][-4:]
        return httpx.Response(200, json=self.state)


def _settings_rows(rt: BarRuntime) -> dict[str, Any]:
    model = settings_model(rt.prefs, rt.menu_env())
    return {f.title: f for t in model.tabs for s in t.sections for f in s.fields}


def _settled() -> None:
    """The form, ping and list threads have ended, including those one of them started."""
    names = ("mnema-bar-ping", "mnema-bar-asr-choices", "mnema-bar-form")
    while running := [t for t in threading.enumerate() if t.name in names]:
        for t in running:
            t.join(timeout=10)


@pytest.fixture
def connected(monkeypatch, tmp_path) -> tuple[BarRuntime, _Forms, _UserServer]:
    """A Mac connected to a server, without a model of its own."""
    monkeypatch.setattr(base, "STANDALONE", True)
    monkeypatch.delenv("MNEMA_MODEL")
    monkeypatch.delenv("MNEMA_API_KEY")
    server = _UserServer(monkeypatch)
    platform = _Forms()
    rt = BarRuntime(platform=platform, prefs_file=tmp_path / "bar.json")
    _settled()
    return rt, platform, server


def test_model_window_with_a_server_sets_the_users_model_there(connected, monkeypatch) -> None:
    """With a connected server the same steps go to the server: it lists the models by the key,
    checks the model (a key refusal goes back to Key) and keeps it; this Mac's .env gets no
    model, the Model row shows the server's one."""
    rt, platform, server = connected
    monkeypatch.setattr(providers, "check_key", lambda *a: pytest.fail("the server checks"))
    rt.handle("model_setup")
    first = platform.next()
    assert first is not None and first.message == "Модель не настроена"
    key_step = platform.submit(provider="gemini")
    assert key_step is not None and key_step.fields[0].placeholder == ""
    refused = platform.submit(key=BAD)
    assert refused is not None and (refused.error, refused.clear) == (
        "Ключ модели не подошёл", ["key"],
    )  # fmt: skip
    model_step = platform.submit(key=GOOD)
    assert model_step is not None
    assert [key for key, *_, checked in _choices(model_step) if checked] == ["gemini-flash-latest"]
    server.refuse["/api/model"] = (400, {"detail": "Ключ модели не подошёл", "retry": "key"})
    back = platform.submit(model="gemini-flash-latest")
    assert back is not None and [f.id for f in back.fields] == ["key"] and back.clear == ["key"]
    platform.submit(key=GOOD)
    assert platform.submit(model="gemini-flash-latest") is None
    _settled()
    puts = [
        body for method, path, body in server.requests if (method, path) == ("PUT", "/api/model")
    ]
    assert puts[-1] == {"model": FLASH, "key": GOOD}
    assert "MNEMA_MODEL" not in dotenv_values(user_env_file())
    assert _settings_rows(rt)["Модель"].value == FLASH

    rt.handle("model_setup")  # again: the saved key stays when nothing is typed
    assert platform.next().message == f"Сейчас: {FLASH}"
    key_step = platform.submit(provider="gemini")
    assert key_step is not None and key_step.fields[0].placeholder == "Оставить …Good"
    platform.submit(key="")
    assert ("POST", "/api/model/key", {"provider": "gemini", "key": None}) in server.requests
    assert platform.submit(model="gemini-pro-latest") is None
    _settled()
    puts = [
        body for method, path, body in server.requests if (method, path) == ("PUT", "/api/model")
    ]
    assert puts[-1] == {"model": "gemini/gemini-pro-latest", "key": None}


def test_cloud_without_a_key_asks_for_the_openai_key_on_the_server(connected, monkeypatch):
    """In the cloud the user has no recognition key on the server: "Set the OpenAI key…" opens a
    window with one secret field; the server checks and keeps the key, the button goes away and
    this Mac's .env has no key."""
    rt, platform, server = connected
    rt.handle("recognition:cloud")
    _settled()
    assert _settings_rows(rt)["Задать ключ OpenAI…"].key == "asr_key_setup"
    rt.handle("asr_key_setup")
    window = platform.next()
    assert window is not None and window.title == "Ключ OpenAI для распознавания"
    assert [(f.id, f.kind) for f in window.fields] == [("key", FormFieldKind.SECRET)]
    assert window.message == "Где взять ключ OpenAI: platform.openai.com/api-keys"
    empty = platform.submit(key="")
    assert empty is not None and empty.error == "Нужен ключ"
    server.refuse["/api/model/asr-key"] = (400, {"detail": "Ключ OpenAI не подошёл"})
    refused = platform.submit(key="sk-proj-bad")
    assert refused is not None and (refused.error, refused.clear) == (
        "Ключ OpenAI не подошёл", ["key"],
    )  # fmt: skip
    assert platform.submit(key=OPENAI) is None
    _settled()
    assert server.requests.count(("PUT", "/api/model/asr-key", {"key": OPENAI})) == 1
    rows = _settings_rows(rt)
    assert "Задать ключ OpenAI…" not in rows and rows["Ключ OpenAI"].value == "…Good"
    assert "MNEMA_ASR_API_KEY" not in dotenv_values(user_env_file())
    rt.handle(rows["Сменить ключ OpenAI…"].key)  # a revoked key is replaced the same way
    platform.next()
    assert platform.submit(key=OPENAI[:-4] + "New1") is None
    _settled()
    assert _settings_rows(rt)["Ключ OpenAI"].value == "…New1"


def test_cloud_on_a_mac_without_a_server_keeps_the_key_here(monkeypatch, tmp_path) -> None:
    """Without a server the Mac recognizes in the cloud itself: the key is checked with OpenAI
    and saved in this Mac's .env."""
    monkeypatch.setattr(base, "STANDALONE", True)
    monkeypatch.delenv("MNEMA_ASR_API_KEY", raising=False)
    asked: list[str] = []

    def openai(request: httpx.Request) -> httpx.Response:
        asked.append(request.headers["authorization"])
        return httpx.Response(200, json={"data": [{"id": "gpt-4o-transcribe", "created": 1}]})

    monkeypatch.setattr(providers, "TRANSPORT", httpx.MockTransport(openai))
    platform = _Forms()
    rt = BarRuntime(platform=platform, prefs_file=tmp_path / "bar.json")
    rt.handle("recognition:cloud")
    _settled()
    assert "Задать ключ OpenAI…" in _settings_rows(rt)
    rt.handle("asr_key_setup")
    platform.next()
    assert platform.submit(key=OPENAI) is None
    _settled()
    assert asked == [f"Bearer {OPENAI}"]
    assert dotenv_values(user_env_file())["MNEMA_ASR_API_KEY"] == OPENAI
    rows = _settings_rows(rt)
    assert "Задать ключ OpenAI…" not in rows and rows["Ключ OpenAI"].value == "…Good"
    assert rows["Сменить ключ OpenAI…"].key == "asr_key_setup"


@pytest.mark.parametrize("window", ["model", "asr_key"])
def test_closing_the_window_while_the_server_saves_shows_what_it_saved(connected, window):
    """The server request cannot be taken back: closed during it, the window shows nothing more,
    and the settings show what the server kept."""
    rt, platform, server = connected
    rt.handle("recognition:cloud")
    _settled()
    saving = "/api/model" if window == "model" else "/api/model/asr-key"
    server.before_answer = lambda method, path: (
        platform.on_event(None) if (method, path) == ("PUT", saving) else None
    )
    rt.handle(f"{window}_setup")
    platform.next()
    if window == "model":
        platform.submit(provider="gemini")
        platform.submit(key=GOOD)
        values = {"model": "gemini-flash-latest"}
    else:
        values = {"key": OPENAI}
    platform.rev += 1
    platform.on_event(FormEvent(op=FormOp.SUBMIT, values=values, rev=platform.rev))
    assert platform.next() is not None  # "Checking…"
    _settled()
    assert platform.shown.empty()
    rows = _settings_rows(rt)
    if window == "model":
        assert rows["Модель"].value == FLASH
    else:
        assert rows["Ключ OpenAI"].value == "…Good"


def test_deepl_key_is_set_on_the_server_tab(connected) -> None:
    """Live translation goes by the model until the user's DeepL key is on the server; the key
    window is on the Server tab and only with a server."""
    rt, platform, server = connected
    assert _settings_rows(rt)["Живой перевод"].value == "Моделью"
    rt.handle("deepl_setup")
    window = platform.next()
    assert window is not None and window.title == "Ключ DeepL"
    assert platform.submit(key=DEEPL) is None
    _settled()
    assert ("PUT", "/api/model/deepl-key", {"key": DEEPL}) in server.requests
    assert _settings_rows(rt)["Живой перевод"].value == "DeepL"
    alone = settings_model(rt.prefs, rt.menu_env().model_copy(update={"server": None}))
    assert "Живой перевод" not in [
        f.title for t in alone.tabs for s in t.sections for f in s.fields
    ]


def test_a_key_typed_for_the_server_is_hidden_in_the_log_before_it_is_sent(connected) -> None:
    """The server checks the key, yet the Mac's log already hides it: a refusal that quotes the
    key never shows it whole."""
    from mnema.llm import redact

    rt, platform, server = connected
    typed = "plain-" + "k" * 24 + "Mine"
    rt.handle("model_setup")
    platform.next()
    platform.submit(provider="gemini")
    refused = platform.submit(key=typed)
    assert refused is not None and refused.error == "Ключ модели не подошёл"
    assert ("POST", "/api/model/key", {"provider": "gemini", "key": typed}) in server.requests
    assert typed not in redact.redact(f"the server refused {typed}")


def test_deepl_key_without_a_server_is_refused_and_saved_nowhere(monkeypatch, tmp_path) -> None:
    """DeepL translates only on the server: without one the window refuses and asks nobody."""
    from mnema.bar.forms import KeyFlow

    monkeypatch.setattr(
        providers, "TRANSPORT", httpx.MockTransport(lambda r: pytest.fail("no network"))
    )
    refused = KeyFlow("deepl", None).submit({"key": DEEPL}, force=False)
    assert refused is not None and refused.error == "Сервер не подключён"
    assert not user_env_file().exists() or not dotenv_values(user_env_file())
