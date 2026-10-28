"""One GTK 3 window drawn from the shared SettingsModel. Call from the GLib main loop."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..i18n import _
from .protocol import FieldKind, Role, SettingsField, SettingsModel, SettingsTab, Tone
from .settings_model import _lower


class SettingsWindow:
    def __init__(self, on_action: Callable[[str], None]):
        import gi

        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        from gi.repository import Gdk, Gtk  # ty: ignore[unresolved-import]

        self._gtk = Gtk
        self._on_action = on_action
        self._model: SettingsModel | None = None
        self._pages: dict[str, Any] = {}
        self._rows: dict[str, list[Any]] = {}
        self.window = Gtk.Window(title=_("Settings"))
        self.window.set_default_size(680, 520)
        self.window.set_position(Gtk.WindowPosition.CENTER)
        self.window.set_role("mnema-settings")
        self.window.connect("delete-event", self._hide)
        self.window.connect(
            "key-press-event",
            lambda _w, event: self._hide() if event.keyval == Gdk.KEY_Escape else False,
        )
        self.notebook = Gtk.Notebook()
        self.notebook.set_scrollable(True)
        self.window.add(self.notebook)

    def _hide(self, *_args: Any) -> bool:
        self.window.hide()
        return True

    def close(self) -> None:
        self.window.destroy()

    def show(self, model: SettingsModel, *, tab: str | None = None) -> None:
        """tab — open on this tab; None — on the previous one."""
        self.update(model)
        ids = [t.id for t in model.tabs]
        if tab in ids:
            self.notebook.set_current_page(ids.index(tab))
        self.window.show_all()
        self.window.present()

    def update(self, model: SettingsModel) -> None:
        """Rebuild changed tabs, keeping the selected tab and the scroll position."""
        if model == self._model:
            return
        Gtk = self._gtk
        previous = {tab.id: tab for tab in self._model.tabs} if self._model else {}
        old_ids = list(previous)
        selected = self.notebook.get_current_page()
        selected_id = old_ids[selected] if 0 <= selected < len(old_ids) else None
        if old_ids != [tab.id for tab in model.tabs]:
            while self.notebook.get_n_pages():
                self.notebook.remove_page(0)
            self._pages.clear()
            self._rows.clear()
            previous = {}
        for tab in model.tabs:
            if tab.id not in self._pages:
                page = Gtk.ScrolledWindow()
                page.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
                self.notebook.append_page(page, Gtk.Label(label=tab.title))
                self._pages[tab.id] = page
            page = self._pages[tab.id]
            self.notebook.set_tab_label_text(page, tab.title)
            if tab == previous.get(tab.id):
                continue
            if self._refresh_fields(tab, previous.get(tab.id)):
                continue
            offset = page.get_vadjustment().get_value()
            self._rows[tab.id] = []
            if child := page.get_child():
                page.remove(child)
                child.destroy()
            body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20, margin=20)
            for section in tab.sections:
                group = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
                if section.title:
                    heading = self._label(section.title)
                    heading.get_style_context().add_class("heading")
                    group.pack_start(heading, False, False, 0)
                frame = Gtk.Frame()
                rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin=14)
                self._rows[tab.id].append(rows)
                for field in section.fields:
                    rows.pack_start(self._field(field), False, False, 0)
                frame.add(rows)
                group.pack_start(frame, False, False, 0)
                body.pack_start(group, False, False, 0)
            page.add(body)
            page.show_all()
            page.get_vadjustment().set_value(offset)
        self.window.set_title(model.title)
        self._model = model.model_copy(deep=True)
        if selected_id is not None:
            ids = [tab.id for tab in model.tabs]
            if selected_id in ids:
                self.notebook.set_current_page(ids.index(selected_id))

    def _refresh_fields(self, tab: SettingsTab, previous: SettingsTab | None) -> bool:
        if previous is None or len(tab.sections) != len(previous.sections):
            return False
        for current, old in zip(tab.sections, previous.sections, strict=True):
            if current.title != old.title or len(current.fields) != len(old.fields):
                return False
            if [(f.kind, f.key) for f in current.fields] != [(f.kind, f.key) for f in old.fields]:
                return False
        for current, old, rows in zip(
            tab.sections, previous.sections, self._rows[tab.id], strict=True
        ):
            for index, (field, before, widget) in enumerate(
                zip(current.fields, old.fields, rows.get_children(), strict=True)
            ):
                if field == before:
                    continue
                rows.remove(widget)
                widget.destroy()
                replacement = self._field(field)
                rows.pack_start(replacement, False, False, 0)
                rows.reorder_child(replacement, index)
                replacement.show_all()
        return True

    def _label(self, text: str, *, secondary: bool = False) -> Any:
        label = self._gtk.Label(label=text, xalign=0)
        label.set_line_wrap(True)
        label.set_max_width_chars(42)
        label.set_selectable(True)
        if secondary:
            label.get_style_context().add_class("dim-label")
        return label

    def _send(self, key: str | None, enabled: bool = True) -> None:
        if enabled and key:
            self._on_action(key)

    def _button(self, title: str, key: str | None, enabled: bool, role: Role = Role.NORMAL) -> Any:
        button = self._gtk.Button(label=title)
        button.set_sensitive(enabled)
        if role == Role.DESTRUCTIVE:
            button.get_style_context().add_class("destructive-action")
        elif role == Role.PRIMARY:
            button.get_style_context().add_class("suggested-action")
        button.connect("clicked", lambda _b: self._send(key, enabled))
        return button

    def _status(self, field: SettingsField) -> Any:
        Gtk = self._gtk
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        if field.tone == Tone.BUSY:
            spinner = Gtk.Spinner()
            spinner.start()
            line.pack_start(spinner, False, False, 0)
        elif field.tone != Tone.NONE:
            icon = {
                Tone.OK: "emblem-ok-symbolic",
                Tone.WARN: "dialog-warning-symbolic",
                Tone.ERROR: "dialog-error-symbolic",
            }[field.tone]
            line.pack_start(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.MENU), False, False, 0)
        line.pack_start(self._label(field.value), True, True, 0)
        box.pack_start(line, False, False, 0)
        if field.detail:
            box.pack_start(self._label(field.detail, secondary=True), False, False, 0)
        return box

    def _field(self, field: SettingsField) -> Any:
        Gtk = self._gtk
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        row.set_name(field.key or field.title)
        if field.kind == FieldKind.BUTTON:
            row.set_halign(Gtk.Align.END)
            row.pack_start(
                self._button(field.title, field.key, field.enabled, field.role), False, False, 0
            )
            return row
        heading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        heading.pack_start(self._label(field.title), False, False, 0)
        if field.detail and field.kind != FieldKind.STATUS:
            heading.pack_start(self._label(field.detail, secondary=True), False, False, 0)
        heading.set_hexpand(True)
        row.pack_start(heading, True, True, 0)
        if field.kind == FieldKind.TOGGLE:
            control = Gtk.Switch(active=field.checked, valign=Gtk.Align.CENTER)
            control.connect("notify::active", lambda *_args: self._send(field.key, field.enabled))
        elif field.kind == FieldKind.CHOICE:
            # Sensitivity is set per choice, not only for the whole list.
            choices = Gtk.ListStore(str, str, bool)
            for choice in field.choices:
                choices.append(
                    [
                        choice.key,
                        " · ".join(filter(None, (choice.title, _lower(choice.detail)))),
                        choice.enabled,
                    ]
                )
            control = Gtk.ComboBox.new_with_model(choices)
            renderer = Gtk.CellRendererText()
            renderer.set_property("wrap-width", 250)
            control.pack_start(renderer, True)
            control.add_attribute(renderer, "text", 1)
            control.add_attribute(renderer, "sensitive", 2)
            active = next((i for i, ch in enumerate(field.choices) if ch.checked), -1)
            control.set_active(active)
            restoring = False

            def changed(combo: Any) -> None:
                nonlocal restoring
                index = combo.get_active()
                if index < 0 or restoring:
                    return
                choice = field.choices[index]
                if not choice.enabled or not field.enabled:
                    restoring = True
                    combo.set_active(active)
                    restoring = False
                    return
                self._send(choice.key)

            control.connect("changed", changed)
        elif field.kind == FieldKind.STATUS:
            control = self._status(field)
        elif field.kind == FieldKind.HOTKEY:
            control = Gtk.Entry(text=field.value, placeholder_text=field.placeholder)
            control.connect(
                "activate",
                lambda entry: self._send(f"{field.key}:{entry.get_text().strip()}", field.enabled),
            )
        else:
            raise ValueError(f"unknown settings field: {field.kind}")
        control.set_sensitive(field.enabled)
        row.pack_start(control, False, False, 0)
        if not field.error:
            return row
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.pack_start(row, False, False, 0)
        error = self._label(field.error)
        error.get_style_context().add_class("error")
        box.pack_start(error, False, False, 0)
        return box
