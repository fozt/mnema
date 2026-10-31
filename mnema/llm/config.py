"""LLM settings (pydantic-settings): which model is active and how to reach it.

One model for every task. On the server (users.strict) the model and keys are the lines of the
user's file ~/.config/mnema/users/<name>.env (the owner's too), written from the app through
the model API: every field comes from there, the process environment and the shared .env do
not reach it; without the file or a line there is none. Outside the server (Mac, CLI, MCP)
MNEMA_MODEL, MNEMA_API_KEY, MNEMA_API_BASE and the cloud recognition key MNEMA_ASR_API_KEY are
lines of ~/.config/mnema/.env or process environment variables (export): with MNEMA_MODEL in
the process environment model, key and address come from there, otherwise all three from the
file; MNEMA_ASR_API_KEY from the environment overrides the file. Files are read every time
LLMSettings is created, so an edit applies without a restart. There are no fallback models.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .. import users
from ..core.base import MediaError
from ..i18n import _

if TYPE_CHECKING:
    from .providers import Provider

Source = Literal["env", "user", "unconfigured"]
# model, key and address always come from one place: a key never reaches another provider
MODEL_GROUP = ("MNEMA_MODEL", "MNEMA_API_KEY", "MNEMA_API_BASE")
# lines of a user's file on the server; read from the file on every access, load_env does not
# copy them to os.environ
USER_VARS = (*MODEL_GROUP, "MNEMA_ASR_API_KEY", "DEEPL_API_KEY")
KEY_VARS = ("MNEMA_ASR_API_KEY", "DEEPL_API_KEY")  # the user's keys besides the model's
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
    """Non-empty model and key lines of an .env file: empty without the file, None when it is
    there but cannot be read."""
    from dotenv import dotenv_values

    try:
        if not path.exists():
            return {}
        if not path.is_file():
            return None
        values = dotenv_values(path, interpolate=False, encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    return {k: v for k, v in values.items() if k in USER_VARS and v}


def _user_lines(user: str) -> dict[str, str]:
    """The lines of a server user's file; an unreadable file has none."""
    return file_values(users.user_env_file(user)) or {}


class LLMSettings(BaseSettings):
    """Cloud model and key: on the server only the user's file; otherwise with MNEMA_MODEL in the
    process environment, model, key and address come from there, otherwise from
    ~/.config/mnema/.env; re-read on every creation. On the server a use outside a user's
    request or job is a RuntimeError, not someone's key spent silently."""

    model_config = SettingsConfigDict(extra="ignore")

    # litellm name "provider/model"
    model: str | None = Field(default=None, validation_alias="MNEMA_MODEL")
    # provider (or gateway) key; api_base for a gateway or a self-hosted model
    api_key: str | None = Field(default=None, validation_alias="MNEMA_API_KEY")
    api_base: str | None = Field(default=None, validation_alias="MNEMA_API_BASE")
    # cloud recognition key (OpenAI), not tied to the summary model
    asr_api_key: str | None = Field(default=None, validation_alias="MNEMA_ASR_API_KEY")
    # one LLM call: without it LiteLLM waits ~100 min and a hung provider stalls processing
    timeout: float = Field(default=90.0, validation_alias="MNEMA_API_TIMEOUT")

    def __init__(self, **values: Any) -> None:
        from .. import config

        if users.strict():
            user = users.current_user()
            if user is None:
                raise RuntimeError("the model is used outside a user's request or job")
            lines = _user_lines(user)
            for name in (*MODEL_GROUP, "MNEMA_ASR_API_KEY"):  # every field: none from elsewhere
                values.setdefault(name, lines.get(name))
            super().__init__(**values)
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
        """Where the model comes from: user — the server user's file, env — this machine's .env
        or environment."""
        if not self.model:
            return "unconfigured"
        return "user" if users.strict() else "env"

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


def user_settings(user: str) -> LLMSettings:
    """The model and keys of a server user's file in any mode (the model API, `users`)."""
    lines = _user_lines(user)
    return LLMSettings.model_construct(
        model=lines.get("MNEMA_MODEL"),
        api_key=lines.get("MNEMA_API_KEY"),
        api_base=lines.get("MNEMA_API_BASE"),
        asr_api_key=lines.get("MNEMA_ASR_API_KEY"),
    )


def deepl_key() -> str | None:
    """The DeepL key of live translation: on the server the line of the user's file; outside
    the server there is no DeepL."""
    if not users.strict():
        return None
    user = users.current_user()
    if user is None:
        raise RuntimeError("the DeepL key is used outside a user's request or job")
    return _user_lines(user).get("DEEPL_API_KEY")


def masked(user: str) -> dict[str, str | None]:
    """The user's model and keys for the app: the keys only as "…a1B2", None when not set."""
    from .redact import mask

    lines = _user_lines(user)

    def hidden(name: str) -> str | None:
        return mask(lines[name]) if name in lines else None

    return {
        "model": lines.get("MNEMA_MODEL"),
        "key": hidden("MNEMA_API_KEY"),
        "asr_key": hidden("MNEMA_ASR_API_KEY"),
        "deepl_key": hidden("DEEPL_API_KEY"),
    }


def current_line() -> str:
    """ "Current: gemini/gemini-flash-latest, key …a1B2" or "Model not set up", for the model
    window. The key is never shown in full."""
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


def _env_path(user: str | None) -> Path | None:
    """The server user's file; None — this machine's .env."""
    return None if user is None else users.user_env_file(user)


def save_model(model: str, api_key: str, *, user: str | None = None) -> None:
    """MNEMA_MODEL and MNEMA_API_KEY as lines of the user's file on the server (None: this
    machine's ~/.config/mnema/.env) in one file replacement, so the server never reads a model
    without its key. The previous model's MNEMA_API_BASE is dropped: credentials_for supplies
    the provider's address."""
    from .. import config

    config.set_env(
        {"MNEMA_MODEL": model, "MNEMA_API_KEY": api_key, "MNEMA_API_BASE": None},
        path=_env_path(user),
    )


def remove_model(*, user: str | None = None) -> bool:
    """Remove the model lines (MNEMA_MODEL, MNEMA_API_KEY, MNEMA_API_BASE); were there any.
    Other lines of the file (the recognition and DeepL keys) stay."""
    from .. import config

    path = _env_path(user)
    if not (path or config.user_env_file()).is_file():
        return False
    return bool(config.set_env(dict.fromkeys(MODEL_GROUP), path=path))


def save_key(name: str, value: str | None, *, user: str) -> None:
    """The user's recognition (MNEMA_ASR_API_KEY) or DeepL (DEEPL_API_KEY) key; None removes
    it."""
    from .. import config

    if name not in KEY_VARS:
        raise ValueError(name)
    path = users.user_env_file(user)
    if value is None and not path.is_file():
        return
    config.set_env({name: value}, path=path)
