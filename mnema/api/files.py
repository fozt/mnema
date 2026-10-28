"""File routes: upload in one request or resumable in chunks, download of job results."""

from __future__ import annotations

import os
import shutil
import uuid
from enum import StrEnum
from pathlib import Path

import anyio
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from ..i18n import _
from .uploads import MAX_UPLOAD_SIZE, OffsetConflict, target

CHUNK_STALL_S = 30.0  # the client goes quiet mid-chunk: the chunk ends, the upload is free
INVALID_NAME = "Invalid file name"
ONLY_RESULTS = "Only a job result can be downloaded"
NO_UPLOAD = "Upload not found"


class UploadStart(BaseModel):
    name: str
    size: int = Field(ge=1, le=MAX_UPLOAD_SIZE)


class _Chunk(StrEnum):
    DONE = "done"
    TOO_BIG = "too_big"
    CUT = "cut"  # the client is gone or silent: what arrived stays


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


async def _receive_chunk(request: Request, file: Path, offset: int, room: int) -> _Chunk:
    """Streams the PUT body onto the file; a chunk past the size is rolled back whole."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > room:
        return _Chunk.TOO_BIG
    fd = await run_in_threadpool(os.open, file, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    outcome, received = _Chunk.DONE, 0
    try:
        while True:
            message = None
            with anyio.move_on_after(CHUNK_STALL_S):
                message = await request.receive()
            if message is None or message["type"] == "http.disconnect":
                outcome = _Chunk.CUT
                break
            data = message.get("body", b"")
            if received + len(data) > room:
                outcome = _Chunk.TOO_BIG
                break
            if data:
                await run_in_threadpool(_write_all, fd, data)
                received += len(data)
            if not message.get("more_body", False):
                break
        if outcome is _Chunk.TOO_BIG:
            os.ftruncate(fd, offset)
        await run_in_threadpool(os.fsync, fd)  # the offset is what is already on disk
    finally:
        os.close(fd)
    return outcome


def register_file_routes(app: FastAPI) -> None:
    @app.post("/api/upload")
    def upload(file: UploadFile = File(...)) -> dict:  # noqa: B008
        # A unique uploads/<uuid>/ per upload: equal names never overwrite each other's input.
        # The folder is ephemeral: jobs.py removes it once the job ends.
        app.state.uploads.sweep()
        dest_dir = app.state.root / "uploads" / uuid.uuid4().hex
        app.state.uploads.writing(dest_dir)
        success = False
        try:
            dest = target(dest_dir, file.filename or "upload.bin")
            if dest is None:
                raise HTTPException(403, _(INVALID_NAME))
            with open(dest, "wb") as f:
                shutil.copyfileobj(file.file, f)  # streamed: recordings can be gigabytes
            success = True
            return {"path": str(dest)}
        finally:
            app.state.uploads.written(dest_dir, success=success)

    @app.post("/api/uploads", status_code=201)
    def start_upload(req: UploadStart) -> dict:
        try:
            part = app.state.uploads.start(req.name, req.size)
        except ValueError as exc:
            raise HTTPException(403, _(INVALID_NAME)) from exc
        return {"id": part.id, "offset": part.offset}

    @app.get("/api/uploads/{upload_id}")
    def upload_state(upload_id: str) -> dict:
        part = app.state.uploads.part(upload_id)
        if part is None:
            raise HTTPException(404, _(NO_UPLOAD))
        return part.model_dump()

    @app.put("/api/uploads/{upload_id}")
    async def upload_chunk(upload_id: str, offset: int, request: Request) -> JSONResponse:
        uploads = app.state.uploads
        try:
            part = uploads.begin_chunk(upload_id, offset)
        except LookupError as exc:
            raise HTTPException(404, _(NO_UPLOAD)) from exc
        except OffsetConflict as exc:
            return JSONResponse({"detail": str(exc), "offset": exc.offset}, status_code=409)
        try:
            file = uploads.root / upload_id / part.name
            outcome = await _receive_chunk(request, file, offset, part.size - offset)
        finally:
            part = uploads.end_chunk(upload_id)  # no await: released on cancellation too
        if part is None:
            raise HTTPException(404, _(NO_UPLOAD))
        if outcome is _Chunk.TOO_BIG:
            raise HTTPException(400, _("The chunk goes past the upload size"))
        if outcome is _Chunk.CUT:
            return JSONResponse(
                {"detail": _("The chunk did not arrive in full"), "offset": part.offset},
                status_code=408,
            )
        return JSONResponse({"offset": part.offset, "path": part.path})

    def _checked(raw: str) -> Path:
        # The node root can be a recordings folder: only FILE results of jobs go out.
        outputs = (app.state.root / "outputs").resolve()
        p = Path(raw).expanduser().resolve()
        if not p.is_relative_to(outputs) or p == outputs:
            raise HTTPException(403, _(ONLY_RESULTS))
        if not p.is_file():
            raise HTTPException(404, _("File not found"))
        return p

    @app.get("/api/download")
    def download(path: str) -> FileResponse:
        # Starlette handles Content-Type and Range/206.
        return FileResponse(_checked(path))
