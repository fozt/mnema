"""Cloud model providers: key check and the key's model list (the model window and the
server's /api/model*), model descriptions with limits, limit reset time, litellm names.

Only GET requests to provider addresses, the key in a header, never in the URL; no generation
requests here. One httpx client, http(): tests substitute TRANSPORT (httpx.MockTransport), and
MnemaClient in the server window uses it too.
"""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta
from enum import StrEnum

import httpx
from pydantic import BaseModel

from ..i18n import _
from .errors import ErrorKind, retry_after


class Provider(StrEnum):
    GEMINI = "gemini"
    DEEPSEEK = "deepseek"
    OPENAI = "openai"
    CLAUDE = "claude"
    QWEN = "qwen"
    OPENROUTER = "openrouter"
    GROQ = "groq"


TITLES: dict[Provider, str] = {
    Provider.GEMINI: "Gemini",
    Provider.DEEPSEEK: "DeepSeek",
    Provider.OPENAI: "OpenAI",
    Provider.CLAUDE: "Claude",
    Provider.QWEN: "Qwen",
    Provider.OPENROUTER: "OpenRouter",
    Provider.GROQ: "Groq",
}
# recommended model (the model window, /api/model/key): if the key lists it, else the first
_RECOMMENDED: dict[Provider, str] = {
    Provider.GEMINI: "gemini-flash-latest",
    Provider.DEEPSEEK: "deepseek-chat",
    Provider.QWEN: "qwen-plus",  # the provider may have no model list
}
_PREFIX: dict[Provider, str] = {
    Provider.GEMINI: "gemini",
    Provider.DEEPSEEK: "deepseek",
    Provider.OPENAI: "openai",
    Provider.CLAUDE: "anthropic",
    Provider.QWEN: "dashscope",
    Provider.OPENROUTER: "openrouter",
    Provider.GROQ: "groq",
}
# Singapore: keys are regional and litellm defaults to Beijing, so the base is always passed
QWEN_BASE = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
# provider key prefixes: one table for telling keys apart and hiding them in logs (redact);
# exact prefixes before the generic one
KEY_PREFIXES: tuple[tuple[str, Provider | None], ...] = (
    ("sk-or-", Provider.OPENROUTER),
    ("gsk_", Provider.GROQ),
    ("sk-ant-", Provider.CLAUDE),
    ("sk-proj-", Provider.OPENAI),
    ("AIza", Provider.GEMINI),
    ("sk-", None),  # DeepSeek and older OpenAI keys: the provider cannot be told
)
# where to get a key: the provider's page without https://
KEY_PAGES: dict[Provider, str] = {
    Provider.GEMINI: "aistudio.google.com/apikey",
    Provider.DEEPSEEK: "platform.deepseek.com/api_keys",
    Provider.OPENAI: "platform.openai.com/api-keys",
    Provider.CLAUDE: "platform.claude.com/settings/keys",
    # Singapore: a key of the same region as QWEN_BASE
    Provider.QWEN: "modelstudio.console.alibabacloud.com/ap-southeast-1/settings/api-key",
    Provider.OPENROUTER: "openrouter.ai/settings/keys",
    Provider.GROQ: "console.groq.com/keys",
}
BAD_KEY = "The key has invalid characters. Copy it again"

TRANSPORT: httpx.BaseTransport | None = None  # tests: httpx.MockTransport


def http(timeout: float = 10.0) -> httpx.Client:
    """Client for provider requests (transport=TRANSPORT)."""
    return httpx.Client(transport=TRANSPORT, timeout=timeout)


class ProviderError(Exception):
    """An error answer from a provider: status_code and str() like litellm exceptions, so
    errors.classify reads both the same way. The text is redacted."""

    def __init__(self, status_code: int | None, message: str) -> None:
        from .redact import redact

        super().__init__(redact(message))
        self.status_code = status_code


class ModelInfo(BaseModel):
    id: str  # name without the litellm prefix: "gemini-flash-latest"
    input_tokens: int | None = None
    output_tokens: int | None = None
    free: bool = False  # OpenRouter: ":free" or zero price


_GEMINI = "https://generativelanguage.googleapis.com/v1beta/models"
_DEEPSEEK = "https://api.deepseek.com"
_OPENAI = "https://api.openai.com/v1/models"
_CLAUDE = "https://api.anthropic.com/v1/models"
_OPENROUTER = "https://openrouter.ai/api/v1"
_GROQ = "https://api.groq.com/openai/v1"
# dated snapshot suffix: -20250929, -2025-08-07, -09-2025, -001
_DATED = re.compile(r"-(\d{8}|\d{4}-\d{2}-\d{2}|\d{2}-\d{4}|\d{3})$")
_CLAUDE_SNAPSHOT = re.compile(r"-\d{8}$")


def _headers(provider: Provider, key: str) -> dict[str, str]:
    if provider == Provider.GEMINI:
        return {"x-goog-api-key": key}
    if provider == Provider.CLAUDE:
        return {"x-api-key": key, "anthropic-version": "2023-06-01"}
    return {"Authorization": f"Bearer {key}"}


def _get(provider: Provider, key: str, url: str, timeout: float) -> dict:
    """GET to the provider; a non-2xx answer raises ProviderError(code, answer text)."""
    with http(timeout) as client:
        r = client.get(url, headers=_headers(provider, key))
    if r.status_code >= 400:
        raise ProviderError(r.status_code, r.text)
    return r.json()


def _int(value: object) -> int | None:
    return int(value) if isinstance(value, int | float | str) and str(value).isdigit() else None


def _gemini_info(item: dict) -> ModelInfo:
    return ModelInfo(
        id=str(item.get("name", "")).removeprefix("models/"),
        input_tokens=_int(item.get("inputTokenLimit")),
        output_tokens=_int(item.get("outputTokenLimit")),
    )


def _claude_info(item: dict) -> ModelInfo:
    return ModelInfo(
        id=str(item.get("id", "")),
        input_tokens=_int(item.get("max_input_tokens")),
        output_tokens=_int(item.get("max_tokens")),
    )


def _price_zero(value: object) -> bool:
    try:
        return float(str(value)) == 0
    except ValueError:
        return False


def _openrouter_info(item: dict) -> ModelInfo:
    top = item.get("top_provider") or {}
    price = item.get("pricing") or {}
    model = str(item["id"])
    return ModelInfo(
        id=model,
        input_tokens=_int(top.get("context_length")) or _int(item.get("context_length")),
        output_tokens=_int(top.get("max_completion_tokens")),
        free=model.endswith(":free")
        or (_price_zero(price.get("prompt")) and _price_zero(price.get("completion"))),
    )


def _raw_models(provider: Provider, key: str, timeout: float) -> list[ModelInfo]:
    """The provider's models as listed (provider order; OpenAI newest first; OpenRouter only
    text-output models, no images or audio)."""
    if provider == Provider.OPENROUTER:
        data = _get(provider, key, f"{_OPENROUTER}/models", timeout)  # the list is public
        return [
            _openrouter_info(m)
            for m in data.get("data", [])
            if m.get("id")
            and (m.get("architecture") or {}).get("output_modalities", ["text"]) == ["text"]
        ]
    if provider == Provider.GEMINI:
        data = _get(provider, key, f"{_GEMINI}?pageSize=1000", timeout)
        return [
            _gemini_info(m)
            for m in data.get("models", [])
            if "generateContent" in (m.get("supportedGenerationMethods") or [])
        ]
    if provider == Provider.CLAUDE:
        data = _get(provider, key, f"{_CLAUDE}?limit=1000", timeout)
        return [_claude_info(m) for m in data.get("data", [])]
    if provider == Provider.QWEN:
        try:
            data = _get(provider, key, f"{QWEN_BASE}/models", timeout)
        except ProviderError as e:
            if e.status_code == 404:  # no list: the model is typed in by hand
                return []
            raise
        return [ModelInfo(id=str(m["id"])) for m in data.get("data", []) if m.get("id")]
    url = {Provider.DEEPSEEK: f"{_DEEPSEEK}/models", Provider.GROQ: f"{_GROQ}/models"}.get(
        provider, _OPENAI
    )
    items = [m for m in _get(provider, key, url, timeout).get("data", []) if m.get("id")]
    if provider == Provider.OPENAI:
        items.sort(key=lambda m: -(m.get("created") or 0))
    return [ModelInfo(id=str(m["id"])) for m in items]


def _not_chat(provider: Provider, model: str) -> bool:
    """litellm knows the model and it is not chat (embeddings, speech, images); unknown is chat."""
    try:
        import litellm

        mode = litellm.get_model_info(litellm_model(provider, model)).get("mode")
    except Exception:  # noqa: BLE001 - unknown to litellm or no litellm (thin client)
        return False
    return mode not in (None, "chat")


def _ordered(provider: Provider, models: list[ModelInfo]) -> list[ModelInfo]:
    """Filter and order: chat only, a dated snapshot hidden when the undated name exists, a lone
    Claude snapshot under its undated name; -latest first, then undated, then dated."""
    ids = {m.id for m in models}
    out: dict[str, ModelInfo] = {}
    for m in models:
        if not m.id or _not_chat(provider, m.id):
            continue
        dated = _DATED.search(m.id)
        if dated and _DATED.sub("", m.id) in ids:
            continue
        if provider == Provider.CLAUDE and _CLAUDE_SNAPSHOT.search(m.id):
            m = m.model_copy(update={"id": _CLAUDE_SNAPSHOT.sub("", m.id)})
        out.setdefault(m.id, m)

    def group(m: ModelInfo) -> int:
        return 0 if m.id.endswith("-latest") else 2 if _DATED.search(m.id) else 1

    return sorted(out.values(), key=group)


def list_models(provider: Provider, key: str, *, timeout: float = 10.0) -> list[ModelInfo]:
    """The key's models, filtered and ordered (chat only, dated snapshots hidden when the
    undated name exists, -latest first; OpenRouter: free first).
    Raises ProviderError | httpx.HTTPError."""
    models = _raw_models(provider, key, timeout)
    if provider == Provider.OPENROUTER:
        return sorted(models, key=lambda m: not m.free)
    return _ordered(provider, models)


def key_models(provider: Provider, key: str, *, timeout: float = 10.0) -> list[ModelInfo]:
    """Check the key and get its models (GET only); OpenRouter's list is public, so GET /key
    checks the key. Raises ProviderError | httpx.HTTPError."""
    if provider == Provider.OPENROUTER:
        _get(provider, key, f"{_OPENROUTER}/key", timeout)
    return list_models(provider, key, timeout=timeout)


def key_problem(provider: Provider, key: str) -> str:
    """Why the key cannot be sent to the provider: empty, invalid characters (it goes into a
    request header), another provider's key; "" if it can."""
    if not key:
        return _("A key is required")
    if not all(" " < c <= "~" for c in key):
        return _(BAD_KEY)
    other = key_looks_like(key)
    if other is not None and other != provider:
        return _("This is a {provider} key", provider=TITLES[other])
    return ""


def check_key(provider: Provider, key: str, *, timeout: float = 10.0) -> list[ModelInfo]:
    """The "Key" step of the model window (here or /api/model/key): the key is hidden in the
    log first, then key_problem, then key_models. A refusal is a MediaError for the user:
    key_problem or the provider's reason, capitalized."""
    from ..core.base import MediaError
    from .errors import human_sentence
    from .redact import add_secret

    if key:
        add_secret(key)
    problem = key_problem(provider, key)
    if problem:
        raise MediaError(problem)
    try:
        return key_models(provider, key, timeout=timeout)
    except (ProviderError, httpx.HTTPError, ValueError) as e:
        raise MediaError(human_sentence(e)) from e


def recommended(provider: Provider, models: list[ModelInfo]) -> str | None:
    """The model preselected in the model window: Gemini
    gemini-flash-latest, DeepSeek deepseek-chat, OpenRouter the first free Qwen; otherwise the
    first listed; with no list, qwen-plus for Qwen and None for the rest."""
    ids = [m.id for m in models]
    if provider == Provider.OPENROUTER:
        free_qwen = next((m.id for m in models if m.free and m.id.startswith("qwen/")), None)
        if free_qwen:
            return free_qwen
    elif _RECOMMENDED.get(provider) in ids or (not ids and provider in _RECOMMENDED):
        return _RECOMMENDED[provider]
    return ids[0] if ids else None


def model_info(
    provider: Provider, key: str, model: str, *, timeout: float = 10.0
) -> ModelInfo | None:
    """The model description with limits: Gemini and Claude by GET, OpenRouter from the public
    list; None for the rest. Raises ProviderError | httpx.HTTPError."""
    if provider == Provider.OPENROUTER:
        return next((m for m in _raw_models(provider, key, timeout) if m.id == model), None)
    if provider == Provider.GEMINI:
        return _gemini_info(_get(provider, key, f"{_GEMINI}/{model}", timeout))
    if provider == Provider.CLAUDE:
        info = _claude_info(_get(provider, key, f"{_CLAUDE}/{model}", timeout))
        return info.model_copy(update={"id": model})  # an alias keeps its own name
    return None


def litellm_model(provider: Provider, model: str) -> str:
    """The litellm name: "gemini/…", "deepseek/…", "openai/…", "anthropic/…", "dashscope/…",
    "openrouter/…", "groq/…"."""
    return f"{_PREFIX[provider]}/{model}"


def litellm_base(provider: Provider) -> str | None:
    """api_base for litellm: Qwen's international region, the default for the rest."""
    return QWEN_BASE if provider == Provider.QWEN else None


def provider_of(litellm_name: str) -> tuple[Provider, str] | None:
    """ "gemini/gemini-pro-latest" → (GEMINI, "gemini-pro-latest"); an unknown prefix → None."""
    prefix, sep, model = litellm_name.partition("/")
    found = next((p for p, x in _PREFIX.items() if x == prefix), None)
    return (found, model) if found is not None and sep and model else None


def key_looks_like(secret: str) -> Provider | None:
    """The provider by key prefix: "AIza" Gemini, "sk-ant-" Claude, "sk-proj-" OpenAI,
    "sk-or-" OpenRouter, "gsk_" Groq."""
    return next((p for prefix, p in KEY_PREFIXES if secret.startswith(prefix)), None)


def reset_time(
    provider: Provider, kind: ErrorKind, exc: BaseException | None, now: datetime
) -> datetime | None:
    """When the limit resets: DAILY_LIMIT is the next midnight America/Los_Angeles for Gemini,
    midnight UTC for OpenRouter, in 1 h for the rest; RATE_LIMIT is retry_after or 60 s;
    otherwise None."""
    if kind == ErrorKind.DAILY_LIMIT:
        if provider not in (Provider.GEMINI, Provider.OPENROUTER):
            return now + timedelta(hours=1)
        from zoneinfo import ZoneInfo

        zone = ZoneInfo("America/Los_Angeles" if provider == Provider.GEMINI else "UTC")
        day = now.astimezone(zone).date() + timedelta(days=1)
        return datetime.combine(day, time(0), tzinfo=zone).astimezone(now.tzinfo)
    if kind == ErrorKind.RATE_LIMIT:
        return now + timedelta(seconds=(retry_after(exc) if exc is not None else None) or 60.0)
    return None
