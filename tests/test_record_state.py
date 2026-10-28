"""Recording state: one rule for the feed, panel, menu and phone (fixture record-state-v1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mnema.bar.panel import panel_model
from mnema.bar.recordings import library
from mnema.bar.settings_model import settings_model
from mnema.bar.state import BarPrefs, BarState, MenuEnv
from mnema.live import meta
from mnema.live.setup import PROCESSING_MARK, unfinished_sessions

TABLE = json.loads((Path(__file__).parent / "fixtures/record-state-v1.json").read_text("utf-8"))
# no "running": the feed and the menu do not show a folder processed on this computer
_WORK_FILES = {"none": None, "interrupted": json.dumps({"node": "http://node:8000"})}


@pytest.mark.parametrize("case", TABLE["cases"], ids=lambda case: case["name"])
def test_files_history_and_work_give_the_shared_state(case):
    work = meta.Work(case["work"])
    assert meta.record_state(case["files"], {"runs": case["runs"]}, work) == case["state"]


@pytest.mark.parametrize(
    "case",
    [case for case in TABLE["cases"] if case["work"] in _WORK_FILES],
    ids=lambda case: case["name"],
)
def test_feed_panel_and_menu_agree_on_the_folder(tmp_path, case):
    """The feed takes the state by the same rule, and "Not processed" in the panel is shown on
    exactly the recordings the menu offers to process."""
    folder = tmp_path / "2026-10-21_08-35"
    folder.mkdir()
    for name, size in case["files"].items():
        (folder / name).write_bytes(b"\0" * size)
    if marker := _WORK_FILES[case["work"]]:
        (folder / PROCESSING_MARK).write_text(marker, encoding="utf-8")
    (folder / meta.META_FILE).write_text(json.dumps({"version": 1, "runs": case["runs"]}))

    cards = library(tmp_path).cards
    assert [c.state for c in cards] == [case["state"]]
    env = MenuEnv(mac=True)
    prefs = BarPrefs()
    [card] = panel_model(BarState(), prefs, env, cards, settings_model(prefs, env)).cards
    assert (card.status == "Не обработана") == (unfinished_sessions(tmp_path) == [folder])


SHOWN = json.loads((Path(__file__).parent / "fixtures/shown-run-v1.json").read_text("utf-8"))


@pytest.mark.parametrize("case", SHOWN["cases"], ids=lambda case: case["name"])
def test_remarks_and_summary_take_the_last_run_that_was_not_stopped(tmp_path, case):
    (tmp_path / meta.META_FILE).write_text(json.dumps({"version": 1, "runs": case["runs"]}))
    shown = case["shown"]
    expected = None if shown is None else meta.Run.model_validate(case["runs"][shown])
    assert meta.last(tmp_path) == expected
