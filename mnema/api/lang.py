"""Language of each request: Accept-Language of HTTP requests and of the /api/live handshake."""

from __future__ import annotations

from fastapi import FastAPI

from ..i18n import from_accept_language, using


class LanguageMiddleware:
    """Pure ASGI: the context variable set here reaches HTTP handlers and the websocket endpoint."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        header = next(
            (v.decode("latin-1") for k, v in scope.get("headers", []) if k == b"accept-language"),
            None,
        )
        with using(from_accept_language(header)):
            await self.app(scope, receive, send)


def add_language_middleware(app: FastAPI) -> None:
    """Add after the other middleware: the last one added is the outermost, so auth replies too
    get the client's language."""
    app.add_middleware(LanguageMiddleware)
