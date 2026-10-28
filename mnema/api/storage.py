"""Runner REST route: is the node available."""

from __future__ import annotations

from fastapi import FastAPI

from .. import node_settings


def register_storage_routes(app: FastAPI) -> None:
    @app.get("/api/capabilities")
    def capabilities() -> dict:
        """Node availability; llm: does it make titles and summaries (a model with a key)."""
        return {
            "ok": True,
            "llm": node_settings.model() is not None,
            "name": node_settings.shown_name(),
        }
