"""A live GTK 3 form from the shared FormAsk: updated in the same window, input kept until the
answer. Secret fields never show what was typed."""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable
from typing import Any

from .forms import FormEvent
from .protocol import FormAsk, FormField, FormOp

logger = logging.getLogger(__name__)


class FormWindow:
    """Construction and update run on the GTK thread; open_form queues them on GLib."""

    def __init__(
        self,
        ask: FormAsk,
        on_event: Callable[[FormEvent | None], None],
        *,
        parent: Any = None,
        window: Any = None,
    ) -> None:
        gi = importlib.import_module("gi")
        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        from gi.repository import Gdk, GLib, Gtk  # ty: ignore[unresolved-import]

        self._gtk, self._glib = Gtk, GLib
        self._on_event = on_event
        self.ask = ask
        self.values = {f.id: "" for f in ask.fields}
        self._last_sent = 0
        self._rendering = False
        self._closed = False
        self._rows: dict[str, tuple[Any, Any, Any]] = {}
        self.controls: dict[str, Any] = {}
        self.window = window if window is not None else Gtk.Window()
        self.window.set_default_size(480, -1)
        self.window.set_resizable(False)
        self.window.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)
        self.window.set_role("mnema-form")
        if parent is not None:
            self.window.set_transient_for(parent)
            self.window.set_modal(True)
        self.window.connect("delete-event", self._cancel)
        self.window.connect(
            "key-press-event",
            lambda _w, event: self._cancel() if event.keyval == Gdk.KEY_Escape else False,
        )
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin=20)
        self.window.add(body)
        self.message = Gtk.Label(xalign=0)
        self.message.set_line_wrap(True)
        body.pack_start(self.message, False, False, 0)
        self.fields = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        body.pack_start(self.fields, False, False, 0)
        self.busy = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.spinner = Gtk.Spinner()
        self.busy.pack_start(self.spinner, False, False, 0)
        self.busy_text = Gtk.Label(xalign=0)
        self.busy.pack_start(self.busy_text, True, True, 0)
        body.pack_start(self.busy, False, False, 0)
        self.error = Gtk.Label(xalign=0)
        self.error.set_line_wrap(True)
        self.error.set_selectable(True)
        body.pack_start(self.error, False, False, 0)
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.force = Gtk.Button()
        self.force.connect("clicked", lambda _b: self._send(FormOp.FORCE))
        buttons.pack_start(self.force, False, False, 0)
        self.submit = Gtk.Button()
        self.submit.get_style_context().add_class("suggested-action")
        self.submit.connect("clicked", lambda _b: self._send(FormOp.SUBMIT))
        buttons.pack_end(self.submit, False, False, 0)
        self.cancel = Gtk.Button()
        self.cancel.connect("clicked", self._cancel)
        buttons.pack_end(self.cancel, False, False, 0)
        body.pack_start(buttons, False, False, 0)
        body.show_all()
        self._render()

    def show(self) -> None:
        self.window.show()
        self.window.present()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.values.clear()
        self.window.destroy()

    def _cancel(self, *_args: Any) -> bool:
        if not self._closed:
            self.close()
            self._on_event(None)
        return True

    def update(self, ask: FormAsk | None) -> None:
        if self._closed:
            return
        if ask is None:
            self.close()
            return
        if ask.rev < self._last_sent:
            return
        for field in ask.fields:
            self.values.setdefault(field.id, "")
        self.ask = ask
        self._render()

    def _send(self, op: FormOp) -> None:
        if self._closed or self.ask.busy:
            return
        self._last_sent += 1
        self._on_event(FormEvent(op=op, values=dict(self.values), rev=self._last_sent))

    def _changed(self, widget: Any, field_id: str) -> None:
        if not self._rendering and not self._closed:
            self.values[field_id] = widget.get_text()

    def _control(self, field: FormField) -> Any:
        widget = self._gtk.Entry(visibility=False)
        widget.connect("activate", lambda _w: self._send(FormOp.SUBMIT))
        widget.set_name(f"mnema-form-{field.id}")
        widget.set_hexpand(True)
        widget.connect("changed", self._changed, field.id)
        return widget

    def _render(self) -> None:
        Gtk = self._gtk
        self._rendering = True
        try:
            self.window.set_title(self.ask.title)
            self.message.set_text(self.ask.message)
            self.message.set_visible(bool(self.ask.message))
            for field in self.ask.fields:
                row = self._rows.get(field.id)
                if row is None:
                    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
                    label = Gtk.Label(xalign=0)
                    widget = self._control(field)
                    label.set_mnemonic_widget(widget)
                    box.pack_start(label, False, False, 0)
                    box.pack_start(widget, False, False, 0)
                    self.fields.pack_start(box, False, False, 0)
                    box.show_all()
                    row = self._rows[field.id] = (box, label, widget)
                    self.controls[field.id] = widget
                box, label, widget = row
                label.set_text(field.label)
                value = self.values.get(field.id, "")
                if widget.get_text() != value:
                    widget.set_text(value)
                widget.set_placeholder_text(field.placeholder)
                box.set_sensitive(not self.ask.busy)
            self.busy_text.set_text(self.ask.busy)
            self.busy.set_visible(bool(self.ask.busy))
            self.spinner.start() if self.ask.busy else self.spinner.stop()
            self.error.set_markup(
                f'<span foreground="#c01c28">{self._glib.markup_escape_text(self.ask.error)}</span>'
            )
            self.error.set_visible(bool(self.ask.error))
            self.cancel.set_label(self.ask.cancel)
            self.submit.set_label(self.ask.ok)
            self.submit.set_sensitive(not self.ask.busy)
            self.force.set_label(self.ask.force)
            self.force.set_visible(bool(self.ask.force))
            self.force.set_sensitive(not self.ask.busy)
        finally:
            self._rendering = False


def open_form(
    ask: FormAsk, on_event: Callable[[FormEvent | None], None], *, parent: Any = None
) -> Callable[[FormAsk | None], None]:
    """The Platform.open_form contract; background engine answers update the window on the GTK
    thread."""
    from gi.repository import GLib  # ty: ignore[unresolved-import]

    windows: list[FormWindow] = []

    def create() -> bool:
        try:
            form = FormWindow(ask, on_event, parent=parent)
            windows.append(form)
            form.show()
        except Exception:
            logger.exception("could not open the form window")
            on_event(None)
        return False

    def update(new: FormAsk | None) -> None:
        def apply() -> bool:
            if windows:
                windows[0].update(new)
            return False

        GLib.idle_add(apply)

    GLib.idle_add(create)
    return update
