"""Exchanging a one-time invitation for a device token: the only route without a token."""

from __future__ import annotations

import json
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from ..devices import OutdatedClient, device_name
from ..i18n import _
from .auth import CLAIM_PATH

REFUSED = "The invitation is not valid: ask the server for a new one"
OUTDATED = "Update the app to connect with this invitation"
# A request without a token: read no more of the body than this; the code and name are shorter.
_BODY_LIMIT = 4096


def _refused() -> JSONResponse:
    return JSONResponse({"detail": _(REFUSED)}, status_code=403)


async def _small_body(request: Request) -> bytes | None:
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > _BODY_LIMIT:
            return None
    return body


def register_device_routes(app: FastAPI) -> None:
    @app.post(CLAIM_PATH)
    async def claim_invite(request: Request) -> JSONResponse:
        """{"invite", "name", "library"} → a device token; refusals are all alike and never echo
        the code. The one exception: a user's invitation needs "library": true (an app that
        knows library numbers), otherwise it is refused as outdated and stays unused."""
        body = await _small_body(request)
        try:
            data = json.loads(body) if body is not None else None
        except ValueError:
            data = None
        if not isinstance(data, dict):
            return _refused()
        code, name = data.get("invite"), data.get("name", "")
        if not isinstance(code, str) or not isinstance(name, str):
            return _refused()
        try:
            claimed = await run_in_threadpool(
                request.app.state.devices.claim,
                code,
                name,
                now=time.time(),
                library=data.get("library") is True,
            )
        except OutdatedClient:
            return JSONResponse({"detail": _(OUTDATED)}, status_code=403)
        if claimed is None:
            return _refused()
        device_id, token = claimed
        return JSONResponse(
            {"token": token, "device": {"id": device_id, "name": device_name(name)}},
            headers={"Cache-Control": "no-store"},
        )
