"""Recording API client: the current job in a marker file, idempotent apply of its result.

The node owns processing: a server over HTTP (MnemaClient) or this Mac (LocalNode runs the
registry operation right here); the client sends the sources and saves the node's files. Each
file is written atomically, and a retry finishes the whole result after a disk failure. The
.mnema-processing marker is the work file: the request to the node and the user's own title,
which names the folder at the end.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from .. import ops
from ..client import KeyTaken, MnemaClient, ServerError, UploadMissing
from ..core import base as _base
from ..core.base import MediaError, Progress, noop
from ..i18n import _
from ..jobs import JobErrorKind
from ..library.disk import migrate_summary
from ..ops import recording_result
from ..sync.scan import FILES_LOCK
from . import meta, remarks
from .models import LiveResult
from .naming import chosen_title, free_name, part_label, started_iso, titled_path
from .persist import _atomic_bytes
from .setup import PROCESSING_MARK

_STOP_WAIT_S = 120.0  # cancelling on the node is cooperative: the model stops at its next answer
_RENEWALS = 1  # how many times a job is resubmitted on its own, with the same uploads
_JOB_LOST = "The server did not keep the processing result. Try again later"
_BUSY = (408, 429)  # the node is busy: retry later with the same uploads
_ALREADY = "This recording is already being processed"
logger = logging.getLogger(__name__)


class _Pending(BaseModel):
    key: str = Field(default_factory=lambda: uuid.uuid4().hex)
    node: str
    op: str
    params: dict[str, Any]
    inputs: dict[str, list[int]]
    uploaded: dict[str, str] = Field(default_factory=dict)
    job: str | None = None
    cancelled: bool = False
    target: str | None = None
    retries: int = Field(default=0, exclude=True)  # per run: a manual retry starts over


def _work(base: Path) -> dict | None:
    """The work file; None when there is none and nothing is being processed."""
    path = base / PROCESSING_MARK
    if path.is_symlink():
        raise MediaError(_("The processing marker cannot be a symbolic link"))
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise MediaError(_("Cannot read the processing state of the recording")) from exc
    if not isinstance(data, dict):
        raise MediaError(_("Cannot read the processing state of the recording"))
    return data


def _write_work(base: Path, data: dict) -> None:
    _atomic_bytes(base / PROCESSING_MARK, json.dumps(data, ensure_ascii=False, indent=2).encode())


def own_title(base: Path) -> str | None:
    """The user's own title of the running processing: it names the folder at the end."""
    with FILES_LOCK:
        title = (_work(base) or {}).get("title")
    return chosen_title(title, part_label(base.name)) if isinstance(title, str) else None


def set_own_title(base: Path, title: str | None, *, create: bool = False) -> bool:
    """Set the user's own title of the running processing; an empty one (naming.chosen_title)
    clears it. Without a work file (processing is over or has not reached this folder) nothing
    changes and the result is False; create means processing of this folder is starting now."""
    with FILES_LOCK:
        data = _work(base)
        if data is None:
            if not create:
                return False
            data = {}
        if chosen := chosen_title(title, part_label(base.name)):
            data["title"] = chosen
        else:
            data.pop("title", None)
        _write_work(base, data)
    return True


def drop_own_title(base: Path) -> None:
    """The folder got a name: the title set earlier no longer waits for processing."""
    with FILES_LOCK:
        try:
            data = _work(base)
        except MediaError:  # a foreign or broken marker is not ours to fix
            return
        if data and "title" in data:
            del data["title"]
            _write_work(base, data)


def drop_request(base: Path) -> None:
    """The request to the node is over (the node forgot it, the job failed, the summary was
    stopped): the next run starts it again. Only the user's own title stays in the work file,
    so the recording is as it was before the request."""
    with FILES_LOCK:
        try:
            title = own_title(base)
        except MediaError:  # a foreign or broken marker is not ours to fix
            return
        (base / PROCESSING_MARK).unlink(missing_ok=True)
        if title:
            _write_work(base, {"title": title})


def begin(base: Path) -> None:
    """Processing of the folder starts: the work file exists, so a title can be set before the
    request."""
    with FILES_LOCK:
        if _work(base) is None:
            _write_work(base, {})


def _save(base: Path, pending: _Pending) -> None:
    with FILES_LOCK:
        title = own_title(base)  # set during processing, so the copy on disk is newer
        data = pending.model_dump(mode="json") | ({"title": title} if title else {})
        _write_work(base, data)


def _load(base: Path) -> _Pending | None:
    data = _work(base)
    if not data or "node" not in data:  # no request yet, only the user's own title
        return None
    try:
        pending = _Pending.model_validate(data)
    except ValueError as exc:
        raise MediaError(_("Cannot read the processing state of the recording")) from exc
    return pending


def _known(base: Path, job: str) -> meta.Run | None:
    try:
        data = json.loads((base / meta.META_FILE).read_text(encoding="utf-8"))
        return next((meta.Run.model_validate(r) for r in data["runs"] if r.get("job") == job), None)
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        return None


def _complete(base: Path, on_base=None) -> Path:
    """Processing is over and its outcome is in the history. A title set after the result was
    applied names the folder here, under the same lock, and the work file goes away."""
    with FILES_LOCK:
        if (own := own_title(base)) and (target := titled_path(base, own)) != base:
            base.rename(target)
            base = target
            if on_base is not None:
                on_base(base)
        (base / PROCESSING_MARK).unlink(missing_ok=True)
    return base


def _stop(base: Path, pending: _Pending, client: MnemaClient | LocalNode, on: Progress) -> None:
    """Cancel the node's previous job and wait for it to end; a new one starts only after."""
    job = pending.job
    assert job is not None
    pending.cancelled = True
    _save(base, pending)  # an interrupted wait must not bring the cancelled job back
    client.cancel(job)
    deadline = time.monotonic() + _STOP_WAIT_S

    def waiting(fraction, message):
        if time.monotonic() >= deadline:
            raise MediaError(
                _(
                    "The previous processing on the server did not stop within 2 minutes; "
                    "try again later"
                )
            )
        on(None, _("Stopping the previous processing…"))

    client.wait_job(job, on=waiting)
    client.forget(job)


def _renew(base: Path, pending: _Pending, reason: str) -> None:
    """The node has no job for this client: submit a new one with a new key and the same
    uploads (the node names a lost upload on submit). Beyond _RENEWALS, fail with reason."""
    if pending.retries >= _RENEWALS:
        drop_request(base)
        raise MediaError(reason)
    pending.key, pending.job, pending.retries = uuid.uuid4().hex, None, pending.retries + 1
    _save(base, pending)


def _refused(exc: ServerError) -> bool:
    """The node refused before any work; busy (408, 429) is not a refusal: the request and
    uploads wait for a retry."""
    return 400 <= exc.status < 500 and exc.status not in _BUSY


def _submit(
    base: Path, pending: _Pending, client: MnemaClient | LocalNode, inputs, uploading
) -> None:
    """Upload the missing sources and submit the job. When the node names a lost upload, only
    that one is sent again, at most once per source; a key taken by another job gets a new key
    with the same uploads."""
    op, resends = pending.op, len(inputs)
    while True:
        for label, path in inputs.items():
            if label not in pending.uploaded:
                pending.uploaded[label] = client.upload_resumable(path, on=uploading)
                _save(base, pending)
        try:
            pending.job = client.submit(
                op,
                key=pending.key,
                params=pending.params,
                path=pending.uploaded["text"] if op == "recording_summary" else None,
                tracks=pending.uploaded if op == "recording" else None,
            )
        except KeyTaken as exc:
            _renew(base, pending, str(exc))
        except ServerError as exc:
            if _refused(exc):  # the recording stays as it was
                drop_request(base)
            raise
        except UploadMissing as exc:
            missing = exc.track or ("text" if op == "recording_summary" else None)
            if missing is not None and missing not in pending.uploaded:
                raise  # a track outside this request: do not upload audio in circles
            if missing is None:
                pending.uploaded.clear()
            else:
                del pending.uploaded[missing]
            _save(base, pending)
            if resends == 0:
                raise
            resends -= 1
        else:
            _save(base, pending)
            return


def _request(params: dict) -> dict:
    # The client derives partial from the folder on submit: an applied result changes it.
    return {key: value for key, value in params.items() if key != "partial"}


def _options(params: dict) -> dict:
    # An applied result updates these fields; any other change is a new user request.
    skip = {"name", "past", "partial"}
    return {key: value for key, value in params.items() if key not in skip}


def _has_text(path: Path) -> bool:
    return path.is_file() and bool(path.read_bytes().strip())


def _result(base: Path, run: meta.Run) -> LiveResult:
    def existing(name: str) -> Path | None:
        path = base / name
        return path if path.is_file() else None

    return LiveResult(
        base=base,
        transcript_txt=None
        if run.kind == "process" and run.status == "failed"
        else existing("transcript.txt"),
        summary_md=None
        if run.kind == "summary" and run.status == "failed"
        else existing("summary.txt"),
        translation_md=existing("translation.md"),
        recordings=[p for p in base.glob("*.wav") if p.is_file()],
        refined_labels=[label for label, track in run.tracks.items() if not track.error],
        silent_labels=[t for t in ("mic", "system") if remarks.track_silent(t) in run.notes],
        languages={label: track.lang for label, track in run.tracks.items() if track.lang},
        remarks=remarks.merged(run.notes),
        info=list(run.info),
    )


def _safe(base: Path, names) -> None:
    if base.is_symlink() or not base.is_dir():
        raise MediaError(_("The recording folder is unavailable or is a symbolic link"))
    for name in names:
        path = base / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise MediaError(_("Cannot replace a recording file: {name}", name=name))


def _apply(base: Path, pending: _Pending, answer: dict, on_base) -> tuple[Path, meta.Run]:
    try:
        result = recording_result(pending.op).model_validate(answer)
        run = meta.Run.model_validate(result.run)
    except ValidationError as exc:
        raise MediaError(_("The server returned an invalid processing result")) from exc
    files = result.files
    run.job = pending.job
    run.settings["server"] = meta.host(pending.node)
    proposed = result.folder
    with FILES_LOCK:
        _safe(base, {*files, meta.META_FILE, PROCESSING_MARK})
        migrate_summary(base, set(os.listdir(base)))  # the new summary replaces the old file
        own = own_title(base)  # set during processing, also after the request was sent
        if pending.target is None:
            if own:
                pending.target = titled_path(base, own).name
            # A title sent with the request named the folder on the node; one cleared later
            # does not.
            elif (
                proposed
                and base.name == pending.params["name"]
                and not pending.params.get("user_title")
            ):
                pending.target = free_name(base.with_name(proposed), own=base).name
            else:
                pending.target = base.name
            _save(base, pending)  # the chosen name survives a crash after rename
        for name, content in files.items():
            path = base / name
            if content is None:
                path.unlink(missing_ok=True)
            else:
                _atomic_bytes(path, content.encode("utf-8"))
        if pending.target != base.name and (own or base.name == pending.params["name"]):
            # The name was taken after it was chosen: take the next free one, as when choosing
            target = free_name(base.with_name(pending.target), own=base)
            if target.name != pending.target:
                pending.target = target.name
                _save(base, pending)
            if target.name != base.name:
                base.rename(target)
                base = target
                if on_base is not None:
                    on_base(base)
        # The history is the last atomic write: it signals a successful apply.
        data = meta._for_write(base)
        if data is None:
            raise MediaError(_("Could not save the processing history"))
        data["runs"].append(run.model_dump(mode="json"))
        _atomic_bytes(
            base / meta.META_FILE, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode()
        )
    return base, run


@contextmanager
def alone(base: Path):
    """One processing per folder on this computer: the app, the CLI and MCP are separate
    processes and do not see each other's jobs. The lock is on the folder itself, so a rename
    keeps it and process exit releases it. When taken, MediaError(_ALREADY); "Move to section"
    (bar/recordings.move_record) sees the same error and does not move the recording."""
    import fcntl

    try:
        fd = os.open(base, os.O_RDONLY)
    except OSError:  # no folder: processing itself reports it
        yield
        return
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise MediaError(_(_ALREADY)) from None
        yield
    finally:
        os.close(fd)


def process(
    base: Path,
    client: MnemaClient | LocalNode,
    op: str,
    params: dict,
    inputs: dict[str, Path],
    *,
    on: Progress = noop,
    on_base=None,
) -> LiveResult:
    """Upload or continue the current request; once applied, forget the result on the node.

    inputs: mic/system for recording, text for recording_summary. Interrupting through progress
    cancels the job; a network failure keeps key/id so the same node can continue it manually.
    A job the node no longer has for this client (it restarted or forgot it) is resubmitted once
    with a new key and the same uploads. A folder another process of this computer is
    processing is left alone with _ALREADY (LocalNode on a Mac, waiting for the server on Linux).
    """
    with alone(base):
        return _process(base, client, op, params, inputs, on=on, on_base=on_base)


def _process(
    base: Path,
    client: MnemaClient | LocalNode,
    op: str,
    params: dict,
    inputs: dict[str, Path],
    *,
    on: Progress,
    on_base,
) -> LiveResult:
    current = base
    pending = _load(base)
    try:
        if pending and pending.node != client._base_url:
            pending = None  # a new explicit run on another node; the old one expires by TTL
        signature = {
            label: [path.stat().st_size, path.stat().st_mtime_ns] for label, path in inputs.items()
        }
        if pending and pending.job and (known := _known(base, pending.job)):
            client.forget(pending.job)
            current = base = _complete(base, on_base)
            if (
                pending.op == op
                and _options(pending.params) == _options(params)
                and pending.inputs == signature
            ):
                return _result(base, known)
            pending = None
        if pending and pending.target == base.name:
            params = {**params, "name": pending.params["name"]}
        if pending and (
            pending.cancelled
            or pending.op != op
            or _request(pending.params) != params
            or pending.inputs != signature
        ):
            if pending.job:
                _stop(base, pending, client, on)
            pending = None
        if pending is None:
            if op == "recording":
                # With an earlier transcript, a failed track fails the run instead of half text.
                params = {**params, "partial": not _has_text(base / "transcript.txt")}
            pending = _Pending(node=client._base_url, op=op, params=params, inputs=signature)
            _save(base, pending)

        def progress(fraction, message):
            try:
                on(fraction, message)
            except BaseException:
                pending.cancelled = True
                _save(base, pending)
                try:
                    if pending.job is not None:
                        client.cancel(pending.job)
                except Exception:  # the user's cancel stays the reason even with no connection
                    logger.debug("could not send the cancel to the node", exc_info=True)
                raise

        def uploading(fraction, message):
            # Uploading is its own stage: its fractions are not part of the node's progress.
            progress(None, _("Uploading to the server…") if fraction is not None else message)

        while True:
            if pending.job is None:
                _submit(base, pending, client, inputs, uploading)
            job_id = pending.job
            assert job_id is not None
            job = client.wait_job(job_id, on=progress)
            if job is None:  # the node no longer has a job for this client
                _renew(base, pending, _(_JOB_LOST))
                continue
            if job["status"] == "error":
                client.forget(job_id)
                if job.get("error_kind") == JobErrorKind.RESTARTED:
                    _renew(base, pending, job.get("error") or _(_JOB_LOST))
                    continue
                drop_request(base)
                raise MediaError(job.get("error") or _("Processing on the server failed"))
            break
        current, run = _apply(base, pending, job["result"], on_base)
        client.forget(job_id)
        current = _complete(current, on_base)
        return _result(current, run)
    except BaseException as exc:
        # The rename may have finished before the metadata failed; the marker moves with it.
        if not current.exists() and pending and pending.target:
            moved = current.with_name(pending.target)
            if moved.is_dir():
                current = moved
        setattr(exc, "mnema_base", current)  # noqa: B010 - path after rename for the caller
        raise


def _params(base: Path, *, summary_lang: str | None) -> dict:
    started = meta.started(base)
    if started is None:
        started = started_iso(base)
        if started is not None:
            meta.set_started(base, started)
    # Unknown start: the node needs some value, but it is not written to meta
    return {"name": base.name, "started": started or meta.now(), "summary_lang": summary_lang}


class LocalNode:
    """The node on this Mac without a server: the MnemaClient methods process() uses, where a
    job is a registry operation (ops) run in this thread, as the server runs it in a job
    thread. Transcription still runs in a short-lived core.worker process. Jobs live in the
    instance: after a restart they are gone and process() submits the request again."""

    _base_url = ""  # no node: empty in the work file, no server in the history

    def __init__(self) -> None:
        self._jobs: dict[str, tuple[str, str | None, dict]] = {}

    def health(self, timeout: float = 2.0) -> dict:
        return {}

    def upload_resumable(self, path: Path | str, *, on: Progress = noop) -> str:
        return str(path)  # nothing to upload: the operation reads the recording in place

    def submit(
        self,
        op: str,
        *,
        key: str,
        params: dict | None = None,
        path: str | None = None,
        tracks: dict[str, str] | None = None,
    ) -> str:
        """Admit the job by the server's rule (ops.admit): a refusal is a ServerError with its
        code. A missing file (the folder was renamed between runs) is UploadMissing, so the
        path is given again."""
        spec = ops.get(op)
        if spec is None:
            raise ServerError(_("Unknown operation: {op}", op=op), 400)
        for label, source in (tracks if tracks is not None else {None: path}).items():
            if not (source and Path(source).is_file()):
                raise UploadMissing(label)
        values = dict(params or {})
        if tracks is not None:
            values["tracks"] = tracks
        try:
            with _base.processing_here():
                values = ops.admit(spec, values)
        except ops.Refused as exc:
            raise ServerError(str(exc), exc.status) from None
        job = uuid.uuid4().hex
        self._jobs[job] = (op, path, values)
        return job

    def wait_job(self, job: str, *, on: Progress = noop) -> dict | None:
        """Run the job; None when there is none. An operation failure (MediaError) becomes an
        error answer, as on the server; an exception raised by on itself (stop, language
        change) propagates as is."""
        task = self._jobs.pop(job, None)
        if task is None:
            return None
        op, path, values = task
        spec = ops.get(op)
        assert spec is not None
        raised: list[BaseException] = []

        def report(fraction: float | None, message: str) -> None:
            try:
                on(fraction, message)
            except BaseException as exc:
                raised.append(exc)
                raise

        try:
            with _base.processing_here():  # remarks name this Mac as the place
                if path is None:  # recording: the operation's work folder is a temporary one
                    with TemporaryDirectory(prefix="mnema-") as root:
                        result = spec.fn(Path(root), values, report)
                else:
                    result = spec.fn(Path(path), values, report)
        except BaseException as exc:
            if raised:
                raise raised[0] from None
            if not isinstance(exc, MediaError):
                raise
            return {"status": "error", "error": str(exc)}
        if raised:
            raise raised[0]
        return {"status": "done", "result": result}

    def cancel(self, job: str) -> None:
        pass  # the exception from on carries the stop

    def forget(self, job: str) -> None:
        pass


def _client(connection):
    """The processing node: the connected server; without one, this Mac, and on other systems a
    refusal (the recording is intact and can be processed later)."""
    if connection is not None:
        return MnemaClient(connection.url, token=connection.token)
    if _base.STANDALONE:
        return LocalNode()
    raise MediaError(_("Server not connected. The recording is saved; process it later"))


def recover_on_node(
    base,
    config,
    connection,
    *,
    start=None,
    end=None,
    retitle=None,
    replace_original=None,
    title=...,
    recording_error=None,
    on_transcript=None,
    on_lang=None,
    on_base=None,
    on=noop,
    tracks=None,
):
    """Prepare the audio on the client, run the node's pipeline, then apply the result.
    tracks: which tracks to transcribe (the "Process recording" window); the others are not
    uploaded and stay out of the shared mp3; None means all. title: the user's own title of
    this run (None for none) written to the work file of the processed folder or part; ...
    keeps what the work file already has."""
    from .setup import _mix_recording, finish_recovery, prepare_recovery

    client = _client(connection)  # the chosen node is fixed for the whole run
    status = client.health()
    if status is None:
        from .setup import finish_command

        command = finish_command(base)
        return LiveResult(
            base=base,
            recordings=list(base.glob("*.wav")),
            defer_command=command,
            remarks=[remarks.server_down(meta.host(connection.url) or connection.url, command)],
        ), ""
    if status.get("auth") is False:
        from ..install_setup import NEW_INVITE

        raise MediaError(NEW_INVITE)
    prepared, label, work = prepare_recovery(
        base,
        config,
        start=start,
        end=end,
        recording_error=recording_error,
        on_base=on_base,
    )

    def drop_part() -> None:
        if work != base:  # refused before work: drop the new part folder
            shutil.rmtree(work, ignore_errors=True)

    try:
        if title is ...:
            begin(work)
        else:
            set_own_title(work, title, create=True)
        paths = {label: writer.path for label, writer in prepared.writers.items()}
        if paths.keys() - {"mic", "system"}:
            raise MediaError(_("The recording folder needs mic.wav and/or system.wav tracks"))
        if tracks is not None:
            paths = {label: path for label, path in paths.items() if label in tracks}
            if not paths:
                raise MediaError(_("No recording track is selected"))
            # An unselected track is not "partly transcribed": the full recording goes as asked
            prepared.writers = {k: w for k, w in prepared.writers.items() if k in paths}
    except MediaError:
        drop_part()
        raise
    values = {
        **_params(work, summary_lang=config.summary_lang),
        "asr": config.asr,
        "lang": config.lang,
        "summary": config.summary,
        "part": prepared.part,
        "recording_error": recording_error,
        "retitle": retitle,
        "user_title": own_title(work),
    }
    if config.translate:
        values["translate"] = config.translate
    try:
        result = process(work, client, "recording", values, paths, on=on, on_base=on_base)
    except ServerError as exc:
        if _refused(exc):
            drop_part()
            setattr(exc, "mnema_base", base)  # noqa: B010 - the part is gone
        raise
    prepared.base = result.base or work
    prepared.info = list(dict.fromkeys([*prepared.info, *result.info]))
    mixed: list[remarks.Remark] = []
    if config.mix_audio:
        chosen = [p for p in result.recordings if p.stem in paths]
        mix = _mix_recording(chosen, prepared.base, mixed, silent=result.silent_labels, on=on)
        if mix is not None:
            result.recordings.append(mix)
    result.remarks += mixed
    result = finish_recovery(
        base,
        work,
        label,
        prepared,
        result,
        replace_original=replace_original,
        on_base=on_base,
    )
    with FILES_LOCK:  # what the client learned after the result goes into the same run
        meta.amend(result.base or prepared.base, notes=mixed, info=result.info)
    if on_lang is not None:
        on_lang(result.languages)
    if result.transcript_txt is not None and on_transcript is not None:
        on_transcript(result.transcript_txt)
    full = result.transcript_txt.read_text(encoding="utf-8") if result.transcript_txt else ""
    return result, full


def summarize_on_node(base, connection, *, lang=None, on=noop, on_base=None):
    """Redoing the summary and title sends the finished transcript, without ASR."""
    past = meta.last(base)
    values = {
        **_params(base, summary_lang=lang),
        "past": past.model_dump(mode="json") if past else None,
    }
    return process(
        base,
        _client(connection),
        "recording_summary",
        values,
        {"text": base / "transcript.txt"},
        on=on,
        on_base=on_base,
    )


def finalize_on_node(prepared, tracks, *, connection, **kwargs):
    """Recording stopped and sources closed: the node gets the WAV, not the client's live
    draft."""
    if not prepared.config.record:
        return finalize_text_on_node(prepared, tracks, connection=connection, **kwargs)
    from .persist import save

    # Live segments already received from the node: save the draft before the final pass.
    with FILES_LOCK:
        saved = save(tracks, base=prepared.base, language=prepared.lang)
    result, full = recover_on_node(
        prepared.base,
        prepared.config,
        connection,
        recording_error=prepared.recording_error,
        **kwargs,
    )
    prepared.base = result.base or prepared.base
    run = meta.last(prepared.base)
    if saved.transcript_txt is not None and (
        result.defer_command or (run is not None and run.status == "failed")
    ):
        draft = prepared.base / "transcript.txt"
        if draft.is_file():
            result.transcript_txt = draft
            notice = remarks.Remark(
                step=remarks.Step.ASR,
                cause=_("The final transcription did not finish; the live draft is saved"),
                hint=_("Process the recording again"),
            )
            result.remarks.append(notice)
            if run is not None:
                with FILES_LOCK:
                    meta.amend(prepared.base, notes=[notice])
            full = draft.read_text(encoding="utf-8")
    return result, full


def finalize_text_on_node(
    prepared,
    tracks,
    *,
    connection,
    retitle=None,
    on=noop,
    on_base=None,
    on_transcript=None,
    on_lang=None,
):
    """--no-record: save the text already received from the node and finish it there."""
    from .persist import save

    with FILES_LOCK:
        saved = save(tracks, base=prepared.base, language=prepared.lang)
    if saved.transcript_txt is None:
        saved.base = prepared.base
        return saved, ""
    values = {
        **_params(prepared.base, summary_lang=prepared.config.summary_lang),
        "finalize": True,
        "summary": prepared.config.summary,
        "lang": prepared.lang,
        "translate": prepared.config.translate,
        "retitle": retitle,
        "user_title": own_title(prepared.base),
        "recording_error": prepared.recording_error,
    }
    result = process(
        prepared.base,
        _client(connection),
        "recording_summary",
        values,
        {"text": saved.transcript_txt},
        on=on,
        on_base=on_base,
    )
    prepared.base = result.base or prepared.base
    if result.transcript_txt is not None and on_transcript is not None:
        on_transcript(result.transcript_txt)
    return result, result.transcript_txt.read_text(
        encoding="utf-8"
    ) if result.transcript_txt else ""
