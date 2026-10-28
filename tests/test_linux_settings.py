"""Real GTK widgets on an isolated display."""


def test_settings_widgets_actions_disabled_and_updates(gtk_process):
    gtk_process("""
        from mnema.bar.linux_settings import SettingsWindow
        from mnema.bar.protocol import (
            Choice, FieldKind as K, SettingsField as F,
            SettingsModel, SettingsTab, SettingsSection,
        )

        def walk(widget):
            yield widget
            if isinstance(widget, Gtk.Container):
                for child in widget.get_children():
                    yield from walk(child)

        fields = [
            F(kind=K.TOGGLE, title="Микрофон", key="toggle_mic", checked=True),
            F(kind=K.TOGGLE, title="Пересказ", key="summary", enabled=False,
              detail="Выберите облачную модель"),
            F(kind=K.CHOICE, title="Обработка", choices=[
                Choice(key="where:local", title="Локально", checked=True),
                Choice(key="where:server", title="Сервер", enabled=False),
                Choice(key="where:other", title="Другой"),
            ]),
            F(kind=K.HOTKEY, title="Сочетание", key="hotkey", value="ctrl+R"),
        ]
        model = SettingsModel(tabs=[
            SettingsTab(id="first", title="Запись", symbol="mic", sections=[]),
            SettingsTab(id="second", title="Обработка", symbol="gear",
                        sections=[SettingsSection(fields=fields)]),
        ])
        events = []
        window = SettingsWindow(events.append)
        window.show(model)
        window.notebook.set_current_page(1)
        widgets = list(walk(window.window))
        labels = [w.get_text() for w in widgets if isinstance(w, Gtk.Label)]
        assert "Выберите облачную модель" in labels
        switches = [w for w in widgets if isinstance(w, Gtk.Switch)]
        switches[0].set_active(False)
        switches[1].set_active(True)
        combo = next(w for w in widgets if isinstance(w, Gtk.ComboBox))
        combo.set_active(1)
        assert combo.get_active() == 0  # a disabled server cannot be chosen
        combo.set_active(2)
        entry = next(w for w in widgets if isinstance(w, Gtk.Entry))
        entry.set_text("ctrl+T")
        entry.emit("activate")
        expected = ["toggle_mic", "where:other", "hotkey:ctrl+T"]
        assert events == expected, events

        updated = model.model_copy(deep=True)
        updated.tabs[1].sections[0].fields[3].value = "ctrl+Y"
        window.update(updated)
        assert window.notebook.get_current_page() == 1
        assert any(isinstance(w, Gtk.Entry) and w.get_text() == "ctrl+Y"
                   for w in walk(window.window))
        assert events == expected
        window.window.emit("delete-event", None)
        assert not window.window.get_visible()
        window.show(updated)
        assert window.window.get_visible() and window.notebook.get_current_page() == 1
        window.close()
    """)


def test_settings_keep_one_window_and_its_page(gtk_process):
    gtk_process("""
        from mnema.bar.app_linux import TrayApp

        def pump():
            while Gtk.events_pending():
                Gtk.main_iteration_do(False)

        def walk(widget):
            yield widget
            if isinstance(widget, Gtk.Container):
                for child in widget.get_children():
                    yield from walk(child)

        tray = TrayApp()
        tray._schedule_settings(show=True)
        tray._schedule_settings(show=True)
        pump()
        window = next(w for w in Gtk.Window.list_toplevels() if w.get_title() == "Настройки")
        notebook = next(w for w in walk(window) if isinstance(w, Gtk.Notebook))
        assert notebook.get_n_pages() == 5
        notebook.set_current_page(1)
        tray._schedule_settings(show=True)
        pump()
        assert [w for w in Gtk.Window.list_toplevels() if w.get_title() == "Настройки"] == [window]
        assert notebook.get_current_page() == 1
        assert not any(isinstance(w, Gtk.SpinButton) for w in walk(window))
        notebook.set_current_page(3)
        tray.runtime.handle("choose_server")  # "Choose server" on a recording card
        pump()
        assert notebook.get_current_page() == 1  # the Processing tab: where to process
        window.destroy()
    """)
