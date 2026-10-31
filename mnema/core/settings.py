"""Core compute settings; the app Settings inherits these fields and their validation."""

from __future__ import annotations

import math
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    Field,
    PositiveInt,
    TypeAdapter,
    ValidationError,
    ValidatorFunctionWrapHandler,
    field_validator,
)
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

ASRThreads = PositiveInt | Literal["auto"]
_THREADS_TYPE = TypeAdapter(ASRThreads)
_REQUEST_THREADS: ContextVar[ASRThreads | None] = ContextVar("asr_threads", default=None)


# Settings file lines read on every RuntimeSettings() rather than copied into the environment
# at startup, so an edit is seen by a running server.
FILE_SETTINGS = ("MNEMA_ASR_THREADS",)
_settings_file: Callable[[], Path] | None = None


def read_settings_file(path: Callable[[], Path]) -> None:
    """The app settings file (~/.config/mnema/.env, set by config.load_env): FILE_SETTINGS are
    read from it on every RuntimeSettings(); the process environment wins over it."""
    global _settings_file
    _settings_file = path


class _SettingsFile(PydanticBaseSettingsSource):
    """FILE_SETTINGS from the settings file; nothing when it is missing or unreadable."""

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        if _settings_file is None:
            return {}
        from dotenv import dotenv_values

        try:
            path = _settings_file()
            values = dotenv_values(path, interpolate=False) if path.is_file() else {}
        except (OSError, UnicodeError):
            return {}
        return {name: value for name in FILE_SETTINGS if (value := values.get(name))}


class RuntimeSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    # None: the cores are split between the running local jobs (job_threads)
    asr_threads: ASRThreads | None = Field(default=None, validation_alias="MNEMA_ASR_THREADS")
    # Log level of mnema.* (runner and its worker processes); independent of uvicorn's level.
    log_level: str = Field(default="INFO", validation_alias="MNEMA_LOG_LEVEL")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
            _SettingsFile(settings_cls),
        )

    @field_validator("asr_threads", mode="wrap")
    @classmethod
    def _tolerant_threads(
        cls, value: object, handler: ValidatorFunctionWrapHandler
    ) -> ASRThreads | None:
        """A typo in .env ("0", "abc") must not break the app and every command: fall back to
        the default rule and log a warning."""
        if isinstance(value, str):
            value = value.strip().lower()
        try:
            return handler(value)
        except ValidationError:
            import logging

            logging.getLogger("mnema").warning(
                "MNEMA_ASR_THREADS=%r is neither a number nor auto, splitting the cores", value
            )
            return None


@contextmanager
def asr_thread_budget(threads: ASRThreads | None) -> Iterator[None]:
    """Thread budget of one call; leaves the environment and concurrent jobs alone."""
    if threads is None:
        yield
        return
    token = _REQUEST_THREADS.set(_THREADS_TYPE.validate_python(threads))
    try:
        yield
    finally:
        _REQUEST_THREADS.reset(token)


def requested_asr_threads() -> ASRThreads:
    """The budget of this call; without one the job is the only running one (CLI, the Mac)."""
    value = _REQUEST_THREADS.get()
    return job_threads(1) if value is None else value


def job_threads(running_local: int) -> int:
    """Threads of a local job at its start: MNEMA_ASR_THREADS, otherwise all cores but one
    split between the running local jobs."""
    setting = RuntimeSettings().asr_threads
    if setting == "auto":
        return available_cpu_threads()
    if setting is not None:
        return setting
    return max(1, (available_cpu_threads() - 1) // max(1, running_local))


def default_workers() -> int:
    """Local job slots without MNEMA_JOB_WORKERS: a third of the cores."""
    return max(1, available_cpu_threads() // 3)


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def _cgroup_dirs() -> set[Path]:
    """The container namespace root and the process cgroups, including parent limits."""
    base = Path("/sys/fs/cgroup")
    roots = {base}
    paths = {base}
    for line in _read(Path("/proc/self/cgroup")).splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        _, controllers, member = parts
        names = set(controllers.split(","))
        groups = [base] if not controllers else []
        if "cpu" in names:
            groups += [base / name for name in ("cpu", "cpu,cpuacct", "cpuacct,cpu")]
        if "cpuset" in names:
            groups.append(base / "cpuset")
        for root in groups:
            roots.add(root)
            current = root / member.lstrip("/")
            while current.is_relative_to(root):
                paths.add(current)
                if current == root:
                    break
                current = current.parent
    return paths | roots


def _cpu_set(value: str) -> set[int]:
    cpus = set()
    for part in value.split(",") if value else []:
        bounds = part.split("-", 1)
        start = int(bounds[0])
        end = int(bounds[-1])
        cpus.update(range(start, end + 1))
    return cpus


def available_cpu_threads() -> int:
    """CPUs available to the process: affinity/cpuset and the cgroup v1/v2 quota, not the host."""
    try:
        affinity = set(os.sched_getaffinity(0))
    except (AttributeError, OSError):  # macOS
        affinity = set(range(os.cpu_count() or 1))
    quotas = []
    for folder in _cgroup_dirs():
        for filename in ("cpuset.cpus.effective", "cpuset.effective_cpus", "cpuset.cpus"):
            try:
                cpus = _cpu_set(_read(folder / filename))
            except ValueError:
                continue
            if cpus:
                affinity &= cpus
        quota = _read(folder / "cpu.max").split()
        if len(quota) != 2:
            quota = [_read(folder / name) for name in ("cpu.cfs_quota_us", "cpu.cfs_period_us")]
        try:
            budget, period = map(int, quota)
            if budget > 0 and period > 0:
                quotas.append(math.ceil(budget / period))
        except ValueError:  # v2 "max", v1 -1, or no controller
            continue
    return max(1, min([len(affinity), *quotas]))


def _number(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:  # v2 "max" or no controller
        return None


def _mem_available(meminfo: str) -> int | None:
    for line in meminfo.splitlines():
        name, _, value = line.partition(":")
        if name == "MemAvailable" and (fields := value.split()):
            return int(fields[0]) * 1024
    return None


def _stat(memory_stat: str, name: str) -> int:
    for line in memory_stat.splitlines():
        key, _, value = line.partition(" ")
        if key == name:
            return _number(value.strip()) or 0
    return 0


def _memory_dirs() -> set[Path]:
    """The memory cgroups of the process and their parents, v2 and v1, in or out of a container."""
    base = Path("/sys/fs/cgroup")
    dirs = set()
    for line in _read(Path("/proc/self/cgroup")).splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        _, controllers, member = parts
        if controllers and "memory" not in controllers.split(","):
            continue
        root = base / "memory" if controllers else base
        dirs.add(root)
        current = root / member.lstrip("/")
        while current != root and current.is_relative_to(root):
            dirs.add(current)
            current = current.parent
    return dirs


def available_memory() -> int | None:
    """MemAvailable capped by the free part of the cgroup v2/v1 memory limits, page cache the
    kernel can reclaim (inactive_file) counted as free like Docker does; None when unknown."""
    try:
        free = _mem_available(Path("/proc/meminfo").read_text())
    except (OSError, ValueError):
        return None
    if free is None:
        return None
    for folder in _memory_dirs():
        for limit_name, usage_name, cache_name in (
            ("memory.max", "memory.current", "inactive_file"),
            ("memory.limit_in_bytes", "memory.usage_in_bytes", "total_inactive_file"),
        ):
            limit = _number(_read(folder / limit_name))
            if limit is not None:
                usage = _number(_read(folder / usage_name)) or 0
                usage = max(0, usage - _stat(_read(folder / "memory.stat"), cache_name))
                free = min(free, max(0, limit - usage))
    return free
