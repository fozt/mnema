"""The "Move to section" item of the recording menu (Mac, Linux window and tray): the whole
folder moves into a section or the root, the meta id stays, so sync sees the move by id as from
Finder; "New section…" creates a section by the folder name rules and moves the recording
into it."""

from __future__ import annotations

import functools
from pathlib import Path

import pytest
from test_bar_headless import _line, _Out, _start

from mnema.bar.headless import Channel, Engine
from mnema.bar.panel import panel_model
from mnema.bar.protocol import ActionMsg, AnswerMsg, AskMsg, Button, QuitMsg, StateMsg
from mnema.bar.recordings import find_recording, library
from mnema.bar.runtime import BarRuntime
from mnema.bar.settings_model import settings_model
from mnema.bar.state import BarPrefs, BarState, Job, MenuEnv, Phase, menu
from mnema.live.meta import META_FILE


class _Platform:
    """The "New section" window answers with a prepared name; the rest is a no-op."""

    def __init__(self, answer: str | None = None) -> None:
        self.answer = answer
        self.asked = 0

    def notify(self, title: str, message: str, path: Path | None) -> None: ...

    def open(self, path: Path) -> None: ...

    def ask_section(self, on_done) -> None:
        self.asked += 1
        on_done(self.answer)


@pytest.fixture
def notes(monkeypatch, tmp_path) -> Path:
    """The root: section «Работа» with a recording, an empty section «Личное», one in the root."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    root = tmp_path / "notes"
    for folder, text in (
        (root / "Работа" / "2026-10-01_План", "mic: план\n"),
        (root / "2026-10-02_Итоги", "mic: итоги\n"),
    ):
        folder.mkdir(parents=True)
        (folder / "transcript.txt").write_text(text, encoding="utf-8")
        (folder / "summary.md").write_text("Суть.\n", encoding="utf-8")
    (root / "Личное").mkdir()
    monkeypatch.setenv("MNEMA_LIVE_DIR", str(root))
    return root


def _record_id(root: Path, relative: str) -> str:
    """The list id of the recording <root>/<relative>."""
    return next(c.id for c in library(root).cards if c.path == root / relative)


def _move_item(panel, title: str) -> Button:
    card = next(c for c in panel.cards if c.title == title)
    return next(b for b in card.menu if b.title == "Переместить в раздел")


def _panel(root: Path, state: BarState | None = None):
    view = library(root)
    prefs = BarPrefs()
    return panel_model(
        state or BarState(), prefs, MenuEnv(), view.cards, settings_model(prefs, MenuEnv()),
        sections=view.sections,
    )  # fmt: skip


def test_card_menu_offers_sections_with_the_current_one_checked(notes) -> None:
    """The submenu: "No section", library sections alphabetically, then a divider and "New
    section…"; the recording's section is checked. Available while another recording is made
    or this one waits; a recording being processed has none."""
    panel = _panel(notes)
    plan = _move_item(panel, "План")
    assert [(b.title, b.checked, b.divider) for b in plan.children] == [
        ("Без раздела", False, False),
        ("Личное", False, False),
        ("Работа", True, False),
        ("Новый раздел…", None, True),
    ]
    root_record = _move_item(panel, "Итоги")
    assert [b.title for b in root_record.children if b.checked] == ["Без раздела"]
    assert plan.enabled and root_record.enabled
    assert _move_item(_panel(notes, BarState(phase=Phase.RECORDING)), "План").enabled
    plan_id = _record_id(notes, "Работа/2026-10-01_План")
    waiting = BarState(jobs=[Job(id=plan_id, folder=notes / "Работа" / "2026-10-01_План")])
    assert _move_item(_panel(notes, waiting), "План").enabled
    running = waiting.model_copy(
        update={"jobs": [waiting.jobs[0].model_copy(update={"running": True})]}
    )
    card = next(c for c in _panel(notes, running).cards if c.title == "План")
    assert not [b for b in card.menu if b.children and b.title == "Переместить в раздел"]


def test_move_keeps_the_folder_and_its_id_and_follows_sections(tmp_path, notes) -> None:
    """A move keeps the folder and its name in the section: files and id intact, the list shows
    it in the new section; "No section" moves it back to the root; its own section does
    nothing."""
    rt = BarRuntime(platform=_Platform(), prefs_file=tmp_path / "bar.json")
    folder = notes / "2026-10-02_Итоги"
    id = _record_id(notes, folder.name)
    meta = (folder / META_FILE).read_bytes()

    rt.handle(f"record:{id}:move:Личное")
    moved = notes / "Личное" / "2026-10-02_Итоги"
    assert not folder.exists() and (moved / META_FILE).read_bytes() == meta
    assert (moved / "transcript.txt").read_text(encoding="utf-8") == "mic: итоги\n"
    assert find_recording(notes, id) == moved
    card = next(c for c in _panel(notes).cards if c.id == id)
    assert card.section == "Личное" and rt.state.error == ""

    rt.handle(f"record:{id}:move:Личное")  # already there: checked, does nothing
    assert find_recording(notes, id) == moved

    rt.handle(f"record:{id}:move:")
    assert find_recording(notes, id) == folder and not moved.exists()
    assert rt.record_sections() == ["Личное", "Работа"]  # an emptied section is still a section


def test_taken_name_gets_a_number_and_missing_section_is_an_error(tmp_path, notes) -> None:
    """A name taken in the section gives "… (2)", folders never merge; a missing section
    (deleted in Finder) gives a notice and the recording stays."""
    other = notes / "Работа" / "2026-10-02_Итоги"
    other.mkdir()
    (other / "transcript.txt").write_text("другая\n", encoding="utf-8")
    rt = BarRuntime(platform=_Platform(), prefs_file=tmp_path / "bar.json")
    folder = notes / "2026-10-02_Итоги"
    id = _record_id(notes, folder.name)

    rt.handle(f"record:{id}:move:Работа")
    assert find_recording(notes, id) == notes / "Работа" / "2026-10-02_Итоги (2)"
    assert (other / "transcript.txt").read_text(encoding="utf-8") == "другая\n"

    (notes / "Личное").rmdir()
    rt.handle(f"record:{id}:move:Личное")
    assert rt.state.error == "Не удалось переместить запись. Подробности в журнале Mnema"
    assert find_recording(notes, id) == notes / "Работа" / "2026-10-02_Итоги (2)"


def test_record_can_not_be_moved_while_it_is_processed(tmp_path, notes) -> None:
    """A recording being processed does not move; one moves while another is recorded."""
    rt = BarRuntime(platform=_Platform("Новый"), prefs_file=tmp_path / "bar.json")
    folder = notes / "2026-10-02_Итоги"
    id = _record_id(notes, folder.name)
    rt.state.jobs = [Job(id=id, folder=folder, running=True)]
    rt.handle(f"record:{id}:move:Личное")
    rt.handle(f"record:{id}:new_section")
    assert find_recording(notes, id) == folder
    assert not (notes / "Новый").exists()
    rt.state.jobs, rt.state.phase = [], Phase.RECORDING
    rt.handle(f"record:{id}:move:Личное")
    assert find_recording(notes, id) == notes / "Личное" / folder.name


@pytest.mark.parametrize(
    ("typed", "section"),
    [
        ("  Проекты  ", "Проекты"),
        ('Клиенты: "Альфа"/бета.', "Клиенты Альфа бета"),  # like a recording folder name
        ("работа", "Работа"),  # the same section in another case, no duplicate
    ],
)
def test_new_section_is_named_like_a_folder_and_receives_the_record(
    tmp_path, notes, typed, section
) -> None:
    platform = _Platform(typed)
    rt = BarRuntime(platform=platform, prefs_file=tmp_path / "bar.json")
    id = _record_id(notes, "2026-10-02_Итоги")
    rt.handle(f"record:{id}:new_section")
    assert platform.asked == 1
    assert find_recording(notes, id) == notes / section / "2026-10-02_Итоги"
    assert sorted(p.name for p in notes.iterdir()) == sorted(
        {"Личное", "Работа", section}
    )  # fmt: skip


@pytest.mark.parametrize("typed", [None, "", " ... ", "???"])
def test_new_section_cancelled_or_empty_creates_nothing(tmp_path, notes, typed) -> None:
    rt = BarRuntime(platform=_Platform(typed), prefs_file=tmp_path / "bar.json")
    id = _record_id(notes, "2026-10-02_Итоги")
    before = sorted(p.name for p in notes.iterdir())
    rt.handle(f"record:{id}:new_section")
    assert sorted(p.name for p in notes.iterdir()) == before
    assert find_recording(notes, id) == notes / "2026-10-02_Итоги" and rt.state.error == ""


def test_new_section_beside_a_record_of_the_same_name(tmp_path, notes) -> None:
    """A recording of that name in the root: the section gets "(2)", the recording stays."""
    same = notes / "Встречи"
    same.mkdir()
    (same / "transcript.txt").write_text("x", encoding="utf-8")
    rt = BarRuntime(platform=_Platform("Встречи"), prefs_file=tmp_path / "bar.json")
    id = _record_id(notes, "2026-10-02_Итоги")
    rt.handle(f"record:{id}:new_section")
    assert find_recording(notes, id) == notes / "Встречи (2)" / "2026-10-02_Итоги"
    assert (same / "transcript.txt").exists()


def test_tray_moves_the_last_record(tmp_path, notes) -> None:
    """Linux tray: "Last recording" has the same submenu; its section is checked."""
    platform = _Platform("Архив")
    rt = BarRuntime(platform=platform, prefs_file=tmp_path / "bar.json")
    folder = notes / "2026-10-02_Итоги"
    rt.state.last_result = folder / "transcript.txt"
    rt.state.last_summary = folder / "summary.md"
    items = menu(rt.state, rt.prefs, MenuEnv(), sections=rt.record_sections())
    last = next(i for i in items if i.title.startswith("Последняя запись"))
    move = next(i for i in last.children if i.title == "Переместить в раздел")
    assert [(i.key, i.title, i.checked) for i in move.children if not i.separator] == [
        ("move_last:", "Без раздела", True),
        ("move_last:Личное", "Личное", False),
        ("move_last:Работа", "Работа", False),
        ("new_section_last", "Новый раздел…", False),
    ]
    rt.handle("move_last:Работа")
    moved = notes / "Работа" / "2026-10-02_Итоги"
    assert moved.is_dir() and rt.state.last_result == moved / "transcript.txt"
    assert rt.state.last_summary == moved / "summary.md"
    rt.handle("new_section_last")
    assert (notes / "Архив" / "2026-10-02_Итоги").is_dir()
    assert rt.state.last_result == notes / "Архив" / "2026-10-02_Итоги" / "transcript.txt"


def test_app_moves_a_record_through_the_protocol(tmp_path, notes) -> None:
    """The Mac app: submenu items are shown keys; "New section…" opens a window with one "Name"
    field, "Create" is disabled without a name; the answer creates the section and moves the
    recording, the list shows it there."""
    out = _Out()
    runtime = functools.partial(BarRuntime, prefs_file=tmp_path / "bar.json")
    thread, app = _start(Engine(Channel(out), runtime=runtime, root=lambda: notes))
    with app:
        state = out.until(lambda m: isinstance(m, StateMsg))
        move = _move_item(state.panel, "Итоги")
        id = next(c.id for c in state.panel.cards if c.title == "Итоги")
        work = next(b.key for b in move.children if b.title == "Работа")
        app.write(_line(ActionMsg(key=work)))
        state = out.until(
            lambda m: (
                isinstance(m, StateMsg) and any(c.section for c in m.panel.cards if c.id == id)
            )
        )
        assert next(c for c in state.panel.cards if c.id == id).section == "Работа"
        new = next(b.key for b in _move_item(state.panel, "Итоги").children if b.divider)
        app.write(_line(ActionMsg(key=new)))
        asked = out.until(lambda m: isinstance(m, AskMsg))
        ask = asked.ask
        assert (ask.kind, ask.title, ask.label, ask.ok, ask.cancel, ask.required) == (
            "title", "Новый раздел", "Название", "Создать", "Отменить", True,
        )  # fmt: skip
        app.write(_line(AnswerMsg(id=asked.id, value="  Проекты ")))
        state = out.until(
            lambda m: (
                isinstance(m, StateMsg)
                and any(c.section == "Проекты" for c in m.panel.cards if c.id == id)
            )
        )
        assert "Проекты" in state.panel.sections
    thread.join(timeout=10)
    assert not thread.is_alive() and isinstance(out.rest()[-1], QuitMsg)
    assert find_recording(notes, id) == notes / "Проекты" / "2026-10-02_Итоги"


def test_record_processed_by_another_process_stays_put(tmp_path, notes) -> None:
    """Another process of this computer processes the recording (CLI `mnema finish`, MCP) and
    holds the folder lock: the move is refused with a notice; once unlocked it moves."""
    import fcntl
    import os

    rt = BarRuntime(platform=_Platform(), prefs_file=tmp_path / "bar.json")
    folder = notes / "2026-10-02_Итоги"
    id = _record_id(notes, folder.name)
    fd = os.open(folder, os.O_RDONLY)  # as recording_client does: lock the folder itself
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        rt.handle(f"record:{id}:move:Личное")
        assert find_recording(notes, id) == folder
        assert rt.state.error == "Запись уже обрабатывается"
    finally:
        os.close(fd)
    rt.handle(f"record:{id}:move:Личное")
    assert find_recording(notes, id) == notes / "Личное" / folder.name


def test_moved_record_keeps_unreachable_server_and_retry(monkeypatch, tmp_path, notes) -> None:
    """Processing never reached the server and the card moved to a section: "<server> is
    unavailable" and "Retry" stay with it there, and the retry processes it there."""
    from mnema.bar.protocol import Card
    from mnema.live.models import LiveResult
    from mnema.live.remarks import server_down

    folder = notes / "2026-10-03_13-05"
    folder.mkdir()
    (folder / "mic.wav").write_bytes(b"RIFF" + b"\0" * 2000)
    seen: list[Path] = []

    def fake_recover(base, config, connection, **kw):
        seen.append(base)
        if len(seen) == 1:
            down = server_down("srv", "mnema finish x")
            return LiveResult(base=base, defer_command="mnema finish x", remarks=[down]), ""
        return LiveResult(base=base), ""

    monkeypatch.setattr("mnema.live.recording_client.recover_on_node", fake_recover)
    rt = BarRuntime(platform=_Platform(), prefs_file=tmp_path / "bar.json")
    rt.recover(folder)
    rt.join(timeout=10)
    assert list(rt.state.unreachable) == [folder]
    id = _record_id(notes, folder.name)
    rt.handle(f"record:{id}:move:Работа")
    moved = notes / "Работа" / folder.name
    assert find_recording(notes, id) == moved
    card: Card = next(c for c in _panel(notes, rt.state).cards if c.id == id)
    assert card.status.endswith("недоступен")
    assert [b.title for b in card.buttons] == ["Повторить"]
    rt.handle(f"record:{id}:retry")
    rt.join(timeout=10)
    assert seen == [folder, moved] and rt.state.unreachable == {}


def test_tray_offers_the_first_section(tmp_path, notes) -> None:
    """No sections yet: the tray's "Last recording" still has "Move to section" with "No
    section" checked and "New section…", the first section is created from there."""
    for section in ("Работа", "Личное"):
        for inner in (notes / section).iterdir():
            inner.rename(notes / inner.name)
        (notes / section).rmdir()
    rt = BarRuntime(platform=_Platform("Архив"), prefs_file=tmp_path / "bar.json")
    folder = notes / "2026-10-02_Итоги"
    rt.state.last_result = folder / "transcript.txt"
    assert rt.record_sections() == []
    items = menu(rt.state, rt.prefs, MenuEnv(), sections=rt.record_sections())
    last = next(i for i in items if i.title.startswith("Последняя запись"))
    move = next(i for i in last.children if i.title == "Переместить в раздел")
    assert [(i.key, i.checked) for i in move.children if not i.separator] == [
        ("move_last:", True),
        ("new_section_last", False),
    ]
    rt.handle("new_section_last")
    assert (notes / "Архив" / folder.name).is_dir()


def test_linux_new_section_window_asks_again_for_an_empty_name(monkeypatch) -> None:
    """Linux (zenity) cannot disable "Create": for a name that leaves nothing for a folder name
    the window asks again; cancel gives None."""
    import subprocess
    import threading
    import types

    from mnema.bar import app_linux

    monkeypatch.setattr(threading.Thread, "start", lambda self: self.run())
    replies = [(0, "???\n"), (0, " ... \n"), (0, "Проекты\n"), (0, "???\n"), (1, "")]
    calls: list[list[str]] = []

    def fake_run(cmd, **k):
        calls.append(cmd)
        code, out = replies.pop(0)
        return types.SimpleNamespace(returncode=code, stdout=out, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    got: list = []
    platform = app_linux.LinuxPlatform()
    platform.ask_section(got.append)
    assert got == ["Проекты"] and len(calls) == 3
    assert "--title=Новый раздел" in calls[0] and "--ok-label=Создать" in calls[0]
    platform.ask_section(got.append)
    assert got == ["Проекты", None]
