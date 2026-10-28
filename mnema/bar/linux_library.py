"""The GTK 3 recordings window the user opens; actions stay with the runtime."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..i18n import _
from .panel import card_subtitle, visible_cards
from .protocol import Button, PanelModel


class LibraryWindow:
    def __init__(self, on_action: Callable[[str], None]) -> None:
        import gi  # ty: ignore[unresolved-import] — GTK is Linux-only

        gi.require_version("Gtk", "3.0")
        from gi.repository import GLib, Gtk  # ty: ignore[unresolved-import]

        self._gtk, self._on_action = Gtk, on_action
        self._model: PanelModel | None = None
        self._updating = False
        self.visible = False
        self._closed = False
        self._pulsing: list[Any] = []  # bars without a share pulse while the window is open
        GLib.timeout_add(120, self._pulse)
        self.window = Gtk.Window(title=_("Mnema recordings"))
        self.window.set_default_size(620, 640)
        self.window.set_position(Gtk.WindowPosition.CENTER)
        self.window.connect("delete-event", self._hide)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin=16)
        self.window.add(body)
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.search = Gtk.SearchEntry(placeholder_text=_("Search by title"))
        self.search.connect("search-changed", lambda *_args: self._render())
        controls.pack_start(self.search, True, True, 0)
        self.sections = Gtk.ComboBoxText()
        self.sections.connect("changed", lambda *_args: self._render())
        controls.pack_start(self.sections, False, False, 0)
        body.pack_start(controls, False, False, 0)
        self.scroll = Gtk.ScrolledWindow()
        self.scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        body.pack_start(self.scroll, True, True, 0)
        self.cleanup = Gtk.Button()  # label and visibility come from the model
        self.cleanup.set_no_show_all(True)
        self.cleanup.connect("clicked", lambda *_args: self._on_action("cleanup_audio"))
        body.pack_start(self.cleanup, False, False, 0)

    def _hide(self, *_args: Any) -> bool:
        self.visible = False
        self.window.hide()
        return True

    def close(self) -> None:
        self.visible = False
        self._closed = True
        self._pulsing = []
        self.window.destroy()

    def _pulse(self) -> bool:
        """GLib tick: pulse the bars without a share; once the window is closed for good, stop."""
        if self.visible:
            for bar in self._pulsing:
                bar.pulse()
        return not self._closed

    def show(self, model: PanelModel) -> None:
        self.visible = True
        self.update(model)
        self.window.show_all()
        self.window.present()

    def update(self, model: PanelModel) -> None:
        old = self._model
        self._model = model
        if old is not None and (
            model.cards,
            model.sections,
            model.cleanup,
            model.empty,
            model.notices,
        ) == (
            old.cards,
            old.sections,
            old.cleanup,
            old.empty,
            old.notices,
        ):
            return
        selected = self.sections.get_active_id()
        names = sorted(set(model.sections) | {c.section for c in model.cards})
        self._updating = True
        self.sections.remove_all()
        self.sections.append("all", _("All sections"))
        for name in names:
            self.sections.append("section:" + name, name or _("No section"))
        self.sections.set_active_id(
            selected if selected in {"section:" + n for n in names} else "all"
        )
        if model.cleanup is None:  # no recordings, no button
            self.cleanup.hide()
        else:
            self.cleanup.set_label(model.cleanup.title)
            self.cleanup.set_sensitive(model.cleanup.enabled)
            self.cleanup.show()
        self._updating = False
        self._render()

    def _label(self, text: str, *, secondary: bool = False) -> Any:
        label = self._gtk.Label(label=text, xalign=0)
        label.set_line_wrap(True)
        label.set_selectable(True)
        if secondary:
            label.get_style_context().add_class("dim-label")
        return label

    def _button(self, model: Button) -> Any:
        button = self._gtk.Button(label=model.title)
        button.set_sensitive(model.enabled)
        button.connect("clicked", lambda *_args: self._on_action(model.key))
        return button

    def _submenu(self, model: Button) -> Any:
        """An item with a submenu (Move to section): a button that opens the choices; the checked
        one shows a radio mark."""
        Gtk = self._gtk
        button = Gtk.MenuButton(label=model.title)
        button.set_sensitive(model.enabled)
        menu = Gtk.Menu()
        for child in model.children:
            if child.divider:
                menu.append(Gtk.SeparatorMenuItem())
            if child.checked is None:
                item = Gtk.MenuItem(label=child.title)
            else:
                item = Gtk.CheckMenuItem(label=child.title)
                item.set_draw_as_radio(True)
                item.set_active(child.checked)  # before connect: set_active itself fires activate
            item.set_sensitive(child.enabled)
            item.connect("activate", lambda *_args, key=child.key: self._on_action(key))
            menu.append(item)
        menu.show_all()
        button.set_popup(menu)
        return button

    def _render(self) -> None:
        model = self._model
        if self._updating or model is None:
            return
        Gtk = self._gtk
        selected = self.sections.get_active_id() or "all"
        query = self.search.get_text()
        section = None if selected == "all" else selected.removeprefix("section:")
        cards = visible_cards(model, query=query, section=section)
        offset = self.scroll.get_vadjustment().get_value()
        if child := self.scroll.get_child():
            self.scroll.remove(child)
            child.destroy()
        self._pulsing = []  # the old bars are gone with their rows
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for notice in model.notices:
            body.pack_start(self._label(notice.text), False, False, 0)
            choices = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            for button in notice.buttons:
                choices.pack_start(self._button(button), False, False, 0)
            body.pack_start(choices, False, False, 0)
        if not cards:
            empty = _("Nothing found") if query.strip() else model.empty
            if section is not None and model.cards:
                empty = _("Section is empty")
            body.pack_start(self._label(empty, secondary=section is not None), False, False, 0)
        for card in cards:
            frame = Gtk.Frame()
            if card.tooltip:
                frame.set_tooltip_text(card.tooltip)
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=12)
            title = Gtk.Button(label=card.title)  # a click opens the recording folder
            title.set_relief(Gtk.ReliefStyle.NONE)
            title.set_sensitive(card.open_key is not None)
            title.connect(
                "clicked",
                lambda *_args, key=card.open_key: self._on_action(key) if key else None,
            )
            row.pack_start(title, False, False, 0)
            for text in (card_subtitle(card, section), card.detail):
                if text:
                    row.pack_start(self._label(text, secondary=True), False, False, 0)
            if card.busy:  # processing: the step is in detail, the bar below it
                bar = Gtk.ProgressBar()
                if card.progress is None:  # no share: a pulsing bar (the _pulse tick)
                    bar.set_pulse_step(0.1)
                    self._pulsing.append(bar)
                else:
                    bar.set_fraction(card.progress)
                row.pack_start(bar, False, False, 0)
            actions = Gtk.MenuButton(label=_("Actions"))
            popup = Gtk.Popover.new(actions)
            menu = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin=8)
            for button in card.menu:
                if button.divider:
                    separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
                    menu.pack_start(separator, False, False, 0)
                item = self._submenu(button) if button.children else self._button(button)
                menu.pack_start(item, False, False, 0)
            popup.add(menu)
            menu.show_all()
            actions.set_popover(popup)
            row.pack_start(actions, False, False, 0)
            frame.add(row)
            body.pack_start(frame, False, False, 0)
        self.scroll.add(body)
        self.scroll.show_all()
        self.scroll.get_vadjustment().set_value(offset)
