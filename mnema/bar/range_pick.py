"""The Process recording window: which part (Start and End sliders with time fields), title,
language, recognition, which tracks, and what to do with the full recording.

RangePick is the platform-neutral window state: a slider moves the time in its field, a typed
time moves the slider, start never passes end; the caption says how much is selected. Windows:
macOS — macos/Dialogs.swift (state port — RangePick.swift); Linux — GTK 3 in its own process
(`python -m mnema.bar.range_pick '<request JSON>'`: the tray runs its own GTK loop).
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable, Iterator

from pydantic import BaseModel, field_validator

from ..core.base import LANG_NAMES, MediaError
from ..i18n import _, current

# English sources; the public TITLE, NAME_PLACEHOLDER, REPLACE_LABEL and SOURCES_LABEL resolve in
# __getattr__, in the language current when they are read.
_TITLE = "Process recording"
_NAME_PLACEHOLDER = "Recording title"
_REPLACE_LABEL = "Move the full recording to the Trash"
_SOURCES_LABEL = "Sources"
CANCELLED = 3  # window exit code for Cancel; any other non-zero means no window, use the fallback

# Language names for language_names(); Russian ones are LANG_NAMES (lowercase, as in prompts).
_EN_NAMES = {
    "ru": "Russian",
    "en": "English",
    "uk": "Ukrainian",
    "de": "German",
    "fr": "French",
    "es": "Spanish",
}


def __getattr__(name: str) -> str:
    """The window's labels as shown, never frozen at this module's import."""
    if name == "TITLE":
        return _(_TITLE)
    if name == "NAME_PLACEHOLDER":
        return _(_NAME_PLACEHOLDER)
    if name == "REPLACE_LABEL":
        return _(_REPLACE_LABEL)
    if name == "SOURCES_LABEL":
        return _(_SOURCES_LABEL)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class _Langs:
    """Language choices: "auto" goes by the call's speech, a chosen one is strict. Iterates
    (code, title) pairs translated at iteration time, so `dict(LANGS)` and `for key, title in
    LANGS` show the current language."""

    def __iter__(self) -> Iterator[tuple[str, str]]:
        yield "auto", _("Automatic")
        yield "ru", _("Russian")
        yield "en", _("English")


LANGS = _Langs()
_CHOICE_ORDER = ("auto", "ru", "en")


def _language_name(code: str) -> str:
    """An unknown code stays as is."""
    return LANG_NAMES.get(code, code) if current() == "ru" else _EN_NAMES.get(code, code)


def language_names(codes: Iterable[str]) -> str:
    """Like "Russian and German": the window's languages first, then the rest; empty — languages
    unknown. Starts with a capital unless the first is an unknown code."""
    found = set(codes)
    ordered = [k for k in _CHOICE_ORDER if k in found] + sorted(found - set(_CHOICE_ORDER))
    names = [_language_name(k) for k in ordered]
    if not names:
        return ""
    text = names[0]
    for name in names[1:]:
        text = _("{first} and {second}", first=text, second=name)
    if ordered[0] not in LANG_NAMES:
        return text
    # English names are capitalized already; capitalize() would lowercase "German"
    return text.capitalize() if current() == "ru" else text


class SourceChoice(BaseModel):
    """A track of the folder: label — mic | system; on — checked at first."""

    label: str
    title: str
    on: bool = True


class RangeRequest(BaseModel):
    length: float  # seconds
    subtitle: str = ""  # "Team daily · 27.09 · 5:12:40"
    name: str = ""  # title to prefill; empty — named by content
    replace: bool = False  # the Trash checkbox, as last time
    # a Mnema recording; otherwise no checkbox: never trash a foreign folder
    can_replace: bool = True
    asr: str = "auto"  # default recognition (the Processing setting)
    asrs: list[tuple[str, str]] = []  # choices (value, title)
    sources: list[SourceChoice] = []  # the folder's tracks; the choice shows with more than one


class RangeAnswer(BaseModel):
    start: str  # as typed; the caller validates
    end: str
    title: str = ""
    replace: bool = False
    lang: str = "auto"  # a LANGS code
    asr: str = ""  # recognition for this recording; empty — as in settings
    sources: list[str] | None = None  # checked tracks; None — the window did not ask

    @field_validator("sources")
    @classmethod
    def _some_source(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and not value:
            raise ValueError("at least one source is required")
        return value


def unasked_sources(req: RangeRequest) -> list[str] | None:
    """Tracks when the window has no choice: the only track, or None without tracks."""
    return [s.label for s in req.sources] or None


class RangePick(BaseModel):
    length: float
    start: float = 0.0
    end: float | None = None  # None — to the end of the recording

    def model_post_init(self, _context: object) -> None:
        if self.end is None:
            self.end = self.length

    def set_start(self, value: float, *, push: bool = True) -> None:
        """push — a slider: a start past the end drags the end along. While a time is typed
        (push=False) the other edge stays: "0:5" on the way to "0:52" must not move it."""
        value = min(max(0.0, value), self.length)
        if push or value <= (self.end or 0.0):
            self.start = value
            self.end = max(self.end or 0.0, value)

    def set_end(self, value: float, *, push: bool = True) -> None:
        value = min(max(0.0, value), self.length)
        if push or value >= self.start:
            self.end = value
            self.start = min(self.start, value)

    def whole(self) -> bool:
        return whole(self.start, self.end or 0.0, self.length)

    def caption(self) -> str:
        from ..live.setup import fmt_clock

        if self.whole():
            return _("Whole recording selected")
        return _("Selected {length}", length=fmt_clock((self.end or 0.0) - self.start))


def whole(start: float, end: float, length: float) -> bool:
    """The fields show whole seconds: 1:35:00 of 1:35:00.8 is the whole recording (fixture
    offsets-v1)."""
    return start < 1 and end >= length - 1


def typed(text: str) -> float | None:
    """Time from a field being typed; None — not a time yet (the slider stays)."""
    from ..live.setup import parse_offset

    try:
        return parse_offset(text) if text.strip() else None
    except MediaError:
        return None


def _gtk_dialog(req: RangeRequest) -> RangeAnswer | None:
    import gi  # ty: ignore[unresolved-import] - PyGObject is Linux-only

    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk  # ty: ignore[unresolved-import]

    from ..live.setup import fmt_clock

    pick = RangePick(length=req.length)
    dialog = Gtk.Dialog(title=_(_TITLE))
    dialog.add_button(_("Cancel"), Gtk.ResponseType.CANCEL)
    dialog.add_button(_("Process"), Gtk.ResponseType.OK)
    dialog.set_default_response(Gtk.ResponseType.OK)
    grid = Gtk.Grid(row_spacing=10, column_spacing=12, margin=16)
    heading = Gtk.Label(label=req.subtitle, xalign=0)
    grid.attach(heading, 0, 0, 3, 1)
    caption = Gtk.Label(label=pick.caption(), xalign=0)
    replace = Gtk.CheckButton(label=_(_REPLACE_LABEL), active=req.replace and req.can_replace)
    rows: list[tuple[Gtk.Scale, Gtk.Entry]] = []
    syncing = False

    def sync(editing: Gtk.Entry | None = None) -> None:
        nonlocal syncing  # the field being typed in is never rewritten, even with a bad time
        syncing = True
        for (scale, entry), value in zip(rows, (pick.start, pick.end or 0.0), strict=True):
            scale.set_value(value)
            if entry is not editing and typed(entry.get_text()) != int(value):
                entry.set_text(fmt_clock(value))
        caption.set_label(pick.caption())
        replace.set_sensitive(not pick.whole())
        syncing = False

    for row, (label, setter, value) in enumerate(
        ((_("Start"), pick.set_start, pick.start), (_("End"), pick.set_end, pick.end or 0.0)), 1
    ):
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, max(req.length, 1), 1)
        scale.set_draw_value(False)
        scale.set_hexpand(True)
        scale.set_size_request(360, -1)
        scale.set_value(value)
        entry = Gtk.Entry(text=fmt_clock(value), width_chars=9, activates_default=True)

        def moved(s: Gtk.Scale, set_value=setter) -> None:
            if not syncing:
                set_value(s.get_value())
                sync()

        def edited(e: Gtk.Entry, set_value=setter) -> None:
            seconds = typed(e.get_text())
            if not syncing and seconds is not None:
                set_value(seconds, push=False)
                sync(editing=e)

        def committed(e: Gtk.Entry, *_args, set_value=setter) -> bool:
            seconds = typed(e.get_text())  # Enter or focus out: the other edge follows
            if not syncing and seconds is not None:
                set_value(seconds)
                sync()
            return False

        scale.connect("value-changed", moved)
        entry.connect("changed", edited)
        entry.connect("activate", committed)
        entry.connect("focus-out-event", committed)
        grid.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
        grid.attach(scale, 1, row, 1, 1)
        grid.attach(entry, 2, row, 1, 1)
        rows.append((scale, entry))
    grid.attach(caption, 1, 3, 2, 1)
    name = Gtk.Entry(text=req.name, placeholder_text=_(_NAME_PLACEHOLDER), activates_default=True)
    grid.attach(Gtk.Label(label=_("Title"), xalign=0), 0, 4, 1, 1)
    grid.attach(name, 1, 4, 2, 1)
    lang = Gtk.ComboBoxText(halign=Gtk.Align.START)
    for key, text in LANGS:
        lang.append(key, text)
    lang.set_active_id("auto")
    grid.attach(Gtk.Label(label=_("Language"), xalign=0), 0, 5, 1, 1)
    grid.attach(lang, 1, 5, 2, 1)
    asr = Gtk.ComboBoxText(halign=Gtk.Align.START)
    for key, text in req.asrs:
        asr.append(key, text)
    asr.set_active_id(req.asr)
    grid.attach(Gtk.Label(label=_("Recognition"), xalign=0), 0, 6, 1, 1)
    grid.attach(asr, 1, 6, 2, 1)
    boxes: list[tuple[str, Gtk.CheckButton]] = []

    def ready(*_args: object) -> None:  # at least one track, or there is nothing to process
        some = any(box.get_active() for _label, box in boxes)
        dialog.set_response_sensitive(Gtk.ResponseType.OK, some or not boxes)

    if len(req.sources) > 1:
        line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        for source in req.sources:
            box = Gtk.CheckButton(label=source.title, active=source.on)
            box.connect("toggled", ready)
            line.pack_start(box, False, False, 0)
            boxes.append((source.label, box))
        grid.attach(Gtk.Label(label=_(_SOURCES_LABEL), xalign=0), 0, 7, 1, 1)
        grid.attach(line, 1, 7, 2, 1)
    if req.can_replace:
        grid.attach(replace, 1, 8, 2, 1)
    replace.set_sensitive(not pick.whole())
    dialog.get_content_area().add(grid)
    dialog.show_all()
    ready()
    ok = dialog.run() == Gtk.ResponseType.OK
    chosen = [label for label, box in boxes if box.get_active()]
    answer = None
    if ok and (chosen or not boxes):
        answer = RangeAnswer(
            start=rows[0][1].get_text(),
            end=rows[1][1].get_text(),
            title=name.get_text(),
            replace=replace.get_active() and req.can_replace,
            lang=lang.get_active_id() or "auto",
            asr=asr.get_active_id() or "",
            sources=chosen if boxes else unasked_sources(req),
        )
    dialog.destroy()
    return answer


def main() -> None:
    """Linux: the GTK window; request — RangeRequest JSON, answer — RangeAnswer JSON on stdout."""
    answer = _gtk_dialog(RangeRequest.model_validate(json.loads(sys.argv[1])))
    if answer is None:
        sys.exit(CANCELLED)
    print(answer.model_dump_json())


if __name__ == "__main__":
    main()
