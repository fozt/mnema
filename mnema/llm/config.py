"""LLM settings (pydantic-settings): which model is active and how to reach it.

One model for every task: MNEMA_MODEL, MNEMA_API_KEY, MNEMA_API_BASE and the cloud recognition
key MNEMA_ASR_API_KEY are lines of ~/.config/mnema/.env (written by `mnema model`) or process
environment variables (service, export). Model, key and address come from one place: with
MNEMA_MODEL in the process environment all three come from there, otherwise all three from the
file; MNEMA_ASR_API_KEY from the environment overrides the file. The file is read every time
LLMSettings is created, so an edit applies without a restart. There are no fallback models.

On the server a user other than the owner may have an own model: the file
~/.config/mnema/users/<name>.env with MNEMA_MODEL is taken whole, the environment and the shared
file do not reach it; an unreadable file means no model at all.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field, PrivateAttr
from pydantic_settings import BaseSettings, SettingsConfigDict

from .. import users
from ..core.base import MediaError
from ..i18n import _

if TYPE_CHECKING:
    from .providers import Provider

Source = Literal["env", "user", "unconfigured"]
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


def file_values(path: Path) -> dict[str, str] | None:
    """Non-empty model lines of an .env file: empty without the file, None when it is there but
    cannot be read."""
    from dotenv import dotenv_values

    try:
        if not path.exists():
            return {}
        if not path.is_file():
            return None
        values = dotenv_values(path, interpolate=False, encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    return {k: v for k, v in values.items() if k in MODEL_VARS and v}


def _own_lines(user: str | None) -> dict[str, str] | None:
    """The model lines of the user's own model; None — the shared model (no user, the owner, no
    file or no MNEMA_MODEL in it). An unreadable file is an own model with no lines."""
    if user is None or user == users.OWNER:
        return None
    values = file_values(users.user_env_file(user))
    if values is None:
        return {}
    return values if values.get("MNEMA_MODEL") else None


def own_model(name: str) -> str | None:
    """The model of the user's own file, None when the user is on the shared model."""
    own = _own_lines(name)
    return own.get("MNEMA_MODEL") if own else None


class LLMSettings(BaseSettings):
    """Cloud model and key: the user's own file when there is one with a model; otherwise with
    MNEMA_MODEL in the process environment, model, key and address come from there, otherwise
    from ~/.config/mnema/.env; re-read on every creation. In the server mode (users.strict) a
    use outside a user's request or job is a RuntimeError, not the owner's key spent silently."""

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
    _own: bool = PrivateAttr(default=False)

    def __init__(self, **values: Any) -> None:
        from .. import config

        user = users.current_user()
        if user is None and users.strict():
            raise RuntimeError("the model is used outside a user's request or job")
        own = _own_lines(user)
        if own is not None:  # the whole file: no line of the environment or the shared file
            for name in MODEL_VARS:
                values.setdefault(name, own.get(name))
            super().__init__(**values)
            self._own = True
            return
        saved = file_values(config.user_env_file()) or {}
        if not os.environ.get("MNEMA_MODEL"):  # model from the file: key and address too
            for name in MODEL_GROUP:
                values.setdefault(name, saved.get(name))
        if not os.environ.get("MNEMA_ASR_API_KEY") and "MNEMA_ASR_API_KEY" in saved:
            values.setdefault("MNEMA_ASR_API_KEY", saved["MNEMA_ASR_API_KEY"])
        super().__init__(**values)

    @property
    def source(self) -> Source:
        """Where the model comes from: user — the user's own file, env — the shared one (file or
        environment)."""
        if not self.model:
            return "unconfigured"
        return "user" if self._own else "env"

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


def _user_file(user: str | None) -> Path | None:
    """The file of the user's own model; None — the shared .env (no user or the owner)."""
    return None if user is None or user == users.OWNER else users.user_env_file(user)


def current_line(*, user: str | None = None) -> str:
    """ "Current: gemini/gemini-flash-latest, key …a1B2" or "Model not set up", for
    `mnema model --show` and the model window; for a user, the own or the shared model. The key
    is never shown in full."""
    from .redact import mask

    path = _user_file(user)
    if path is not None:
        with users.as_user(user):
            settings = LLMSettings()
        if settings.source == "user":
            if settings.api_key:
                key = mask(settings.api_key)
                return _("Own model: {model}, key {key}", model=settings.model, key=key)
            return _("Own model: {model}, no key", model=settings.model)
        if file_values(path) is None:
            return _("The model file cannot be read: {path}", path=path)
        if settings.model:
            return _("Shared server model: {model}", model=settings.model)
        return _("Model not set up")
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


def save_model(model: str, api_key: str, *, user: str | None = None) -> None:
    """MNEMA_MODEL and MNEMA_API_KEY as lines of ~/.config/mnema/.env (or of the user's own file)
    in one file replacement, so the server never reads a model without its key. The previous
    model's MNEMA_API_BASE is dropped: credentials_for supplies the provider's address."""
    from .. import config

    config.set_env(
        {"MNEMA_MODEL": model, "MNEMA_API_KEY": api_key, "MNEMA_API_BASE": None},
        path=_user_file(user),
    )


def remove_model(*, user: str | None = None) -> bool:
    """Remove the model lines (MNEMA_MODEL, MNEMA_API_KEY, MNEMA_API_BASE); were there any. The
    user is on the shared model again, other lines of the file stay."""
    from .. import config

    path = _user_file(user)
    if not (path or config.user_env_file()).is_file():
        return False
    return bool(config.set_env(dict.fromkeys(MODEL_GROUP), path=path))
