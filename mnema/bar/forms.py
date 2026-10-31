"""Windows with a check, as pure logic without threads: the server connection (macOS and Linux
app), the summary model and the recognition and DeepL keys (Mac only).

ask() opens the window; submit() is "Next" or "Check and save" (force is "Save anyway") and
returns the next window state, or None when saved and the window closes. An error keeps the window
open with a red line and keeps what was typed. The window state never holds secrets.
"""

from __future__ import annotations

from typing import Literal

import httpx
from pydantic import BaseModel, Field

from ..connection import Connection
from ..core.base import MediaError
from ..i18n import _
from ..llm import providers
from ..llm.providers import KEY_PAGES, TITLES, ModelInfo, Provider
from .protocol import Choice, FormAsk, FormField, FormFieldKind, FormOp
from .server_model import ModelRefused, ServerModel

_OPENAI_KEYS = KEY_PAGES[Provider.OPENAI]
_DEEPL_KEYS = "deepl.com/your-account/keys"

_CHECKING = "Checking…"


def checking() -> str:
    """The busy text of a form window while its step checks over the network."""
    return _(_CHECKING)


def __getattr__(name: str) -> str:
    # `from .forms import CHECKING` in older callers: translated when imported
    if name == "CHECKING":
        return checking()
    raise AttributeError(name)


def _setup_text(text: str) -> str:
    """A message of install_setup (NO_ANSWER, PASTE_INVITE…), shown in the window language."""
    return _(text)


class FormEvent(BaseModel):
    """An action in a form window. repr hides the values: they include the model key."""

    op: FormOp
    values: dict[str, str] = Field(repr=False)
    rev: int = 0


class ServerFlow:
    """The "Connect to server" window: a server invitation (#invite=) or the address of an open
    server; a line with a token (#token=) is refused. The invitation is exchanged for this
    device's token and saved at once, since it cannot be exchanged twice. The same address
    without an invitation keeps the saved token. The check is the same probe as the phone's;
    saving is install_setup.connect, as in the installer. The window never shows the token.
    result is (answers, accepted the token) after saving."""

    checks = True  # every step checks the server

    def __init__(self) -> None:
        self.closed = False
        self.result: tuple[bool, bool] | None = None
        self._last = (False, True)
        self._claimed: tuple[str, str, str] | None = None  # (url, code, device token)

    def _form(self, *, error: str = "", force: str = "") -> FormAsk:
        from ..config import Settings
        from .settings_model import host

        s = Settings()
        return FormAsk(
            title=_("Connect to server"),
            message=_("Connected: {host}", host=host(s.runner)) if s.runner else "",
            fields=[
                FormField(
                    id="connection",
                    label=_("Invitation"),
                    placeholder=s.runner or _("Invitation from the server"),
                ),
            ],
            error=error,
            force=force,
            ok=_("Check and save"),
        )

    def ask(self) -> FormAsk:
        return self._form()

    def _device_token(self, url: str, code: str) -> str | FormAsk | None:
        """Exchange the invitation for a device token (once per invitation) and save it at once.
        A refusal returns the window with an error; None when the window closed first."""
        from ..install_setup import connect, exchange_invite

        if self._claimed is not None and self._claimed[:2] == (url, code):
            return self._claimed[2]
        if self.closed:
            return None
        try:
            token = exchange_invite(url, code, transport=providers.TRANSPORT)
        except ValueError as exc:
            return self._form(error=str(exc))
        self._claimed = (url, code, token)
        try:
            connect(url, token)
        except OSError as e:
            return self._form(error=_("Could not save: {error}", error=e.strerror or e))
        return token

    def submit(self, values: dict[str, str], *, force: bool) -> FormAsk | None:
        from ..client import MnemaClient
        from ..config import Settings
        from ..connection import Answer, parse_connection
        from ..install_setup import NEW_INVITE, NO_ANSWER, NOT_MNEMA, PASTE_INVITE, connect

        saved = Settings()
        text = values.get("connection", "").strip()
        if not text and saved.runner:
            url, token = saved.runner, saved.token or ""
        else:
            if not text:
                return self._form(error=_setup_text(PASTE_INVITE))
            try:
                connection = parse_connection(text)
            except ValueError as exc:
                return self._form(error=str(exc))
            if connection.token is not None:
                return self._form(error=_setup_text(PASTE_INVITE))
            url, token = connection.url, ""
            if connection.invite is not None:
                claimed = self._device_token(url, connection.invite)
                if not isinstance(claimed, str):
                    return claimed
                token = claimed
            elif url == saved.runner:
                token = saved.token or ""
        if not force:
            client = MnemaClient(url, token=token or None, transport=providers.TRANSPORT)
            answer, _name = client.probe(timeout=3.0)
            if answer is Answer.NO_ANSWER:
                self._last = (False, True)
                error = NO_ANSWER
            elif answer is Answer.TOKEN:
                self._last = (True, False)
                error = NEW_INVITE if token else PASTE_INVITE
            elif answer is Answer.NOT_MNEMA:
                self._last = (False, True)
                error = NOT_MNEMA
            else:
                self._last, error = (True, True), ""
            if error:
                return self._form(error=_setup_text(error), force=_("Save anyway"))
        if self.closed:
            return None
        try:
            connect(url, token)
        except OSError as e:
            return self._form(error=_("Could not save: {error}", error=e.strerror or e))
        self.result = self._last
        return None


class _Closed(Exception):
    """The model window closed during the check request: retry pauses stop at once."""


class ModelFlow:
    """The "Summary model" window, with the steps of `mnema model`: Provider (no network), Key
    (providers.check_key, a GET of the model list; a refusal clears the field), Model
    (claude.check_model, then llm.config.save_model). After a refusal the engine picks the step:
    a wrong key goes back to Key, no models back to Provider. The key lives only in the window
    field, here until the window closes, and in redact; the window state shows only "Keep …a1B2".
    With a connected server (server) the model is the user's on the server: the same steps go to
    /api/model* and the server checks and keeps it; this Mac's .env is not touched. current is
    the server's model as the settings know it."""

    def __init__(self, server: Connection | None = None, *, current: str | None = None) -> None:
        from ..llm.config import LLMSettings

        self.closed = False
        self._server = ServerModel(server) if server is not None else None
        self._current = current if server is not None else LLMSettings().model
        found = providers.provider_of(self._current or "")
        self._provider = found[0] if found else Provider.GEMINI
        self._step = "provider"
        self._key = ""
        self._saved: str | None = None  # the server's key of the current model, as a mask
        self._models: list[ModelInfo] = []

    @property
    def checks(self) -> bool:
        """The Provider step needs no network, except to ask the server for its saved key."""
        return self._step != "provider" or self._server is not None

    def ask(self) -> FormAsk:
        return self._provider_form()

    def _form(self, field: FormField, message: str, **kw) -> FormAsk:
        return FormAsk(title=_("Summary model"), message=message, fields=[field], **kw)

    def _current_line(self) -> str:
        from ..llm.config import current_line

        if self._server is None:
            return current_line()
        return (
            _("Current: {model}", model=self._current) if self._current else _("Model not set up")
        )

    def _provider_form(self, error: str = "") -> FormAsk:
        self._step = "provider"
        field = FormField(
            id="provider",
            label=_("Provider"),
            kind=FormFieldKind.CHOICE,
            choices=[
                Choice(key=p.value, title=TITLES[p], checked=p == self._provider) for p in Provider
            ],
        )
        return self._form(field, self._current_line(), ok=_("Next"), error=error)

    def _saved_key(self) -> str | None:
        """The saved key of the chosen provider as a mask: this Mac's, or the server's when its
        model is of this provider."""
        from ..llm.config import LLMSettings
        from ..llm.redact import mask

        if self._server is None:
            saved = LLMSettings().provider_key(self._provider)
            return mask(saved) if saved else None
        found = providers.provider_of(self._current or "")
        return self._saved if found and found[0] == self._provider else None

    def _key_form(self, error: str = "") -> FormAsk:
        """A refused key comes back with the error and a cleared field."""
        self._step = "key"
        saved = self._saved_key()
        field = FormField(
            id="key",
            label=_("API key"),
            placeholder=_("Keep {key}", key=saved) if saved else "",
        )
        page = _(
            "Get a {provider} key: {page}",
            provider=TITLES[self._provider],
            page=KEY_PAGES[self._provider],
        )
        return self._form(field, page, ok=_("Next"), error=error, clear=["key"] if error else [])

    def _model_form(self, chosen: str | None, error: str = "") -> FormAsk:
        self._step = "model"
        models = self._models or [ModelInfo(id=chosen or "")]
        field = FormField(
            id="model",
            label=_("Model"),
            kind=FormFieldKind.CHOICE,
            choices=[
                Choice(
                    key=m.id,
                    title=m.id,
                    detail=_("free") if m.free else "",
                    checked=m.id == chosen,
                )
                for m in models
            ],
        )
        return self._form(field, "", ok=_("Check and save"), error=error)

    def _stop_if_closed(self, _fraction: float | None, _message: str) -> None:
        if self.closed:
            raise _Closed

    def _choose_provider(self, values: dict[str, str]) -> FormAsk:
        try:
            self._provider = Provider(values.get("provider", ""))
        except ValueError:
            pass  # not in the list: keep the previous choice
        if self._server is not None:
            try:
                found = self._server.model()
            except (MediaError, httpx.HTTPError, ValueError):
                found = {}  # the key step goes on without "Keep …"
            if isinstance(found, dict):
                model, key = found.get("model"), found.get("key")
                self._current = model if isinstance(model, str) else None
                self._saved = key if isinstance(key, str) else None
        return self._key_form()

    def _check_key(self, typed: str) -> tuple[list[ModelInfo], str | None]:
        """The provider's models by the key and the recommended one; a refusal is a MediaError.
        The server checks with the saved key when nothing was typed."""
        from ..llm.config import LLMSettings

        if self._server is None:
            key = typed or LLMSettings().provider_key(self._provider) or ""
            models = providers.check_key(self._provider, key)
            self._key = key
            return models, providers.recommended(self._provider, models)
        keep = not typed and self._saved_key() is not None
        answer = self._server.model_key(self._provider.value, None if keep else typed)
        models = [
            ModelInfo(id=m["id"], free=bool(m.get("free")))
            for m in answer.get("models", [])
            if isinstance(m, dict) and isinstance(m.get("id"), str)
        ]
        best = answer.get("recommended")
        self._key = typed
        return models, best if isinstance(best, str) else providers.recommended(
            self._provider, models
        )

    def _save(self, name: str) -> FormAsk | None:
        """Check the model and save it, here or on the server; a refusal returns to the step
        the check names."""
        from ..llm.claude import check_model
        from ..llm.config import credentials_for, save_model

        model = name.partition("/")[2]
        if self._server is not None:
            try:
                self._server.save_model(name, self._key or None)
            except ModelRefused as e:
                if self.closed:
                    return None
                return (
                    self._key_form(str(e)) if e.retry == "key" else self._model_form(model, str(e))
                )
            except (MediaError, httpx.HTTPError) as e:
                return None if self.closed else self._model_form(model, _error(e))
            return None
        try:
            refusal = check_model(credentials_for(name, self._key), on=self._stop_if_closed)
        except _Closed:
            return None
        if self.closed:
            return None
        if refusal is not None:
            return (
                self._key_form(refusal.text)
                if refusal.key
                else self._model_form(model, refusal.text)
            )
        save_model(name, self._key)
        return None

    def submit(self, values: dict[str, str], *, force: bool) -> FormAsk | None:
        if self._step == "provider":
            return self._choose_provider(values)
        if self._step == "key":
            try:
                models, best = self._check_key(values.get("key", "").strip())
            except (MediaError, httpx.HTTPError) as e:
                return self._key_form(_error(e))
            if best is None:
                return self._provider_form(
                    _("The provider returned no models. Choose another provider")
                )
            self._models = models
            return self._model_form(best)
        best = providers.recommended(self._provider, self._models)
        model = values.get("model", "")
        if model not in {m.id for m in self._models}:
            model = best or ""
        return self._save(providers.litellm_model(self._provider, model))


def _error(exc: Exception) -> str:
    """A refusal as the server or the provider said it; no answer from the server in words."""
    from ..install_setup import NO_ANSWER

    return str(exc) if isinstance(exc, MediaError) else _setup_text(NO_ANSWER)


class KeyFlow:
    """A window with one key: the OpenAI key for cloud recognition (kind "asr"), kept by the
    connected server or, without one, in this Mac's .env; the DeepL key for live translation
    (kind "deepl"), on the server only. The check is the server's or providers.check_key; the
    key lives only in the window field and in redact."""

    checks = True

    def __init__(self, kind: Literal["asr", "deepl"], server: Connection | None) -> None:
        self.closed = False
        self._kind = kind
        self._server = ServerModel(server) if server is not None else None

    def _form(self, error: str = "") -> FormAsk:
        if self._kind == "asr":
            title, provider, page = _("OpenAI key for recognition"), "OpenAI", _OPENAI_KEYS
        else:
            title, provider, page = _("DeepL key"), "DeepL", _DEEPL_KEYS
        return FormAsk(
            title=title,
            message=_("Get a {provider} key: {page}", provider=provider, page=page),
            fields=[FormField(id="key", label=_("API key"))],
            error=error,
            ok=_("Check and save"),
            clear=["key"] if error else [],
        )

    def ask(self) -> FormAsk:
        return self._form()

    def submit(self, values: dict[str, str], *, force: bool) -> FormAsk | None:
        from ..config import set_env
        from ..llm.redact import add_secret

        key = values.get("key", "").strip()
        if not key:
            return self._form(_("A key is required"))
        add_secret(key)
        try:
            if self._server is None:
                providers.check_key(Provider.OPENAI, key)
            elif self._kind == "asr":
                self._server.save_asr_key(key)
            else:
                self._server.save_deepl_key(key)
        except (MediaError, httpx.HTTPError) as e:
            return None if self.closed else self._form(_error(e))
        if self.closed:
            return None
        if self._server is None:
            try:
                set_env({"MNEMA_ASR_API_KEY": key})
            except OSError as e:
                return self._form(_("Could not save: {error}", error=e.strerror or e))
        return None
