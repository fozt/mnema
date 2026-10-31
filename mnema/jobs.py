"""API jobs: the model, path safety, running operations."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

from .core.base import MediaError, Progress
from .i18n import _, catalog, current, using
from .users import OWNER

logger = logging.getLogger(__name__)
SERVER_FAILURE = "Processing failed on the server. Details are in the server log"


def job_error_text(exc: BaseException) -> str:
    """The job error for the client: the MediaError text; anything else goes only to the log."""
    text = str(exc).strip() if isinstance(exc, MediaError) else ""
    if is_cancel_text(text):
        return _(CANCEL_MESSAGE)
    return text or _(SERVER_FAILURE)


NODE_OWNER = "node"  # jobs of the node token (and of an open node without a token); sees all jobs
# 409 on POST /api/jobs: the retry key belongs to a job this client cannot see
KEY_TAKEN = "The retry key is taken by another job"
KEY_REASON = "key_taken"
RESULT_TTL_DAYS = 7  # finished jobs and their result files (outputs/) live this many days


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


class JobErrorKind(StrEnum):
    """Why a job ended with an error: the client acts on it."""

    CANCELLED = "cancelled"
    RESTARTED = "restarted"  # the server restarted mid-job
    FAILED = "failed"


class JobView(BaseModel):
    """What a client sees of a job (GET /api/jobs/{id}): progress and result, without node
    paths, parameters or the retry key."""

    id: str
    status: JobStatus = JobStatus.PENDING
    progress: float | None = None
    message: str = ""
    result: dict | None = None
    error: str | None = None
    error_kind: JobErrorKind | None = None


class Job(JobView):
    op: str
    path: str = ""
    tracks: dict[str, str] | None = None
    key: str | None = None
    owner: str = NODE_OWNER  # id of the device that queued the job, or NODE_OWNER
    user: str = OWNER  # the server user of that device: the job runs as them
    params: dict = Field(default_factory=dict)
    lang: str = Field(default_factory=current)  # language of the client that queued the job
    # lifecycle stamps (unix time): how long a job waited and how long it ran
    created_at: float = Field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None


def sees(owner: str, job: Job) -> bool:
    """A device sees only its own jobs; the node token sees all."""
    return owner == NODE_OWNER or job.owner == owner


class DuplicateJobKey(Exception):
    """Another request already created a job with this retry key."""

    def __init__(self, job_id: str):
        super().__init__(job_id)
        self.job_id = job_id


def safe_path(root: Path, raw: str) -> Path:
    """The resolved path inside root, else MediaError (no directory traversal)."""
    resolved = Path(raw).expanduser().resolve()
    root = root.resolve()
    if not resolved.is_relative_to(root):
        raise MediaError(_("The path is outside the working folder: {path}", path=raw))
    return resolved


OperationFn = Callable[[Path, dict, "Progress"], dict]


# Raised in English inside the process (live/node_process compares the text);
# job_error_text renders it in the job's language.
CANCEL_MESSAGE = "The client cancelled the job."


def is_cancel_text(text: str | None) -> bool:
    """The job error is the cancel text, in either interface language."""
    return text is not None and text in (CANCEL_MESSAGE, catalog().get(CANCEL_MESSAGE))


def run_job(
    job: Job,
    fn: OperationFn,
    cancelled: Callable[[], bool] = lambda: False,
) -> None:
    """Run the operation, updating the job: progress, result or error.

    cancelled() is cooperative cancellation, checked on every progress callback: the job
    thread cannot be killed, but engines call on() regularly and an exception from it breaks
    their loop. Phases without callbacks (VAD and the like) run until the next callback."""
    with using(job.lang):
        _run_job(job, fn, cancelled)


def _run_job(job: Job, fn: OperationFn, cancelled: Callable[[], bool]) -> None:
    job.status = JobStatus.RUNNING
    if job.started_at is None:  # the HTTP queue may have set the status before run_job
        job.started_at = time.time()

    def on(fraction: float | None, message: str) -> None:
        if cancelled():
            raise MediaError(CANCEL_MESSAGE)
        if fraction is not None:
            job.progress = fraction
        if message:
            job.message = message

    try:
        job.result = fn(Path(job.path), job.params, on)
        if cancelled():
            raise MediaError(CANCEL_MESSAGE)
        # The result before the status: a poll that sees done already sees the result.
        job.finished_at = time.time()
        job.status = JobStatus.DONE
    except Exception as exc:
        job.error = job_error_text(exc)
        job.error_kind = (
            JobErrorKind.CANCELLED
            if cancelled() or is_cancel_text(job.error)
            else JobErrorKind.FAILED
        )
        logger.exception("job %s (%s) failed", job.id, job.op)
        job.finished_at = time.time()
        job.status = JobStatus.ERROR
