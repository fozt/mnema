"""Cloud model providers (mnema/llm/providers.py): model list and key check, recommended model,
model description, refusal kind, limit reset time. The boundary is the network:
httpx.MockTransport via providers.TRANSPORT; any request other than a GET to a provider address
fails the test."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from mnema.llm import errors, providers
from mnema.llm.errors import ErrorKind
from mnema.llm.providers import Provider


def test_litellm_names_roundtrip_and_foreign_prefix_is_unknown() -> None:
    for p in Provider:
        name = providers.litellm_model(p, "m-1")
        assert providers.provider_of(name) == (p, "m-1")
    assert (
        providers.litellm_model(Provider.CLAUDE, "claude-sonnet-5") == "anthropic/claude-sonnet-5"
    )
    free = "qwen/qwen3.8-27b:free"
    assert providers.provider_of(f"openrouter/{free}") == (Provider.OPENROUTER, free)
    assert providers.provider_of("together_ai/x") is None
    assert providers.provider_of("gemini") is None
    assert providers.litellm_base(Provider.QWEN) == providers.QWEN_BASE
    assert providers.litellm_base(Provider.GEMINI) is None


def test_key_looks_like_by_prefix() -> None:
    assert providers.key_looks_like("AIza" + "x" * 35) == Provider.GEMINI
    assert providers.key_looks_like("sk-ant-api03-x") == Provider.CLAUDE
    assert providers.key_looks_like("sk-proj-x") == Provider.OPENAI
    assert providers.key_looks_like("sk-or-v1-" + "0" * 64) == Provider.OPENROUTER
    assert providers.key_looks_like("gsk_" + "x" * 52) == Provider.GROQ
    assert providers.key_looks_like("sk-test") is None


# ── network at the boundary ──


def _serve(monkeypatch, routes: dict[str, tuple[int, object]]) -> list[httpx.Request]:
    """The provider at the boundary: path (with query) → (code, JSON). GET only, key in a header."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET", f"не GET: {request.method} {request.url}"
        assert "key=" not in str(request.url), "ключ ушёл в адрес"
        seen.append(request)
        path = request.url.path + (f"?{request.url.query.decode()}" if request.url.query else "")
        code, body = routes.get(path, (404, {"error": {"message": f"нет {path}"}}))
        return httpx.Response(code, json=body)

    monkeypatch.setattr(providers, "TRANSPORT", httpx.MockTransport(handler))
    return seen


_GEMINI_LIST = {
    "models": [
        {
            "name": "models/gemini-2.5-flash",
            "supportedGenerationMethods": ["generateContent", "countTokens"],
            "inputTokenLimit": 1048576,
            "outputTokenLimit": 65536,
        },
        {
            "name": "models/gemini-2.5-flash-preview-09-2025",
            "supportedGenerationMethods": ["generateContent"],
        },
        {"name": "models/gemini-flash-latest", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},
        {"name": "models/gemini-2.0-flash-001", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-2.0-flash", "supportedGenerationMethods": ["generateContent"]},
    ]
}


def test_gemini_list_keeps_chat_models_latest_first_and_hides_dated_twins(monkeypatch) -> None:
    """Gemini: generateContent only, without the models/ prefix; a dated snapshot is hidden when
    the undated name exists; -latest first; the key in the x-goog-api-key header."""
    seen = _serve(monkeypatch, {"/v1beta/models?pageSize=1000": (200, _GEMINI_LIST)})
    models = providers.list_models(Provider.GEMINI, "AIza-key")
    assert [m.id for m in models] == [
        "gemini-flash-latest", "gemini-2.5-flash", "gemini-2.0-flash",
        "gemini-2.5-flash-preview-09-2025",
    ]  # fmt: skip
    assert (models[1].input_tokens, models[1].output_tokens) == (1048576, 65536)
    assert seen[0].headers["x-goog-api-key"] == "AIza-key"


def test_claude_lone_snapshot_shows_as_alias_and_openai_newest_first(monkeypatch) -> None:
    """Claude: a lone "name-YYYYMMDD" snapshot shows under the undated name, with limits; OpenAI:
    newest first, non-chat models known to litellm (embeddings, images) hidden."""
    claude = {
        "data": [
            {"id": "claude-sonnet-5", "max_input_tokens": 1000000, "max_tokens": 128000},
            {"id": "claude-haiku-4-5-20251001", "max_input_tokens": 200000, "max_tokens": 64000},
        ]
    }
    openai = {
        "data": [
            {"id": "gpt-4o", "created": 100},
            {"id": "text-embedding-3-small", "created": 300},
            {"id": "gpt-5", "created": 200},
            {"id": "my-custom-model", "created": 50},
        ]
    }
    seen = _serve(
        monkeypatch,
        {"/v1/models?limit=1000": (200, claude), "/v1/models": (200, openai)},
    )
    got = providers.list_models(Provider.CLAUDE, "sk-ant-x")
    assert [(m.id, m.output_tokens) for m in got] == [
        ("claude-sonnet-5", 128000), ("claude-haiku-4-5", 64000),
    ]  # fmt: skip
    assert seen[0].headers["x-api-key"] == "sk-ant-x"
    assert seen[0].headers["anthropic-version"] == "2023-06-01"
    names = [m.id for m in providers.list_models(Provider.OPENAI, "sk-proj-x")]
    assert names == ["gpt-5", "gpt-4o", "my-custom-model"]
    assert seen[-1].headers["authorization"] == "Bearer sk-proj-x"


def test_gemini_model_description_gives_limits(monkeypatch) -> None:
    info = {"name": "models/gemini-flash-latest", "inputTokenLimit": 1048576,
            "outputTokenLimit": 65535}  # fmt: skip
    seen = _serve(monkeypatch, {"/v1beta/models/gemini-flash-latest": (200, info)})
    found = providers.model_info(Provider.GEMINI, "AIza-key", "gemini-flash-latest")
    assert found is not None and (found.input_tokens, found.output_tokens) == (1048576, 65535)
    assert seen[0].headers["x-goog-api-key"] == "AIza-key"
    with pytest.raises(providers.ProviderError) as missing:
        providers.model_info(Provider.GEMINI, "AIza-key", "gemini-0")
    assert errors.classify(missing.value) == ErrorKind.NO_MODEL


# Google's real answer to a request from a country where the Gemini API does not work
_GOOGLE_REGION = {
    "error": {
        "code": 400,
        "message": "User location is not supported for the API use.",
        "status": "FAILED_PRECONDITION",
    }
}


def test_request_from_unsupported_country_is_region(monkeypatch) -> None:
    """Gemini refuses requests from this country or network (400 FAILED_PRECONDITION): kind REGION,
    not a generic model service error, for the model list and for generation (the same answer
    wrapped by litellm)."""
    _serve(monkeypatch, {"/v1beta/models?pageSize=1000": (400, _GOOGLE_REGION)})
    with pytest.raises(providers.ProviderError) as listing:
        providers.key_models(Provider.GEMINI, "AIza-key")
    assert errors.classify(listing.value) == ErrorKind.REGION
    assert errors.human_reason(listing.value) == (
        "сервис модели не принимает запросы из вашей страны или сети"
    )
    body = json.dumps(_GOOGLE_REGION, indent=2)
    generation = _err(
        f"litellm.BadRequestError: Vertex_ai_betaException BadRequestError - b'{body}'", 400
    )
    assert errors.classify(generation) == ErrorKind.REGION


def test_qwen_without_list_and_dated_snapshot_hidden(monkeypatch) -> None:
    """Qwen: the international address; no /models (404) means no list and the model is typed in;
    a dated snapshot is hidden when the undated name is listed."""
    seen = _serve(monkeypatch, {})
    assert providers.key_models(Provider.QWEN, "sk-q") == []
    assert seen[0].url.host == "dashscope-intl.aliyuncs.com"
    listed = {"data": [{"id": "qwen-plus"}, {"id": "qwen-plus-2025-07-28"}]}
    _serve(monkeypatch, {"/compatible-mode/v1/models": (200, listed)})
    assert [m.id for m in providers.list_models(Provider.QWEN, "sk-q")] == ["qwen-plus"]


def _router(id_: str, *, price: str = "0.000001", out: list[str] | None = None) -> dict:
    """A model of OpenRouter's public list (shape of GET /api/v1/models)."""
    return {
        "id": id_,
        "context_length": 131072,
        "architecture": {"input_modalities": ["text"], "output_modalities": out or ["text"]},
        "pricing": {"prompt": price, "completion": price},
        "top_provider": {"context_length": 262144, "max_completion_tokens": 235929},
    }


_ROUTER = {
    "data": [
        _router("qwen/qwen3.8-max"),
        _router("google/gemma-4-31b-it:free", price="0"),
        _router("google/gemini-3-pro-image", out=["image", "text"]),
        _router("stealth/space-bunny-alpha", price="0"),
        _router("qwen/qwen3.8-27b:free", price="0"),
        _router("openrouter/auto", price="-1"),
    ]
}


def test_openrouter_list_free_first_and_free_qwen_recommended(monkeypatch) -> None:
    """OpenRouter: a public list, text-output models only; free ones (":free" or zero price)
    first in provider order; limits are the provider's window and answer cap. The recommended
    model is the first free Qwen, otherwise the first listed."""
    seen = _serve(monkeypatch, {"/api/v1/models": (200, _ROUTER)})
    models = providers.list_models(Provider.OPENROUTER, "sk-or-v1-x")
    assert [(m.id, m.free) for m in models] == [
        ("google/gemma-4-31b-it:free", True),
        ("stealth/space-bunny-alpha", True),
        ("qwen/qwen3.8-27b:free", True),
        ("qwen/qwen3.8-max", False),
        ("openrouter/auto", False),
    ]
    assert (models[2].input_tokens, models[2].output_tokens) == (262144, 235929)
    assert seen[0].url.host == "openrouter.ai"

    def pick(*ids: str) -> str | None:
        return providers.recommended(Provider.OPENROUTER, [m for m in models if m.id in ids])

    assert pick(*(m.id for m in models)) == "qwen/qwen3.8-27b:free"
    assert pick("stealth/space-bunny-alpha", "qwen/qwen3.8-max") == "stealth/space-bunny-alpha"
    assert providers.recommended(Provider.OPENROUTER, []) is None


def test_recommended_model_by_provider() -> None:
    """Enter in mnema model: Gemini gemini-flash-latest, DeepSeek deepseek-chat (when listed),
    otherwise the first; Qwen without a list qwen-plus, others without a list nothing."""

    def models(*ids: str) -> list[providers.ModelInfo]:
        return [providers.ModelInfo(id=i) for i in ids]

    gemini = models("gemini-pro-latest", "gemini-flash-latest")
    assert providers.recommended(Provider.GEMINI, gemini) == "gemini-flash-latest"
    assert (
        providers.recommended(Provider.GEMINI, models("gemini-pro-latest")) == "gemini-pro-latest"
    )
    deepseek = models("deepseek-reasoner", "deepseek-chat")
    assert providers.recommended(Provider.DEEPSEEK, deepseek) == "deepseek-chat"
    assert providers.recommended(Provider.OPENAI, models("gpt-5", "gpt-4o")) == "gpt-5"
    assert providers.recommended(Provider.QWEN, []) == "qwen-plus"
    assert providers.recommended(Provider.CLAUDE, []) is None


def test_openrouter_key_is_checked_by_get_key(monkeypatch) -> None:
    """OpenRouter's list is public, so GET /api/v1/key checks the key (no generation or quota):
    a good key gives its models; 401 rejects the key and no list is loaded."""
    routes = {"/api/v1/models": (200, _ROUTER), "/api/v1/key": (200, {"data": {"usage": 0}})}
    seen = _serve(monkeypatch, routes)
    found = providers.key_models(Provider.OPENROUTER, "sk-or-v1-x")
    assert found[0].id == "google/gemma-4-31b-it:free"
    assert [r.url.path for r in seen] == ["/api/v1/key", "/api/v1/models"]
    assert seen[0].headers["authorization"] == "Bearer sk-or-v1-x"

    bad = {"error": {"message": "User not found.", "code": 401}}
    seen = _serve(monkeypatch, {"/api/v1/models": (200, _ROUTER), "/api/v1/key": (401, bad)})
    with pytest.raises(providers.ProviderError) as wrong:
        providers.key_models(Provider.OPENROUTER, "sk-or-v1-x")
    assert errors.classify(wrong.value) == ErrorKind.INVALID_KEY
    assert [r.url.path for r in seen] == ["/api/v1/key"]


# ── refusal kind and reset time ──


def _err(text: str, code: int | None = None, name: str = "ProviderError") -> Exception:
    e = type(name, (Exception,), {})(text)
    if code:
        e.status_code = code  # type: ignore[attr-defined]
    return e


# OpenRouter (docs/api-reference/errors, limits): the free models' daily limit and an
# upstream provider's limit
_FREE_DAY = (
    "Rate limit exceeded: free-models-per-day. Add 10 credits to unlock 1000 free model "
    "requests per day"
)
_UPSTREAM = (
    "qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or add "
    "your own key to accumulate your rate limits"
)
# free models are not allowed by the account's privacy settings
_PRIVACY = (
    'OpenrouterException - {"error":{"message":"No endpoints found matching your data policy '
    '(Free model publication). Configure: https://openrouter.ai/settings/privacy","code":404}}'
)


def test_classify_order_of_provider_errors() -> None:
    """The order of checks matters: "limit: 0" with PerDay means the model is not in the plan;
    insufficient_quota is money; Gemini's "exceeded your current quota … billing" is a limit;
    403 leaked is a disabled key, other 403s no access to the model; 404 no model."""
    free = (
        "You exceeded your current quota, please check your plan and billing details. "
        '"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"'
    )
    cases = [
        (_err(free + ", limit: 0, model: gemini-2.5-pro", 429), ErrorKind.NO_MODEL),
        (_err("insufficient_quota: You exceeded your current quota", 429), ErrorKind.NO_MONEY),
        (_err(free, 429), ErrorKind.DAILY_LIMIT),
        (_err(free.replace("PerDay", "PerMinute"), 429), ErrorKind.RATE_LIMIT),
        (_err("Access denied, please make sure your account is in good standing. Arrearage", 400),
         ErrorKind.NO_MONEY),
        (_err("Your API key was reported as leaked. Please use another API key.", 403),
         ErrorKind.DISABLED),
        (_err("Generative Language API has not been used in project 1 before or it is disabled",
              403), ErrorKind.DISABLED),
        (_err("Permission denied on resource project 1", 403), ErrorKind.NO_MODEL),
        (_err('{"error": {"status": "NOT_FOUND"}}'), ErrorKind.NO_MODEL),
        (_err("model_not_found", 404), ErrorKind.NO_MODEL),
        (_err("InvalidApiKey: Invalid API-key provided.", 401), ErrorKind.INVALID_KEY),
        (_err("Overloaded", 529), ErrorKind.OVERLOADED),
        (_err("connection refused", name="ConnectError"), ErrorKind.NETWORK),
        (_err(_FREE_DAY, 429), ErrorKind.DAILY_LIMIT),
        (_err("Rate limit exceeded: free-models-per-min. ", 429), ErrorKind.RATE_LIMIT),
        (_err(_UPSTREAM, 429), ErrorKind.RATE_LIMIT),
        (_err("Insufficient credits. Add more using https://openrouter.ai/credits", 402),
         ErrorKind.NO_MONEY),
        (_err("OpenrouterException - Insufficient credits"), ErrorKind.NO_MONEY),
        (_err(_PRIVACY, 404), ErrorKind.PRIVACY),
    ]  # fmt: skip
    assert [errors.classify(e) for e, _ in cases] == [k for _, k in cases]
    assert errors.human_reason(cases[5][0]) == "ключ модели отключён"
    assert errors.human_reason(cases[7][0]) == "нет доступа к модели"
    privacy = "модель закрыта настройками приватности OpenRouter"
    assert errors.human_reason(cases[-1][0]) == privacy


def test_reset_time_gemini_daily_at_los_angeles_midnight() -> None:
    berlin = ZoneInfo("Europe/Berlin")
    now = datetime(2026, 10, 2, 14, 5, tzinfo=berlin)
    daily = _err('"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"', 429)
    until = providers.reset_time(Provider.GEMINI, ErrorKind.DAILY_LIMIT, daily, now)
    assert until == datetime(2026, 10, 3, 9, 0, tzinfo=berlin)  # 00:00 in Los Angeles
    other = providers.reset_time(Provider.DEEPSEEK, ErrorKind.DAILY_LIMIT, daily, now)
    assert other == now + timedelta(hours=1)
    hinted = _err('"retryDelay": "16s"', 429)
    rate = providers.reset_time(Provider.GEMINI, ErrorKind.RATE_LIMIT, hinted, now)
    assert rate == now + timedelta(seconds=16)
    assert providers.reset_time(
        Provider.OPENAI, ErrorKind.RATE_LIMIT, _err("slow down", 429), now
    ) == now + timedelta(seconds=60)
    assert providers.reset_time(Provider.OPENAI, ErrorKind.NO_MONEY, daily, now) is None
    free = _err(_FREE_DAY, 429)
    router = providers.reset_time(Provider.OPENROUTER, ErrorKind.DAILY_LIMIT, free, now)
    assert router == datetime(2026, 10, 3, 2, 0, tzinfo=berlin)  # 00:00 UTC


def test_provider_error_text_hides_key() -> None:
    """The provider's answer text has no key (litellm and the providers put it in the URL)."""
    secret = "AIza" + "S" * 35
    e = providers.ProviderError(400, f"bad request https://x/v1?key={secret}")
    assert secret not in str(e) and e.status_code == 400


_CYRILLIC = re.compile("[а-яА-ЯёЁ]")


def test_key_step_and_cloud_recognition_refusals_read_in_english(monkeypatch) -> None:
    """The "Key" step and cloud recognition refusals, in an English interface."""
    from mnema import node_settings
    from mnema.core.base import MediaError
    from mnema.i18n import using

    bad = {"error": {"message": "API key not valid.", "code": 401}}
    _serve(monkeypatch, {"/v1beta/models?pageSize=1000": (401, bad)})
    monkeypatch.delenv("MNEMA_ASR_API_KEY", raising=False)
    with using("en"):
        problems = [
            providers.key_problem(Provider.GEMINI, ""),
            providers.key_problem(Provider.GEMINI, "AIza ключ"),
            providers.key_problem(Provider.GEMINI, "sk-ant-" + "x" * 30),
        ]
        with pytest.raises(MediaError) as refused:
            providers.check_key(Provider.GEMINI, "AIza" + "x" * 35)
        no_key = node_settings.asr_problem("openai/gpt-4o-transcribe")
        unknown = node_settings.asr_problem("deepseek/whisper")
    assert problems == [
        "A key is required",
        "The key has invalid characters. Copy it again",
        "This is a Claude key",
    ]
    assert str(refused.value) == "The model key was rejected"
    assert no_key == "No OpenAI key for recognition. Set it in Mnema settings on the Mac"
    assert unknown == "Unknown recognition method"
    texts = [*problems, str(refused.value), no_key, unknown]
    assert not [t for t in texts if _CYRILLIC.search(t)]
