"""A recording's title is only its folder name: the menu, the feed and renaming see the same
thing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mnema.bar.recordings import library
from mnema.bar.runtime import BarRuntime
from mnema.bar.state import result_name, title_of
from mnema.live import meta
from mnema.live.setup import is_default_name, live_base, rename_session


def test_automatic_title_names_the_folder_and_keeps_its_history(tmp_path):
    folder = tmp_path / "2026-10-21_08-35"
    folder.mkdir()
    history = {"version": 1, "runs": [{"status": "ok"}], "extra": {"keep": True}}
    (folder / meta.META_FILE).write_text(json.dumps(history), encoding="utf-8")
    (folder / "transcript.txt").write_text("mic: API-клиент\n", encoding="utf-8")

    named = rename_session(folder, "Дейли: API и API-клиент")
    assert named.name == "2026-10-21_Дейли API и API-клиент"
    assert json.loads((named / meta.META_FILE).read_text(encoding="utf-8")) == history
    assert title_of(named) == "Дейли API и API-клиент"
    assert result_name(named / "transcript.txt") == "Дейли API и API-клиент · 21.10"
    assert library(tmp_path).cards[0].title == "Дейли API и API-клиент"


@pytest.mark.parametrize("name", ["2026-10-21_08-35", "2026-10-21_08-35 (2)"])
def test_timestamp_names_display_as_time(name):
    assert is_default_name(name)
    assert title_of(Path(name)) is None
    assert result_name(Path(name)) == "21.10, 08:35"


def test_default_name_and_date_survive_file_modification(tmp_path, monkeypatch):
    import os
    from datetime import datetime

    monkeypatch.setenv("MNEMA_LIVE_DIR", str(tmp_path))
    first, second = live_base(), live_base()
    assert "_" in first.name and second.name == f"{first.name} (2)"
    old = tmp_path / "2026-10-20_08-35"
    old.mkdir()
    transcript = old / "transcript.txt"
    transcript.write_text("mic: текст\n", encoding="utf-8")
    now = datetime(2026, 10, 21).timestamp()
    os.utime(transcript, (now, now))
    [card] = library(tmp_path).cards
    assert card.started == datetime(2026, 10, 20, 8, 35) and card.time_known


def test_manual_clock_like_name_is_a_title():
    name = "2026-10-21_08.35"
    assert not is_default_name(name)
    assert title_of(Path(name)) == "08.35"
    assert result_name(Path(name)) == "08.35 · 21.10"


def test_manual_range_in_title_is_not_duplicated_on_collision(tmp_path):
    first, second = tmp_path / "2026-10-21_08-35", tmp_path / "2026-10-21_09-35"
    first.mkdir()
    second.mkdir()
    title = "Дейли (1.00–2.00)"
    a = rename_session(first, title)
    b = rename_session(second, title)
    assert b.name == "2026-10-21_Дейли (1.00–2.00) (2)"
    assert title_of(a) == title_of(b) == title


@pytest.mark.parametrize(
    "name", ["2026-10-21_08-35 (0.10–1.35)", "2026-10-21_08-35 (0.10–1.35) (2)"]
)
def test_unnamed_part_displays_time_and_range(tmp_path, name):
    from datetime import datetime

    from test_bar_panel import _panel

    folder = tmp_path / name
    folder.mkdir()
    (folder / "transcript.txt").write_text("mic: текст\n", encoding="utf-8")
    assert is_default_name(folder) and is_default_name(name)
    assert title_of(folder) is None
    assert result_name(folder / "transcript.txt") == "21.10, 08:35 (0.10–1.35)"
    [card] = library(tmp_path).cards
    assert card.started == datetime(2026, 10, 21, 8, 35) and card.time_known
    today = card.model_copy(update={"started": datetime(2026, 9, 30, 8, 35)})
    [shown] = _panel(cards=[today]).cards
    assert shown.title == "Сегодня, 8:35 (0.10–1.35)"


def test_naming_a_part_keeps_its_range_and_drops_the_collision_mark(tmp_path):
    folder = tmp_path / "2026-10-21_08-35 (0.10–1.35) (2)"
    folder.mkdir()
    named = rename_session(folder, "Дейли API")
    assert named.name == "2026-10-21_Дейли API (0.10–1.35)"
    assert title_of(named) == "Дейли API (0.10–1.35)"


def test_rename_changes_only_the_folder_name_and_keeps_the_part(tmp_path, monkeypatch):
    monkeypatch.setenv("MNEMA_LIVE_DIR", str(tmp_path))
    folder = tmp_path / "2026-10-21_08-35 (0.10–1.35)"
    folder.mkdir()
    files = {
        "transcript.txt": "mic: текст\n",
        "summary.txt": "Суть.\n\n- пункт\n",
        meta.META_FILE: json.dumps(
            {"version": 1, "runs": [], "started": "2026-10-21T08:35:00+03:00"}
        ),
    }
    for name, text in files.items():
        (folder / name).write_text(text, encoding="utf-8")
    rt = BarRuntime(prefs_file=tmp_path / "bar.json")
    rt.state.last_result = folder / "transcript.txt"

    rt.rename_folder(folder, "Дейли: итоги")
    renamed = tmp_path / "2026-10-21_Дейли итоги (0.10–1.35)"
    assert not folder.exists() and rt.state.last_result == renamed / "transcript.txt"
    assert {n: (renamed / n).read_text(encoding="utf-8") for n in files} == files

    rt.rename_folder(renamed, "")  # an empty title gives the time-based name again
    assert (tmp_path / "2026-10-21_08-35 (0.10–1.35)").is_dir()
    rt.rename_folder(tmp_path / "2026-10-21_08-35 (0.10–1.35)", None)  # the window was closed
    assert (tmp_path / "2026-10-21_08-35 (0.10–1.35)").is_dir()
