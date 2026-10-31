"""Model provider errors, shared by call retries, recording remarks and the model check of the
model window (classify, check_refusal).

Quota and credits are not the recording's fault: such calls are not retried (backing off on 402
is pointless).
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel

from ..i18n import _


class ErrorKind(StrEnum):
    """Why the model provider refused."""

    INVALID_KEY = "invalid_key"
    NO_MONEY = "no_money"
    RATE_LIMIT = "rate_limit"
    DAILY_LIMIT = "daily_limit"
    OVERLOADED = "overloaded"
    NETWORK = "network"
    DISABLED = "disabled"  # key disabled (leaked, service turned off)
    NO_MODEL = "no_model"  # no access to the model (not offered, not in the plan)
    REGION = "region"  # provider refuses requests from this country or network
    PRIVACY = "privacy"  # OpenRouter: blocked by the account's privacy settings
    OTHER = "other"


# explicit money words only: Gemini's 429 "check your plan and billing details" is a rate limit
_NO_MONEY = (
    "insufficient balance", "insufficient funds", "credit balance", "credits are depleted",
    "purchase credits", "out of credits", "arrearage", "insufficient credits",
)  # fmt: skip
_LIMIT = (
    "ratelimit", "rate limit", "rate_limit", "resource_exhausted", "quota", "too many requests",
)  # fmt: skip
_BAD_KEY = (
    "api key not valid", "api_key_invalid", "invalid api key", "incorrect api key",
    "invalidapikey",
)  # fmt: skip
_DISABLED = (
    "leaked", "api_key_service_blocked", "service_disabled", "has not been used in project",
    "deactivated", "disabled", "suspended",
)  # fmt: skip
_OVERLOADED = ("overloaded", "high demand", "unavailable", "internalserver")
_NETWORK = (
    "timeout", "timed out", "connect", "peer closed", "incomplete chunked", "remoteprotocol",
)  # fmt: skip
_FREE_TIER_ZERO = re.compile(r"limit:\s*0\b")  # model not in Gemini's free tier
# daily quota: Gemini quotaId …PerDay…, OpenRouter "free-models-per-day", OpenAI "per day";
# OpenRouter "free-models-per-min" is per minute
_PER_DAY = re.compile(r"perday|per[- ]day")
# the model's free tier is used up for good (DashScope "AllocationQuota.FreeTierOnly")
_FREE_TIER_OVER = re.compile(r"freetieronly|free tier\b.*\bexhausted")
# Gemini 400 FAILED_PRECONDITION "User location is not supported for the API use.", OpenAI 403
# unsupported_country_region_territory, "… unavailable in your region"
_REGION = re.compile(
    r"location is not supported|in your (?:region|country)|unsupported_country|"
    r"country, region,? or territory"
)


def classify(exc: BaseException) -> ErrorKind:
    """Refusal kind from the HTTP code and the text; the order of checks matters. 429 is a rate
    limit (daily for a PerDay quota) except "limit: 0" (model not in the plan) and
    insufficient_quota (no money); money is 402 or explicit balance words; country or network
    comes first: "Service unavailable in your region" is not overload. Overload words count
    only without a code."""
    exc = root_cause(exc)
    code = getattr(exc, "status_code", None)
    text = f"{type(exc).__name__} {exc}".lower()
    if _REGION.search(text):
        return ErrorKind.REGION
    if "data policy" in text:  # 404 OpenRouter "No endpoints found matching your data policy"
        return ErrorKind.PRIVACY
    if _FREE_TIER_OVER.search(text):
        return ErrorKind.NO_MODEL
    if code == 429 or (code != 402 and any(m in text for m in _LIMIT)):
        if _FREE_TIER_ZERO.search(text):
            return ErrorKind.NO_MODEL
        if "insufficient_quota" in text:
            return ErrorKind.NO_MONEY
        daily = _PER_DAY.search(text) and "per-min" not in text
        return ErrorKind.DAILY_LIMIT if daily else ErrorKind.RATE_LIMIT
    if code == 402 or any(m in text for m in _NO_MONEY):
        return ErrorKind.NO_MONEY
    if code == 404 or "model_not_found" in text or "not_found" in text:
        return ErrorKind.NO_MODEL
    if code == 403:
        return ErrorKind.DISABLED if any(m in text for m in _DISABLED) else ErrorKind.NO_MODEL
    if code == 401 or "authentication" in text or any(m in text for m in _BAD_KEY):
        return ErrorKind.INVALID_KEY
    name = type(exc).__name__.lower()
    if code == 408 or "timeout" in name or "connection" in name or "connecterror" in name:
        return ErrorKind.NETWORK
    if (isinstance(code, int) and code >= 500) or (
        code is None and any(m in text for m in _OVERLOADED)
    ):
        return ErrorKind.OVERLOADED
    if any(m in text for m in _NETWORK):
        return ErrorKind.NETWORK
    return ErrorKind.OTHER


def retry_after(exc: BaseException) -> float | None:
    """Seconds the server asks to wait before a retry: the Retry-After header or Gemini's
    RetryInfo retryDelay ("retryDelay": "16s"); None if it does not ask."""
    headers = getattr(getattr(exc, "response", None), "headers", None)
    value = headers.get("retry-after") if headers is not None else None
    if value is not None and re.fullmatch(r"\d+(?:\.\d+)?", str(value).strip()):
        return float(value)
    m = re.search(r"retryDelay\W+(\d+(?:\.\d+)?)s", str(exc))
    return float(m[1]) if m else None


_WRAPPER_MARKERS = ("are this object's requirements defined correctly",)


def root_cause(exc: BaseException) -> BaseException:
    """Unwrap uninformative wrappers: transformers wraps any import failure in "Could not import
    module X. Are this object's requirements defined correctly?", the real cause is __cause__."""
    seen = 0
    while exc.__cause__ is not None and seen < 5:
        if not any(m in str(exc).lower() for m in _WRAPPER_MARKERS):
            break
        exc = exc.__cause__
        seen += 1
    return exc


# English sources; shown through _() (live/remarks uses the same wording for the same kinds)
_REASONS = {
    ErrorKind.INVALID_KEY: "the model key was rejected",
    ErrorKind.NO_MONEY: "the account is out of money",
    ErrorKind.RATE_LIMIT: "request limit exceeded",
    ErrorKind.DAILY_LIMIT: "daily request limit used up",
    ErrorKind.OVERLOADED: "the model service is not responding",
    ErrorKind.DISABLED: "the model key is disabled",
    ErrorKind.NO_MODEL: "no access to the model",
    ErrorKind.REGION: "the model service does not accept requests from your country or network",
    ErrorKind.PRIVACY: "the model is blocked by OpenRouter privacy settings",
}
# Texts that mean "no model" or "no key", in both interface languages (an error may come from a
# server or an older version speaking the other one)
_NO_MODEL_LEGACY = "не задана llm-модель"
_NOT_CONFIGURED = (
    "no model configured",
    "no model is configured",
    "model not set up",
    "модель не настроена",
    "не настроена модель",
    "облачной модели",
)
_NO_KEY = ("no model key set", "не задан ключ модели", "mnema_api_key")


def human_reason(exc: BaseException) -> str:
    """Why the model refused, in the user's words ("the account is out of money"), for
    remarks after processing and for logs; never contains a key (llm.redact)."""
    return _reason(root_cause(exc))


def _reason(exc: BaseException) -> str:
    from .redact import redact

    text = redact(str(exc)).strip()  # before truncation: redact cannot spot a cut key
    lowered = text.lower()
    if _NO_MODEL_LEGACY in lowered:
        return _("no model is selected in the settings")
    if any(p in lowered for p in _NOT_CONFIGURED):
        return _("no model configured")
    if any(p in lowered for p in _NO_KEY):
        return _("no model key set")
    if "torchvision::" in lowered or "could not find qwen3forcausallm" in lowered:
        return _("Mnema is not fully installed, reinstall it")
    kind = classify(exc)
    if kind in _REASONS:
        return _(_REASONS[kind])
    if kind == ErrorKind.NETWORK:
        timeout = "timeout" in type(exc).__name__.lower() or "timed out" in lowered
        if timeout:
            return _("the model service did not answer in time")
        return _("no connection to the model service")
    code = getattr(exc, "status_code", None)
    if code:
        return _("the model service answered with error {code}", code=code)
    first = text.splitlines()[0] if text else ""
    if not first:
        return _("unknown model service error")
    return (first[:120] + "…") if len(first) > 120 else first


def human_sentence(exc: BaseException) -> str:
    """human_reason capitalized: the error in the model window."""
    reason = human_reason(exc)
    return reason[:1].upper() + reason[1:]


# the check failed because of the key: back to the "Key" step, not to choosing a model
KEY_TROUBLE = frozenset({ErrorKind.INVALID_KEY, ErrorKind.DISABLED, ErrorKind.NO_MONEY})


class Refusal(BaseModel):
    """A failed model check: the text for the user and whether the key is to blame."""

    text: str
    key: bool  # True: the "Key" step again, otherwise "Model"


def check_refusal(exc: BaseException | None) -> Refusal:
    """ "The model key was rejected: enter another key", "No access to the model: choose another
    model"; exc None means the model answered with empty text."""
    if exc is None:
        return Refusal(
            text=_("The model returned an empty answer: choose another model"), key=False
        )
    key = classify(exc) in KEY_TROUBLE
    reason = human_sentence(exc)
    if key:
        return Refusal(text=_("{reason}: enter another key", reason=reason), key=True)
    return Refusal(text=_("{reason}: choose another model", reason=reason), key=False)
