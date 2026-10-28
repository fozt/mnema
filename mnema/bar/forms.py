"""Windows with a check, as pure logic without threads: the server connection (macOS and Linux
app) and the summary model (Mac only).

ask() opens the window; submit() is "Next" or "Check and save" (force is "Save anyway") and
returns the next window state, or None when saved and the window closes. An error keeps the window
open with a red line and keeps what was typed. The window state never holds secrets.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..core.base import MediaError
from ..i18n import _
from ..llm import providers
from ..llm.providers import KEY_PAGES, TITLES, ModelInfo, Provider
from .protocol import Choice, FormAsk, FormField, FormFieldKind, FormOp

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
    field, here until the window closes, and in redact; the window state shows only "Keep …a1B2"."""

    def __init__(self) -> None:
        from ..llm.config import LLMSettings

        self.closed = False
        found = providers.provider_of(LLMSettings().model or "")
        self._provider = found[0] if found else Provider.GEMINI
        self._step = "provider"
        self._key = ""
        self._models: list[ModelInfo] = []

    @property
    def checks(self) -> bool:
        """The Provider step needs no network."""
        return self._step != "provider"

    def ask(self) -> FormAsk:
        return self._provider_form()

    def _form(self, field: FormField, message: str, **kw) -> FormAsk:
        return FormAsk(title=_("Summary model"), message=message, fields=[field], **kw)

    def _provider_form(self, error: str = "") -> FormAsk:
        from ..llm.config import current_line

        self._step = "provider"
        field = FormField(
            id="provider",
            label=_("Provider"),
            kind=FormFieldKind.CHOICE,
            choices=[
                Choice(key=p.value, title=TITLES[p], checked=p == self._provider) for p in Provider
            ],
        )
        return self._form(field, current_line(), ok=_("Next"), error=error)

    def _key_form(self, error: str = "") -> FormAsk:
        """A refused key comes back with the error and a cleared field."""
        from ..llm.config import LLMSettings
        from ..llm.redact import mask

        self._step = "key"
        saved = LLMSettings().provider_key(self._provider)
        field = FormField(
            id="key",
            label=_("API key"),
            placeholder=_("Keep {key}", key=mask(saved)) if saved else "",
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

    def submit(self, values: dict[str, str], *, force: bool) -> FormAsk | None:
        from ..llm.claude import check_model
        from ..llm.config import LLMSettings, credentials_for, save_model

        if self._step == "provider":
            try:
                self._provider = Provider(values.get("provider", ""))
            except ValueError:
                pass  # not in the list: keep the previous choice
            return self._key_form()
        if self._step == "key":
            typed = values.get("key", "").strip()
            key = typed or LLMSettings().provider_key(self._provider) or ""
            try:
                models = providers.check_key(self._provider, key)
            except MediaError as e:
                return self._key_form(str(e))
            best = providers.recommended(self._provider, models)
            if best is None:
                return self._provider_form(
                    _("The provider returned no models. Choose another provider")
                )
            self._key, self._models = key, models
            return self._model_form(best)
        best = providers.recommended(self._provider, self._models)
        model = values.get("model", "")
        if model not in {m.id for m in self._models}:
            model = best or ""
        name = providers.litellm_model(self._provider, model)
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
