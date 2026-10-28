"""Recording title, folder name and start, from fixtures shared with the phone: folder-titles-v1,
folder-names-v1, recording-start-v1, summary-v1."""

from __future__ import annotations

import json
import os
import struct
import time
from datetime import datetime
from pathlib import Path

import pytest

from mnema.bar.recordings import library
from mnema.live.naming import (
    chosen_title,
    display_title,
    folder_name,
    free_name,
    part_of,
    recording_start,
    started_iso,
    wav_seconds,
)
from mnema.live.setup import rename_session

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


TITLES = _load("folder-titles-v1.json")["cases"]
NAMES = _load("folder-names-v1.json")
STARTS = _load("recording-start-v1.json")
SUMMARIES = _load("summary-v1.json")["cases"]


@pytest.fixture
def zone(monkeypatch):
    """File times of the fixture, in its time zone."""
    monkeypatch.setenv("TZ", STARTS["zone"])
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _meta(folder: Path, started: str | None) -> None:
    data = {"version": 1, "runs": []}
    if started is not None:
        data["started"] = started
    (folder / ".mnema-meta.json").write_text(json.dumps(data), encoding="utf-8")


@pytest.mark.parametrize("case", TITLES, ids=lambda case: case["folder"])
def test_folder_name_gives_the_shared_title_and_start(tmp_path, case):
    shown = display_title(case["folder"])
    assert shown.title == case["title"]
    assert shown.part == (f" ({case['part']})" if "part" in case else "")
    folder = tmp_path / case["folder"]
    folder.mkdir()
    start, known = recording_start(folder)
    if case["start"] is not None:
        expected = datetime.fromisoformat(case["start"])
        by_time = "T" in case["start"]
        assert (start if by_time else start.date()) == (expected if by_time else expected.date())
        assert known == by_time


@pytest.mark.parametrize("case", NAMES["names"], ids=lambda case: case["name"][:40])
def test_title_gives_the_shared_folder_name(tmp_path, case):
    for name in case.get("taken", []):
        (tmp_path / name).mkdir()
    folder = tmp_path / case["current"]
    folder.mkdir()
    (folder / "mic.wav").write_bytes(b"audio")
    _meta(folder, case["started"])
    renamed = rename_session(folder, case["title"])
    assert renamed.name == case["name"]
    assert (renamed / "mic.wav").read_bytes() == b"audio"
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
        {*case.get("taken", []), case["name"]}
    )


@pytest.mark.parametrize("case", NAMES["free"], ids=lambda case: case["free"][:40])
def test_taken_name_gives_the_shared_free_name(tmp_path, case):
    for name in case["taken"]:
        (tmp_path / name).mkdir()
    assert free_name(tmp_path / case["name"]).name == case["free"]


def test_taken_name_by_a_link_is_not_followed(tmp_path):
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "2026-10-21_Дейли").symlink_to(tmp_path / "missing")
    assert free_name(tmp_path / "2026-10-21_Дейли").name == "2026-10-21_Дейли (2)"


@pytest.mark.parametrize("case", NAMES["parts"], ids=lambda case: case["label"])
def test_part_bounds_give_the_shared_label(case):
    assert part_of(case["start"], case["end"]) == case["label"]


@pytest.mark.parametrize("case", NAMES["own"], ids=lambda case: repr(case["typed"]))
def test_typed_own_title_gives_the_shared_one(case):
    assert chosen_title(case["typed"], case["part"]) == case["own"]


@pytest.mark.parametrize("case", NAMES["by_time"], ids=lambda case: case["name"])
def test_start_gives_the_shared_name_by_time(case):
    assert folder_name(datetime.fromisoformat(case["start"]), "") == case["name"]


def _track(path: Path, case: dict) -> None:
    """A track file of a fixture lengths case: no header (size) or a 16-bit PCM WAV (wav)."""
    if "size" in case:
        with path.open("wb") as f:
            f.truncate(case["size"])
        return
    wav = case["wav"]
    rate, channels = wav["rate"], wav.get("channels", 1)
    block = channels * 2
    data = wav["frames"] * block
    fmt = struct.pack("<HHIIHH", 1, channels, rate, rate * block, block, 16)

    def chunk(name: bytes, body: bytes, size: int | None = None) -> bytes:
        return name + struct.pack("<I", len(body) if size is None else size) + body

    body = chunk(b"fmt ", fmt)
    if "before" in wav:
        body += chunk(b"LIST", bytes(wav["before"]))
    body += chunk(b"data", bytes(data), wav.get("declared", data))
    if "after" in wav:
        body += chunk(b"LIST", bytes(wav["after"]))
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body) + 4) + b"WAVE" + body)


@pytest.mark.parametrize(
    "case", STARTS["lengths"], ids=lambda case: str(case.get("size", case.get("wav")))
)
def test_wav_track_gives_the_shared_length(tmp_path, case):
    track = tmp_path / "mic.wav"
    _track(track, case)
    assert wav_seconds(track) == case["seconds"]


@pytest.mark.parametrize("case", STARTS["cases"], ids=lambda case: case["folder"])
def test_folder_gives_the_shared_recording_start(tmp_path, zone, case):
    folder = tmp_path / case["folder"]
    folder.mkdir()
    if "meta" in case:
        _meta(folder, case["meta"])
    for file in case.get("files", []):
        path = folder / file["name"]
        with path.open("wb") as f:
            f.truncate(file["size"])
        stamp = datetime.fromisoformat(file["mtime"]).timestamp()
        os.utime(path, (stamp, stamp))
    start, known = recording_start(folder)
    assert known == case["known"]
    if "start" in case:
        assert start == datetime.fromisoformat(case["start"])
    assert (started_iso(folder) is not None) == case.get("stored", True)


@pytest.mark.parametrize("case", SUMMARIES, ids=lambda case: case["text"][:30])
def test_summary_file_gives_the_shared_gist(tmp_path, case):
    folder = tmp_path / "2026-10-21_Дейли"
    folder.mkdir()
    (folder / "transcript.txt").write_text("mic: текст\n", encoding="utf-8")
    (folder / "summary.md").write_text(case["text"], encoding="utf-8")
    [card] = library(tmp_path).cards
    assert card.gist == case["about"]
    assert card.title == "Дейли"  # the title follows the folder name
