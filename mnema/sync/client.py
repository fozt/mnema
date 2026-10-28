"""HTTP to the selected library only; an ambiguous response means a new sync, not a replay."""

from __future__ import annotations

import hashlib
from typing import Any

import httpx

from ..config import Settings
from ..connection import parse_connection
from ..i18n import _
from ..library.models import FileBody, FileName, Manifest, Record, Stamp
from .scan import SyncError
from .state import server_key


class LibraryClient:
    def __init__(self, connection: str, *, transport: Any = None) -> None:
        node = parse_connection(connection)
        if node.invite:
            raise SyncError(_("Connect this device to the server first"))
        settings = Settings()
        self.server = server_key(connection)
        self._http = httpx.AsyncClient(
            base_url=node.url + "/",
            headers={"Authorization": f"Bearer {node.token}"} if node.token else {},
            verify=settings.ca or True,
            timeout=httpx.Timeout(
                connect=settings.http_connect_timeout,
                read=settings.http_read_timeout,
                write=settings.http_write_timeout,
                pool=settings.http_pool_timeout,
            ),
            transport=transport,
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self._http.request(method, "api/library" + path, **kwargs)
        except httpx.TransportError as exc:
            raise SyncError(_("The sync server is unavailable; files are saved")) from exc
        if response.status_code == 409:
            raise SyncError(_("Files on the server changed; sync will run again"))
        if response.status_code == 404:
            raise SyncError(_("The library or recording on the selected server is unavailable"))
        if response.status_code in (401, 403):
            from ..install_setup import NEW_INVITE

            raise SyncError(NEW_INVITE)
        if not response.is_success:
            raise SyncError(_("Could not read or change the server library"))
        return response

    async def manifest(self) -> Manifest:
        payload = (await self._request("GET", "")).json()
        if not isinstance(payload, dict) or not {"records", "sections"} <= payload.keys():
            raise SyncError(_("The server returned an incomplete list of recordings"))
        return Manifest.model_validate(payload)

    async def create(self, id: str, path: str, files: dict[FileName, FileBody]) -> Record:
        response = await self._request(
            "POST",
            "/records",
            json={
                "id": id,
                "path": path,
                "files": {name: body.model_dump() for name, body in files.items()},
            },
        )
        return Record.model_validate(response.json())

    async def read(self, id: str, name: FileName) -> tuple[bytes, Stamp]:
        response = await self._request("GET", f"/records/{id}/files/{name}")
        stamp = Stamp(
            sha256=response.headers["ETag"].strip('"'),
            mtime_ns=int(response.headers["X-Mnema-Mtime-Ns"]),
        )
        if hashlib.sha256(response.content).hexdigest() != stamp.sha256:
            raise SyncError(_("The received file does not match its checksum"))
        response.content.decode("utf-8")
        return response.content, stamp

    async def write(
        self, id: str, name: FileName, data: bytes, mtime_ns: int, expected: str | None
    ) -> Stamp:
        headers = {"Content-Type": "text/plain", "X-Mnema-Mtime-Ns": str(mtime_ns)}
        headers.update({"If-Match": expected} if expected else {"If-None-Match": "*"})
        return Stamp.model_validate(
            (
                await self._request(
                    "PUT", f"/records/{id}/files/{name}", content=data, headers=headers
                )
            ).json()
        )

    async def remove_file(self, id: str, name: FileName, expected: str) -> None:
        await self._request("DELETE", f"/records/{id}/files/{name}", headers={"If-Match": expected})

    async def move(self, id: str, path: str, expected_path: str) -> Record:
        return Record.model_validate(
            (
                await self._request(
                    "PATCH", f"/records/{id}", json={"path": path, "expected_path": expected_path}
                )
            ).json()
        )

    async def remove(self, id: str, expected_tag: str) -> None:
        await self._request("DELETE", f"/records/{id}", headers={"If-Match": expected_tag})

    async def section(self, path: str, delete: bool = False) -> None:
        await self._request("DELETE" if delete else "PUT", "/sections", params={"path": path})

    async def aclose(self) -> None:
        await self._http.aclose()
