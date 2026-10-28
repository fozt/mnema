"""Live session orchestration: source(s) → ASR → tracks, translation at line boundaries
(sentences accumulate until a speech pause or a reasonable length, so the translator gets
coherent phrases rather than fragments), output to a terminal view. Runs on the main thread;
Ctrl-C ends it.

Recording comes first: recorders and sources start at once, ASR warms up in the background
(the facade's start_warmup), and an ASR failure (warmup failed, pipeline died) turns the source
into a voice recorder: recording goes on until the stop, the cause shows in the view and in
asr_failures.

The transcriber here is a synchronous facade (feed/poll/finish → LiveChunk); WhisperLiveKit's
async details live in asr.py. Optional facade members (start_warmup(), ready) are read with
getattr, like dropped_seconds of the sources. Speakers are separated by source (label = name).
"""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Protocol, cast

from ..core.base import Progress, noop
from ..core.models import Segment
from ..i18n import _
from . import remarks
from .models import LiveChunk, Track
from .remarks import track_stopped
from .sources import AudioSource
from .translate import strip_noise, worth_translating

# end of a sentence: final punctuation, maybe followed by a closing quote or bracket
_SENTENCE_END = re.compile(r"[.!?…]['\"»)]?\s*$")

# Line coalescing: a dot alone is no reason to flush, since speech with pauses breaks into
# fragments useless for translation ("Sprint 10." / "40." / "So..."). Accumulate until a speech
# pause or a reasonable length; the translation delay is bounded by the pause.
_PAUSE_FLUSH = 1.2  # seconds without new ASR deltas → the sentence is over
_PAUSE_FORCE_FLUSH = 3.5  # a long pause: flush even without final punctuation (auto language
# often omits the dot; otherwise an unfinished piece would wait for _MAX_PENDING)
_MIN_FLUSH = 60  # characters: a coherent enough piece is flushed at once
# starvation guard: decoding without final punctuation (auto language, continuous speech) would
# accumulate forever and translation would never start, so split at a word boundary
_MAX_PENDING = 300
# ASR stall watchdog: the draft grows but nothing is committed for this long → the engine
# stalled (WLK stops committing, buffer_transcription swells, the live text freezes). An honest
# status instead of a silent freeze; recording is unaffected, the final offline pass rebuilds it.
_STALL_AFTER = 20.0
_DRAFT_TAIL = 240  # draft characters on screen: the tail, not the whole sheet

# How long to wait for the translation worker on stop: one unfinished push, not the backlog
_STOP_TRANSLATE_WAIT = 15.0

# Backpressure: a source drops its oldest frames when its queue overflows (see sources.py) and
# accumulates dropped_seconds. A drop episode gets ONE marker in the transcript, once the losses
# stop growing.
_DROP_SETTLE = 2.0  # seconds without growing losses → the drop episode is over
_DROP_ANNOUNCE_MIN = 1.0  # total losses under a second are not announced

# Silence filter: on pauses Whisper hallucinates a continuation of the dialog and WLK's VAD/VAC
# let it through. A committed segment whose interval falls on actual silence of the source (its
# energy timeline, speech_energy via getattr; sources without it disable the filter) is dropped
# entirely: not in the Track, not on screen, not translated.
# Threshold from live measurements: room noise RMS 61–223 (median ~80), speech through speakers
# 736–1946, speech in smoke runs 1189–3868; 300 (≈ −41 dBFS int16) is safely above the noise and
# far below quiet speech.
_SILENCE_RMS = 300.0
# Edge tolerances (ASR timestamps drift against the timeline; a delta starts where the PREVIOUS
# emission ended, so the left edge may catch the tail of a real phrase): the left edge is cut by
# _GATE_LEAD (at most half the interval, so short real deltas survive), the right one extended by
# _GATE_TAIL. The error is asymmetric: a missed hallucination is a junk line, a false drop is lost
# speech, so every tolerance leans towards keeping.
_GATE_LEAD = 1.0
_GATE_TAIL = 2.5

_log = logging.getLogger(__name__)


def _tidy(text: str) -> str:
    """Collapse extra spaces and drop the space before punctuation (WLK emits ' .' deltas)."""
    text = re.sub(r"\s{2,}", " ", text)
    return re.sub(r"\s+([.!?,;:…])", r"\1", text)


class Transcriber(Protocol):
    def feed(self, pcm: bytes) -> None: ...
    def poll(self) -> LiveChunk: ...
    def finish(self) -> LiveChunk: ...


class Translator(Protocol):
    # on_partial is optional: a streaming translator (LLM) grows the translation in the view,
    # a non-streaming one (DeepL) ignores it
    def push(self, sentence: str, on_partial: Callable[[str], None] | None = None) -> str: ...


class Dialog(Protocol):
    """Two-way translation: phrase → (speaker label, translator of the direction)."""

    def route(self, sentence: str) -> tuple[str, Translator]: ...


class View(Protocol):
    def commit(self, label: str, text: str, translation: str | None) -> None: ...
    def hold(self, label: str, text: str) -> None: ...
    def partial(self, label: str, text: str, translation: str) -> None: ...
    def set_line(self, label: str, text: str) -> None: ...
    def set_status(self, label: str, text: str) -> None: ...


class Recorder(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...


class LiveSession:
    """Loop: read source blocks → feed/poll ASR → accumulate committed segments in a Track;
    at a sentence boundary translate (if set) and show; on stop drain and return the tracks.
    """

    def __init__(
        self,
        sources: Iterable[AudioSource],
        transcriber_factory: Callable[[AudioSource], Transcriber],
        *,
        translator: Translator | None = None,
        dialog: Dialog | Mapping[str, Dialog] | None = None,
        view: View | None = None,
        recorder: Recorder | Iterable[Recorder] | None = None,
        on: Progress = noop,
        clock: Callable[[], float] = time.monotonic,
        on_copy: Callable[[str], bool] | None = None,
        stop_after_silence: float = 0.0,
        on_started: Callable[[], None] | None = None,
    ) -> None:
        self._on_started = on_started
        self._sources = list(sources)
        self._make = transcriber_factory
        self._translator = translator
        self._dialog = dialog
        self._view = view
        # several recorders: a WAV track per source plus an optional screen (ffmpeg)
        if recorder is None:
            self._recorders: list[Recorder] = []
        elif isinstance(recorder, Iterable):
            # isinstance erases the Iterable parameters; restore them for ty
            self._recorders = list(cast("Iterable[Recorder]", recorder))
        else:
            self._recorders = [recorder]
        self._on = on
        self._clock = clock
        # stall watchdog: last ASR commit and "already announced" per source
        self._last_commit: dict[str, float] = {}
        self._stalled: set[str] = set()
        # auto-stop on silence: seconds without committed speech; 0 turns it off
        self._stop_after_silence = stop_after_silence
        self._last_delta: dict[str, float] = {}
        # backpressure: last seen losses, when they last grew and how much was announced
        self._drop_last: dict[str, float] = {}
        self._drop_grew_at: dict[str, float] = {}
        self._drop_announced: dict[str, float] = {}
        self._notice_seen: dict[str, str] = {}  # one-off source notices (getattr notice)
        self._warming: dict[str, float] = {}  # source → start of the ASR warmup
        self._ready_shown: set[str] = set()  # sources that already showed "ready"
        # silence filter: source → (speech_energy, transcriber), built in run()
        self._gates: dict[str, tuple[Callable[[float, float], float | None], Any]] = {}
        # source → committed segments dropped as silence hallucinations
        self.silence_dropped: dict[str, int] = {}
        # source → why its ASR failed: the session fell back to recording only
        self.asr_failures: dict[str, str] = {}
        self.recording_error: str | None = None
        self._stopped = False
        self._copy_asap = threading.Event()  # Ctrl-T: flush and copy the text to the clipboard
        self._on_copy = on_copy  # puts text on the clipboard (UI layer; None turns it off)
        # translation runs in a background worker: a model request per sentence takes seconds
        # and would otherwise block reading the microphone (growing recognition delay).
        # A None translator in an item means the line goes without a translation (backlog drop)
        self._tq: queue.Queue[tuple[str, str, Translator | None] | None] | None = None

    def stop(self) -> None:
        self._stopped = True

    def copy_now(self) -> None:
        """Flush the buffer and copy the session's whole text to the clipboard (Ctrl-T in the CLI).

        Thread- and signal-safe: it only sets a flag, the work happens on the loop's tick.
        """
        self._copy_asap.set()

    def run(self) -> list[Track]:
        tracks: dict[str, Track] = {s.name: Track(label=s.name) for s in self._sources}
        transcribers: dict[str, Transcriber] = {s.name: self._make(s) for s in self._sources}
        # raw ASR deltas; at a sentence boundary they go to the Track as one segment
        pending: dict[str, list[Segment]] = {s.name: [] for s in self._sources}
        # silence filter: only sources with an energy timeline (optional speech_energy); the
        # transcriber is needed for its prebuffer offset
        self._gates = {
            s.name: (fn, transcribers[s.name])
            for s in self._sources
            if callable(fn := getattr(s, "speech_energy", None))
        }

        worker: threading.Thread | None = None
        if self._translator is not None or self._dialog is not None:
            self._tq = queue.Queue()
            worker = threading.Thread(target=self._translate_worker, daemon=True)
            worker.start()

        started_sources: list[AudioSource] = []
        started_recorders: list[Recorder] = []
        try:
            # registered before start(): it may open a resource and fail on the next step
            for r in self._recorders:
                started_recorders.append(r)
                r.start()
            for s in self._sources:
                started_sources.append(s)
                s.start()
            if self._on_started is not None:
                self._on_started()
            for name, tr in transcribers.items():  # warmup only after recording started
                try:
                    start = getattr(tr, "start_warmup", None)
                    if callable(start):
                        start()
                except Exception as e:  # noqa: BLE001 - ASR failure → recording only
                    self._asr_fail(tracks[name], pending, name, e)
            self._on(None, _("Listening… Ctrl-C to stop"))
            self._loop(tracks, transcribers, pending)
        except KeyboardInterrupt:
            pass
        except BaseException:
            if self._tq is not None:
                self._tq.put(None)
            for tr in transcribers.values():
                close = getattr(tr, "close", None)
                if close is not None:
                    close()
            raise
        finally:
            stops: list[tuple[Callable[[], None], str]] = [
                (s.stop, track_stopped(s.name)) for s in reversed(started_sources)
            ]
            # read at call time: the remark is in the current language
            stops += [(r.stop, remarks.FILES_NOT_SAVED) for r in reversed(started_recorders)]
            for stop, reason in stops:
                try:
                    stop()
                except Exception:  # noqa: BLE001 - one failure must not keep the others open
                    self.recording_error = self.recording_error or reason
                    _log.warning("live: a resource did not close", exc_info=True)
            self._check_recording_error()

        # The final stage stays responsive: the screen shows what it waits for, and a second
        # Ctrl-C does not break saving, it only stops waiting for the tails (the WAV transcription
        # in finalize rebuilds the whole text anyway).
        interrupted = False
        for name, tr in transcribers.items():
            if name in self.asr_failures:
                close = getattr(tr, "close", None)
                if close is not None:
                    close()
                continue  # a dead ASR is not finished: its track is closed by a marker
            if interrupted:
                close = getattr(tr, "close", None)
                if close is not None:
                    close()
                self._flush(tracks[name], pending, name, force=True)
                continue
            self._set_status(name, _("⏹ Stopping: finishing the transcript…"))
            try:
                self._emit(tracks[name], pending, name, tr.finish())
            except KeyboardInterrupt:
                interrupted = True  # stop waiting: save what there is
            except Exception as e:  # noqa: BLE001 - the final drain is best effort too
                self._asr_fail(tracks[name], pending, name, e)
            self._flush(tracks[name], pending, name, force=True)  # unfinished sentence tail
        for s in self._sources:  # stopped in the middle of a drop episode: announce the rest
            tr = transcribers.get(s.name)
            if s.name in self.asr_failures or not getattr(tr, "consumes_audio", True):
                continue  # recording only: the ASR queue was not consumed, audio is intact
            dropped = float(getattr(s, "dropped_seconds", 0.0) or 0.0)
            if dropped - self._drop_announced.get(s.name, 0.0) >= _DROP_ANNOUNCE_MIN:
                self._announce_drop(tracks[s.name], pending, s.name, dropped)
        if worker is not None and self._tq is not None:
            # The translation backlog has a time budget: what made it is translated, the rest
            # goes without a translation (finalize translates the whole conversation). Otherwise
            # a dozen phrases × seconds of the model = minutes of silence after Ctrl-C.
            self._tq.put(None)  # sentinel: the worker exits at the end of the queue
            if self._sources:
                self._set_status(self._sources[0].name, _("⏹ Stopping: finishing the translation…"))
            try:
                worker.join(timeout=_STOP_TRANSLATE_WAIT)
                if worker.is_alive():  # out of budget: the rest goes untranslated
                    self._drop_translation_backlog()
                    worker.join(timeout=_STOP_TRANSLATE_WAIT)
            except KeyboardInterrupt:
                pass  # do not wait: the lines are already in the tracks
        for name, n in self.silence_dropped.items():
            _log.debug("silence filter [%s]: segments dropped this session: %d", name, n)
        self._check_recording_error()
        return list(tracks.values())

    def _drop_translation_backlog(self) -> None:
        """Queued lines go without a translation (None translator), with the sentinel last: the
        worker commits them in order after the current push and exits."""
        assert self._tq is not None
        backlog: list = []
        while True:
            try:
                backlog.append(self._tq.get_nowait())
            except queue.Empty:
                break
        for item in backlog:
            if item is not None:
                self._tq.put((item[0], item[1], None))
        self._tq.put(None)

    def _set_status(self, label: str, text: str) -> None:
        """Source status in the view, if any; a drawing failure does not break the final stage."""
        if self._view is None:
            return
        try:
            self._view.set_status(label, text)
        except Exception:  # noqa: BLE001
            pass

    def _check_recording_error(self) -> bool:
        errors = [getattr(r, "error", None) for r in self._recorders]
        errors += [getattr(s, "recording_error", None) for s in self._sources]
        self.recording_error = self.recording_error or next((str(e) for e in errors if e), None)
        if self.recording_error is None:
            return False
        self._stopped = True
        for source in self._sources:
            self._set_status(
                source.name, _("Recording stopped: {reason}", reason=self.recording_error)
            )
        return True

    def _translate_worker(self) -> None:
        """Translates queued lines one by one; a line is committed together with its translation
        (while it is in flight the view holds the line in the current turn). A streaming
        translator grows the translation in place (view.partial).
        """
        assert self._tq is not None
        while True:
            item = self._tq.get()
            if item is None:
                return
            label, text, translator = item
            if translator is None:  # session stop: the backlog goes untranslated
                translation = ""
            else:
                try:
                    translation = translator.push(text, on_partial=self._partial_cb(label, text))
                except Exception:  # noqa: BLE001 - live translation is best effort
                    translation = ""  # the held line is released anyway
            if self._view is not None:
                try:
                    self._view.commit(label, text, translation or None)
                except Exception:  # noqa: BLE001 - a drawing failure must not kill the worker
                    _log.exception("live: committing a line to the view failed")

    def _partial_cb(self, label: str, text: str) -> Callable[[str], None] | None:
        """Callback of the growing translation; None without a view (no reason to stream)."""
        view = self._view
        if view is None:
            return None

        def cb(translation: str) -> None:
            view.partial(label, text, translation)

        return cb

    def _loop(
        self,
        tracks: dict[str, Track],
        transcribers: dict[str, Transcriber],
        pending: dict[str, list[Segment]],
    ) -> None:
        active = list(self._sources)
        loop_start = self._clock()  # reference until the first committed phrase
        while active and not self._stopped:
            if self._check_recording_error():
                break
            # auto-stop on silence: speech over means the call is over. Counted only after the
            # first real speech, so starting early and recording only never cut the recording
            if self._stop_after_silence > 0:
                last = max(self._last_delta.values(), default=loop_start)
                if self._last_delta and self._clock() - last >= self._stop_after_silence:
                    self._auto_stop()
                    break
            if self._copy_asap.is_set():
                self._copy_asap.clear()
                for src in active:  # flush first so the tail of current speech is copied
                    self._flush(tracks[src.name], pending, src.name, force=True)
                self._copy_text(tracks)
            for s in list(active):
                block = s.read()
                if self._check_recording_error():
                    break
                # notice before the end branch: the source's last notice (the microphone did
                # not come back) arrives with the same read() that returned None
                self._check_notice(tracks[s.name], pending, s)
                if block is None:
                    active.remove(s)  # the source ended
                    continue
                tr = transcribers[s.name]
                if s.name in self.asr_failures or not getattr(tr, "consumes_audio", True):
                    # recording only (rec or a dead ASR): the source's tee writes the audio and
                    # ASR is not fed; ASR queue drops are not announced, since the recording is
                    # intact (the tee is before the queue)
                    continue
                self._check_drop(tracks[s.name], pending, s)
                self._check_warming(s.name, tr)
                if not block:
                    continue  # no data yet
                try:
                    tr.feed(block)
                    self._emit(tracks[s.name], pending, s.name, tr.poll())
                except Exception as e:  # noqa: BLE001 - an ASR failure does not stop recording
                    self._asr_fail(tracks[s.name], pending, s.name, e)

    def _auto_stop(self) -> None:
        """Stop on silence: mark the stop and tell the view (run() drains and saves)."""
        minutes = self._stop_after_silence / 60
        _log.info("auto-stop: %.0f s of silence after the last speech", self._stop_after_silence)
        if self._view is not None:
            self._view.set_status(
                next(iter(self._last_delta)),
                _("⏹ Auto-stop: {minutes} min of silence", minutes=f"{minutes:.0f}"),
            )
        self._stopped = True

    def _check_warming(self, name: str, tr: Transcriber) -> None:
        """ASR readiness in the source's live line: warming up → ready.

        The client cannot tell whether the engine is ready (warming up or downloading on the
        runner takes up to a minute). The warmup line is updated with a growing seconds counter,
        since the view clears a frozen line as stale; "ready" also shows when the engine is warm
        from the start, as the confirmation is needed anyway."""
        if name in self._ready_shown:
            return  # done: the speech drafts own the live line from now on
        ready = bool(getattr(tr, "ready", True))  # fakes and simple facades lack the property
        if not ready:
            now = self._clock()
            since = self._warming.setdefault(name, now)
            if self._view is not None:
                self._view.set_status(
                    name,
                    _(
                        "⏳ Recognition engine warming up — {seconds}s "
                        "(up to a minute on the first run; audio is already recording)…",
                        seconds=int(now - since),
                    ),
                )
        else:
            self._ready_shown.add(name)
            self._warming.pop(name, None)
            if self._view is not None:
                self._view.set_status(name, _("✅ Ready — speak"))

    def _copy_text(self, tracks: dict[str, Track]) -> None:
        """Hand the session text without [markers] to on_copy; the outcome goes to the view."""
        if self._on_copy is None:
            return
        parts = [
            seg.text
            for t in tracks.values()
            for seg in t.committed
            if not (seg.text.startswith("[") and seg.text.endswith("]"))  # markers
        ]
        text = _tidy(" ".join(parts)).strip()
        notice = _("📋 Nothing to copy yet")
        if text:
            try:
                ok = self._on_copy(text)
            except Exception:  # noqa: BLE001 - the clipboard is a convenience
                ok = False
            notice = _("📋 Copied to the clipboard") if ok else _("📋 Couldn't copy")
        if self._view is not None:  # status channel: speech drafts do not overwrite it
            self._view.set_status(next(iter(tracks), "mic"), notice)

    def _asr_fail(
        self, track: Track, pending: dict[str, list[Segment]], name: str, err: Exception
    ) -> None:
        """The source's ASR died → recording only: keep the cause, close the track with a marker."""
        reason = str(err).strip() or type(err).__name__
        self.asr_failures[name] = reason
        self._warming.pop(name, None)
        self._flush(track, pending, name, force=True)  # what was recognized goes to the track
        note = _("[transcription unavailable: {reason} — recording audio only]", reason=reason)
        if track.committed:  # the transcript breaks off mid-speech: mark the spot
            end = track.committed[-1].end
            track.committed.append(Segment(start=end, end=end, text=note, speaker=name))
        if self._view is not None:
            self._view.set_line(name, "")
            self._view.commit(name, note, None)

    def _check_notice(
        self, track: Track, pending: dict[str, list[Segment]], source: AudioSource
    ) -> None:
        """A one-off source notice (such as the microphone reopening) as a marker in the track."""
        # notice is an optional attribute of the source; a change is announced
        note = str(getattr(source, "notice", "") or "")
        if not note or note == self._notice_seen.get(source.name):
            return
        self._notice_seen[source.name] = note
        self._flush(track, pending, source.name, force=True)  # do not join across the break
        end = track.committed[-1].end if track.committed else 0.0
        track.committed.append(Segment(start=end, end=end, text=note, speaker=source.name))
        if self._view is not None:
            self._view.commit(source.name, note, None)

    def _check_drop(
        self, track: Track, pending: dict[str, list[Segment]], source: AudioSource
    ) -> None:
        """Watch the source's losses; announce a finished episode with one marker."""
        # dropped_seconds is optional: fakes and simple sources may lack it
        dropped = float(getattr(source, "dropped_seconds", 0.0) or 0.0)
        if dropped <= 0:
            return
        name = source.name
        now = self._clock()
        if dropped > self._drop_last.get(name, 0.0):
            self._drop_last[name] = dropped
            self._drop_grew_at[name] = now  # still going: announce once it settles
            return
        settled = now - self._drop_grew_at.get(name, now) >= _DROP_SETTLE
        if settled and dropped - self._drop_announced.get(name, 0.0) >= _DROP_ANNOUNCE_MIN:
            self._announce_drop(track, pending, name, dropped)

    def _announce_drop(
        self, track: Track, pending: dict[str, list[Segment]], name: str, dropped: float
    ) -> None:
        """Close the current line and put a gap marker into the track and the view."""
        seconds = dropped - self._drop_announced.get(name, 0.0)
        self._drop_announced[name] = dropped
        # speech before and after the gap must not join into one phrase
        self._flush(track, pending, name, force=True)
        note = _(
            "[skipped ~{seconds} s — transcription couldn't keep up]", seconds=f"{seconds:.0f}"
        )
        end = track.committed[-1].end if track.committed else 0.0
        track.committed.append(Segment(start=end, end=end, text=note, speaker=name))
        if self._view is not None:
            self._view.commit(name, note, None)

    def _emit(
        self, track: Track, pending: dict[str, list[Segment]], name: str, chunk: LiveChunk
    ) -> None:
        now = self._clock()
        # A speech pause flushes what accumulated before the new deltas, as a separate line. A
        # short pause (_PAUSE_FLUSH) releases only a finished sentence (_flush without force
        # needs final punctuation); a long one (_PAUSE_FORCE_FLUSH) flushes as is.
        if pending[name]:
            idle = now - self._last_delta.get(name, now)
            if idle >= _PAUSE_FORCE_FLUSH:
                self._flush(track, pending, name, force=True)
            elif idle >= _PAUSE_FLUSH:
                self._flush(track, pending, name)
        for seg in chunk.committed:
            if self._silent(name, seg):
                continue  # silence hallucination: skip track, view, translation and last_delta
            pending[name].append(seg)
            self._last_delta[name] = now
            joined = _tidy("".join(s.text for s in pending[name])).strip()
            if _SENTENCE_END.search(joined) and len(joined) >= _MIN_FLUSH:
                self._flush(track, pending, name)  # a coherent piece: no need to wait
            elif len(joined) >= _MAX_PENDING:
                # no final punctuation (auto language, continuous speech): force a split
                self._flush(track, pending, name, force=True)
        # stall watchdog: audio flows (the draft lives) but nothing is committed for too long
        if chunk.committed:
            self._last_commit[name] = now
            if name in self._stalled:
                self._stalled.discard(name)
                _log.info("ASR [%s] recovered after a stall", name)
        elif chunk.draft:
            last = self._last_commit.setdefault(name, now)
            if now - last >= _STALL_AFTER and name not in self._stalled:
                self._stalled.add(name)
                _log.warning(
                    "ASR [%s] stalled: %.0f s of draft without commits (len=%d)",
                    name,
                    now - last,
                    len(chunk.draft),
                )
                if self._view is not None:
                    # recording goes on (the tee is independent of ASR); the final offline
                    # pass rebuilds the transcript from it
                    self._view.set_status(
                        name,
                        _(
                            "⚠ Stream stalled — Ctrl-C builds the transcript from the recording "
                            "(for calls, `mnema rec` is more reliable)"
                        ),
                    )
        # one live line = the current unfinished line + the draft tail (only the tail: a swollen
        # buffer_transcription would bloat the render)
        current = _tidy("".join(s.text for s in pending[name])).strip()
        draft = chunk.draft or ""
        if len(draft) > _DRAFT_TAIL:
            draft = "…" + draft[-_DRAFT_TAIL:]
        line = (current + (" " + draft if draft else "")).strip()
        if self._view is not None:  # an empty one too: clears the line and refreshes its age
            self._view.set_line(name, line)

    def _silent(self, name: str, seg: Segment) -> bool:
        """The segment's interval falls on actual silence of the source → hallucination, drop it.

        Any doubt favours the segment: no timeline, a degenerate interval, speech near the edges.
        """
        gate = self._gates.get(name)
        if gate is None or seg.end <= seg.start:
            return False
        energy, tr = gate
        # the warmup prebuffer may have dropped old PCM: ASR times are shifted by its length
        off = float(getattr(tr, "fed_offset_seconds", 0.0) or 0.0)
        start = seg.start + off + min(_GATE_LEAD, (seg.end - seg.start) / 2)
        end = seg.end + off + _GATE_TAIL
        rms = energy(start, end)
        if rms is None or rms >= _SILENCE_RMS:
            if rms is not None:
                _log.debug(
                    "silence filter [%s]: %.1f–%.1f s kept (rms=%.0f ≥ %.0f): %r",
                    name,
                    seg.start,
                    seg.end,
                    rms,
                    _SILENCE_RMS,
                    seg.text.strip(),
                )
            return False
        self.silence_dropped[name] = self.silence_dropped.get(name, 0) + 1
        _log.debug(
            "silence filter [%s]: %.1f–%.1f s DROPPED (rms=%.0f < %.0f): %r",
            name,
            seg.start,
            seg.end,
            rms,
            _SILENCE_RMS,
            seg.text.strip(),
        )
        return True

    def _flush(
        self, track: Track, pending: dict[str, list[Segment]], name: str, *, force: bool = False
    ) -> None:
        segs = pending[name]
        text = _tidy("".join(s.text for s in segs)).strip()
        if not text or (not force and not _SENTENCE_END.search(text)):
            return
        pending[name] = []
        # non-verbal noise is cleaned for the view and translation; the file keeps the raw text
        shown = strip_noise(text)
        # dialog mode: route only decides the translation direction (by the phrase language);
        # a "source → Dialog" mapping pins the translation context to the source
        dialog: Dialog | None
        if isinstance(self._dialog, Mapping):
            # isinstance erases the Mapping parameters; restore them for ty
            dialog = cast("Mapping[str, Dialog]", self._dialog).get(name)
        else:
            dialog = self._dialog
        label = name  # a line is always labelled with the source name (mic/system)
        translator = self._translator
        if dialog is not None and shown:
            _routed, translator = dialog.route(shown)
        # the whole sentence goes to the Track: start of the first delta, end of the last
        track.committed.append(
            Segment(start=segs[0].start, end=segs[-1].end, text=text, speaker=label)
        )
        if not shown:
            return  # pure noise: neither shown nor translated
        # interjections and lines without letters are not translated
        if self._tq is not None and translator is not None and worth_translating(shown):
            if self._view is not None:  # the line shows at once and is committed with its
                self._view.hold(label, shown)  # translation later
            self._tq.put((label, shown, translator))
        elif self._view is not None:
            self._view.commit(label, shown, None)
