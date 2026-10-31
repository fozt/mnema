"""Job REST routes: create, status, cancel, delete."""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import cast

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import core as mc
from ..core.backends import is_cloud
from ..core.settings import asr_thread_budget, job_threads
from ..i18n import _, current, using
from ..jobs import (
    CANCEL_MESSAGE,
    KEY_REASON,
    KEY_TAKEN,
    NODE_OWNER,
    RESULT_TTL_DAYS,
    DuplicateJobKey,
    Job,
    JobErrorKind,
    JobStatus,
    JobView,
    is_cancel_text,
    job_error_text,
    run_job,
    safe_path,
    sees,
)
from ..llm.config import LLMSettings
from ..ops import TEXT_SUFFIXES, InputKind, Refused, admit, no_model
from ..users import OWNER, as_user
from .models import JobRequest
from .uploads import GONE, UploadBusy

TRACK_UPLOADED = "A track must be an uploaded file"

_log = logging.getLogger(__name__)


def _relocate_file_output(root: Path, job_id: str, result: object) -> None:
    """A FILE result (compress/mp3) is written next to the input, in the ephemeral uploads/
    that is removed before the client calls /api/download (a silent 404). Move it to the
    durable root/outputs/<job-id>/ and rewrite the path in result (the object that becomes
    job.result).

    Called inside the op wrapper, before run_job sets done: a client that sees done already
    sees the final path."""
    if not isinstance(result, dict):
        return
    data = cast("dict[str, object]", result)
    raw = data.get("path")
    if not isinstance(raw, str):
        return
    src = Path(raw)
    if not src.is_file():
        raise mc.MediaError(_("The operation did not save the result file."))
    dest_dir = root / "outputs" / job_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    shutil.move(str(src), str(dest))
    data["path"] = str(dest)


SHORT_JOB_MAX_S = 300.0  # short recordings get their own queue slot, never wait for a long one
_VIEW = set(JobView.model_fields)


def _job_of(app: FastAPI, request: Request, job_id: str) -> Job:
    """The job for the caller; someone else's job looks like a missing one: 404."""
    job = app.state.store.get(job_id)
    if job is None or not sees(request.state.owner, job):
        raise HTTPException(404, _("Job not found"))
    return job


def _same_key(owner: str, previous: Job | None) -> dict | JSONResponse:
    """The same retry key is the same job; the key of someone else's job is refused without
    its id."""
    if previous is None or not sees(owner, previous):
        return JSONResponse(status_code=409, content={"detail": _(KEY_TAKEN), "reason": KEY_REASON})
    return {"id": previous.id}


def _device_inputs(app: FastAPI, request: Request, req: JobRequest) -> JSONResponse | None:
    """A device gives only uploads of its user and does not choose the transcription model;
    the node token any path inside the root. Checked before admission and ffprobe."""
    if request.state.owner == NODE_OWNER:
        return None
    if req.op == "transcribe" and req.params.get("model") is not None:
        raise HTTPException(422, _("The server chooses the recognition model"))
    inputs = req.tracks or {None: req.path}
    for label, raw in inputs.items():
        try:
            path = safe_path(app.state.root, raw)
        except mc.MediaError as exc:
            raise HTTPException(403, str(exc)) from exc
        if app.state.uploads.directory(path) is None:
            if label is not None:
                raise HTTPException(403, _(TRACK_UPLOADED))
            raise HTTPException(403, _("The input must be an uploaded file"))
        if app.state.uploads.owner(path) != request.state.user:  # looks like a deleted one
            detail = {"detail": _(GONE)}
            if label is not None:
                detail["track"] = label
            return JSONResponse(status_code=404, content=detail)
    return None


def _is_short(root: Path, path: str | None) -> bool:
    """The job's media is at most SHORT_JOB_MAX_S long (ffprobe, ~0.1 s); unreadable is long."""
    if not path:
        return False
    try:
        duration = mc.probe(safe_path(root, path)).duration
    except Exception:  # noqa: BLE001 - the job itself deals with unreadable input
        return False
    return 0.0 < (duration or 0.0) <= SHORT_JOB_MAX_S


def _remove_outputs(app: FastAPI, job_id: str) -> None:
    outputs = app.state.root / "outputs"
    folder = outputs / job_id
    if outputs.is_symlink() or folder.parent != outputs:
        return
    if folder.is_symlink():
        folder.unlink(missing_ok=True)
    else:
        shutil.rmtree(folder, ignore_errors=True)


def sweep(app: FastAPI, now: float) -> None:
    """Once an hour, on a job request, remove old results and forgotten uploads."""
    from ..server import _sweep_outputs

    with app.state.job_queue.lock:
        last = getattr(app.state, "last_sweep", None)
        if last is not None and now - last < 3600:
            return
        for job_id in app.state.store.sweep_finished(now - RESULT_TTL_DAYS * 86400):
            _remove_outputs(app, job_id)
            app.state.cancels.discard(job_id)
        outputs = app.state.root / "outputs"
        if not outputs.is_symlink():
            _sweep_outputs(outputs)
        app.state.uploads.sweep()
        app.state.last_sweep = now


def register_job_routes(app: FastAPI) -> None:
    @app.post("/api/jobs", response_model=None)
    def create_job(req: JobRequest, request: Request) -> dict | JSONResponse:
        sweep(app, time.time())
        owner = request.state.owner
        with app.state.job_queue.lock:
            if req.key is not None and (previous := app.state.store.find_by_key(req.key)):
                return _same_key(owner, previous)
        spec = app.state.operations.get(req.op)
        if spec is None:
            raise HTTPException(400, _("Unknown operation: {op}", op=req.op))
        if spec.requires_model and not LLMSettings().cloud:
            raise HTTPException(400, no_model())
        if (spec.input_kind == InputKind.TRACKS) != (req.tracks is not None):
            raise HTTPException(422, _("The operation takes another kind of input"))
        if (refused := _device_inputs(app, request, req)) is not None:
            return refused
        tracks = None
        try:
            if req.tracks is not None:
                tracks = {}
                for label, raw in req.tracks.items():
                    path = safe_path(app.state.root, raw)
                    if not path.is_relative_to(app.state.root / "uploads") or (
                        app.state.uploads.directory(path) is None
                    ):
                        raise mc.MediaError(_(TRACK_UPLOADED))
                    tracks[label] = str(path)
                if len(set(tracks.values())) != len(tracks):
                    raise HTTPException(422, _("Track paths must differ"))
            else:
                safe_path(app.state.root, req.path)
        except mc.MediaError as exc:
            raise HTTPException(403, str(exc)) from exc
        # Admission before the job exists, the same rule as processing on the Mac (ops.admit):
        # parameters by schema, recognition, the summary model; a refusal keeps its code.
        try:
            values = {**req.params, "tracks": tracks} if tracks is not None else req.params
            params = admit(spec, values)
        except Refused as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        # ready text gets its own queue lane, without ffprobe
        text = spec.input_kind == InputKind.TEXT and Path(req.path).suffix.lower() in TEXT_SUFFIXES
        inputs = tracks or {None: req.path}
        # cloud recognition: its own queue lane, no engine in memory
        cloud = not text and is_cloud(params.get("asr") or params.get("backend"))
        short = (
            not text
            and not cloud
            and all(_is_short(app.state.root, path) for path in inputs.values())
        )
        # One lock: no other request takes the slot between the room check and the submit.
        with app.state.job_queue.lock:
            if req.key is not None and (previous := app.state.store.find_by_key(req.key)):
                return _same_key(owner, previous)
            if not app.state.job_queue.has_room(text=text, cloud=cloud, user=request.state.user):
                raise HTTPException(
                    429, _("The server is busy: the queue is full. Try again later")
                )
            claimed: list[str] = []
            try:
                for label, path in inputs.items():
                    try:
                        app.state.uploads.claim(path)
                    except (UploadBusy, FileNotFoundError) as exc:
                        for held in claimed:
                            app.state.uploads.release(held, delete=False)
                        status = 409 if isinstance(exc, UploadBusy) else 404
                        detail = {"detail": str(exc)}
                        if label is not None:
                            detail["track"] = label
                        return JSONResponse(status_code=status, content=detail)
                    claimed.append(path)
                return _enqueue_job(
                    req,
                    spec,
                    params,
                    tracks=tracks,
                    short=short,
                    text=text,
                    cloud=cloud,
                    owner=owner,
                    user=request.state.user,
                )
            except DuplicateJobKey as exc:
                for held in claimed:
                    app.state.uploads.release(held, delete=False)
                return _same_key(owner, app.state.store.get(exc.job_id))
            except Exception:
                for held in claimed:
                    app.state.uploads.release(held, delete=False)
                raise

    def _enqueue_job(
        req, spec, params, *, tracks, short: bool, text: bool, cloud: bool, owner: str, user: str
    ) -> dict:
        job = app.state.store.create(
            op=req.op,
            path=str(app.state.root) if tracks is not None else req.path,
            params=params,
            tracks=tracks,
            key=req.key,
            lang=current(),
        )
        job.owner, job.user = owner, user  # saved by the next update, under the same queue lock
        job.message = _("Queued")
        app.state.store.update(job)
        app.state.cancels.discard(job.id)

        # FILE op: the result moves to durable outputs/ inside the op wrapper, before run_job
        # sets done (otherwise a poll could see the ephemeral path).
        op_fn = spec.fn
        if spec.output_kind == "file":

            def _relocating_fn(path, params, on, _base=spec.fn, _root=app.state.root, _jid=job.id):
                result = _base(path, params, on)
                try:
                    _relocate_file_output(_root, _jid, result)
                except Exception as exc:
                    raw = result.get("path") if isinstance(result, dict) else None
                    src = Path(raw).resolve() if isinstance(raw, str) else None
                    durable = (
                        src is not None
                        and src.is_file()
                        and src.is_relative_to(_root)
                        and not src.is_relative_to(_root / "uploads")
                    )
                    if not durable:
                        raise mc.MediaError(
                            _("Could not save the result file: {error}", error=exc)
                        ) from exc
                    _log.warning("moving the FILE result failed; kept the original %s", src)
                return result

            op_fn = _relocating_fn

        def _run_and_persist(j=job, fn=op_fn) -> None:
            def _cleanup_inputs() -> None:
                # An uploaded file belongs to one job; the user's recordings outside uploads/
                # stay in place.
                for path in j.tracks.values() if j.tracks else [j.path]:
                    app.state.uploads.release(path)

            try:
                if j.id in app.state.cancels:
                    raise mc.MediaError(CANCEL_MESSAGE)
                j.status = JobStatus.RUNNING
                j.started_at = time.time()
                j.message = _("Starting processing")
                app.state.store.update(j)
                _log.info("job %s started: op=%s user=%s", j.id, j.op, j.user)

                def _finishing(p, params, on, _f=fn):
                    # the ephemeral input goes before run_job sets done: a client that sees
                    # done must not find temporary folders; the finally below covers errors
                    if j.id in app.state.cancels:
                        raise mc.MediaError(CANCEL_MESSAGE)
                    result = _f(p, params, on)
                    _cleanup_inputs()
                    return result

                run_job(j, _finishing, cancelled=lambda: j.id in app.state.cancels)
                app.state.store.update(j)  # the final status/result/error into SQLite
            except Exception as exc:  # a failure before run_job also ends the job
                j.error = job_error_text(exc)
                j.error_kind = (
                    JobErrorKind.CANCELLED
                    if j.id in app.state.cancels or is_cancel_text(j.error)
                    else JobErrorKind.FAILED
                )
                j.finished_at = time.time()
                j.status = JobStatus.ERROR
                _log.exception("preparing job %s failed", j.id)
                app.state.store.update(j)
            finally:
                _log.info("job %s ended: status=%s user=%s", j.id, j.status, j.user)
                _cleanup_inputs()  # idempotent: on success it already ran before done
                with app.state.job_queue.lock:
                    app.state.cancels.discard(j.id)

        def _fail_before_run(error: str) -> None:
            with app.state.job_queue.lock:
                job.error = error
                job.error_kind = (
                    JobErrorKind.CANCELLED if is_cancel_text(error) else JobErrorKind.FAILED
                )
                job.finished_at = time.time()
                job.status = JobStatus.ERROR
                app.state.store.update(job)
                for path in job.tracks.values() if job.tracks else [job.path]:
                    app.state.uploads.release(path)
                app.state.cancels.discard(job.id)
            _log.info("job %s ended before start: %s user=%s", job.id, error, job.user)

        def _execute() -> None:
            # a local job gets its share of the cores as it starts
            threads = None if text or cloud else job_threads(app.state.job_queue.running_local)
            with using(job.lang), as_user(job.user), asr_thread_budget(threads):
                _execute_job()

        def _cancel_pending() -> None:
            with using(job.lang):  # the cancelling request may speak another language
                _fail_before_run(_(CANCEL_MESSAGE))

        def _execute_job() -> None:
            if job.id in app.state.cancels:
                _fail_before_run(_(CANCEL_MESSAGE))
                return
            if job.user != OWNER and not _active(job.user):
                _fail_before_run(_("The user was removed"))
                return
            try:
                _run_and_persist()
            except Exception as exc:
                _log.exception("preparing job %s failed", job.id)
                # An error before run_job must not leave the job PENDING forever.
                if job.status not in (JobStatus.DONE, JobStatus.ERROR):
                    _fail_before_run(job_error_text(exc))

        try:
            app.state.job_queue.submit(
                job.id,
                _execute,
                _cancel_pending,
                short=short,
                text=text,
                cloud=cloud,
                user=job.user,
            )
        except Exception as exc:
            _fail_before_run(job_error_text(exc))
            raise
        _log.info("job %s queued: op=%s user=%s", job.id, job.op, job.user)
        return {"id": job.id}

    def _active(user: str) -> bool:
        found = app.state.devices.user(user)
        return found is not None and not found.removed

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str, request: Request) -> dict:
        """Cooperative cancellation: the flag is checked on every progress callback of the job.

        The ASR process also checks it while loading and during VAD through the parent's
        callback. A finished job is a no-op (ok=False)."""
        with app.state.job_queue.lock:
            job = _job_of(app, request, job_id)
            if job.status in (JobStatus.DONE, JobStatus.ERROR):
                return {"ok": False, "status": job.status}
            app.state.cancels.add(job_id)
            app.state.job_queue.cancel_pending(job_id)
            return {"ok": True, "status": job.status}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str, request: Request) -> dict:
        sweep(app, time.time())
        return _job_of(app, request, job_id).model_dump(include=_VIEW)

    @app.delete("/api/jobs/{job_id}")
    def delete_job(job_id: str, request: Request) -> dict:
        with app.state.job_queue.lock:
            job = _job_of(app, request, job_id)
            if job.status in (JobStatus.PENDING, JobStatus.RUNNING):
                raise HTTPException(409, _("The job has not finished yet"))
            app.state.store.delete(job_id)
            _remove_outputs(app, job_id)
            app.state.cancels.discard(job_id)
            return {"ok": True}
