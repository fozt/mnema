"""API for the library's plain files, under the same token as the rest of the node API."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Annotated

from fastapi import Body, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from ..i18n import _
from ..library.files import LibraryError, LibraryFiles
from ..library.models import CreateRecord, FileName, Manifest, MoveRecord, Record, Stamp
from ..users import OWNER, user_library


class Libraries:
    """The libraries of the server's users: one object per user for good, made on first use."""

    def __init__(self, root: Path, owner: LibraryFiles | None) -> None:
        self._root = root
        self._found = {} if owner is None else {OWNER: owner}
        self._off = owner is None
        self._lock = threading.Lock()

    def get(self, user: str) -> LibraryFiles | None:
        """The user's library, or None when the library is turned off on this server."""
        if self._off:
            return None
        with self._lock:
            if user not in self._found:
                self._found[user] = LibraryFiles(user_library(self._root, user))
            return self._found[user]


def _match(value: str | None) -> str:
    if not value:
        raise HTTPException(409, _("The previous hash is needed; sync again"))
    return value.removeprefix('"').removesuffix('"')


def register_library_routes(app: FastAPI) -> None:
    def library(request: Request) -> LibraryFiles:
        store = app.state.libraries.get(request.state.user)
        if store is None:
            raise HTTPException(404, _("The library is turned off on this server"))
        return store

    @app.exception_handler(LibraryError)
    async def library_error(request, exc: LibraryError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.get("/api/library", response_model=Manifest)
    def manifest(request: Request) -> Manifest:
        found = app.state.devices.user(request.state.user)
        return library(request).manifest().model_copy(update={"library": found.library})

    @app.post("/api/library/records", response_model=Record)
    def create(request: Request, body: CreateRecord) -> Record:
        return library(request).create(body.id, body.path, body.files)

    @app.get("/api/library/records/{id}/files/{name}")
    def read(request: Request, id: str, name: FileName) -> Response:
        data, stamp = library(request).read(id, name)
        return Response(
            data,
            media_type="text/plain",
            headers={"ETag": f'"{stamp.sha256}"', "X-Mnema-Mtime-Ns": str(stamp.mtime_ns)},
        )

    @app.put("/api/library/records/{id}/files/{name}", response_model=Stamp)
    def write(
        request: Request,
        id: str,
        name: FileName,
        x_mnema_mtime_ns: Annotated[int, Header(ge=-(2**63), le=2**63 - 1)],
        data: Annotated[bytes, Body(media_type="text/plain")] = b"",
        if_match: Annotated[str | None, Header()] = None,
        if_none_match: Annotated[str | None, Header()] = None,
    ) -> Stamp:
        store = library(request)
        if if_none_match == "*" and if_match is None:
            expected = None
        elif if_match and if_none_match is None:
            expected = _match(if_match)
        else:
            raise HTTPException(409, _("The previous hash or a file-absent condition is needed"))
        return store.write(id, name, data, x_mnema_mtime_ns, expected)

    @app.delete("/api/library/records/{id}/files/{name}", status_code=204)
    def remove_file(
        request: Request, id: str, name: FileName, if_match: Annotated[str | None, Header()] = None
    ) -> Response:
        library(request).remove_file(id, name, _match(if_match))
        return Response(status_code=204)

    @app.patch("/api/library/records/{id}", response_model=Record)
    def move(request: Request, id: str, body: MoveRecord) -> Record:
        return library(request).move(id, body.path, body.expected_path)

    @app.delete("/api/library/records/{id}", status_code=204)
    def remove(
        request: Request, id: str, if_match: Annotated[str | None, Header()] = None
    ) -> Response:
        library(request).remove(id, _match(if_match))
        return Response(status_code=204)

    @app.put("/api/library/sections", status_code=204)
    def create_section(request: Request, path: str) -> Response:
        library(request).section(path)
        return Response(status_code=204)

    @app.delete("/api/library/sections", status_code=204)
    def remove_section(request: Request, path: str) -> Response:
        library(request).section(path, delete=True)
        return Response(status_code=204)
