"""Paragraph times and the time check of summary bullets."""

import pytest

from mnema.core.base import fmt_stamp, parse_stamp, unstamped
from mnema.llm.claude import check_stamps


@pytest.mark.parametrize(
    ("seconds", "stamp"),
    [(0, "0:00"), (12.6, "0:12"), (725, "12:05"), (3599.9, "59:59"), (3723.9, "1:02:03")],
)
def test_stamp_rounds_down(seconds, stamp) -> None:
    assert fmt_stamp(seconds) == stamp
    assert parse_stamp(f"[{stamp}]") == int(seconds)


@pytest.mark.parametrize("stamp", ["[0:75]", "[60:00]", "[1:60:00]", "[1:7]", "0:12", "x[0:12]"])
def test_invalid_stamp(stamp) -> None:
    assert parse_stamp(stamp) is None


def test_unstamped_only_removes_line_prefixes() -> None:
    assert unstamped("[0:12] mic: abc\n\n[1:00:00] system: d") == "mic: abc\n\nsystem: d"
    assert unstamped("mic: [0:12] abc\n[неразборчиво] d") == "mic: [0:12] abc\n[неразборчиво] d"


@pytest.mark.parametrize(
    ("items", "transcript", "expected"),
    [
        (
            ["[0:31] А", "[99:00] Б", "В", "[1:7] Г", "[4:05] Д", "[4:06] Е", "[9:99] Ж"],
            "[0:12] mic: x\n\n[4:05] system: y",
            ["[0:31] А", "Б", "В", "[1:7] Г", "[4:05] Д", "Е", "Ж"],
        ),
        # the transcript has no times: the time is dropped, the empty "[9:99]" bullet is gone
        (["[0:00] А", "[1:02:03] Б", "В", "[9:99]"], "mic: x", ["А", "Б", "В"]),
    ],
)
def test_summary_stamps_are_bounded_by_transcript(items, transcript, expected) -> None:
    assert check_stamps(items, transcript) == expected
