"""The runner's bounded queue: local jobs by free memory and cores, own lanes for short jobs,
text and cloud recognition."""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter, OrderedDict
from collections.abc import Callable
from enum import StrEnum

from pydantic import BaseModel

from .core.settings import available_memory, default_workers

MEMORY_PER_JOB = 5 * 1024**3  # a local recognition job at its peak
MEMORY_RECHECK_S = 10.0

_log = logging.getLogger(__name__)


class _Lane(StrEnum):
    MAIN = "main"  # MNEMA_JOB_WORKERS: recognition and media
    SHORT = "short"  # beyond workers: short jobs only (voice messages, video notes)
    TEXT = "text"  # title and summary of ready text: no engines, waits on the cloud model
    CLOUD = "cloud"  # cloud recognition: no engine in memory, no memory check


_LOCAL = (_Lane.MAIN, _Lane.SHORT)


class _Task(BaseModel):
    run: Callable[[], None]
    cancel: Callable[[], None]
    short: bool = False
    text: bool = False
    cloud: bool = False
    user: str | None = None

    @property
    def local(self) -> bool:
        return not self.text and not self.cloud

    def fits(self, lane: _Lane) -> bool:
        return {
            _Lane.MAIN: self.local,
            _Lane.SHORT: self.local and self.short,
            _Lane.TEXT: self.text,
            _Lane.CLOUD: self.cloud,
        }[lane]


class JobQueue:
    """Only running jobs have threads, and they end when the queue is empty.

    A short job (up to 5 minutes of audio) never waits for a long recording: it has its own
    slot beyond workers and goes first in the shared queue. A text job (a summary, up to
    ~7 min) and a cloud recognition job have lanes of their own: they neither wait for local
    recognition nor take its slots. A local job starts only with MEMORY_PER_JOB free per
    starting job or when no local job runs; held ones are rechecked when a slot frees and
    every recheck_s while they wait.
    A freed slot goes to the user whose last job started longest ago (None is one user), and
    while others are around one user cannot hold more than half of the waiting places.
    lock makes the room check, the Job creation and submit one atomic API operation.
    """

    def __init__(
        self,
        workers: int | None,
        pending: int,
        *,
        short_slots: int = 1,
        text_slots: int = 2,
        cloud_slots: int = 4,
        memory: Callable[[], int | None] | None = None,
        recheck_s: float = MEMORY_RECHECK_S,
    ):
        self.workers = default_workers() if workers is None else workers
        self.short_slots = short_slots
        self.text_slots = text_slots
        self.cloud_slots = cloud_slots
        self.pending_limit = pending
        self.memory = memory  # None — this machine's free memory (available_memory)
        self.recheck_s = recheck_s
        self.lock = threading.RLock()
        self._busy = dict.fromkeys(_Lane, 0)
        self._pending: OrderedDict[str, _Task] = OrderedDict()
        self._running: Counter[str | None] = Counter()
        self._started: dict[str | None, int] = {}
        self._clock = 0
        self._watching = False

    @property
    def running_local(self) -> int:
        with self.lock:
            return sum(self._busy[lane] for lane in _LOCAL)

    def _slots(self, lane: _Lane) -> int:
        return {
            _Lane.MAIN: self.workers,
            _Lane.SHORT: self.short_slots,
            _Lane.TEXT: self.text_slots,
            _Lane.CLOUD: self.cloud_slots,
        }[lane]

    def has_room(self, *, text: bool = False, cloud: bool = False, user: str | None = None) -> bool:
        with self.lock:
            lane = _Lane.TEXT if text else _Lane.CLOUD if cloud else _Lane.MAIN
            if self._busy[lane] < self._slots(lane) and not any(
                t.fits(lane) for t in self._pending.values()
            ):
                return True
            if len(self._pending) >= self.pending_limit:
                return False
            others = any(t.user != user for t in self._pending.values()) or any(
                n > 0 and u != user for u, n in self._running.items()
            )
            waiting = sum(t.user == user for t in self._pending.values())
            return not (others and waiting >= max(1, self.pending_limit // 2))

    def submit(
        self,
        job_id: str,
        run: Callable[[], None],
        cancel: Callable[[], None],
        *,
        short: bool = False,
        text: bool = False,
        cloud: bool = False,
        user: str | None = None,
    ) -> None:
        with self.lock:
            cloud = cloud and not text
            self._pending[job_id] = _Task(
                run=run,
                cancel=cancel,
                short=short and not text and not cloud,
                text=text,
                cloud=cloud,
                user=user,
            )
            self._fill()

    def _fill(self, keep: _Lane | None = None) -> _Task | None:
        """Starts waiting jobs on free slots. keep is the lane of a thread that has just freed
        its slot: its next job is returned to that thread instead of a new one."""
        started = 0
        reading: list[int | None] = []

        def memory_ok() -> bool:
            if sum(self._busy[lane] for lane in _LOCAL) == 0:
                return True
            if not reading:
                reading.append(self._free_memory())
            free = reading[0]
            return free is None or free >= MEMORY_PER_JOB * (started + 1)

        mine = None
        for lane in sorted(_Lane, key=lambda lane: lane != keep):
            while self._busy[lane] < self._slots(lane):
                task = self._next(lane, memory_ok)
                if task is None:
                    break
                self._busy[lane] += 1
                self._begin(task)
                started += task.local
                if lane == keep and mine is None:
                    mine = task
                else:
                    self._spawn(task, lane)
        if self._held():
            self._watch()
        return mine

    def _free_memory(self) -> int | None:
        try:
            return (self.memory or available_memory)()
        except Exception:
            _log.exception("reading free memory failed")
            return None

    def _held(self) -> bool:
        """A local job waits although a slot for it is free: memory holds it."""
        return any(
            t.fits(lane) and self._busy[lane] < self._slots(lane)
            for t in self._pending.values()
            for lane in _LOCAL
        )

    def _watch(self) -> None:
        if self._watching:
            return
        try:
            threading.Thread(target=self._recheck, name="mnema-queue-memory", daemon=True).start()
        except Exception:  # held jobs still start when a slot frees
            _log.exception("starting the queue memory check failed")
            return
        self._watching = True

    def _recheck(self) -> None:
        while True:
            time.sleep(self.recheck_s)
            with self.lock:
                self._fill()
                if not self._held():
                    self._watching = False
                    return

    def _spawn(self, task: _Task, lane: _Lane) -> None:
        try:
            threading.Thread(target=self._work, args=(task, lane), daemon=True).start()
        except Exception:
            self._busy[lane] -= 1
            self._running[task.user] -= 1
            _log.exception("starting a runner job thread failed")
            task.cancel()

    def _begin(self, task: _Task) -> None:
        self._clock += 1
        self._started[task.user] = self._clock
        self._running[task.user] += 1

    def cancel_pending(self, job_id: str) -> bool:
        with self.lock:
            task = self._pending.pop(job_id, None)
            if task is None:
                return False
            task.cancel()
            return True

    def _next(self, lane: _Lane, memory_ok: Callable[[], bool]) -> _Task | None:
        """The next job for a free slot of the lane: short jobs go first, the short slot takes
        only short jobs, a local one only when memory allows. Of the users with such jobs the
        one that started longest ago goes first, equal ones in order of arrival."""
        wanted = [(k, t) for k, t in self._pending.items() if t.fits(lane)]
        if not wanted or (lane in _LOCAL and not memory_ok()):
            return None
        turn = min(self._started.get(t.user, 0) for _, t in wanted)
        mine = [(k, t) for k, t in wanted if self._started.get(t.user, 0) == turn]
        job_id = next((k for k, t in mine if t.short), mine[0][0])
        return self._pending.pop(job_id)

    def _work(self, task: _Task, lane: _Lane) -> None:
        while True:
            next_task = None
            try:
                task.run()
            except Exception:
                # The API wrapper stores ordinary errors; an unexpected one must not hold the slot.
                _log.exception("unhandled runner job error")
            finally:
                with self.lock:
                    self._running[task.user] -= 1
                    self._busy[lane] -= 1
                    next_task = self._fill(keep=lane)
            if next_task is None:
                return
            task = next_task
