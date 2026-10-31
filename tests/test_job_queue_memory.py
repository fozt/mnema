"""Local jobs start by free memory; cloud recognition has its own lane without the check."""

import threading
import time
from pathlib import Path

import pytest

from mnema.core.settings import available_memory
from mnema.job_queue import MEMORY_PER_JOB, JobQueue

GIB = 1024**3


class Probe:
    """Jobs that record their start order and finish when released."""

    def __init__(self):
        self.order = []
        self.gates = {}
        self.lock = threading.Lock()

    def add(self, queue, name, **kwargs):
        gate = self.gates[name] = threading.Event()

        def run():
            with self.lock:
                self.order.append(name)
            gate.wait(5)

        queue.submit(name, run, lambda: None, **kwargs)

    def started(self, count):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with self.lock:
                if len(self.order) >= count:
                    return list(self.order)
            time.sleep(0.002)
        pytest.fail(f"only {self.order} started")

    def still(self, seconds=0.1):
        time.sleep(seconds)
        with self.lock:
            return list(self.order)

    def release_all(self):
        for gate in self.gates.values():
            gate.set()


class Memory:
    def __init__(self, free):
        self.free = free

    def __call__(self):
        return self.free


def test_low_memory_holds_a_second_local_job_but_not_a_cloud_one_and_recovery_starts_it():
    memory, probe = Memory(4 * GIB), Probe()
    queue = JobQueue(workers=2, pending=8, memory=memory, recheck_s=0.01)
    try:
        probe.add(queue, "long")
        probe.add(queue, "second")
        probe.add(queue, "cloud", cloud=True)
        assert sorted(probe.started(2)) == ["cloud", "long"]
        assert sorted(probe.still()) == ["cloud", "long"]
        memory.free = MEMORY_PER_JOB
        assert probe.started(3)[-1] == "second"
    finally:
        probe.release_all()


def test_the_first_local_job_starts_at_any_memory():
    probe = Probe()
    queue = JobQueue(workers=2, pending=8, memory=Memory(0))
    try:
        probe.add(queue, "only")
        assert probe.started(1) == ["only"]
    finally:
        probe.release_all()


def test_a_freed_slot_starts_a_job_held_by_memory():
    probe = Probe()
    queue = JobQueue(workers=2, pending=8, memory=Memory(GIB), recheck_s=60)
    try:
        probe.add(queue, "first")
        probe.add(queue, "held", short=True)
        assert probe.still() == ["first"]
        probe.gates["first"].set()
        assert probe.started(2) == ["first", "held"]
    finally:
        probe.release_all()


def test_free_memory_is_shared_by_jobs_starting_together():
    memory, probe = Memory(GIB), Probe()
    queue = JobQueue(workers=4, pending=8, memory=memory, recheck_s=60)
    try:
        for name in ("running", "a", "b", "c"):
            probe.add(queue, name)
        assert probe.still() == ["running"]
        memory.free = 2 * MEMORY_PER_JOB + GIB
        probe.gates["running"].set()
        assert sorted(probe.started(3)) == ["a", "b", "running"]
        assert sorted(probe.still()) == ["a", "b", "running"]
    finally:
        probe.release_all()


def test_unknown_memory_does_not_hold_local_jobs():
    probe = Probe()
    queue = JobQueue(workers=2, pending=8, memory=lambda: None)
    try:
        probe.add(queue, "a")
        probe.add(queue, "b")
        assert probe.started(2) == ["a", "b"]
    finally:
        probe.release_all()


def test_cloud_lane_has_four_slots_and_does_not_take_local_jobs():
    probe = Probe()
    queue = JobQueue(workers=1, pending=8, memory=lambda: None)
    try:
        probe.add(queue, "local")
        probe.add(queue, "waiting")
        for n in range(5):
            probe.add(queue, f"cloud{n}", cloud=True)
        assert sorted(probe.started(5)) == ["cloud0", "cloud1", "cloud2", "cloud3", "local"]
        assert queue.has_room(cloud=True)  # a waiting place is still free
        probe.gates["cloud0"].set()
        assert probe.started(6)[-1] == "cloud4"
        assert "waiting" not in probe.still()
    finally:
        probe.release_all()


@pytest.fixture
def memory_files(monkeypatch):
    """/proc and the cgroup files are the external boundary: the test sets their contents."""
    files = {}
    original = Path.read_text

    def read(path, *args, **kwargs):
        name = str(path)
        if name.startswith(("/proc/", "/sys/fs/cgroup")):
            if name not in files:
                raise FileNotFoundError(name)
            return files[name]
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    return files


MEMINFO = "MemTotal:       32768000 kB\nMemFree:  1000 kB\nMemAvailable:   20971520 kB\n"


def test_available_memory_is_mem_available_without_cgroup_limits(memory_files):
    memory_files["/proc/meminfo"] = MEMINFO
    assert available_memory() == 20 * GIB


def test_available_memory_is_capped_by_cgroup_v2_limit_of_the_process_or_a_parent(memory_files):
    memory_files.update(
        {
            "/proc/meminfo": MEMINFO,
            "/proc/self/cgroup": "0::/jobs/worker\n",
            "/sys/fs/cgroup/memory.max": "max\n",
            "/sys/fs/cgroup/jobs/memory.max": str(8 * GIB),
            "/sys/fs/cgroup/jobs/memory.current": str(5 * GIB),
            "/sys/fs/cgroup/jobs/worker/memory.max": "max",
            "/sys/fs/cgroup/jobs/worker/memory.current": str(5 * GIB),
        }
    )
    assert available_memory() == 3 * GIB


def test_available_memory_is_capped_by_cgroup_v1_limit_in_a_container(memory_files):
    memory_files.update(
        {
            "/proc/meminfo": MEMINFO,
            "/proc/self/cgroup": "5:memory:/docker/job\n3:cpu,cpuacct:/docker/job\n",
            "/sys/fs/cgroup/memory/memory.limit_in_bytes": str(6 * GIB),
            "/sys/fs/cgroup/memory/memory.usage_in_bytes": str(GIB),
        }
    )
    assert available_memory() == 5 * GIB


def test_available_memory_is_unknown_without_proc(memory_files):
    assert available_memory() is None
