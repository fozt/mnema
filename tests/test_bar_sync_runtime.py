"""Desktop lifecycle, UUID actions and bulk choices use the file-sync controller."""

import json
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from mnema.bar import runtime
from mnema.bar.panel import panel_model
from mnema.bar.protocol import SettingsModel
from mnema.bar.recordings import library
from mnema.bar.state import MenuEnv, Phase
from mnema.connection import Connection, parse_connection
from mnema.core import base
from mnema.i18n import set_language
from mnema.sync.engine import SyncStatus
from mnema.sync.state import checkpoint_path


class Controller:
    def __init__(self, root, state_path, **callbacks):
        self.root, self.state_path, self.callbacks = root, state_path, callbacks
        self.starts = self.wakes = self.closes = 0
        self.choices = []

    def start(self):
        self.starts += 1

    def wake(self):
        self.wakes += 1

    def close(self):
        self.closes += 1

    def resolve_bulk(self, ids, *, restore):
        self.choices.append((ids, restore))

    def send_to_library(self, library):
        self.choices.append(library)


class Platform:
    def __init__(self):
        self.opened, self.trashed = [], []
        self.answer = None

    def open(self, path):
        self.opened.append(path)

    def trash(self, path):
        self.trashed.append(path)
        # Represents the platform moving the file/folder away, not a product trash fallback.
        path.rename(path.with_name("trashed-" + path.name))

    def ask_title(self, current, on_done, **kwargs):
        self.answer = on_done

    def confirm(self, title, message, ok, on_done):
        self.asked = (title, message, ok)
        self.answer = on_done

    def ask_range(self, request, on_done):
        self.answer = on_done


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MNEMA_LIVE_DIR", str(tmp_path / "recordings"))
    monkeypatch.setattr(runtime.BarRuntime, "_check_server_async", lambda *a, **k: None)
    monkeypatch.setattr(runtime.BarRuntime, "_load_asr_choices", lambda *a, **k: None)
    monkeypatch.setattr(runtime, "SyncController", Controller)
    platform = Platform()
    updates = []
    rt = runtime.BarRuntime(
        prefs_file=tmp_path / "config/bar.json",
        platform=platform,
        on_change=lambda: updates.append(True),
    )
    return rt, platform, tmp_path / "recordings", updates


def record(root: Path, name="Call", *, audio=False, processed=False):
    folder = root / name
    folder.mkdir(parents=True)
    id = str(uuid4())
    runs = [{"status": "ok"}] if processed else []
    (folder / ".mnema-meta.json").write_text(json.dumps({"id": id, "runs": runs}))
    (folder / "transcript.txt").write_text("Hello")
    if audio:
        (folder / "mic.wav").write_bytes(b"\0" * 64)
    return folder, id


def test_explicit_gui_lifecycle_does_not_recreate_missing_root_and_sync_is_independent(
    app, monkeypatch
):
    rt, platform, root, updates = app
    assert not root.exists() and rt._sync is None
    monkeypatch.setenv("MNEMA_RUNNER", "http://chosen-library")
    monkeypatch.setenv("MNEMA_TOKEN", "library-token")
    rt.start_sync()
    rt.start_sync()
    controller = rt._sync
    assert controller.starts == 1 and not root.exists()
    assert controller.state_path == checkpoint_path()  # the same sync.json as the list
    assert parse_connection(controller.callbacks["selected"]()) == Connection(
        url="http://chosen-library", token="library-token"
    )
    monkeypatch.setattr(base, "STANDALONE", True)  # the Mac processes itself, the server syncs
    assert rt.processing_connection() is None
    assert controller.callbacks["selected"]() == "http://chosen-library#token=library-token"
    monkeypatch.setattr(base, "STANDALONE", False)  # Linux processes on the same server
    assert rt.processing_connection().token == "library-token"
    assert controller.wakes == 0
    monkeypatch.setenv("MNEMA_RUNNER", "")
    assert controller.callbacks["selected"]() is None
    rt.close()
    assert controller.closes == 1


def test_busy_callback_and_completion_wakeup_do_not_follow_timer(app):
    rt, platform, root, updates = app
    folder, id = record(root)
    rt.start_sync()
    rt._active = folder
    assert rt._sync.callbacks["busy"]() == {str(folder), id}
    rt._prepared = SimpleNamespace(base=root / "Renamed")
    assert rt._sync.callbacks["busy"]() == {str(root / "Renamed")}
    rt._prepared = rt._active = None
    rt._update_next = time.time() + 3600
    rt.tick()
    assert rt._sync.wakes == 0
    rt._library_changed()
    assert rt._sync.wakes == 1 and rt.library_rev == 1


def test_bulk_buttons_bind_exact_current_ids_and_errors_use_existing_panel(app):
    rt, platform, root, updates = app
    rt.start_sync()
    ids = [str(uuid4()), str(uuid4())]
    rt._sync.callbacks["on_change"](SyncStatus(bulk_missing=ids, errors=["Нет доступа к узлу"]))
    panel = panel_model(rt.state, rt.prefs, MenuEnv(), [], SettingsModel(tabs=[]))
    assert panel.status.icon == "warning" and rt.library_rev == 0
    choices = panel.notices[0].buttons
    assert [b.title for b in choices] == ["Удалить везде", "Вернуть с сервера"]
    assert "На сервере они сохранены" in panel.notices[0].text
    assert panel.notices[1].text == "Синхронизация: нет доступа к узлу"
    rt.handle(choices[1].key)
    assert rt._sync.choices == [(sorted(ids), True)]
    rt._sync_changed(SyncStatus(bulk_missing=[ids[0], str(uuid4())]))
    rt.handle(choices[0].key)
    assert len(rt._sync.choices) == 1  # stale visible choice cannot target another set


def test_card_actions_follow_finder_move_and_rename_dialog_re_resolves_identity(app):
    rt, platform, root, updates = app
    folder, id = record(root)
    panel = panel_model(rt.state, rt.prefs, MenuEnv(), library(root).cards, SettingsModel(tabs=[]))
    card = panel.cards[0]
    assert card.open_key == f"record:{id}:folder"  # a click opens the recording folder
    renamed = folder.rename(root / "New name")
    rt.handle(card.open_key)
    rt.handle(f"record:{id}:transcript")
    assert platform.opened == [renamed, renamed / "transcript.txt"]
    rt.handle(f"record:{id}:rename")
    section = root / "Work"
    section.mkdir()
    moved = renamed.rename(section / renamed.name)
    # Replacing the old path while the title dialog is open must not change that other record.
    other, other_id = record(root, "New name")
    platform.answer("Final")
    current = next(c.path for c in library(root).cards if c.id == id)
    assert current.parent == section and "Final" in current.name and not moved.exists()
    assert other.exists() and json.loads((other / ".mnema-meta.json").read_text())["id"] == other_id


def test_trash_rejects_busy_or_duplicate_identity_and_uses_native_callback(app):
    rt, platform, root, updates = app
    folder, id = record(root)
    rt.start_sync()
    rt._active = folder
    rt.handle(f"record:{id}:trash")
    assert not platform.trashed
    rt._active = None
    duplicate, _ = record(root, "Copy")
    (duplicate / ".mnema-meta.json").write_text((folder / ".mnema-meta.json").read_text())
    before = rt._sync.wakes
    rt.handle(f"record:{id}:trash")  # the copy became a new record, the action hits the original
    assert platform.trashed == [folder] and rt._sync.wakes == before + 1
    assert json.loads((duplicate / ".mnema-meta.json").read_text())["id"] != id


def test_cleanup_confirms_then_rechecks_current_busy_state_and_keeps_text(
    app, tmp_path, monkeypatch
):
    """Audio of a processed recording goes to the Trash only after sync with the chosen server
    confirmed its texts, and not while recording; the texts stay."""
    import asyncio

    import httpx

    from mnema.server import create_app
    from mnema.sync.client import LibraryClient
    from mnema.sync.engine import SyncEngine

    rt, platform, root, updates = app
    folder, id = record(root, audio=True, processed=True)
    monkeypatch.setenv("MNEMA_RUNNER", "http://node")
    rt.handle("cleanup_audio")  # texts not on the server yet: nothing to delete, and why
    assert platform.answer is None
    assert rt.state.notice == "Звук можно удалить после синхронизации с сервером"
    monkeypatch.setenv("MNEMA_LIBRARY", "true")
    (tmp_path / "node/library").mkdir(parents=True)  # the installer creates it
    server = create_app(tmp_path / "node")
    engine = SyncEngine(
        root,
        checkpoint_path(),
        selected=rt.sync_connection,
        busy=rt._sync_busy,
        trash=platform.trash,
        client_factory=lambda c: LibraryClient(c, transport=httpx.ASGITransport(server)),
    )
    assert not asyncio.run(engine.run_once()).errors
    rt.handle("cleanup_audio")
    rt.state.phase = Phase.RECORDING
    platform.answer(True)
    assert not platform.trashed
    rt.state.phase = Phase.IDLE
    rt.handle("cleanup_audio")
    assert platform.asked == (
        "Удалить звук 1 записи?",
        "Освободится 0,0 МБ. Тексты останутся.",
        "Удалить",
    )
    platform.answer(True)
    assert platform.trashed == [folder / "mic.wav"]
    assert rt.state.notice == "Звук удалён у записей: 1"
    assert (folder / "transcript.txt").read_text() == "Hello"
    assert (folder / ".mnema-meta.json").exists()


@pytest.mark.parametrize(
    ("mac", "place"), [(True, "на этом Mac"), (False, "на этом компьютере")], ids=["mac", "linux"]
)
def test_cleanup_without_server_says_texts_stay_only_here(app, monkeypatch, mac, place):
    """No server connected: audio of a processed recording can be deleted, the question names
    the machine that keeps the texts. A server connected during the question: the deletion
    follows the question's rule."""
    rt, platform, root, _ = app
    monkeypatch.setattr(base, "STANDALONE", mac)
    folder, _ = record(root, audio=True, processed=True)
    rt.handle("cleanup_audio")
    assert platform.asked == (
        "Удалить звук 1 записи?",
        f"Освободится 0,0 МБ. Тексты останутся только {place}.",
        "Удалить",
    )
    monkeypatch.setenv("MNEMA_RUNNER", "http://node")
    platform.answer(True)
    assert platform.trashed == [folder / "mic.wav"]
    assert (folder / "transcript.txt").read_text() == "Hello"


@pytest.mark.parametrize(
    ("name", "title"),
    [
        ("Home", "Disconnect from the server “Home”?"),
        ("Server", "Disconnect from the server?"),
        ("Сервер", "Disconnect from the server?"),
    ],
)
def test_disconnect_question_is_english_in_english(app, monkeypatch, name, title):
    """An English server reports its default name as "Server": the question does not repeat it."""
    set_language("en")
    rt, platform, _root, _updates = app
    monkeypatch.setattr(base, "STANDALONE", True)
    monkeypatch.setenv("MNEMA_RUNNER", "http://srv:8123")
    rt.server_name = name
    rt.handle("server_off")
    assert platform.asked == (
        title,
        "Recordings stay on this Mac and on the server. "
        "To connect again, you need a new invitation from the server",
        "Disconnect",
    )


@pytest.mark.parametrize(
    ("count", "title"),
    [(1, "Delete audio of 1 recording?"), (2, "Delete audio of 2 recordings?")],
)
def test_cleanup_question_is_english_in_english(app, monkeypatch, count, title):
    set_language("en")
    rt, platform, root, _ = app
    monkeypatch.setattr(base, "STANDALONE", True)
    for i in range(count):
        record(root, f"Call {i}", audio=True, processed=True)
    rt.handle("cleanup_audio")
    asked_title, message, ok = platform.asked
    assert (asked_title, ok) == (title, "Delete")
    assert message.startswith("This frees ")
    assert message.endswith(". The texts stay only on this Mac.")
    platform.answer(True)
    assert rt.state.notice == f"Audio deleted from recordings: {count}"


def test_missing_record_id_never_deletes_replacement_at_old_path(app):
    rt, platform, root, updates = app
    folder, id = record(root)
    (folder / ".mnema-meta.json").write_text(json.dumps({"id": str(uuid4())}))
    rt.handle(f"record:{id}:trash")
    assert folder.exists() and not platform.trashed
    assert rt.state.error == "Запись не найдена"


def test_bulk_button_from_previous_server_is_rejected_even_with_same_ids(app, monkeypatch):
    rt, platform, root, updates = app
    rt.start_sync()
    ids = [str(uuid4()), str(uuid4())]
    monkeypatch.setenv("MNEMA_RUNNER", "http://a")
    rt._sync_changed(SyncStatus(connection="http://a", bulk_missing=ids))
    panel = panel_model(rt.state, rt.prefs, MenuEnv(), [], SettingsModel(tabs=[]))
    old_delete = panel.notices[0].buttons[0].key
    monkeypatch.setenv("MNEMA_RUNNER", "http://b")
    rt.handle(old_delete)  # changed selection, still displaying A
    assert not rt._sync.choices
    rt._sync_changed(SyncStatus(connection="http://b", bulk_missing=ids))
    rt.handle(old_delete)  # B now displays exactly the same UUIDs
    assert not rt._sync.choices
    current = panel_model(rt.state, rt.prefs, MenuEnv(), [], SettingsModel(tabs=[]))
    rt.handle(current.notices[0].buttons[0].key)
    assert rt._sync.choices == [(sorted(ids), False)]


def test_explicit_connection_initializes_root_but_never_recreates_known_missing_library(app):
    """A server saved in the "Server" window creates a new library and wakes the sync; a library
    that went away (a checkpoint exists, even unreadable or a link) is not recreated."""
    from mnema.bar.forms import ServerFlow

    rt, platform, root, updates = app
    rt.start_sync()
    assert not root.exists()

    def save() -> None:
        flow = ServerFlow()
        assert flow.submit({"connection": "http://library"}, force=True) is None
        rt._form_saved(flow)

    save()
    assert root.is_dir() and rt._sync.wakes == 1
    root.rmdir()
    checkpoint = checkpoint_path()
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    checkpoint.write_text("even unreadable checkpoint remains evidence")
    save()
    assert not root.exists()
    checkpoint.unlink()
    checkpoint.symlink_to(checkpoint.with_name("missing.json"))
    save()
    assert not root.exists() and checkpoint.is_symlink()


def test_initialize_library_does_not_treat_checkpoint_stat_error_as_absence(app, monkeypatch):
    from mnema.sync.state import initialize_library

    rt, platform, root, updates = app
    checkpoint = root.parent / "blocked/sync.json"
    lstat = Path.lstat

    def unreadable(path, *args, **kwargs):
        if path == checkpoint:
            raise PermissionError("not readable")
        return lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", unreadable)
    with pytest.raises(PermissionError):
        initialize_library(root, checkpoint)
    assert not root.exists()


def test_active_rename_keeps_previous_remote_record_and_checkpoint(app, tmp_path, monkeypatch):
    import asyncio

    import httpx

    from mnema.server import create_app
    from mnema.sync.client import LibraryClient
    from mnema.sync.engine import SyncEngine
    from mnema.sync.state import load

    monkeypatch.setenv("MNEMA_LIBRARY", "true")
    rt, platform, root, updates = app
    folder, _ = record(root, "Old")
    (tmp_path / "node/library").mkdir(parents=True)  # the installer creates it
    server = create_app(tmp_path / "node")
    checkpoint = tmp_path / "sync.json"
    engine = SyncEngine(
        root,
        checkpoint,
        selected=lambda: "http://node",
        busy=rt._sync_busy,
        trash=platform.trash,
        client_factory=lambda c: LibraryClient(c, transport=httpx.ASGITransport(server)),
    )
    assert not asyncio.run(engine.run_once()).errors
    [id] = load(checkpoint).records
    before = checkpoint.read_bytes()
    current = folder.rename(root / "New")
    rt._active = current
    assert id in rt._sync_busy()
    result = asyncio.run(engine.run_once())
    assert not result.errors and not result.changed
    assert checkpoint.read_bytes() == before
    assert server.state.library.manifest().records[0].path == "Old"
    assert load(checkpoint).records[id].path == "Old"


def test_open_text_through_a_symlinked_library_root(app, tmp_path, monkeypatch):
    rt, platform, _, _ = app
    real = tmp_path / "disk" / "mnema"
    link = tmp_path / "mnema"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv("MNEMA_LIVE_DIR", str(link))
    _, id = record(real)
    (real / "Call/summary.txt").write_text("Итог")
    rt.handle(f"record:{id}:transcript")
    rt.handle(f"record:{id}:summary")
    assert platform.opened == [link / "Call/transcript.txt", link / "Call/summary.txt"]


@pytest.mark.parametrize("on_server", [True, False], ids=["missing-on-server", "missing-here"])
def test_bulk_choice_on_the_mac_names_this_mac(on_server):
    """Sync notices on a Mac call the machine "this Mac"."""
    state = runtime.BarState(sync_missing=[str(uuid4())], sync_missing_on_server=on_server)
    prefs = runtime.BarPrefs()
    notice = panel_model(state, prefs, MenuEnv(mac=True), [], SettingsModel(tabs=[])).notices[0]
    if on_server:
        assert notice.text == "На сервере нет записей: 1. На этом Mac они сохранены."
        assert [b.title for b in notice.buttons] == ["Удалить на этом Mac", "Вернуть на сервер"]
    else:
        assert notice.text == "На этом Mac нет записей: 1. На сервере они сохранены."
        assert [b.title for b in notice.buttons] == ["Удалить везде", "Вернуть с сервера"]


def test_bulk_choice_for_records_missing_on_server(app):
    rt, platform, root, updates = app
    rt.start_sync()
    ids = [str(uuid4()), str(uuid4())]
    rt._sync.callbacks["on_change"](SyncStatus(bulk_missing=ids, bulk_on_server=True))
    notice = panel_model(rt.state, rt.prefs, MenuEnv(), [], SettingsModel(tabs=[])).notices[0]
    assert notice.text == "На сервере нет записей: 2. На этом компьютере они сохранены."
    assert [b.title for b in notice.buttons] == ["Удалить на этом компьютере", "Вернуть на сервер"]
    rt.handle(notice.buttons[1].key)
    assert rt._sync.choices == [(sorted(ids), True)]


@pytest.mark.parametrize(
    ("mac", "count", "text"),
    [
        (
            True,
            1,
            "На сервере другая библиотека записей. Отправить в неё 1 запись с этого Mac?",
        ),
        (
            False,
            3,
            "На сервере другая библиотека записей. Отправить в неё 3 записи с этого компьютера?",
        ),
    ],
    ids=["mac", "computer"],
)
def test_another_library_asks_before_sending_and_the_answer_names_it(
    app, monkeypatch, mac, count, text
):
    rt, platform, root, updates = app
    monkeypatch.setenv("MNEMA_RUNNER", "http://srv")
    rt.start_sync()
    rt._sync.callbacks["on_change"](
        SyncStatus(connection="http://srv", other_library=count, foreign="L2")
    )
    panel = panel_model(rt.state, rt.prefs, MenuEnv(mac=mac), [], SettingsModel(tabs=[]))
    [notice] = panel.notices
    assert panel.status.icon == "warning"
    assert notice.text == text
    assert [b.title for b in notice.buttons] == ["Отправить"]  # no "Close": the question stays
    send = notice.buttons[0].key
    rt._sync_changed(SyncStatus(connection="http://srv", other_library=count, foreign="L3"))
    rt.handle(send)  # the server changed its library after the notice was drawn
    assert not rt._sync.choices
    current = panel_model(rt.state, rt.prefs, MenuEnv(mac=mac), [], SettingsModel(tabs=[]))
    rt.handle(current.notices[0].buttons[0].key)
    assert rt._sync.choices == ["L3"]
    rt._sync_changed(SyncStatus(connection="http://srv"))
    assert not panel_model(rt.state, rt.prefs, MenuEnv(), [], SettingsModel(tabs=[])).notices
