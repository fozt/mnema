"""The "Summary model" window on the Mac (bar/forms.ModelFlow through BarRuntime.handle and window
events): the steps and texts of `mnema model`; the key lives only in the window field.

Mocked boundaries: the provider network (providers.TRANSPORT) and litellm.completion.
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
