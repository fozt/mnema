"""The real GTK recordings window and search on an isolated display, no window on the desktop."""


def test_library_sections_filter_actions_and_refresh_preserve_search(gtk_process):
    gtk_process("""
from mnema.bar.linux_library import LibraryWindow
from mnema.bar.protocol import Button, Card, Notice, PanelModel, SettingsModel, StatusItem

def walk(widget):
    yield widget
    if isinstance(widget, Gtk.Container):
        for child in widget.get_children():
            yield from walk(child)

events = []
model = PanelModel(screen="idle", status=StatusItem(icon="idle"), settings=SettingsModel(tabs=[]),
    sections=["Работа", "Пустой"], cards=[
        Card(id="a", title="План", subtitle="Сегодня", section="Работа", open_key="open:a",
             gist="Невидимое слово", detail="Невидимое слово",
             menu=[Button(key="record:a:folder", title="Открыть папку"),
                   Button(key="record:a:trash", title="Удалить запись", divider=True)]),
        Card(id="b", title="Звонок", subtitle="Вчера", open_key="open:b"),
    ], cleanup=Button(key="cleanup_audio", title="Удалить звук обработанных записей…"))
window = LibraryWindow(events.append)
assert not window.window.get_visible()
window.show(model)
assert window.visible
assert window.cleanup.get_visible()
assert window.cleanup.get_label() == "Удалить звук обработанных записей…"
def labels():
    return [w.get_text() for w in walk(window.scroll) if isinstance(w, Gtk.Label)]
# All sections: an empty section shows only when it is selected itself
assert "Раздел пуст" not in labels() and "План" in labels() and "Звонок" in labels()
original = list(walk(window.scroll))
actions = next(w for w in original if isinstance(w, Gtk.MenuButton))
popup = actions.get_popover()
assert sum(isinstance(w, Gtk.Separator) for w in walk(popup)) == 1  # a separator before delete
popup.popup()
timer = model.model_copy(deep=True)
timer.status.text = "00:32"
window.update(timer)
assert list(walk(window.scroll)) == original and actions.get_popover() is popup
assert popup.get_visible()
popup.popdown()
window.search.set_text("пЛаН")
window.search.emit("search-changed")
assert "План" in labels() and "Звонок" not in labels()
assert not window.sections.get_sensitive()  # search covers all sections
changed = model.model_copy(deep=True)
changed.cards[0].subtitle = "Новое время"
window.update(changed)
assert window.search.get_text() == "пЛаН" and "Новое время" in labels()
button = next(w for w in walk(window.scroll)
              if isinstance(w, Gtk.Button) and w.get_label() == "План")
button.clicked()
window.cleanup.clicked()
assert events == ["open:a", "cleanup_audio"]
window.search.set_text("Невидимое")
window.search.emit("search-changed")
assert "Ничего не найдено" in labels()
window.search.set_text("")
window.search.emit("search-changed")
window.sections.set_active_id("section:Пустой")
assert "Раздел пуст" in labels() and "План" not in labels()
bulk = changed.model_copy(deep=True)
bulk.notices = [Notice(text="Две записи не найдены", buttons=[
    Button(key="sync_delete:a,b", title="Удалить везде"),
    Button(key="sync_restore:a,b", title="Вернуть с сервера"),
]), Notice(text="Синхронизация: узел недоступен")]
window.update(bulk)
assert "Две записи не найдены" in labels() and "Синхронизация: узел недоступен" in labels()
next(w for w in walk(window.scroll)
     if isinstance(w, Gtk.Button) and w.get_label() == "Вернуть с сервера").clicked()
assert events[-1] == "sync_restore:a,b"
window.update(bulk.model_copy(update={"cleanup": None}))  # no recordings, no button
assert not window.cleanup.get_visible()
window.close()
assert not window.visible
""")
