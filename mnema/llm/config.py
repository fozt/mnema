"""LLM settings (pydantic-settings): which model is active and how to reach it.

One model for every task: MNEMA_MODEL, MNEMA_API_KEY, MNEMA_API_BASE and the cloud recognition
key MNEMA_ASR_API_KEY are lines of ~/.config/mnema/.env (written by `mnema model`) or process
environment variables (service, export). Model, key and address come from one place: with
MNEMA_MODEL in the process environment all three come from there, otherwise all three from the
file; MNEMA_ASR_API_KEY from the environment overrides the file. The file is read every time
LLMSettings is created, so an edit applies without a restart. There are no fallback models.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from ..core.base import MediaError
from ..i18n import _

if TYPE_CHECKING:
    from .providers import Provider

Source = Literal["env", "unconfigured"]
# model, key and address always come from one place: a key never reaches another provider
MODEL_GROUP = ("MNEMA_MODEL", "MNEMA_API_KEY", "MNEMA_API_BASE")
# model lines: read from the file on every access, load_env does not copy them to os.environ
MODEL_VARS = (*MODEL_GROUP, "MNEMA_ASR_API_KEY")
# English sources, translated where shown
NO_MODEL = "No model configured"
NO_KEY = "No model key set"


class Credentials(BaseModel):
    """The only way into litellm: model, key and address."""

    model: str  # litellm name
    api_key: str
    api_base: str | None = None

    @property
    def name(self) -> str:
        """The model name without the litellm prefix."""
        return self.model.split("/", 1)[-1]


def credentials_for(model: str, api_key: str, api_base: str | None = None) -> Credentials:
    """Credentials of a model; without an address, the provider's (Qwen: international region)."""
    from .providers import litellm_base, provider_of

    found = provider_of(model)
    base = api_base or (litellm_base(found[0]) if found else None)
    return Credentials(model=model, api_key=api_key, api_base=base)


def file_values() -> dict[str, str]:
    """Non-empty model lines of ~/.config/mnema/.env; empty if the file is missing or unreadable."""
    from dotenv import dotenv_values

    from .. import config

    path = config.user_env_file()
    try:
        if not path.is_file():
            return {}
        values = dotenv_values(path, interpolate=False, encoding="utf-8")
    except (OSError, UnicodeError):
        return {}
    return {k: v for k, v in values.items() if k in MODEL_VARS and v}


class LLMSettings(BaseSettings):
    """Cloud model and key: with MNEMA_MODEL in the process environment, model, key and address
    come from there, otherwise from ~/.config/mnema/.env; re-read on every creation."""

    model_config = SettingsConfigDict(extra="ignore")

    # litellm name "provider/model"
    model: str | None = Field(default=None, validation_alias="MNEMA_MODEL")
    # provider (or gateway) key; api_base for a gateway or a self-hosted model
    api_key: str | None = Field(default=None, validation_alias="MNEMA_API_KEY")
    api_base: str | None = Field(default=None, validation_alias="MNEMA_API_BASE")
    # cloud recognition key of a provider other than MNEMA_MODEL's (Groq, OpenAI)
    asr_api_key: str | None = Field(default=None, validation_alias="MNEMA_ASR_API_KEY")
    # one LLM call: without it LiteLLM waits ~100 min and a hung provider stalls processing
    timeout: float = Field(default=90.0, validation_alias="MNEMA_API_TIMEOUT")

    def __init__(self, **values: Any) -> None:
        saved = file_values()
        if not os.environ.get("MNEMA_MODEL"):  # model from the file: key and address too
            for name in MODEL_GROUP:
                values.setdefault(name, saved.get(name))
        if not os.environ.get("MNEMA_ASR_API_KEY") and "MNEMA_ASR_API_KEY" in saved:
            values.setdefault("MNEMA_ASR_API_KEY", saved["MNEMA_ASR_API_KEY"])
        super().__init__(**values)

    @property
    def source(self) -> Source:
        """Where the model comes from: env when MNEMA_MODEL is set (file or environment)."""
        return "env" if self.model else "unconfigured"

    @property
    def cloud(self) -> bool:
        """A cloud model with a key is set."""
        return bool(self.model and self.api_key)

    def credentials(self) -> Credentials:
        """Key and address of the active model for litellm. MediaError without a model or a key
        (MNEMA_API_KEY is required: a provider key is never picked up implicitly)."""
        model = self.model
        if not model:
            raise MediaError(_(NO_MODEL))
        if not self.api_key:
            raise MediaError(_(NO_KEY))
        return credentials_for(model, self.api_key, self.api_base)

    def provider_key(self, provider: Provider) -> str | None:
        """MNEMA_API_KEY as the provider's own key: its model and its address (not a gateway or a
        self-hosted server, whose key must not reach the provider); otherwise None."""
        from .providers import litellm_base, provider_of

        found = provider_of(self.model or "")
        if found is None or found[0] != provider or not self.api_key:
            return None
        if self.api_base and self.api_base.rstrip("/") != (litellm_base(provider) or ""):
            return None
        return self.api_key

    def asr_key(self, provider: Provider) -> str | None:
        """The provider's cloud recognition key: its own MNEMA_API_KEY (provider_key), otherwise
        MNEMA_ASR_API_KEY if it looks like this provider's key: Groq keys always start with
        "gsk_", an OpenAI key is any key not of another provider."""
        from .providers import Provider, key_looks_like

        own = self.provider_key(provider)
        if own is not None:
            return own
        secret = self.asr_api_key
        if not secret:
            return None
        looks = key_looks_like(secret)
        if provider == Provider.GROQ:
            return secret if looks == Provider.GROQ else None
        return secret if looks in (None, provider) else None


def current_line() -> str:
    """ "Current: gemini/gemini-flash-latest, key …a1B2" or "Model not set up", for
    `mnema model --show` and the model window; the key is never shown in full."""
    from .redact import mask

    settings = LLMSettings()
    model = settings.model
    if not model:
        return _("Model not set up")
    env = bool(os.environ.get("MNEMA_MODEL"))
    if settings.api_key:
        key = mask(settings.api_key)
        if env:
            return _(
                "Current: {model}, key {key}, from the process environment", model=model, key=key
            )
        return _("Current: {model}, key {key}", model=model, key=key)
    if env:
        return _("Current: {model}, no key, from the process environment", model=model)
    return _("Current: {model}, no key", model=model)


def save_model(model: str, api_key: str) -> None:
    """MNEMA_MODEL and MNEMA_API_KEY as lines of ~/.config/mnema/.env in one file replacement, so
    the server never reads a model without its key. The previous model's MNEMA_API_BASE is
    dropped: credentials_for supplies the provider's address."""
    from .. import config

    config.set_env({"MNEMA_MODEL": model, "MNEMA_API_KEY": api_key, "MNEMA_API_BASE": None})


def remove_model() -> bool:
    """Remove the model lines (MNEMA_MODEL, MNEMA_API_KEY, MNEMA_API_BASE); were there any."""
    from .. import config

    if not config.user_env_file().is_file():
        return False
    return bool(config.set_env(dict.fromkeys(MODEL_GROUP)))
