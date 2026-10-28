"""Language-dependent formatting shared by several areas."""

from __future__ import annotations

_MONTHS = {
    "ru": (
        "янв.",
        "февр.",
        "мар.",
        "апр.",
        "мая",
        "июн.",
        "июл.",
        "авг.",
        "сент.",
        "окт.",
        "нояб.",
        "дек.",
    ),  # fmt: skip
    "en": ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
}


def month_short(month: int) -> str:
    """Short month name for dates in captions: ru «окт.», en «Oct»."""
    from . import current

    return _MONTHS[current()][month - 1]
