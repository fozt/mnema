"""Desktop sync lifecycle over ASGI, with no background requests to real nodes."""

import asyncio
import queue
import shutil
import threading

import httpx
from test_sync_engine import Transport, recording, remote_record

from mnema.bar.sync_controller import SyncController
from mnema.server import create_app
from mnema.sync.client import LibraryClient


def test_worker_starts_explicitly_wakes_periodically_and_stops(tmp_path, monkeypatch):
    monkeypatch.setenv("MNEMA_LIBRARY", "true")
    (tmp_path / "server/library").mkdir(parents=True)  # the installer creates it
    app = create_app(tmp_path / "server")
    root = tmp_path / "client"
    root.mkdir()
    results = queue.Queue()
    selected = [None]
    requests = []

    class Transport(httpx.ASGITransport):
        async def handle_async_request(self, request):
            requests.append(str(request.url))
            return await super().handle_async_request(request)

    controller = SyncController(
        root,
        tmp_path / "sync.json",
        selected=lambda: selected[0],
        busy=lambda: set(),
        trash=lambda path: None,
        on_change=results.put,
        interval=0.1,
        client_factory=lambda connection: LibraryClient(connection, transport=Transport(app)),
    )
    assert not requests
    controller.start()
    assert not results.get(timeout=3).errors and not requests
    selected[0] = "http://explicit-node#token=specific-token"
    record = remote_record(app, "new")
    controller.wake()
    try:
        while not (root / "new/transcript.txt").exists():
            assert not results.get(timeout=3).errors
        assert controller.status.changed
        remote_record(app, "periodic")
        while not (root / "periodic/transcript.txt").exists():
            assert not results.get(timeout=3).errors
        assert all(url.startswith("http://explicit-node/") for url in requests)
    finally:
        controller.close()
    assert record.id in (tmp_path / "sync.json").read_text()


def test_worker_bulk_restore_uses_callbacks_and_stays_serial(tmp_path, monkeypatch):
    monkeypatch.setenv("MNEMA_LIBRARY", "true")
    (tmp_path / "server/library").mkdir(parents=True)  # the installer creates it
    app = create_app(tmp_path / "server")
    root = tmp_path / "client"
    root.mkdir()
    a, b = recording(root, "a"), recording(root, "b")
    results = queue.Queue()
    controller = SyncController(
        root,
        tmp_path / "sync.json",
        selected=lambda: "http://node",
        busy=lambda: set(),
        trash=lambda path: None,
        on_change=results.put,
        client_factory=lambda connection: LibraryClient(
            connection, transport=httpx.ASGITransport(app)
        ),
    )
    controller.start()
    try:
        assert not results.get(timeout=3).errors
        shutil.rmtree(a)
        shutil.rmtree(b)
        controller.wake()
        held = results.get(timeout=3)
        assert len(held.bulk_missing) == 2
        controller.resolve_bulk(held.bulk_missing, restore=True)
        restored = results.get(timeout=3)
        assert not restored.errors and not restored.bulk_missing
        assert a.exists() and b.exists()
        assert len(app.state.library.manifest().records) == 2
    finally:
        controller.close()


def test_worker_sends_to_another_library_only_on_the_answer_with_its_number(tmp_path, monkeypatch):
    monkeypatch.setenv("MNEMA_LIBRARY", "true")
    (tmp_path / "server/library").mkdir(parents=True)  # the installer creates it
    app = create_app(tmp_path / "server")
    root = tmp_path / "client"
    root.mkdir()
    recording(root, "a")
    transport = Transport(app)
    transport.library = "L1"
    results = queue.Queue()
    controller = SyncController(
        root,
        tmp_path / "sync.json",
        selected=lambda: "http://node",
        busy=lambda: set(),
        trash=lambda path: None,
        on_change=results.put,
        client_factory=lambda connection: LibraryClient(connection, transport=transport),
    )
    controller.start()
    try:
        assert not results.get(timeout=3).errors
        [record] = app.state.library.manifest().records
        app.state.library.remove(record.id, record.tag)
        transport.library = "L2"  # the server was installed again at the same address
        controller.wake()
        asked = results.get(timeout=3)
        assert (asked.other_library, asked.foreign) == (1, "L2")
        controller.send_to_library("L1")  # not the number asked about
        controller.wake()
        assert results.get(timeout=3).other_library == 1
        controller.send_to_library("L2")
        sent = results.get(timeout=3)
        assert not sent.errors and sent.other_library is None
        assert [r.path for r in app.state.library.manifest().records] == ["a"]
        assert (root / "a").exists()
    finally:
        controller.close()


def test_close_cancels_inflight_http_without_applying_or_leaving_worker(tmp_path):
    root = tmp_path / "client"
    root.mkdir()
    started, stopped = threading.Event(), threading.Event()

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

    controller = SyncController(
        root,
        tmp_path / "sync.json",
        selected=lambda: "http://node",
        busy=lambda: set(),
        trash=lambda path: None,
        on_change=lambda status: None,
        client_factory=lambda connection: LibraryClient(connection, transport=Transport()),
    )
    controller.start()
    assert started.wait(3)
    controller.close()
    assert stopped.is_set()
    assert not (tmp_path / "sync.json").exists()


def test_bulk_queue_is_bound_to_the_server_that_produced_the_status(tmp_path):
    """Switch both before the click and while its action is already queued."""
    from mnema.sync.engine import SyncStatus

    selected = ["http://a#token=secret-a"]
    ids = ["id-a", "id-b"]
    received = []
    controller = SyncController(
        tmp_path,
        tmp_path / "sync.json",
        selected=lambda: selected[0],
        busy=lambda: set(),
        trash=lambda path: None,
        on_change=received.append,
        client_factory=lambda connection: (_ for _ in ()).throw(AssertionError("no HTTP")),
    )
    controller._status = SyncStatus(connection=selected[0], bulk_missing=ids)
    selected[0] = "http://b#token=secret-b"
    controller.resolve_bulk(ids, restore=False)
    assert controller._choice is None
    selected[0] = "http://a#token=secret-a"
    controller.resolve_bulk(ids, restore=False)
    choice = controller._choice
    assert choice is not None
    selected[0] = "http://b#token=secret-b"
    # The worker passes the captured connection into the real engine under its async lock.
    status = asyncio.run(controller.engine.resolve_bulk(choice[0], choice[1], connection=choice[2]))
    assert status.errors and not status.changed
    assert not (tmp_path / "sync.json").exists()
    assert "secret" not in status.model_dump_json() and "secret" not in repr(status)
