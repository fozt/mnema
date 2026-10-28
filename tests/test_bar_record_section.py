"""The section before recording (Mac and Linux): the choice made while idle is remembered, a new
recording is created right in the section folder; a vanished section means the root and No section.
Everything that looks for recordings sees one in a section just as in the root."""

from __future__ import annotations

import io
import logging
from pathlib import Path

import pytest
from test_bar import _AskPlatform, _patch_record, _wav
from test_bar_headless import _engine, _line, _Out

from mnema.bar.panel import panel_model
from mnema.bar.protocol import ActionMsg, StateMsg
from mnema.bar.recordings import library
from mnema.bar.runtime import BarRuntime
from mnema.bar.settings_model import settings_model
from mnema.bar.state import BarPrefs, BarState, MenuEnv, Phase, load_prefs, menu, save_prefs
from mnema.live.meta import RecordState
from mnema.live.setup import unfinished_sessions
from mnema.sync.state import Checkpoint, checkpoint_path, save


@pytest.fixture
def notes(monkeypatch, tmp_path) -> Path:
    """A library root: a section with a recording, an empty section and a recording in the root
    (not a section)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))  # its own sync.json
    root = tmp_path / "notes"
    (root / "Работа" / "2026-10-01_08-00").mkdir(parents=True)
    (root / "Работа" / "2026-10-01_08-00" / "transcript.txt").write_text("mic: план\n")
    (root / "Пустой").mkdir()
    _wav(root / "2026-10-02_09-00" / "mic.wav")
    (root / "2026-10-02_09-00" / "transcript.txt").write_text("mic: итоги\n")
    monkeypatch.setenv("MNEMA_LIVE_DIR", str(root))
    return root


def _section_menu(rt: BarRuntime):
    items = menu(rt.state, rt.prefs, MenuEnv(), sections=rt.record_sections())
    return next((i for i in items if i.title.startswith("Раздел")), None)


def test_tray_menu_offers_sections_and_remembers_the_choice(tmp_path, notes) -> None:
    """The Section: … submenu with radio choices: No section and the library's sections; the choice
    is remembered, a section missing on disk or a recording keeps the previous choice."""
    rt = BarRuntime(prefs_file=tmp_path / "bar.json")
    assert rt.record_sections() == ["Пустой", "Работа"]
    sub = _section_menu(rt)
    assert sub is not None and sub.title == "Раздел: Без раздела"
    assert [(c.key, c.title, c.checked, c.radio) for c in sub.children] == [
        ("record_section:", "Без раздела", True, True),
        ("record_section:Пустой", "Пустой", False, True),
        ("record_section:Работа", "Работа", False, True),
    ]
    rt.handle("record_section:Работа")
    assert load_prefs(tmp_path / "bar.json").record_section == "Работа"
    for bad in ("2026-10-02_09-00", "Нет такого", "../notes", "Работа/2026-10-01_08-00"):
        rt.handle(f"record_section:{bad}")
    assert rt.prefs.record_section == "Работа"
    assert _section_menu(rt).title == "Раздел: Работа"
    assert menu(BarState(), rt.prefs, MenuEnv()) == menu(
        BarState(), rt.prefs, MenuEnv(), sections=[]
    )
    assert not any(i.title.startswith("Раздел") for i in menu(BarState(), rt.prefs, MenuEnv()))
    rt.handle("record_section:")
    assert load_prefs(tmp_path / "bar.json").record_section == ""


def test_panel_section_row_shows_the_choice_or_none(tmp_path) -> None:
    """The Mac panel: a Section row while idle; a vanished section shows No section; no sections,
    no row."""

    def section(prefs: BarPrefs, sections: list[str]):
        settings = settings_model(prefs, MenuEnv())
        return panel_model(BarState(), prefs, MenuEnv(), [], settings, sections=sections).idle

    row = section(BarPrefs(record_section="Работа"), ["Работа", "Архив"]).section
    assert row is not None and (row.title, row.value) == ("Раздел", "Работа")
    assert [(c.key, c.checked) for c in row.choices] == [
        ("record_section:", False),
        ("record_section:Архив", False),
        ("record_section:Работа", True),
    ]
    gone = section(BarPrefs(record_section="Старый"), ["Работа"]).section
    assert gone is not None and gone.value == "Без раздела" and gone.choices[0].checked
    assert section(BarPrefs(record_section="Работа"), []).section is None


def test_new_recording_is_created_inside_the_section(monkeypatch, tmp_path, notes) -> None:
    """A recording is written right into the section folder (not moved afterwards), sync leaves it
    alone while recording; renaming keeps it there; the feed and unprocessed list see it in the
    section."""
    from mnema.sync.scan import scan

    monkeypatch.setenv("MNEMA_MODEL", "")
    monkeypatch.setenv("MNEMA_API_KEY", "")
    captured: dict = {}
    _patch_record(monkeypatch, tmp_path, captured)
    monkeypatch.setenv("MNEMA_LIVE_DIR", str(notes))
    save_prefs(BarPrefs(record_section="Работа"), tmp_path / "bar.json")
    rt = BarRuntime(platform=_AskPlatform("Итоги"), prefs_file=tmp_path / "bar.json")
    before = set(notes.iterdir())
    rt.handle("start")
    assert captured["started"].wait(10)
    folder = rt.active_folder
    assert folder is not None and folder.parent == notes / "Работа"
    assert (folder / ".mnema-meta.json").exists() and set(notes.iterdir()) == before
    busy = scan(notes, rt._sync_busy())
    assert f"Работа/{folder.name}" in busy.blocked_paths and "Работа" in busy.manifest.sections
    rt.handle("stop")
    rt.join(20)
    assert rt.state.phase == Phase.IDLE and not rt.state.error
    result = rt.state.last_result
    assert result is not None and result.parent.parent == notes / "Работа"
    assert result.parent.name.endswith("_Итоги")  # the recording title after processing
    card = next(c for c in library(notes).cards if c.title == "Итоги")
    assert card.section == "Работа" and card.path == result.parent
    assert card.state is RecordState.PROCESSED
    assert unfinished_sessions(notes) == [] and rt.prefs.record_section == "Работа"


@pytest.mark.parametrize("chosen", ["Архив", "2026-10-02_09-00", "../x"])
def test_section_gone_records_in_root_and_resets(monkeypatch, tmp_path, notes, chosen, caplog):
    """The section is missing on disk, is a recording rather than a section, or has an unsafe name —
    the recording goes to the root, the choice becomes No section, a note goes to the log."""
    captured: dict = {}
    _patch_record(monkeypatch, tmp_path, captured)
    monkeypatch.setenv("MNEMA_LIVE_DIR", str(notes))
    save_prefs(BarPrefs(record_section=chosen), tmp_path / "bar.json")
    rt = BarRuntime(prefs_file=tmp_path / "bar.json")
    with caplog.at_level(logging.WARNING, logger="mnema.bar.runtime"):
        rt.handle("start")
        assert captured["started"].wait(10)
    folder = rt.active_folder
    assert folder is not None and folder.parent == notes
    assert load_prefs(tmp_path / "bar.json").record_section == ""
    assert f'section "{chosen}" is gone' in caplog.text
    rt.stop(cancel=True)
    rt.join(20)
    assert not (notes / "Архив").exists() and not (tmp_path / "x").exists()


def test_audio_in_a_known_section_does_not_become_a_recording(tmp_path, notes) -> None:
    """A section from the last sync with a mic.wav dropped in, and a section subfolder with audio,
    are not recordings: no Not processed with Process… in the menu, no card in the feed, and the
    section stays a section for a new recording."""
    save(checkpoint_path(), Checkpoint(sections=["Пустой", "Работа"]))
    _wav(notes / "Пустой" / "mic.wav")
    _wav(notes / "Работа" / "Материалы" / "memo.wav")
    rt = BarRuntime(prefs_file=tmp_path / "bar.json")
    items = menu(rt.state, rt.prefs, MenuEnv(), sections=rt.record_sections())
    assert not [i for i in items if i.title.startswith("⚠︎ Не обработана")]
    assert rt.record_sections() == ["Пустой", "Работа"]
    assert {c.path for c in library(notes).cards} == {
        notes / "Работа" / "2026-10-01_08-00",
        notes / "2026-10-02_09-00",
    }


def test_headless_accepts_only_the_sections_it_showed(tmp_path, notes, caplog) -> None:
    """The Mac app: a choice from the Section row is remembered; the engine refuses a section the
    panel never showed."""
    out = _Out()
    engine = _engine(tmp_path, notes, out)
    stdin = io.StringIO(
        _line(ActionMsg(key="record_section:Работа"))
        + _line(ActionMsg(key="record_section:2026-10-02_09-00"))
    )
    engine.run(stdin)
    assert engine.rt.prefs.record_section == "Работа"
    assert "the panel never showed: record_section:2026-10-02_09-00" in caplog.text
    states = [m for m in out.rest() if isinstance(m, StateMsg)]
    row = states[-1].panel.idle.section
    assert row is not None and row.value == "Работа"
    assert [c.title for c in row.choices] == ["Без раздела", "Пустой", "Работа"]
