"""`mnema model`: provider, key, model, the check, and the lines of ~/.config/mnema/.env.

Mocked boundaries: input (input, getpass), the provider network (providers.TRANSPORT) and
litellm.completion; the settings file is in the test's tmp (conftest).
"""

from __future__ import annotations

import getpass
import os

import pytest

from mnema import config
from mnema.cli.parser import build_parser
from mnema.llm.config import LLMSettings
from mnema.llm.redact import redact
from tests import model_stubs
from tests.model_stubs import chunk, error

GOOD = "AIza" + "g" * 31 + "Good"
SAVED = "AIza" + "s" * 31 + "Save"
BAD = "AIza" + "b" * 31 + "Bad1"


@pytest.fixture(autouse=True)
def _no_model(monkeypatch):
    for name in ("MNEMA_MODEL", "MNEMA_API_KEY", "MNEMA_API_BASE", "MNEMA_ASR_API_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def gemini(monkeypatch) -> list[str]:
    """Gemini at the boundary: accepts GOOD and SAVED. Returns the request keys."""
    return model_stubs.gemini(monkeypatch, GOOD, SAVED)


@pytest.fixture
def model_calls(monkeypatch):
    """litellm at the boundary: answers[model] is a text or an exception; the calls (kwargs)."""
    stub = model_stubs.Litellm(monkeypatch)
    return stub.calls, stub.answers


def _typed(monkeypatch, lines: list[str], secrets: list[str]) -> list[str]:
    """Answers in order: input gets lines, getpass gets secrets. Returns the prompts asked."""
    asked: list[str] = []

    def ask(queue: list[str]):
        def answer(prompt: str = "") -> str:
            asked.append(prompt)
            if not queue:
                pytest.fail(f"unexpected prompt: {prompt}")
            return queue.pop(0)

        return answer

    monkeypatch.setattr("builtins.input", ask(lines))
    monkeypatch.setattr(getpass, "getpass", ask(secrets))
    return asked


def _run(*flags: str) -> None:
    """`mnema model …` through the real argument parsing."""
    args = build_parser().parse_args(["model", *flags])
    args.func(args)


def _env(*lines: str):
    path = config.user_env_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{line}\n" for line in lines))
    return path


def test_four_steps_write_the_model_and_keep_other_lines(
    monkeypatch, capsys, gemini, model_calls
) -> None:
    """Provider by number; a wrong key gives the reason and asks again; Enter takes the
    recommended model; once it answers, two lines in .env (others intact, 600, folder 700), no
    key in the output; LLMSettings sees the model without a restart."""
    path = _env("# мои настройки", "MNEMA_RUNNER=http://runner:8000", "DEEPL_API_KEY=deepl-keep")
    asked = _typed(monkeypatch, ["1", ""], [BAD, GOOD])
    _run()
    out = capsys.readouterr()
    assert "Модель не настроена" in out.out
    assert "Ключ модели не подошёл" in out.out and "Модель отвечает" in out.out
    assert gemini == [BAD, GOOD] and len([p for p in asked if p.startswith("Ключ Gemini")]) == 2
    calls, _ = model_calls
    [call] = calls
    assert (call["model"], call["api_key"]) == ("gemini/gemini-flash-latest", GOOD)
    assert call["max_tokens"] <= 2048 and call["stream"] is True
    text = path.read_text()
    assert text.startswith("# мои настройки\nMNEMA_RUNNER=http://runner:8000\n")
    assert "DEEPL_API_KEY=deepl-keep" in text
    assert path.stat().st_mode & 0o777 == 0o600 and path.parent.stat().st_mode & 0o777 == 0o700
    settings = LLMSettings()
    assert (settings.source, settings.model, settings.api_key) == (
        "env", "gemini/gemini-flash-latest", GOOD,
    )  # fmt: skip
    for secret in (GOOD, BAD):
        assert secret not in out.out + out.err
        assert secret not in redact(f"ответ провайдера: {secret}")  # and in the log before saving
    assert "…Good" in out.out


def test_enter_keeps_the_saved_key(monkeypatch, capsys, gemini, model_calls) -> None:
    """A saved model of the same provider: Enter keeps the provider and the key (checked by the
    same GET), the model by number."""
    _env("MNEMA_MODEL=gemini/gemini-pro-latest", f"MNEMA_API_KEY={SAVED}")
    asked = _typed(monkeypatch, ["", "3"], [""])
    _run()
    out = capsys.readouterr().out
    assert "Сейчас: gemini/gemini-pro-latest, ключ …Save" in out
    assert any("[оставить …Save]" in p for p in asked)
    assert gemini == [SAVED]
    settings = LLMSettings()
    assert (settings.model, settings.api_key, settings.api_base) == (
        "gemini/gemini-flash-lite-latest", SAVED, None,
    )  # fmt: skip
    assert SAVED not in out


def test_gateway_key_is_not_offered_to_the_provider(monkeypatch, capsys, gemini, model_calls):
    """A model behind a gateway (MNEMA_API_BASE): the gateway key is neither offered nor sent to
    the provider; after a new key the gateway address is gone."""
    gateway = "gateway-secret-123456"
    path = _env(
        "MNEMA_MODEL=gemini/gemini-pro-latest",
        f"MNEMA_API_KEY={gateway}",
        "MNEMA_API_BASE=https://gateway.example/v1",
    )
    asked = _typed(monkeypatch, ["", ""], [GOOD])
    _run()
    assert not any("оставить" in p for p in asked)
    assert gemini == [GOOD] and gateway not in str(model_calls[0])
    settings = LLMSettings()
    assert (settings.model, settings.api_key, settings.api_base) == (
        "gemini/gemini-flash-latest", GOOD, None,
    )  # fmt: skip
    assert "MNEMA_API_BASE" not in path.read_text() and gateway not in capsys.readouterr().out


def test_key_of_another_provider_is_not_sent(monkeypatch, capsys, gemini, model_calls) -> None:
    """Another provider's key: the reason, asked again, no request."""
    wrong = "sk-or-v1-" + "o" * 30
    _typed(monkeypatch, ["1", ""], [wrong, GOOD])
    _run()
    out = capsys.readouterr().out
    assert "Это ключ OpenRouter" in out and wrong not in out
    assert gemini == [GOOD]


def test_failed_check_goes_back_to_the_model(monkeypatch, capsys, gemini, model_calls) -> None:
    """The model did not answer (no access): the reason and the model choice again; the file is
    written only after an answer."""
    calls, answers = model_calls
    answers["gemini/gemini-flash-latest"] = error("models/gemini-flash-latest is not found", 404)
    path = _env("DEEPL_API_KEY=deepl-keep")

    def typed_model(prompt: str = "") -> str:
        assert path.read_text() == "DEEPL_API_KEY=deepl-keep\n"  # unchanged before the check
        return lines.pop(0)

    lines = ["1", "", "1"]
    monkeypatch.setattr("builtins.input", typed_model)
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": GOOD)
    _run()
    out = capsys.readouterr().out
    assert "Нет доступа к модели: выберите другую модель" in out
    assert [c["model"] for c in calls] == ["gemini/gemini-flash-latest", "gemini/gemini-pro-latest"]
    assert LLMSettings().model == "gemini/gemini-pro-latest"


def test_rejected_key_at_check_goes_back_to_the_key(
    monkeypatch, capsys, gemini, model_calls
) -> None:
    """The key passed the model list but generation answered 401: asked for the key again."""

    def completion_by_key(**kw):
        if kw["api_key"] == SAVED:
            return iter([chunk("готово")])
        raise error("API key not valid", 401)

    import litellm

    monkeypatch.setattr(litellm, "completion", completion_by_key)
    _typed(monkeypatch, ["1", "", ""], [GOOD, SAVED])
    _run()
    out = capsys.readouterr().out
    assert "Ключ модели не подошёл: введите другой ключ" in out
    assert gemini == [GOOD, SAVED] and LLMSettings().api_key == SAVED


def test_ctrl_c_leaves_the_file_unchanged(monkeypatch, gemini, model_calls) -> None:
    path = _env("MNEMA_MODEL=gemini/gemini-pro-latest", f"MNEMA_API_KEY={SAVED}")
    before = path.read_bytes()

    def interrupted(prompt: str = "") -> str:
        raise KeyboardInterrupt

    _typed(monkeypatch, ["1"], [])
    monkeypatch.setattr(getpass, "getpass", interrupted)
    with pytest.raises(KeyboardInterrupt):
        _run()
    assert path.read_bytes() == before and model_calls[0] == []


def test_show_prints_only_the_current_line(monkeypatch, capsys) -> None:
    _typed(monkeypatch, [], [])
    _run("--show")
    assert capsys.readouterr().out.strip() == "Модель не настроена"
    _env("MNEMA_MODEL=deepseek/deepseek-chat", "MNEMA_API_KEY=sk-deepseek-1234567890abcd")
    _run("--show")
    assert capsys.readouterr().out.strip() == "Сейчас: deepseek/deepseek-chat, ключ …abcd"
    monkeypatch.setenv("MNEMA_MODEL", "openai/gpt-5")  # the process environment wins over the file
    _run("--show")
    assert capsys.readouterr().out.strip() == (
        "Сейчас: openai/gpt-5, ключа нет, из окружения процесса"
    )  # the file key is never given to the environment model


def test_remove_drops_the_model_lines_only(capsys) -> None:
    path = _env(
        "MNEMA_RUNNER=http://runner:8000",
        "MNEMA_MODEL=gemini/gemini-flash-latest",
        f"MNEMA_API_KEY={SAVED}",
        "MNEMA_ASR_API_KEY=gsk_" + "a" * 20,
    )
    _run("--remove")
    assert "Модель убрана из" in capsys.readouterr().out
    assert (
        path.read_text()
        == "MNEMA_RUNNER=http://runner:8000\nMNEMA_ASR_API_KEY=gsk_" + "a" * 20 + "\n"
    )
    assert LLMSettings().source == "unconfigured"
    _run("--remove")
    assert "модели нет" in capsys.readouterr().out


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_unreadable_settings_file_does_not_stop_commands(monkeypatch, capsys, caplog) -> None:
    """~/.config/mnema/.env is unreadable (another owner): commands keep working, the reason goes
    to the log instead of a traceback."""
    path = config.user_env_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("MNEMA_MODEL=gemini/x\n")
    path.chmod(0)
    try:
        config.load_env()
        _run("--show")
    finally:
        path.chmod(0o600)
    assert str(path) in caplog.text


def test_the_four_steps_speak_english(monkeypatch, capsys, gemini, model_calls) -> None:
    """In English the steps, the prompts and the result lines of `mnema model` carry no
    Russian (the provider's own refusal texts belong to the llm layer)."""
    import re

    from mnema import i18n

    i18n.set_language("en")
    asked = _typed(monkeypatch, ["1", ""], [GOOD])
    _run()
    out = capsys.readouterr().out
    for line in ("1. Provider", "2. Key", "3. Model", "4. Check", "The model answers"):
        assert line in out
    assert "Saved to " in out and "key …Good" in out
    assert asked[0].startswith("Number") and asked[1].startswith("Gemini key")
    cyrillic = re.compile("[А-Яа-яЁё]")
    assert not [p for p in asked if cyrillic.search(p)]
    own = [ln for ln in out.splitlines() if ln.startswith(("1.", "2.", "3.", "4.", "  "))]
    assert own and not [ln for ln in own if cyrillic.search(ln)]
