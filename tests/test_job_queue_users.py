"""The queue takes waiting jobs of different users in turn."""

import threading
import time

import pytest

from mnema.job_queue import JobQueue


class Probe:
    """Jobs that record their start order and finish when released."""

    def __init__(self):
        self.order = []
        self.gates = {}
        self.lock = threading.Lock()

    def job(self, name):
        gate = self.gates[name] = threading.Event()

        def run():
            with self.lock:
                self.order.append(name)
            gate.wait(5)

        return run

    def add(self, queue, name, **kwargs):
        queue.submit(name, self.job(name), lambda: None, **kwargs)

    def release(self, name):
        self.gates[name].set()

    def drain(self, expected):
        for step, name in enumerate(expected, start=1):
            assert self.started(step)[-1] == name
            self.release(name)

    def started(self, count):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with self.lock:
                if len(self.order) >= count:
                    return list(self.order)
            time.sleep(0.002)
        pytest.fail(f"only {self.order} started")


def test_waiting_jobs_of_other_users_go_between_one_users_jobs():
    queue, probe = JobQueue(workers=1, pending=16), Probe()
    for name in ("a1", "a2", "a3"):
        probe.add(queue, name, user="a")
    probe.add(queue, "b1", user="b")
    probe.drain(["a1", "b1", "a2", "a3"])


def test_users_who_never_ran_go_first_and_ties_are_fifo():
    queue, probe = JobQueue(workers=1, pending=16), Probe()
    probe.add(queue, "x", user="a")
    for name, user in (("a2", "a"), ("c1", "c"), ("b1", "b")):
        probe.add(queue, name, user=user)
    probe.drain(["x", "c1", "b1", "a2"])


def test_jobs_without_a_user_count_as_one_user():
    queue, probe = JobQueue(workers=1, pending=16), Probe()
    probe.add(queue, "n1")
    probe.add(queue, "n2")
    probe.add(queue, "a1", user="a")
    probe.add(queue, "n3")
    probe.drain(["n1", "a1", "n2", "n3"])


def test_one_user_may_fill_the_whole_queue_alone():
    queue, probe = JobQueue(workers=1, pending=16), Probe()
    probe.add(queue, "run", user="a")
    for i in range(15):
        assert queue.has_room(user="a")
        probe.add(queue, f"w{i}", user="a")
    assert queue.has_room(user="a")
    probe.add(queue, "w15", user="a")
    assert not queue.has_room(user="a")
    for gate in probe.gates.values():
        gate.set()


def test_a_user_with_others_present_is_limited_to_half_of_the_queue():
    queue, probe = JobQueue(workers=1, pending=16), Probe()
    probe.add(queue, "b-run", user="b")
    for i in range(8):
        assert queue.has_room(user="a")
        probe.add(queue, f"a{i}", user="a")
    assert not queue.has_room(user="a")
    assert queue.has_room(user="b")
    for gate in probe.gates.values():
        gate.set()


def test_the_half_limit_counts_a_waiting_job_of_another_user_too():
    queue, probe = JobQueue(workers=1, pending=4), Probe()
    probe.add(queue, "a-run", user="a")
    probe.add(queue, "b1", user="b")
    probe.add(queue, "a1", user="a")
    probe.add(queue, "a2", user="a")
    assert not queue.has_room(user="a")
    assert queue.has_room(user="b")
    for gate in probe.gates.values():
        gate.set()


def test_a_short_job_does_not_wait_for_a_long_one_of_any_user():
    queue, probe = JobQueue(workers=1, pending=16, memory=lambda: None), Probe()
    probe.add(queue, "long", user="a")
    probe.add(queue, "voice", user="b", short=True)
    assert probe.started(2) == ["long", "voice"]
    for gate in probe.gates.values():
        gate.set()


def test_the_text_lane_has_two_slots_of_its_own():
    queue, probe = JobQueue(workers=1, pending=16), Probe()
    probe.add(queue, "long", user="a")
    probe.add(queue, "t1", user="a", text=True)
    probe.add(queue, "t2", user="b", text=True)
    assert probe.started(3) == ["long", "t1", "t2"]
    probe.add(queue, "t3", user="b", text=True)
    probe.add(queue, "t4", user="a", text=True)
    probe.release("t2")
    assert probe.started(4)[-1] == "t4"
    for gate in probe.gates.values():
        gate.set()
