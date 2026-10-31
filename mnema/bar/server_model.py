"""The user's model and keys on the connected server (/api/model*), for the Mac windows.

The server checks every key and model itself and keeps them in the user's file; answers carry
keys only as a mask ("…a1B2"). A refusal is a ServerError with the server's text; a refused
model also says which step to retry (retry: "key" or "model").
"""

from __future__ import annotations

from typing import Any

import httpx

from ..client import ServerError
from ..connection import Connection
from ..i18n import _, current
from ..llm import providers

_TIMEOUT = httpx.Timeout(60.0, connect=5.0)  # the server checks the key with its provider


class ModelRefused(ServerError):
    """PUT /api/model refused: the text and the step to go back to."""

    def __init__(self, text: str, status: int, retry: str) -> None:
        super().__init__(text, status)
        self.retry = retry


class ServerModel:
    """GET, POST and PUT of /api/model* as the connected device's user."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def _call(self, method: str, path: str, body: dict | None = None) -> Any:
        headers = {"Accept-Language": current()}
        if self._connection.token:
            headers["Authorization"] = f"Bearer {self._connection.token}"
        with httpx.Client(
            base_url=self._connection.url,
            headers=headers,
            transport=providers.TRANSPORT,
            timeout=_TIMEOUT,
        ) as client:
            resp = client.request(method, path, json=body)
        if resp.is_error:
            try:
                answer = resp.json()
            except ValueError:
                answer = {}
            detail = answer.get("detail") if isinstance(answer, dict) else None
            text = detail.strip()[:300] if isinstance(detail, str) and detail.strip() else ""
            text = text or _("The server returned error {code}", code=resp.status_code)
            retry = answer.get("retry") if isinstance(answer, dict) else None
            if retry in ("key", "model"):
                raise ModelRefused(text, resp.status_code, retry)
            raise ServerError(text, resp.status_code)
        return resp.json() if resp.content else None

    def model(self) -> dict:
        """{model, key, asr_key, deepl_key}: the model name and the keys as masks, or None."""
        return self._call("GET", "/api/model")

    def model_key(self, provider: str, key: str | None) -> dict:
        """The provider's models by the key ({models: [{id, title, free}], recommended}); None —
        the key saved for this provider."""
        return self._call("POST", "/api/model/key", {"provider": provider, "key": key})

    def save_model(self, model: str, key: str | None) -> dict:
        """Check the model and save it with the key (None keeps the saved one)."""
        return self._call("PUT", "/api/model", {"model": model, "key": key})

    def save_asr_key(self, key: str | None) -> dict:
        return self._call("PUT", "/api/model/asr-key", {"key": key})

    def save_deepl_key(self, key: str | None) -> dict:
        return self._call("PUT", "/api/model/deepl-key", {"key": key})
