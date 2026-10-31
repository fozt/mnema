"""The macOS app panel (PanelModel): what a click on the icon shows, as finished strings. A pure
function of the bar state, the settings and the recordings feed: times, dates and percentages are
formatted here, Swift only lays them out."""

from __future__ import annotations

import re
import textwrap
from datetime import datetime, timedelta

from ..connection import VersionGap, version_gap
from ..i18n import _, _n, fmt_bar_panel
from ..live.meta import UNFINISHED, RecordState
from .protocol import (
    Button,
    Card,
    Choice,
    FieldKind,
    IdleView,
    Notice,
    PanelModel,
    RecordingView,
    Role,
    SettingsField,
    SettingsModel,
    Source,
    StatusItem,
)
from .recordings import RecordingCard
from .settings_model import server_label
from .state import (
    MOVE_TO_SECTION,
    NEW_SECTION,
    NO_SECTION,
    NO_SUMMARY,
    OPEN_FOLDER,
    OPEN_SUMMARY,
    OPEN_TEXT,
    QUEUED,
    QUIT,
    SET_TITLE,
    STOP_PROCESSING,
    SUMMARIZE,
    TRASH,
    UNQUEUE,
    BarPrefs,
    BarState,
    Job,
    MenuEnv,
    Phase,
    SourceMeter,
    can_relang,
    fmt_elapsed,
    language_choices,
    language_label,
    languages_label,
    processing_step,
    record_section,
    source_name,
    step_line,
    title,
    update_line,
)

_SYMBOLS = {
    "mic": ("mic.fill", "mic.slash.fill"),
    "system": ("speaker.wave.2.fill", "speaker.slash.fill"),
}
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s")


def _when(t: datetime, now: datetime, *, clock: bool = True) -> str:
    """A day with time: "Today, 9:15", "Yesterday, 9:15", "Sep 28, 9:15", another year
    "Sep 28, 2025, 9:15"; clock=False (the time is unknown) — the day alone."""
    if t.date() == now.date():
        day = _("Today")
    elif t.date() == (now - timedelta(days=1)).date():
        day = _("Yesterday")
    else:
        day = fmt_bar_panel.short_date(t, year=t.year != now.year)
    return f"{day}, {t.hour}:{t.minute:02d}" if clock else day


def _length(seconds: float) -> str:
    """A length: "1 h 12 min", "2 h", "14 min", "40 s"."""
    s = int(seconds)
    if s < 60:
        return _("{n} s", n=s)
    h, m = divmod(s // 60, 60)
    if not h:
        return _("{n} min", n=m)
    return _("{h} h {m} min", h=h, m=m) if m else _("{h} h", h=h)


def _source(label: str, *, on: bool, key: str) -> Source:
    symbol, symbol_off = _SYMBOLS.get(label, _SYMBOLS["mic"])
    return Source(
        label=label, title=source_name(label), symbol=symbol, symbol_off=symbol_off, on=on, key=key
    )


def _live_source(m: SourceMeter) -> Source:
    """A source while recording: its level and whether it is off."""
    source = _source(m.label, on=not m.muted, key=f"source:{m.label}")
    source.levels = [round(v, 3) for v in m.history]
    source.status = _("Off") if m.muted else ""
    return source


def _notice(text: str, limit: int = 400) -> str:
    """A notice starts with a capital; it wraps lines, a very long text is cut on a word."""
    text = text[:1].upper() + text[1:]
    return text if len(text) <= limit else textwrap.shorten(text, limit, placeholder="…")


def _after_colon(text: str) -> str:
    """The text after "Sync:" starts in lower case; an abbreviation ("HTTP …") stays."""
    return text[:1].lower() + text[1:] if text[1:2].islower() else text


def _first_sentence(remark: str) -> str:
    line = _SENTENCE_END.split(remark.strip().splitlines()[0], maxsplit=1)[0]
    return re.sub(r"[,;:]…$", "…", textwrap.shorten(line.removesuffix("."), 90, placeholder="…"))


def _missing_on_server(count: int, *, mac: bool) -> tuple[str, str, str]:
    """Sync found recordings gone on the server: the notice and its two buttons."""
    if mac:
        return (
            _("Recordings missing on the server: {n}. They are kept on this Mac.", n=count),
            _("Delete on this Mac"),
            _("Restore to the server"),
        )
    return (
        _("Recordings missing on the server: {n}. They are kept on this computer.", n=count),
        _("Delete on this computer"),
        _("Restore to the server"),
    )


def _missing_here(count: int, *, mac: bool) -> tuple[str, str, str]:
    """Sync found recordings gone here but kept on the server."""
    text = (
        _("Recordings missing on this Mac: {n}. They are kept on the server.", n=count)
        if mac
        else _("Recordings missing on this computer: {n}. They are kept on the server.", n=count)
    )
    return text, _("Delete everywhere"), _("Restore from the server")


def _other_library(count: int, *, mac: bool) -> str:
    """Sync found another library at the same server: nothing changes until the answer."""
    if mac:
        return _n(
            "The server has another recordings library. Send {n} recording from this Mac to it?",
            "The server has another recordings library. Send {n} recordings from this Mac to it?",
            count,
        )
    return _n(
        "The server has another recordings library. Send {n} recording from this computer to it?",
        "The server has another recordings library. Send {n} recordings from this computer to it?",
        count,
    )


def _move_menu(c: RecordingCard, sections: list[str], *, enabled: bool) -> Button:
    """The "Move to section" submenu: the root, the library sections, after a separator "New
    section…"; the recording's own section is checked and does nothing."""
    choices = [
        Button(
            key=f"record:{c.id}:move:{name}",
            title=name or _(NO_SECTION),
            checked=name == c.section,
        )
        for name in ("", *sections)
    ]
    choices.append(Button(key=f"record:{c.id}:new_section", title=_(NEW_SECTION), divider=True))
    return Button(
        key=f"record:{c.id}:move", title=_(MOVE_TO_SECTION), enabled=enabled, children=choices
    )


def _card(
    c: RecordingCard,
    *,
    remarks: list[str],
    now: datetime,
    unreachable: str | None = None,
    sections: list[str] | None = None,
    job: Job | None = None,
) -> Card:
    """A click opens the recording's folder, the rest is the context menu; the detail line holds
    one button when the recording waits for an action. unreachable — the server that did not
    answer this recording's processing: Retry and Choose server, processing never moves by itself.
    job — the recording in the processing queue: running shows the step and a bar, waiting
    "Queued"."""
    when = _when(c.started, now, clock=c.time_known)
    length = _length(c.seconds) if c.seconds else ""

    def key(action: str) -> str:
        return f"record:{c.id}:{action}"

    def item(action: str, title: str, *, divider: bool = False) -> Button:
        """Available while another recording runs or is processed: processing goes to the queue."""
        return Button(key=key(action), title=title, divider=divider)

    opens = [item("summary", _(OPEN_SUMMARY))] if c.has_summary else []
    if c.has_text:
        opens.append(item("transcript", _(OPEN_TEXT)))
    opens.append(item("folder", _(OPEN_FOLDER)))
    rename = item("rename", _("Rename…"))
    # a recording being processed (here or by another process) is not moved
    move = _move_menu(c, sections or [], enabled=c.state is not RecordState.PROCESSING)
    trash = Button(key=key("trash"), title=_(TRASH), role=Role.DESTRUCTIVE, divider=True)
    redo = c.has_audio and c.has_sound
    inline: list[Button] = []
    if c.has_sound and (c.state in UNFINISHED or c.state is RecordState.AUDIO_ONLY):
        # "Not processed" as in the menu (unfinished_sessions); a stopped one has audio only
        status = _("Not transcribed") if c.state is RecordState.AUDIO_ONLY else _("Not processed")
        detail, detail_warn = status, True
        process = item("process", _("Process…"), divider=True)
        inline.append(process.model_copy(update={"divider": False}))
        menu = [*opens, process, rename, move, trash]
    else:
        status = "" if c.has_text or c.has_summary else _("No speech")
        rename.divider = True
        menu = [*opens, rename, move]
        if c.has_text and not c.has_summary:  # the model failed or the summary was off
            menu.append(item("summarize", _(SUMMARIZE)))
        if redo:
            menu.append(item("process", _("Process again…")))
        menu.append(trash)
        if status:
            detail, detail_warn = status, False
        elif c.gist:
            detail, detail_warn = c.gist, False
        elif c.has_text and not c.has_summary:  # why there is no summary — in the tooltip
            detail, detail_warn = _(NO_SUMMARY), bool(remarks)
        else:
            detail, detail_warn = "", False
    if unreachable is not None:
        status = detail = _("{server} is unavailable", server=unreachable)
        detail_warn = True
        remarks = [r for r in remarks if r != status]  # the same remark is already the status
        retry = item("retry", _("Retry"))
        choose = Button(key="choose_server", title=_("Choose server…"))  # settings open any time
        inline = [retry]
        menu[0] = menu[0].model_copy(update={"divider": True})
        menu = [retry, choose, *menu]
    busy, progress = False, None
    if job is not None:
        detail, detail_warn, status, inline = _(QUEUED), False, _(QUEUED), []
        if job.running:
            busy, progress = True, processing_step(job)[1]
            detail = status = step_line(job)
            menu = _running_menu(c, job)
        else:
            unqueue = Button(key=key("stop"), title=_(UNQUEUE))
            menu = [*opens, rename.model_copy(update={"divider": True}), move, unqueue, trash]
    # the language of the last processing, for reference
    tooltip = [c.gist, *remarks, languages_label(c.languages)]
    return Card(
        id=c.id,
        section=c.section,
        title=c.title or when + c.part,
        subtitle=" · ".join(part for part in (when if c.title else "", length) if part),
        gist=c.gist,
        status=status,
        warn=bool(remarks),
        remarks=remarks,
        open_key=key("folder"),
        buttons=inline,
        menu=menu,
        detail=detail,
        detail_warn=detail_warn,
        tooltip="\n".join(line for line in tooltip if line),
        busy=busy,
        progress=progress,
    )


def _running_menu(c: RecordingCard, job: Job) -> list[Button]:
    """The menu of a recording being processed: open what is ready; language and own title (not
    during the summary); stop."""

    def key(action: str) -> str:
        return f"record:{c.id}:{action}"

    menu: list[Button] = []
    if job.transcript_ready is not None:  # the new transcript is ready (it replaces the old one)
        menu.append(Button(key=key("transcript"), title=_(OPEN_TEXT)))
    menu.append(Button(key=key("folder"), title=_(OPEN_FOLDER)))
    if can_relang(job):  # another language redoes the recording strictly in it
        applied = job.applied_languages
        choices = [
            Button(key=key(f"lang:{k}"), title=t, checked=applied == {k})
            for k, t in language_choices()
        ]
        menu.append(
            Button(key=key("lang"), title=language_label(job), divider=True, children=choices)
        )
        menu.append(Button(key=key("title"), title=_(SET_TITLE)))
    menu.append(Button(key=key("stop"), title=_(STOP_PROCESSING), divider=True))
    return menu


def _section(prefs: BarPrefs, sections: list[str]) -> SettingsField | None:
    """The section of a new recording: the root and the library sections; no sections — no row."""
    if not sections:
        return None
    chosen = record_section(prefs, sections)
    return SettingsField(
        kind=FieldKind.CHOICE,
        title=_("Section"),
        value=chosen or _(NO_SECTION),
        choices=[
            Choice(
                key=f"record_section:{name}", title=name or _(NO_SECTION), checked=name == chosen
            )
            for name in ("", *sections)
        ],
    )


def panel_model(
    state: BarState,
    prefs: BarPrefs,
    env: MenuEnv,
    cards: list[RecordingCard],
    settings: SettingsModel,
    *,
    now: datetime | None = None,
    sections: list[str] | None = None,
) -> PanelModel:
    now = now or datetime.now()
    idle = state.phase == Phase.IDLE  # not recording; queued processing does not count
    t = title(state, discreet=prefs.discreet)
    tooltip = "Mnema"
    if t.icon != "idle":  # discreet mode looks idle from outside
        if state.phase == Phase.RECORDING:
            tooltip = _("Mnema: recording paused") if state.paused else _("Mnema: recording")
        elif state.jobs:
            tooltip = _("Mnema: processing a recording")
    dismiss = Button(key="dismiss_notice", title=_("Close"), symbol="xmark", compact=True)
    notices = [
        Notice(text=_notice(text), buttons=[dismiss])
        for text in (state.error.strip(), state.notice.strip())
        if text
    ]
    if state.sync_missing:
        ids, count = ",".join(sorted(state.sync_missing)), len(state.sync_missing)
        text, delete, restore = (
            _missing_on_server(count, mac=env.mac)
            if state.sync_missing_on_server
            else _missing_here(count, mac=env.mac)
        )
        notices.append(
            Notice(
                text=text,
                buttons=[
                    Button(
                        key=f"sync_delete:{state.sync_revision}:{ids}",
                        title=delete,
                        role=Role.DESTRUCTIVE,
                    ),
                    Button(key=f"sync_restore:{state.sync_revision}:{ids}", title=restore),
                ],
            )
        )
    if state.sync_other_library is not None:
        send = Button(key=f"sync_send:{state.sync_revision}:{state.sync_foreign}", title=_("Send"))
        notices.append(
            Notice(text=_other_library(state.sync_other_library, mac=env.mac), buttons=[send])
        )
    # the server refuses the token: sync and processing share the trouble, so one notice with a
    # button and no "Sync: …" about the same thing
    refused = env.server is not None and not env.server_auth
    # another Mnema version on the server: which side to update, above its own sync errors (they
    # may have another cause, such as the server being down)
    gap = version_gap(env.server_version) if env.server is not None and not refused else None
    if refused:
        text = _("The server no longer accepts this device. Get a new invitation from the server")
        connect = Button(key="server_setup", title=_("Connect…"))
        notices.append(Notice(text=text, buttons=[connect]))
    elif gap is VersionGap.APP:
        text = _("The server runs Mnema {server}. Update the app", server=env.server_version)
        update = [Button(key="update", title=_("Update"), enabled=not state.busy)]
        offered = env.update and not env.update_step
        notices.append(Notice(text=text, buttons=update if offered else []))
    elif gap is VersionGap.SERVER:
        text = _(
            "The server runs Mnema {server}, older than this app. Update the server",
            server=env.server_version,
        )
        notices.append(Notice(text=text))
    if not refused:
        notices.extend(
            Notice(text=_("Sync: {error}", error=_after_colon(e))) for e in state.sync_errors
        )
    if env.update_step:
        notices.append(Notice(kind="info", text=update_line(env), progress=env.update_progress))
    elif env.update and gap is not VersionGap.APP:  # else the version notice has the button
        update = Button(key="update", title=_("Update"), enabled=not state.busy)
        notices.append(
            Notice(
                kind="info",
                text=_("Mnema {version} available", version=env.update),
                buttons=[update],
            )
        )
    # the header is the connected server, wherever processing runs; no server — empty
    where, where_state = "", ""
    if env.server:
        name = server_label(env.server, env.server_name)
        where, where_state = name, "ok"
        if not env.server_auth:  # it answers but refuses: not "unavailable"
            where, where_state = _("{server}: invitation needed", server=name), "down"
        elif not env.server_ok:  # said in words, not only by the dot colour
            where, where_state = _("{server} is unavailable", server=name), "down"

    idle_view = recording = None
    if state.phase == Phase.RECORDING:
        screen = "recording"
        meters = state.meters or [  # before the first tick — from the settings
            SourceMeter(label="mic", muted=not prefs.mic),
            SourceMeter(label="system", muted=not prefs.system),
        ]
        pause = (
            Button(key="resume", title=_("Resume"), symbol="play.fill")
            if state.paused
            else Button(key="pause", title=_("Pause"), symbol="pause.fill")
        )
        recording = RecordingView(
            heading=_("Paused") if state.paused else _("Recording in progress"),
            timer=fmt_elapsed(state.elapsed),
            paused=state.paused,
            sources=[_live_source(m) for m in meters],
            buttons=[
                pause,
                Button(key="stop", title=_("Stop"), role=Role.PRIMARY, symbol="stop.fill"),
                Button(key="discard", title=_("Discard recording…"), role=Role.DESTRUCTIVE),
                Button(
                    key="discreet",
                    title=_("Hide recording in the menu bar"),
                    symbol="eye.slash.circle.fill" if prefs.discreet else "eye.slash",
                    checked=prefs.discreet,
                    compact=True,
                ),
            ],
        )
    else:
        screen = "idle"
        idle_view = IdleView(
            start=Button(
                key="start",
                title=_("Start recording"),
                role=Role.PRIMARY,
                symbol="record.circle",
                enabled=prefs.mic or prefs.system,
            ),
            sources=[
                _source("mic", on=prefs.mic, key="toggle_mic"),
                _source("system", on=prefs.system, key="toggle_system"),
            ],
            section=_section(prefs, sorted(set(sections or []))),
        )

    all_sections = sorted(set(sections or []) | {c.section for c in cards if c.section})
    jobs = {job.id: job for job in state.jobs}
    shown_cards = [
        _card(
            c,
            remarks=[_first_sentence(r) for r in c.remarks][:3],
            now=now,
            unreachable=state.unreachable.get(c.path),
            sections=all_sections,
            job=jobs.get(c.id),
        )
        for c in cards
    ]
    return PanelModel(
        screen=screen,
        status=StatusItem.model_validate(t.model_dump() | {"tooltip": tooltip}),
        where=where,
        where_state=where_state,
        notices=notices,
        idle=idle_view,
        recording=recording,
        cards=shown_cards,
        sections=all_sections,
        # shown whenever there are recordings: with nothing to delete a notice says why
        # (cleanup_plan)
        cleanup=Button(
            key="cleanup_audio",
            title=_("Delete audio of processed recordings…"),
            symbol="waveform",
            enabled=idle,
        )
        if cards
        else None,
        footer=[  # the "More" menu: library commands, then quit after a separator
            Button(key="open_folder", title=_("Open recordings folder"), symbol="folder"),
            Button(
                key="process_folder",
                title=_("Process recording…"),
                symbol="waveform.badge.plus",  # while recording — into the queue
            ),
            Button(key="quit", title=_(QUIT), symbol="power", divider=True),
        ],
        settings=settings,
        hotkey=prefs.hotkey,
        empty=_("No recordings yet"),
    )


def visible_cards(panel: PanelModel, *, query: str = "", section: str | None = None) -> list[Card]:
    """The window's recordings as one feed by date, as on the Mac. The search matches titles in
    the chosen section only. section — the filter's section, None — all."""
    needle = query.strip().casefold()
    return [
        c
        for c in panel.cards
        if (section is None or c.section == section) and needle in c.title.casefold()
    ]


def card_subtitle(card: Card, section: str | None) -> str:
    """Date and length; in the feed of all sections also the recording's section, if any."""
    shown = card.section if section is None else ""
    return " · ".join(part for part in (card.subtitle, shown) if part)
