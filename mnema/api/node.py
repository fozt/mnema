"""Clients only read the node's capabilities and current settings."""

from fastapi import FastAPI

from .. import node_settings as node


def register_node_routes(app: FastAPI) -> None:
    @app.get("/api/node/asr")
    def node_asr() -> dict:
        return {"choices": node.asr_choices()}

    @app.get("/api/node/settings")
    def get_node_settings() -> dict:
        return node.current()
