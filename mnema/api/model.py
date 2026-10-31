"""The asking user's model and keys: the app sets them, the server keeps users/<name>.env.

Keys come back only masked; the checks go to the providers from the server. A key from a
request body is hidden in the log (redact.add_secret) before anything else.
"""

from __future__ import annotations

import contextvars
import threading
from concurrent.futures import Future
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from ..core.base import MediaError
from ..i18n import _
from ..llm import providers
from ..llm.config import masked, remove_model, save_key, save_model, user_settings
from ..llm.providers import Provider
from ..llm.redact import add_secret

if TYPE_CHECKING:
    from ..llm.config import Credentials
    from ..llm.errors import Refusal

# the app waits 120 s for an answer, a check with the model's retries can take minutes
MODEL_CHECK_SECONDS = 45.0


class KeyCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Provider
    key: str | None = None  # None: the saved key of this provider


class ModelChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str  # litellm name "provider/model"
    key: str | None = None  # None: the saved key of the model's provider


class Key(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str | None = None  # None: remove the key


def _refused(text: str, retry: str | None = None) -> JSONResponse:
    body = {"detail": text} if retry is None else {"detail": text, "retry": retry}
    return JSONResponse(body, status_code=400)


def _typed(key: str) -> str:
    add_secret(key)
    return key.strip()


def _key(request: Request, provider: Provider, typed: str | None) -> str:
    if typed is not None:
        return _typed(typed)
    return user_settings(request.state.user).provider_key(provider) or ""


def _check_in_time(creds: Credentials) -> Refusal | None:
    """claude.check_model, at most MODEL_CHECK_SECONDS: past that a TimeoutError, and the check
    left behind stops at its next retry pause; its answer goes nowhere."""
    from ..llm.claude import check_model

    late = threading.Event()
    done: Future[Refusal | None] = Future()

    def stop_when_late(_fraction: float | None, _message: str) -> None:
        if late.is_set():
            raise TimeoutError

    def check() -> None:
        try:
            done.set_result(check_model(creds, on=stop_when_late))
        except Exception as e:  # noqa: BLE001 - a stop by stop_when_late: nobody waits for it
            done.set_exception(e)

    run = contextvars.copy_context().run  # the asking user: LLMSettings needs her on the server
    threading.Thread(target=run, args=(check,), daemon=True, name="mnema-model-check").start()
    try:
        return done.result(timeout=MODEL_CHECK_SECONDS)
    except TimeoutError:
        late.set()
        raise


def register_model_routes(app: FastAPI) -> None:
    @app.get("/api/model")
    def get_model(request: Request) -> dict:
        return masked(request.state.user)

    @app.post("/api/model/key")
    def check_key(req: KeyCheck, request: Request):
        key = _key(request, req.provider, req.key)
        try:
            models = providers.check_key(req.provider, key)
        except MediaError as e:
            return _refused(str(e))
        return {
            "models": [{"id": m.id, "title": m.id, "free": m.free} for m in models],
            "recommended": providers.recommended(req.provider, models),
        }

    @app.put("/api/model")
    def put_model(req: ModelChoice, request: Request):
        from ..llm.config import credentials_for

        found = providers.provider_of(req.model)
        if found is None:
            return _refused(_("Choose a model from the list"), "model")
        key = _key(request, found[0], req.key)
        problem = providers.key_problem(found[0], key)
        if problem:
            return _refused(problem, "key")
        try:
            refusal = _check_in_time(credentials_for(req.model, key))
        except TimeoutError:
            return _refused(_("The model did not answer in time. Try again"), "model")
        if refusal is not None:
            return _refused(refusal.text, "key" if refusal.key else "model")
        save_model(req.model, key, user=request.state.user)
        return masked(request.state.user)

    @app.delete("/api/model", status_code=204)
    def delete_model(request: Request) -> Response:
        remove_model(user=request.state.user)
        return Response(status_code=204)

    @app.put("/api/model/asr-key")
    def put_asr_key(req: Key, request: Request):
        key = None if req.key is None else _typed(req.key)
        if key is not None:
            try:
                providers.check_key(Provider.OPENAI, key)
            except MediaError as e:
                return _refused(str(e))
        save_key("MNEMA_ASR_API_KEY", key, user=request.state.user)
        return masked(request.state.user)

    @app.put("/api/model/deepl-key")
    def put_deepl_key(req: Key, request: Request):
        from ..live.translate import check_deepl_key

        key = None if req.key is None else _typed(req.key)
        if key is not None:
            try:
                check_deepl_key(key)
            except MediaError as e:
                return _refused(str(e))
        save_key("DEEPL_API_KEY", key, user=request.state.user)
        return masked(request.state.user)
