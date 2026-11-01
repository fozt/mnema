"""Recording of a live session. The recording comes first: audio is written from a tee of each
source's own PCM (WavTrackWriter, one WAV track per source, so mic and system are transcribed
separately), not by a second capture. The screen video on the Mac is written by Mnema.app itself
(ScreenRecorder only drives it).
"""

from __future__ import annotations

import array
import logging
import shutil
import threading
import time
import wave
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

SCREEN_FILE = "screen.mov"  # the app's video while recording
VIDEO_FILE = "video.mp4"  # the video with the recording's sound
SCREEN_MIN_FREE = 2 * 2**30  # less free space: no video, the audio keeps the disk
SCREEN_STOP_WAIT = 30.0  # seconds for the app to finish the file


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


class ScreenRecorder:
    """The screen video of a recording on the Mac. Mnema.app captures and writes it
    (ScreenCaptureKit); this side starts, pauses and stops it through the app protocol: send(op,
    path) is Platform.screen, the app's answers come to event(). The Recorder contract of the
    session; a failure never stops the audio: error holds its reason (permission, no_space,
    display_lost, writer) for the remark. start() does not wait for the app: a stop pressed while
    the video is starting stops it at once; the app reports a capture without frames itself.
    """

    def __init__(
        self,
        path: Path,
        send: Callable[[str, Path | None], None],
        *,
        stop_wait: float = SCREEN_STOP_WAIT,
    ) -> None:
        self.path = Path(path)
        self._send = send
        self._stop_wait = stop_wait
        self.origin: float | None = None  # time.monotonic() of the audio start, set by the session
        self.offset: float | None = None  # seconds from the audio start to the first frame
        self.error = ""
        self.ended = False  # the user stopped it from the menu bar indicator: no failure
        self._lock = threading.Lock()
        self._answer = threading.Event()
        self._state = "idle"  # idle | starting | running | stopping | done
        self._sent_at = 0.0

    @property
    def running(self) -> bool:
        return self._state in ("starting", "running")

    def start(self) -> None:
        try:
            free = shutil.disk_usage(self.path.parent).free
        except OSError:
            free = SCREEN_MIN_FREE  # unknown: the app reports a full disk itself
        if free < SCREEN_MIN_FREE:
            logger.warning("screen video not started: %d bytes free", free)
            self.error = "no_space"
            return
        with self._lock:
            self._state = "starting"
            self._sent_at = time.monotonic()  # the app counts offset from the start it got
        self._tell("start", self.path)

    def event(self, state: str, reason: str = "", offset: float | None = None) -> None:
        """The app's answer: started (offset — from its start to the first frame), failed with a
        reason, stopped (the file is closed). A stopped nobody asked for: the user stopped the
        capture from the menu bar, the video simply ends there."""
        with self._lock:
            if state == "started" and self._state == "starting":
                self._state = "running"
                if offset is not None:
                    lead = self._sent_at - self.origin if self.origin is not None else 0.0
                    self.offset = max(0.0, lead + offset)
            elif state == "failed":
                logger.warning("screen video failed: %s", reason)
                self.error = self.error or reason or "writer"
                self._state = "done"
            elif state == "stopped":
                if self._state in ("starting", "running"):
                    logger.info("the screen video was stopped in the app")
                    self.ended = True
                self._state = "done"
            self._answer.set()

    def pause(self) -> None:
        if self.running:  # while starting too: the app keeps it for the first frame
            self._tell("pause")

    def resume(self) -> None:
        if self.running:
            self._tell("resume")

    def stop(self) -> None:
        with self._lock:
            state = self._state
            if state in ("starting", "running"):
                self._state = "stopping"
                self._answer.clear()
        if state not in ("starting", "running") or not self._tell("stop"):
            return
        if not self._answer.wait(self._stop_wait):
            logger.warning("the app did not finish the screen video in time")
            with self._lock:
                self.error = self.error or "writer"
                self._state = "done"

    def _tell(self, op: str, path: Path | None = None) -> bool:
        try:
            self._send(op, path)
        except Exception:  # noqa: BLE001 - the app is gone: the audio goes on without video
            logger.warning("screen video: %s did not reach the app", op, exc_info=True)
            with self._lock:
                self.error = self.error or "writer"
                self._state = "done"
            return False
        return True
