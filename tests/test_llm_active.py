"""One model, no fallback: ~/.config/mnema/.env lines or the process environment, or no model;
model limits and request sizes.

The external boundaries are mocked: litellm.completion and the litellm table, the provider
network (providers.TRANSPORT).
"""

from __future__ import annotations

import os
import re
import types
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from mnema import config
from mnema.core.base import MediaError
from mnema.llm import claude, errors, limits, providers
from mnema.llm.config import Credentials, LLMSettings, current_line
from mnema.llm.errors import ErrorKind
from mnema.llm.notes import Cloud

GEMINI_KEY = "AIza" + "g" * 31 + "a1B2"


@pytest.fixture(autouse=True)
def _no_provider_network(monkeypatch):
    """Provider model descriptions (limits) answer 404, so the litellm table is used; own cache."""
    monkeypatch.setattr(
        providers, "TRANSPORT", httpx.MockTransport(lambda r: httpx.Response(404, json={}))
    )
    monkeypatch.setattr(limits, "_FETCHED", {})


# ── error classification ──


def _err(text: str, code: int | None = None, name: str = "ProviderError") -> Exception:
    e = type(name, (Exception,), {})(text)
    if code:
        e.status_code = code  # type: ignore[attr-defined]
    return e


def test_classify_provider_errors() -> None:
    """Refusal kind: 429 is a limit (daily for a PerDay quota) even with "billing" in the text;
    money only for 402 and explicit balance words; key, overload, connection."""
    kind = errors.ErrorKind
    free_tier = (
        "You exceeded your current quota, please check your plan and billing details. "
        '"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"'
    )
    cases = [
        (_err(free_tier, 429), kind.RATE_LIMIT),
        (_err(free_tier.replace("PerMinute", "PerDay"), 429), kind.DAILY_LIMIT),
        (_err("Insufficient Balance", 402), kind.NO_MONEY),
        (_err("DeepSeekException - Insufficient Balance"), kind.NO_MONEY),
        (_err("Your credit balance is too low to access the API", 400), kind.NO_MONEY),
        (_err("API key not valid. Please pass a valid API key.", 400), kind.INVALID_KEY),
        (_err("unauthorized", 401), kind.INVALID_KEY),
        (_err("This model is currently experiencing high demand.", 503), kind.OVERLOADED),
        (_err("The model is overloaded."), kind.OVERLOADED),
        (_err("bad gateway", 502), kind.OVERLOADED),
        (_err("timed out", name="ReadTimeout"), kind.NETWORK),
        (_err("peer closed connection without sending complete message body"), kind.NETWORK),
        (_err("boom", 500, name="APIConnectionError"), kind.NETWORK),
        (_err("bad request", 400), kind.OTHER),
        (_err("insufficient_quota: You exceeded your current quota", 429), kind.NO_MONEY),
        (_err("Service unavailable in your region", 400), kind.REGION),
        (_err("Consumer 'api_key:AIza…' has been suspended.", 403), kind.DISABLED),
        (
            _err(
                "The free tier of the model has been exhausted. AllocationQuota.FreeTierOnly", 403
            ),
            kind.NO_MODEL,
        ),  # for good: no retry and no status reset after a minute
    ]
    assert [errors.classify(e) for e, _ in cases] == [k for _, k in cases]
    assert errors.human_reason(cases[0][0]) == "превышен лимит запросов"  # not "money"
    assert errors.human_reason(cases[1][0]) == "исчерпан дневной лимит запросов"


def test_retry_after_from_header_or_gemini_retry_info() -> None:
    hinted = _err("rate limited", 429)
    hinted.response = httpx.Response(429, headers={"Retry-After": "7"})  # type: ignore[attr-defined]
    assert errors.retry_after(hinted) == 7.0
    gemini = _err('{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "16.5s"}')
    assert errors.retry_after(gemini) == 16.5
    assert errors.retry_after(_err("nothing")) is None


# ── model from ~/.config/mnema/.env ──


def _env_file(monkeypatch, *lines: str):
    """~/.config/mnema/.env with these lines and no model in the process environment."""
    for name in ("MNEMA_MODEL", "MNEMA_API_KEY", "MNEMA_API_BASE", "MNEMA_ASR_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    path = config.user_env_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{line}\n" for line in lines))
    return path


def _models(monkeypatch, model: str = "gemini/gemini-flash-latest", key: str = GEMINI_KEY):
    """Model and key as .env lines (written by mnema model)."""
    return _env_file(monkeypatch, "DEEPL_API_KEY=deepl-other", f"MNEMA_MODEL={model}",
                     f"MNEMA_API_KEY={key}")  # fmt: skip


def _chunk(text: str | None, reason: str | None = None):
    delta = types.SimpleNamespace(content=text)
    return types.SimpleNamespace(choices=[types.SimpleNamespace(delta=delta, finish_reason=reason)])


def _litellm(monkeypatch, answer) -> list[dict]:
    """litellm at the boundary: answer(kwargs) → text or an exception. Returns the calls."""
    import litellm

    calls: list[dict] = []

    def completion(**kw):
        calls.append(kw)
        out = answer(kw)
        if isinstance(out, BaseException):
            raise out
        return iter([_chunk(out, "stop")])

    monkeypatch.setattr(litellm, "completion", completion)
    monkeypatch.setattr(claude.time, "sleep", lambda s: None)
    return calls


_SUMMARY_JSON = '{"title": "Дейли", "about": "Дейли команды.", "summary": ["Решили."]}'


def test_model_from_env_file_answers_with_its_own_key(monkeypatch) -> None:
    """A model from .env: litellm gets its name and key even with a different GEMINI_API_KEY in
    the environment."""
    _models(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-чужой")
    calls = _litellm(monkeypatch, lambda kw: _SUMMARY_JSON)
    s = claude.title_summary_ex("mic: обсудили релиз")
    assert (s.title, s.engine, s.note) == ("Дейли", "cloud", None)
    [call] = calls
    assert (call["model"], call["api_key"]) == ("gemini/gemini-flash-latest", GEMINI_KEY)
    assert "MNEMA_MODEL" not in os.environ


def test_daily_limit_leaves_no_summary_and_no_other_model(monkeypatch) -> None:
    """Gemini answers with its daily limit: no summary, no other model called; the note has the
    kind and the reset time (midnight in Los Angeles). The next call still goes to it."""
    _models(monkeypatch)
    daily = _err('"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"', 429)
    calls = _litellm(monkeypatch, lambda kw: daily)
    s = claude.title_summary_ex("mic: обсудили релиз")
    assert (s.title, s.points, s.engine) == ("", [], "none")
    note = s.note
    assert note is not None and (note.cloud, note.kind, note.model) == (
        Cloud.FAILED, ErrorKind.DAILY_LIMIT, "gemini-flash-latest",
    )  # fmt: skip
    la = note.until.astimezone(ZoneInfo("America/Los_Angeles"))
    assert (la.hour, la.minute) == (0, 0) and note.until > datetime.now().astimezone()
    assert {c["model"] for c in calls} == {"gemini/gemini-flash-latest"}

    claude.title_ex("mic: обсудили релиз")
    assert len(calls) == 2  # an earlier refusal does not block the call

    calls = _litellm(monkeypatch, lambda kw: _SUMMARY_JSON)
    assert claude.title_summary_ex("mic: обсудили релиз").title == "Дейли"


def test_rate_limit_retries_then_gives_up_on_the_same_model(monkeypatch) -> None:
    _models(monkeypatch)
    calls = _litellm(monkeypatch, lambda kw: _err("Too Many Requests", 429))
    title, engine, note = claude.title_ex("mic: обсудили релиз")
    assert (title, engine) == ("", "none")
    assert note is not None and note.kind == ErrorKind.RATE_LIMIT and note.until is not None
    assert len(calls) == 4 and {c["model"] for c in calls} == {"gemini/gemini-flash-latest"}


@pytest.mark.parametrize("lines", [(), ("MNEMA_API_KEY=" + GEMINI_KEY,)], ids=["empty", "key"])
def test_no_model_leaves_title_and_summary_unavailable(monkeypatch, lines) -> None:
    """No model in .env or the environment (a key without a model too): no generation."""
    _env_file(monkeypatch, *lines)
    calls = _litellm(monkeypatch, lambda kw: pytest.fail("модель не выбрана"))
    summary = claude.title_summary_ex("mic: обсудили релиз\n" * 20_000)
    assert (summary.title, summary.points, summary.engine) == ("", [], "none")
    assert summary.note is not None and summary.note.cloud == Cloud.NOT_SET
    title, engine, note = claude.title_ex("mic: обсудили релиз")
    assert (title, engine) == ("", "none")
    assert note is not None and note.cloud == Cloud.NOT_SET
    assert LLMSettings().source == "unconfigured" and not claude.llm_available()
    with pytest.raises(MediaError, match="^Не настроена модель$"):
        claude.translate("mic: привет")
    assert calls == []


def test_summary_parts_stay_on_one_model_when_env_file_changes(monkeypatch) -> None:
    """The .env model changes in the middle of a summary in parts: the remaining parts and the final
    pass use the model whose limits cut the text; the next call uses the new one, no restart."""
    path = _models(monkeypatch)
    monkeypatch.setattr(limits, "model_limits", lambda creds: limits.Limits(
        input_tokens=30_720, output_tokens=8_192))  # fmt: skip

    def answer(kw):
        if len(calls) == 1:  # while the first part ran, mnema model saved another model
            path.write_text(
                f"MNEMA_MODEL=gemini/gemini-flash-lite-latest\nMNEMA_API_KEY={GEMINI_KEY}\n"
            )
        return _SUMMARY_JSON if "ПОЛНУЮ" in kw["messages"][0]["content"] else "- обсудили"

    calls = _litellm(monkeypatch, answer)
    text = "\n".join(["mic: " + "слово " * 40] * 1000)
    assert claude.title_summary_ex(text).engine == "cloud" and len(calls) > 2
    assert {c["model"] for c in calls} == {"gemini/gemini-flash-latest"}
    claude.title_ex("mic: текст")
    assert calls[-1]["model"] == "gemini/gemini-flash-lite-latest"


def test_second_summary_part_failure_leaves_no_summary(monkeypatch) -> None:
    """One model does every part of a text: if the second part fails there is no summary at all and
    no final pass is requested."""
    _models(monkeypatch)
    monkeypatch.setattr(limits, "model_limits", lambda creds: limits.Limits(
        input_tokens=30_720, output_tokens=8_192))  # fmt: skip

    def answer(kw):
        system, part = kw["messages"][0]["content"], kw["messages"][1]["content"]
        if "КУСОК" in system and part != calls[0]["messages"][1]["content"]:
            return _err(
                "Too Many Requests", 429
            )  # the second part hits a limit, even after retries
        return "- обсудили часть"

    calls = _litellm(monkeypatch, answer)
    text = "\n".join(["mic: " + "слово " * 40] * 1000)  # ~245k characters: parts for qwen-max
    s = claude.title_summary_ex(text)
    assert (s.engine, s.points) == ("none", [])
    assert not any("ПОЛНУЮ" in c["messages"][0]["content"] for c in calls)


# ── process environment and file ──


def test_process_environment_overrides_the_env_file(monkeypatch) -> None:
    """The .env lines are the default model; MNEMA_MODEL in the process environment (service,
    export) takes model, key and address from there, not from the file: a Gemini key never goes
    to OpenAI nor the environment model to the file's gateway; load_env does not copy model
    lines into the environment."""
    path = _models(monkeypatch)
    with path.open("a") as file:
        file.write("MNEMA_API_BASE=https://gateway.example/v1\n")
    settings = LLMSettings()
    assert (settings.source, settings.model) == ("env", "gemini/gemini-flash-latest")
    calls = _litellm(monkeypatch, lambda kw: "Имя")
    claude.title_ex("mic: текст")
    assert (calls[0]["model"], calls[0]["api_key"]) == ("gemini/gemini-flash-latest", GEMINI_KEY)

    monkeypatch.setenv("MNEMA_MODEL", "openai/gpt-5")
    monkeypatch.setenv("MNEMA_API_KEY", "sk-env")
    claude.title_ex("mic: текст")
    assert (calls[1]["model"], calls[1]["api_key"]) == ("openai/gpt-5", "sk-env")
    assert calls[1].get("api_base") is None  # the file's gateway is not for the environment model
    monkeypatch.delenv("MNEMA_API_KEY")
    with pytest.raises(MediaError, match="^Не задан ключ модели$"):
        LLMSettings().credentials()  # the file's key is not used for the environment model

    monkeypatch.setenv("MNEMA_API_KEY", "sk-env")  # an environment key without its model: none
    monkeypatch.delenv("MNEMA_MODEL")
    creds = LLMSettings().credentials()
    assert (creds.model, creds.api_key) == ("gemini/gemini-flash-latest", GEMINI_KEY)
    monkeypatch.delenv("MNEMA_API_KEY")
    monkeypatch.delenv("DEEPL_API_KEY", raising=False)  # the environment is restored after the test
    config.load_env()
    assert {"MNEMA_MODEL", "MNEMA_API_KEY"}.isdisjoint(os.environ)
    assert os.environ["DEEPL_API_KEY"] == "deepl-other"  # other lines are still copied


def test_qwen_without_base_goes_to_the_international_region(monkeypatch) -> None:
    """mnema model does not write MNEMA_API_BASE: Qwen uses the international region; a custom
    address overrides it."""
    path = _models(monkeypatch, "dashscope/qwen-plus", "sk-qwen-key-12345678")
    assert LLMSettings().credentials().api_base == providers.QWEN_BASE
    with path.open("a") as file:
        file.write("MNEMA_API_BASE=https://dashscope.aliyuncs.com/compatible-mode/v1\n")
    assert LLMSettings().credentials().api_base.startswith("https://dashscope.aliyuncs.com")


# ── model limits and request sizes ──


def _limits(monkeypatch, input_tokens: int, output_tokens: int) -> None:
    import litellm

    info = {"max_input_tokens": input_tokens, "max_output_tokens": output_tokens}
    monkeypatch.setattr(litellm, "get_model_info", lambda model: info)


@pytest.mark.parametrize(
    ("window", "out", "summary_calls"),
    [(131_072, 8_192, 1), (30_720, 8_192, 4)],
    ids=["deepseek-chat", "qwen-max"],
)
def test_summary_one_pass_follows_model_input(monkeypatch, window, out, summary_calls) -> None:
    """200k characters: one pass for DeepSeek (131k input), three parts and a final pass for
    qwen-max (30k)."""
    monkeypatch.setenv("MNEMA_MODEL", "deepseek/deepseek-chat")
    _limits(monkeypatch, window, out)
    calls = _litellm(monkeypatch, lambda kw: _SUMMARY_JSON)
    text = "\n".join(["mic: " + "обсудили сроки " * 13] * 1000)
    assert 190_000 < len(text) < 210_000
    claude.title_summary_ex(text)
    assert len(calls) == summary_calls


def test_summary_single_long_track_and_reduced_parts_fit_model_input(monkeypatch) -> None:
    """A long line of one track is split with its labels; bulky part answers are shortened too
    before the final pass. No request exceeds its budget and the last part's data is kept.
    """
    monkeypatch.setenv("MNEMA_MODEL", "deepseek/deepseek-chat")
    _limits(monkeypatch, 7000, 8192)
    decisions: list[str] = []

    def answer(kw):
        system, content = kw["messages"][0]["content"], kw["messages"][1]["content"]
        if "ПОЛНУЮ" in system:
            assert all(decision in content for decision in decisions)
            return _SUMMARY_JSON
        if not content.startswith("Собеседники: "):
            return "\n".join(re.findall(r"решение-\d+", content))
        assert content.startswith("Собеседники: ")
        decision = f"решение-{len(decisions) + 1}"
        decisions.append(decision)
        return "- " + decision + ". " + "Содержание. " * 330

    calls = _litellm(monkeypatch, answer)
    result = claude.title_summary_ex("system: " + "обсудили задачу " * 4000)
    assert result.engine == "cloud" and result.title
    assert len(decisions) >= 5
    assert any(c["messages"][1]["content"].startswith("Часть") for c in calls[:-1])
    for call in calls:
        budget = (7000 - 2000 - call["max_tokens"]) * 3
        assert len(call["messages"][1]["content"]) <= budget


def test_summary_stops_when_model_does_not_shorten_parts(monkeypatch) -> None:
    monkeypatch.setenv("MNEMA_MODEL", "deepseek/deepseek-chat")
    _limits(monkeypatch, 7000, 8192)
    calls = _litellm(monkeypatch, lambda kw: kw["messages"][1]["content"])
    result = claude.title_summary_ex("system: " + "обсудили задачу " * 4000)
    assert result.engine == "none" and not result.points and not result.title
    assert result.note and "сократить пересказ" in result.note.cloud_detail
    assert len(calls) < 10
    assert not any("ПОЛНУЮ" in c["messages"][0]["content"] for c in calls)


def test_gemini_behind_gateway_skips_google(monkeypatch) -> None:
    """Gemini with a custom address (gateway): Google is not asked for the description, the text
    goes to the gateway."""
    monkeypatch.setenv("MNEMA_MODEL", "gemini/gemini-flash-latest")
    monkeypatch.setenv("MNEMA_API_BASE", "https://gateway.example/v1")
    direct: list[str] = []

    def google(request: httpx.Request) -> httpx.Response:
        direct.append(str(request.url))
        return httpx.Response(500)

    monkeypatch.setattr(providers, "TRANSPORT", httpx.MockTransport(google))
    calls = _litellm(monkeypatch, lambda kw: _SUMMARY_JSON)
    claude.title_summary_ex("mic: Ну, привет.")
    assert direct == [] and calls[0]["api_base"] == "https://gateway.example/v1"


ROUTER_KEY = "sk-or-v1-" + "r" * 60 + "Rr09"
FREE_QWEN = "qwen/qwen3.8-27b:free"


def _router(monkeypatch) -> list[str]:
    """A free Qwen via OpenRouter is active; the public model list at the network boundary (window
    262144, answer cap 235929). Returns the OpenRouter request URLs."""
    _models(monkeypatch, f"openrouter/{FREE_QWEN}", ROUTER_KEY)
    asked: list[str] = []
    top = {"context_length": 262144, "max_completion_tokens": 235929}
    listing = {"data": [{"id": FREE_QWEN, "pricing": {"prompt": "0"}, "top_provider": top}]}

    def router(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET", request.url
        asked.append(str(request.url))
        return httpx.Response(200, json=listing)

    monkeypatch.setattr(providers, "TRANSPORT", httpx.MockTransport(router))
    return asked


def test_openrouter_limits_come_from_its_list(monkeypatch) -> None:
    """OpenRouter model limits come from the public list (once per process), not a guess."""
    import litellm

    asked = _router(monkeypatch)
    monkeypatch.setattr(litellm, "get_model_info", lambda model: pytest.fail("справочник"))
    creds = LLMSettings().credentials()
    assert limits.model_limits(creds) == limits.Limits(input_tokens=262144, output_tokens=235929)
    assert limits.one_pass_chars(creds, 2500) == 300_000
    limits.model_limits(creds)

    assert asked == ["https://openrouter.ai/api/v1/models"]


def test_limits_description_then_litellm(monkeypatch) -> None:
    """Limits: Gemini from Google's model description (once per process, the key in a header); an
    unknown model gets 32768/8192."""
    import litellm

    _models(monkeypatch)
    asked: list[httpx.Request] = []

    def google(request: httpx.Request) -> httpx.Response:
        asked.append(request)
        body = {"name": "models/gemini-flash-latest", "inputTokenLimit": 1048576,
                "outputTokenLimit": 65535}  # fmt: skip
        return httpx.Response(200, json=body)

    monkeypatch.setattr(providers, "TRANSPORT", httpx.MockTransport(google))
    creds = LLMSettings().credentials()
    assert limits.model_limits(creds).output_tokens == 65535
    limits.model_limits(creds)
    assert len(asked) == 1 and asked[0].headers["x-goog-api-key"] == GEMINI_KEY
    assert "key=" not in str(asked[0].url)

    monkeypatch.setattr(litellm, "get_model_info", lambda model: (_ for _ in ()).throw(KeyError()))
    odd = Credentials(model="openai/никому-не-известная", api_key="sk-x")
    assert limits.model_limits(odd) == limits.Limits(input_tokens=32768, output_tokens=8192)
    assert limits.one_pass_chars(odd, 2500) == (32768 - 2000 - 2500) * 3


def test_failed_model_description_is_not_asked_again_for_ten_minutes(monkeypatch) -> None:
    """Google's model description failed (country, timeout): later calls do not wait for it and use
    the litellm table; after 10 minutes it is asked again; a success lasts for the process."""
    _models(monkeypatch)
    _limits(monkeypatch, 1_000_000, 8_192)
    region = {"error": {"code": 400, "message": "User location is not supported for the API use.",
                        "status": "FAILED_PRECONDITION"}}  # fmt: skip
    found = {"name": "models/gemini-flash-latest", "outputTokenLimit": 65535}
    answer = {"code": 400, "body": region}
    asked: list[str] = []

    def google(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        return httpx.Response(answer["code"], json=answer["body"])

    monkeypatch.setattr(providers, "TRANSPORT", httpx.MockTransport(google))
    clock = [1000.0]
    monkeypatch.setattr(limits.time, "monotonic", lambda: clock[0])
    creds = LLMSettings().credentials()

    def output() -> int | None:
        return limits.model_limits(creds).output_tokens

    assert (output(), output(), len(asked)) == (8_192, 8_192, 1)
    clock[0] += 9 * 60
    assert (output(), len(asked)) == (8_192, 1)
    clock[0] += 2 * 60
    answer.update(code=200, body=found)
    assert (output(), len(asked)) == (65535, 2)
    clock[0] += 24 * 3600
    assert (output(), len(asked)) == (65535, 2)


# ── parsing the model answer ──


def test_title_summary_parses_model_shapes() -> None:
    """Model answers in different shapes: JSON in a ``` wrapper, object points, truncated JSON,
    non-JSON."""

    def parsed(raw: str) -> tuple[str, str, list[str]]:
        r = claude._parse_summary(raw)
        return r.title, r.about, r.points

    assert parsed(
        '```json\n{"title": "Дейли", "about": "Дейли команды.", "summary": ["А", "Б"]}\n```'
    ) == ("Дейли", "Дейли команды.", ["А", "Б"])
    assert parsed(
        '{"title": "Разговор", "summary": [{"topic": "Планы", "point": "Встреча в 11"}]}'
    ) == ("Разговор", "", ["Планы: Встреча в 11"])
    broken = (
        '{"title": "Сессия", "about": "Разговор о границах.", "summary": ["границы, '
        + "границы, " * 500
    )
    assert parsed(broken) == ("Сессия", "Разговор о границах.", [])  # looped
    cut = (
        '{\n"title": "Созвон о сне",\n"about": "Про сон и работу.",\n"summary": [\n'
        '"Обсудили сон.",\n{"topic": "Работа", "point": "Уволился по соглашению."},\n'
        '"Собеседница считает'
    )
    assert parsed(cut) == (
        "Созвон о сне",
        "Про сон и работу.",
        ["Обсудили сон.", "Работа: Уволился по соглашению."],
    )  # hit the answer cap: the whole points before the cut
    assert parsed('{"title": "Т", "summary": {"темы": ["А"], "кто_что_берёт": ["mic: Б"]}}')[2] == [
        "Темы: А",
        "Кто что берёт: mic: Б",
    ]  # a dict of sections (Gemma)
    assert parsed("Созвон\n- пункт один\n- пункт два") == (
        "Созвон", "", ["пункт один", "пункт два"],
    )  # fmt: skip


# ── English interface ──

_CYRILLIC = re.compile("[а-яА-ЯёЁ]")


def test_refusal_reasons_and_model_line_read_in_english(monkeypatch) -> None:
    """Refusal reasons, model check refusals and the current model line, in English; a Russian
    "no model" error (an older server, a Russian job) still reads as "no model configured"."""
    from mnema.i18n import using

    failures = [
        _err("Invalid API Key", 401),
        _err("Insufficient Balance", 402),
        _err("Too Many Requests", 429),
        _err("Request timed out", name="Timeout"),
        _err("boom", 500),
        _err("teapot", 418),
        MediaError("Не настроена модель"),
        MediaError("No model is configured on the server"),
        MediaError("No model key set"),
    ]
    _env_file(monkeypatch)
    with using("en"):
        reasons = [errors.human_reason(e) for e in failures]
        refusals = [errors.check_refusal(e).text for e in failures[:2]]
        empty = errors.check_refusal(None).text
        none = current_line()
        _models(monkeypatch)
        line = current_line()
    assert reasons == [
        "the model key was rejected",
        "the account is out of money",
        "request limit exceeded",
        "the model service did not answer in time",
        "the model service is not responding",
        "the model service answered with error 418",
        "no model configured",
        "no model configured",
        "no model key set",
    ]
    assert refusals == [
        "The model key was rejected: enter another key",
        "The account is out of money: enter another key",
    ]
    assert empty == "The model returned an empty answer: choose another model"
    assert (none, line) == ("Model not set up", "Current: gemini/gemini-flash-latest, key …a1B2")
    assert not [t for t in (*reasons, *refusals, empty, none, line) if _CYRILLIC.search(t)]


def test_summary_in_parts_reports_progress_in_english(monkeypatch) -> None:
    """Every progress line of a summary in parts and of a retry pause is English."""
    from mnema.i18n import using

    _models(monkeypatch)
    monkeypatch.setattr(limits, "model_limits", lambda creds: limits.Limits(
        input_tokens=30_720, output_tokens=8_192))  # fmt: skip
    tries: list[int] = []

    def answer(kw):
        tries.append(1)
        if len(tries) == 1:
            return _err("Service overloaded", 503)
        if "КУСОК" in kw["messages"][0]["content"]:
            return "- обсудили часть"
        return _SUMMARY_JSON

    _litellm(monkeypatch, answer)
    seen: list[str] = []
    text = "\n".join(["mic: " + "слово " * 40] * 1000)
    with using("en"):
        summary = claude.title_summary_ex(text, on=lambda f, m: seen.append(m))
        with pytest.raises(MediaError) as no_model:
            _env_file(monkeypatch)
            claude.translate("mic: hello")
    assert summary.title == "Дейли"
    assert "The model is busy, retrying in 5 s…" in seen
    assert "Summarizing part 1 of 3…" in seen and "Writing the summary and title…" in seen
    assert str(no_model.value) == "No model configured"
    assert not [m for m in seen if _CYRILLIC.search(m)]
