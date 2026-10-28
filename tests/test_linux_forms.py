"""Real GTK 3 fields and signals on a separate Broadway display, no desktop."""

from __future__ import annotations


def test_secret_connection_survives_error_without_reopening(gtk_process):
    gtk_process(r"""
import os
from mnema.bar.forms import ServerFlow
from mnema.bar.linux_forms import FormWindow

os.environ['MNEMA_RUNNER'] = 'https://saved-server'
os.environ['MNEMA_TOKEN'] = 'previous-secret'
flow = ServerFlow()
events = []
def event(value):
    events.append(value)
    if value:
        answer = flow.submit(value.values, force=False)
        assert answer is not None
        view.update(answer.model_copy(update={'rev': value.rev}))
window = Gtk.OffscreenWindow()
view = FormWindow(flow.ask(), event, window=window)
entry = view.controls['connection']
assert entry.get_text() == '' and not entry.get_visibility()
assert 'previous-secret' not in entry.get_placeholder_text()
entry.set_text('invalid server address')
view.submit.clicked()
assert view.error.get_text() and entry.get_text() == 'invalid server address'
assert view.window is window and view.controls['connection'] is entry
view.cancel.clicked()
assert events[-1] is None and events.count(None) == 1
assert os.environ['MNEMA_TOKEN'] == 'previous-secret'
""")


def test_form_revision_busy_state_and_force(gtk_process):
    gtk_process(r"""
from mnema.bar.linux_forms import FormWindow
from mnema.bar.protocol import FormAsk, FormField

events = []
ask = FormAsk(title='Сервер', fields=[FormField(id='connection', label='Приглашение')])
view = FormWindow(ask, events.append, window=Gtk.OffscreenWindow())
entry = view.controls['connection']
assert entry.get_text() == '' and not entry.get_visibility()
entry.set_text('typed-invite')
assert events == []  # the engine hears only about button presses
view.submit.clicked()
assert events[-1].values['connection'] == 'typed-invite' and events[-1].rev == 1
view.update(ask.model_copy(update={'rev': 0, 'error': 'устаревшая ошибка'}))
assert view.error.get_text() == ''
view.update(ask.model_copy(update={'rev': 1, 'busy': 'Проверяю…', 'force': 'Всё равно'}))
assert not view.submit.get_sensitive() and not entry.is_sensitive()
assert not view.force.get_sensitive() and view.cancel.get_sensitive()
view.update(ask.model_copy(update={'rev': 1, 'error': '<invalid key>', 'force': 'Всё равно'}))
assert view.error.get_text() == '<invalid key>' and view.submit.get_sensitive()
assert entry.get_text() == 'typed-invite'
view.force.clicked()
assert events[-1].op.value == 'force' and events[-1].values['connection'] == 'typed-invite'
assert events[-1].rev == 2
view.close()
""")


def test_form_updates_from_worker_thread_via_glib(gtk_process):
    gtk_process(r"""
import threading, time
from mnema.bar.linux_forms import open_form
from mnema.bar.protocol import FormAsk, FormField

ask = FormAsk(title='Thread form', fields=[FormField(id='connection', label='Приглашение')])
events = []
update = open_form(ask, events.append)
def pump(until):
    deadline = time.monotonic() + 3
    while not until() and time.monotonic() < deadline:
        while GLib.MainContext.default().pending():
            GLib.MainContext.default().iteration(False)
        time.sleep(.01)
    assert until()
def windows():
    return [w for w in Gtk.Window.list_toplevels() if w.get_title() == 'Thread form']
pump(lambda: bool(windows()))
window = windows()[0]
def descendants(widget):
    yield widget
    if isinstance(widget, Gtk.Container):
        for child in widget.get_children():
            yield from descendants(child)
entry = next(w for w in descendants(window) if w.get_name() == 'mnema-form-connection')
entry.set_text('typed-invite')
worker = threading.Thread(target=lambda: update(ask.model_copy(update={'error': 'Invalid'})))
worker.start(); worker.join()
pump(lambda: any(isinstance(w, Gtk.Label) and w.get_text() == 'Invalid'
                 for w in descendants(window)))
assert windows() == [window] and entry.get_text() == 'typed-invite'
worker = threading.Thread(target=lambda: update(None))
worker.start(); worker.join()
pump(lambda: not windows())
assert events == []  # a close by the engine is not reported as a user cancel
""")
