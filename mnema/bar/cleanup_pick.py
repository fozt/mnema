"""The "Delete audio and video…" window on Linux: what to delete, as checkboxes (CleanupAsk).

GTK 3 in its own process (`python -m mnema.bar.cleanup_pick '<CleanupAsk JSON>'`, like
range_pick: the tray runs its own GTK loop); without GTK, zenity's checklist. macOS draws the same
request in macos/Dialogs.swift.
"""

from __future__ import annotations

import json
import sys

from .protocol import CleanupAsk

CANCELLED = 3  # window exit code for Cancel; any other non-zero means no window, use the fallback


def zenity_command(ask: CleanupAsk) -> list[str]:
    """zenity cannot disable a row: a line with nothing to delete is left out."""
    rows = [
        value
        for c in ask.choices
        if c.enabled
        for value in ("TRUE" if c.checked else "FALSE", c.key, c.title)
    ]
    return [
        "zenity", "--list", "--checklist", f"--title={ask.title}", f"--text={ask.message}",
        "--column=", "--column=", "--column=", "--hide-column=2", "--print-column=2",
        "--separator=,", "--hide-header", f"--ok-label={ask.ok}", f"--cancel-label={ask.cancel}",
        *rows,
    ]  # fmt: skip


def zenity_answer(ask: CleanupAsk, code: int, stdout: str) -> list[str] | None:
    """The checked keys in the window's order; Cancel or nothing checked — None."""
    if code != 0:
        return None
    picked = {key for key in stdout.strip().split(",") if key}
    chosen = [c.key for c in ask.choices if c.enabled and c.key in picked]
    return chosen or None


def _gtk_dialog(ask: CleanupAsk) -> list[str] | None:
    import gi  # ty: ignore[unresolved-import] - PyGObject is Linux-only

    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk  # ty: ignore[unresolved-import]

    dialog = Gtk.Dialog(title=ask.title)
    dialog.add_button(ask.cancel, Gtk.ResponseType.CANCEL)
    ok = dialog.add_button(ask.ok, Gtk.ResponseType.OK)
    ok.get_style_context().add_class("destructive-action")
    dialog.set_default_response(Gtk.ResponseType.CANCEL)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=16)
    boxes: list[tuple[str, Gtk.CheckButton]] = []

    def update(*_args: object) -> None:
        ok.set_sensitive(any(b.get_active() for _key, b in boxes))

    for choice in ask.choices:
        check = Gtk.CheckButton(label=choice.title, active=choice.checked and choice.enabled)
        check.set_sensitive(choice.enabled)
        check.connect("toggled", update)
        box.pack_start(check, False, False, 0)
        if choice.detail:
            detail = Gtk.Label(label=choice.detail, xalign=0, margin_start=26)
            detail.set_line_wrap(True)
            detail.get_style_context().add_class("dim-label")
            box.pack_start(detail, False, False, 0)
        boxes.append((choice.key, check))
    if ask.message:
        message = Gtk.Label(label=ask.message, xalign=0, margin_top=6)
        message.set_line_wrap(True)
        box.pack_start(message, False, False, 0)
    update()
    dialog.get_content_area().add(box)
    dialog.show_all()
    response = dialog.run()
    chosen = [key for key, b in boxes if b.get_active()]
    dialog.destroy()
    return chosen if response == Gtk.ResponseType.OK and chosen else None


def main() -> None:
    """Linux: the GTK window; request — CleanupAsk JSON, answer — the checked keys on stdout."""
    answer = _gtk_dialog(CleanupAsk.model_validate(json.loads(sys.argv[1])))
    if answer is None:
        sys.exit(CANCELLED)
    print(json.dumps(answer))


if __name__ == "__main__":
    main()
