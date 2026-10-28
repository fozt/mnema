"""Repository layer: Protocol interfaces of the stores and their factory.

server.py depends on the interfaces, not on the store; the implementation is SQLite
(mnema.store).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .jobs import Job
from .store import SqliteJobStore


class JobRepo(Protocol):
    def create(
        self,
        *,
        op: str,
        path: str,
        params: dict,
        tracks: dict[str, str] | None = None,
        key: str | None = None,
        lang: str | None = None,
    ) -> Job: ...
    def get(self, job_id: str) -> Job | None: ...
    def find_by_key(self, key: str) -> Job | None: ...
    def delete(self, job_id: str) -> bool: ...
    def update(self, job: Job) -> None: ...
    def sweep_running(self, error: str) -> int: ...
    def sweep_finished(self, before: float) -> list[str]: ...


@dataclass(frozen=True)
class Repos:
    """The application's repositories, behind interfaces."""

    jobs: JobRepo


def create_repos(mnema_dir: Path) -> Repos:
    """The application's repositories: jobs live in jobs.db of mnema_dir."""
    return Repos(jobs=SqliteJobStore(mnema_dir / "jobs.db"))
