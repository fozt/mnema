"""Mnema settings as one model for the macOS and Linux windows: shared labels and availability."""

from __future__ import annotations

import re

from .. import __version__
from ..i18n import _, current
from .protocol import (
    Choice,
    FieldKind,
    Role,
    SettingsField,
    SettingsModel,
    SettingsSection,
    SettingsTab,
    Tone,
)
from .state import (
    BarPrefs,
    MenuEnv,
    Recognition,
    asr_options,
    discreet_title,
    update_line,
)

# the runtime keeps one of these as hotkey_error; settings_model translates it when shown
HOTKEY_BUSY = "Shortcut is in use"
HOTKEY_INVALID = "Add ⌃, ⌥ or ⌘"


def host(url: str) -> str:
    """Server address without the scheme: "srv:8000"."""
    return re.sub(r"^https?://", "", url).rstrip("/")


def server_label(server: str | None, name: str | None = None) -> str:
    """The server by the name from its capabilities, else by its address."""
    return name or (host(server) if server else _("Server"))


def _lower(text: str) -> str:
    """Russian shows the chosen option in lower case, except a name with inner capitals
    ("MacBook Pro"); English keeps the option's own case."""
    if current() != "ru":
        return text
    word = text.split(" ", 1)[0]
    return text if word[1:] != word[1:].lower() else text[:1].lower() + text[1:]


def _toggle(title: str, key: str, checked: bool) -> SettingsField:
    return SettingsField(kind=FieldKind.TOGGLE, title=title, key=key, checked=checked)


def _choice(
    title: str, choices: list[Choice], *, value: str | None = None, enabled: bool = True
) -> SettingsField:
    """A choice; value is the chosen option in short (by default its title, see _lower)."""
    if value is None:
        value = _lower(next((c.title for c in choices if c.checked), ""))
    return SettingsField(
        kind=FieldKind.CHOICE, title=title, value=value, enabled=enabled, choices=choices
    )


def here(mac: bool, *, start: bool = False) -> str:
    """This machine in the user's words, as macOS calls itself "this Mac"; start — at the start
    of a sentence."""
    if mac:
        return _("On this Mac") if start else _("on this Mac")
    return _("On this computer") if start else _("on this computer")


def recognition_at(prefs: BarPrefs, env: MenuEnv) -> Recognition:
    """The place that recognizes: on a Mac, "On the server" without a server is this Mac (the
    rule of node.selected_processing)."""
    if env.mac and prefs.recognition == Recognition.SERVER and env.server is None:
        return Recognition.MAC
    return prefs.recognition


def _recognition_choice(prefs: BarPrefs, env: MenuEnv) -> SettingsField:
    """On this Mac (Mac only), on the server (unavailable without one) or in the cloud."""
    place = recognition_at(prefs, env)
    choices = [
        Choice(
            key=f"recognition:{Recognition.SERVER}",
            title=_("On the server"),  # a Mac has one server: its name is not needed here
            checked=place == Recognition.SERVER,
            enabled=env.server is not None or not env.mac,
            detail="" if env.server is not None or not env.mac else _("Server not connected"),
        ),
        Choice(
            key=f"recognition:{Recognition.CLOUD}",
            title=_("In the cloud"),
            checked=place == Recognition.CLOUD,
        ),
    ]
    if env.mac:
        here = Choice(
            key=f"recognition:{Recognition.MAC}",
            title=_("On this Mac"),
            checked=place == Recognition.MAC,
        )
        choices.insert(0, here)
    return _choice(_("Recognition"), choices)


def _model_rows(env: MenuEnv) -> list[SettingsField]:
    """Mac: the model of the place where it lives, the user's on the connected server or this
    Mac's own; "Choose model…" sets it there. The row above "Write summaries" tells why it is
    unavailable."""
    f = SettingsField
    if env.server is not None:
        # "Not set up" only when the server answered without a model; a server that has not
        # answered yet gets an empty value
        if env.server_model:
            value, tone = env.server_model, Tone.OK
        elif env.server_ok and not env.server_llm:
            value, tone = _("Not set up"), Tone.WARN
        else:
            value, tone = "", Tone.NONE
    else:
        value, tone = env.model or _("Not set up"), Tone.OK if env.model else Tone.WARN
    return [
        f(kind=FieldKind.STATUS, title=_("Model"), value=value, tone=tone),
        f(kind=FieldKind.BUTTON, title=_("Choose model…"), key="model_setup"),
    ]


def _asr_key_rows(env: MenuEnv) -> list[SettingsField]:
    """Mac in the cloud: the OpenAI key masked and the button that sets or replaces it."""
    f = SettingsField
    if env.asr_key is None:
        return [f(kind=FieldKind.BUTTON, title=_("Set the OpenAI key…"), key="asr_key_setup")]
    return [
        f(kind=FieldKind.STATUS, title=_("OpenAI key"), value=env.asr_key),
        f(kind=FieldKind.BUTTON, title=_("Change the OpenAI key…"), key="asr_key_setup"),
    ]


def _translation_rows(env: MenuEnv) -> list[SettingsField]:
    """Mac with a server: live translation by the user's DeepL key or by the model."""
    f = SettingsField
    if not env.mac or env.server is None or not env.server_auth:
        return []
    return [
        f(
            kind=FieldKind.STATUS,
            title=_("Live translation"),
            value="DeepL" if env.server_deepl else _("By the model"),
        ),
        f(kind=FieldKind.BUTTON, title=_("Set the DeepL key…"), key="deepl_setup"),
    ]


def _server_rows(env: MenuEnv) -> list[SettingsField]:
    """Connection: the server by name (the address below it) and its state, the recordings of
    the last sync with it, "Connect…" or "Connect another…", "Disconnect"."""
    f = SettingsField
    if env.server is None:
        rows = [
            f(
                kind=FieldKind.STATUS,
                title=_("Server"),
                value=_("Not connected"),
                tone=Tone.NONE if env.mac else Tone.WARN,  # Linux cannot process without one
            )
        ]
    else:
        if not env.server_auth:
            state, tone = _("New invitation needed"), Tone.ERROR
        elif env.server_ok:
            state, tone = _("Connected"), Tone.OK
        else:
            state, tone = _("Unavailable"), Tone.WARN
        rows = [
            f(
                kind=FieldKind.STATUS,
                title=server_label(env.server, env.server_name),
                value=state,
                detail=host(env.server) if env.server_name else "",
                tone=tone,
            )
        ]
    if env.server_records is not None:
        rows.append(
            f(
                kind=FieldKind.STATUS,
                title=_("Recordings on the server"),
                value=str(env.server_records),
            )
        )
    connected = env.server is not None and env.server_auth
    rows.append(
        f(
            kind=FieldKind.BUTTON,
            title=_("Connect another…") if connected else _("Connect…"),
            key="server_setup",
        )
    )
    if env.server is not None:
        rows.append(
            f(
                kind=FieldKind.BUTTON,
                title=_("Disconnect"),
                key="server_off",
                role=Role.DESTRUCTIVE,
            )
        )
    return rows


def _update_status(env: MenuEnv) -> SettingsField:
    """The Mnema version row: what is happening with updates now, not when it was checked."""
    status = SettingsField(kind=FieldKind.STATUS, title=f"Mnema {__version__}")
    if env.update_step:  # the download has a bar; the steps after it a spinner
        status.value, status.progress = update_line(env), env.update_progress
        status.tone = Tone.NONE if env.update_progress is not None else Tone.BUSY
    elif env.update_checking:
        status.value, status.tone = _("Checking…"), Tone.BUSY
    elif env.update_error:
        status.value = _("Could not update") if env.update else _("Could not check")
        status.detail = env.update_error
    elif env.update:
        status.value = _("Version {version} available", version=env.update)
        status.tone = Tone.WARN
    elif env.update_checked:
        status.value = _("Latest version")
    return status


def settings_model(
    prefs: BarPrefs,
    env: MenuEnv,
    *,
    hotkey_error: str = "",
    idle: bool = True,
) -> SettingsModel:
    """All settings on the Recording, Server and General tabs. hotkey_error is the English
    source text of the shortcut field's error. Unless idle (nothing records or processes), the
    Recording tab (except the Model row) and the server buttons are unavailable, and handle
    does not change them."""
    f, c = SettingsField, Choice
    summary_lang = prefs.summary_lang
    # the user's model on the server summarizes when one is connected; a server that has not
    # answered yet keeps the summary on and lets the processing ask
    if env.mac and env.server is None:
        summary_ready = env.model is not None
    else:
        summary_ready = env.server_llm or (env.mac and not env.server_ok)
    options = asr_options(env, prefs.asr)
    processing = [
        _recognition_choice(prefs, env),
        _choice(
            _("Engine"),
            [
                c(key=f"asr:{value}", title=title, checked=value == prefs.asr)
                for value, title in options
            ],
            value=dict(options).get(prefs.asr, prefs.asr),
        ),
    ]
    if env.mac and recognition_at(prefs, env) == Recognition.CLOUD:
        processing += _asr_key_rows(env)
    if env.mac:
        processing += _model_rows(env)
    processing += [
        f(
            kind=FieldKind.TOGGLE,
            title=_("Write summaries"),
            key="summary",
            checked=prefs.summary and summary_ready,
            enabled=summary_ready,
            detail="" if summary_ready or env.mac else _("Model not set up"),
        ),
        _choice(
            _("Summary language"),
            [
                c(
                    key="summary_lang:auto",
                    title=_("Same as the conversation"),
                    checked=summary_lang is None,
                ),
                c(key="summary_lang:ru", title=_("Russian"), checked=summary_lang == "ru"),
                c(key="summary_lang:en", title=_("English"), checked=summary_lang == "en"),
            ],
            enabled=prefs.summary and summary_ready,
        ),
    ]
    record = SettingsTab(
        id="record",
        title=_("Recording"),
        symbol="mic",
        sections=[
            SettingsSection(
                title=_("Audio sources"),
                fields=[
                    _toggle(_("Microphone"), "toggle_mic", prefs.mic),
                    _toggle(_("System audio"), "toggle_system", prefs.system),
                ],
            ),
            SettingsSection(title=_("Processing"), fields=processing),
        ],
    )
    server = _server_rows(env)
    translation = _translation_rows(env)
    if not idle:  # next recording's settings and the connection are locked while busy
        locked = [x for s in record.sections for x in s.fields if x.kind != FieldKind.STATUS]
        buttons = [x for x in (*server, *translation) if x.kind == FieldKind.BUTTON]
        for field in [*locked, *buttons]:
            field.enabled = False
            for item in field.choices:
                item.enabled = False
    general = [
        _toggle(discreet_title(env.mac), "discreet", prefs.discreet),
        _toggle(_("Open at login"), "autostart", prefs.autostart),
    ]
    if env.mac:  # the macOS app registers the shortcut; on Linux it belongs to GNOME settings
        hotkey = f(
            kind=FieldKind.HOTKEY,
            title=_("Start or stop recording"),
            key="hotkey",
            value=prefs.hotkey or "",
            placeholder=_("Press a shortcut"),
            error=_(hotkey_error) if hotkey_error else "",
        )
        general = [hotkey, *general, f(kind=FieldKind.BUTTON, title=_("Quit Mnema"), key="quit")]
    updates = [
        _update_status(env),
        f(
            kind=FieldKind.BUTTON,
            title=_("Check again") if env.update_error else _("Check for updates"),
            key="update_check",
            enabled=not (env.update_step or env.update_checking),
        ),
    ]
    if env.update:
        updates.append(
            f(
                kind=FieldKind.BUTTON,
                title=_("Update"),
                key="update",
                enabled=idle and not (env.update_step or env.update_checking),
            )
        )
    return SettingsModel(
        tabs=[
            record,
            SettingsTab(
                id="server",
                title=_("Server"),
                symbol="server.rack",
                sections=[
                    SettingsSection(title=_("Connection"), fields=server),
                    *([SettingsSection(fields=translation)] if translation else []),
                ],
            ),
            SettingsTab(
                id="general",
                title=_("General"),
                symbol="gearshape",
                sections=[
                    SettingsSection(fields=general),
                    SettingsSection(title=_("Updates"), fields=updates),
                ],
            ),
        ]
    )
