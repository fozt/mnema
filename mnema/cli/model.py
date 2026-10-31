"""`mnema model`: the cloud model of this machine in four steps — provider, key, model, check.

Writes MNEMA_MODEL and MNEMA_API_KEY lines to ~/.config/mnema/.env, which `mnema serve` reads
on every request; with --user, to the user's own file ~/.config/mnema/users/<name>.env. The
key is never printed in full; Ctrl-C exits without changes. The step rules are shared with the
Mac model window (providers.check_key, claude.check_model); this module is only terminal input
and output.
"""

from __future__ import annotations

import getpass
import os
from pathlib import Path

from rich.text import Text

import mnema.cli as _cli

from .. import users
from ..core.base import MediaError
from ..i18n import _
from ..llm import providers
from ..llm.config import MODEL_GROUP, Credentials, LLMSettings, credentials_for, current_line
from ..llm.errors import Refusal
from ..llm.providers import TITLES, ModelInfo, Provider
from ..llm.redact import add_secret, mask
from .ui import die

_PROVIDERS = list(Provider)


def _say(text: str, style: str = "") -> None:
    _cli.console.print(Text(text, style=style), soft_wrap=True)


def _provider(current: Provider | None) -> Provider:
    _say(_("1. Provider"), "bold")
    for number, provider in enumerate(_PROVIDERS, 1):
        _say(f"  {number}. {TITLES[provider]}")
    default = f" [{TITLES[current]}]" if current else ""
    while True:
        answer = input(_("Number{default}: ", default=default)).strip()
        if not answer and current is not None:
            return current
        if answer.isdigit() and 1 <= int(answer) <= len(_PROVIDERS):
            return _PROVIDERS[int(answer) - 1]
        _say(_("Enter a number from the list"), "yellow")


def _key(provider: Provider, saved: str | None) -> tuple[str, list[ModelInfo]]:
    """The key and its models: the provider's model list is the check (GET only)."""
    _say(_("2. Key"), "bold")
    keep = _(" [keep {key}]", key=mask(saved)) if saved else ""
    while True:
        prompt = _("{provider} key{keep}: ", provider=TITLES[provider], keep=keep)
        key = getpass.getpass(prompt).strip() or saved
        if not key:
            _say(_("A key is required"), "yellow")
            continue
        add_secret(key)
        problem = providers.key_problem(provider, key)
        if problem:  # found without a request: no "Checking the key…" step
            _say(problem, "red")
            continue
        try:
            with _cli.console.status(_("Checking the key…")):
                return key, providers.check_key(provider, key)
        except MediaError as e:
            _say(str(e), "red")


def _model(provider: Provider, models: list[ModelInfo], current: str | None) -> str:
    """A model number or name; Enter takes the recommended one."""
    _say(_("3. Model"), "bold")
    best = providers.recommended(provider, models)
    for number, m in enumerate(models, 1):
        marks = [mark for mark, on in ((_("free"), m.free), (_("selected"), m.id == current)) if on]
        _say(f"  {number}. {m.id}" + "".join(f" · {mark}" for mark in marks))
    if not models:
        _say(_("The provider gave no model list. Enter a model name"))
    default = f" [{best}]" if best else ""
    while True:
        answer = input(_("Number or name{default}: ", default=default)).strip()
        if not answer and best:
            return best
        if answer.isdigit() and models:
            if 1 <= int(answer) <= len(models):
                return models[int(answer) - 1].id
            _say(_("No such number"), "yellow")
            continue
        if answer:
            found = providers.provider_of(answer)
            return found[1] if found is not None and found[0] == provider else answer
        _say(_("Enter a model number or name"), "yellow")


def _works(creds: Credentials) -> Refusal | None:
    """Try the model: None when it answers, otherwise the refusal."""
    from ..llm import claude

    _say(_("4. Check"), "bold")
    with _cli.console.status(_("Waiting for the model to answer…")) as status:
        return claude.check_model(creds, on=lambda _fraction, message: status.update(message))


def _overridden() -> None:
    """MNEMA_MODEL in the process environment (export, service): model, key and address come
    from there."""
    if os.environ.get("MNEMA_MODEL"):
        names = ", ".join(name for name in MODEL_GROUP if os.environ.get(name))
        _say(
            _(
                "{names} set in the process environment: the model comes from there, "
                "not from the file",
                names=names,
            ),
            "yellow",
        )


def _server_user(raw: str | None) -> str | None:
    """The user of --user on this computer's server; None — the shared model."""
    from ..devices import DeviceStore, devices_db
    from ..install_setup import server_root

    if raw is None or raw.lower() == users.OWNER:
        return None
    try:
        name = users.valid_name(raw)
    except MediaError as e:
        die(str(e))
        return None
    db = devices_db(server_root())
    found = DeviceStore(db).user(name) if db.is_file() else None
    if found is None or found.removed:
        die(users.no_user(name))
    return name


def _file(user: str | None) -> Path:
    from .. import config

    return config.user_env_file() if user is None else users.user_env_file(user)


def _configure(user: str | None) -> None:
    from ..llm.config import save_model

    _say(current_line(user=user))
    with users.as_user(user):
        settings = LLMSettings()
    # a user's choice starts from her own file only, never from the shared model and key
    mine = user is None or settings.source == "user"
    found = providers.provider_of(settings.model or "") if mine else None
    provider = _provider(found[0] if found else None)
    # a gateway key is never offered to a provider
    saved = settings.provider_key(provider) if mine else None
    current = found[1] if found is not None and found[0] == provider else None
    key, models = _key(provider, saved)
    while True:
        model = _model(provider, models, current)
        name = providers.litellm_model(provider, model)
        refusal = _works(credentials_for(name, key))
        if refusal is None:
            break
        _say(refusal.text, "red")
        if refusal.key:
            key, models = _key(provider, saved)
    _say(_("The model answers"), "green")
    save_model(name, key, user=user)
    _say(_("Saved to {path}: {model}, key {key}", path=_file(user), model=name, key=mask(key)))
    _say(_("The server picks up the model without a restart"))
    if user is None:
        _overridden()


def cmd_model(args) -> None:
    """`mnema model`: provider, key, model, check; --show prints the current one; --remove
    removes it. --user: the own model of a server user, --remove puts her on the shared one."""
    user = _server_user(args.user)
    if user is not None:
        os.umask(0o077)  # docker exec does not inherit the server's umask
    if args.show:
        _say(current_line(user=user))
        return
    if args.remove:
        from ..llm.config import remove_model

        removed = remove_model(user=user)
        path = _file(user)
        _say(
            _("Model removed from {path}", path=path)
            if removed
            else _("No model in {path}", path=path)
        )
        if user is None:
            _overridden()
        return
    _configure(user)
