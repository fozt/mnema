"""Interface language: English source strings in code, Russian from the JSON catalog.

The language comes from the system (``detect``) for the app and the CLI, and from the
request's Accept-Language on the server (``using`` per request and per job). Translation
happens where a text is shown, never at import: the server picks the language per request.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
from collections.abc import Iterator
from functools import cache
from pathlib import Path

from .fmt import month_short

LANGS = ("en", "ru")
DEFAULT = "en"
ENV = "MNEMA_UI_LANG"

_CATALOG_DIR = Path(__file__).with_name("ru")
_lang: contextvars.ContextVar[str | None] = contextvars.ContextVar("mnema_ui_lang", default=None)
_process: str | None = None


def normalize(lang: str | None) -> str:
    """'ru', 'ru_RU.UTF-8', 'ru-RU' → 'ru'; anything else → 'en'."""
    return "ru" if (lang or "").strip().lower().startswith("ru") else DEFAULT


def detect() -> str:
    """System language: MNEMA_UI_LANG, then the POSIX locale variables; the first one set wins."""
    for name in (ENV, "LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(name, "").strip()
        if value:
            return normalize(value)
    return DEFAULT


def set_language(lang: str) -> None:
    """Process default (app, CLI, server without Accept-Language)."""
    global _process
    _process = normalize(lang)


def current() -> str:
    value = _lang.get()
    if value is not None:
        return value
    return _process if _process is not None else detect()


@contextlib.contextmanager
def using(lang: str | None) -> Iterator[str]:
    """Language of the current context (a server request, a job thread)."""
    token = _lang.set(normalize(lang) if lang else current())
    try:
        yield _lang.get() or DEFAULT
    finally:
        _lang.reset(token)


def from_accept_language(header: str | None) -> str:
    """The supported language the client prefers most; no header — the process language."""
    if not header or not header.strip():
        return _process if _process is not None else detect()
    best, best_q = DEFAULT, -1.0
    for item in header.split(","):
        tag, _sep, params = item.strip().partition(";")
        q = 1.0
        for param in params.split(";"):
            key, _eq, value = param.strip().partition("=")
            if key.strip().lower() == "q":
                try:
                    q = float(value)
                except ValueError:
                    q = 0.0
        primary = tag.strip().lower().split("-")[0].split("_")[0]
        if primary not in LANGS or q <= 0:
            continue
        if q > best_q:  # equal weights keep the header's order
            best, best_q = primary, q
    return best


@cache
def catalog() -> dict[str, str | list[str]]:
    """Every mnema/i18n/ru/*.json merged; one key in two files must have one value."""
    merged: dict[str, str | list[str]] = {}
    for path in sorted(_CATALOG_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for key, value in data.items():
            if key in merged and merged[key] != value:
                raise ValueError(f"{path.name}: {key!r} differs from another catalog file")
            merged[key] = value
    return merged


def _(text: str, /, **kw: object) -> str:
    """``text`` is the English source; Russian from the catalog when the language is ru."""
    if current() == "ru":
        value = catalog().get(text)
        if isinstance(value, str):
            text = value
    return text.format(**kw) if kw else text


def ru_form(n: int) -> int:
    """Index of the Russian plural form: 0 — 1, 21; 1 — 2–4, 22; 2 — 5+, 11–14, 0."""
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return 0
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return 1
    return 2


def _n(singular: str, plural: str, n: int, /, **kw: object) -> str:
    """Plural by ``n``; the Russian entry under ``singular`` holds [one, few, many]."""
    text = singular if n == 1 else plural
    if current() == "ru":
        value = catalog().get(singular)
        if isinstance(value, list) and len(value) == 3:
            text = value[ru_form(n)]
    return text.format(n=n, **kw)


__all__ = [
    "DEFAULT",
    "ENV",
    "LANGS",
    "_",
    "_n",
    "catalog",
    "current",
    "detect",
    "from_accept_language",
    "month_short",
    "normalize",
    "ru_form",
    "set_language",
    "using",
]
