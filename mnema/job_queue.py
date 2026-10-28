"""The runner's bounded queue, with slots of their own for short jobs and text."""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from collections.abc import Callable
from enum import StrEnum

from pydantic import BaseModel


class _Lane(StrEnum):
    MAIN = "main"  # MNEMA_JOB_WORKERS: recognition and media
    SHORT = "short"  # beyond workers: short jobs only (voice messages, video notes)
    TEXT = "text"  # title and summary of ready text: no engines, waits on the cloud model


class _Task(BaseModel):
    run: Callable[[], None]
    cancel: Callable[[], None]
    short: bool = False
    text: bool = False


class JobQueue:
    """Only running jobs have threads, and they end when the queue is empty.

    A short job (up to 5 minutes of audio) never waits for a long recording: it has its own
    slot beyond workers and goes first in the shared queue. A text job (a summary, up to
    ~7 min) has its own lane: it neither waits for recognition nor takes its slots, so no
    recognition model is added to memory.
    lock makes the room check, the Job creation and submit one atomic API operation.
    """

    def __init__(self, workers: int, pending: int, short_slots: int = 1, text_slots: int = 2):
        self.workers = workers
        self.short_slots = short_slots
        self.text_slots = text_slots
        self.pending_limit = pending
        self.lock = threading.RLock()
        self._busy = dict.fromkeys(_Lane, 0)
        self._pending: OrderedDict[str, _Task] = OrderedDict()

    def has_room(self, *, text: bool = False) -> bool:
        with self.lock:
            free = (
                self._busy[_Lane.TEXT] < self.text_slots
                if text
                else self._busy[_Lane.MAIN] < self.workers
            )
            return free or len(self._pending) < self.pending_limit

    def submit(
        self,
        job_id: str,
        run: Callable[[], None],
        cancel: Callable[[], None],
        *,
        short: bool = False,
        text: bool = False,
    ) -> None:
        with self.lock:
            task = _Task(run=run, cancel=cancel, short=short and not text, text=text)
            if (lane := self._free_lane(task)) is not None:
                self._start(task, lane)
            else:
                self._pending[job_id] = task

    def _free_lane(self, task: _Task) -> _Lane | None:
        if task.text:
            return _Lane.TEXT if self._busy[_Lane.TEXT] < self.text_slots else None
        if self._busy[_Lane.MAIN] < self.workers:
            return _Lane.MAIN
        if task.short and self._busy[_Lane.SHORT] < self.short_slots:
            return _Lane.SHORT
        return None

    def _start(self, task: _Task, lane: _Lane) -> None:
        self._busy[lane] += 1
        try:
            threading.Thread(target=self._work, args=(task, lane), daemon=True).start()
        except Exception:
            self._busy[lane] -= 1
            task.cancel()
            raise

    def cancel_pending(self, job_id: str) -> bool:
        with self.lock:
            task = self._pending.pop(job_id, None)
            if task is None:
                return False
            task.cancel()
            return True

    def _next(self, lane: _Lane) -> _Task | None:
        """The next job for a freed slot: the text lane takes only text, short jobs go first,
        the short slot takes only short jobs."""
        pending = self._pending.items()
        if lane is _Lane.TEXT:
            job_id = next((k for k, t in pending if t.text), None)
        else:
            job_id = next((k for k, t in pending if t.short), None)
            if job_id is None and lane is _Lane.MAIN:
                job_id = next((k for k, t in pending if not t.text), None)
        return self._pending.pop(job_id) if job_id is not None else None

    def _work(self, task: _Task, lane: _Lane) -> None:
        while True:
            next_task = None
            try:
                task.run()
            except Exception:
                # The API wrapper stores ordinary errors; an unexpected one must not hold the slot.
                logging.getLogger(__name__).exception("unhandled runner job error")
            finally:
                with self.lock:
                    next_task = self._next(lane)
                    if next_task is None:
                        self._busy[lane] -= 1
            if next_task is None:
                return
            task = next_task
