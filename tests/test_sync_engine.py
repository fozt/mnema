"""Two file sides through the real ASGI API, without models or an external network."""

import asyncio
import json
import os
import shutil
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from mnema.library.models import FileBody, FileName
from mnema.server import create_app
from mnema.sync.client import LibraryClient
from mnema.sync.engine import SyncEngine
from mnema.sync.state import checkpoint_path, load

LIBRARY = json.loads((Path(__file__).parent / "fixtures/library-v1.json").read_text())


class Transport(httpx.AsyncBaseTransport):
    def __init__(self, app, hook=lambda request, response: None):
        self.inner, self.hook = httpx.ASGITransport(app), hook
        self.requests = []

    async def handle_async_request(self, request):
        self.requests.append((request.method, request.url.path))
        response = await self.inner.handle_async_request(request)
        self.hook(request, response)
        return response


def recording(root, name, *, id=None, text="оригинал", mtime=100):
    folder = root / name
    folder.mkdir(parents=True)
    data = {
        "id": id or str(uuid4()),
        "started": "2020-01-02T03:04:05+03:00",
        "device": "Mac",
        "runs": [{"remarks": ["human"]}],
        "unknown": {"value": 1.5},
    }
    (folder / FileName.META).write_text(json.dumps(data, ensure_ascii=False))
    edit(folder / FileName.TRANSCRIPT, text, mtime)
    edit(folder / FileName.SUMMARY, "пересказ", mtime)
    return folder


def edit(path, text, mtime):
    path.write_text(text)
    os.utime(path, ns=(mtime, mtime))


def remote_record(app, name, *, id=None, text="сервер", mtime=200):
    id = id or str(uuid4())
    return app.state.library.create(
        id,
        name,
        {
            FileName.META: FileBody(
                text=json.dumps(
                    {"id": id, "started": "2020-01-02T03:04:05+03:00", "device": "Other"}
                ),
                mtime_ns=mtime,
            ),
            FileName.TRANSCRIPT: FileBody(text=text, mtime_ns=mtime),
            FileName.SUMMARY: FileBody(text="remote summary", mtime_ns=mtime),
        },
    )


@pytest.fixture
def pair(tmp_path, monkeypatch):
    monkeypatch.setenv("MNEMA_LIBRARY", "true")
    (tmp_path / "server/library").mkdir(parents=True)  # the installer creates the folder
    app = create_app(tmp_path / "server")
    root = tmp_path / "client"
    root.mkdir()
    checkpoint = checkpoint_path()  # where the feed reads it
    selected, busy, trashed = ["http://node#token=private"], set(), []
    transport = Transport(app)

    def trash(path):
        dest = tmp_path / "system-trash" / path.name
        dest.parent.mkdir(exist_ok=True)
        path.rename(dest)
        trashed.append(dest)

    def engine():
        return SyncEngine(
            root,
            checkpoint,
            selected=lambda: selected[0],
            busy=lambda: busy,
            trash=trash,
            client_factory=lambda connection: LibraryClient(connection, transport=transport),
        )

    return SimpleNamespace(
        app=app,
        root=root,
        selected=selected,
        busy=busy,
        trashed=trashed,
        transport=transport,
        engine=engine,
        checkpoint=checkpoint,
    )


def run(pair):
    return asyncio.run(pair.engine().run_once())


def synced(pair, name="record", **kwargs):
    folder = recording(pair.root, name, **kwargs)
    result = run(pair)
    assert not result.errors, result.errors
    id = json.loads((folder / FileName.META).read_text())["id"]
    return folder, id


def server_folder(pair, id):
    record = next(r for r in pair.app.state.library.manifest().records if r.id == id)
    return pair.app.state.library.root / record.path


def test_first_publication_keeps_identity_and_never_uploads_audio_or_notes(pair):
    folder = recording(pair.root, "record")
    (folder / "mic.wav").write_bytes(b"local audio")
    (folder / ".mnema-notes.txt").write_text("notes")
    (folder / ".mnema-processing").write_text("local marker")
    before = (folder / FileName.META).read_bytes()
    assert not run(pair).errors
    assert (folder / FileName.META).read_bytes() == before
    [record] = pair.app.state.library.manifest().records
    assert record.id == json.loads(before)["id"]
    assert set(p.name for p in (pair.app.state.library.root / "record").iterdir()) == set(FileName)
    assert "private" not in pair.checkpoint.read_text()
    assert not run(pair).changed


def test_remote_only_download_preserves_bytes_mtime_and_empty_sections(pair):
    record = remote_record(pair.app, "Работа/remote")
    pair.app.state.library.section("Пустой")
    assert not run(pair).errors
    folder = pair.root / record.path
    assert (folder / FileName.TRANSCRIPT).read_text() == "сервер"
    assert (folder / FileName.TRANSCRIPT).stat().st_mtime_ns == 200
    assert (pair.root / "Пустой").is_dir()
    assert not list(folder.glob("*.wav"))
    assert not run(pair).changed


def test_independent_files_and_same_file_latest_original_mtime(pair):
    folder, id = synced(pair)
    remote = server_folder(pair, id)
    edit(folder / FileName.TRANSCRIPT, "local text", 300)
    edit(remote / FileName.SUMMARY, "remote summary", 400)
    assert not run(pair).errors
    assert (remote / FileName.TRANSCRIPT).read_text() == "local text"
    assert (folder / FileName.SUMMARY).read_text() == "remote summary"
    edit(folder / FileName.TRANSCRIPT, "older local", 500)
    edit(remote / FileName.TRANSCRIPT, "newer remote", 600)
    assert not run(pair).errors
    assert (folder / FileName.TRANSCRIPT).read_text() == "newer remote"
    assert (folder / FileName.TRANSCRIPT).stat().st_mtime_ns == 600


def test_equal_time_conflict_does_not_overwrite_either_side(pair):
    folder, id = synced(pair)
    remote = server_folder(pair, id)
    edit(folder / FileName.SUMMARY, "local", 300)
    edit(remote / FileName.SUMMARY, "remote", 300)
    assert run(pair).errors
    assert (folder / FileName.SUMMARY).read_text() == "local"
    assert (remote / FileName.SUMMARY).read_text() == "remote"


def test_concurrent_renames_preserve_both_paths(pair):
    folder, id = synced(pair)
    folder.rename(pair.root / "Local")
    server_folder(pair, id).rename(pair.app.state.library.root / "Remote")
    assert run(pair).errors
    assert (pair.root / "Local").is_dir() and (pair.app.state.library.root / "Remote").is_dir()


def test_single_delete_and_restored_folder_is_published_again(pair):
    folder, id = synced(pair)
    # Emulates the Finder Trash, outside Mnema and without the network.
    trash = pair.root.parent / "finder-trash"
    folder.rename(trash)
    assert not run(pair).errors
    assert pair.app.state.library.manifest().records == []
    assert id not in load(pair.checkpoint).records
    trash.rename(folder)
    # The id is forgotten by both the server and the checkpoint: this is a new recording.
    assert not run(pair).errors
    assert [r.id for r in pair.app.state.library.manifest().records] == [id]
    assert (server_folder(pair, id) / FileName.TRANSCRIPT).read_text() == "оригинал"


def test_bulk_delete_holds_then_explicit_choice_deletes_all(pair):
    a, aid = synced(pair, "a")
    b, bid = synced(pair, "b")
    shutil.rmtree(a)
    shutil.rmtree(b)
    result = run(pair)
    assert set(result.bulk_missing) == {aid, bid}
    assert len(pair.app.state.library.manifest().records) == 2
    result = asyncio.run(pair.engine().resolve_bulk([aid, bid], restore=False))
    assert not result.errors
    assert pair.app.state.library.manifest().records == []
    assert load(pair.checkpoint).records == {}


@pytest.mark.parametrize("stop", [httpx.ReadError, asyncio.CancelledError])
def test_bulk_restore_survives_interruption_after_first_record(pair, stop):
    """Interrupted on the second recording: the next sync does not take a single missing one for a
    deletion on the server. A cancel stops the cycle without saving, so only saving the whole set
    before the first transfer helps."""
    a, aid = synced(pair, "a")
    b, bid = synced(pair, "b")
    shutil.rmtree(pair.root)
    assert len(run(pair).bulk_missing) == 2
    downloads = set()

    def stop_second(request, response):
        if request.method == "GET" and request.url.path.endswith("/.mnema-meta.json"):
            downloads.add(request.url.path.split("/")[4])
            if len(downloads) == 2:
                raise stop("interrupted")

    pair.transport.hook = stop_second
    if stop is asyncio.CancelledError:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(pair.engine().resolve_bulk([aid, bid], restore=True))
    else:
        assert asyncio.run(pair.engine().resolve_bulk([aid, bid], restore=True)).errors
    assert len(pair.app.state.library.manifest().records) == 2
    pair.transport.hook = lambda request, response: None
    assert not run(pair).errors
    assert a.is_dir() and b.is_dir()
    assert len(pair.app.state.library.manifest().records) == 2


def test_restore_checkpoint_failure_precedes_any_download(pair, monkeypatch):
    a, aid = synced(pair, "a")
    b, bid = synced(pair, "b")
    shutil.rmtree(a)
    shutil.rmtree(b)
    pair.transport.requests.clear()
    real_replace = os.replace

    def full(source, destination):
        if Path(destination) == pair.checkpoint:
            raise OSError("disk full")
        return real_replace(source, destination)

    monkeypatch.setattr(os, "replace", full)
    result = asyncio.run(pair.engine().resolve_bulk([aid, bid], restore=True))
    assert result.errors
    assert not any("/files/" in path for _, path in pair.transport.requests)
    assert set(load(pair.checkpoint).records) == {aid, bid}


def test_remote_delete_uses_trash_with_audio_and_busy_defers(pair):
    folder, id = synced(pair)
    (folder / "mic.wav").write_bytes(b"audio")
    record = pair.app.state.library.manifest().records[0]
    pair.app.state.library.remove(id, record.tag)
    pair.busy.add(id)
    assert not run(pair).errors and folder.exists()
    assert id in load(pair.checkpoint).records
    pair.busy.clear()
    assert not run(pair).errors and not folder.exists()
    assert (pair.trashed[0] / "mic.wav").read_bytes() == b"audio"
    assert id not in load(pair.checkpoint).records


def test_server_switch_during_manifest_never_applies_previous_response(pair):
    remote_record(pair.app, "remote")
    pair.transport.hook = lambda request, response: pair.selected.__setitem__(0, "http://other")
    result = run(pair)
    assert result.errors and list(pair.root.iterdir()) == []
    assert not pair.checkpoint.exists()


def test_lost_checkpoint_keeps_local_audio_and_uses_full_sync(pair):
    folder, id = synced(pair)
    (folder / "mic.wav").write_bytes(b"audio")
    pair.checkpoint.unlink()
    assert not run(pair).errors
    assert json.loads((folder / FileName.META).read_text())["id"] == id
    assert (folder / "mic.wav").read_bytes() == b"audio"
    assert not pair.trashed


def test_initial_missing_root_does_not_create_or_delete_anything(pair):
    pair.root.rmdir()
    remote_record(pair.app, "remote")
    assert run(pair).errors
    assert not pair.root.exists() and not pair.checkpoint.exists()
    assert len(pair.app.state.library.manifest().records) == 1


def test_copy_keeps_tracked_id_and_collision_never_overwrites(pair):
    original, id = synced(pair, "z-original")
    before = (original / FileName.META).read_bytes()
    shutil.copytree(original, pair.root / "a-copy")
    assert not run(pair).errors
    copy_id = json.loads((pair.root / "a-copy" / FileName.META).read_text())["id"]
    assert copy_id != id and (original / FileName.META).read_bytes() == before
    assert {(r.id, r.path) for r in pair.app.state.library.manifest().records} == {
        (id, "z-original"),
        (copy_id, "a-copy"),
    }
    remote_record(pair.app, "occupied")
    recording(pair.root, "occupied", text="local collision")
    assert not run(pair).errors
    values = {
        (p.name, (p / FileName.TRANSCRIPT).read_text())
        for p in pair.root.iterdir()
        if p.name.startswith("occupied")
    }
    assert {text for _, text in values} == {"сервер", "local collision"}


def test_local_edit_during_download_is_preserved_then_becomes_latest(pair):
    folder, id = synced(pair)
    remote = server_folder(pair, id)
    edit(remote / FileName.TRANSCRIPT, "remote change", 200)
    fired = False

    def edit_while_downloading(request, response):
        nonlocal fired
        if request.method == "GET" and request.url.path.endswith("/transcript.txt") and not fired:
            fired = True
            edit(folder / FileName.TRANSCRIPT, "local typing", 300)

    pair.transport.hook = edit_while_downloading
    assert run(pair).errors
    assert (folder / FileName.TRANSCRIPT).read_text() == "local typing"
    pair.transport.hook = lambda request, response: None
    assert not run(pair).errors
    assert (remote / FileName.TRANSCRIPT).read_text() == "local typing"


def test_busy_started_during_download_defers_apply(pair):
    folder, id = synced(pair)
    edit(server_folder(pair, id) / FileName.TRANSCRIPT, "remote change", 200)

    def start_processing(request, response):
        if request.method == "GET" and request.url.path.endswith("/transcript.txt"):
            pair.busy.add(id)

    pair.transport.hook = start_processing
    assert run(pair).errors
    assert (folder / FileName.TRANSCRIPT).read_text() == "оригинал"
    assert id in load(pair.checkpoint).records
    pair.transport.hook = lambda request, response: None
    pair.busy.clear()
    assert not run(pair).errors
    assert (folder / FileName.TRANSCRIPT).read_text() == "remote change"


def test_remote_edit_after_manifest_causes_409_without_stale_overwrite(pair):
    folder, id = synced(pair)
    remote = server_folder(pair, id)
    edit(folder / FileName.TRANSCRIPT, "local version", 200)
    fired = False

    def edit_after_scan(request, response):
        nonlocal fired
        if request.method == "GET" and request.url.path == "/api/library" and not fired:
            fired = True
            edit(remote / FileName.TRANSCRIPT, "new remote", 300)

    pair.transport.hook = edit_after_scan
    assert run(pair).errors
    assert (remote / FileName.TRANSCRIPT).read_text() == "new remote"
    pair.transport.hook = lambda request, response: None
    assert not run(pair).errors
    assert (folder / FileName.TRANSCRIPT).read_text() == "new remote"


def test_lost_put_response_is_observed_without_replaying_stale_operation(pair):
    folder, id = synced(pair)
    edit(folder / FileName.TRANSCRIPT, "local change", 200)
    pair.transport.requests.clear()

    def lose_response(request, response):
        if request.method == "PUT" and request.url.path.endswith("/transcript.txt"):
            raise httpx.ReadError("lost acknowledgement")

    pair.transport.hook = lose_response
    assert run(pair).errors
    pair.transport.hook = lambda request, response: None
    assert not run(pair).errors
    writes = [
        p
        for method, p in pair.transport.requests
        if method == "PUT" and p.endswith("/transcript.txt")
    ]
    assert len(writes) == 1
    assert (server_folder(pair, id) / FileName.TRANSCRIPT).read_text() == "local change"


def test_offline_local_delete_and_edit_stay_in_files_until_reconnection(pair):
    a, aid = synced(pair, "a")
    b, bid = synced(pair, "b")
    shutil.rmtree(a)
    edit(b / FileName.SUMMARY, "offline edit", 200)
    before = pair.checkpoint.read_bytes()

    def offline(request, response):
        raise httpx.ConnectError("unreachable")

    pair.transport.hook = offline
    assert run(pair).errors
    assert pair.checkpoint.read_bytes() == before
    assert not a.exists() and (b / FileName.SUMMARY).read_text() == "offline edit"
    pair.transport.hook = lambda request, response: None
    assert not run(pair).errors
    assert {r.id for r in pair.app.state.library.manifest().records} == {bid}
    assert (server_folder(pair, bid) / FileName.SUMMARY).read_text() == "offline edit"


def test_new_server_does_not_inherit_old_deletions(pair, tmp_path):
    folder, id = synced(pair)
    shutil.rmtree(folder)
    (tmp_path / "other-server/library").mkdir(parents=True)
    other = create_app(tmp_path / "other-server")
    remote_record(other, "from other", id=id, text="new library")
    pair.transport.inner = httpx.ASGITransport(other)
    pair.selected[0] = "http://other#token=different"
    assert not run(pair).errors
    assert (pair.root / "from other" / FileName.TRANSCRIPT).read_text() == "new library"
    assert len(other.state.library.manifest().records) == 1
    assert load(pair.checkpoint).server == "http://other"


def test_server_switch_during_file_download_does_not_apply_response(pair):
    folder, id = synced(pair)
    edit(server_folder(pair, id) / FileName.TRANSCRIPT, "from previous server", 200)
    before = load(pair.checkpoint)

    def switch(request, response):
        if request.method == "GET" and request.url.path.endswith("/transcript.txt"):
            pair.selected[0] = "http://other"

    pair.transport.hook = switch
    assert run(pair).errors
    assert (folder / FileName.TRANSCRIPT).read_text() == "оригинал"
    after = load(pair.checkpoint)
    assert after.server == before.server
    assert after.records[id].files == before.records[id].files
    assert after.records[id].path == before.records[id].path


def test_removed_known_root_with_one_record_deletes_without_recreating_it(pair):
    _, id = synced(pair)
    shutil.rmtree(pair.root)
    assert not run(pair).errors
    assert pair.app.state.library.manifest().records == []
    assert not pair.root.exists()


def test_empty_section_rename_and_record_section_move(pair):
    (pair.root / "Empty").mkdir()
    assert run(pair).changed
    (pair.root / "Empty").rename(pair.root / "New empty")
    assert not run(pair).errors
    assert pair.app.state.library.manifest().sections == ["New empty"]
    folder, id = synced(pair)
    folder.rename(pair.root / "New empty" / "record")
    assert not run(pair).errors
    assert (
        server_folder(pair, id).relative_to(pair.app.state.library.root).as_posix()
        == "New empty/record"
    )


def _folders(root):
    return sorted(p.name for p in root.iterdir() if not p.name.startswith("."))


def test_server_move_into_section_and_case_only_section_rename_reach_this_device(pair):
    """Another device moved a recording into a section, then changed the section's case
    ("projects" → "Projects"): on case-insensitive storage (APFS) the folder changes case too, the
    server is not rolled back."""
    name = "2026-08-12_Дейли таблицы и графики"
    _, id = synced(pair, name)
    library = pair.app.state.library
    library.move(id, f"projects/{name}", name)
    assert not run(pair).errors
    assert _folders(pair.root) == ["projects"]

    (library.root / "projects").rename(library.root / "Projects")
    assert not run(pair).errors
    assert _folders(pair.root) == ["Projects"]
    folder = pair.root / "Projects" / name
    assert (folder / FileName.TRANSCRIPT).read_text() == "оригинал"
    assert json.loads((folder / FileName.META).read_text())["id"] == id
    pair.transport.requests.clear()
    assert not run(pair).changed  # the old case does not go back to the server
    assert all(method == "GET" for method, _ in pair.transport.requests)
    assert server_folder(pair, id).relative_to(library.root).as_posix() == f"Projects/{name}"
    assert library.manifest().sections == ["Projects"]
    assert load(pair.checkpoint).sections == ["Projects"]
    assert not pair.trashed


def test_case_only_record_rename_on_server_renames_the_folder_here(pair):
    _, id = synced(pair, "Работа/дейли")
    section = pair.app.state.library.root / "Работа"
    (section / "дейли").rename(section / "Дейли")  # a Linux server: case matters
    assert not run(pair).errors
    assert _folders(pair.root / "Работа") == ["Дейли"]
    pair.transport.requests.clear()
    assert not run(pair).changed
    assert all(method == "GET" for method, _ in pair.transport.requests)
    assert server_folder(pair, id).name == "Дейли"


def test_empty_section_renamed_on_server_only_by_case_stays_everywhere(pair):
    (pair.root / "projects").mkdir()
    assert not run(pair).errors
    library = pair.app.state.library
    (library.root / "projects").rename(library.root / "Projects")
    assert not run(pair).errors
    assert _folders(pair.root) == ["Projects"]
    assert not run(pair).changed
    assert library.manifest().sections == ["Projects"]
    assert load(pair.checkpoint).sections == ["Projects"]


def test_duplicate_id_without_tracked_path_keeps_earlier_created_copy(pair):
    folder, id = synced(pair, "original")
    shutil.copytree(folder, pair.root / "b")
    time.sleep(0.02)  # the second copy's ctime is surely later
    shutil.copytree(folder, pair.root / "a")
    shutil.rmtree(folder)
    assert not run(pair).errors
    assert json.loads((pair.root / "b" / FileName.META).read_text())["id"] == id
    assert json.loads((pair.root / "a" / FileName.META).read_text())["id"] != id
    assert server_folder(pair, id).name == "b"


def test_trash_failure_retains_checkpoint_and_files(pair):
    folder, id = synced(pair)
    record = pair.app.state.library.manifest().records[0]
    pair.app.state.library.remove(id, record.tag)
    engine = pair.engine()

    def unavailable(path):
        raise OSError("native trash unavailable")

    engine.trash = unavailable
    assert asyncio.run(engine.run_once()).errors
    assert folder.exists() and id in load(pair.checkpoint).records


def test_lost_create_response_keeps_one_uuid_and_completes_from_manifest(pair):
    folder = recording(pair.root, "record")

    def lost(request, response):
        if request.method == "POST":
            raise httpx.ReadError("lost create reply")

    pair.transport.hook = lost
    assert run(pair).errors
    published_id = json.loads((folder / FileName.META).read_text())["id"]
    pair.transport.hook = lambda request, response: None
    assert not run(pair).errors
    assert [r.id for r in pair.app.state.library.manifest().records] == [published_id]
    assert json.loads((folder / FileName.META).read_text())["id"] == published_id
    assert len([method for method, _ in pair.transport.requests if method == "POST"]) == 1
    assert not run(pair).changed


def test_lost_record_delete_response_forgets_only_after_new_complete_manifest(pair):
    folder, id = synced(pair)
    shutil.rmtree(folder)

    def lost(request, response):
        if request.method == "DELETE":
            raise httpx.ReadError("lost delete reply")

    pair.transport.hook = lost
    assert run(pair).errors
    assert id in load(pair.checkpoint).records
    assert pair.app.state.library.manifest().records == []
    pair.transport.hook = lambda request, response: None
    assert not run(pair).errors
    assert id not in load(pair.checkpoint).records
    assert not folder.exists()
    assert len([method for method, _ in pair.transport.requests if method == "DELETE"]) == 1


def test_lost_rename_response_reconciles_current_paths_without_replaying(pair):
    folder, id = synced(pair)
    folder.rename(pair.root / "Finder name")

    def lost(request, response):
        if request.method == "PATCH":
            raise httpx.ReadError("lost rename reply")

    pair.transport.hook = lost
    assert run(pair).errors
    assert server_folder(pair, id).name == "Finder name"
    pair.transport.hook = lambda request, response: None
    assert not run(pair).errors
    assert load(pair.checkpoint).records[id].path == "Finder name"
    assert len([method for method, _ in pair.transport.requests if method == "PATCH"]) == 1
    assert not run(pair).changed


def test_section_delete_race_preserves_new_server_record(pair):
    (pair.root / "Section").mkdir()
    assert not run(pair).errors
    (pair.root / "Section").rmdir()
    manifests = []

    def add_after_full_scan(request, response):
        if request.method == "GET" and request.url.path == "/api/library":
            manifests.append(1)
            if len(manifests) == 2:
                remote_record(pair.app, "Section/just arrived")

    pair.transport.hook = add_after_full_scan
    assert run(pair).errors
    assert (pair.app.state.library.root / "Section/just arrived").is_dir()
    pair.transport.hook = lambda request, response: None
    assert not run(pair).errors
    assert (pair.root / "Section/just arrived").is_dir()


def test_lost_file_delete_and_empty_text_do_not_remove_record_or_siblings(pair):
    folder, id = synced(pair)
    (folder / FileName.SUMMARY).unlink()

    def lost(request, response):
        if request.method == "DELETE" and request.url.path.endswith("/summary.md"):
            raise httpx.ReadError("lost file deletion")

    pair.transport.hook = lost
    assert run(pair).errors
    pair.transport.hook = lambda request, response: None
    assert not run(pair).errors
    remote = server_folder(pair, id)
    assert not (remote / FileName.SUMMARY).exists()
    edit(remote / FileName.TRANSCRIPT, "", 300)
    assert not run(pair).errors
    assert (folder / FileName.TRANSCRIPT).read_bytes() == b""
    assert (folder / FileName.META).exists()


def test_atomic_download_failure_keeps_old_text_and_retries_from_checkpoint(pair, monkeypatch):
    folder, id = synced(pair)
    edit(server_folder(pair, id) / FileName.TRANSCRIPT, "new", 300)
    replace = os.replace

    def fail(source, destination):
        if Path(destination) == folder / FileName.TRANSCRIPT:
            raise OSError("disk failure")
        return replace(source, destination)

    with monkeypatch.context() as context:
        context.setattr(os, "replace", fail)
        assert run(pair).errors
    assert (folder / FileName.TRANSCRIPT).read_text() == "оригинал"
    assert not list(folder.glob(".mnema-sync-*"))
    assert not run(pair).errors
    assert (folder / FileName.TRANSCRIPT).read_text() == "new"


def test_two_clients_keep_shared_wire_bytes_and_checkpoint_without_noop_writes(pair, tmp_path):
    fixture = json.loads((Path(__file__).parent / "fixtures/library-v1.json").read_text())
    creation = fixture["create"]
    pair.app.state.library.create(
        creation["id"],
        creation["path"],
        {
            FileName(name): FileBody.model_validate(value)
            for name, value in creation["files"].items()
        },
    )
    other_root = tmp_path / "second-client"
    other_root.mkdir()
    other_checkpoint = tmp_path / "second-sync.json"
    other = SyncEngine(
        other_root,
        other_checkpoint,
        selected=lambda: pair.selected[0],
        busy=lambda: set(),
        trash=lambda path: pytest.fail("unexpected deletion"),
        client_factory=lambda connection: LibraryClient(connection, transport=pair.transport),
    )
    assert not run(pair).errors
    assert not asyncio.run(other.run_once()).errors
    expected = json.loads((Path(__file__).parent / "fixtures/sync-checkpoint-v1.json").read_text())
    assert json.loads(pair.checkpoint.read_text()) == expected
    assert json.loads(other_checkpoint.read_text()) == expected
    pair.transport.requests.clear()
    for _ in range(2):
        assert not run(pair).changed
        assert not asyncio.run(other.run_once()).changed
    assert all(method == "GET" for method, _ in pair.transport.requests)
    for name, body in creation["files"].items():
        for root in (pair.root, other_root):
            assert (root / creation["path"] / name).read_text() == body["text"]


@pytest.mark.parametrize(
    "payload",
    [{}, {"sections": []}, {"records": []}]
    + [
        {"records": [], "sections": [name]}
        for name in ("../bad", "a/b", "", ".", "..", "\0bad", ".obsidian")
    ],
)
def test_invalid_http_manifest_never_means_remote_deletion(pair, payload):
    folder, id = synced(pair)
    before = pair.checkpoint.read_bytes()
    files = {p.name: p.read_bytes() for p in folder.iterdir()}
    pair.transport.requests.clear()
    pair.transport.inner = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    result = run(pair)
    assert result.errors and not result.changed
    assert pair.checkpoint.read_bytes() == before
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == files
    assert not pair.trashed
    assert all(method == "GET" for method, _ in pair.transport.requests)
    assert id in load(pair.checkpoint).records


@pytest.mark.parametrize(
    "path", ["../outside", "Работа/Звонок/Лишнее", ".obsidian", "", "Работа/", "escape"]
)
def test_bad_record_path_is_refused_alike_by_server_and_sync(pair, tmp_path, path):
    """A path outside the root, hidden, deeper than a section or through a link pointing outside:
    the server does not create such a recording (409), and sync does not download it, with the
    same error text; nothing is written outside."""
    outside = tmp_path / "outside"
    outside.mkdir()
    if path == "escape":
        for root in (pair.app.state.library.root, pair.root):
            (root / path).symlink_to(outside, target_is_directory=True)
    refused = TestClient(pair.app).post(
        "/api/library/records", json={**LIBRARY["create"], "path": path}
    )
    assert refused.status_code == 409
    record = {**LIBRARY["manifest"]["records"][0], "path": path}
    pair.transport.inner = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"records": [record], "sections": []})
    )
    result = run(pair)
    assert refused.json()["detail"] in result.errors and not result.changed
    assert list(outside.iterdir()) == [] and not pair.trashed


@pytest.mark.parametrize("invalid", [{}, {"records": [], "sections": ["../bad"]}])
def test_invalid_second_manifest_does_not_remove_empty_sections(pair, invalid):
    folder, _ = synced(pair)
    section = pair.root / "Empty section"
    section.mkdir()
    assert not run(pair).errors
    before = pair.checkpoint.read_bytes()
    requests = []

    def response(request):
        requests.append(request.method)
        payload = (
            pair.app.state.library.manifest().model_dump(mode="json")
            if len(requests) == 1
            else invalid
        )
        return httpx.Response(200, json=payload)

    pair.transport.inner = httpx.MockTransport(response)
    assert run(pair).errors
    assert section.is_dir() and folder.is_dir() and not pair.trashed
    assert requests == ["GET", "GET"]
    assert pair.checkpoint.read_bytes() == before


def test_dangling_root_symlink_is_not_deletion_of_known_record(pair):
    from mnema.sync.scan import scan

    _, id = synced(pair)
    before = pair.checkpoint.read_bytes()
    moved = pair.root.with_name("original-client")
    pair.root.rename(moved)
    pair.root.symlink_to(pair.root.with_name("missing-target"), target_is_directory=True)
    found = scan(pair.root, set())
    assert not found.complete and not found.missing_root and found.blocked_paths == ["."]
    pair.transport.requests.clear()
    result = run(pair)
    assert result.errors and not result.changed
    assert pair.checkpoint.read_bytes() == before
    assert [r.id for r in pair.app.state.library.manifest().records] == [id]
    assert pair.root.is_symlink() and (moved / "record/transcript.txt").exists()
    assert all(method == "GET" for method, _ in pair.transport.requests)


def test_remote_section_symlink_outside_root_prevents_record_deletion(pair, tmp_path):
    folder, _ = synced(pair)
    before = pair.checkpoint.read_bytes()
    outside = tmp_path / "outside"
    outside.mkdir()
    (pair.root / "Alias").symlink_to(outside, target_is_directory=True)
    pair.transport.requests.clear()
    pair.transport.inner = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"records": [], "sections": ["Alias"]})
    )
    result = run(pair)
    assert result.errors and not result.changed
    assert pair.checkpoint.read_bytes() == before
    assert folder.is_dir() and not pair.trashed
    assert all(method == "GET" for method, _ in pair.transport.requests)


@pytest.mark.parametrize("busy_after_scan", [True, False])
def test_fresh_delete_scan_respects_new_busy_or_blocked_path(pair, monkeypatch, busy_after_scan):
    from mnema.sync import engine as module

    folder = recording(pair.root, "Old")
    run(pair)
    id = json.loads((folder / FileName.META).read_text())["id"]
    parked = folder.rename(pair.root.parent / "outside")
    previous = pair.checkpoint.read_bytes()
    actual_scan = module.scan
    calls = 0

    def scan_then_start(root, busy, **options):
        nonlocal calls
        calls += 1
        if calls == 2:
            if busy_after_scan:
                parked.rename(pair.root / "New")
                pair.busy.update({id, str(pair.root / "New")})
                busy = pair.busy
            else:
                result = actual_scan(root, busy, **options)
                result.blocked_paths.append("Old")
                return result
        return actual_scan(root, busy, **options)

    monkeypatch.setattr(module, "scan", scan_then_start)
    result = run(pair)
    assert calls >= 2 and result.errors and not result.changed
    assert id in load(pair.checkpoint).records
    assert pair.checkpoint.read_bytes() == previous
    assert len(pair.app.state.library.manifest().records) == 1


def test_record_files_in_synced_empty_section_keep_it_a_section(pair):
    from mnema.bar.recordings import library

    section = pair.root / "Работа"
    section.mkdir()
    assert not run(pair).errors and load(pair.checkpoint).sections == ["Работа"]
    (section / "transcript.txt").write_text("случайный файл")
    (section / "mic.wav").write_bytes(b"\0" * 32044)
    view = library(pair.root)
    assert view.sections == ["Работа"] and view.cards == []
    assert not run(pair).errors and not (section / FileName.META).exists()
    manifest = pair.app.state.library.manifest()
    assert manifest.records == [] and manifest.sections == ["Работа"]
    (section / "transcript.txt").unlink()
    folder, id = synced(pair, "Встреча")
    folder.rename(section / "Встреча")
    assert [(c.section, c.id) for c in library(pair.root).cards] == [("Работа", id)]
    assert not run(pair).errors
    manifest = pair.app.state.library.manifest()
    assert [(r.id, r.path) for r in manifest.records] == [(id, "Работа/Встреча")]
    assert not (section / FileName.META).exists() and not pair.trashed


def test_known_section_stays_a_section_after_server_switch(pair, tmp_path):
    section = pair.root / "Работа"
    section.mkdir()
    assert not run(pair).errors
    (section / FileName.SUMMARY).write_text("случайный файл")
    (tmp_path / "second/library").mkdir(parents=True)
    second = create_app(tmp_path / "second")
    switch_server(pair, second, "http://second")
    assert not run(pair).errors and not (section / FileName.META).exists()
    manifest = second.state.library.manifest()
    assert manifest.records == [] and manifest.sections == ["Работа"]


def test_record_inside_record_names_the_folder(pair):
    outer, _ = synced(pair, "Звонок")
    inner, _ = synced(pair, "Внутри")
    inner.rename(outer / "Внутри")
    result = run(pair)
    assert "Запись «Звонок/Внутри» лежит внутри другой записи" in result.errors
    assert len(pair.app.state.library.manifest().records) == 2 and not pair.trashed


def test_missing_server_library_after_restart_is_unavailable_not_empty(pair):
    folder, id = synced(pair)
    before = pair.checkpoint.read_bytes()
    library = pair.app.state.library.root
    shutil.rmtree(library)  # the disk or volume is not mounted when the service starts
    pair.transport.inner = httpx.ASGITransport(create_app(library.parent))
    result = run(pair)
    assert result.errors and not result.changed and not pair.trashed
    assert folder.is_dir() and pair.checkpoint.read_bytes() == before
    assert not library.exists()


def switch_server(pair, app, url):
    pair.transport.inner = httpx.ASGITransport(app)
    pair.selected[0] = url


def test_server_switch_and_back_keeps_ids_without_duplicates(pair, tmp_path):
    folders = [synced(pair, f"rec {i}")[0] for i in range(3)]
    ids = {json.loads((f / FileName.META).read_text())["id"] for f in folders}
    first = pair.app
    (tmp_path / "second/library").mkdir(parents=True)
    second = create_app(tmp_path / "second")
    switch_server(pair, second, "http://second")
    assert not run(pair).errors
    switch_server(pair, first, "http://node#token=private")
    assert not run(pair).errors and not run(pair).changed
    for app in (first, second):
        assert {r.id for r in app.state.library.manifest().records} == ids
    assert sorted(p.name for p in pair.root.iterdir()) == ["rec 0", "rec 1", "rec 2"]


def test_two_devices_moving_to_new_server_keep_one_copy(pair, tmp_path):
    folder, id = synced(pair, "call")
    other_root, other_checkpoint = tmp_path / "phone", tmp_path / "phone-sync.json"
    other_root.mkdir()

    def other():
        return SyncEngine(
            other_root,
            other_checkpoint,
            selected=lambda: pair.selected[0],
            busy=lambda: set(),
            trash=lambda path: pytest.fail("unexpected deletion"),
            client_factory=lambda connection: LibraryClient(connection, transport=pair.transport),
        )

    assert not asyncio.run(other().run_once()).errors
    (tmp_path / "new/library").mkdir(parents=True)
    new = create_app(tmp_path / "new")
    switch_server(pair, new, "http://new")
    assert not run(pair).errors
    assert not asyncio.run(other().run_once()).errors
    assert [r.id for r in new.state.library.manifest().records] == [id]
    assert [p.name for p in pair.root.iterdir()] == ["call"]
    assert [p.name for p in other_root.iterdir()] == ["call"]


def test_idle_sync_does_not_rewrite_checkpoint(pair):
    for i in range(3):
        synced(pair, f"rec {i}")
    (pair.root / "Пустой").mkdir()
    assert not run(pair).errors
    before = pair.checkpoint.stat()
    for _ in range(2):
        assert not run(pair).changed
    assert pair.checkpoint.stat().st_mtime_ns == before.st_mtime_ns
    edit(pair.root / "rec 1" / FileName.SUMMARY, "новый пересказ", 500)
    assert run(pair).changed
    assert pair.checkpoint.stat().st_mtime_ns != before.st_mtime_ns


@pytest.mark.parametrize("ancestor", [False, True])
def test_library_root_through_a_link_syncs_like_the_real_folder(pair, tmp_path, ancestor):
    disk = tmp_path / "disk"
    disk.mkdir()
    pair.root.rename(disk / "client")
    if ancestor:  # a link above the root: /home → another disk
        (tmp_path / "home").symlink_to(disk, target_is_directory=True)
        root = tmp_path / "home" / "client"
    else:  # the root itself is a link: ~/mnema → /mnt/disk/mnema
        root = tmp_path / "mnema"
        root.symlink_to(disk / "client", target_is_directory=True)
    folder = recording(root, "Работа/Встреча")

    def run_at_link():
        return asyncio.run(
            SyncEngine(
                root,
                pair.checkpoint,
                selected=lambda: pair.selected[0],
                busy=lambda: set(),
                trash=lambda path: pytest.fail("unexpected deletion"),
                client_factory=lambda c: LibraryClient(c, transport=pair.transport),
            ).run_once()
        )

    assert not run_at_link().errors
    id = json.loads((folder / FileName.META).read_text())["id"]
    assert [r.path for r in pair.app.state.library.manifest().records] == ["Работа/Встреча"]
    edit(server_folder(pair, id) / FileName.TRANSCRIPT, "с сервера", 300)
    assert not run_at_link().errors
    assert (folder / FileName.TRANSCRIPT).read_text() == "с сервера"


def test_root_replaced_by_a_link_after_start_deletes_nothing(pair, tmp_path):
    _, id = synced(pair)
    engine = pair.engine()  # the root is resolved when the sync starts
    before = pair.checkpoint.read_bytes()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    pair.root.rename(tmp_path / "parked")
    pair.root.symlink_to(elsewhere, target_is_directory=True)
    result = asyncio.run(engine.run_once())
    assert result.errors and not result.changed and not pair.trashed
    assert pair.checkpoint.read_bytes() == before
    assert [r.id for r in pair.app.state.library.manifest().records] == [id]
    assert list(elsewhere.iterdir()) == []


def test_known_record_without_its_files_is_not_a_deletion(pair):
    folder, id = synced(pair, "Работа/Встреча")
    before = pair.checkpoint.read_bytes()
    deeper = folder / "Старое"
    deeper.mkdir()
    for path in folder.iterdir():
        if path != deeper:
            # The recording went one level deeper; the walk does not see it.
            path.rename(deeper / path.name)
    pair.transport.requests.clear()
    result = run(pair)
    assert result.errors and not result.changed and not pair.trashed
    assert pair.checkpoint.read_bytes() == before
    assert [r.id for r in pair.app.state.library.manifest().records] == [id]
    assert all(method == "GET" for method, _ in pair.transport.requests)


def test_record_moved_into_a_new_subfolder_deletes_nothing_anywhere(pair, tmp_path):
    folder, id = synced(pair, "Работа/Встреча")
    phone = tmp_path / "phone"
    phone.mkdir()

    def other():
        return SyncEngine(
            phone,
            tmp_path / "phone-sync.json",
            selected=lambda: pair.selected[0],
            busy=lambda: set(),
            trash=lambda path: pytest.fail("unexpected deletion"),
            client_factory=lambda connection: LibraryClient(connection, transport=pair.transport),
        )

    assert not asyncio.run(other().run_once()).errors
    (pair.root / "Работа/Старые").mkdir()
    folder.rename(pair.root / "Работа/Старые/Встреча")
    before = pair.checkpoint.read_bytes()
    pair.transport.requests.clear()
    result = run(pair)
    assert "Запись «Работа/Старые/Встреча» лежит в подпапке раздела" in result.errors
    assert not result.changed and not result.bulk_missing and not pair.trashed
    assert pair.checkpoint.read_bytes() == before
    assert all(method == "GET" for method, _ in pair.transport.requests)
    assert not asyncio.run(other().run_once()).errors
    assert [r.id for r in pair.app.state.library.manifest().records] == [id]
    assert (phone / "Работа/Встреча" / FileName.TRANSCRIPT).read_text() == "оригинал"
    (pair.root / "Работа/Старые").rename(pair.root / "Старые")  # the subfolder became a section
    assert not run(pair).errors
    assert not asyncio.run(other().run_once()).errors
    [record] = pair.app.state.library.manifest().records
    assert record.id == id and record.path == "Старые/Встреча" and not pair.trashed
    assert (phone / "Старые/Встреча" / FileName.TRANSCRIPT).read_text() == "оригинал"


def lose_on_server(pair, *ids):
    for record in pair.app.state.library.manifest().records:
        if record.id in ids:
            pair.app.state.library.remove(record.id, record.tag)


@pytest.mark.parametrize("restore", [True, False])
def test_bulk_disappearance_on_server_holds_until_explicit_choice(pair, restore):
    a, aid = synced(pair, "a")
    b, bid = synced(pair, "b")
    lose_on_server(pair, aid, bid)  # the server lost its data, the address is the same
    for _ in range(2):
        result = run(pair)
        assert not result.errors and result.bulk_on_server
        assert result.bulk_missing == sorted([aid, bid])
        assert a.is_dir() and b.is_dir() and not pair.trashed
    result = asyncio.run(pair.engine().resolve_bulk(result.bulk_missing, restore=restore))
    assert not result.errors and not result.bulk_missing
    remote = {r.id for r in pair.app.state.library.manifest().records}
    # "Restore to the server": the same recordings are on the server again, files in place.
    if restore:
        assert remote == {aid, bid} and a.is_dir() and b.is_dir() and not pair.trashed
        assert (server_folder(pair, aid) / FileName.TRANSCRIPT).read_text() == "оригинал"
    else:  # "Delete on this computer": the regular Trash
        assert remote == set() and not a.exists() and not b.exists()
        assert len(pair.trashed) == 2 and load(pair.checkpoint).records == {}


def test_bulk_return_to_server_survives_interruption_after_first_record(pair):
    """ "Restore to the server": the app was closed after the server accepted the first recording.
    The set is removed from the checkpoint before the first upload, so the next sync does not take
    the second one for a single missing recording on the server and move it to the Trash."""
    a, aid = synced(pair, "a")
    b, bid = synced(pair, "b")
    lose_on_server(pair, aid, bid)
    assert run(pair).bulk_on_server

    def stop_after_create(request, response):
        if request.method == "POST" and request.url.path.endswith("/records"):
            raise asyncio.CancelledError("closed")

    pair.transport.hook = stop_after_create
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(pair.engine().resolve_bulk([aid, bid], restore=True))
    assert len(pair.app.state.library.manifest().records) == 1
    pair.transport.hook = lambda request, response: None
    assert not run(pair).errors
    assert a.is_dir() and b.is_dir() and not pair.trashed
    assert {r.id for r in pair.app.state.library.manifest().records} == {aid, bid}


def test_both_sides_bulk_disappearances_are_asked_one_after_another(pair):
    (a, aid), (b, bid) = synced(pair, "a"), synced(pair, "b")
    (c, cid), (d, did) = synced(pair, "c"), synced(pair, "d")
    shutil.rmtree(a)
    shutil.rmtree(b)
    lose_on_server(pair, cid, did)
    result = run(pair)
    assert result.bulk_missing == sorted([aid, bid]) and not result.bulk_on_server
    assert c.is_dir() and d.is_dir() and not pair.trashed
    result = asyncio.run(pair.engine().resolve_bulk(result.bulk_missing, restore=True))
    assert a.is_dir() and b.is_dir()
    assert result.bulk_missing == sorted([cid, did]) and result.bulk_on_server
    assert c.is_dir() and d.is_dir() and not pair.trashed
    result = asyncio.run(pair.engine().resolve_bulk(result.bulk_missing, restore=False))
    assert not result.errors and not result.bulk_missing
    assert not c.exists() and not d.exists() and len(pair.trashed) == 2
    assert {r.id for r in pair.app.state.library.manifest().records} == {aid, bid}


DECISIONS = json.loads((Path(__file__).parent / "fixtures/sync-decisions-v1.json").read_text())


@pytest.mark.parametrize("case", DECISIONS["cases"], ids=lambda case: case["name"])
def test_shared_sync_decisions(pair, tmp_path, case):
    """Deletions, copies and busy recordings: the same decisions as the phone (shared fixture)."""
    ids = {path: synced(pair, path)[1] for path in case["synced"]}
    names = {id: path for path, id in ids.items()}
    root = pair.root
    for copy, source in case.get("copied", {}).items():
        shutil.copytree(root / source, root / copy)
    for old, new in case.get("renamed", {}).items():
        (root / new).parent.mkdir(parents=True, exist_ok=True)
        (root / old).rename(root / new)
    for path in case.get("removed", []):
        shutil.rmtree(root / path)
    for path in case.get("broken", []):
        (root / path / FileName.META).write_text("broken")
    if case.get("root_removed"):
        shutil.rmtree(root)
    lose_on_server(pair, *(ids[path] for path in case.get("lost", [])))
    pair.busy.update(ids[path] for path in case.get("busy", []))
    if case.get("server_unavailable"):
        pair.transport.inner = httpx.MockTransport(
            lambda request: httpx.Response(503, json={"detail": "Библиотека недоступна"})
        )
    mode = root.stat().st_mode if root.exists() else None
    if case.get("root_unreadable"):
        root.chmod(0)
        if os.access(root, os.R_OK):
            root.chmod(mode)
            pytest.skip("root can read a closed folder")
    trashed = []

    def trash(path):
        trashed.append(path.relative_to(root).as_posix())
        path.rename(tmp_path / str(uuid4()))

    engine = SyncEngine(
        root,
        pair.checkpoint,
        selected=lambda: pair.selected[0],
        busy=lambda: pair.busy,
        trash=trash,
        client_factory=lambda connection: LibraryClient(connection, transport=pair.transport),
    )
    try:
        result = asyncio.run(engine.run_once())
    finally:
        if mode is not None:
            root.chmod(mode)

    assert bool(result.errors) == case.get("errors", False), result.errors
    assert sorted(names[id] for id in result.bulk_missing) == case.get("held", [])
    if case.get("held"):
        assert result.bulk_on_server == (case["held_on"] == "server")
    local = [p.parent.relative_to(root).as_posix() for p in root.rglob(FileName.META)]
    assert sorted(local) == sorted(case["local"])
    records = pair.app.state.library.manifest().records
    assert {r.path: names.get(r.id, "new") for r in records} == case["server"]
    assert sorted(trashed) == sorted(case.get("trashed", []))
