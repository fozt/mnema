"""The warm recognition process with real child processes: the real serving loop of
core/worker.py around a fake recognition."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from mnema.core import worker as w
from mnema.core.base import MediaError
from mnema.job_queue import MEMORY_PER_JOB, JobQueue

_SCRIPT = r"""
import os, sys, time
from mnema import i18n
from mnema.core import worker

served = []

def execute(operation, payload, on):
    served.append(operation)
    mode = payload.get("mode")
    if mode == "crash":
        os._exit(17)
    if mode == "error":
        raise worker.MediaError("запись не читается")
    if mode == "hang":
        on(None, "начал")
        time.sleep(300)
    return {"pid": os.getpid(), "served": len(served), "threads": payload.get("threads"),
            "language": i18n.current()}

worker._execute = execute
worker.main()
"""
_STATUS = (None, "Готовлюсь")


@pytest.fixture
def warm(monkeypatch, tmp_path):
    (tmp_path / "fixture_warm_worker.py").write_text(_SCRIPT)
    monkeypatch.setenv(
        "PYTHONPATH", os.pathsep.join(filter(None, (str(tmp_path), os.getenv("PYTHONPATH"))))
    )
    actual, workers = w.Worker, []

    def start():
        workers.append(actual("fixture_warm_worker"))
        return workers[-1]

    monkeypatch.setattr(w, "Worker", start)
    process = w.WarmProcess(threads=3)
    yield process
    process.release()
    for child in workers:
        child.close()
        assert child.process.poll() is not None
        assert not Path(child._temp.name).exists()


def job(process, on=lambda *_: None, **payload):
    return process.run("transcribe", payload, _STATUS, on)


def test_jobs_share_one_process_with_the_thread_count_of_its_model(warm):
    first, second = job(warm), job(warm)
    assert first["pid"] == second["pid"] != os.getpid()
    assert second["served"] == 2
    assert second["threads"] == 3


def test_a_failed_job_keeps_the_process(warm):
    first = job(warm)
    with pytest.raises(MediaError, match="запись не читается"):
        job(warm, mode="error")
    assert job(warm)["pid"] == first["pid"]


def test_a_crash_fails_its_job_and_the_next_one_starts_a_new_process(warm):
    first = job(warm)
    with pytest.raises(MediaError, match="без результата|without a result"):
        job(warm, mode="crash")
    assert job(warm)["pid"] != first["pid"]


def test_a_cancel_ends_the_process_and_the_next_job_goes_on(warm):
    first = job(warm)

    def cancel(_fraction, message):
        if message == "начал":
            raise MediaError("Отменено")

    with pytest.raises(MediaError, match="Отменено"):
        job(warm, cancel, mode="hang")
    assert job(warm)["pid"] != first["pid"]


def test_a_process_past_the_memory_limit_is_replaced_after_its_job(warm, monkeypatch):
    monkeypatch.setattr(w, "WARM_MEMORY_LIMIT", 1)
    first = job(warm)  # its answer still arrives
    assert job(warm)["pid"] != first["pid"]


def test_release_frees_an_idle_process(warm):
    first = job(warm)
    warm.release()
    assert job(warm)["pid"] != first["pid"]


def test_texts_follow_the_job_not_the_first_job_of_the_process(warm):
    from mnema import i18n

    with i18n.using("ru"):
        assert job(warm)["language"] == "ru"
    with i18n.using("en"):
        assert job(warm)["language"] == "en"


def test_a_job_with_a_key_never_enters_the_warm_process(warm, monkeypatch):
    seen = []
    monkeypatch.setattr(warm, "run", lambda *args: seen.append(args))
    monkeypatch.setattr(w, "Worker", lambda: (_ for _ in ()).throw(MediaError("холодный")))
    with w.warm(warm), pytest.raises(MediaError, match="холодный"):
        w.run("transcribe", {"key": "sk-secret"})
    assert seen == []


def test_the_model_can_be_ready_before_the_first_job(warm):
    warm.preload()
    assert job(warm)["served"] == 2


def test_the_queue_takes_the_memory_back_when_a_job_needs_it():
    released = []
    started = threading.Event()
    free = [MEMORY_PER_JOB - 1]
    queue = JobQueue(
        1, 4, memory=lambda: free[0], recheck_s=0.01, release=lambda: released.append(1)
    )
    queue.submit("long", started.set, lambda: None)
    assert started.wait(5)  # nothing else runs: it starts, and memory was asked back
    assert released
