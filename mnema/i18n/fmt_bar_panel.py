"""Dates and language names in the panel and the tray, per interface language."""

from __future__ import annotations

from datetime import datetime

from .fmt import month_short


def _ru() -> bool:
    from . import current

    return current() == "ru"


def day_month(t: datetime) -> str:
    """Compact day for recording names: ru «27.09», en «Sep 27»."""
    return f"{t:%d.%m}" if _ru() else f"{month_short(t.month)} {t.day}"


def short_date(t: datetime, *, year: bool) -> str:
    """Short date: Sep 28 or Sep 28, 2025; Russian puts the day first, no comma before the year."""
    if _ru():
        return f"{t.day} {month_short(t.month)}" + (f" {t.year}" if year else "")
    return f"{month_short(t.month)} {t.day}" + (f", {t.year}" if year else "")


def names_in_sentence(names: str) -> str:
    """Language names after "Language:": Russian writes them lowercase, English keeps capitals."""
    return names.lower() if _ru() else names
