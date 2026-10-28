"""Audio sources for live mode: a pluggable abstraction, like core.backends.TranscriptionBackend.

This module has MicSource (the microphone via sounddevice); system audio is in system_audio.py
(a Core Audio tap on macOS, parec on Linux). The source label is the speaker: live mode tells
speakers apart by source, not by diarization.
"""

from __future__ import annotations

import logging
import math
import platform
import queue
import time
from array import array
from collections import deque
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from ..core.base import MediaError
from ..i18n import _

logger = logging.getLogger(__name__)


@runtime_checkable
class AudioSource(Protocol):
    """Source contract: blocks of PCM16 mono 16 kHz under the source's label.

    read() returns the next block; b"" means no data yet (transient), None means the source has
    ended (after stop and draining the queue). Optional attributes, read by LiveSession through
    getattr: dropped_seconds (float, cumulative), the seconds of audio the source dropped before
    delivering them; speech_energy(start, end), the peak RMS of the delivered stream over the
    interval (used to filter hallucinations on silence).
    """

    name: str

    def available(self) -> bool: ...
    def start(self) -> None: ...
    def read(self) -> bytes | None: ...
    def stop(self) -> None: ...


def pcm_rms(pcm: bytes) -> float:
    """RMS of a PCM16 mono block. Plain Python (array), about a millisecond per second of audio."""
    n = len(pcm) // 2
    if not n:
        return 0.0
    a = array("h")
    a.frombytes(pcm[: n * 2])
    return math.sqrt(sum(x * x for x in a) / n)


# Energy chronicle window: the silence filter needs the last tens of seconds (ASR commit lag);
# 5 minutes is a generous margin, older history is trimmed.
_ENERGY_WINDOW = 300.0


class EnergyChronicle:
    """Energy chronicle of the delivered stream: (t_start, t_end, rms) for every delivered block.

    Time is seconds since the first delivered block, the same stream and coordinates ASR sees.
    Frames dropped by backpressure before delivery are not recorded and do not advance time,
    exactly as in the fed stream, so the coordinates match ASR. Used from one session thread
    (push in read(), energy in _emit), so no lock is needed.
    """

    def __init__(self, *, byterate: int, window: float = _ENERGY_WINDOW) -> None:
        self._byterate = byterate
        self._window = window
        self._entries: deque[tuple[float, float, float]] = deque()
        self._pos = 0.0  # seconds of stream delivered so far

    def push(self, pcm: bytes) -> None:
        if not pcm:
            return
        t0 = self._pos
        self._pos = t1 = t0 + len(pcm) / self._byterate
        self._entries.append((t0, t1, pcm_rms(pcm)))
        floor = t1 - self._window
        while self._entries and self._entries[0][1] < floor:
            self._entries.popleft()

    def energy(self, start: float, end: float) -> float | None:
        """Peak RMS of the blocks overlapping [start, end]; None outside the chronicle."""
        best: float | None = None
        for t0, t1, rms in self._entries:  # ordered by time
            if t1 <= start:
                continue
            if t0 >= end:
                break
            best = rms if best is None else max(best, rms)
        return best


# The queue holds about 30 s of audio: if ASR or the network falls behind, buffering more only
# grows the delay. On overflow the oldest frames are dropped (live speech matters more than a
# stale tail); the loss is reported through dropped_seconds.
_QUEUE_SECONDS = 30

# Reopening the microphone: when the device disappears mid-call (Bluetooth headphones ran out),
# the PortAudio stream dies and does not come back. Reopen on the configured device, then on the
# current default (with the headphones gone, the default is the built-in microphone).
_REOPEN_INTERVAL = 2.0  # seconds between reopen attempts
_REOPEN_BUDGET = 60.0  # seconds since the stream died; after that the source ends


class MicUnavailable(MediaError):
    """The microphone did not open even after refreshing the device list."""


def mic_missing_text() -> str:
    """What to do when there is no microphone or it did not open, in this system's terms."""
    if platform.system() != "Darwin":
        return _("Microphone not found. Connect one and try again")
    from .system_audio import in_mnema_app

    if in_mnema_app():
        return _(
            "Microphone not found. Connect one or allow Mnema to use the microphone in System "
            "Settings"
        )
    return _(
        "Microphone not found. Connect one or allow the terminal to use the microphone in System "
        "Settings"
    )


def _refresh_portaudio() -> None:
    """Reload PortAudio's device list (headphones plugged in or out). Affects only PortAudio
    streams, which here is just the microphone; system audio uses a Core Audio tap or parec."""
    try:
        import sounddevice as sd

        sd._terminate()
        sd._initialize()
    except Exception:  # noqa: BLE001 - best-effort
        pass


class MicSource:
    """The microphone via sounddevice.RawInputStream (16 kHz, mono, int16).

    The stream callback puts frames in a queue and read() takes them. sounddevice is imported
    lazily so the package imports without it (thin client). The recording comes first: every
    frame goes to `tee` before the dropping queue, so the WAV track is complete even under ASR
    backpressure.

    A dead stream (finished_callback, an inactive stream) does not end the source: read() returns
    b"" and tries to reopen the stream every _REOPEN_INTERVAL within _REOPEN_BUDGET; the new
    stream writes to the same tee, so the WAV track continues. The outcome is in `notice`, which
    the session reads through getattr and marks in the transcript.
    """

    def __init__(
        self,
        device: int | str | None = None,
        *,
        samplerate: int = 16000,
        blocksize: int = 1600,  # 100 ms at 16 kHz
        label: str = "mic",
        tee: Callable[[bytes], None] | None = None,
        clock: Callable[[], float] = time.monotonic,  # injectable for tests of reopening
    ) -> None:
        self.name = label
        self._device = device
        self._samplerate = samplerate
        self._blocksize = blocksize
        self._tee = tee
        self.recording_error: str | None = None
        self._clock = clock
        maxblocks = max(1, int(_QUEUE_SECONDS * samplerate / blocksize))
        self._queue: queue.Queue[bytes | None] = queue.Queue(maxsize=maxblocks)
        self._dropped_bytes = 0
        self._chronicle = EnergyChronicle(byterate=samplerate * 2)  # PCM16: 2 bytes per sample
        self._stream = None
        self._stopped = False
        self.notice = ""  # one-off message to the user; the session announces each change
        self._dead = False
        self._died_at = 0.0
        self._reopen_last = 0.0
        self._reopen_count = 0
        self._gen = 0  # stream generation: an old stream's finished_callback leaves the new one

    @property
    def dropped_seconds(self) -> float:
        """Seconds of audio dropped because the queue overflowed (PCM16 mono)."""
        return self._dropped_bytes / (self._samplerate * 2)

    def speech_energy(self, start: float, end: float) -> float | None:
        """Peak RMS of the delivered stream over [start, end] seconds (ASR stream time)."""
        return self._chronicle.energy(start, end)

    def _put_dropping_oldest(self, item: bytes | None) -> None:
        """Enqueue without blocking; on overflow drop the oldest frames."""
        while True:
            try:
                self._queue.put_nowait(item)
                return
            except queue.Full:
                try:
                    old = self._queue.get_nowait()
                    if old is not None:
                        self._dropped_bytes += len(old)
                except queue.Empty:  # raced with the reader: there is room now, put again
                    pass

    def available(self) -> bool:
        try:
            import sounddevice as sd
        except Exception:
            return False
        try:
            return any(d.get("max_input_channels", 0) > 0 for d in sd.query_devices())
        except Exception:
            return False

    def start(self) -> None:
        """Open the microphone. PortAudio keeps the device list from initialization, so after
        switching headphones or AirPods opening fails (-9986); refresh the list and try again
        (the configured device, then the current default)."""
        try:
            self._stream = self._open_stream(self._device)
            self._stream.start()
            return
        except Exception as first:  # noqa: BLE001 - second chance below
            error: Exception = first
        _refresh_portaudio()
        for device in [self._device, None] if self._device is not None else [None]:
            try:
                self._stream = self._open_stream(device)
                self._stream.start()
                return
            except Exception as e:  # noqa: BLE001
                error = e
        raise MicUnavailable(mic_missing_text()) from error

    def _open_stream(self, device: int | str | None):  # noqa: ANN202 - lazy sounddevice type
        import sounddevice as sd

        self._gen += 1
        gen = self._gen

        def _callback(indata, frames, time, status) -> None:  # noqa: ANN001 - sounddevice contract
            # errors are not raised into the audio thread; the frame is queued as is
            data = bytes(indata)
            if self._tee is not None:
                try:
                    self._tee(data)
                except Exception:  # noqa: BLE001 - the session stops capture outside the callback
                    from .remarks import track_stopped

                    logger.warning("track %s is not being written", self.name, exc_info=True)
                    self.recording_error = self.recording_error or track_stopped(self.name)
            self._put_dropping_oldest(data)

        def _finished() -> None:
            # the stream died on its own (device gone, PortAudio error): not our stop(), not an
            # earlier stream
            if not self._stopped and gen == self._gen:
                self._mark_dead()

        return sd.RawInputStream(
            samplerate=self._samplerate,
            blocksize=self._blocksize,
            channels=1,
            dtype="int16",
            device=device,
            callback=_callback,
            finished_callback=_finished,
        )

    def read(self) -> bytes | None:
        """All queued frames at once, like the system source: one 100 ms block per visit does not
        drain the queue when the session loop shares turns between sources (the other source's
        blocking read() takes up to 0.5 s in silence), and the overflow reported false gaps."""
        if self._dead and not self._stopped:
            self._try_reopen()
        try:
            first = self._queue.get(timeout=0.5)
        except queue.Empty:
            if self._stopped:
                return None
            # silence plus an inactive stream: died without finished_callback
            if not self._dead and self._stream_died():
                self._mark_dead()
            return b""
        if first is None:
            return None
        self._chronicle.push(first)  # the chronicle follows what is delivered, block by block
        parts = [first]
        while True:
            try:
                nxt = self._queue.get_nowait()
            except queue.Empty:
                break
            if nxt is None:
                # stop sentinel: deliver what was collected; _stopped is already True (stop()
                # sets it before the sentinel), so the next read() returns None
                break
            self._chronicle.push(nxt)
            parts.append(nxt)
        return b"".join(parts)

    def _stream_died(self) -> bool:
        """The stream stopped by itself (device gone, PortAudio error)."""
        s = self._stream
        return s is None or bool(getattr(s, "stopped", False)) or not getattr(s, "active", True)

    def _mark_dead(self) -> None:
        if self._dead:
            return
        self._dead = True
        self._died_at = self._clock()
        self._reopen_last = float("-inf")  # the first attempt is on the next read()

    def _try_reopen(self) -> None:
        """Reopen at most every _REOPEN_INTERVAL; once the budget is spent the source ends."""
        now = self._clock()
        if now - self._reopen_last < _REOPEN_INTERVAL:
            return
        self._reopen_last = now
        if now - self._died_at > _REOPEN_BUDGET:
            self.notice = _("[the microphone did not come back within ~60 s, track stopped]")
            self._stopped = True
            self._close_stream()
            self._put_dropping_oldest(None)
            return
        self._close_stream()
        _refresh_portaudio()  # a newly connected device is visible only after this
        # the configured device is usually gone; the current default is tried after it
        for device in [self._device, None] if self._device is not None else [None]:
            try:
                stream = self._open_stream(device)
                stream.start()
            except Exception:  # noqa: BLE001 - no device yet, retry after the interval
                continue
            self._stream = stream
            self._dead = False
            self._reopen_count += 1
            name = self._device_name(device)
            if self._reopen_count > 1:
                self.notice = _(
                    "[microphone reopened ×{count}: {name}, some speech may be missing]",
                    count=self._reopen_count,
                    name=name,
                )
            else:
                self.notice = _(
                    "[microphone reopened: {name}, some speech may be missing]", name=name
                )
            return

    def _device_name(self, device: int | str | None) -> str:
        try:
            import sounddevice as sd

            info = sd.query_devices(device, "input")
            return str(info["name"])
        except Exception:  # noqa: BLE001 - the device name is decoration only
            return _("default device") if device is None else str(device)

    def _close_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.stop()
            stream.close()
        except Exception:  # noqa: BLE001 - a dead stream may not close cleanly
            pass

    def stop(self) -> None:
        self._stopped = True
        self._close_stream()
        self._put_dropping_oldest(None)  # end sentinel, without blocking on a full queue
