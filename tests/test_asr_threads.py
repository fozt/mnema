"""Recognition CPU budget: the OS limits (affinity, cgroup files) are stubbed."""

import os
from pathlib import Path

import pytest

from mnema.core import backends as b


@pytest.fixture(autouse=True)
def cpu_environment(monkeypatch):
    """OS limits are the external boundary: the test does not inherit its CI container quotas."""
    files = {}
    original = Path.read_text

    def read(path, *args, **kwargs):
        if str(path).startswith("/sys/fs/cgroup") or str(path) == "/proc/self/cgroup":
            return files.get(str(path), "")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.delenv("MNEMA_ASR_THREADS", raising=False)
    return files


@pytest.mark.parametrize(
    "setting, available, expected",
    [
        *[("auto", n, n) for n in (6, 32)],
        # unset: all but one core for the one running job
        *[(None, n, e) for n, e in ((1, 1), (2, 1), (6, 5))],
        ("2", 24, 2),
        ("32", 24, 24),  # an explicit number is capped at what is available
    ],
)
def test_asr_cpu_budget_respects_available_cpus(monkeypatch, setting, available, expected):
    monkeypatch.setattr(os, "sched_getaffinity", lambda _: set(range(available)), raising=False)
    if setting is None:
        monkeypatch.delenv("MNEMA_ASR_THREADS", raising=False)
    else:
        monkeypatch.setenv("MNEMA_ASR_THREADS", setting)
    assert b._asr_cpu_threads() == expected


@pytest.mark.parametrize(
    "value, expected",
    [
        *[(v, None) for v in ("0", "-1", "1.5", "all", "")],
        ("Auto", "auto"),  # a real .env typo: case and spaces do not matter
        (" 6 ", 6),
    ],
)
def test_asr_cpu_setting_has_one_validation_for_app_and_core(monkeypatch, value, expected):
    """One check for the app and the core; an invalid value falls back to the default rule."""
    from mnema.config import Settings
    from mnema.core.settings import RuntimeSettings

    monkeypatch.setenv("MNEMA_ASR_THREADS", value)
    for cls in (Settings, RuntimeSettings):
        assert cls().asr_threads == expected


def test_cpu_budget_falls_back_to_cpu_count_on_macos(monkeypatch):
    def no_affinity(_):
        raise AttributeError

    monkeypatch.setattr(os, "sched_getaffinity", no_affinity, raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: 10)
    monkeypatch.setenv("MNEMA_ASR_THREADS", "auto")
    assert b._asr_cpu_threads() == 10


def test_cpu_budget_respects_cgroup_v2_cpuset_and_parent_quota(monkeypatch, cpu_environment):
    monkeypatch.setenv("MNEMA_ASR_THREADS", "auto")
    monkeypatch.setattr(os, "sched_getaffinity", lambda _: set(range(32)), raising=False)
    cpu_environment.update(
        {
            "/proc/self/cgroup": "0::/jobs/worker",
            "/sys/fs/cgroup/cpu.max": "max 100000",
            "/sys/fs/cgroup/jobs/cpu.max": "250000 100000",  # 2.5 CPUs → three worker threads
            "/sys/fs/cgroup/jobs/worker/cpu.max": "400000 100000",
            "/sys/fs/cgroup/jobs/worker/cpuset.cpus.effective": "2-5,7",
        }
    )
    assert b._asr_cpu_threads() == 3
    cpu_environment["/sys/fs/cgroup/jobs/worker/cpuset.cpus.effective"] = "2,4"
    assert b._asr_cpu_threads() == 2


def test_cpu_budget_respects_cgroup_v1_and_container_namespace(monkeypatch, cpu_environment):
    monkeypatch.setenv("MNEMA_ASR_THREADS", "auto")
    monkeypatch.setattr(os, "sched_getaffinity", lambda _: set(range(32)), raising=False)
    cpu_environment.update(
        {
            "/proc/self/cgroup": "3:cpu,cpuacct:/docker/job\n4:cpuset:/docker/job",
            "/sys/fs/cgroup/cpu,cpuacct/cpu.cfs_quota_us": "100000",
            "/sys/fs/cgroup/cpu,cpuacct/cpu.cfs_period_us": "100000",
            "/sys/fs/cgroup/cpuset/cpuset.cpus": "0-3",
        }
    )
    assert b._asr_cpu_threads() == 1
    cpu_environment["/sys/fs/cgroup/cpu,cpuacct/cpu.cfs_quota_us"] = "-1"
    assert b._asr_cpu_threads() == 4
