"""Time in the "Process recording" window, from the fixture shared with the Mac and the phone
(offsets-v1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mnema.bar.range_pick import RangePick, typed
from mnema.live.setup import fmt_clock

TABLE = json.loads((Path(__file__).parent / "fixtures/offsets-v1.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", TABLE["typed"], ids=lambda case: case["text"] or "пусто")
def test_typed_time_gives_the_shared_seconds(case):
    assert typed(case["text"]) == case["seconds"]


@pytest.mark.parametrize("case", TABLE["clock"], ids=lambda case: case["clock"])
def test_seconds_give_the_shared_clock(case):
    assert fmt_clock(case["seconds"]) == case["clock"]


@pytest.mark.parametrize("case", TABLE["whole"], ids=str)
def test_bounds_give_the_shared_whole_recording(case):
    pick = RangePick(length=case["length"], start=case["start"], end=case["end"])
    assert pick.whole() == case["whole"]
