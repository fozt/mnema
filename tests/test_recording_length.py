"""A recording's length and its label, shared with the phone (fixtures recording-length-v1 and
length-labels-v1): the Mac and Linux panel and the phone list show the same length."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from mnema import i18n
from mnema.bar.panel import length_label, panel_model
from mnema.bar.recordings import library
from mnema.bar.settings_model import settings_model
from mnema.bar.state import BarPrefs, BarState, MenuEnv
from mnema.live import meta

FIXTURES = Path(__file__).parent / "fixtures"
LENGTHS = json.loads((FIXTURES / "recording-length-v1.json").read_text("utf-8"))["cases"]
LABELS = json.loads((FIXTURES / "length-labels-v1.json").read_text("utf-8"))["cases"]


@pytest.mark.parametrize("case", LENGTHS, ids=lambda case: case["name"])
def test_wav_and_history_give_the_shared_length(case) -> None:
    data = None if case["runs"] is None else {"version": 1, "runs": case["runs"]}
    assert meta.recording_seconds(case["wav"], data) == case["seconds"]


@pytest.mark.parametrize("case", LABELS, ids=lambda case: case["en"])
def test_length_reads_the_same_on_every_device(case) -> None:
    for lang in ("ru", "en"):
        with i18n.using(lang):
            assert length_label(case["seconds"]) == case[lang]


def test_synced_recording_without_audio_shows_its_length(tmp_path) -> None:
    """Sync brings the texts and the history, not the audio: the length comes from the history."""
    folder = tmp_path / "2026-09-30_Созвон"
    folder.mkdir()
    (folder / "transcript.txt").write_text("mic: привет\n", encoding="utf-8")
    run = {"status": "ok", "tracks": {"mic": {"seconds": 4200.3}, "system": {"seconds": 4199.0}}}
    history = {"version": 1, "started": "2026-09-30T09:15:00", "runs": [run]}
    (folder / meta.META_FILE).write_text(json.dumps(history), encoding="utf-8")
    stamp = datetime(2026, 9, 30, 11, 0).timestamp()
    os.utime(folder / "transcript.txt", (stamp, stamp))

    cards = library(tmp_path).cards
    prefs, env = BarPrefs(), MenuEnv(mac=True)
    panel = panel_model(
        BarState(), prefs, env, cards, settings_model(prefs, env), now=datetime(2026, 9, 30, 12)
    )
    [card] = panel.cards
    assert card.subtitle == "Сегодня, 9:15 · 1 ч 10 мин"
