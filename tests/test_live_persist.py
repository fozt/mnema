"""Saving live session artifacts (tmp_path, no network or ffmpeg)."""

from __future__ import annotations

import errno

import pytest

from mnema.core.models import Segment
from mnema.live import persist
from mnema.live.models import Track


def _tracks() -> list[Track]:
    return [
        Track(
            label="mic",
            committed=[
                Segment(start=0.0, end=1.0, text="привет", speaker="mic"),
                Segment(start=1.0, end=2.0, text="как дела", speaker="mic"),
            ],
        )
    ]


def test_saved_dialogue_keeps_pauses_track_labels_and_all_words(tmp_path) -> None:
    tracks = [
        Track(
            label="system",
            committed=[
                Segment(start=0, end=1, text="Срок — 30 октября.", speaker="system"),
                Segment(start=1.2, end=2, text="Пока не согласован.", speaker="system"),
                Segment(start=4, end=5, text="Следующий вопрос.", speaker="system"),
                Segment(start=7, end=8, text="Хорошо.", speaker="system"),
            ],
        ),
        Track(
            label="mic",
            committed=[Segment(start=5.2, end=6, text="Я уточню.", speaker="mic")],
        ),
    ]

    path = persist.write_transcript(tracks, tmp_path, "ru")
    assert path is not None
    assert path.read_text(encoding="utf-8") == (
        "[0:00] system: Срок — 30 октября. Пока не согласован.\n\n"
        "[0:04] system: Следующий вопрос.\n\n[0:05] mic: Я уточню.\n\n[0:07] system: Хорошо.\n"
    )
    assert persist.dialogue_text(tracks, "ru") == path.read_text(encoding="utf-8").strip()


@pytest.mark.parametrize("stage", ["fsync", "replace"])
def test_replacing_transcript_keeps_original_when_disk_write_fails(
    monkeypatch, tmp_path, stage
) -> None:
    previous = b"mic: user corrections\nsystem: previous complete response\n"
    transcript = tmp_path / "transcript.txt"
    transcript.write_bytes(previous)

    def disk_full(*args):
        raise OSError(errno.ENOSPC, "No space left on device")

    if stage == "fsync":
        monkeypatch.setattr(persist.os, "fsync", disk_full)
    else:
        monkeypatch.setattr(type(transcript), "replace", disk_full)
    with pytest.raises(OSError, match="No space"):
        persist.write_transcript(_tracks(), tmp_path, "ru")
    assert transcript.read_bytes() == previous
    assert not list(tmp_path.glob("*.tmp"))


def test_rewritten_transcript_keeps_ordinary_permissions(tmp_path) -> None:
    """The atomic write does not leave the transcript owner-only (mkstemp creates 0600): sync and
    other programs read it."""
    import os

    from mnema.live.persist import _atomic_bytes

    new = tmp_path / "transcript.txt"
    _atomic_bytes(new, b"text")
    assert new.stat().st_mode & 0o777 == 0o666 & ~_mask()
    old = tmp_path / "summary.txt"
    old.write_text("x")
    os.chmod(old, 0o644)
    _atomic_bytes(old, b"y")
    assert old.stat().st_mode & 0o777 == 0o644


def _mask() -> int:
    import os

    current = os.umask(0)
    os.umask(current)
    return current


def test_summary_file_keeps_the_gist_first_and_the_blocks_after_the_topics(tmp_path) -> None:
    """The panel and the phone read the gist as the first paragraph: topics with details and the
    headed blocks after them do not change it."""
    from mnema.bar.recordings import _gist
    from mnema.live.persist import write_summary
    from mnema.llm.claude import SummaryBlock

    path = write_summary(
        tmp_path,
        "Суть разговора.",
        ["[0:05] Тема\n  - Деталь один.\n  - Деталь два."],
        [SummaryBlock(title="Договорились", points=["[0:20] Выкатить в пятницу."])],
    )
    assert path.read_text(encoding="utf-8") == (
        "Суть разговора.\n\n"
        "- [0:05] Тема\n  - Деталь один.\n  - Деталь два.\n"
        "\nДоговорились:\n- [0:20] Выкатить в пятницу.\n"
    )
    assert _gist(path.read_text(encoding="utf-8")) == "Суть разговора."


def test_summary_template_comes_from_the_library_and_the_recordings_section(tmp_path) -> None:
    """The library's template applies to every recording, a section's only to its recordings;
    a folder outside the library has none."""
    from mnema.live.persist import SUMMARY_TEMPLATE, summary_hint

    root = tmp_path / "mnema"
    (root / "Работа" / "2026-11-13_Дейли").mkdir(parents=True)
    (root / "2026-11-13_Звонок").mkdir()
    assert summary_hint(root / "2026-11-13_Звонок", root) is None
    (root / SUMMARY_TEMPLATE).write_text("Меня зовут Аня.\n", encoding="utf-8")
    (root / "Работа" / SUMMARY_TEMPLATE).write_text("Кто что сделал.", encoding="utf-8")
    assert summary_hint(root / "2026-11-13_Звонок", root) == "Меня зовут Аня."
    assert (
        summary_hint(root / "Работа" / "2026-11-13_Дейли", root)
        == "Меня зовут Аня.\n\nКто что сделал."
    )
    assert summary_hint(tmp_path / "elsewhere" / "2026-11-13_Звонок", root) is None
