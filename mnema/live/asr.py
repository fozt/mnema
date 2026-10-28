"""Adapter over WhisperLiveKit: a synchronous feed/poll/finish facade producing LiveChunk.

The contract, as observed on a real Mac:
- TranscriptionEngine(model_size=..., backend="mlx-whisper", backend_policy="simulstreaming",
  lan=...); AudioProcessor(transcription_engine=...); AlignAtt with mlx works.
- create_tasks() returns an async generator of FrontData; process_audio(bytes | None) feeds it
  (it expects a container, not raw PCM); FrontData.lines holds Segment(speaker=-1) and
  SilentSegment(speaker=-2, skipped); AlignAtt commits append-only (text grows, no revisions);
  buffer_transcription is the draft.

The recording comes first: the model warms up in the background (start_warmup) and feed never
waits for it; until then PCM collects in a bounded prebuffer (PcmPrebuffer) and is pumped into ASR
once it is ready.

The mapping (_extract/_to_chunk) is pure and unit tested. The async facade (background event loop
plus a WAV wrapper around PCM) is checked with a real microphone.
"""

from __future__ import annotations

import contextvars
import struct
import threading
from collections import deque
from typing import Any

from ..core.base import MediaError
from ..core.models import Segment
from ..core.settings import ASRThreads, RuntimeSettings
from ..i18n import _
from .models import LiveChunk

_SILENCE_SPEAKER = -2  # WLK marks SilentSegment.speaker = -2


def _extract(front: Any) -> tuple[str, float, str]:
    """FrontData -> (committed text of the real segments, end of the last one, draft tail)."""
    parts: list[str] = []
    end = 0.0
    for ln in getattr(front, "lines", None) or []:
        if getattr(ln, "speaker", None) == _SILENCE_SPEAKER:
            continue
        text = getattr(ln, "text", "") or ""
        if text.strip():
            parts.append(text)
            end = float(getattr(ln, "end", 0.0) or 0.0)
    draft = getattr(front, "buffer_transcription", "") or ""
    return "".join(parts), end, draft


# live deltas are words or phrases (a couple of sentences at most on the final flush); a larger
# "delta" is not speech but history rewritten by WLK
_MAX_DELTA = 2000


def _overlap(tail: str, head: str) -> int:
    """Largest k such that head[:k] is a suffix of tail (KMP prefix function, O(n))."""
    s = head + "\x00" + tail
    pi = [0] * len(s)
    for i in range(1, len(s)):
        k = pi[i - 1]
        while k and s[i] != s[k]:
            k = pi[k - 1]
        if s[i] == s[k]:
            k += 1
        pi[i] = k
    return pi[-1]


def _delta_text(emitted: str, full: str) -> str | None:
    """The new suffix of `full` after the already emitted text; None when out of sync.

    WLK text normally grows append-only. When WLK trims its context ("Trimming context"), `full`
    starts with the tail of `emitted` instead, so the longest overlap of that tail with the head
    of `full` is found (KMP, unbounded: on a long session the kept tail can be thousands of
    characters) and only the continuation is returned. No overlap, or a session-sized "delta",
    means WLK rewrote its history; emitting it again would flood the output and the translation
    on every poll, so None tells the caller to resync (emitted = full) without emitting.
    """
    if not full:
        return ""  # an empty front (silence) is neither a resync nor new text
    if full.startswith(emitted):
        return full[len(emitted) :]
    k = _overlap(emitted[-len(full) :], full)
    if k and len(full) - k <= _MAX_DELTA:
        return full[k:]
    return None


def _wav_stream_header(rate: int = 16000, ch: int = 1, bits: int = 16) -> bytes:
    """Header of a streaming WAV (huge data size): ffmpeg reads the PCM stream until EOF."""
    big = 0x7FFFFFFF
    byte_rate = rate * ch * bits // 8
    block = ch * bits // 8
    return (
        b"RIFF"
        + struct.pack("<I", 36 + big)
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, ch, rate, byte_rate, block, bits)
        + b"data"
        + struct.pack("<I", big)
    )


# WLK decoding is not thread safe across sessions: TranscriptionEngine is a process singleton
# (shared torch model) and upstream recommends one connection per process
# (thread_safety.RECOMMENDED_CONNECTIONS_PER_WORKER = 1). With two sources (--system) decoding is
# serialized by a shared lock: concurrent calls broke decoding (RuntimeError in qkv_attention)
# and WLK swallowed the error, so the source just went silent. The lock is reentrant because
# process_iter calls model.infer itself.
_DECODE_LOCK = threading.RLock()
# Engine initialization lock (model loading): concurrent mic and system warmup raced inside tqdm
# (see _pipeline). Only creation is serialized, not inference.
_ENGINE_INIT_LOCK = threading.Lock()


def _serialize_decode(online: Any) -> None:
    """Wrap the decode entry points of the WLK online processor in the cross-session lock.

    process_iter is the main entry (transcription_processor); model.infer is the final flush of
    audio_processor on stop, which bypasses process_iter and needs the lock too.
    """

    def locked(fn: Any) -> Any:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with _DECODE_LOCK:
                return fn(*args, **kwargs)

        return wrapper

    online.process_iter = locked(online.process_iter)
    model = getattr(online, "model", None)
    if model is not None and hasattr(model, "infer"):
        model.infer = locked(model.infer)


# PCM16 mono 16 kHz (the AudioSource contract): 32000 bytes per second of audio
_PCM_BYTERATE = 16000 * 2


class PcmPrebuffer:
    """Bounded PCM prebuffer while ASR warms up; audio older than the limit is dropped.

    The recording stays complete (the source's tee); a very long warmup only loses the transcript
    of the beginning. Used from one session thread (feed/finish), so no lock is needed.
    """

    def __init__(self, seconds: float = 60.0) -> None:
        self._limit = int(seconds * _PCM_BYTERATE)
        self._chunks: deque[bytes] = deque()
        self._size = 0
        self._dropped = 0

    @property
    def dropped_seconds(self) -> float:
        """Seconds of PCM dropped (warmup longer than the limit): the stream fed to ASR starts
        this much later than the one the source delivered."""
        return self._dropped / _PCM_BYTERATE

    def push(self, pcm: bytes) -> None:
        self._chunks.append(pcm)
        self._size += len(pcm)
        while self._size > self._limit and len(self._chunks) > 1:
            old = self._chunks.popleft()
            self._size -= len(old)
            self._dropped += len(old)

    def drain(self) -> list[bytes]:
        chunks = list(self._chunks)
        self._chunks.clear()
        self._size = 0
        return chunks


class _InProcessTranscriber:
    """The engine inside the child process. The parent uses StreamingTranscriber below."""

    def __init__(
        self,
        *,
        lang: str | None = None,
        backend: str = "mlx-whisper",
        policy: str = "simulstreaming",
        model: str = "large-v3-turbo",
        label: str = "mic",
        threads: ASRThreads | None = None,
    ) -> None:
        self._lang = lang
        self._backend = backend
        self._policy = policy
        self._model = model
        self._label = label
        self._threads = RuntimeSettings().asr_threads if threads is None else threads
        self._loop: Any = None
        self._thread: threading.Thread | None = None
        self._ap: Any = None
        self._latest: Any = None
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._error: Exception | None = None
        self._wav_sent = False
        self._prebuf = PcmPrebuffer()  # PCM during warmup: feed does not wait for the model
        # state of the append-only mapping
        self._emitted_text = ""
        self._emitted_end = 0.0

    # ── pure mapping (unit tested) ─────────────────────────────────────────────

    def _to_chunk(self, front: Any) -> LiveChunk:
        full, end, draft = _extract(front)
        delta = _delta_text(self._emitted_text, full)
        if delta is None:  # out of sync: resync without emitting again
            self._emitted_text = full
            self._emitted_end = end
            return LiveChunk(draft=draft.strip())
        committed: list[Segment] = []
        if delta.strip():
            # keep WLK's raw text with its own leading spaces; the session joins pieces as is,
            # otherwise a word split between commits gets an extra space inside it
            committed = [Segment(start=self._emitted_end, end=end, text=delta, speaker=self._label)]
            self._emitted_text += delta  # whole session; the overlap is searched in its tail
            self._emitted_end = end
        return LiveChunk(committed=committed, draft=draft.strip())

    # ── async facade (checked with a real microphone) ─────────────────────────

    def start_warmup(self) -> None:
        """Start the pipeline in the background without blocking; see the `ready` property.

        Until it is ready, feed collects PCM in the prebuffer, so neither the recording nor the
        session waits for the model.
        """
        if self._thread is not None:
            return
        import asyncio

        try:
            import whisperlivekit as wlk  # ty: ignore[unresolved-import]
        except Exception as e:  # noqa: BLE001
            raise MediaError(
                _(
                    "WhisperLiveKit is missing. Reinstall Mnema (./install.sh client or "
                    "./install.sh server)"
                )
            ) from e
        # WLK loggers spam INFO/WARNING to stderr (where rich.Live draws); setLevel on the parent
        # does not silence children with their own handlers, so disable globally up to WARNING
        import logging

        logging.disable(logging.WARNING)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, args=(wlk,), daemon=True)
        self._thread.start()

    @property
    def ready(self) -> bool:
        """The model is warm and the pipeline alive: feed goes to ASR, not to the prebuffer."""
        return self._ready.is_set() and self._error is None

    @property
    def fed_offset_seconds(self) -> float:
        """Offset of ASR time against the source's stream: when the warmup prebuffer dropped old
        PCM, t=0 of the fed stream is later than the start of the source's chronicle."""
        return self._prebuf.dropped_seconds

    def warmup(self) -> None:
        """Blocking warmup, for when readiness is needed before speech starts (scripts, runner)."""
        self.start_warmup()
        if not self._ready.wait(timeout=120):
            raise MediaError(_("WhisperLiveKit did not start: the model warmup timed out."))
        if self._error is not None:  # initialization failed: fail fast with the real cause
            raise MediaError(
                _("WhisperLiveKit did not start: {error}", error=self._error)
            ) from self._error

    def _run(self, wlk: Any) -> None:
        import asyncio

        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._pipeline(wlk))
        except Exception as e:  # noqa: BLE001 - reported to the main thread
            self._error = e
        finally:
            self._ready.set()  # unblocks warmup on error (success sets it in _pipeline)
            # cancel WLK background tasks (watchdog etc.) before close, or asyncio warns
            # "Task was destroyed but it is pending!" on exit
            try:
                pending = [t for t in asyncio.all_tasks(self._loop) if not t.done()]
                for t in pending:
                    t.cancel()
                if pending:
                    self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:  # noqa: BLE001 - teardown must not break the session
                pass
            self._loop.close()

    async def _pipeline(self, wlk: Any) -> None:
        import torch

        from ..core.backends import _asr_cpu_threads
        from ..core.settings import asr_thread_budget

        with asr_thread_budget(self._threads):
            budget = _asr_cpu_threads()
        torch.set_num_threads(budget)
        # engine initialization is serialized (_ENGINE_INIT_LOCK): mic and system warm up on
        # their own threads, and concurrent model loading raced on tqdm's class lock
        # ("type object 'tqdm' has no attribute '_lock'"). The second load comes from the
        # cache, so waiting costs no time.
        with _ENGINE_INIT_LOCK:
            engine = wlk.TranscriptionEngine(
                model_size=self._model,
                backend=self._backend,
                backend_policy=self._policy,
                lan=self._lang or "auto",  # None -> auto (otherwise WLK may assume English)
                # the simulstreaming audio buffer stays at its default (30 s): an A/B test with
                # continuous speech showed a 15 s window does not smooth decode peaks (the max
                # pause got worse) and loses letters; buffer_trimming* only affects
                # localagreement
            )
            self._ap = wlk.AudioProcessor(transcription_engine=engine)
        _serialize_decode(self._ap.transcription)
        results = await self._ap.create_tasks()  # coroutine -> async generator of FrontData
        self._ready.set()
        async for front in results:
            # empty status fronts do not overwrite the last meaningful one, otherwise the final
            # flush text would be lost when WLK sends an empty status after it
            if getattr(front, "lines", None) or getattr(front, "buffer_transcription", ""):
                with self._lock:
                    self._latest = front
        await self._ap.cleanup()  # stops WLK's watchdog and ffmpeg

    def feed(self, pcm: bytes) -> None:
        if self._error is not None:  # the pipeline died or warmup failed
            raise MediaError(_("Recognition stream failed: {error}", error=self._error)) from (
                self._error
            )
        self.start_warmup()
        if not self.ready:  # still warming up: buffer, pump once ready
            self._prebuf.push(pcm)
            return
        for chunk in [*self._prebuf.drain(), pcm]:
            self._send(chunk)

    def _send(self, pcm: bytes) -> None:
        import asyncio

        data = pcm
        if not self._wav_sent:  # the first piece carries a WAV header so ffmpeg sees a container
            data = _wav_stream_header() + pcm
            self._wav_sent = True
        try:
            asyncio.run_coroutine_threadsafe(self._ap.process_audio(data), self._loop)
        except RuntimeError:  # the loop has stopped; the _error check above reports why
            pass

    def poll(self) -> LiveChunk:
        with self._lock:
            front = self._latest
        return LiveChunk() if front is None else self._to_chunk(front)

    def finish(self) -> LiveChunk:
        import asyncio

        if self._ap is not None and self._loop is not None and self._error is None:
            # the model warmed up just before stop: the prebuffer tail still goes to ASR before EOF
            for chunk in self._prebuf.drain():
                self._send(chunk)
            try:
                fut = asyncio.run_coroutine_threadsafe(self._ap.process_audio(None), self._loop)
                fut.result(timeout=10)
            except Exception:  # noqa: BLE001
                pass
            if self._thread is not None:
                # after EOF WLK flushes the buffer tail into committed text asynchronously and ends
                # the generator; wait for the pipeline, or the last words are lost
                self._thread.join(timeout=30)
        with self._lock:
            front = self._latest
        return LiveChunk() if front is None else self._to_chunk(front)


class StreamingTranscriber(_InProcessTranscriber):
    """One live session is one process; finish/close give all its memory back to the OS.

    The parent keeps only the bounded PCM prebuffer and segments not yet returned.
    Initialization, WLK, torch/MLX and ffmpeg live in the child process.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._abort = threading.Event()
        self._finish = threading.Event()
        self._pending = LiveChunk()
        self._worker: Any = None

    def start_warmup(self) -> None:
        if self._thread is not None or self._abort.is_set():
            return
        # the worker process inherits the language of the caller (a /api/live request)
        context = contextvars.copy_context()
        self._thread = threading.Thread(
            target=context.run, args=(self._process,), name="mnema-live-process", daemon=True
        )
        self._thread.start()

    def _process(self) -> None:
        import base64

        from ..core.worker import Worker

        try:
            with Worker("mnema.live.worker") as worker:
                self._worker = worker
                if self._abort.is_set():
                    return
                worker.send(
                    {
                        "lang": self._lang,
                        "backend": self._backend,
                        "policy": self._policy,
                        "model": self._model,
                        "label": self._label,
                        "threads": self._threads,
                    }
                )
                ending = waiting = False
                while not self._abort.is_set():
                    if self.ready and not ending and not waiting:
                        with self._lock:
                            pcm = b"".join(self._prebuf.drain())
                        if pcm:
                            worker.send({"type": "feed", "pcm": base64.b64encode(pcm).decode()})
                            waiting = True
                        elif self._finish.is_set():
                            worker.send({"type": "finish"})
                            ending = True
                    event = worker.receive(timeout=0.05)
                    if event is None:
                        continue
                    kind = event.get("type")
                    if kind == "ready":
                        self._ready.set()
                    elif kind in ("chunk", "done"):
                        chunk = LiveChunk.model_validate(event["value"])
                        with self._lock:
                            self._pending.committed.extend(chunk.committed)
                            self._pending.draft = chunk.draft
                        waiting = False
                        if kind == "done":
                            break
                    elif kind == "error":
                        raise MediaError(event["message"])
                    else:
                        raise MediaError(_("Unknown reply from the live process"))
        except Exception as exc:  # the child's error surfaces in feed/poll/warmup
            if not self._abort.is_set():
                self._error = exc
        finally:
            self._ready.set()

    def warmup(self) -> None:
        try:
            super().warmup()
        except BaseException:
            self.close()
            raise

    def feed(self, pcm: bytes) -> None:
        if self._error is not None:
            raise MediaError(_("Recognition stream failed: {error}", error=self._error)) from (
                self._error
            )
        if self._finish.is_set() or self._abort.is_set():
            return
        self.start_warmup()
        with self._lock:
            self._prebuf.push(pcm)

    def poll(self) -> LiveChunk:
        if self._error is not None:
            raise MediaError(_("Recognition stream failed: {error}", error=self._error)) from (
                self._error
            )
        with self._lock:
            chunk = self._pending
            self._pending = LiveChunk(draft=chunk.draft)
        return chunk

    def close(self) -> None:
        """Interrupt warmup or decoding and wait for the child process to exit."""
        self._abort.set()
        if self._worker is not None:
            self._worker.close()
        if self._thread is not None:
            self._thread.join(timeout=10)

    def finish(self) -> LiveChunk:
        try:
            if self._thread is not None and self.ready:
                self._finish.set()
                self._thread.join(timeout=45)
                if self._thread.is_alive():
                    raise MediaError(_("Live recognition did not finish in time"))
            return self.poll()
        finally:
            self.close()
