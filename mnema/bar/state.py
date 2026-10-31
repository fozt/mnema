"""Menu bar model: pure logic without a GUI, testable on any OS.

BarState and BarPrefs (kept between launches) describe everything the user sees. `title()` is the
menu bar icon and caption, `menu()` the GNOME tray menu: app_linux.py only draws it and passes the
clicked key to BarRuntime.handle(). The macOS panel is built by panel.py.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterator, Mapping, Sequence
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field, field_validator, model_validator

from .. import i18n
from ..core import base
from ..i18n import _, fmt_bar_panel
from ..live.models import FINALIZE_STEPS, STEP_MESSAGES, TITLE_MESSAGE
from ..live.naming import display_title, recording_start
from ..live.remarks import Remark, menu_lines
from .range_pick import LANGS, language_names

# English source strings: translated where shown, never at import
QUIT = "Quit Mnema"
TRASH = "Delete recording"  # the whole folder goes to the Trash
OPEN_TEXT = "Open transcript"
OPEN_SUMMARY = "Open summary"
OPEN_FOLDER = "Open folder"  # the recording's folder, not the library root
NO_SUMMARY = "No summary"
SUMMARIZE = "Summarize"
SUMMARIZING = "Writing the summary and title…"  # the only step of a SUMMARIZE job
NO_SECTION = "No section"  # the recording lies in the library root
MOVE_TO_SECTION = "Move to section"
NEW_SECTION = "New section…"
SOURCE_TITLES = {"mic": "Microphone", "system": "System audio"}


def source_name(label: str) -> str:
    """A source as the user knows it; an unknown track label is shown as is."""
    return _(SOURCE_TITLES[label]) if label in SOURCE_TITLES else label


class Phase(StrEnum):
    """One recording at a time; processing is separate, in the BarState.jobs queue."""

    IDLE = "idle"
    RECORDING = "recording"


class JobKind(StrEnum):
    RECORD = "record"  # just recorded
    RECOVER = "recover"  # "Process…", "Retry"
    SUMMARIZE = "summarize"


class Where(StrEnum):
    """Where the Mac processes recordings: itself or on the connected server (MNEMA_RUNNER).
    Linux always processes on the server."""

    MAC = "mac"
    SERVER = "server"  # node.SERVER: the CLI and MCP read the same value


def asr_at(choice: str, *, on_server: bool) -> str:
    """Speech recognition where processing runs: only the server has cloud models and engines
    this Mac lacks (the app ships no GigaAM), so on this Mac such a choice works as automatic."""
    if not base.STANDALONE or on_server:
        return choice
    from ..core.backends import engine

    found = engine(choice)
    missing = found is not None and not found().available()
    return "auto" if "/" in choice or missing else choice


_HOTKEY_KEY = re.compile(r"[A-Z0-9]|F(?:[1-9]|1[0-2])|Space")
_HOTKEY_MODIFIERS = {"ctrl", "opt", "shift", "cmd"}


def valid_hotkey(spec: str) -> bool:
    """The start/stop recording shortcut as the macOS app writes it ("ctrl+opt+R"): distinct
    modifiers, at least one besides shift; the key is a letter, a digit, F1–F12 or Space. The
    only check: the Mac app just parses it."""
    *mods, key = spec.split("+")
    return (
        bool(_HOTKEY_KEY.fullmatch(key))
        and len(set(mods)) == len(mods)
        and set(mods) <= _HOTKEY_MODIFIERS
        and any(m != "shift" for m in mods)
    )


class BarPrefs(BaseModel):
    """Settings for the next recording, kept between launches (~/.config/mnema/bar.json)."""

    where: Where = Where.MAC
    mic: bool = True
    system: bool = True
    summary: bool = True  # summary.txt of the whole recording, and the title from it
    asr: str = "auto"  # auto | parakeet | gigaam | whisper | "provider/model"
    summary_lang: str | None = None  # None — the language of the conversation
    replace_original: bool = True  # processing a part moves the full recording to the Trash
    discreet: bool = False  # while recording, the menu bar looks idle
    autostart: bool = False  # mirrors the autostart file (the file on disk is the truth)
    hotkey: str | None = None  # "ctrl+opt+R" — start/stop recording (macOS app)
    record_section: str = ""  # library section for new recordings; empty — the root

    @field_validator("hotkey")
    @classmethod
    def _checked_hotkey(cls, value: str | None) -> str | None:
        """A hand-edited bar.json gets the same check: an invalid shortcut never reaches the app."""
        return value if value and valid_hotkey(value) else None

    @field_validator("where", mode="before")
    @classmethod
    def _known_where(cls, value: object) -> Where:
        """Anything but "server" means this Mac."""
        return Where.SERVER if value == Where.SERVER else Where.MAC

    @model_validator(mode="after")
    def _asr_at_where(self) -> BarPrefs:
        """What cannot run on this Mac (cloud, an engine it lacks) becomes automatic here."""
        self.asr = asr_at(self.asr, on_server=self.where == Where.SERVER)
        return self


def prefs_path() -> Path:
    from ..config import bar_prefs_file

    return bar_prefs_file()


def load_prefs(path: Path | None = None) -> BarPrefs:
    """A broken or missing file gives the defaults: the app must start anyway."""
    p = path or prefs_path()
    try:
        return BarPrefs.model_validate(json.loads(p.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001 - any read failure gives the defaults
        return BarPrefs()


_SAVING = threading.Lock()  # the menu and the recording worker save from different threads


def save_prefs(prefs: BarPrefs, path: Path | None = None) -> None:
    p = path or prefs_path()
    with _SAVING:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(prefs.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(p)


class SourceMeter(BaseModel):
    """Live level of a source while recording."""

    label: str
    history: list[float] = Field(default_factory=list)  # recent levels 0..1
    muted: bool = False  # turned off in the menu (silence is written)


class Job(BaseModel):
    """A recording in the app's processing queue, as the panel and the menu show it. BarRuntime
    controls it (stop, language, own title)."""

    id: str  # meta.id of the recording
    folder: Path  # renamed or moved — the job finds it by id
    kind: JobKind = JobKind.RECORD
    running: bool = False  # otherwise waiting in the queue
    stage: str = ""  # the start message of the current step (STEP_MESSAGES), in either language
    progress: float | None = None  # overall share 0..1
    lang: str | None = None  # language chosen explicitly (strict); None — from speech
    languages: dict[str, str] = Field(default_factory=dict)  # track → language already applied
    transcript_ready: Path | None = None  # the transcript is ready, the summary still runs
    elapsed: float = 0.0  # seconds running

    @property
    def applied_languages(self) -> set[str]:
        """Languages decided per track, else the chosen one; empty — not known yet."""
        return set(self.languages.values()) or ({self.lang} if self.lang else set())


class BarState(BaseModel):
    phase: Phase = Phase.IDLE
    paused: bool = False
    elapsed: float = 0.0  # recording length without pauses, s
    meters: list[SourceMeter] = Field(default_factory=list)
    jobs: list[Job] = Field(default_factory=list)  # processing queue; the running one is first
    notice: str = ""  # warning before or during recording (no server, server down)
    last_result: Path | None = None  # transcript.txt, or the folder when there is no text
    last_warnings: list[Remark] = Field(default_factory=list)
    last_summary: Path | None = None  # summary.txt of the last recording
    unfinished: list[Path] = Field(default_factory=list)  # interrupted recordings without text
    # recording → the server that did not answer: "<server> is unavailable", Retry, Choose server
    unreachable: dict[Path, str] = Field(default_factory=dict)
    error: str = ""
    sync_revision: int = 0  # UI only: revision of the selected connection
    sync_missing: list[str] = Field(default_factory=list)
    sync_missing_on_server: bool = False  # sync_missing are gone on the server, not here
    sync_other_library: int | None = None  # the server has another library: local recordings
    sync_foreign: str = ""  # that library's number, for the answer
    sync_errors: list[str] = Field(default_factory=list)

    @property
    def running(self) -> Job | None:
        """The job processing now."""
        return next((job for job in self.jobs if job.running), None)

    @property
    def waiting(self) -> list[Job]:
        """Jobs waiting behind the running one."""
        return [job for job in self.jobs if not job.running]

    @property
    def busy(self) -> bool:
        """Recording or processing: quitting or updating would interrupt something."""
        return self.phase == Phase.RECORDING or bool(self.jobs)


class MenuEnv(BaseModel):
    """What the menu depends on, computed by BarRuntime; no secrets."""

    server: str | None = None  # MNEMA_RUNNER; None — not set
    server_ok: bool = False
    server_auth: bool = True  # False — the server answered 401/403
    server_name: str | None = None  # node name from its capabilities
    server_llm: bool = False  # the server makes titles and summaries itself
    server_model: str | None = None  # server model without the key; None — none or unknown
    server_records: int | None = None  # recordings in the last sync with this server
    server_version: str | None = None  # Mnema version from its capabilities; None — unknown
    model: str | None = None  # this Mac's model without the key; None — not set (or not a Mac)
    mac: bool = False  # standalone Mac (base.STANDALONE)
    asr_choices: list[tuple[str, str]] = Field(default_factory=list)
    update: str = ""  # the newer version, "0.4.0"; empty — nothing or not checked
    update_error: str = ""  # the last check or update failed: the reason
    update_step: str = ""  # an update is running: its step
    update_progress: float | None = None  # the step's share 0..1 (the download); None — unknown
    update_checking: bool = False  # a check is running
    update_checked: bool = False  # an update check has finished at least once


def asr_options(env: MenuEnv, current: str) -> list[tuple[str, str]]:
    """Recognition choices of this Mac or the server; the saved choice shows even without a list."""
    from ..llm.providers import TITLES, provider_of

    out = list(env.asr_choices)
    if current not in {value for value, _title in out}:
        provider = provider_of(current)
        title = (
            f"{TITLES[provider[0]]} · {provider[1]}"
            if provider
            else _("Automatic")
            if current == "auto"
            else current
        )
        out.append((current, title))
    return out


class MenuItem(BaseModel):
    """A menu item. key is the BarRuntime.handle action (None — an inactive info line)."""

    key: str | None = None
    title: str = ""
    enabled: bool = True
    checked: bool = False
    radio: bool = False  # one of the submenu's options
    separator: bool = False
    children: list[MenuItem] = Field(default_factory=list)


def fmt_elapsed(seconds: float) -> str:
    s = int(seconds)
    h, m, sec = s // 3600, s % 3600 // 60, s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def _step_sources() -> dict[str, int]:
    """Start message of each step → its number on the common scale (steps split it evenly)."""
    steps = {STEP_MESSAGES[s]: i for i, s in enumerate(FINALIZE_STEPS)}
    steps[TITLE_MESSAGE] = FINALIZE_STEPS.index("summary")
    return steps


def _source_text(stage: str) -> str | None:
    """The source string of a step message in either language: the server renders progress in
    the client's language, this Mac in its own."""
    ru = i18n.catalog()
    for text in (*_step_sources(), SUMMARIZING):
        if stage == text or stage == ru.get(text):
            return text
    return None


class _StepIndex(Mapping[str, int]):
    """`message in STEP_AT` and `STEP_AT[message]` accept a step message in either language."""

    def _table(self) -> dict[str, int]:
        ru = i18n.catalog()
        table = _step_sources()
        for text, at in list(table.items()):
            if isinstance(translated := ru.get(text), str):
                table.setdefault(translated, at)
        return table

    def __getitem__(self, message: str) -> int:
        return self._table()[message]

    def __iter__(self) -> Iterator[str]:
        return iter(self._table())

    def __len__(self) -> int:
        return len(self._table())


STEP_AT: Mapping[str, int] = _StepIndex()


QUEUED = "Queued"
STOP_PROCESSING = "Stop processing"
UNQUEUE = "Remove from queue"
SET_TITLE = "Set title…"


def stage_text(stage: str) -> str:
    """A step message in the interface language; an unknown message as it came."""
    source = _source_text(stage)
    return _(source) if source is not None else stage


def processing_step(job: Job) -> tuple[str, float | None]:
    """The current step and its own share 0..1: steps take very different time, so an overall
    percentage lies. None — the step reports no share (summary): a bar without percent."""
    stage = job.stage or STEP_MESSAGES[FINALIZE_STEPS[0]]
    local = (job.progress or 0.0) * len(FINALIZE_STEPS) - STEP_AT.get(stage, 0)
    return stage_text(stage), (min(local, 1.0) if local > 1e-6 else None)


def step_line(job: Job, *, coarse: bool = False) -> str:
    """The step with its share, "Recognizing speech… 40%"; without a share, the step alone.
    coarse — 5% steps: the tray menu is rebuilt whole and closes under the cursor."""
    stage, local = processing_step(job)
    if local is None:
        return stage
    percent = int(int(local * 20) / 20 * 100) if coarse else int(local * 100)
    return _("{step} {percent}%", step=stage, percent=percent)


def update_line(env: MenuEnv, *, coarse: bool = False) -> str:
    """The running update's step with its share, "Downloading Mnema 0.4.0… 40%"; coarse — 5%
    steps, as in step_line."""
    share = env.update_progress
    if share is None:
        return env.update_step
    percent = int(int(share * 20) / 20 * 100) if coarse else int(share * 100)
    return _("{step} {percent}%", step=env.update_step, percent=percent)


def languages_label(codes: set[str] | list[str]) -> str:
    """The languages as "Language: Russian"; no languages — empty."""
    names = language_names(codes)
    return _("Language: {names}", names=fmt_bar_panel.names_in_sentence(names)) if names else ""


def language_label(job: Job) -> str:
    """The language applied to a running recording, or that it is still being detected."""
    return languages_label(job.applied_languages) or _("Language: detecting…")


def language_choices() -> list[tuple[str, str]]:
    """Languages a running recording can be redone in, as shown."""
    return [(k, _(t)) for k, t in LANGS if k != "auto"]


def can_relang(job: Job) -> bool:
    """Language and own title can change until the summary of a ready transcript starts."""
    return job.running and job.kind != JobKind.SUMMARIZE and _source_text(job.stage) != SUMMARIZING


def has_warning(state: BarState) -> bool:
    """Yellow icon: something keeps the recording from getting its text, visible before the
    call ends."""
    return bool(
        state.notice
        or state.error
        or state.sync_errors
        or state.sync_missing
        or state.sync_other_library is not None
    )


class Title(BaseModel):
    """The menu bar item: which icon and what text next to it."""

    icon: str  # idle | recording | paused | saving | warning
    text: str = ""  # timer "12:34"; empty — the icon alone


def title(state: BarState, *, discreet: bool = False, coarse: bool = False) -> Title:
    """coarse — for the GNOME panel, where every caption change redraws the indicator (blinks):
    the timer goes in minutes."""
    if coarse and state.phase == Phase.RECORDING and not (discreet or state.paused):
        icon = "warning" if has_warning(state) else "recording"
        return Title(icon=icon, text=_("{n} min", n=int(state.elapsed // 60)))
    if coarse and state.paused and state.phase == Phase.RECORDING and not discreet:
        return Title(icon="paused", text=_("paused"))
    if discreet and state.busy:
        return Title(icon="idle")  # from outside, a recording looks idle
    if state.phase == Phase.RECORDING:  # recording outranks processing
        if state.paused:
            return Title(icon="paused", text=fmt_elapsed(state.elapsed))
        icon = "warning" if has_warning(state) else "recording"
        return Title(icon=icon, text=fmt_elapsed(state.elapsed))
    if state.jobs:
        return Title(icon="saving")  # the card and the menu show the step
    asking = state.sync_missing or state.sync_other_library is not None
    return Title(icon="warning" if state.error or state.sync_errors or asking else "idle")


_ARTIFACTS = {".txt", ".md", ".mp3", ".wav"}  # files inside a recording folder


def is_artifact(path: Path) -> bool:
    """The path is a file of the recording, not its folder. Path.suffix alone misleads: the
    folder "2026-09-27_Release v2.0" has the suffix ".0"."""
    return path.suffix.lower() in _ARTIFACTS


def folder_of(path: Path) -> Path:
    return path.parent if is_artifact(path) else path


def title_of(path: Path) -> str | None:
    """The recording title: its folder name without the date; a time-only name gives None."""
    return display_title(folder_of(path).name).title


def result_name(path: Path) -> str:
    """The recording for menus and notifications: "Daily · Sep 27", "Sep 27, 13:05", an untitled
    part "Sep 27, 13:05 (0.10–1.35)"."""
    folder = folder_of(path)
    shown = display_title(folder.name)
    start, _start_known = recording_start(folder)
    day = fmt_bar_panel.day_month(start)
    if shown.title is None:
        return f"{day}, {start:%H:%M}{shown.part}"
    return f"{shown.title} · {day}"


def readable_notes(remarks: list[Remark]) -> list[str]:
    """Processing remarks as 1–3 menu lines; the full text is "Details"."""
    return menu_lines(remarks)


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:]


def record_section(prefs: BarPrefs, sections: Sequence[str]) -> str:
    """The section of the next recording as shown: a vanished one is the root (empty)."""
    return prefs.record_section if prefs.record_section in sections else ""


def discreet_title(mac: bool) -> str:
    """Only the Mac has a menu bar; GNOME has the top bar."""
    return _("Hide recording in the menu bar") if mac else _("Hide recording in the top bar")


def _discreet_item(prefs: BarPrefs, env: MenuEnv) -> MenuItem:
    """Available while recording too: screen sharing may start in the middle of a call."""
    return MenuItem(key="discreet", title=discreet_title(env.mac), checked=prefs.discreet)


def _move_last(folder: Path, sections: Sequence[str]) -> MenuItem:
    """The "Move to section" submenu of the last recording: the root, the sections, a new one."""
    from ..config import library_root

    root = library_root().absolute()
    here = folder.parent.name if folder.parent.parent == root else ""
    choices = [
        MenuItem(
            key=f"move_last:{name}", title=name or _(NO_SECTION), checked=name == here, radio=True
        )
        for name in ("", *sections)
    ]
    choices += [MenuItem(separator=True), MenuItem(key="new_section_last", title=_(NEW_SECTION))]
    return MenuItem(title=_(MOVE_TO_SECTION), children=choices)


def _processing_items(state: BarState) -> list[MenuItem]:
    """Processing in the tray, idle or recording: which recording and for how long, the step in
    5% steps, language, transcript, own title, stop; how many wait."""
    items: list[MenuItem] = []
    job = state.running
    if job is not None:
        name = result_name(job.folder)[:40]
        minutes = int(job.elapsed // 60)  # shows the process is alive during a long step
        heading = (
            _("Processing: {name} · {n} min", name=name, n=minutes)
            if minutes
            else _("Processing: {name}", name=name)
        )
        items.append(MenuItem(title=heading, enabled=False))
        items.append(MenuItem(title=step_line(job, coarse=True), enabled=False))
        key = f"record:{job.id}"
        if can_relang(job):  # another language redoes the recording strictly in it
            applied = job.applied_languages
            choices = [
                MenuItem(key=f"{key}:lang:{k}", title=t, checked=applied == {k}, radio=True)
                for k, t in language_choices()
            ]
            items.append(MenuItem(title=language_label(job), children=choices))
        if job.transcript_ready is not None:  # readable while the summary runs
            items.append(MenuItem(key=f"{key}:transcript", title=_(OPEN_TEXT)))
        if can_relang(job):
            items.append(MenuItem(key=f"{key}:title", title=_(SET_TITLE)))
        items.append(MenuItem(key=f"{key}:stop", title=_(STOP_PROCESSING)))
    if waiting := state.waiting:
        items.append(MenuItem(title=_("Queued: {n}", n=len(waiting)), enabled=False))
    return items


def _source_row(m: SourceMeter, *, paused: bool) -> str:
    name = source_name(m.label)
    if m.muted:
        return _("{name}   off", name=name)
    if paused:
        return _("{name}   paused", name=name)
    return _("{name}   recording", name=name)


def menu(
    state: BarState, prefs: BarPrefs, env: MenuEnv | None = None, *, sections: Sequence[str] = ()
) -> list[MenuItem]:
    """The tray menu; sections — library sections to choose from when idle."""
    env = env or MenuEnv()
    sep = MenuItem(separator=True)
    items: list[MenuItem] = []
    if state.phase == Phase.RECORDING:
        # AppIndicator rebuilds the whole menu: ticking seconds and levels would blink, so the
        # timer is the caption next to the icon
        heading = _("Paused") if state.paused else _("Recording in progress")
        items.append(MenuItem(title=heading, enabled=False))
        for m in state.meters:  # a source row is a toggle: checked — being recorded
            row = _source_row(m, paused=state.paused)
            items.append(MenuItem(key=f"source:{m.label}", title=row, checked=not m.muted))
        if state.notice:
            items.append(MenuItem(title=f"⚠︎ {_sentence(state.notice)}", enabled=False))
        items.append(sep)
        items.append(
            MenuItem(key="resume" if state.paused else "pause",
                     title=_("Resume") if state.paused else _("Pause"))
        )  # fmt: skip
        items.append(MenuItem(key="stop", title=_("Stop recording")))
        items.append(MenuItem(key="discard", title=_("Discard recording…")))
        if state.jobs:
            items += [sep, *_processing_items(state)]
        items += [sep, _discreet_item(prefs, env), MenuItem(key="quit", title=_(QUIT))]
        return items
    if state.error:
        items.append(MenuItem(title=f"⚠︎ {_sentence(state.error[:80])}", enabled=False))
    if state.notice:
        items.append(MenuItem(title=f"⚠︎ {_sentence(state.notice)}", enabled=False))
    if env.update_step:
        items.append(MenuItem(title=update_line(env, coarse=True), enabled=False))
    # sources of the next recording, as on the Mac panel
    items.append(MenuItem(key="toggle_mic", title=source_name("mic"), checked=prefs.mic))
    items.append(MenuItem(key="toggle_system", title=source_name("system"), checked=prefs.system))
    if sections:
        chosen = record_section(prefs, sections)
        choices = [
            MenuItem(
                key=f"record_section:{name}",
                title=name or _(NO_SECTION),
                checked=name == chosen,
                radio=True,
            )
            for name in ("", *sections)
        ]
        shown = chosen or _(NO_SECTION)
        items.append(MenuItem(title=_("Section: {name}", name=shown), children=choices))
    start = MenuItem(key="start", title=_("Start recording"), enabled=prefs.mic or prefs.system)
    items.append(start)
    items.append(sep)
    # the same order as a panel card's menu: open, separator, actions; the full set of record
    # actions is in the Recordings window (linux_library)
    if state.last_result is not None:
        has_text = is_artifact(state.last_result) and state.last_result.suffix == ".txt"
        sub = []
        if state.last_summary is not None:
            sub.append(MenuItem(key="open_summary", title=_(OPEN_SUMMARY)))
        if has_text:
            sub.append(MenuItem(key="open_last", title=_(OPEN_TEXT)))
        sub += [MenuItem(key="reveal_last", title=_(OPEN_FOLDER)), sep]
        if not has_text:  # audio only: processing was stopped or gave no text
            sub.append(MenuItem(key=f"recover:{state.last_result}", title=_("Process…")))
        sub.append(MenuItem(key="rename", title=_("Rename…")))
        sub.append(_move_last(folder_of(state.last_result), sections))
        if has_text and state.last_summary is None:
            folder = folder_of(state.last_result)
            sub.append(MenuItem(key=f"summarize:{folder}", title=_(SUMMARIZE)))
        if state.last_warnings:
            sub.append(sep)
            sub += [MenuItem(title=f"⚠︎ {line}", enabled=False) for line in readable_notes(
                state.last_warnings
            )]  # fmt: skip
            sub.append(MenuItem(key="open_notes", title=_("Details")))
        mark = "  ⚠︎" if state.last_warnings else ""
        name = result_name(state.last_result)[:40]
        last = _("Last recording: {name}", name=name) + mark
        items.append(MenuItem(title=last, children=sub))
    for d in state.unfinished:
        items.append(
            MenuItem(
                title="⚠︎ " + _("Not processed: {name}", name=result_name(d)),
                children=[
                    MenuItem(key=f"reveal:{d}", title=_(OPEN_FOLDER)),
                    sep,
                    MenuItem(key=f"recover:{d}", title=_("Process…")),
                ],
            )
        )
    if state.jobs:
        items += [*_processing_items(state), sep]
    items.append(MenuItem(key="open_folder", title=_("Open recordings folder")))
    # any recording folder, whole or a part: interrupted, left running, files from elsewhere
    items.append(MenuItem(key="process_folder", title=_("Process recording…")))
    settings = MenuItem(key="settings", title=_("Settings"))
    items += [sep, settings, sep, MenuItem(key="quit", title=_(QUIT))]
    return items
