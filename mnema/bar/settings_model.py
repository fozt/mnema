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
from .state import BarPrefs, MenuEnv, Where, asr_options, discreet_title

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


def on_server(prefs: BarPrefs, env: MenuEnv) -> bool:
    """The Mac processes on the server when "On the server" is chosen and a server is connected
    (the rule of node.selected_processing); Linux always does."""
    if not env.mac:
        return True
    return prefs.where == Where.SERVER and env.server is not None


def _where_choice(prefs: BarPrefs, env: MenuEnv) -> SettingsField:
    """Mac: process on this Mac or on the connected server; without a server the second option
    is unavailable."""
    there = on_server(prefs, env)
    return _choice(
        _("Where to process"),
        [
            Choice(key=f"where:{Where.MAC}", title=_("On this Mac"), checked=not there),
            Choice(
                key=f"where:{Where.SERVER}",
                title=_("On the server"),  # a Mac has one server: its name is not needed here
                checked=there,
                enabled=env.server is not None,
                detail="" if env.server is not None else _("Server not connected"),
            ),
        ],
    )


def _model_rows(prefs: BarPrefs, env: MenuEnv) -> list[SettingsField]:
    """Mac: the model of the place that processes; the row above "Write summaries" tells why it is
    unavailable. "Choose model…" exists only for this Mac's own model."""
    f = SettingsField
    if on_server(prefs, env):
        # "Not set up" only when the server answered without a model; a server that summarizes
        # but did not name its model, or has not answered yet, gets an empty value
        if env.server_llm:
            value, tone = env.server_model or "", Tone.OK
        elif env.server_ok:
            value, tone = _("Not set up on the server"), Tone.WARN
        else:
            value, tone = "", Tone.NONE
        return [f(kind=FieldKind.STATUS, title=_("Model"), value=value, tone=tone)]
    return [
        f(
            kind=FieldKind.STATUS,
            title=_("Model"),
            value=env.model or _("Not set up"),
            tone=Tone.OK if env.model else Tone.WARN,
        ),
        f(kind=FieldKind.BUTTON, title=_("Choose model…"), key="model_setup"),
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
    # the model of the place that processes summarizes; a Mac whose server has not answered yet
    # keeps the summary on and lets the processing ask
    if on_server(prefs, env):
        summary_ready = env.server_llm or (env.mac and not env.server_ok)
    else:
        summary_ready = env.model is not None
    options = asr_options(env, prefs.asr)
    processing = [_where_choice(prefs, env)] if env.mac else []
    processing += [
        _choice(
            _("Recognition"),
            [
                c(key=f"asr:{value}", title=title, checked=value == prefs.asr)
                for value, title in options
            ],
            value=dict(options).get(prefs.asr, prefs.asr),
        ),
    ]
    if env.mac:
        processing += _model_rows(prefs, env)
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
    if not idle:  # next recording's settings and the connection are locked while busy
        locked = [x for s in record.sections for x in s.fields if x.kind != FieldKind.STATUS]
        for field in [*locked, *(x for x in server if x.kind == FieldKind.BUTTON)]:
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
        f(
            kind=FieldKind.STATUS,
            title=f"Mnema {__version__}",
            value=env.update_step
            or (_("Could not check") if env.update_error else "")
            or (_("Update available") if env.update else "")
            or (_("Up to date") if env.update_checked else ""),
            detail=(env.update_error or env.update) if not env.update_step else "",
            tone=Tone.BUSY if env.update_step else Tone.WARN if env.update else Tone.NONE,
        ),
        f(
            kind=FieldKind.BUTTON,
            title=_("Check for updates"),
            key="update_check",
            enabled=not env.update_step,
        ),
    ]
    if env.update:
        updates.append(
            f(
                kind=FieldKind.BUTTON,
                title=_("Update"),
                key="update",
                enabled=idle and not env.update_step,
            )
        )
    return SettingsModel(
        tabs=[
            record,
            SettingsTab(
                id="server",
                title=_("Server"),
                symbol="server.rack",
                sections=[SettingsSection(title=_("Connection"), fields=server)],
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
