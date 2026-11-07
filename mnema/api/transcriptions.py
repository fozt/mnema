"""The OpenAI-compatible transcription route, for third-party services.

A door over the transcribe operation: the upload, the queue, the job's owner and its cancel are
the job API's own; here is only the OpenAI contract around them. The apps keep their recording
jobs, with resumable uploads, polling and summaries.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping
from pathlib import Path

import anyio
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData, UploadFile
from starlette.exceptions import HTTPException as FormRefused
from starlette.formparsers import MultiPartException

from .. import core as mc
from ..core.models import Transcript
from ..i18n import _
from ..jobs import SERVER_FAILURE, Job, JobStatus
from .files import CHUNK_STALL_S, save_upload
from .jobs import stop_job
from .models import JobRequest

OPENAI_PREFIX = "/api/v1/"
TRANSCRIPTIONS = f"{OPENAI_PREFIX}audio/transcriptions"
# whisper-1 only for clients with the name built in. An allow-list on purpose: a cloud model name
# never gets through, so a third-party service cannot spend the OpenAI key of the token's user.
MODELS = ("mnema", "whisper-1")
MAX_REQUEST_BYTES = 1024**3  # 9 h of 16 kHz WAV, 18 h of 128 kbps MP3; on disk twice while saved
TIMEOUT_S = 3600.0  # queue and recognition together; a longer recording belongs in the apps
POLL_S = 0.25
RETRY_AFTER_S = 30
_FIELDS_LIMIT = 32  # the contract has about ten
_FORMATS = frozenset({"json", "text", "srt", "vtt", "verbose_json"})
_GRANULARITIES = frozenset({"segment", "word"})
_OVER = (JobStatus.DONE, JobStatus.ERROR)

BAD_TOKEN = "The token is not valid"
BAD_FORM = "The form could not be read"
TOO_LARGE = "The request is over the {limit} GiB limit"
NO_FILE = "Attach the audio as the file field"
NO_MODEL_FIELD = "The model field is needed"
UNKNOWN_MODEL = "Unknown model: {model}. Available: {models}"
UNKNOWN_FORMAT = "Unknown response format: {format}"
UNKNOWN_GRANULARITY = "Unknown timestamp granularity: {value}"
ONLY_VERBOSE = "Timestamp granularities need the verbose_json response format"
NO_STREAM = "Streaming is not supported"
NO_AUDIO = "The audio could not be read"
TOO_LONG = "The transcription took too long and was cancelled"

_log = logging.getLogger(__name__)


def openai_error(
    status: int,
    message: str,
    *,
    kind: str = "invalid_request_error",
    code: str | None = None,
    param: str | None = None,
    headers: Mapping[str, str] | None = None,
    background: BackgroundTask | None = None,
) -> JSONResponse:
    error = {"message": message, "type": kind, "param": param, "code": code}
    return JSONResponse({"error": error}, status, headers, background=background)


class _Failure(Exception):
    """The request ends with this OpenAI error."""

    def __init__(self, status: int, message: str, **details) -> None:
        super().__init__(message)
        self.status, self.message, self.details = status, message, details

    def response(self) -> JSONResponse:
        return openai_error(self.status, self.message, **self.details)


class _ClientGone(Exception):
    pass


def _busy(message: str) -> _Failure:
    return _Failure(
        429,
        message,
        kind="rate_limit_error",
        code="rate_limit_exceeded",
        headers={"Retry-After": str(RETRY_AFTER_S)},
    )


def _queue_full() -> _Failure:
    return _busy(_("The server is busy: the queue is full. Try again later"))


def _too_large() -> _Failure:
    limit = MAX_REQUEST_BYTES // 1024**3
    return _Failure(413, _(TOO_LARGE, limit=limit), code="file_too_large", param="file")


def _server_failure(message: str, code: str = "server_error") -> _Failure:
    return _Failure(500, message, kind="server_error", code=code)


async def _read_form(request: Request) -> FormData:
    """The multipart form with the file spooled to disk; the limit holds for a body without
    Content-Length too, and a client that leaves or goes quiet ends the reading."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_REQUEST_BYTES:
        raise _too_large()
    seen = 0
    stop: Exception | None = None

    async def receive():
        nonlocal seen, stop
        message = None
        with anyio.move_on_after(CHUNK_STALL_S):
            message = await request.receive()
        seen += len(message.get("body", b"")) if message else 0
        if message is None or message["type"] == "http.disconnect":
            stop = _ClientGone()
        elif seen > MAX_REQUEST_BYTES:
            stop = _too_large()
        if stop is not None:
            raise MultiPartException("stopped")  # the parser closes its temporary file on this
        return message

    try:
        return await Request(request.scope, receive).form(max_files=1, max_fields=_FIELDS_LIMIT)
    except FormRefused as exc:  # how Request.form reports the parser, our stop included
        raise stop or _Failure(400, _(BAD_FORM)) from exc


class _Ask(BaseModel):
    """What the client asks of this transcription, read from the form."""

    lang: str | None = None
    format: str = "json"
    granularities: frozenset[str] = frozenset()


def _asked(form: FormData) -> _Ask:
    """The form by the contract. prompt and temperature are accepted and do not change
    Parakeet."""
    model = form.get("model")
    if not isinstance(model, str) or not model:
        raise _Failure(400, _(NO_MODEL_FIELD), code="missing_required_parameter", param="model")
    if model not in MODELS:
        text = _(UNKNOWN_MODEL, model=model, models=", ".join(MODELS))
        raise _Failure(404, text, code="model_not_found", param="model")
    if str(form.get("stream", "")).lower() in ("true", "1"):
        raise _Failure(400, _(NO_STREAM), code="unsupported_value", param="stream")
    answer = str(form.get("response_format") or "json")
    if answer not in _FORMATS:
        text = _(UNKNOWN_FORMAT, format=answer)
        raise _Failure(400, text, code="unsupported_value", param="response_format")
    wanted = form.getlist("timestamp_granularities[]") + form.getlist("timestamp_granularities")
    for value in wanted:
        if value not in _GRANULARITIES:
            text = _(UNKNOWN_GRANULARITY, value=value)
            raise _Failure(400, text, code="unsupported_value", param="timestamp_granularities")
    if wanted and answer != "verbose_json":
        raise _Failure(400, _(ONLY_VERBOSE), param="timestamp_granularities")
    language = str(form.get("language") or "").strip().lower() or None
    return _Ask(lang=language, format=answer, granularities=frozenset(map(str, wanted)))


def _file_of(form: FormData) -> UploadFile:
    file = form.get("file")
    if not isinstance(file, UploadFile):
        raise _Failure(400, _(NO_FILE), code="missing_required_parameter", param="file")
    return file


def _duration_of(path: Path) -> float:
    """The audio's length; a file without readable audio is the client's mistake, refused
    before it takes a place in the queue."""
    try:
        info = mc.probe(path)
    except mc.MediaError as exc:
        raise _Failure(400, _(NO_AUDIO), code="invalid_file_format", param="file") from exc
    if not info.has_audio:
        raise _Failure(400, _(NO_AUDIO), code="invalid_file_format", param="file")
    return info.duration or 0.0


def _refused(exc: HTTPException) -> _Failure:
    """create_job's refusal in OpenAI terms: the only parameter it can refuse is the language."""
    if exc.status_code == 429:
        return _queue_full()
    if exc.status_code == 422:
        return _Failure(400, str(exc.detail), code="unsupported_language", param="language")
    return _server_failure(str(exc.detail))


def _stamp(seconds: float, separator: str) -> str:
    millis = round(seconds * 1000)
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02}{separator}{millis:03}"


def _srt(transcript: Transcript) -> str:
    return "\n".join(
        f"{i}\n{_stamp(s.start, ',')} --> {_stamp(s.end, ',')}\n{s.text}\n"
        for i, s in enumerate(transcript.segments, 1)
    )


def _vtt(transcript: Transcript) -> str:
    cues = (
        f"{_stamp(s.start, '.')} --> {_stamp(s.end, '.')}\n{s.text}\n" for s in transcript.segments
    )
    return "\n".join(["WEBVTT\n", *cues])


def _verbose(transcript: Transcript, duration: float, granularities: frozenset[str]) -> dict:
    """verbose_json. Typed clients require the confidence fields of a segment; Parakeet has no
    such numbers, so they stay neutral."""
    answer: dict = {
        "task": "transcribe",
        "language": transcript.language or "unknown",
        "duration": duration,
        "text": transcript.text,
    }
    if "segment" in granularities or not granularities:
        answer["segments"] = [
            {
                "id": i,
                "seek": 0,
                "start": s.start,
                "end": s.end,
                "text": s.text,
                "tokens": [],
                "temperature": 0.0,
                "avg_logprob": 0.0,
                "compression_ratio": 1.0,
                "no_speech_prob": 0.0,
            }
            for i, s in enumerate(transcript.segments)
        ]
    if "word" in granularities:
        answer["words"] = [
            {"word": w.text, "start": w.start, "end": w.end} for w in transcript.words
        ]
    return answer


def _answer(ask: _Ask, transcript: Transcript, duration: float) -> Response:
    if ask.format == "text":
        return PlainTextResponse(transcript.text)
    if ask.format == "srt":
        return PlainTextResponse(_srt(transcript))
    if ask.format == "vtt":
        return PlainTextResponse(_vtt(transcript))
    if ask.format == "verbose_json":
        return JSONResponse(_verbose(transcript, duration, ask.granularities))
    return JSONResponse({"text": transcript.text})


async def _left(request: Request) -> None:
    """Ends when the client leaves. request.is_disconnected() cannot tell: it asks in a cancelled
    scope, and under the auth middleware receive never gets to look."""
    while (await request.receive())["type"] != "http.disconnect":
        pass


def register_transcription_routes(
    app: FastAPI, create_job: Callable[[JobRequest, Request], dict | JSONResponse]
) -> None:
    async def forget_when_over(job_id: str) -> None:
        """Nobody can ask for this job by id: its text does not stay on the server."""
        while True:
            job = await run_in_threadpool(app.state.store.get, job_id)
            if job is None:
                return
            if job.status in _OVER:
                await run_in_threadpool(app.state.store.delete, job_id)
                return
            await anyio.sleep(POLL_S)

    async def waited(left: asyncio.Task, job_id: str) -> Job:
        deadline = time.monotonic() + TIMEOUT_S
        while True:
            job = await run_in_threadpool(app.state.store.get, job_id)
            if job is None:  # only a finished job can be deleted, by the node token
                raise _server_failure(_(SERVER_FAILURE))
            if job.status in _OVER:
                return job
            if left.done():
                await run_in_threadpool(stop_job, app, job_id)
                await forget_when_over(job_id)
                raise _ClientGone
            if time.monotonic() >= deadline:
                await run_in_threadpool(stop_job, app, job_id)
                raise _Failure(
                    504,
                    _(TOO_LONG),
                    kind="server_error",
                    code="timeout",
                    background=BackgroundTask(forget_when_over, job_id),
                )
            await anyio.sleep(POLL_S)

    async def finished(request: Request, job_id: str) -> Job:
        """Holds the connection until the job ends; a client that leaves cancels it."""
        left = asyncio.create_task(_left(request))
        try:
            return await waited(left, job_id)
        finally:
            left.cancel()

    async def queued(request: Request, path: Path, ask: _Ask) -> str:
        params: dict = {"backend": "parakeet"}
        if ask.lang is not None:
            params["lang"] = ask.lang
        job = JobRequest(op="transcribe", path=str(path), params=params)
        try:
            created = await run_in_threadpool(create_job, job, request)
        except HTTPException as exc:
            raise _refused(exc) from exc
        if isinstance(created, JSONResponse):  # only a fresh upload gets here: it cannot be taken
            raise _server_failure(_(SERVER_FAILURE))
        return created["id"]

    async def transcribe(request: Request) -> Response:
        # A full queue answers before the whole file travels; create_job decides for good.
        if not app.state.job_queue.has_room(user=request.state.user):
            raise _queue_full()
        form = await _read_form(request)
        try:
            ask = _asked(form)
            path = await run_in_threadpool(save_upload, app, _file_of(form), request.state.user)
        except HTTPException as exc:  # only the file name can be refused here
            raise _Failure(400, str(exc.detail), code="invalid_value", param="file") from exc
        finally:
            await form.close()
        job_id = None
        try:
            duration = await run_in_threadpool(_duration_of, path)
            job_id = await queued(request, path, ask)
        finally:
            if job_id is None:  # no job took the upload, so nothing else removes it
                app.state.uploads.release(str(path))
        job = await finished(request, job_id)
        await run_in_threadpool(app.state.store.delete, job_id)
        if job.status is JobStatus.ERROR:
            raise _server_failure(job.error or _(SERVER_FAILURE), "transcription_failed")
        return _answer(ask, Transcript.model_validate(job.result), duration)

    @app.post(TRANSCRIPTIONS)
    async def transcriptions(request: Request) -> Response:
        try:
            return await transcribe(request)
        except _Failure as failure:
            return failure.response()
        except _ClientGone:
            return Response(status_code=499)
        except Exception:
            _log.exception("transcription request failed")
            return _server_failure(_(SERVER_FAILURE)).response()
