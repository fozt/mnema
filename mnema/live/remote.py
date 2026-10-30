"""Remote ASR transcriber: the warmup/feed/poll/finish contract over a websocket to the runner.

The microphone stays local; PCM frames go to the runner (mnema serve) and the transcript comes
back as LiveChunk messages. Recording comes first: connecting and waiting for 'ready' run in the
background (start_warmup), and feed buffers PCM in a bounded prebuffer until then (like the local
facade in asr.py). Message mapping (_ws_url/_parse) is pure and unit-tested.
"""

from __future__ import annotations

import contextvars
import json
import threading
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

from ..core.base import MediaError
from ..core.models import Segment
from ..i18n import _, current
from .asr import PcmPrebuffer
from .models import LiveChunk


def _ws_url(runner: str, params: dict) -> str:
    """http(s)://host → ws(s)://host/api/live?<params> (None parameters are dropped)."""
    parts = urlsplit(runner)
    scheme = "wss" if parts.scheme == "https" else "ws"
    query = urlencode({k: v for k, v in params.items() if v is not None})
    return urlunsplit((scheme, parts.netloc, "/api/live", query, ""))


class RemoteStreamingTranscriber:
    """ASR on the runner over a websocket; same contract as the local StreamingTranscriber."""

    def __init__(
        self,
        *,
        runner: str,
        token: str | None = None,
        ca: str | None = None,
        lang: str | None = None,
        label: str = "mic",
    ) -> None:
        self._url = _ws_url(runner, {"lang": lang})
        self._token = token
        self._ca = ca
        self._label = label
        self._loop: Any = None
        self._thread: threading.Thread | None = None
        self._task: Any = None
        self._ws: Any = None
        self._ready = threading.Event()
        self._error: Exception | None = None
        self._finishing = False
        self._lock = threading.Lock()
        self._prebuf = PcmPrebuffer()  # PCM while connecting/warming up: feed never waits
        self._committed: list[Segment] = []
        self._draft = ""

    def _parse(self, data: dict) -> LiveChunk | None:
        """Runner message → LiveChunk (None for control messages such as {'type':'ready'})."""
        if data.get("type"):
            return None
        segs = [
            Segment(
                start=float(c["start"]),
                end=float(c["end"]),
                text=str(c["text"]),
                speaker=self._label,
            )
            for c in data.get("committed", [])
        ]
        return LiveChunk(committed=segs, draft=str(data.get("draft", "")))

    def _ingest(self, data: dict) -> None:
        if data.get("type") == "ready":
            self._ready.set()
            return
        if data.get("type") == "error":  # a server error surfaces, it is never swallowed
            self._error = MediaError(str(data.get("detail") or _("server error")))
            self._ready.set()
            return
        chunk = self._parse(data)
        if chunk is None:
            return
        with self._lock:
            self._committed.extend(chunk.committed)
            self._draft = chunk.draft

    def start_warmup(self) -> None:
        """Connect to the runner in the background without blocking; see the ready property."""
        if self._thread is not None or self._finishing:
            return
        import asyncio

        try:
            import websockets  # noqa: F401
        except Exception as e:  # noqa: BLE001
            raise MediaError(
                _("websockets is missing — reinstall Mnema (./install.sh client).")
            ) from e
        self._loop = asyncio.new_event_loop()
        # the socket thread builds error texts: it keeps the caller's interface language
        context = contextvars.copy_context()
        self._thread = threading.Thread(target=context.run, args=(self._run,), daemon=True)
        self._thread.start()

    @property
    def ready(self) -> bool:
        """The runner answered 'ready' and the socket is alive: feed goes to the runner."""
        return self._ready.is_set() and self._error is None

    @property
    def fed_offset_seconds(self) -> float:
        """ASR time offset against the source stream: the connect prebuffer dropped old PCM,
        so t=0 of the fed stream is later than the start of the source's timeline."""
        return self._prebuf.dropped_seconds

    def warmup(self) -> None:
        """Blocking connect, for when readiness is needed before speech starts (scripts)."""
        try:
            self.start_warmup()
            if not self._ready.wait(timeout=60):
                raise MediaError(
                    _("The server isn't ready: check the connection, the token and the model.")
                )
            if self._error is not None:
                raise MediaError(
                    _("Couldn't connect to the server: {error}", error=self._error)
                ) from self._error
        except BaseException:
            self.close()
            raise

    def _run(self) -> None:
        import asyncio

        asyncio.set_event_loop(self._loop)
        try:
            self._task = self._loop.create_task(self._pipeline())
            if self._finishing:
                self._task.cancel()
            self._loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001 - reported to the main thread, not as a traceback
            self._error = e
        finally:
            self._ready.set()  # unblock warmup (on success _ingest already set it)
            pending = [t for t in asyncio.all_tasks(self._loop) if not t.done()]
            for task in pending:
                task.cancel()
            self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._loop.close()

    async def _pipeline(self) -> None:
        import ssl

        import websockets

        headers = {"Accept-Language": current()}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        sslctx = None
        if self._url.startswith("wss"):
            # without MNEMA_CA the system trust store; with it, a custom root
            sslctx = ssl.create_default_context(cafile=self._ca if self._ca else None)
        async with websockets.connect(
            self._url, additional_headers=headers, ssl=sslctx, max_size=None, close_timeout=2
        ) as ws:
            self._ws = ws
            async for raw in ws:
                self._ingest(json.loads(raw))
        # a clean close without finish() means the runner dropped the session: report it
        # instead of silently losing audio
        if not self._finishing and self._error is None:
            self._error = MediaError(_("the server closed the connection"))

    def feed(self, pcm: bytes) -> None:
        if self._error is not None:  # do not send into a dead socket
            raise MediaError(
                _("Lost connection to the server: {error}", error=self._error)
            ) from self._error
        self.start_warmup()
        if not self.ready or self._ws is None:  # still connecting: buffer, flushed on 'ready'
            self._prebuf.push(pcm)
            return
        for chunk in [*self._prebuf.drain(), pcm]:  # frame by frame (socket message limits)
            self._send(chunk)

    async def _send_quiet(self, data: Any) -> None:
        """Send without raising: a dropped connection is already visible through _error.

        Otherwise the abandoned task (_send does not collect the future) dies with
        ConnectionClosedError and asyncio prints "Task exception was never retrieved" with a
        traceback after the session has already been saved.
        """
        try:
            await self._ws.send(data)
        except Exception:  # noqa: BLE001 - socket closed: the cause is already in _error
            pass

    def _send(self, pcm: bytes) -> None:
        import asyncio

        sending = self._send_quiet(pcm)
        try:
            asyncio.run_coroutine_threadsafe(sending, self._loop)
        except RuntimeError:  # loop already stopped: the cause surfaces on the next feed
            sending.close()

    def poll(self) -> LiveChunk:
        with self._lock:
            chunk = LiveChunk(committed=list(self._committed), draft=self._draft)
            self._committed = []
        return chunk

    def close(self) -> None:
        """Close the connection, also during a cold warmup, so the runner stops its worker."""
        self._finishing = True
        if self._loop is not None and self._task is not None and not self._loop.is_closed():
            try:
                self._loop.call_soon_threadsafe(self._task.cancel)
            except RuntimeError:  # the thread closed the loop between the check and the cancel
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)

    def finish(self) -> LiveChunk:
        try:
            return self._finish()
        finally:
            self.close()

    def _finish(self) -> LiveChunk:
        import asyncio

        self._finishing = True  # before 'end': the server's close after it is not a drop
        # Drain only if the engine actually warmed up ('ready' arrived). Otherwise (Ctrl-C during
        # a cold engine start on the runner) the server neither answers 'end' nor closes the
        # socket while loading the model, and the join would hang for the whole timeout.
        if (
            self._ws is not None
            and self._loop is not None
            and self._error is None
            and self._ready.is_set()
        ):
            # 'ready' arrived right before the stop: the prebuffer tail still goes before 'end'
            for chunk in self._prebuf.drain():
                self._send(chunk)
            sending = self._send_quiet(json.dumps({"type": "end"}))
            try:
                fut = asyncio.run_coroutine_threadsafe(sending, self._loop)
                fut.result(timeout=5)  # _send_quiet never raises: wait for the actual send
            except Exception:  # noqa: BLE001 - loop dead or timed out: skip the drain
                if self._loop.is_closed():
                    sending.close()
            if self._thread is not None:
                # a healthy drain of the final segments takes a fraction of a second
                self._thread.join(timeout=5)
        return self.poll()
