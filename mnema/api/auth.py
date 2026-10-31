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
from ..users import OWNER, Caller, as_user

# The only way in without a token: exchanging an invitation for a device token.
CLAIM_PATH = "/api/devices/claim"
# the node token, and anyone on a server without a token
NODE = Caller(device=NODE_OWNER, user=OWNER)
# a request that passes without a token (the claim, pages outside /api/): sees no jobs
NOBODY = Caller(device="", user=OWNER)


def bearer(header: str) -> str:
    return header[7:].strip() if header.lower().startswith("bearer ") else ""


async def caller(app: FastAPI, provided: str, node_token: str) -> Caller | None:
    """Who is asking: NODE for the node token (MNEMA_TOKEN), a device that is not revoked with
    its user, None when the token is not accepted."""
    if not provided:
        return None
    if hmac.compare_digest(provided.encode(), node_token.encode()):
        return NODE
    return await run_in_threadpool(app.state.devices.verify, provided, now=time.time())


def add_auth_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def require_token(request, call_next):
        """With a server token (install_setup.server_token), /api/* requires the Bearer token
        of the node or of a device.

        Without a token the server is open (local development). The caller goes to
        request.state.owner (a device sees only its own jobs) and request.state.user; the
        request runs as that user (users.as_user).
        """
        token = server_token()
        path = request.scope["path"]
        claim = request.method == "POST" and path == CLAIM_PATH
        found: Caller | None = NOBODY
        if not token:
            found = NODE
        elif path.startswith("/api/") and not claim:
            found = await caller(
                request.app, bearer(request.headers.get("authorization", "")), token
            )
            if found is None:
                return JSONResponse(
                    {"detail": _("A new invitation from the server is needed")}, status_code=401
                )
        request.state.owner, request.state.user = found.device, found.user
        with as_user(found.user):
            return await call_next(request)
