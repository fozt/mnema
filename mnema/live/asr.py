"""Live text on Parakeet, phrase by phrase: PCM → silero (core/vad.py) → a phrase closes on a
pause of 0.6 s or at 15 s → Parakeet Ultra on ONNX (core/parakeet.py) → committed.

A phrase is decoded together with the audio of its track before it (up to 10 s, less at the
start): alone, a short phrase came out in the wrong language every third time. Only the words
of the phrase are shown: the border is the middle of the pause between it and the previous one
(a word started before it belongs to the previous phrase).

One session is one process (live/worker.py runs PhraseTranscriber); StreamingTranscriber is its
facade in the parent: the recording comes first, PCM waits in a bounded prebuffer until the
model is loaded, and finish/close give all the memory back to the OS.
"""

from __future__ import annotations

import contextvars
import threading
from collections import deque
from typing import Any

from ..core.asr_engine import REPLY_GAP_S, group_speech
from ..core.base import MediaError
from ..core.models import Segment
from ..core.settings import ASRThreads, RuntimeSettings
from ..i18n import _
from .models import LiveChunk

SR = 16000
PHRASE_MAX_S = 15.0  # as the recognition window of a track: shorter is no better
CUT_SEARCH_S = 2.0  # a phrase at its limit is cut at the quietest point of its last seconds
TAIL_S = 10.0  # the track's audio before a phrase, decoded with it
DRAFT = "…"  # while speech is heard and not yet recognized
NOT_DOWNLOADED = (
    "The recognition model isn't on the server yet. Process any recording on the server, "
    "then try again"
)

# PCM16 mono 16 kHz (the AudioSource contract): 32000 bytes per second of audio
_PCM_BYTERATE = SR * 2


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


class PhraseTranscriber:
    """One track's live text inside the session process: feed PCM, poll the phrases.

    Sample positions count from the first fed PCM. The region is what silero looks at for the
    next phrase: from the end of the previous one; the audio is kept from TAIL_S before it.
    """

    def __init__(
        self, *, lang: str | None = None, label: str = "mic", threads: ASRThreads | None = None
    ) -> None:
        self._lang = lang
        self._label = label
        self._threads = RuntimeSettings().asr_threads if threads is None else threads
        self._model: Any = None
        self._vad: Any = None
        self._audio: Any = None  # float32 samples from self._base on
        self._base = 0
        self._fed = 0  # samples fed so far
        self._region = 0  # the first window of the region
        self._probs: list[float] = []  # speech probabilities of the region's windows
        self._prev_end: int | None = None  # where the previous phrase ended
        self._committed: list[Segment] = []
        self._draft = ""
        self._odd = b""  # PCM16 is a byte stream: a chunk may end in the middle of a sample

    def warmup(self) -> None:
        """Load the model; the first download is the batch jobs' business, not a live wait."""
        import numpy as np

        from ..core import parakeet, vad
        from ..core.settings import asr_thread_budget

        if not parakeet.downloaded():
            raise MediaError(_(NOT_DOWNLOADED))
        with asr_thread_budget(self._threads):
            self._model = parakeet.model()
        self._vad = vad.Stream()
        self._audio = np.zeros(0, np.float32)

    def feed(self, pcm: bytes) -> None:
        import numpy as np

        from ..core import vad

        pcm = self._odd + pcm
        whole = len(pcm) - len(pcm) % 2
        self._odd = pcm[whole:]
        samples = np.frombuffer(pcm[:whole], dtype="<i2").astype(np.float32) / 32768
        self._audio = np.concatenate([self._audio, samples])
        self._fed += len(samples)
        while self._vad_at() + vad.WINDOW <= self._fed:
            self._listen(vad.WINDOW)
        self._phrases(final=False)

    def poll(self) -> LiveChunk:
        chunk = LiveChunk(committed=self._committed, draft=self._draft)
        self._committed = []
        return chunk

    def finish(self) -> LiveChunk:
        """The end of the stream: the open phrase is recognized too."""
        if self._vad_at() < self._fed:
            self._listen(self._fed - self._vad_at())
        self._phrases(final=True)
        self._draft = ""
        return self.poll()

    # ── phrases ──

    def _vad_at(self) -> int:
        from ..core import vad

        return (self._region + len(self._probs)) * vad.WINDOW

    def _listen(self, size: int) -> None:
        start = self._vad_at() - self._base
        self._probs.append(self._vad.window(self._audio[start : start + size]))

    def _phrases(self, *, final: bool) -> None:
        from ..core import vad

        while (phrase := self._next(final=final)) is not None:
            start, end = phrase
            if end > start:
                self._recognize(start, end)
                self._prev_end = end
            self._move(max(end // vad.WINDOW, self._region + 1))
        heard = bool(self._spans(final=final))
        if not heard:
            # silence: the region keeps a second, enough for speech that is only beginning
            self._move(self._region + len(self._probs) - SR // vad.WINDOW)
        self._draft = DRAFT if heard else ""

    def _spans(self, *, final: bool) -> list[tuple[int, int]]:
        """Speech of the region (samples from its start), as silero's get_speech_timestamps."""
        from ..core import vad

        samples = self._fed - self._region * vad.WINDOW if final else len(self._probs) * vad.WINDOW
        return [(x["start"], x["end"]) for x in vad.timestamps(self._probs, samples)]

    def _next(self, *, final: bool) -> tuple[int, int] | None:
        """The next closed phrase (stream samples): speech joined across pauses up to 0.6 s,
        closed by a longer pause, the end of the stream or the limit."""
        from ..core import vad

        spans = self._spans(final=final)
        if not spans:
            return None
        gap, limit = int(REPLY_GAP_S * SR), int(PHRASE_MAX_S * SR)
        groups = group_speech(spans, limit, gap)
        start, end = groups[0]
        base = self._region * vad.WINDOW
        heard = len(self._probs) * vad.WINDOW
        if end - start > limit:  # one speech span past the limit, maybe still going
            end = self._quietest(start + limit - int(CUT_SEARCH_S * SR), start + limit)
        elif not (final or len(groups) > 1 or heard - end > gap + vad.WINDOW):
            return None  # the speech may still go on after a short pause
        return max(base + start, self._prev_end or 0), base + end

    def _quietest(self, lo: int, hi: int) -> int:
        """The start of the window with the lowest speech probability in [lo, hi) (region)."""
        from ..core import vad

        first, last = -(-lo // vad.WINDOW), min(hi // vad.WINDOW, len(self._probs))
        window = min(range(first, last), key=lambda i: self._probs[i], default=last)
        return window * vad.WINDOW

    def _recognize(self, start: int, end: int) -> None:
        from ..core import parakeet

        tail = max(self._base, start - int(TAIL_S * SR))
        border = tail
        if self._prev_end is not None:
            border = start - min((start - self._prev_end) // 2, start - tail)
        audio = self._audio[tail - self._base : end - self._base]
        words = [
            w for w in self._model.words(audio, offset=tail / SR) if w.start >= border / SR - 1e-6
        ]
        if not words:
            return
        text = " ".join(w.text for w in words)
        phrase = Segment(start=words[0].start, end=words[-1].end, text=text, speaker=self._label)
        for seg in parakeet.tidy([phrase], self._lang):
            # a leading space: the session joins the pieces as is
            self._committed.append(seg.model_copy(update={"text": " " + seg.text}))

    def _move(self, window: int) -> None:
        """Start the region at this window; the audio is kept from TAIL_S before it."""
        from ..core import vad

        if window > self._region:
            del self._probs[: window - self._region]
            self._region = window
        keep = max(self._base, self._region * vad.WINDOW - int(TAIL_S * SR))
        if keep > self._base:
            self._audio = self._audio[keep - self._base :]
            self._base = keep


class StreamingTranscriber:
    """The parent side of one live session process (live/worker.py).

    The parent keeps only the bounded PCM prebuffer and segments not yet returned; the model,
    onnxruntime and numpy live in the child.
    """

    def __init__(
        self, *, lang: str | None = None, label: str = "mic", threads: ASRThreads | None = None
    ) -> None:
        self._lang = lang
        self._label = label
        self._threads = RuntimeSettings().asr_threads if threads is None else threads
        self._prebuf = PcmPrebuffer()  # PCM during warmup: feed does not wait for the model
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._error: Exception | None = None
        self._loaded = False  # the model answered ready: later errors are a broken stream
        self._abort = threading.Event()
        self._finish = threading.Event()
        self._pending = LiveChunk()
        self._worker: Any = None
        self._thread: threading.Thread | None = None

    def start_warmup(self) -> None:
        """Start the process in the background without blocking; see the ready property."""
        if self._thread is not None or self._abort.is_set():
            return
        # the worker process inherits the language of the caller (a /api/live request)
        context = contextvars.copy_context()
        self._thread = threading.Thread(
            target=context.run, args=(self._process,), name="mnema-live-process", daemon=True
        )
        self._thread.start()

    @property
    def ready(self) -> bool:
        """The model is loaded and the process alive: feed goes to recognition."""
        return self._ready.is_set() and self._error is None

    @property
    def fed_offset_seconds(self) -> float:
        """Offset of ASR time against the source's stream: when the warmup prebuffer dropped old
        PCM, t=0 of the fed stream is later than the start of the source's chronicle."""
        return self._prebuf.dropped_seconds

    def _process(self) -> None:
        import base64

        from ..core.worker import Worker

        try:
            with Worker("mnema.live.worker") as worker:
                self._worker = worker
                if self._abort.is_set():
                    return
                worker.send({"lang": self._lang, "label": self._label, "threads": self._threads})
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
                        self._loaded = True
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

    def _raise(self) -> None:
        error = self._error
        if error is None:
            return
        if not self._loaded:  # the model did not load: its reason as is
            raise MediaError(str(error)) from error
        raise MediaError(_("Recognition stream failed: {error}", error=error)) from error

    def warmup(self, timeout: float = 120.0) -> None:
        """Blocking warmup, for when readiness is needed before speech starts (scripts, tests)."""
        try:
            self.start_warmup()
            if not self._ready.wait(timeout=timeout):
                raise MediaError(
                    _(
                        "The recognition model did not start in {minutes} min. Try again later",
                        minutes=f"{timeout / 60:.0f}",
                    )
                )
            self._raise()
        except BaseException:
            self.close()
            raise

    def feed(self, pcm: bytes) -> None:
        self._raise()
        if self._finish.is_set() or self._abort.is_set():
            return
        self.start_warmup()
        with self._lock:
            self._prebuf.push(pcm)

    def poll(self) -> LiveChunk:
        self._raise()
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
