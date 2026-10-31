"""Bearer token authentication middleware: the node token or a device token."""

from __future__ import annotations

import hmac
import time

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from ..i18n import _
from ..install_setup import server_token
from ..jobs import NODE_OWNER
from ..users import OWNER, as_user

# The only way in without a token: exchanging an invitation for a device token.
CLAIM_PATH = "/api/devices/claim"


def bearer(header: str) -> str:
    return header[7:].strip() if header.lower().startswith("bearer ") else ""


async def caller(app: FastAPI, provided: str, node_token: str) -> str | None:
    """Who is asking: NODE_OWNER for the node token (MNEMA_TOKEN), a device id for a device
    that is not revoked, None when the token is not accepted."""
    if not provided:
        return None
    if hmac.compare_digest(provided.encode(), node_token.encode()):
        return NODE_OWNER
    found = await run_in_threadpool(app.state.devices.verify, provided, now=time.time())
    return found.device if found is not None else None


async def authorized(app: FastAPI, provided: str, node_token: str) -> bool:
    return await caller(app, provided, node_token) is not None


def add_auth_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def require_token(request, call_next):
        """With a server token (install_setup.server_token), /api/* requires the Bearer token
        of the node or of a device.

        Without a token the server is open (local development). The caller goes to
        request.state.owner: a device sees only its own jobs.
        """
        token = server_token()
        path = request.scope["path"]
        claim = request.method == "POST" and path == CLAIM_PATH
        if not token:
            request.state.owner = NODE_OWNER
        elif path.startswith("/api/") and not claim:
            provided = bearer(request.headers.get("authorization", ""))
            owner = await caller(request.app, provided, token)
            if owner is None:
                return JSONResponse(
                    {"detail": _("A new invitation from the server is needed")}, status_code=401
                )
            request.state.owner = owner
        with as_user(OWNER):  # the server mode needs a user: every caller is the owner for now
            return await call_next(request)
