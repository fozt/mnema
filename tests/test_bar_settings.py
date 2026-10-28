"""Settings as one model (mnema/bar/settings_model.py): the macOS and Linux settings windows are
built from it; the shortcut and summary language are BarRuntime.handle actions."""

from __future__ import annotations

import json
import queue
import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from dotenv import dotenv_values

from mnema import __version__
from mnema.bar.forms import FormEvent, ServerFlow
from mnema.bar.protocol import (
    FieldKind,
    FormAsk,
    FormOp,
    Role,
    SettingsField,
    SettingsModel,
    Tone,
)
from mnema.bar.runtime import BarRuntime
from mnema.bar.settings_model import HOTKEY_BUSY, HOTKEY_INVALID, settings_model
from mnema.bar.state import (
    BarPrefs,
    MenuEnv,
    Phase,
    Where,
    asr_options,
    discreet_title,
    load_prefs,
    save_prefs,
    valid_hotkey,
)
from mnema.i18n import using
from mnema.install_setup import NEW_INVITE, PASTE_INVITE
from mnema.llm import providers

HOTKEYS = json.loads(
    (Path(__file__).parent / "fixtures/hotkeys-v1.json").read_text(encoding="utf-8")
)


def _fields(model: SettingsModel) -> list[SettingsField]:
    return [f for t in model.tabs for s in t.sections for f in s.fields]


def _field(model: SettingsModel, title: str) -> SettingsField:
    return next(f for f in _fields(model) if f.title == title)


@pytest.fixture
def rt(monkeypatch, tmp_path) -> BarRuntime:
    monkeypatch.setenv("MNEMA_LIVE_DIR", str(tmp_path / "notes"))
    return BarRuntime(prefs_file=tmp_path / "bar.json")


_ASR = [("auto", "Автоматически"), ("parakeet", "Parakeet"), ("gigaam", "GigaAM"),
        ("whisper", "Whisper")]  # fmt: skip
HOME = MenuEnv(
    mac=True, server="http://srv:8123", server_ok=True, server_name="Дом", server_records=128,
    model="gemini/gemini-flash-latest", asr_choices=_ASR,
)  # fmt: skip


@pytest.mark.parametrize("idle", [True, False], ids=["idle", "busy"])
def test_mac_settings_are_what_the_app_fixtures_show(idle) -> None:
    """The Mac's Recording and Server tabs with the server "Дом" and a model match the Fixtures
    Swift draws the settings and checks the layout from."""
    from mnema.bar.protocol import _settings_sample

    built = settings_model(BarPrefs(hotkey="ctrl+opt+R", autostart=True), HOME, idle=idle)
    sample = _settings_sample(idle=idle)
    assert [t.id for t in built.tabs] == ["record", "server", "general"]
    assert built.tabs[:2] == sample.tabs[:2]


def test_record_tab_and_server_buttons_locked_while_busy() -> None:
    """While recording or processing the Recording tab is unavailable with its options (handle does
    not change it then), and so are the server buttons; the Model row, the server state, the
    shortcut, hiding the recording, autostart and quit always stay."""
    prefs = BarPrefs()
    idle = _fields(settings_model(prefs, HOME))
    assert [f.key for f in idle if not f.enabled] == []
    busy = _fields(settings_model(prefs, HOME, idle=False))
    assert [f.title for f in busy if not f.enabled] == [
        "Микрофон", "Системный звук", "Где обрабатывать", "Распознавание", "Выбрать модель…",
        "Составлять пересказ", "Язык пересказа", "Подключить другой…", "Отключить",
    ]  # fmt: skip
    assert [c.key for f in busy for c in f.choices if c.enabled] == []
    assert [f.title for f in busy if f.enabled] == [
        "Модель", "Дом", "Записей на сервере",
        "Начать или остановить запись",
        "Скрывать запись в строке меню", "Открывать при входе в систему", "Завершить Mnema",
        f"Mnema {__version__}", "Проверить обновления",
    ]  # fmt: skip


def test_model_row_and_choice_only_on_the_mac() -> None:
    """On the Mac its own model summarizes: the Model row and "Choose model…" above the toggle,
    with no note on it; Linux summarizes with the server's model, with no row or button and the
    note "Model not set up"."""

    def processing(env: MenuEnv) -> list[tuple]:
        [tab] = [t for t in settings_model(BarPrefs(), env).tabs if t.id == "record"]
        [section] = [s for s in tab.sections if s.title == "Обработка"]
        return [(f.title, f.value, f.detail, f.tone, f.key) for f in section.fields]

    assert processing(MenuEnv(mac=True))[:5] == [
        ("Где обрабатывать", "на этом Mac", "", Tone.NONE, None),
        ("Распознавание", "Автоматически", "", Tone.NONE, None),
        ("Модель", "Не настроена", "", Tone.WARN, None),
        ("Выбрать модель…", "", "", Tone.NONE, "model_setup"),
        ("Составлять пересказ", "", "", Tone.NONE, "summary"),
    ]
    assert processing(HOME)[2] == ("Модель", "gemini/gemini-flash-latest", "", Tone.OK, None)
    linux = processing(MenuEnv(server="http://srv:8123"))
    assert [row[0] for row in linux] == ["Распознавание", "Составлять пересказ", "Язык пересказа"]
    assert linux[1][2] == "Модель не настроена"
    assert processing(MenuEnv(server="http://srv:8123", server_llm=True))[1][2] == ""


def test_server_states() -> None:
    """Connection: the server by name with the address as a note; the recordings of the last sync
    with it; "Connect…" without a server and after a token refusal, otherwise "Connect another…".
    """

    def server(env: MenuEnv) -> list[tuple]:
        [tab] = [t for t in settings_model(BarPrefs(), env).tabs if t.id == "server"]
        return [(f.kind, f.title, f.value, f.detail, f.key, f.role) for f in tab.sections[0].fields]

    status, button = FieldKind.STATUS, FieldKind.BUTTON
    for env in (MenuEnv(), MenuEnv(mac=True)):
        assert server(env) == [
            (status, "Сервер", "Не подключён", "", None, Role.NORMAL),
            (button, "Подключить…", "", "", "server_setup", Role.NORMAL),
        ]
    assert server(HOME) == [
        (status, "Дом", "Подключён", "srv:8123", None, Role.NORMAL),
        (status, "Записей на сервере", "128", "", None, Role.NORMAL),
        (button, "Подключить другой…", "", "", "server_setup", Role.NORMAL),
        (button, "Отключить", "", "", "server_off", Role.DESTRUCTIVE),
    ]
    unnamed = server(MenuEnv(server="http://srv:8123/", server_ok=True))
    assert unnamed[0][1:4] == ("srv:8123", "Подключён", "") and len(unnamed) == 3
    down = server(MenuEnv(server="https://srv", server_name="Дом"))
    assert (down[0][2], down[1][1]) == ("Недоступен", "Подключить другой…")
    refused = server(MenuEnv(server="https://srv", server_name="Дом", server_auth=False))
    assert (refused[0][2], refused[1][1]) == ("Нужно новое приглашение", "Подключить…")
    tones = [
        settings_model(BarPrefs(), env).tabs[1].sections[0].fields[0].tone
        for env in (
            MenuEnv(), MenuEnv(mac=True), MenuEnv(server="http://s", server_ok=True),
            MenuEnv(server="http://s"), MenuEnv(server="http://s", server_auth=False),
        )
    ]  # fmt: skip
    assert tones == [Tone.WARN, Tone.NONE, Tone.OK, Tone.WARN, Tone.ERROR]


def _checkpoint(server: str, count: int) -> None:
    """sync.json of the last sync with server: count recordings."""
    from uuid import uuid4

    from mnema.sync.state import Checkpoint, checkpoint_path, save

    ids = [str(uuid4()) for _ in range(count)]
    records = {
        i: {"id": i, "path": f"Запись {n}", "files": {}, "tag": "t"} for n, i in enumerate(ids)
    }
    save(checkpoint_path(), Checkpoint.model_validate({"server": server, "records": records}))


def test_records_on_server_only_from_the_sync_with_this_server(monkeypatch, tmp_path) -> None:
    """Recordings on the server is the count of the last sync when it was with the connected
    server; another server or none, no row."""
    rt = BarRuntime(prefs_file=tmp_path / "bar.json")
    _checkpoint("http://srv:8123", 3)
    assert rt.menu_env().server_records is None  # no server connected
    monkeypatch.setenv("MNEMA_RUNNER", "http://srv:8123")
    assert rt.menu_env().server_records == 3
    _checkpoint("http://srv:8123", 5)  # synced again
    assert rt.menu_env().server_records == 5
    monkeypatch.setenv("MNEMA_RUNNER", "http://other:8000")
    assert rt.menu_env().server_records is None


@pytest.mark.parametrize(
    ("mac", "place"), [(True, "на этом Mac"), (False, "на этом компьютере")], ids=["mac", "linux"]
)
def test_disconnect_asks_first_and_keeps_the_library(
    monkeypatch, tmp_path, env_file, mac, place
) -> None:
    """Disconnect asks first, naming the server; Cancel changes nothing; Disconnect empties the
    address and token in .env (owner only), sync from the previous point stays intact, no
    notification. While recording the button does nothing."""
    from mnema.config import set_env
    from mnema.core import base
    from mnema.sync.state import checkpoint_path

    monkeypatch.setattr(base, "STANDALONE", mac)
    monkeypatch.delenv("MNEMA_RUNNER")
    monkeypatch.delenv("MNEMA_TOKEN")
    set_env({"MNEMA_RUNNER": "http://srv:8123", "MNEMA_TOKEN": "s3cret-token"})
    _checkpoint("http://srv:8123", 2)
    platform = _LiveForms()
    rt = BarRuntime(platform=platform, prefs_file=tmp_path / "bar.json")
    rt.server_name = "Дом"
    rt.state.phase = Phase.RECORDING
    rt.handle("server_off")
    assert platform.confirms == []
    rt.state.phase = Phase.IDLE
    platform.yes = False
    rt.handle("server_off")
    assert platform.confirms == [
        (
            "Отключиться от сервера «Дом»?",
            f"Записи останутся {place} и на сервере. Подключиться снова можно по новому "
            "приглашению с сервера",
            "Отключить",
        )
    ]
    assert rt.server == "http://srv:8123"
    rt.server_name = "Сервер"  # the default name is not repeated
    rt.handle("server_off")
    assert platform.confirms[-1][0] == "Отключиться от сервера?"
    platform.yes = True
    sync = checkpoint_path().read_bytes()
    rt.handle("server_off")
    env = dotenv_values(env_file)
    assert (env["MNEMA_RUNNER"], env["MNEMA_TOKEN"]) == ("", "")
    assert env_file.stat().st_mode & 0o777 == 0o600 and rt.server is None
    assert checkpoint_path().read_bytes() == sync and platform.notes == []
    [tab] = [t for t in settings_model(rt.prefs, rt.menu_env()).tabs if t.id == "server"]
    assert [f.title for f in tab.sections[0].fields] == ["Сервер", "Подключить…"]


def test_disconnect_answer_after_another_server_was_connected_changes_nothing(
    monkeypatch, tmp_path, env_file
) -> None:
    """Another server was connected while the question was open: the Disconnect answer belongs to
    the former one."""
    from mnema.config import set_env

    monkeypatch.delenv("MNEMA_RUNNER")
    set_env({"MNEMA_RUNNER": "http://srv:8123"})
    answers: list[Callable[[bool], None]] = []
    platform = _LiveForms()
    platform.confirm = lambda title, message, ok, on_done: answers.append(on_done)
    rt = BarRuntime(platform=platform, prefs_file=tmp_path / "bar.json")
    rt.handle("server_off")
    set_env({"MNEMA_RUNNER": "http://other:8000"})
    answers[0](True)
    assert dotenv_values(env_file)["MNEMA_RUNNER"] == "http://other:8000"


def _hotkey_field(rt: BarRuntime) -> SettingsField:
    model = settings_model(rt.prefs, MenuEnv(mac=True), hotkey_error=rt.hotkey_error)
    return _field(model, "Начать или остановить запись")


def test_hotkey_actions(rt: BarRuntime, tmp_path) -> None:
    """A shortcut from the settings field: a valid one is remembered; an invalid one (the Mac only
    parses the press) keeps the old one with an error on the field; one taken by the system is
    dropped with an error; an empty one is dropped."""
    prefs_file = tmp_path / "bar.json"
    rt.handle("hotkey:ctrl+opt+R")
    assert rt.prefs.hotkey == "ctrl+opt+R" and load_prefs(prefs_file).hotkey == "ctrl+opt+R"
    for bad in HOTKEYS["invalid"]:
        rt.handle(f"hotkey:{bad}")
        field = _hotkey_field(rt)
        assert (field.value, bool(field.error)) == ("ctrl+opt+R", True), bad
    assert load_prefs(prefs_file).hotkey == "ctrl+opt+R"
    rt.handle("hotkey_busy:cmd+F5")  # a stale answer: what is taken is not what is set now
    assert rt.prefs.hotkey == "ctrl+opt+R"

    rt.handle("hotkey_busy:ctrl+opt+R")
    field = _hotkey_field(rt)
    assert rt.prefs.hotkey is None and load_prefs(prefs_file).hotkey is None
    assert field.value == "" and field.error

    rt.handle("hotkey:cmd+shift+Space")
    assert rt.prefs.hotkey == "cmd+shift+Space" and not _hotkey_field(rt).error
    rt.handle("hotkey:")
    assert rt.prefs.hotkey is None and load_prefs(prefs_file).hotkey is None


def test_hotkey_rule_is_shared_with_the_mac_app() -> None:
    """Which shortcuts the engine accepts is tests/fixtures/hotkeys-v1.json, also read by
    HotKeyTests.swift: a key the engine accepts the Mac has too, and the other way round."""
    assert all(valid_hotkey(f"ctrl+opt+{key}") for key in HOTKEYS["keys"])
    assert not any(valid_hotkey(f"ctrl+opt+{key}") for key in HOTKEYS["unknown"])
    assert all(valid_hotkey(spec) for spec in HOTKEYS["valid"])
    assert not any(valid_hotkey(spec) for spec in HOTKEYS["invalid"])


@pytest.mark.parametrize(
    ("written", "shown"),
    [("shift+R", ""), ("ctrl+ctrl+R", ""), ("R", ""), ("cmd+ctrl+R", "cmd+ctrl+R")],
)
def test_hotkey_written_into_bar_json_by_hand_follows_the_same_rule(tmp_path, written, shown):
    """A shortcut written into bar.json by hand follows the field's rule: an invalid one is neither
    shown nor passed to the Mac (which only parses it)."""
    import json

    path = tmp_path / "bar.json"
    path.write_text(json.dumps({"hotkey": written}))
    field = _field(
        settings_model(load_prefs(path), MenuEnv(mac=True)), "Начать или остановить запись"
    )
    assert field.value == shown


def test_summary_lang_choice(rt: BarRuntime, tmp_path) -> None:
    rt.handle("summary_lang:en")
    assert rt.prefs.summary_lang == "en" and load_prefs(tmp_path / "bar.json").summary_lang == "en"
    assert _field(settings_model(rt.prefs, MenuEnv()), "Язык пересказа").value == ("английский")
    rt.handle("summary_lang:auto")
    assert rt.prefs.summary_lang is None
    rt.state.phase = Phase.RECORDING
    rt.handle("summary_lang:ru")  # the next recording's settings do not change while recording
    assert rt.prefs.summary_lang is None


# windows with a check: address, states


@pytest.fixture
def env_file(monkeypatch, tmp_path):
    """The test's ~/.config/mnema/.env; the window also writes the process environment, which
    teardown restores."""
    for name in ("MNEMA_RUNNER", "MNEMA_TOKEN", "HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("MNEMA_ASR_THREADS", "4")
    monkeypatch.setenv("MNEMA_LIVE_DIR", str(tmp_path / "notes"))  # connecting opens it
    return tmp_path / ".config/mnema/.env"


def _transport(monkeypatch, handler: Callable[[httpx.Request], httpx.Response]) -> None:
    monkeypatch.setattr(providers, "TRANSPORT", httpx.MockTransport(handler))


def _mnema_server(request: httpx.Request) -> httpx.Response:
    """Servers by name: down does not answer, locked is 401, other is not Mnema, srv is Mnema."""
    host = request.url.host
    if host == "down":
        raise httpx.ConnectError("нет связи", request=request)
    if host == "locked":
        return httpx.Response(401)
    if host == "other":
        return httpx.Response(200, json={"service": "other"})
    return httpx.Response(200, json={"ok": True, "llm": False})


def test_server_window_checks_before_saving(monkeypatch, tmp_path, env_file) -> None:
    """The Server window: an invitation or an open server's address, a line with a token is
    refused; saves what passed the check, or on "Save anyway"."""
    _transport(monkeypatch, _mnema_server)
    flow = ServerFlow()
    first = flow.ask()
    assert [(f.id, f.placeholder) for f in first.fields] == [
        ("connection", "Приглашение с сервера"),
    ]
    assert first.ok == "Проверить и сохранить" and not first.force

    def error(connection: str, *, force: bool = False) -> tuple[str, str]:
        ask = flow.submit({"connection": connection}, force=force)
        assert ask is not None
        return ask.error, ask.force

    assert error(" ") == (PASTE_INVITE, "")
    assert error("srv 8000")[1] == ""
    for force in (False, True):
        assert error("srv:8000#token=s3cret-token", force=force) == (PASTE_INVITE, "")
    again = "Сохранить всё равно"
    assert error("down:8000") == ("Сервер не отвечает. Проверьте адрес и запущен ли сервер", again)
    assert error("locked") == (PASTE_INVITE, again)
    assert error("other:8000") == ("По этому адресу нет сервера Mnema", again)
    assert not env_file.exists()

    assert flow.submit({"connection": "srv:8000"}, force=False) is None
    env = dotenv_values(env_file)
    assert (env["MNEMA_RUNNER"], env["MNEMA_TOKEN"]) == ("http://srv:8000", "")
    assert env_file.stat().st_mode & 0o777 == 0o600 and flow.result == (True, True)
    rt = BarRuntime(prefs_file=tmp_path / "bar.json")
    assert rt.menu_env().server == "http://srv:8000"
    assert [f.placeholder for f in ServerFlow().ask().fields] == ["http://srv:8000"]
    assert ServerFlow().submit({"connection": "down"}, force=True) is None
    env = dotenv_values(env_file)
    assert (env["MNEMA_RUNNER"], env["MNEMA_TOKEN"]) == ("http://down", "")


def test_server_window_keeps_the_saved_token_for_the_same_address(
    monkeypatch, tmp_path, env_file
) -> None:
    """A saved device token goes only to the same address: an empty field and the same address are
    checked with it, a refused one gives "New invitation needed". The token never reaches the
    window or the app settings."""
    seen: list[tuple[str, str | None]] = []

    def server(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.headers.get("authorization")))
        return _mnema_server(request)

    _transport(monkeypatch, server)
    monkeypatch.setenv("MNEMA_RUNNER", "http://locked")
    monkeypatch.setenv("MNEMA_TOKEN", "s3cret-token")
    flow = ServerFlow()
    assert "s3cret-token" not in flow.ask().model_dump_json()
    for typed in ("", "locked"):
        ask = flow.submit({"connection": typed}, force=False)
        assert ask is not None and ask.error == NEW_INVITE
    assert seen == [("locked", "Bearer s3cret-token")] * 2
    rt = BarRuntime(prefs_file=tmp_path / "bar.json")
    for idle in (True, False):
        shown = settings_model(rt.prefs, rt.menu_env(), idle=idle).model_dump_json()
        assert "s3cret-token" not in shown
    assert flow.submit({"connection": "locked"}, force=True) is None
    assert dotenv_values(env_file)["MNEMA_TOKEN"] == "s3cret-token"


class _LiveForms:
    """The macOS app with a live form window: every window state goes to a queue."""

    def __init__(self) -> None:
        self.shown: queue.Queue[FormAsk | None] = queue.Queue()
        self.on_event: Callable[[FormEvent | None], None] = lambda ev: None
        self.notes: list[tuple[str, str]] = []
        self.confirms: list[tuple[str, str, str]] = []
        self.yes = True

    def open_form(self, ask: FormAsk, on_event: Callable[[FormEvent | None], None]) -> Any:
        self.on_event = on_event
        self.shown.put(ask)
        return self.shown.put

    def next(self) -> FormAsk | None:
        return self.shown.get(timeout=10)

    def notify(self, title: str, message: str, path: Any) -> None:
        self.notes.append((title, message))

    def confirm(self, title: str, message: str, ok: str, on_done: Callable[[bool], None]) -> None:
        self.confirms.append((title, message, ok))
        on_done(self.yes)

    def open(self, path: Any) -> None: ...

    def reveal(self, path: Any) -> None: ...


def _join(name: str) -> None:
    for t in threading.enumerate():
        if t.name == name:
            t.join(timeout=10)


def test_server_window_busy_then_result_one_check_at_a_time(monkeypatch, tmp_path, env_file):
    """A window event: "Checking…" at once, then the result with the event number; while a check
    runs, other window events are ignored; "Save anyway" saves and closes the window, and the menu
    is reread at once. A window closed during the check gets no result."""
    gate, hosts = threading.Event(), []

    def slow(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        assert gate.wait(10)
        raise httpx.ConnectError("нет связи", request=request)

    _transport(monkeypatch, slow)
    platform = _LiveForms()
    rt = BarRuntime(platform=platform, prefs_file=tmp_path / "bar.json")
    rt.handle("server_setup")
    assert platform.next().title == "Подключение к серверу"
    values = {"connection": "srv:8000"}
    platform.on_event(FormEvent(op=FormOp.SUBMIT, values=values, rev=1))
    busy = platform.next()
    assert busy is not None and (busy.busy, busy.rev, busy.error) == ("Проверяю…", 1, "")
    platform.on_event(FormEvent(op=FormOp.SUBMIT, values=values, rev=2))
    gate.set()
    failed = platform.next()
    assert failed is not None and (failed.rev, failed.busy, failed.force) == (
        1, "", "Сохранить всё равно",
    )  # fmt: skip
    assert failed.error == "Сервер не отвечает. Проверьте адрес и запущен ли сервер"
    assert hosts == ["srv"]
    rev = rt.env_rev
    platform.on_event(FormEvent(op=FormOp.FORCE, values=values, rev=3))
    assert platform.next().busy == "Проверяю…"
    assert platform.next() is None  # saved: close the window
    _join("mnema-bar-form")  # the window closes before the menu environment updates
    assert dotenv_values(env_file)["MNEMA_RUNNER"] == "http://srv:8000" and hosts == ["srv"]
    assert rt.server == "http://srv:8000" and not rt.server_ok and rt.env_rev > rev

    gate.clear()
    rt.handle("server_setup")
    platform.next()
    platform.on_event(FormEvent(op=FormOp.SUBMIT, values={"connection": "other"}, rev=1))
    platform.next()  # "Checking…"
    platform.on_event(None)  # window closed
    gate.set()
    _join("mnema-bar-form")
    assert platform.shown.empty() and dotenv_values(env_file)["MNEMA_RUNNER"] == "http://srv:8000"

    rt.state.phase = Phase.RECORDING
    rt.handle("server_setup")  # no server window while recording
    assert platform.shown.empty()


def test_invite_with_line_breaks_is_refused_before_any_request(monkeypatch, env_file) -> None:
    """An invitation copied with a line break inside is a red line without a request: it never goes
    into a request (or whole into the error text and the log)."""
    sent: list[str] = []
    _transport(monkeypatch, lambda r: sent.append(str(r.url)) or httpx.Response(200, json={}))
    ask = ServerFlow().submit({"connection": "srv:8000#invite=abc%0Adef"}, force=False)
    assert ask is not None and ask.error and "abc" not in ask.error and sent == []


def test_general_tab_checks_and_applies_updates() -> None:
    """General → Updates: what is newer and "Check for updates"; "Update" only when there is
    something to install, and not while recording, processing or updating."""
    from mnema import __version__

    what = "Mnema 0.2.0"
    title = f"Mnema {__version__}"

    def section(env: MenuEnv, idle: bool = True):
        [tab] = [t for t in settings_model(BarPrefs(), env, idle=idle).tabs if t.id == "general"]
        return {f.title: f for f in tab.sections[-1].fields}

    never = section(MenuEnv(mac=True))
    assert never[title].value == "" and "Обновить" not in never
    fresh = section(MenuEnv(mac=True, update_checked=True))
    assert fresh[title].value == "Последняя версия"
    found = section(MenuEnv(mac=True, update=what, update_checked=True))
    status = found[title]
    assert (status.value, status.detail, status.tone) == ("Доступно обновление", what, Tone.WARN)
    assert found["Обновить"].key == "update" and found["Обновить"].enabled
    assert not section(MenuEnv(mac=True, update=what), idle=False)["Обновить"].enabled
    going = section(MenuEnv(mac=True, update=what, update_step="Готовлю новую версию…"))
    assert going[title].value == "Готовлю новую версию…"
    assert not going["Обновить"].enabled and not going["Проверить обновления"].enabled
    unavailable = section(MenuEnv(update_checked=True, update_error="Канал ещё не опубликован"))
    assert unavailable[title].value == "Не удалось проверить"
    assert unavailable[title].detail == "Канал ещё не опубликован"


def test_processing_tab_offers_local_and_cloud_speech_models() -> None:
    """Linux recognition: the list comes from the server; the saved choice shows while the server
    is unavailable too."""
    env = MenuEnv(
        asr_choices=[
            ("auto", "Автоматически"),
            ("parakeet", "Parakeet"),
            ("gigaam", "GigaAM"),
            ("whisper", "Whisper"),
            ("groq/whisper-large-v3-turbo", "Groq · whisper-large-v3-turbo"),
        ],
    )
    field = _field(settings_model(BarPrefs(), env), "Распознавание")
    assert [(c.key, c.title) for c in field.choices] == [
        ("asr:auto", "Автоматически"), ("asr:parakeet", "Parakeet"), ("asr:gigaam", "GigaAM"),
        ("asr:whisper", "Whisper"),
        ("asr:groq/whisper-large-v3-turbo", "Groq · whisper-large-v3-turbo"),
    ]  # fmt: skip
    assert field.value == "Автоматически"
    gone = _field(settings_model(BarPrefs(asr="openai/whisper-1"), MenuEnv()), "Распознавание")
    assert gone.value == "OpenAI · whisper-1" and gone.choices[-1].checked


def test_speech_model_choice_is_remembered(tmp_path) -> None:
    """The recognition choice is remembered between launches (bar.json)."""
    from mnema.bar.runtime import BarRuntime

    rt = BarRuntime(prefs_file=tmp_path / "bar.json")
    rt.handle("asr:whisper")
    assert BarRuntime(prefs_file=tmp_path / "bar.json").prefs.asr == "whisper"


@pytest.mark.parametrize(
    ("missing", "configured"),
    [
        (MenuEnv(mac=True), MenuEnv(mac=True, model="gemini/gemini-flash-latest")),  # own model
        (MenuEnv(model="gemini/gemini-flash-latest"), MenuEnv(server_llm=True)),  # server's
    ],
    ids=["mac", "linux"],
)
def test_summary_settings_require_a_model_but_preserve_preference(missing, configured):
    """Summaries come from this Mac's model, on Linux from the server's; without one the toggle is
    unavailable and the choice in bar.json stays."""
    prefs = BarPrefs(summary=True)
    toggle = _field(settings_model(prefs, missing), "Составлять пересказ")
    assert not toggle.enabled and not toggle.checked
    assert not _field(settings_model(prefs, missing), "Язык пересказа").enabled
    toggle = _field(settings_model(prefs, configured), "Составлять пересказ")
    assert toggle.enabled and toggle.checked
    assert _field(settings_model(prefs, configured), "Язык пересказа").enabled
    off = settings_model(BarPrefs(summary=False), configured)
    assert not _field(off, "Язык пересказа").enabled  # summaries off


def test_server_token_survives_configuration_reload(monkeypatch, env_file):
    """A device token with characters like ${SHELL} goes into .env as is, without substitution."""
    from mnema.config import Settings, load_env

    token = "literal-${SHELL}-token"

    def node(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/devices/claim":
            return httpx.Response(200, json={"token": token, "device": {"id": "d1"}})
        return _mnema_server(request)

    _transport(monkeypatch, node)
    assert ServerFlow().submit({"connection": "http://srv#invite=code"}, force=False) is None
    monkeypatch.delenv("MNEMA_TOKEN")
    load_env()
    assert Settings().token == token


def test_quit_keeps_settings_written_while_running(rt: BarRuntime, tmp_path) -> None:
    """Settings changed while the app was running (the installer, another window): quitting does
    not bring back the old ones from the app's memory."""
    path = tmp_path / "bar.json"
    outside = load_prefs(path)
    outside.asr = "whisper"
    save_prefs(outside, path)
    rt.handle("quit")
    assert rt.quit_requested and load_prefs(path).asr == "whisper"


def _home_server(monkeypatch) -> None:
    """The connected server "Дом" with a model and cloud recognition, through MockTransport."""
    import mnema.client as client_mod

    real = client_mod.MnemaClient
    answers = {
        "/api/capabilities": {"ok": True, "llm": True, "name": "Дом"},
        "/api/node/settings": {"name": "Дом", "model": "deepseek/deepseek-chat", "engines": True},
        "/api/node/asr": {
            "choices": [
                {"id": "auto", "title": "Автоматически"},
                {"id": "groq/whisper-large-v3", "title": "Groq · whisper-large-v3"},
            ]
        },
    }
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=answers[r.url.path]))
    monkeypatch.setattr(
        client_mod,
        "MnemaClient",
        lambda url, *, token=None: real(url, token=token, transport=transport),
    )


def _processing(rt: BarRuntime) -> dict[str, SettingsField]:
    [tab] = [t for t in settings_model(rt.prefs, rt.menu_env()).tabs if t.id == "record"]
    [section] = [s for s in tab.sections if s.title == "Обработка"]
    return {f.title: f for f in section.fields}


def _settled() -> None:
    for name in ("mnema-bar-ping", "mnema-bar-asr-choices"):
        _join(name)


def test_mac_switches_where_to_process(monkeypatch, tmp_path) -> None:
    """Where to process on the Mac: On the server goes to bar.json, recognition from the server's
    list with cloud models, the Model row shows the server's model without "Choose model…"; On this
    Mac turns it all back. The choice does not change while recording."""
    from mnema.core import base

    monkeypatch.setattr(base, "STANDALONE", True)
    monkeypatch.setenv("MNEMA_LIVE_DIR", str(tmp_path / "notes"))
    monkeypatch.setenv("MNEMA_RUNNER", "http://srv:8123")
    _home_server(monkeypatch)
    rt = BarRuntime(prefs_file=tmp_path / "bar.json")
    _settled()
    rows = _processing(rt)
    where = rows["Где обрабатывать"]
    assert [(c.key, c.title, c.checked, c.enabled) for c in where.choices] == [
        ("where:mac", "На этом Mac", True, True),
        ("where:server", "На сервере", False, True),
    ]
    assert rows["Модель"].value == "anthropic/claude-sonnet-5"  # this Mac's model
    assert "Выбрать модель…" in rows
    assert "asr:groq/whisper-large-v3" not in [c.key for c in rows["Распознавание"].choices]

    rt.state.phase = Phase.RECORDING
    rt.handle("where:server")
    assert rt.prefs.where == "mac"
    rt.state.phase = Phase.IDLE
    rt.handle("where:server")
    _settled()
    assert json.loads((tmp_path / "bar.json").read_text(encoding="utf-8"))["where"] == "server"
    rows = _processing(rt)
    assert rows["Где обрабатывать"].value == "на сервере"
    assert [c.key for c in rows["Распознавание"].choices] == [
        "asr:auto",
        "asr:groq/whisper-large-v3",
    ]
    assert (rows["Модель"].value, rows["Модель"].tone) == ("deepseek/deepseek-chat", Tone.OK)
    assert "Выбрать модель…" not in rows
    assert rows["Составлять пересказ"].enabled
    assert rt.processing_connection() is not None

    rt.handle("asr:groq/whisper-large-v3")
    rt.handle("where:mac")
    _settled()
    rows = _processing(rt)
    assert rows["Где обрабатывать"].value == "на этом Mac" and "Выбрать модель…" in rows
    assert rt.prefs.asr == "auto"  # only the server recognizes in the cloud
    assert "asr:groq/whisper-large-v3" not in [c.key for c in rows["Распознавание"].choices]
    assert rt.processing_connection() is None


def test_mac_server_without_a_model_and_without_a_server() -> None:
    """On the server, and the server has no model: "Model · Not set up on the server", summaries
    unavailable; no server: On the server is unavailable and processing stays on this Mac."""
    prefs = BarPrefs(where="server")
    rows = {f.title: f for f in _fields(settings_model(prefs, HOME.model_copy(update={
        "server_llm": False})))}  # fmt: skip
    assert (rows["Модель"].value, rows["Модель"].tone) == ("Не настроена на сервере", Tone.WARN)
    assert not rows["Составлять пересказ"].enabled and "Выбрать модель…" not in rows
    alone = _field(settings_model(prefs, MenuEnv(mac=True)), "Где обрабатывать")
    assert alone.value == "на этом Mac"
    assert [(c.title, c.checked, c.enabled, c.detail) for c in alone.choices] == [
        ("На этом Mac", True, True, ""),
        ("На сервере", False, False, "Сервер не подключён"),
    ]
    linux = settings_model(BarPrefs(), MenuEnv(server="http://srv:8123", server_name="Дом"))
    assert "Где обрабатывать" not in [f.title for f in _fields(linux)]


def test_disconnect_returns_processing_to_this_mac(monkeypatch, tmp_path, env_file) -> None:
    """Disconnect with On the server chosen turns the choice back to On this Mac, in bar.json
    too."""
    from mnema.config import set_env
    from mnema.core import base

    monkeypatch.setattr(base, "STANDALONE", True)
    set_env({"MNEMA_RUNNER": "http://srv:8123", "MNEMA_TOKEN": "s3cret-token"})
    _home_server(monkeypatch)
    rt = BarRuntime(platform=_LiveForms(), prefs_file=tmp_path / "bar.json")
    rt.handle("where:server")
    _settled()
    assert rt.processing_connection() is not None
    rt.handle("server_off")
    _settled()
    assert rt.prefs.where == "mac" and rt.processing_connection() is None
    assert json.loads((tmp_path / "bar.json").read_text(encoding="utf-8"))["where"] == "mac"
    where = _field(settings_model(rt.prefs, rt.menu_env()), "Где обрабатывать")
    assert [(c.checked, c.enabled) for c in where.choices] == [(True, True), (False, False)]


def test_mac_on_server_while_the_server_is_away(monkeypatch, tmp_path) -> None:
    """A Mac set to On the server started while the server "Дом" did not answer: the Model row does
    not claim there is no model and summaries stay available (processing will ask anyway). Once the
    server answers, the app learns its name, model and recognition list itself; when it goes away
    again, what is known stays."""
    import mnema.bar.runtime as runtime_mod
    import mnema.client as client_mod
    from mnema.core import base

    monkeypatch.setattr(base, "STANDALONE", True)
    monkeypatch.setenv("MNEMA_LIVE_DIR", str(tmp_path / "notes"))
    monkeypatch.setenv("MNEMA_RUNNER", "http://srv:8123")
    save_prefs(BarPrefs(where="server"), tmp_path / "bar.json")
    up = threading.Event()
    answers = {
        "/api/capabilities": {"ok": True, "llm": True, "name": "Дом"},
        "/api/node/settings": {"name": "Дом", "model": "deepseek/deepseek-chat", "engines": True},
        "/api/node/asr": {"choices": [{"id": "groq/whisper-large-v3", "title": "Groq"}]},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if not up.is_set():
            raise httpx.ConnectError("нет сети", request=request)
        return httpx.Response(200, json=answers[request.url.path])

    real = client_mod.MnemaClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        client_mod,
        "MnemaClient",
        lambda url, *, token=None: real(url, token=token, transport=transport),
    )
    rt = BarRuntime(prefs_file=tmp_path / "bar.json")
    rt._update_at = float("inf")  # update checks are not this test's business
    _settled()
    rows = _processing(rt)
    assert rows["Модель"].value != "Не настроена на сервере"
    assert rows["Составлять пересказ"].enabled and rows["Составлять пересказ"].checked

    up.set()
    monkeypatch.setattr(runtime_mod, "_REPING_EVERY", 0.0)
    rt.tick()
    _settled()
    _settled()  # the recognition list comes after the server answers
    rows = _processing(rt)
    assert rows["Где обрабатывать"].value == "на сервере"
    assert (rows["Модель"].value, rows["Модель"].tone) == ("deepseek/deepseek-chat", Tone.OK)
    assert "asr:groq/whisper-large-v3" in [c.key for c in rows["Распознавание"].choices]

    up.clear()
    rt.handle("where:server")  # ask the server again, and it does not answer
    _settled()
    rows = _processing(rt)
    assert not rt.server_ok
    assert rows["Где обрабатывать"].value == "на сервере"
    assert rows["Модель"].value == "deepseek/deepseek-chat"


def test_mac_server_model_name_unknown_is_not_a_missing_model() -> None:
    """The server summarizes (llm) but its model name is unknown: the Model row does not say "Not
    set up on the server" and summaries stay available."""
    env = HOME.model_copy(update={"server_llm": True, "server_model": None})
    rows = {f.title: f for f in _fields(settings_model(BarPrefs(where="server"), env))}
    assert rows["Модель"].value != "Не настроена на сервере"
    assert rows["Модель"].tone != Tone.WARN
    assert rows["Составлять пересказ"].enabled


_CYRILLIC = re.compile(r"[А-Яа-яЁё]")
_HOME_EN = HOME.model_copy(update={"server_name": "Home"})


def _texts(model: SettingsModel) -> list[str]:
    texts = [model.title]
    for tab in model.tabs:
        texts += [tab.title, *(s.title for s in tab.sections)]
        for f in (x for s in tab.sections for x in s.fields):
            texts += [f.title, f.value, f.detail, f.placeholder, f.error]
            texts += [t for c in f.choices for t in (c.title, c.detail)]
    return [t for t in texts if t]


@pytest.mark.parametrize(
    ("env", "prefs"),
    [
        (_HOME_EN, BarPrefs(hotkey="ctrl+opt+R")),
        (MenuEnv(mac=True), BarPrefs()),
        (_HOME_EN, BarPrefs(where=Where.SERVER)),
        (MenuEnv(mac=False), BarPrefs()),
        (MenuEnv(mac=False, server="http://srv:8123", server_auth=False), BarPrefs()),
    ],
    ids=["mac-home", "mac-alone", "mac-on-server", "linux-alone", "linux-new-invite"],
)
def test_settings_render_in_english(env: MenuEnv, prefs: BarPrefs) -> None:
    with using("en"):
        model = settings_model(prefs, env, hotkey_error=HOTKEY_BUSY, idle=False)
        # the discreet toggle and recognition names come from state.py
        foreign = {discreet_title(env.mac), *(t for _k, t in asr_options(env, prefs.asr))}
    texts = [t for t in _texts(model) if t not in foreign]
    assert not [t for t in texts if _CYRILLIC.search(t)]
    assert model.title == "Settings"
    assert [t.title for t in model.tabs] == ["Recording", "Server", "General"]


def test_hotkey_error_is_shown_in_the_interface_language() -> None:
    def error(lang: str) -> str:
        with using(lang):
            model = settings_model(BarPrefs(), HOME, hotkey_error=HOTKEY_INVALID)
        return next(f.error for f in _fields(model) if f.key == "hotkey")

    assert (error("en"), error("ru")) == ("Add ⌃, ⌥ or ⌘", "Добавьте ⌃, ⌥ или ⌘")


def test_forms_render_in_english(env_file) -> None:
    from mnema.bar.forms import ModelFlow, checking

    with using("en"):
        server = ServerFlow().ask()
        flow = ModelFlow()
        provider = flow.ask()
        key = flow.submit({"provider": "gemini"}, force=False)
        busy = checking()
    assert key is not None
    assert (server.title, server.fields[0].label, server.ok) == (
        "Connect to server",
        "Invitation",
        "Check and save",
    )
    assert (provider.title, provider.fields[0].label, provider.ok) == (
        "Summary model",
        "Provider",
        "Next",
    )
    assert key.message.startswith("Get a Gemini key: ") and key.fields[0].label == "API key"
    assert busy == "Checking…"
    # the provider step's message is llm.config.current_line
    for ask in (server, key, provider.model_copy(update={"message": ""})):
        shown = [ask.title, ask.message, ask.ok, ask.cancel]
        shown += [t for f in ask.fields for t in (f.label, f.placeholder)]
        assert not [t for t in shown if _CYRILLIC.search(t)], shown
