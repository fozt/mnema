"""One background sync loop; the UI passes the chosen node, what is busy and the system trash."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from pathlib import Path

from ..sync.client import LibraryClient
from ..sync.engine import SYNC_FAILED, SyncEngine, SyncStatus

logger = logging.getLogger(__name__)


class SyncController:
    def __init__(
        self,
        root: Path,
        state_path: Path,
        *,
        selected: Callable[[], str | None],
        busy: Callable[[], set[str]],
        trash: Callable[[Path], None],
        on_change: Callable[[SyncStatus], None],
        interval: float = 180,
        client_factory: Callable[[str], LibraryClient] = LibraryClient,
    ) -> None:
        self.engine = SyncEngine(
            root,
            state_path,
            selected=selected,
            busy=busy,
            trash=trash,
            client_factory=client_factory,
        )
        self._on_change, self._interval = on_change, interval
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._event: asyncio.Event | None = None
        self._task: asyncio.Task | None = None
        self._closed = False
        self._choice: tuple[list[str], bool, str] | None = None  # only a UI button pressed now
        self._send: tuple[str, str] | None = None  # "Send" to another library: number, connection
        self._status = SyncStatus()

    @property
    def status(self) -> SyncStatus:
        with self._lock:
            return self._status.model_copy(deep=True)

    def start(self) -> None:
        with self._lock:
            if self._thread is not None or self._closed:
                return
            self._thread = threading.Thread(target=self._worker, name="mnema-sync", daemon=True)
            self._thread.start()

    def wake(self) -> None:
        with self._lock:
            if self._loop is not None and self._event is not None and not self._closed:
                self._loop.call_soon_threadsafe(self._event.set)

    def resolve_bulk(self, ids: list[str], *, restore: bool) -> None:
        with self._lock:
            connection = self._status.connection
            if (
                self._closed
                or connection is None
                or self.engine.selected() != connection
                or set(ids) != set(self._status.bulk_missing)
            ):
                return
            self._choice = (list(ids), restore, connection)
        self.wake()

    def send_to_library(self, library: str) -> None:
        with self._lock:
            connection = self._status.connection
            if (
                self._closed
                or connection is None
                or self.engine.selected() != connection
                or self._status.other_library is None
                or library != self._status.foreign
            ):
                return
            self._send = (library, connection)
        self.wake()

    def close(self, timeout: float = 2) -> None:
        with self._lock:
            self._closed = True
            if self._loop is not None and self._task is not None:
                self._loop.call_soon_threadsafe(self._task.cancel)
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def _worker(self) -> None:
        asyncio.run(self._run())

    async def _run(self) -> None:
        event = asyncio.Event()
        event.set()  # the first sync at startup
        with self._lock:
            if self._closed:
                return
            self._loop, self._event, self._task = (
                asyncio.get_running_loop(),
                event,
                asyncio.current_task(),
            )
        try:
            while True:
                try:
                    await asyncio.wait_for(event.wait(), timeout=self._interval)
                except TimeoutError:
                    pass
                event.clear()
                with self._lock:
                    choice, self._choice = self._choice, None
                    send, self._send = self._send, None
                try:
                    if choice:
                        status = await self.engine.resolve_bulk(
                            choice[0], choice[1], connection=choice[2]
                        )
                    elif send:
                        status = await self.engine.send_to_library(send[0], connection=send[1])
                    else:
                        status = await self.engine.run_once()
                except Exception:  # noqa: BLE001 — a failed pass does not stop the next ones
                    logger.warning("sync with the server failed", exc_info=True)
                    status = SyncStatus(errors=[SYNC_FAILED])
                with self._lock:
                    self._status = status
                try:
                    self._on_change(status.model_copy(deep=True))
                except Exception:  # noqa: BLE001 — a closed window does not stop syncing
                    logger.exception("could not update the library state")
        except asyncio.CancelledError:
            pass
        finally:
            with self._lock:
                self._loop = self._event = self._task = None
