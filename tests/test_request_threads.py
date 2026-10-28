"""The CPU budget of one job: the HTTP contract and independence of parallel calls."""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from mnema.core import backends
from mnema.core.settings import asr_thread_budget, requested_asr_threads


@pytest.fixture(autouse=True)
def cpu_environment(monkeypatch):
    """OS limits are fixed regardless of the machine running the test."""
    original = Path.read_text

    def read(path, *args, **kwargs):
        if str(path).startswith("/sys/fs/cgroup") or str(path) == "/proc/self/cgroup":
            return ""
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(os, "sched_getaffinity", lambda _: set(range(8)), raising=False)
    monkeypatch.setenv("MNEMA_ASR_THREADS", "4")


@pytest.mark.parametrize("params", [{}, {"threads": 2}, {"threads": "auto"}])
def test_runner_rejects_client_cpu_setting(monkeypatch, tmp_path, params):
    """The node sets the CPU budget; client overrides are rejected."""
    from fastapi.testclient import TestClient

    from mnema.core import worker
    from mnema.server import create_app

    monkeypatch.setenv("MNEMA_ASR_THREADS", "6")
    payloads = []

    def run(operation, payload, *, on):  # boundary: the recognition process
        payloads.append(payload)
        return {"text": "", "segments": []}

    monkeypatch.setattr(worker, "run", run)
    source = tmp_path / "call.wav"
    source.write_bytes(b"audio")
    client = TestClient(create_app(tmp_path))
    response = client.post(
        "/api/jobs", json={"op": "transcribe", "path": str(source), "params": params}
    )
    if params:
        assert response.status_code == 422
        assert payloads == []
        return
    job = response.json()
    for _ in range(250):
        job = client.get(f"/api/jobs/{job['id']}").json()
        if job["status"] in ("done", "error"):
            break
        time.sleep(0.02)
    assert job["status"] == "done", job
    assert [p["threads"] for p in payloads] == [6]


def test_nested_cpu_budget_restores_after_error():
    with asr_thread_budget(2):
        with pytest.raises(RuntimeError), asr_thread_budget("auto"):
            assert requested_asr_threads() == "auto"
            raise RuntimeError("отмена")
        assert requested_asr_threads() == 2
    assert requested_asr_threads() == 4


def test_concurrent_requests_keep_their_cpu_budget_and_environment():
    entered = threading.Barrier(2)
    checked = threading.Barrier(2)

    def run(value):
        with asr_thread_budget(value):
            entered.wait(timeout=5)
            actual = backends._asr_cpu_threads()
            checked.wait(timeout=5)
            assert os.environ["MNEMA_ASR_THREADS"] == "4"
        return actual, requested_asr_threads()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run, value) for value in (2, "auto")]
        assert [f.result(timeout=5) for f in futures] == [(2, 4), (8, 4)]
