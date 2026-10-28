"""Model selection steps shared by `mnema model` and the Mac model window: the key
(providers.check_key) and the test request (claude.check_model).

Mocked boundaries: the provider network (providers.TRANSPORT) and litellm.completion.
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from mnema.core.base import MediaError
from mnema.llm import claude, providers
from mnema.llm.config import credentials_for
from mnema.llm.providers import Provider
from mnema.llm.redact import redact
from tests import model_stubs
from tests.model_stubs import error

GOOD = "AIza" + "g" * 31 + "Good"


def test_refused_key_is_hidden_in_the_log(monkeypatch) -> None:
    """A key with invalid characters looks like no provider's key, yet it stays hidden in the log
    after the refusal: what was typed is a secret."""
    model_stubs.gemini(monkeypatch, GOOD)
    key = f"AIza ключ {uuid.uuid4().hex}"  # one per test: the process's hidden keys are not reset
    assert key in redact(f"журнал: {key}")
    with pytest.raises(MediaError):
        providers.check_key(Provider.GEMINI, key)
    assert key not in redact(f"журнал: {key}")


def test_no_connection_to_the_provider_is_named_in_words(monkeypatch) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(providers, "TRANSPORT", httpx.MockTransport(down))
    with pytest.raises(MediaError, match="^Нет связи с сервисом модели$"):
        providers.check_key(Provider.GEMINI, GOOD)


def test_key_without_a_known_owner_is_sent(monkeypatch) -> None:
    """ "sk-" is used by DeepSeek and older OpenAI keys: such a key goes to the OpenAI provider."""
    key = "sk-" + "d" * 32
    seen: list[str] = []

    def openai(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        return httpx.Response(200, json={"data": [{"id": "gpt-5", "created": 1}]})

    monkeypatch.setattr(providers, "TRANSPORT", httpx.MockTransport(openai))
    assert [m.id for m in providers.check_key(Provider.OPENAI, key)] == ["gpt-5"]
    assert seen == [f"Bearer {key}"]


def _creds():
    return credentials_for("gemini/gemini-flash-latest", GOOD)


@pytest.mark.parametrize(
    ("answer", "text", "key"),
    [
        (
            error("Insufficient Balance", 402),
            "Закончились деньги на счёте: введите другой ключ",
            True,
        ),
        (
            error("API key was reported as leaked", 403),
            "Ключ модели отключён: введите другой ключ",
            True,
        ),
        ("", "Модель вернула пустой ответ: выберите другую модель", False),
    ],
    ids=["no-money", "leaked", "empty"],
)
def test_refusal_says_what_to_change(monkeypatch, answer, text, key) -> None:
    """Wrong key (rejected, disabled, out of money): back to the key; otherwise another model."""
    stub = model_stubs.Litellm(monkeypatch)
    stub.answers["gemini/gemini-flash-latest"] = answer
    refusal = claude.check_model(_creds())
    assert refusal is not None and (refusal.text, refusal.key) == (text, key)


def test_closing_the_window_ends_the_retry_pause_at_once(monkeypatch) -> None:
    """The model is overloaded: a retry after a pause; when on raises (the window was closed) the
    check ends at once, with no pause and no second request, and it is not a model refusal: on's
    exception propagates as is."""
    stub = model_stubs.Litellm(monkeypatch)
    stub.answers["gemini/gemini-flash-latest"] = [error("The model is overloaded", 503), "готово"]

    class Closed(Exception):
        pass

    def on(_fraction, message: str) -> None:
        if message.startswith("Модель занята"):
            # A key refusal text: it must not be mistaken for a model refusal.
            raise Closed("API key not valid")

    with pytest.raises(Closed):
        claude.check_model(_creds(), on=on)
    assert len(stub.calls) == 1 and stub.sleeps == []


def test_overloaded_model_is_asked_again(monkeypatch) -> None:
    stub = model_stubs.Litellm(monkeypatch)
    stub.answers["gemini/gemini-flash-latest"] = [error("The model is overloaded", 503), "готово"]
    assert claude.check_model(_creds()) is None
    assert len(stub.calls) == 2 and sum(stub.sleeps) == 5.0
