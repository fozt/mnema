"""Model limits: summary request sizes from the model's real limits, not constants."""

from __future__ import annotations

import logging
import time

from pydantic import BaseModel

from .config import Credentials

logger = logging.getLogger(__name__)


class Limits(BaseModel):
    input_tokens: int | None = None
    output_tokens: int | None = None


_DEFAULT = Limits(input_tokens=32_768, output_tokens=8_192)

_ONE_PASS_MAX = 300_000  # one-pass cap: quality on very long input was never measured
_PROMPT_TOKENS = 2_000  # the instructions
CHARS_PER_TOKEN = 3  # with a margin for Cyrillic
# provider model descriptions: (model, key) → (limits, when asked); a success lasts for the
# process, a failure (None) for _RETRY_FAILED, so calls do not each wait for a refusal or timeout
_FETCHED: dict[tuple[str, str], tuple[Limits | None, float]] = {}
_RETRY_FAILED = 600.0


def _described(creds: Credentials) -> Limits | None:
    """The model description from Gemini, Claude and OpenRouter without a custom address (a
    gateway would not accept the provider key)."""
    from .providers import Provider, model_info, provider_of

    found = provider_of(creds.model)
    described = (Provider.GEMINI, Provider.CLAUDE, Provider.OPENROUTER)
    if creds.api_base or found is None or found[0] not in described:
        return None
    cache = (creds.model, creds.api_key)
    known = _FETCHED.get(cache)
    if known is not None and (known[0] is not None or time.monotonic() - known[1] < _RETRY_FAILED):
        return known[0]
    try:
        info = model_info(found[0], creds.api_key, found[1])
    except Exception as e:  # noqa: BLE001 - no description: fall back to litellm
        from .errors import human_reason

        logger.warning(
            "could not get the limits of %s from the provider (%s)", creds.name, human_reason(e)
        )
        info = None
    got = Limits(input_tokens=info.input_tokens, output_tokens=info.output_tokens) if info else None
    _FETCHED[cache] = (got, time.monotonic())
    return got


def _litellm(creds: Credentials) -> Limits | None:
    try:
        import litellm

        litellm.suppress_debug_info = True
        info = litellm.get_model_info(creds.model)
    except Exception:  # noqa: BLE001 - unknown to litellm or no litellm
        return None
    return Limits(
        input_tokens=info.get("max_input_tokens") or None,
        output_tokens=info.get("max_output_tokens") or None,
    )


def model_limits(creds: Credentials) -> Limits:
    """Input and output limits: the Gemini, Claude or OpenRouter description without a custom
    address (providers.model_info, once per process), then litellm.get_model_info, then
    32768/8192 with a warning in the log."""
    out = Limits()
    for source in (lambda: _described(creds), lambda: _litellm(creds)):
        if out.input_tokens and out.output_tokens:
            break
        if found := source():
            out = Limits(
                input_tokens=out.input_tokens or found.input_tokens,
                output_tokens=out.output_tokens or found.output_tokens,
            )
    if not (out.input_tokens and out.output_tokens):
        logger.warning("limits of %s are unknown", creds.name)
        out = Limits(
            input_tokens=out.input_tokens or _DEFAULT.input_tokens,
            output_tokens=out.output_tokens or _DEFAULT.output_tokens,
        )
    return out


def one_pass_chars(creds: Credentials, out: int) -> int:
    """How many characters fit into one request with an answer of up to out tokens:
    min(300_000, (input − 2000 − out) × 3)."""
    window = model_limits(creds).input_tokens or 0
    return max(1_000, min(_ONE_PASS_MAX, (window - _PROMPT_TOKENS - out) * CHARS_PER_TOKEN))
