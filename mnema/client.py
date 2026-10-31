"""MnemaClient: the HTTP client of the mnema server REST API."""

from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

from .connection import Answer, answer_of
from .core.base import MediaError, noop, phased
from .i18n import _, current
from .jobs import KEY_REASON, KEY_TAKEN

# How long to wait for an unreachable runner (an update restarts the service in tens of seconds).
_RESTART_WAIT_S = 90.0
_RESTART_POLL_S = 3.0

# The active remote job of this process: Ctrl-C in the CLI cancels it, otherwise it keeps
# burning the runner's GPU for nobody. Written by the polling thread, read by the exit handler.
_active_job: dict | None = None


def cancel_active_job() -> str | None:
    """Best-effort cancel of the active remote job (for Ctrl-C): its id or None.

    Synchronous httpx with a short timeout: called from the exit handler."""
    global _active_job
    info, _active_job = _active_job, None
    if not info:
        return None
    try:
        httpx.post(
            f"{info['base_url']}/api/jobs/{info['job_id']}/cancel",
            headers=info["headers"],
            timeout=5.0,
            verify=info["verify"],
        )
        return info["job_id"]
    except Exception:  # noqa: BLE001 - exiting anyway, the cancel must not get in the way
        return None


# Gateway answers while the runner restarts (a service update): polling outlives them
_GATEWAY_ERRORS = (502, 503, 504)
GATEWAY_DOWN = "The server is restarting or unavailable"
_RESTART_GRACE = 90.0  # how long to wait for the runner to come back, s
_CHUNK_RETRY = (408, 409, *_GATEWAY_ERRORS)


class ServerError(MediaError):
    """The server answered with an error: its detail or the code in words; status is the code."""

    def __init__(self, text: str, status: int) -> None:
        super().__init__(text)
        self.status = status


class UploadMissing(MediaError):
    """The upload is gone from the node or held by another job: the client sends this source
    again (track; None when the job has a single input)."""

    def __init__(self, track: str | None) -> None:
        super().__init__(_("The upload is no longer available on the server"))
        self.track = track


class KeyTaken(MediaError):
    """The retry key belongs to a job this client cannot see: a new job needs a new key."""

    def __init__(self) -> None:
        super().__init__(_(KEY_TAKEN))


class _UploadReader:
    """A file wrapper reporting upload progress: httpx reads the body in chunks and each read
    reports the share sent. Other methods (seek/tell for the length) go to the file."""

    def __init__(self, fh, total: int, on) -> None:
        self._fh = fh
        self._total = total
        self._on = on

    def __getattr__(self, name: str):
        return getattr(self._fh, name)

    def read(self, size: int = -1) -> bytes:
        chunk = self._fh.read(size)
        if chunk:
            self._on(min(self._fh.tell() / self._total, 1.0), _("Uploading to the server…"))
        return chunk


class MnemaClient:
    """Client of the mnema server REST API.

    Parameters:
        base_url: the server's base URL, e.g. ``http://localhost:8000``.
        token: the Bearer token, when the server requires one.
        transport: an httpx transport for tests (``httpx.ASGITransport``). ASGITransport works
            only with AsyncClient, so the client uses AsyncClient inside and runs coroutines
            with asyncio.
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        transport: Any = None,
    ) -> None:
        from .config import Settings

        s = Settings()
        headers: dict[str, str] = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._base_url = base_url
        self._auth = headers
        self._transport = transport
        # MNEMA_CA is the root CA of an own TLS proxy in front of the server (a certificate
        # without a domain): trusted when set, else the httpx default. Never verify=False:
        # a MITM would make TLS worthless.
        self._verify: str | bool = s.ca or True
        # Timeouts suit a WAN runner, not just localhost: the httpx default (5 s for all)
        # breaks the upload or download of a large recording midway.
        self._timeout = httpx.Timeout(
            connect=s.http_connect_timeout,
            read=s.http_read_timeout,
            write=s.http_write_timeout,
            pool=s.http_pool_timeout,
        )
        # Retries only on transient drops (httpx.TransportError), and not forever.
        self._retries = s.http_retries
        self._retry_backoff = s.http_retry_backoff

    @property
    def _headers(self) -> dict[str, str]:
        """Authorization and the interface language: the server answers in it."""
        return {**self._auth, "Accept-Language": current()}

    def _make_async_client(self) -> httpx.AsyncClient:
        """An AsyncClient with this client's settings."""
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            transport=self._transport,
            verify=self._verify,
            timeout=self._timeout,
        )

    async def _request_with_retry(self, make_request):
        """Runs ``make_request()``, retrying on transient connection drops.

        ``make_request`` is an async callback that makes the request anew on every attempt (an
        upload or download may have broken off mid-file).
        """
        attempts = self._retries + 1
        last_exc: httpx.TransportError | None = None
        for attempt in range(attempts):
            try:
                return await make_request()
            except httpx.TransportError as exc:
                last_exc = exc
                if attempt + 1 >= attempts:
                    break
                await asyncio.sleep(self._retry_backoff * (attempt + 1))
        assert last_exc is not None
        raise last_exc

    async def _wait_out_restart(self, do_call, on=noop):
        """Makes the call, waiting out an unreachable runner (a restart during a deploy).

        Retries ONLY on ConnectError/ConnectTimeout: no connection means the request was surely
        not sent, which is safe even for the non-idempotent POST /api/jobs. Drops after sending
        are not retried here: that could run the job twice.
        """
        deadline = time.monotonic() + _RESTART_WAIT_S
        while True:
            try:
                return await do_call()
            except (httpx.ConnectError, httpx.ConnectTimeout):
                if time.monotonic() >= deadline:
                    raise
                on(None, _("Server unavailable, waiting…"))
                await asyncio.sleep(_RESTART_POLL_S)

    @staticmethod
    def _run(coro):
        """Runs a coroutine from synchronous code, in a separate thread when an event loop is
        already running (pytest-asyncio)."""
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        if running_loop is not None and running_loop.is_running():
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                future = ex.submit(asyncio.run, coro)
                return future.result()
        return asyncio.run(coro)

    def health(self, timeout: float = 2.0) -> dict | None:
        """Is the Mnema server alive: GET /api/capabilities within `timeout` s, without retries
        or waiting for a restart (probe). None: no answer or not a Mnema server; 401/403:
        {"ok": True, "auth": False}, alive but the token is wrong; else its capabilities.
        Called at the start and end of live/rec and before ops, so a down server is reported
        at once rather than an hour later at the end of a call."""
        answer, caps = self.probe(timeout)
        if answer is Answer.TOKEN:
            return {"ok": True, "auth": False}
        return caps if answer is Answer.MNEMA else None

    def probe(self, timeout: float = 2.0) -> tuple[Answer, dict]:
        """What the address answered to GET /api/capabilities (connection.answer_of), and the
        Mnema server's capabilities; the Server window tells "no answer" from "not Mnema"."""
        try:
            return self._run(self._probe(timeout))
        except Exception:  # noqa: BLE001 - any transport failure means no answer
            return Answer.NO_ANSWER, {}

    async def _probe(self, timeout: float) -> tuple[Answer, dict]:
        async with httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            transport=self._transport,
            verify=self._verify,
            timeout=timeout,
        ) as client:
            resp = await client.get("/api/capabilities")
        return answer_of(resp.status_code, resp.content)

    def upload(self, path: Path | str, on=noop) -> str:
        """Uploads a file to the server and returns its path there.

        on(fraction, message) reports the bytes sent: a large recording takes minutes, and
        without progress that looks like a hang."""
        return self._run(self._upload(Path(path), on))

    def upload_resumable(self, path: Path | str, *, chunk: int = 4 * 1024 * 1024, on=noop) -> str:
        """Uploads a file in resumable chunks (POST/PUT /api/uploads), returns its node path.

        A drop mid-chunk does not restart the upload: the node knows how many bytes arrived and
        answers a wrong offset with 409 and its own."""
        return self._run(self._upload_resumable(Path(path), chunk, on))

    def download(self, server_path: str, dest: Path | str) -> Path:
        """Downloads a runner file (GET /api/download?path=) to dest and returns dest.

        For file results (compress, mp3): the op returns a path on the runner's disk and this
        fetches it. Streamed: recordings can be large.
        """
        return self._run(self._download(server_path, Path(dest)))

    def asr_choices(self) -> list[dict[str, str]]:
        """The recognition choices of the selected node, with its keys and engines."""
        return self._run(self._node_call("GET", "/api/node/asr"))["choices"]

    def node_settings(self) -> dict:
        """The node name, its model without the key, and its engines (GET /api/node/settings)."""
        return self._run(self._node_call("GET", "/api/node/settings"))

    def translate_phrase(self, text: str, *, context: list[str], target_lang: str) -> str:
        """Translate a live phrase with the node's model (POST /api/translate/phrase)."""
        body = {"text": text, "context": context, "target_lang": target_lang}
        return self._run(self._node_call("POST", "/api/translate/phrase", body))["text"]

    def claim_invite(self, code: str, name: str) -> str:
        """Exchange the node's invitation for this device's token (POST /api/devices/claim).

        The request has no Bearer; a refusal is a MediaError with the node's text, never the
        code."""
        return self._run(self._claim_invite(code, name))

    async def _claim_invite(self, code: str, name: str) -> str:
        async with httpx.AsyncClient(
            base_url=self._base_url,
            transport=self._transport,
            verify=self._verify,
            timeout=self._timeout,
        ) as client:
            resp = await client.post(
                "/api/devices/claim", json={"invite": code, "name": name, "library": True}
            )
        if resp.status_code == 403:
            try:
                detail = resp.json()["detail"]
            except (ValueError, KeyError, TypeError):
                detail = None
            raise MediaError(
                detail if isinstance(detail, str) else _("The server refused the invitation")
            )
        self._raise_for_status(resp)
        return resp.json()["token"]

    def submit(
        self,
        op: str,
        *,
        key: str,
        params: dict | None = None,
        path: str | None = None,
        tracks: dict[str, str] | None = None,
    ) -> str:
        """Queue a job with a retry key: a lost answer never starts a second processing."""
        if not key or (path is None) == (tracks is None):
            raise MediaError(_("A job key and either a path or tracks are needed"))
        payload = {"op": op, "key": key, "params": params or {}}
        payload.update({"path": path} if path is not None else {"tracks": tracks})
        return self._run(self._submit(payload))

    async def _submit(self, payload: dict) -> str:
        async with self._make_async_client() as client:
            response = await self._request_with_retry(
                lambda: client.post("/api/jobs", json=payload)
            )
            if response.status_code in (404, 409):
                try:
                    body = response.json()
                    track, reason = body.get("track"), body.get("reason")
                except (ValueError, AttributeError):
                    track = reason = None
                if response.status_code == 409 and reason == KEY_REASON:
                    raise KeyTaken
                raise UploadMissing(track if isinstance(track, str) else None)
            self._raise_for_status(response)
            return response.json()["id"]

    def cancel(self, job_id: str) -> None:
        """Cancel this client's job; a finished or deleted one is a no-op."""
        self._run(self._job_call("POST", job_id, "/cancel"))

    def forget(self, job_id: str) -> None:
        """Delete a finished job once the client has saved its result."""
        self._run(self._job_call("DELETE", job_id))

    async def _job_call(self, method: str, job_id: str, suffix: str = "") -> dict | None:
        async with self._make_async_client() as client:
            response = await self._request_with_retry(
                lambda: client.request(method, f"/api/jobs/{job_id}{suffix}")
            )
            if response.status_code == 404:
                return None
            self._raise_for_status(response)
            return response.json()

    def wait_job(self, job_id: str, *, on=noop, poll_interval: float = 0.3) -> dict | None:
        """Wait for the job to finish; the node keeps the result until forget()."""
        return self._run(self._wait_existing(job_id, on=on, poll_interval=poll_interval))

    async def _wait_existing(self, job_id: str, *, on, poll_interval: float) -> dict | None:
        async with self._make_async_client() as client:
            return await self._wait_job(client, job_id, on=on, poll_interval=poll_interval)

    def run(
        self,
        op: str,
        path: str | None = None,
        params: dict | None = None,
        *,
        url: str | None = None,
        urls: list[str] | None = None,
        cookies: str | None = None,
        on=noop,
        poll_interval: float = 0.3,
    ) -> dict:
        """Runs an operation on the server and polls until it ends.

        The input is exactly one of path (a file already on the runner), url or urls. Links are
        downloaded and joined on the client; cookies stay on the client. on(fraction|None,
        message) gets download, upload and processing progress. A failure of the preparation
        or of the remote operation is raised as MediaError.
        """
        if sum(value is not None for value in (path, url, urls)) != 1:
            raise MediaError(_("Exactly one of path, url or urls is needed"))
        params = dict(params or {})
        source_name = None
        if url is not None or urls is not None:
            from . import ops
            from .inputs import local_input

            spec = ops.get(op)
            audio_only = spec is not None and spec.audio_only
            download_on, upload_on, on = phased(on, 0.3, 0.2, 0.5)
            source = urls if urls is not None else url
            assert source is not None
            with local_input(
                source, cookies=cookies, want_video=not audio_only, on=download_on
            ) as src:
                source_name = src.stem
                path = self.upload(src, on=upload_on)
            # The file and cookies are not needed after the upload: freed before the queue wait.
        result = self._run(self._run_op(op, path, params, on=on, poll_interval=poll_interval))
        if source_name is not None:
            result.setdefault("source_name", source_name)
        return result

    async def _node_call(self, method: str, path: str, body: dict | None = None) -> dict:
        async with self._make_async_client() as client:
            resp = await client.request(method, path, json=body)
        if resp.is_error:
            try:
                detail = resp.json().get("detail")
            except (ValueError, AttributeError):  # the gateway answered with non-JSON
                detail = None
            if isinstance(detail, str):  # the node's 409/422 is already in the user's words
                raise MediaError(detail)
        self._raise_for_status(resp)
        return resp.json()

    async def _upload(self, src: Path, on=noop) -> str:
        total = max(src.stat().st_size, 1)

        async def do_upload() -> str:
            async with self._make_async_client() as client:
                with src.open("rb") as fh:
                    # httpx types files as IO[bytes] but only calls .read(), which the wrapper has
                    reader: Any = _UploadReader(fh, total, on)
                    resp = await client.post(
                        "/api/upload",
                        files={"file": (src.name, reader, "application/octet-stream")},
                    )
                self._raise_for_status(resp)
                return resp.json()["path"]

        # two layers: _request_with_retry mends short drops, _wait_out_restart waits out an
        # unreachable runner (an upload is effectively idempotent: the file is ephemeral)
        return await self._wait_out_restart(lambda: self._request_with_retry(do_upload), on=on)

    async def _upload_resumable(self, src: Path, chunk: int, on=noop) -> str:
        size = src.stat().st_size
        if size == 0:
            return await self._upload(src, on)
        async with self._make_async_client() as client:

            async def start() -> httpx.Response:
                return await client.post("/api/uploads", json={"name": src.name, "size": size})

            # a lost answer leaves an empty upload; the node removes it by TTL
            resp = await self._wait_out_restart(lambda: self._request_with_retry(start), on=on)
            self._raise_for_status(resp)
            url = f"/api/uploads/{resp.json()['id']}"
            offset, attempt = 0, 0
            deadline = time.monotonic() + _RESTART_WAIT_S
            with src.open("rb") as fh:
                while True:
                    fh.seek(offset)
                    data = fh.read(chunk)
                    error: httpx.TransportError | None = None
                    try:
                        resp = await client.put(
                            url,
                            params={"offset": offset},
                            content=data,
                            headers={"Content-Type": "application/octet-stream"},
                        )
                    except httpx.TransportError as exc:
                        error = exc
                    if error is None and resp.status_code == 409:
                        known = resp.json().get("offset")
                        if isinstance(known, int) and known != offset:
                            offset = known  # the node knows best: a lost answer or a broken chunk
                            continue
                    if error is not None or resp.status_code in _CHUNK_RETRY:
                        # a drop, a partial chunk, a previous chunk still busy, a node restart:
                        # short failures are retried, a restart or busy upload waits up to 90 s
                        if attempt >= self._retries and time.monotonic() >= deadline:
                            if error is not None:
                                raise error
                            self._raise_for_status(resp)
                        attempt += 1
                        on(None, _("Connection to the server dropped, continuing…"))
                        await asyncio.sleep(min(self._retry_backoff * attempt, _RESTART_POLL_S))
                        continue
                    self._raise_for_status(resp)
                    body = resp.json()
                    if body.get("path"):
                        on(1.0, _("Uploading to the server…"))
                        return body["path"]
                    if not data:
                        raise MediaError(
                            _("The server took all the bytes but did not finish the upload")
                        )
                    offset, attempt = body["offset"], 0
                    deadline = time.monotonic() + _RESTART_WAIT_S
                    on(min(offset / size, 1.0), _("Uploading to the server…"))

    async def _download(self, server_path: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)

        async def do_download() -> Path:
            async with self._make_async_client() as client:
                async with client.stream(
                    "GET", "/api/download", params={"path": server_path}
                ) as resp:
                    if resp.is_error:
                        # a stream has not read the body yet: read it for the error text
                        await resp.aread()
                        self._raise_for_status(resp)
                    # The previous result survives a drop, a cancel or a disk failure: a
                    # temporary file alongside, replaced atomically on the same file system.
                    partial: Path | None = None
                    try:
                        with tempfile.NamedTemporaryFile(
                            dir=dest.parent, prefix=f".{dest.name}.", suffix=".part", delete=False
                        ) as fh:
                            partial = Path(fh.name)
                            async for chunk in resp.aiter_bytes():
                                fh.write(chunk)
                        partial.replace(dest)
                    finally:
                        if partial is not None:
                            partial.unlink(missing_ok=True)
            return dest

        return await self._request_with_retry(do_download)

    async def _run_op(
        self,
        op: str,
        path: str | None,
        params: dict,
        *,
        on,
        poll_interval: float,
    ) -> dict:
        payload = {"op": op, "path": path, "params": params}
        async with self._make_async_client() as client:
            # No retry on drops AFTER sending: POST /api/jobs is not idempotent and a lost
            # answer would start a second job. An unreachable runner (the request never left)
            # is waited out by _wait_out_restart. Polling below (GET) is safe.
            resp = await self._wait_out_restart(
                lambda: client.post("/api/jobs", json=payload), on=on
            )
            self._raise_for_status(resp)
            job_id: str = resp.json()["id"]
            job = await self._wait_job(client, job_id, on=on, poll_interval=poll_interval)
            if job is None:
                raise MediaError(_("The job result is no longer available on the server"))
            if job["status"] == "error":
                raise MediaError(job.get("error") or _("The operation failed"))
            return job["result"]

    async def _wait_job(self, client, job_id: str, *, on, poll_interval: float) -> dict | None:
        global _active_job
        _active_job = {
            "base_url": self._base_url,
            "headers": dict(self._headers),
            "verify": self._verify,
            "job_id": job_id,
        }

        down_since: float | None = None  # the first gateway failure (the runner restart window)
        while True:

            async def poll_status(job_id: str = job_id) -> dict | None:
                resp = await client.get(f"/api/jobs/{job_id}")
                if resp.status_code == 404:
                    return None
                self._raise_for_status(resp)
                return resp.json()

            try:
                job = await self._request_with_retry(poll_status)
                down_since = None
            except (MediaError, httpx.TransportError) as exc:
                # A runner restart (deploy) does not end the poll: wait for it to return.
                # Transport drops (ConnectError/ReadTimeout while it restarts) count as 502.
                # The returning runner closes the interrupted job itself (sweep).
                if isinstance(exc, MediaError) and not (
                    isinstance(exc, ServerError) and exc.status in _GATEWAY_ERRORS
                ):
                    raise
                now = asyncio.get_event_loop().time()
                down_since = down_since if down_since is not None else now
                if now - down_since > _RESTART_GRACE:
                    raise
                on(None, _("Server unavailable, waiting…"))
                await asyncio.sleep(2.0)
                continue
            if job is None:
                _active_job = None
                return None
            status = job.get("status")

            on(job.get("progress"), job.get("message", ""))

            if status == "done":
                _active_job = None
                return job
            if status == "error":
                _active_job = None
                return job

            await asyncio.sleep(poll_interval)

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        """Raises ServerError on an HTTP error: the server's detail is already user-facing."""
        if resp.is_error:
            code = resp.status_code
            if code in _GATEWAY_ERRORS:  # the gateway's answer while the server restarts
                raise ServerError(_(GATEWAY_DOWN), code)
            try:
                detail = resp.json().get("detail")
            except (ValueError, AttributeError):
                detail = None
            text = detail.strip()[:300] if isinstance(detail, str) and detail.strip() else ""
            raise ServerError(text or _("The server returned error {code}", code=code), code)
