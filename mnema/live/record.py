"""Recording of a live session. The recording comes first: audio is written from a tee of each
source's own PCM (WavTrackWriter, one WAV track per source, so mic and system are transcribed
separately), not by a second capture.
"""

from __future__ import annotations

import array
import logging
import threading
import wave
from pathlib import Path

logger = logging.getLogger(__name__)


_WAV_LIMIT = 0xFFFFFFFF - 44 - 2**20  # data length limit of the WAV header, with a margin


class WavTrackWriter:
    """Incremental WAV track of a source: PCM16 mono 16 kHz, exactly the stream ASR sees.

    write() is called by the tee callback on the capture thread, before the dropping queue, so the
    track is complete even under ASR backpressure; the wave module keeps the header length
    current and finalizes it on stop(). start()/stop() are the session's Recorder contract;
    write() before start() or after stop() is ignored (the capture callback can outlive the
    session by a frame).
    """

    def __init__(self, path: Path, *, samplerate: int = 16000) -> None:
        self.path = Path(path)
        self._samplerate = samplerate
        self._wav: wave.Wave_write | None = None
        self._lock = threading.Lock()
        self.level = 0.0  # level meter for the app: last frame level 0..1
        # paused: frames are dropped; all tracks pause together, so they stay in sync
        self.paused = False
        # source switched off mid-recording: silence of the same length is written, so the track
        # stays in sync and can be switched back on at any time
        self.muted = False
        self.live_bytes = 0  # bytes not written as mute silence; 0 means never switched on
        self.written = 0  # audio bytes in the file, mute silence included
        self.full = False  # reached the WAV limit (4 GB, about 37 h); nothing more is written
        self.error: str | None = None  # first write failure; the session stops all sources

    def start(self) -> None:
        with self._lock:
            if self._wav is not None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            w = wave.open(str(self.path), "wb")
            self._wav = w  # stop() closes even a half-configured file if start fails
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self._samplerate)

    def write(self, pcm: bytes) -> None:
        if self.paused:
            self.level = 0.0
            return
        muted = self.muted
        with self._lock:
            if self.error is not None:
                return
            if self._wav is not None and self.written + len(pcm) > _WAV_LIMIT:
                # the WAV header length is 32 bits and would overflow, corrupting the file;
                # stop writing here, the app then stops the recording (full)
                self.full = True
                return
            if self._wav is not None:
                try:
                    self._wav.writeframes(bytes(len(pcm)) if muted else pcm)
                except Exception as exc:  # noqa: BLE001 - reported to the session outside the callback
                    self._failed(exc)
                    return
                self.written += len(pcm)
                if not muted:
                    self.live_bytes += len(pcm)
        if muted:
            self.level = 0.0
            return
        self._meter(pcm)

    def _meter(self, pcm: bytes) -> None:
        """Frame peak from every 8th sample: enough for the meter, cheap on the capture thread."""
        if len(pcm) < 2:
            return
        samples = array.array("h")
        samples.frombytes(pcm[: len(pcm) - len(pcm) % 2])
        peak = max((abs(x) for x in samples[::8]), default=0)
        self.level = min(peak / 32768.0 * 4.0, 1.0)  # x4: laptop mic speech is about 0.1-0.3 FS

    def stop(self) -> None:
        with self._lock:
            wav, self._wav = self._wav, None
            if wav is not None:
                try:
                    wav.close()  # finalizes the header length
                except Exception as exc:  # noqa: BLE001 - the other tracks are closed too
                    self._failed(exc, closing=True)

    def _failed(self, exc: Exception, *, closing: bool = False) -> None:
        if self.error is None:
            from .remarks import FILES_NOT_SAVED, track_stopped

            logger.warning("%s was not written", self.path.name, exc_info=exc)
            self.error = FILES_NOT_SAVED if closing else track_stopped(self.path.stem)
        self.level = 0.0
