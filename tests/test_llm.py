"""LLM layer unit tests (no API calls)."""

from __future__ import annotations

import re

import httpx
import pytest

from mnema.llm import claude, limits, providers
from mnema.llm.config import Credentials


@pytest.fixture(autouse=True)
def _no_provider_network(monkeypatch):
    """Provider model descriptions (limits) answer 404, so the litellm table is used; own cache."""
    monkeypatch.setattr(
        providers, "TRANSPORT", httpx.MockTransport(lambda r: httpx.Response(404, json={}))
    )
    monkeypatch.setattr(limits, "_FETCHED", {})


def _c(model: str) -> Credentials:
    """Model and key from the environment (as for the server and the CLI)."""
    return Credentials(model=model, api_key="sk-test")


def _answers(monkeypatch, answer) -> list[dict]:
    """The model at the external boundary (streaming litellm.completion): answer(kwargs) → the
    answer text or an exception. Returns the calls (kwargs), retries included."""
    import litellm

    calls: list[dict] = []

    def completion(**kw):
        calls.append(kw)
        return iter([_fake_litellm_chunk(answer(kw))])

    monkeypatch.setattr(litellm, "completion", completion)
    monkeypatch.setattr(claude.time, "sleep", lambda s: None)  # retries without waiting
    return calls


def _sys(kw: dict) -> str:
    return kw["messages"][0]["content"]


def _user(kw: dict) -> str:
    return kw["messages"][1]["content"]


def _no_api(monkeypatch) -> None:
    """Any model call fails the test (checks that the API is never reached)."""

    def boom(*a, **k):
        raise AssertionError("_stream_text не должен вызываться")

    monkeypatch.setattr(claude, "_stream_text", boom)


def test_detect_lang_counts_yo_as_cyrillic() -> None:
    """The detector counts "ё" as Cyrillic."""
    assert claude._detect_lang("ёжик идёт по лесу") == "ru"


def test_env_credentials_need_key(monkeypatch) -> None:
    """Environment (server, CLI): MNEMA_MODEL + MNEMA_API_KEY (+ MNEMA_API_BASE); without a key a
    clear error rather than litellm picking up a provider key; without MNEMA_MODEL no model."""
    from mnema.core.base import MediaError
    from mnema.llm.config import LLMSettings

    monkeypatch.setenv("MNEMA_API_BASE", "https://host/v1")
    creds = LLMSettings().credentials()
    assert (creds.model, creds.api_key, creds.api_base) == (
        "anthropic/claude-sonnet-5", "sk-test", "https://host/v1",
    )  # fmt: skip
    monkeypatch.delenv("MNEMA_API_KEY")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    assert not LLMSettings().cloud  # a model without a key: steps are skipped where possible
    with pytest.raises(MediaError, match="^Не задан ключ модели$"):
        LLMSettings().credentials()
    monkeypatch.delenv("MNEMA_MODEL")
    settings = LLMSettings()
    assert (settings.model, settings.source, settings.cloud) == (None, "unconfigured", False)


def test_summary_names_tracks_me_and_others(monkeypatch) -> None:
    """The model sees mic/system as Я/Собеседники (Me/Others for English), in parts too, and the
    JSON answer becomes the title, the gist and the points."""

    def fake(kw: dict) -> str:
        return '{"title": "Созвон", "summary": ["Пункт."]}' if "JSON" in _sys(kw) else "- пункт"

    calls = _answers(monkeypatch, fake)
    lines = ["mic: " + "слово " * 40, "system: " + "ответ " * 40] * 1000
    claude.title_summary_ex("\n".join(lines))  # in parts
    parts = [_user(kw) for kw in calls[:-1]]
    assert len(parts) > 1
    assert all("Я: слово" in p and "Собеседники: ответ" in p for p in parts)
    assert not any(re.search(r"^(mic|system):", p, flags=re.M) for p in parts)

    calls = _answers(
        monkeypatch,
        lambda kw: (
            '{"other": "мама", "title": "Звонок маме", "about": "Договорились.", '
            '"summary": ["Мама ждёт в пятницу."]}'
        ),
    )
    result = claude.title_summary_ex("mic: Мама, приеду в пятницу.\nsystem: Хорошо, буду ждать.")
    assert len(calls) == 1
    assert (result.title, result.about, result.points) == (
        "Звонок маме", "Договорились.", ["Мама ждёт в пятницу."],
    )  # fmt: skip

    calls.clear()
    claude.title_summary_ex("mic: We agreed on Friday.\nsystem: Fine, I take the release.")
    assert _user(calls[0]) == "Me: We agreed on Friday.\nOthers: Fine, I take the release."


def test_title_long_recording_includes_middle_and_end_in_one_request(monkeypatch) -> None:
    calls = _answers(monkeypatch, lambda kw: "Дейли про релиз")
    text = "Приветствие " + "а" * 5000 + "ОБСУЖДЕНИЕ РЕЛИЗА" + "б" * 5000 + "РЕШЕНИЕ В КОНЦЕ"
    assert claude.title_ex(text)[0] == "Дейли про релиз"
    assert len(calls) == 1
    sent = _user(calls[0])
    assert len(sent) <= 4000
    assert all(marker in sent for marker in ("Приветствие", "ОБСУЖДЕНИЕ РЕЛИЗА", "РЕШЕНИЕ В КОНЦЕ"))
    assert "отрывки начала, середины и конца" in _sys(calls[0])


@pytest.mark.parametrize(
    "label", ["mic: ", "Собеседники: ", "Others: ", "[0:12] mic: ", "[1:02:03] Я: ", ""]
)
def test_split_summary_long_line_keeps_words_and_source(label: str) -> None:
    words = [f"слово{i}" for i in range(100)]
    parts = claude._split_text(label + " ".join(words), 80)
    assert all(len(part) <= 80 and part.startswith(label) for part in parts)
    assert " ".join(part[len(label) :] for part in parts).split() == words


def test_summary_bullet_time_is_not_a_speaker_label() -> None:
    text = "- [0:12] " + "слово " * 200
    parts = claude._split_text(text, 120)
    assert " ".join(parts).split() == text.split()
    assert all("[0:12]" not in part for part in parts[1:])


def test_speaker_names_preserve_paragraph_times() -> None:
    named, rule = claude._speakers("[0:12] mic: Привет\n\n[0:20] system: Да")
    assert named == "[0:12] Я: Привет\n\n[0:20] Собеседники: Да"
    assert "Метка «[м:сс]» в начале абзаца — время от начала записи." in rule


@pytest.mark.parametrize("timed", [False, True])
def test_summary_requests_and_validates_times_only_for_stamped_transcript(
    monkeypatch, timed
) -> None:
    calls = _answers(
        monkeypatch,
        lambda kw: (
            '{"title": "[0:00] Созвон", "about": "[0:12] Решили.", '
            '"summary": ["[0:20] А", "[9:99] Б", "[0:21] В"]}'
        ),
    )
    text = "[0:12] mic: Привет\n\n[0:20] system: Да" if timed else "mic: Привет\n\nsystem: Да"
    result = claude.title_summary_ex(text)
    assert result.title == "Созвон"
    assert result.about == "Решили."
    assert result.points == (["[0:20] А", "Б", "В"] if timed else ["А", "Б", "В"])
    assert ("Каждый пункт начинается с времени абзаца" in _sys(calls[0])) == timed


def test_long_summary_keeps_times_from_original_transcript(monkeypatch) -> None:
    def answer(kw):
        if "JSON" in _sys(kw):
            return '{"title": "Созвон", "summary": ["[4:05] В конце", "[4:06] За пределами"]}'
        return "- [0:12] Первый пункт"

    calls = _answers(monkeypatch, answer)
    text = ("[0:12] mic: " + "слово " * 40 + "\n\n") * 1600 + "[4:05] system: Конец"
    result = claude.title_summary_ex(text)
    assert len(calls) > 2
    assert all("- [м:сс]" in _sys(call) for call in calls[:-1])
    assert "Каждый пункт начинается с времени абзаца" in _sys(calls[-1])
    assert result.points == ["[4:05] В конце", "За пределами"]


def test_split_summary_long_word_and_multiple_lines_fit_exact_budget() -> None:
    parts = claude._split_text("mic: " + "а" * 230 + "\nsystem: готово", 40)
    assert all(len(part) <= 40 for part in parts)
    lines = "\n".join(parts).splitlines()
    assert "".join(line.removeprefix("mic: ") for line in lines[:-1]) == "а" * 230
    assert lines[-1] == "system: готово"
    assert claude._split_text("1234\n56789", 10) == ["1234\n56789"]


def _fake_litellm_chunk(text: str | None):
    """A fake litellm/OpenAI stream chunk: chunk.choices[0].delta.content."""
    import types

    delta = types.SimpleNamespace(content=text)
    choice = types.SimpleNamespace(delta=delta)
    return types.SimpleNamespace(choices=[choice])


def test_stream_text_feeds_on_text_with_growing_full_text(monkeypatch) -> None:
    """_stream_text gives on_text the growing FULL text on every delta (not an 80-character status
    tail); live translation streaming to the view relies on it. litellm is mocked."""
    import litellm

    seen_kwargs: dict = {}

    def fake_completion(**kw):
        seen_kwargs.update(kw)
        # an empty chunk (choices=[] and delta=None) must not break the stream
        return iter(
            [_fake_litellm_chunk("Один"), _fake_litellm_chunk(" два"), _fake_litellm_chunk(".")]
        )

    monkeypatch.setattr(litellm, "completion", fake_completion)
    got: list[str] = []
    out = claude._stream_text("s", "c", creds=_c("anthropic/claude-sonnet-5"), on_text=got.append)
    assert got == ["Один", "Один два", "Один два."]
    assert out == "Один два."
    # system and user go as plain messages (OpenAI format), streaming on
    assert seen_kwargs["messages"][0] == {"role": "system", "content": "s"}
    assert seen_kwargs["messages"][1] == {"role": "user", "content": "c"}
    assert seen_kwargs["stream"] is True
    assert seen_kwargs["model"] == "anthropic/claude-sonnet-5"
    assert seen_kwargs["timeout"] == 90.0  # the LLMSettings timeout reaches litellm


class _Transient(Exception):
    status_code = 503  # overload: retry


class _Limit(Exception):
    status_code = 429


def _flaky(monkeypatch, errors: list[Exception]) -> tuple[list[int], list[float]]:
    """litellm at the boundary: the first calls raise the errors in order, then it answers.
    Returns (calls, pauses)."""
    import litellm

    calls: list[int] = []
    slept: list[float] = []

    def completion(**kw):
        calls.append(1)
        if len(calls) <= len(errors):
            raise errors[len(calls) - 1]
        return iter([_fake_litellm_chunk("готово")])

    monkeypatch.setattr(litellm, "completion", completion)
    monkeypatch.setattr(claude.time, "sleep", slept.append)
    return calls, slept


def test_stream_text_retries_overload_with_backoff(monkeypatch) -> None:
    """Overload and dropped connections: up to three retries after 5, 15, 45 s; the fourth
    failure propagates."""
    calls, slept = _flaky(monkeypatch, [_Transient("high demand"), TimeoutError("timed out")])
    assert claude._stream_text("s", "c", creds=_c("anthropic/claude-sonnet-5")) == "готово"
    assert (len(calls), sum(slept)) == (3, 20)

    calls, slept = _flaky(monkeypatch, [_Transient("overloaded")] * 4)
    with pytest.raises(_Transient):
        claude._stream_text("s", "c", creds=_c("anthropic/claude-sonnet-5"))
    assert (len(calls), sum(slept)) == (4, 65)


def test_stream_text_waits_as_long_as_server_asks(monkeypatch) -> None:
    """A rate limit with a server hint of up to a minute waits as asked (Gemini retryDelay); a
    longer wait or a daily limit propagates at once."""
    short = _Limit('{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "16s"}')
    calls, slept = _flaky(monkeypatch, [short])
    assert claude._stream_text("s", "c", creds=_c("gemini/gemini-flash-latest")) == "готово"
    assert (len(calls), sum(slept)) == (2, 16)

    long = _Limit('"retryDelay": "3600s"')
    daily = _Limit(
        '"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier", "retryDelay": "16s"'
    )
    for err in (long, daily):
        calls, slept = _flaky(monkeypatch, [err])
        with pytest.raises(_Limit):
            claude._stream_text("s", "c", creds=_c("gemini/gemini-flash-latest"))
        assert (len(calls), slept) == (1, [])


class _Refused(Exception):
    status_code = 400


@pytest.mark.parametrize(
    ("model", "error"),
    [
        ("anthropic/claude-sonnet-5", ValueError("bad request")),
        ("gemini/gemini-flash-latest", _Refused("Thinking level LOW is not supported")),
        ("openrouter/qwen/qwen3-32b", _Refused("Request contains an invalid argument")),
    ],
)
def test_stream_text_no_retry_on_permanent(monkeypatch, model, error) -> None:
    """A permanent error (no key, 400, including a reasoning refusal) is not retried and does not
    change parameters: it propagates after one call."""
    calls, slept = _flaky(monkeypatch, [error])
    with pytest.raises(type(error)):
        claude._stream_text("s", "c", creds=_c(model))
    assert (len(calls), slept) == (1, [])


def test_stream_text_retry_after_partial_only_without_live_text(monkeypatch) -> None:
    """A connection dropped mid-answer is retried from the start, but not for a live stream
    (on_text), whose text would repeat."""
    import litellm

    calls: list[int] = []

    def midfail(**kw):
        calls.append(1)

        def gen():
            yield _fake_litellm_chunk("часть")
            if len(calls) == 1:
                raise _Transient("503")
            yield _fake_litellm_chunk(" и конец")

        return gen()

    monkeypatch.setattr(litellm, "completion", midfail)
    monkeypatch.setattr(claude.time, "sleep", lambda s: None)
    assert claude._stream_text("s", "c", creds=_c("anthropic/claude-sonnet-5")) == "часть и конец"
    calls.clear()
    with pytest.raises(_Transient):
        claude._stream_text("s", "c", creds=_c("anthropic/claude-sonnet-5"), on_text=lambda t: None)
    assert len(calls) == 1


def test_translate_empty_returns_empty_without_api(monkeypatch) -> None:
    """An empty or blank transcript never calls the API. A recording without speech gets no paid
    request and no made-up folder name."""
    _no_api(monkeypatch)
    assert claude.translate("") == ""
    assert claude.translate("   \n  ") == ""
    assert claude.title_ex("   ")[0] == ""


@pytest.mark.parametrize(
    ("answer", "text", "title"),
    [
        # the title line is not part of the translation
        ("НАЗВАНИЕ: Дейли про релиз\n\nПеревод текста.", "Перевод текста.", "Дейли про релиз"),
        # no НАЗВАНИЕ line: the whole answer is the translation, title_ex makes the title
        ("Просто перевод.", "Просто перевод.", None),
    ],
)
def test_translate_titled_parses_first_line(monkeypatch, answer, text, title) -> None:
    """translate_titled: the title is the first line of the same answer and is cut off."""
    monkeypatch.setattr(claude, "_stream_text", lambda s, c, **k: answer)
    assert claude.translate_titled("hello there") == (text, title)


def _record_calls(monkeypatch) -> list[dict]:
    """Mocks _stream_text: records (system, content) and answers "ПЕРЕВОДn" by call number."""
    calls: list[dict] = []

    def fake(system, content, **k):
        calls.append({"system": system, "content": content})
        return f"ПЕРЕВОД{len(calls)}"

    monkeypatch.setattr(claude, "_stream_text", fake)
    return calls


def _three_paragraphs() -> tuple[str, str, str, str]:
    """Three paragraphs, each under the chunk threshold but any two together over it: one paragraph
    per chunk (no sentence splitting inside a paragraph)."""
    para_len = int(claude._TRANSLATE_CHUNK_CHARS * 0.6)
    p1, p2, p3 = "П" * para_len, "О" * para_len, "Т" * para_len
    return f"{p1}\n\n{p2}\n\n{p3}", p1, p2, p3


@pytest.mark.parametrize("long", [False, True])
def test_translate_long_text_chunks_and_joins_in_order(monkeypatch, long) -> None:
    """A long transcript (paragraphs over the threshold) takes several calls and the joined result
    has every part in order; the translated tail goes into the system prompt from the second
    chunk on. A short one (one chunk) takes one call without chunking overhead."""
    calls = _record_calls(monkeypatch)
    transcript, *chunks = _three_paragraphs() if long else ("hello there", "hello there")

    result = claude.translate(transcript)

    assert [c["content"] for c in calls] == chunks
    assert result == "\n\n".join(f"ПЕРЕВОД{i}" for i in range(1, len(chunks) + 1))
    # the first chunk has no continuation context
    assert "предыдущий" not in calls[0]["system"]
    if long:  # the second and third chunks get the tail of the translation, not of the source
        assert "ПЕРЕВОД1" in calls[1]["system"]
        assert "ПЕРЕВОД2" in calls[2]["system"]


def test_translate_long_text_reports_per_chunk_progress(monkeypatch) -> None:
    """A multi-chunk translation moves the progress chunk by chunk. _stream_text sends messages only
    (fraction=None), so without it the bar would stand still for the whole translation."""
    _record_calls(monkeypatch)
    transcript, *_ = _three_paragraphs()  # exactly 3 chunks
    fractions: list[float] = []

    def on(frac, msg):
        if frac is not None:
            fractions.append(frac)

    claude.translate(transcript, on=on)

    assert fractions == [pytest.approx(1 / 3), pytest.approx(2 / 3), pytest.approx(1.0)]


def test_translate_titled_long_text_title_from_first_chunk(monkeypatch) -> None:
    """translate_titled on a long text: the title from the first chunk, the translation from all."""
    calls: list[dict] = []

    def fake(system, content, **k):
        calls.append({"system": system, "content": content})
        if len(calls) == 1:
            return f"{claude._TITLE_PREFIX} Длинное имя\n\nПЕРЕВОД1"
        return f"ПЕРЕВОД{len(calls)}"

    monkeypatch.setattr(claude, "_stream_text", fake)
    transcript, *_ = _three_paragraphs()

    text, title = claude.translate_titled(transcript)

    assert title == "Длинное имя"
    assert text == "ПЕРЕВОД1\n\nПЕРЕВОД2\n\nПЕРЕВОД3"
    assert len(calls) == 3
    # the title request is only in the first chunk's system prompt
    assert "первой строкой" not in calls[1]["system"]
    assert "первой строкой" not in calls[2]["system"]


def test_translate_long_paragraph_without_breaks_splits_by_sentences(monkeypatch) -> None:
    """A paragraph without line breaks longer than the threshold is cut at sentence boundaries; no
    sentence is lost or repeated."""
    sentences = [f"Предложение{i}." for i in range(600)]
    paragraph = " ".join(sentences)
    assert len(paragraph) > claude._TRANSLATE_CHUNK_CHARS  # really over the threshold

    calls: list[str] = []

    def echo(system, content, **k):
        calls.append(content)
        return content  # an identity "translation" makes it easy to check nothing is lost

    monkeypatch.setattr(claude, "_stream_text", echo)

    result = claude.translate(paragraph)

    assert len(calls) > 1  # the paragraph really is cut into several chunks
    for s in sentences:
        assert result.count(s) == 1  # every sentence appears exactly once


@pytest.mark.parametrize(
    ("answers", "limits", "result"),
    [
        # the second, complete answer is returned whole and the cut draft is dropped
        (
            [("обрыв на полусло", "length"), ("полный ответ целиком", "stop")],
            [100, 200],
            "полный ответ целиком",
        ),
        # cut every time: after two retries the last text, not an error
        ([("часть текста", "length")], [100, 200, 400], "часть текста"),
    ],
)
def test_stream_text_retries_on_length_truncation(monkeypatch, answers, limits, result):
    """finish_reason=length → a retry with doubled max_tokens (Gemini Pro thinking is dynamic and
    cannot be turned off, so a fixed limit can be eaten by reasoning)."""
    import types

    from mnema.llm import claude

    calls = []

    class FakeDelta:
        def __init__(self, content):
            self.content = content

    class FakeChoice:
        def __init__(self, content, reason):
            self.delta = FakeDelta(content)
            self.finish_reason = reason

    class FakeChunk:
        def __init__(self, content, reason=None):
            self.choices = [FakeChoice(content, reason)]

    def fake_completion(**kw):
        calls.append(kw["max_tokens"])
        return iter([FakeChunk(*answers[min(len(calls), len(answers)) - 1])])

    fake_litellm = types.SimpleNamespace(completion=fake_completion, drop_params=True)
    monkeypatch.setitem(__import__("sys").modules, "litellm", fake_litellm)
    monkeypatch.setenv("MNEMA_API_KEY", "k")

    out = claude._stream_text(
        "система", "вход", creds=_c("gemini/gemini-pro-latest"), max_tokens=100
    )

    assert out == result
    assert calls == limits  # retried with a doubled limit


@pytest.mark.parametrize(
    "model",
    [
        "gemini/gemini-flash-latest",
        "gemini/gemini-flash-lite-latest",
        "gemini/gemini-3-flash-preview",
        "gemini/gemini-2.5-flash",
        "gemini/gemini-2.5-flash-lite",
    ],
)
def test_gemini_flash_sends_low_reasoning_in_one_request(monkeypatch, model):
    """Both supported SDK versions turn low into a Gemini budget or level."""
    import json

    import litellm
    from litellm import main as litellm_main

    bodies: list[dict] = []

    def send(self, request, *a, **k):
        bodies.append(json.loads(request.content))
        chunk = {
            "candidates": [
                {
                    "content": {"parts": [{"text": "готово"}], "role": "model"},
                    "finishReason": "STOP",
                    "index": 0,
                }
            ]
        }
        return httpx.Response(
            200,
            request=request,
            headers={"Content-Type": "text/event-stream"},
            text=f"data: {json.dumps(chunk)}\n\n",
        )

    monkeypatch.setattr(litellm, "completion", litellm_main.completion)
    monkeypatch.setattr(httpx.Client, "send", send)
    assert claude._stream_text("s", "c", creds=_c(model)) == "готово"
    assert len(bodies) == 1
    thinking = bodies[0]["generationConfig"]["thinkingConfig"]
    assert thinking in (
        {"thinkingBudget": 1024, "includeThoughts": True},
        {"thinkingLevel": "low", "includeThoughts": True},
    )


@pytest.mark.parametrize(
    "model,expected",
    [
        ("anthropic/claude-sonnet-4-5", {"thinking": {"type": "enabled", "budget_tokens": 1024}}),
        ("openai/gpt-5", {"reasoning_effort": "low"}),
        ("openrouter/openai/gpt-5", {"reasoning_effort": "low"}),
        ("openrouter/qwen/qwen3-32b", ({}, {"reasoning_effort": "low"})),
        ("openai/gpt-4o", {}),
    ],
)
def test_sdk_maps_or_drops_low_and_provider_rejection_is_one_request(monkeypatch, model, expected):
    """Real SDK request bodies: supported providers and a model without reasoning; HTTP 400 is
    not retried."""
    import json

    import litellm
    from litellm import main as litellm_main

    bodies = []

    def send(self, request, *a, **k):
        bodies.append(json.loads(request.content))
        return httpx.Response(
            400,
            request=request,
            json={"error": {"type": "invalid_request_error", "message": "reasoning unsupported"}},
        )

    monkeypatch.setattr(litellm, "completion", litellm_main.completion)
    monkeypatch.setattr(httpx.Client, "send", send)
    with pytest.raises(litellm.BadRequestError):
        claude._stream_text("s", "c", creds=_c(model))
    assert len(bodies) == 1
    sent = {
        key: value
        for key, value in bodies[0].items()
        if key in {"reasoning_effort", "thinking", "reasoning", "output_config"}
    }
    # SDK 1.90 does not know reasoning for Qwen3 on OpenRouter; 1.106 passes low.
    assert sent in (expected if isinstance(expected, tuple) else (expected,))


def test_gemini_requests_turn_safety_filters_off(monkeypatch) -> None:
    """Gemini gets BLOCK_NONE for the four filter categories (personal calls were cut by
    content_filter); other providers do not get the parameter."""
    calls = _answers(monkeypatch, lambda kw: "Имя")
    monkeypatch.setenv("MNEMA_MODEL", "gemini/gemini-flash-latest")
    claude.title_ex("mic: текст")
    safety = calls[-1]["safety_settings"]
    assert {x["category"] for x in safety} == {
        "HARM_CATEGORY_HARASSMENT",
        "HARM_CATEGORY_HATE_SPEECH",
        "HARM_CATEGORY_SEXUALLY_EXPLICIT",
        "HARM_CATEGORY_DANGEROUS_CONTENT",
    }
    assert {x["threshold"] for x in safety} == {"BLOCK_NONE"}
    monkeypatch.setenv("MNEMA_MODEL", "anthropic/claude-sonnet-5")
    claude.title_ex("mic: текст")
    assert "safety_settings" not in calls[-1]


@pytest.mark.parametrize("task", ["title", "live"])
def test_short_llm_tasks_leave_room_for_sdk_reasoning(monkeypatch, task):
    """A short answer never sets max_tokens below the SDK's thinking budget."""
    import json

    import litellm
    from litellm import main as litellm_main

    from mnema.live.translate import LiveTranslator

    bodies = []

    def send(self, request, *a, **k):
        bodies.append(json.loads(request.content))
        return httpx.Response(
            400,
            request=request,
            json={"error": {"type": "invalid_request_error", "message": "test rejection"}},
        )

    monkeypatch.setenv("MNEMA_MODEL", "anthropic/claude-sonnet-4-5")
    monkeypatch.setattr(litellm, "completion", litellm_main.completion)
    monkeypatch.setattr(httpx.Client, "send", send)
    if task == "title":
        assert claude.title_ex("mic: краткое обсуждение")[0] == ""
    else:
        with pytest.raises(litellm.BadRequestError):
            LiveTranslator("en").push("Привет")
    assert len(bodies) == 1
    body = bodies[0]
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 1024}
    assert body["max_tokens"] == 2048 > body["thinking"]["budget_tokens"], body
