"""WebSocket proxy: PCM and segments go to a separate live session process."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time

import anyio
from fastapi import FastAPI, WebSocket

from ..core.base import MediaError
from ..i18n import _
from ..live.asr import StreamingTranscriber
from .auth import authorized, bearer

logger = logging.getLogger(__name__)
# the client sends PCM only after ready, so idle time counts from ready
_LIVE_IDLE_TIMEOUT = 120.0
# the child waits 120 s for the model itself; the margin covers its start and a hang before warmup
_LIVE_WARMUP_TIMEOUT = 180.0


def register_live_routes(app: FastAPI) -> None:
    @app.websocket("/api/live")
    async def live_ws(ws: WebSocket) -> None:
        from ..install_setup import server_token

        token = server_token()
        if token:
            provided = bearer(ws.headers.get("authorization", ""))
            if not await authorized(app, provided, token):
                await ws.close(code=1008)
                return
        await ws.accept()
        transcriber = StreamingTranscriber(
            lang=ws.query_params.get("lang") or None,
            policy="simulstreaming",
            model="large-v3",
            backend="whisper",
        )

        heard = time.monotonic()

        async def receive() -> bool:
            nonlocal heard
            while True:
                msg = await ws.receive()
                heard = time.monotonic()
                if msg.get("type") == "websocket.disconnect":
                    return False
                data = msg.get("bytes")
                if data:
                    transcriber.feed(data)
                elif msg.get("text") and json.loads(msg["text"]).get("type") == "end":
                    return True

        async def results() -> None:
            nonlocal heard
            ready = False
            draft = ""
            started = time.monotonic()
            while True:
                chunk = transcriber.poll()  # warmup errors also come through poll
                if not ready and transcriber.ready:
                    await ws.send_json({"type": "ready"})
                    ready = True
                    heard = time.monotonic()
                if chunk.committed or chunk.draft != draft:
                    await ws.send_json(chunk.model_dump())
                    draft = chunk.draft
                if ready and time.monotonic() - heard > _LIVE_IDLE_TIMEOUT:
                    raise TimeoutError
                if not ready and time.monotonic() - started > _LIVE_WARMUP_TIMEOUT:
                    logger.warning(
                        "live: the model did not warm up in %.0f seconds", _LIVE_WARMUP_TIMEOUT
                    )
                    raise MediaError(
                        _(
                            "The recognition model did not start in {minutes} min. Try again later",
                            minutes=f"{_LIVE_WARMUP_TIMEOUT / 60:.0f}",
                        )
                    )
                await asyncio.sleep(0.05)

        tasks: list[asyncio.Task] = []
        if not app.state.live_slots.acquire(blocking=False):
            await ws.send_json(
                {"type": "error", "detail": _("Live text on the server is busy. Try again later")}
            )
            await ws.close(code=1013)
            return
        try:
            transcriber.start_warmup()
            incoming = asyncio.create_task(receive())
            outgoing = asyncio.create_task(results())
            tasks = [incoming, outgoing]
            completed, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in completed:
                task.result()  # a process or socket error must reach the shared finally
            if incoming in completed and incoming.result():
                outgoing.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await outgoing
                tail = await asyncio.to_thread(transcriber.finish)
                if tail.committed or tail.draft:
                    await ws.send_json(tail.model_dump())
        except TimeoutError:
            logger.info("live: no PCM for %.0f seconds, closing the process", _LIVE_IDLE_TIMEOUT)
        except Exception as exc:
            logger.warning("live: recognition failed", exc_info=True)
            detail = str(exc).strip() if isinstance(exc, MediaError) else ""
            with contextlib.suppress(Exception):
                await ws.send_json(
                    {"type": "error", "detail": detail or _("Recognition failed on the server")}
                )
        finally:
            # ASGI may cancel the whole scope on disconnect; the process and the slot must
            # still be cleaned up.
            with anyio.CancelScope(shield=True):
                try:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    await asyncio.to_thread(transcriber.close)
                finally:
                    app.state.live_slots.release()
                with contextlib.suppress(Exception):
                    await ws.close()
