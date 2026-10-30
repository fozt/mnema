"""Protocol between the Mnema macOS app (Swift) and the Python engine.

The engine is Python inside the Mnema.app process (`mnema.bar --headless`, headless.py): it
writes one JSON message per line to fd 1 (state / ask / form / notify / settings / quit) and
reads fd 0 (action / answer / form_event / panel). Every text the user sees arrives as a finished
string in these models, already in the interface language; Swift only lays it out. The Swift
mirror is macos/Sources/Mnema/Protocol.swift; samples for Swift tests and snapshots are
macos/Fixtures (`python -m mnema.bar.protocol`).
"""

from __future__ import annotations

import json
import sys
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter

from ..i18n import _, using
from .range_pick import LANGS

VERSION = 1
FIXTURES = Path(__file__).parent / "macos" / "Fixtures"


class Role(StrEnum):
    NORMAL = "normal"
    PRIMARY = "primary"
    DESTRUCTIVE = "destructive"


class Button(BaseModel):
    """A button or menu item; key is the action for BarRuntime.handle."""

    key: str
    title: str
    role: Role = Role.NORMAL
    enabled: bool = True
    symbol: str = ""  # SF Symbol
    checked: bool | None = None  # a toggle; None: a plain button
    compact: bool = False  # icon only, title is the tooltip
    divider: bool = False  # menu item: a separator before it
    # menu item: a submenu ("Move to section"); an item with a submenu has no action of its own
    children: list[Button] = Field(default_factory=list)


class StatusItem(BaseModel):
    """The menu bar item."""

    icon: Literal["idle", "recording", "paused", "saving", "warning"]
    text: str = ""  # "12:34", "42%"
    tooltip: str = "Mnema"


class Notice(BaseModel):
    text: str
    buttons: list[Button] = Field(default_factory=list)
    kind: Literal["warn", "info"] = "warn"  # info: news, not trouble (an update is available)
    progress: float | None = None  # a bar under the text, 0..1 (the update download); None — none


class Source(BaseModel):
    """An audio source: a toggle when idle, a row with its level while recording."""

    label: str  # mic | system
    title: str
    symbol: str
    symbol_off: str
    on: bool
    key: str
    enabled: bool = True
    levels: list[float] = Field(default_factory=list)  # 0..1, newest last
    status: str = ""  # "Off"


class IdleView(BaseModel):
    start: Button
    sources: list[Source]
    # section of the next recording (CHOICE of record_section:<name>, empty: the root);
    # None: there are no sections
    section: SettingsField | None = None


class RecordingView(BaseModel):
    heading: str
    timer: str
    paused: bool = False
    sources: list[Source]
    buttons: list[Button]


class Card(BaseModel):
    """A recording in the list: title, a date and length line, a detail line."""

    id: str  # meta.id
    section: str = ""  # empty: the library root
    title: str
    subtitle: str  # "Yesterday, 14:06 · 30 min"
    gist: str = ""
    status: str = ""  # "Not processed"
    warn: bool = False  # processing left remarks
    remarks: list[str] = Field(default_factory=list)
    open_key: str | None = None  # click on the card: the recording folder
    buttons: list[Button] = Field(default_factory=list)  # in the detail line, at most one
    menu: list[Button] = Field(default_factory=list)  # context menu
    detail: str = ""  # third line: state, gist or "No summary"; reasons go to the tooltip
    detail_warn: bool = False  # detail tells what is wrong: shown with a triangle
    tooltip: str = ""  # gist, remarks, language, one per line
    busy: bool = False  # being processed: a bar under detail (the step is in detail)
    progress: float | None = None  # step share 0..1; None while busy: an indeterminate bar


class FieldKind(StrEnum):
    TOGGLE = "toggle"
    CHOICE = "choice"
    BUTTON = "button"
    STATUS = "status"
    HOTKEY = "hotkey"


class Tone(StrEnum):
    NONE = "none"
    OK = "ok"
    WARN = "warn"
    ERROR = "error"
    BUSY = "busy"  # a spinner instead of the dot


class Choice(BaseModel):
    key: str
    title: str
    detail: str = ""
    checked: bool = False
    enabled: bool = True


class SettingsField(BaseModel):
    kind: FieldKind
    title: str
    # hotkey: the app sends "<key>:<shortcut>"
    key: str | None = None
    checked: bool = False
    enabled: bool = True
    # choice: the chosen option in short; status: the status text; hotkey: "ctrl+opt+R"
    value: str = ""
    # status: a caption under the value
    detail: str = ""
    placeholder: str = ""
    error: str = ""
    role: Role = Role.NORMAL
    choices: list[Choice] = Field(default_factory=list)
    tone: Tone = Tone.NONE  # status: the dot before value
    progress: float | None = None  # status: a bar under the value, 0..1; None — none


class SettingsSection(BaseModel):
    title: str = ""
    fields: list[SettingsField]


class SettingsTab(BaseModel):
    id: str
    title: str
    symbol: str
    sections: list[SettingsSection]


class SettingsModel(BaseModel):
    title: str = Field(default_factory=lambda: _("Settings"))
    tabs: list[SettingsTab]


class PanelModel(BaseModel):
    version: int = VERSION
    screen: Literal["idle", "recording"]  # processing shows in the cards
    status: StatusItem
    header: str = "Mnema"
    where: str = ""  # the connected server: "Home", "Home is unavailable"; empty: none
    where_state: Literal["", "ok", "down"] = ""
    notices: list[Notice] = Field(default_factory=list)
    idle: IdleView | None = None
    recording: RecordingView | None = None
    empty: str = Field(default_factory=lambda: _("No recordings yet"))
    cards: list[Card] = Field(default_factory=list)
    sections: list[str] = Field(default_factory=list)  # empty sections included
    cleanup: Button | None = None
    footer: list[Button] = Field(default_factory=list)
    settings: SettingsModel
    hotkey: str | None = None  # the start/stop recording shortcut; None: not set


class TitleAsk(BaseModel):
    """A window with one title field: the recording title, a new section."""

    kind: Literal["title"] = "title"
    title: str = Field(default_factory=lambda: _("Recording title"))
    value: str = ""
    placeholder: str = Field(default_factory=lambda: _("Recording title"))
    ok: str = Field(default_factory=lambda: _("Save"))
    cancel: str = Field(default_factory=lambda: _("Skip"))
    label: str = ""  # field caption; empty: none
    # "ok" is unavailable while nothing of the title is left for a folder name
    # (naming.folder_title: forbidden and control characters, edge spaces and dots do not count)
    required: bool = False


def new_section_ask() -> TitleAsk:
    """ "New section…" in a recording's menu: the section is created and the recording moved."""
    return TitleAsk(
        title=_("New section"),
        label=_("Section name"),
        placeholder="",
        ok=_("Create"),
        cancel=_("Cancel"),
        required=True,
    )


def __getattr__(name: str) -> TitleAsk:
    # `from .protocol import NEW_SECTION_ASK` in older callers: translated when imported
    if name == "NEW_SECTION_ASK":
        return new_section_ask()
    raise AttributeError(name)


class ConfirmAsk(BaseModel):
    kind: Literal["confirm"] = "confirm"
    title: str
    message: str
    ok: str
    cancel: str = Field(default_factory=lambda: _("Cancel"))


class FolderAsk(BaseModel):
    kind: Literal["folder"] = "folder"
    start: str
    message: str = Field(default_factory=lambda: _("Choose the recording folder"))
    ok: str = Field(default_factory=lambda: _("Choose"))


class RangeAsk(BaseModel):
    kind: Literal["range"] = "range"
    title: str = Field(default_factory=lambda: _("Process recording"))
    subtitle: str = ""
    length: float
    name: str = ""
    replace: bool = False
    can_replace: bool = True
    start_label: str = Field(default_factory=lambda: _("Start"))
    end_label: str = Field(default_factory=lambda: _("End"))
    name_label: str = Field(default_factory=lambda: _("Title"))
    placeholder: str = Field(default_factory=lambda: _("Recording title"))
    replace_label: str = Field(default_factory=lambda: _("Move the full recording to the Trash"))
    whole_caption: str = Field(default_factory=lambda: _("Whole recording selected"))
    # {} is filled by the app with the selected length, "0:12:30"
    part_caption: str = Field(default_factory=lambda: _("Selected {}"))
    lang_label: str = Field(default_factory=lambda: _("Language"))
    lang: str = "auto"  # the chosen key of langs; the answer's lang field
    langs: list[Choice] = Field(
        default_factory=lambda: [Choice(key=k, title=_(t)) for k, t in LANGS]
    )
    asr_label: str = Field(default_factory=lambda: _("Recognition"))
    asr: str = "auto"  # the chosen key of asrs; the answer's asr field
    asrs: list[Choice] = Field(default_factory=list)
    sources_label: str = Field(default_factory=lambda: _("Sources"))
    # the folder's tracks (key: mic | system, checked: checked at first), shown when there are
    # more than one; the answer's sources field holds the checked keys, at least one
    sources: list[Choice] = Field(default_factory=list)
    ok: str = Field(default_factory=lambda: _("Process"))
    cancel: str = Field(default_factory=lambda: _("Cancel"))


class FormFieldKind(StrEnum):
    SECRET = "secret"  # input is hidden; the engine never sends a value
    CHOICE = "choice"  # one of choices; the chosen one is Choice.checked


class FormField(BaseModel):
    """A form field: the app keeps what was typed and sends it only with a window event."""

    id: str  # connection, provider, key, model
    label: str
    kind: FormFieldKind = FormFieldKind.SECRET
    placeholder: str = ""
    # choice: key is the value in values, detail explains the option ("free")
    choices: list[Choice] = Field(default_factory=list)


class FormAsk(BaseModel):
    """A live window with fields and a check: engine answers update it in place and keep what
    was typed. Actions are FormEventMsg; the user closing it is AnswerMsg(None)."""

    kind: Literal["form"] = "form"
    title: str
    message: str = ""
    fields: list[FormField]
    rev: int = 0  # the window event this state answers; 0: opening
    error: str = ""  # a red line under the fields; the window stays open
    busy: str = ""  # "Checking…": everything but cancel is unavailable
    ok: str = Field(default_factory=lambda: _("Save"))
    force: str = ""  # "Save anyway" after a failed check; empty: no such button
    cancel: str = Field(default_factory=lambda: _("Cancel"))
    # fields whose input the app clears: only in an answer to a check (a refused key); empty
    # in "Checking…" and in a failure answer, or input typed after the refusal would vanish
    clear: list[str] = Field(default_factory=list)


Ask = Annotated[TitleAsk | ConfirmAsk | FolderAsk | RangeAsk | FormAsk, Field(discriminator="kind")]


class StateMsg(BaseModel):
    type: Literal["state"] = "state"
    panel: PanelModel


class AskMsg(BaseModel):
    type: Literal["ask"] = "ask"
    id: int
    ask: Ask


class NotifyMsg(BaseModel):
    type: Literal["notify"] = "notify"
    title: str
    message: str
    open_key: str | None = None  # click on the notification


class FormMsg(BaseModel):
    """Engine → app: the new state of an open form window; ask=None closes it (saved)."""

    type: Literal["form"] = "form"
    id: int
    ask: FormAsk | None = None


class SettingsMsg(BaseModel):
    """Engine → app: open the settings window on tab (a SettingsModel tab id)."""

    type: Literal["settings"] = "settings"
    tab: str


class QuitMsg(BaseModel):
    type: Literal["quit"] = "quit"


class ActionMsg(BaseModel):
    type: Literal["action"] = "action"
    key: str


class AnswerMsg(BaseModel):
    """A window answer: title a string, confirm a bool, folder a path, range the RangeAnswer
    fields; None: closed without an answer (always None for a form: its actions are
    FormEventMsg)."""

    type: Literal["answer"] = "answer"
    id: int
    value: Any = None


class FormOp(StrEnum):
    SUBMIT = "submit"  # "Check and save"
    FORCE = "force"  # "Save anyway"


class FormEventMsg(BaseModel):
    """App → engine: an action in a form window; values holds every field: a secret as typed, a
    choice as the option key. repr hides the values: they include the model key."""

    type: Literal["form_event"] = "form_event"
    id: int
    rev: int  # window event number, from 1 up
    op: FormOp
    values: dict[str, str] = Field(repr=False)


class PanelMsg(BaseModel):
    type: Literal["panel"] = "panel"
    open: bool


Outgoing = Annotated[
    StateMsg | AskMsg | FormMsg | NotifyMsg | SettingsMsg | QuitMsg, Field(discriminator="type")
]
Incoming = Annotated[ActionMsg | AnswerMsg | FormEventMsg | PanelMsg, Field(discriminator="type")]
OUTGOING: TypeAdapter[Any] = TypeAdapter(Outgoing)
INCOMING: TypeAdapter[Any] = TypeAdapter(Incoming)


def encode(msg: BaseModel) -> str:
    """A protocol line: JSON without inner newlines, ending in "\\n"."""
    return msg.model_dump_json() + "\n"


def decode_incoming(line: str) -> ActionMsg | AnswerMsg | FormEventMsg | PanelMsg:
    """A line from the app → a message; ValueError for a broken line."""
    return INCOMING.validate_json(line)


# Samples for macos/Fixtures. They hold the Russian UI as data: the Swift tests and snapshots
# read them, and conftest pins Russian.

_HOME = "/Users/me/mnema"


def _updates(status: SettingsField) -> SettingsSection:
    buttons = [
        SettingsField(kind=FieldKind.BUTTON, title="Проверить обновления", key="update_check"),
        SettingsField(kind=FieldKind.BUTTON, title="Обновить", key="update"),
    ]
    for button in buttons:  # not while an update is running
        button.enabled = status.progress is None
    return SettingsSection(title="Обновления", fields=[status, *buttons])


_UPDATE_FOUND = SettingsField(
    kind=FieldKind.STATUS, title="Mnema 0.2.0", value="Доступна версия 0.3.0", tone=Tone.WARN
)


def _settings_sample(*, idle: bool = True, update: SettingsField = _UPDATE_FOUND) -> SettingsModel:
    """Mac settings with the server "Дом" connected and a model chosen."""
    f, c = SettingsField, Choice
    model = SettingsModel(
        tabs=[
            SettingsTab(
                id="record",
                title="Запись",
                symbol="mic",
                sections=[
                    SettingsSection(
                        title="Источники звука",
                        fields=[
                            f(
                                kind=FieldKind.TOGGLE,
                                title="Микрофон",
                                key="toggle_mic",
                                checked=True,
                            ),
                            f(
                                kind=FieldKind.TOGGLE,
                                title="Системный звук",
                                key="toggle_system",
                                checked=True,
                            ),
                        ],
                    ),
                    SettingsSection(
                        title="Обработка",
                        fields=[
                            f(
                                kind=FieldKind.CHOICE,
                                title="Где обрабатывать",
                                value="на этом Mac",
                                choices=[
                                    c(key="where:mac", title="На этом Mac", checked=True),
                                    c(key="where:server", title="На сервере"),
                                ],
                            ),
                            f(
                                kind=FieldKind.CHOICE,
                                title="Распознавание",
                                value="Автоматически",
                                choices=[
                                    c(key="asr:auto", title="Автоматически", checked=True),
                                    c(key="asr:parakeet", title="Parakeet"),
                                    c(key="asr:gigaam", title="GigaAM"),
                                    c(key="asr:whisper", title="Whisper"),
                                ],
                            ),
                            f(
                                kind=FieldKind.STATUS,
                                title="Модель",
                                value="gemini/gemini-flash-latest",
                                tone=Tone.OK,
                            ),
                            f(kind=FieldKind.BUTTON, title="Выбрать модель…", key="model_setup"),
                            f(
                                kind=FieldKind.TOGGLE,
                                title="Составлять пересказ",
                                key="summary",
                                checked=True,
                            ),
                            f(
                                kind=FieldKind.CHOICE,
                                title="Язык пересказа",
                                value="как в разговоре",
                                choices=[
                                    c(
                                        key="summary_lang:auto",
                                        title="Как в разговоре",
                                        checked=True,
                                    ),
                                    c(key="summary_lang:ru", title="Русский"),
                                    c(key="summary_lang:en", title="Английский"),
                                ],
                            ),
                        ],
                    ),
                ],
            ),
            SettingsTab(
                id="server",
                title="Сервер",
                symbol="server.rack",
                sections=[
                    SettingsSection(
                        title="Подключение",
                        fields=[
                            f(
                                kind=FieldKind.STATUS,
                                title="Дом",
                                value="Подключён",
                                detail="srv:8123",
                                tone=Tone.OK,
                            ),
                            f(kind=FieldKind.STATUS, title="Записей на сервере", value="128"),
                            f(
                                kind=FieldKind.BUTTON,
                                title="Подключить другой…",
                                key="server_setup",
                            ),
                            f(
                                kind=FieldKind.BUTTON,
                                title="Отключить",
                                key="server_off",
                                role=Role.DESTRUCTIVE,
                            ),
                        ],
                    )
                ],
            ),
            SettingsTab(
                id="general",
                title="Общие",
                symbol="gearshape",
                sections=[
                    SettingsSection(
                        fields=[
                            f(
                                kind=FieldKind.HOTKEY,
                                title="Начать или остановить запись",
                                key="hotkey",
                                value="ctrl+opt+R",
                                placeholder="Нажмите сочетание",
                            ),
                            f(
                                kind=FieldKind.TOGGLE,
                                title="Скрывать запись в строке меню",
                                key="discreet",
                            ),
                            f(
                                kind=FieldKind.TOGGLE,
                                title="Открывать при входе в систему",
                                key="autostart",
                                checked=True,
                            ),
                            f(kind=FieldKind.BUTTON, title="Завершить Mnema", key="quit"),
                        ]
                    ),
                    _updates(update),
                ],
            ),
        ]
    )
    if not idle:  # as settings_model(idle=False): Recording except the Model row, and server
        buttons = ("server_setup", "server_off")
        for tab in model.tabs:
            for field in (x for s in tab.sections for x in s.fields):
                if (tab.id == "record" and field.kind != FieldKind.STATUS) or field.key in buttons:
                    field.enabled = False
                    for item in field.choices:
                        item.enabled = False
    return model


def _menu(d: str, *items: tuple[str, str], divider: tuple[str, ...] = ()) -> list[Button]:
    """A card menu of (action, title); divider: the actions with a separator before them."""
    return [
        Button(
            key=f"record:{d}:{action}",
            title=title,
            role=Role.DESTRUCTIVE if action == "trash" else Role.NORMAL,
            divider=action in divider,
        )
        for action, title in items
    ]


def _with_move(menu: list[Button], d: str, section: str = "") -> list[Button]:
    """ "Move to section" after "Rename…": the root, the sections, "New section…"."""
    choices = [
        Button(key=f"record:{d}:move:{name}", title=name or "Без раздела", checked=name == section)
        for name in ("", "Работа")
    ]
    choices.append(Button(key=f"record:{d}:new_section", title="Новый раздел…", divider=True))
    move = Button(key=f"record:{d}:move", title="Переместить в раздел", children=choices)
    at = next(i for i, b in enumerate(menu) if b.key.endswith(":rename")) + 1
    return [*menu[:at], move, *menu[at:]]


def _cards_sample() -> list[Card]:
    a = "00000000-0000-4000-8000-000000000001"
    d = "00000000-0000-4000-8000-000000000002"
    b = "00000000-0000-4000-8000-000000000003"
    u = "00000000-0000-4000-8000-000000000004"
    opens = (("summary", "Открыть пересказ"), ("transcript", "Открыть расшифровку"))
    folder, rename = ("folder", "Открыть папку"), ("rename", "Переименовать…")
    trash = ("trash", "Удалить запись")
    gist = (
        "Обсудили сроки задач спринта: договорились сначала закончить тесты, а "
        "рефакторинг перенести на следующую неделю."
    )
    return [
        Card(
            id=a,
            section="Работа",
            title="Weekly team sync",
            subtitle="Сегодня, 9:15 · 14 мин",
            gist=gist,
            detail=gist,
            tooltip=f"{gist}\nЯзык: русский",
            open_key=f"record:{a}:folder",
            menu=_with_move(
                _menu(
                    a,
                    *opens,
                    folder,
                    rename,
                    ("process", "Обработать заново…"),
                    trash,
                    divider=("rename", "trash"),
                ),
                a,
                "Работа",
            ),
        ),
        Card(
            id=d,
            title="Вчера, 13:05",
            subtitle="3 ч 58 мин",
            warn=True,
            remarks=["Дневной лимит модели", "Без названия, без пересказа"],
            detail="Без пересказа",
            detail_warn=True,
            tooltip="Дневной лимит модели\nБез названия, без пересказа\nЯзык: русский",
            open_key=f"record:{d}:folder",
            menu=_with_move(
                _menu(
                    d,
                    opens[1],
                    folder,
                    rename,
                    ("summarize", "Составить пересказ"),
                    ("process", "Обработать заново…"),
                    trash,
                    divider=("rename", "trash"),
                ),
                d,
            ),
        ),
        Card(
            id=b,
            title="Release planning 2.0",
            subtitle="Вчера, 9:00 · 16 мин",
            gist=("Agreed to ship 2.0 at the end of November and to fix the open bugs first."),
            warn=True,
            remarks=["Модель не ответила"],
            detail=("Agreed to ship 2.0 at the end of November and to fix the open bugs first."),
            tooltip=(
                "Agreed to ship 2.0 at the end of November and to fix the open bugs "
                "first.\nМодель не ответила\nЯзык: английский"
            ),
            open_key=f"record:{b}:folder",
            menu=_with_move(
                _menu(b, *opens, folder, rename, trash, divider=("rename", "trash")), b
            ),
        ),
        Card(
            id=u,
            title="28 сент., 14:05",
            subtitle="47 мин",
            status="Не обработана",
            detail="Не обработана",
            detail_warn=True,
            open_key=f"record:{u}:folder",
            buttons=[Button(key=f"record:{u}:process", title="Обработать…")],
            menu=_with_move(
                _menu(
                    u,
                    folder,
                    ("process", "Обработать…"),
                    rename,
                    trash,
                    divider=("process", "trash"),
                ),
                u,
            ),
        ),
    ]


def _sources(*, recording: bool, paused: bool = False) -> list[Source]:
    if not recording:
        return [
            Source(
                label="mic",
                title="Микрофон",
                symbol="mic.fill",
                symbol_off="mic.slash.fill",
                on=True,
                key="toggle_mic",
            ),
            Source(
                label="system",
                title="Системный звук",
                symbol="speaker.wave.2.fill",
                symbol_off="speaker.slash.fill",
                on=True,
                key="toggle_system",
            ),
        ]
    return [
        Source(
            label="mic",
            title="Микрофон",
            symbol="mic.fill",
            symbol_off="mic.slash.fill",
            on=True,
            key="source:mic",
            levels=[] if paused else [0.2, 0.5, 0.8, 0.6, 0.4, 0.7],
        ),
        Source(
            label="system",
            title="Системный звук",
            symbol="speaker.wave.2.fill",
            symbol_off="speaker.slash.fill",
            on=True,
            key="source:system",
            levels=[] if paused else [0.0] * 6,
        ),
    ]


def _footer() -> list[Button]:
    """The panel's "More" menu: library commands, a separator, quit."""
    return [
        Button(key="open_folder", title="Открыть папку записей", symbol="folder"),
        Button(key="process_folder", title="Обработать запись…", symbol="waveform.badge.plus"),
        Button(key="quit", title="Завершить Mnema", symbol="power", divider=True),
    ]


def _processing_cards() -> list[Card]:
    """A recording being processed (step and bar, a menu with language and stop) and one
    waiting behind it."""
    r = "00000000-0000-4000-8000-000000000005"
    q = "00000000-0000-4000-8000-000000000006"
    langs = [
        Button(key=f"record:{r}:lang:{k}", title=t, checked=k == "ru")
        for k, t in LANGS
        if k != "auto"
    ]
    running = Card(
        id=r,
        title="Сегодня, 10:05",
        subtitle="42 мин",
        status="Распознаю речь… 40\u00a0%",
        detail="Распознаю речь… 40\u00a0%",
        open_key=f"record:{r}:folder",
        busy=True,
        progress=0.4,
        menu=[
            Button(key=f"record:{r}:folder", title="Открыть папку"),
            Button(
                key=f"record:{r}:lang",
                title="Язык: русский",
                divider=True,
                children=langs,
            ),
            Button(key=f"record:{r}:title", title="Задать название…"),
            Button(key=f"record:{r}:stop", title="Остановить обработку", divider=True),
        ],
    )
    waiting = Card(
        id=q,
        title="Сегодня, 11:20",
        subtitle="8 мин",
        status="В очереди",
        detail="В очереди",
        open_key=f"record:{q}:folder",
        menu=_with_move(
            _menu(
                q,
                ("folder", "Открыть папку"),
                ("rename", "Переименовать…"),
                ("stop", "Убрать из очереди"),
                ("trash", "Удалить запись"),
                divider=("rename", "trash"),
            ),
            q,
        ),
    )
    return [waiting, running]


def _panel(**kw: Any) -> PanelModel:
    base: dict[str, Any] = {
        "where": "Дом",
        "where_state": "ok",
        "cards": _cards_sample(),
        "footer": _footer(),
        "settings": _settings_sample(),
        "hotkey": "ctrl+opt+R",
    }
    return PanelModel(**(base | kw))


def _recording(paused: bool) -> PanelModel:
    buttons = [
        Button(
            key="resume" if paused else "pause",
            title="Продолжить" if paused else "Пауза",
            symbol="play.fill" if paused else "pause.fill",
        ),
        Button(key="stop", title="Остановить", role=Role.PRIMARY, symbol="stop.fill"),
        Button(key="discard", title="Отменить запись…", role=Role.DESTRUCTIVE),
        Button(
            key="discreet",
            title="Скрывать запись в строке меню",
            symbol="eye.slash",
            checked=False,
            compact=True,
        ),
    ]
    return _panel(
        settings=_settings_sample(idle=False),
        screen="recording",
        status=StatusItem(
            icon="paused" if paused else "warning", text="12:34", tooltip="Mnema: идёт запись"
        ),
        recording=RecordingView(
            heading="Пауза" if paused else "Идёт запись",
            timer="12:34",
            paused=paused,
            sources=_sources(recording=True, paused=paused),
            buttons=buttons,
        ),
    )


_SUBMITTED = {"connection": "http://srv:8000#invite=test-invite"}


def _connection_form(**kw: Any) -> FormAsk:
    return FormAsk(
        title="Подключение к серверу",
        fields=[FormField(id="connection", label="Приглашение", placeholder="http://srv:8000")],
        ok="Проверить и сохранить",
        **kw,
    )


_MODEL_TITLE = "Модель для пересказа"


def _model_form(fields: list[FormField], *, ok: str = "Далее", **kw: Any) -> FormAsk:
    """The summary model window at one of its steps: provider, key, model."""
    return FormAsk(title=_MODEL_TITLE, fields=fields, ok=ok, **kw)


def _model_provider() -> FormAsk:
    from ..llm.providers import TITLES, Provider

    field = FormField(
        id="provider",
        label="Провайдер",
        kind=FormFieldKind.CHOICE,
        choices=[
            Choice(key=p.value, title=TITLES[p], checked=p == Provider.GEMINI) for p in Provider
        ],
    )
    return _model_form([field], message="Сейчас: gemini/gemini-flash-latest, ключ …a1B2")


def _model_key() -> FormAsk:
    field = FormField(id="key", label="Ключ API", placeholder="Оставить …a1B2")
    return _model_form([field], message="Где взять ключ Gemini: aistudio.google.com/apikey")


def _model_choice() -> FormAsk:
    """The Model step for OpenRouter: free models first, the recommended one chosen."""
    free = ["qwen/qwen3-235b-a22b:free", "deepseek/deepseek-chat-v3.1:free"]
    paid = ["anthropic/claude-sonnet-5", "openai/gpt-5.5"]
    field = FormField(
        id="model",
        label="Модель",
        kind=FormFieldKind.CHOICE,
        choices=[
            *(Choice(key=m, title=m, detail="бесплатная", checked=m == free[0]) for m in free),
            *(Choice(key=m, title=m) for m in paid),
        ],
    )
    return _model_form([field], rev=3, ok="Проверить и сохранить")


def samples() -> dict[str, BaseModel]:
    """Messages of both directions: Fixtures file name → message. Built in Russian whatever the
    system language, so `python -m mnema.bar.protocol` always writes the same Fixtures."""
    with using("ru"):
        return _samples()


def _samples() -> dict[str, BaseModel]:
    idle = IdleView(
        start=Button(key="start", title="Начать запись", role=Role.PRIMARY, symbol="record.circle"),
        sources=_sources(recording=False),
    )
    section = SettingsField(
        kind=FieldKind.CHOICE,
        title="Раздел",
        value="Работа",
        choices=[
            Choice(key="record_section:", title="Без раздела"),
            Choice(key="record_section:Работа", title="Работа", checked=True),
        ],
    )
    return {
        "out-state-idle": StateMsg(
            panel=_panel(
                screen="idle",
                status=StatusItem(icon="idle"),
                idle=idle.model_copy(update={"section": section}),
                sections=["Работа"],
                cleanup=Button(
                    key="cleanup_audio",
                    title="Удалить звук обработанных записей…",
                    symbol="waveform",
                ),
                notices=[
                    Notice(
                        kind="info",
                        text="Доступна Mnema 0.3.0",
                        buttons=[Button(key="update", title="Обновить")],
                    )
                ],
            )
        ),
        "out-state-updating": StateMsg(  # the download of a new version: a bar under the text
            panel=_panel(
                screen="idle",
                status=StatusItem(icon="idle"),
                idle=idle,
                notices=[
                    Notice(kind="info", text="Скачиваю Mnema 0.3.0… 42\u00a0%", progress=0.42)
                ],
                settings=_settings_sample(
                    update=SettingsField(
                        kind=FieldKind.STATUS,
                        title="Mnema 0.2.0",
                        value="Скачиваю Mnema 0.3.0… 42\u00a0%",
                        progress=0.42,
                    )
                ),
            )
        ),
        "out-state-empty": StateMsg(
            panel=_panel(
                screen="idle",
                status=StatusItem(icon="warning"),
                idle=idle,
                cards=[],
                notices=[
                    Notice(
                        text="Не удалось переименовать запись. Подробности в журнале Mnema",
                        buttons=[
                            Button(
                                key="dismiss_notice", title="Закрыть", symbol="xmark", compact=True
                            )
                        ],
                    )
                ],
            )
        ),
        "out-state-recording": StateMsg(panel=_recording(paused=False)),
        "out-state-paused": StateMsg(panel=_recording(paused=True)),
        "out-state-processing": StateMsg(  # idle on top, processing in the list
            panel=_panel(
                screen="idle",
                status=StatusItem(icon="saving", tooltip="Mnema: обработка записи"),
                idle=idle,
                cards=[*_processing_cards(), *_cards_sample()],
            )
        ),
        "out-state-recording-queue": StateMsg(  # a new recording while the last one is processed
            panel=_recording(paused=False).model_copy(
                update={"cards": [*_processing_cards()[1:], *_cards_sample()]}
            )
        ),
        "out-ask-title": AskMsg(id=1, ask=TitleAsk(value="Дейли")),
        "out-ask-section": AskMsg(id=9, ask=new_section_ask()),
        "out-ask-confirm": AskMsg(
            id=2,
            ask=ConfirmAsk(
                title="Отменить запись?",
                message="Звук не сохранится.",
                ok="Отменить запись",
                cancel="Продолжить запись",
            ),
        ),
        "out-ask-folder": AskMsg(id=3, ask=FolderAsk(start=_HOME)),
        "out-ask-range": AskMsg(
            id=4,
            ask=RangeAsk(
                length=5560.8,
                subtitle="Дейли · 27.09 · 1:32:40",
                name="Дейли",
                replace=True,
                asr="whisper",
                asrs=[
                    Choice(key="auto", title="Автоматически"),
                    Choice(key="gigaam", title="GigaAM"),
                    Choice(key="whisper", title="Whisper"),
                    Choice(
                        key="groq/whisper-large-v3-turbo", title="Groq · whisper-large-v3-turbo"
                    ),
                ],
                sources=[
                    Choice(key="mic", title="Микрофон", checked=True),
                    Choice(key="system", title="Системный звук"),  # a silent track
                ],
            ),
        ),
        "out-ask-form-server": AskMsg(id=7, ask=_connection_form()),
        "out-form-busy": FormMsg(id=7, ask=_connection_form(rev=3, busy="Проверяю…")),
        "out-form-error": FormMsg(
            id=7,
            ask=_connection_form(rev=3, error="Сервер не отвечает", force="Сохранить всё равно"),
        ),
        "out-form-close": FormMsg(id=7, ask=None),
        "out-ask-form-model-provider": AskMsg(id=8, ask=_model_provider()),
        "out-ask-form-model-key": AskMsg(id=8, ask=_model_key()),
        "out-form-model-model": FormMsg(id=8, ask=_model_choice()),
        "out-notify": NotifyMsg(
            title="Запись обработана",
            message="Дейли · 30.09",
            open_key=f"open:{_HOME}/2026-09-30_Дейли/transcript.txt",
        ),
        "out-settings": SettingsMsg(tab="server"),
        "out-quit": QuitMsg(),
        "in-action": ActionMsg(key="start"),
        "in-form-submit": FormEventMsg(id=7, rev=3, op=FormOp.SUBMIT, values=_SUBMITTED),
        "in-form-force": FormEventMsg(id=7, rev=4, op=FormOp.FORCE, values=_SUBMITTED),
        "in-form-model": FormEventMsg(
            id=8, rev=4, op=FormOp.SUBMIT, values={"model": "qwen/qwen3-235b-a22b:free"}
        ),
        "in-answer-cancel": AnswerMsg(id=2, value=None),
        "in-panel": PanelMsg(open=True),
    }


def write_fixtures(folder: Path = FIXTURES) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    for name, msg in samples().items():
        path = folder / f"{name}.json"
        path.write_text(
            json.dumps(msg.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        written.append(path)
    return written


if __name__ == "__main__":
    for p in write_fixtures(Path(sys.argv[1]) if len(sys.argv) > 1 else FIXTURES):
        print(p)
