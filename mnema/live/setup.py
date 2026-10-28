"""Building a live session from its configuration, shared by the CLI (cmd_live) and mnema-bar.

Here: sources (microphone, system audio), WAV tracks (tee), the two-way translation dialog,
transcribers (server, or a recorder stub) and the LiveSession itself; the caller supplies the
view and how to stop. Finalization (finalize_live) runs an offline pass over the recorded WAV
tracks (the live text is only a draft), then the final translation, a title, a summary and
the saved files.
Heavy imports are lazy: the module imports without sounddevice/whisperlivekit (thin client).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, field_validator

from ..connection import Connection

# _audio/_persist/_translate are imported up front, not at stop: a `git pull` during a
# recording would otherwise pair a new persist.py with the old setup.py at finalization.
from ..core import audio as _audio  # noqa: F401
from ..core.base import LANG_NAMES, MediaError, Progress, noop, phased, unstamped
from ..core.models import Segment, Transcript
from ..dispatch import dispatch
from ..i18n import _
from ..llm import claude
from ..llm.errors import ErrorKind, human_reason
from ..llm.notes import Cloud, LightNote
from ..sync.scan import FILES_LOCK
from . import meta as _meta
from . import persist as _persist  # noqa: F401
from . import remarks as _remarks
from . import translate as _translate  # noqa: F401
from .models import (
    FINALIZE_STEPS,
    STEP_MESSAGES,
    TITLE_MESSAGE,
    LiveChunk,
    LiveResult,
    Track,
)
from .naming import (
    PART_LABEL,
    WAV_HEADER,
    chosen_title,
    chunk_follows,
    folder_name,
    folder_title,
    free_name,
    is_default_name,
    part_label,
    part_of,
    started_iso,
    titled_path,
    wav_seconds,
)
from .remarks import Remark, Step

logger = logging.getLogger(__name__)
INCOMPLETE_INSTALL = "Mnema is not fully installed. Reinstall it"
# Progress line of the recognition engines (core, cloud ASR); a track names its source instead.
RECOGNIZING = "Recognizing…"

if TYPE_CHECKING:
    from .session import LiveSession, View


def _step(message: str) -> str:
    """A finalization step message from live.models, shown in the current language."""
    return _(message)


class LiveConfig(BaseModel):
    """Live session parameters: the cmd_live flags shared with the menu bar app."""

    system: bool = False  # second source: system audio (tracks are labelled mic/system)
    mic: bool = True  # False: no microphone, system audio only (needs system=True)
    lang: str | None = None  # recognition language as chosen; None: decided by the recording
    translate: str | None = None  # my language for two-way translation; None: no translation
    summary: bool = True  # summary.md of the whole recording; the title comes from the same pass
    summary_lang: str | None = None  # summary and title language (ru/en); None: the call's
    # auto (Parakeet, without it GigaAM for Russian, else Whisper) | parakeet | gigaam |
    # whisper | cloud "provider/model" (llm/asr)
    asr: str = "auto"
    stream: bool = True  # live text (streaming ASR); False: "rec", the transcript is built by
    # the offline pass at stop (batch quality, the GPU stays free during the call)
    record: bool = True
    mix_audio: bool = True  # the final MP3; --no-save skips it
    runner: str | None = None  # resolved runner URL (resolve_runner), or None for local

    @field_validator("translate")
    @classmethod
    def _translation_language(cls, value: str | None) -> str | None:
        if value is not None and value not in LANG_NAMES:
            raise ValueError(_("Enter a translation language code, such as ru or en"))
        return value


def live_base(
    output: str | Path | None = None,
    *,
    root: Path | None = None,
    section: str = "",
) -> Path:
    """The session folder: output, else a new folder in root (default: the library root).

    All session files live in this ONE folder under fixed names (transcript.txt,
    translation.md, <label>.wav). section is a section of that root (the caller checks it);
    the recording folder is created right inside it.
    """
    if output:
        base = Path(output)
    else:
        from ..config import library_root

        parent = root or library_root()
        if section:
            parent = parent / section
        # Untitled: the start date and time; a title replaces the time later.
        base = free_name(parent / folder_name(datetime.now(), ""))
    base.mkdir(parents=True, exist_ok=True)
    if _meta.started(base) is None:
        _meta.set_started(base, _meta.now())
    return base


def rename_session(base: Path, title: str) -> Path:
    """Title the recording folder (naming.titled_path): the date is the recording start, a part
    mark stays, an empty title gives the start-time name, a taken name gets "… (2)". A title
    set earlier for the processing no longer waits for it."""
    from .recording_client import drop_own_title

    new = titled_path(base, title)
    if new != base:
        base.rename(new)
    drop_own_title(new)
    return new


def _final_translate(
    text: str, *, to_lang: str, titled: bool, on: Progress
) -> tuple[str | None, str | None, bool]:
    """The final translation (and the title in the same call) by the cloud model.

    Best effort: a failure does not stop saving — (None, None, True): the transcript is saved
    without a translation and the caller adds a remark (shown after the live screen; a log line
    inside the redrawn area got lost).
    """
    try:
        if claude.llm_available():
            if titled:
                md, title = claude.translate_titled(text, to_lang=to_lang, on=on)
                return md, title, False
            return claude.translate(text, to_lang=to_lang, on=on), None, False
    except Exception:  # noqa: BLE001 - translation is best effort, the transcript matters more
        logger.debug("final translation failed", exc_info=True)
    return None, None, True


def _lang_code(text: str, lang: str | None) -> str:
    """Title and summary language: the chosen one (ru/en), else the call's (a Russian prompt
    would otherwise give an English meeting a Russian title)."""
    from .translate import detect_lang

    return lang if lang in LANG_NAMES else detect_lang(text)


def _lang_name(text: str, lang: str | None) -> str:
    """Title and summary language as a word, for the model."""
    return LANG_NAMES[_lang_code(text, lang)]


def _retitle(
    base: Path,
    rec_paths: list[Path],
    text: str,
    *,
    title: str | None = None,
    warnings: list[Remark] | None = None,
    lang: str | None = None,
) -> tuple[Path, list[Path]]:
    """Replace the timestamp name of the session FOLDER with a title from the content; the
    files inside keep their fixed names, only the folder moves.

    title is a ready title (the final translation returns one in the same request); without it
    the cloud model of this process makes one. lang is the title language (ru/en, as for the
    summary), None for the call's. Best effort: without text, a key, or on an error the start
    date and time stay.
    """
    if not text or (not title and not claude.llm_available()):
        return base, rec_paths
    try:
        lang = _lang_name(text, lang)
        if not title:
            title, _engine, note = claude.title_ex(text, lang=lang)
            remark = _remarks.light(Step.TITLE, note)
            if remark and warnings is not None:
                warnings.append(remark)
        title = (title or "").strip() if folder_title(title or "") else ""
    except Exception as e:  # noqa: BLE001 - the title is best effort, saving goes on
        title = ""
        if warnings is not None:  # say why the folder has no title
            warnings.append(_remarks.title_failed(human_reason(e)))
    if not title:
        return base, rec_paths
    try:
        new_base = rename_session(base, title)
    except OSError:
        return base, rec_paths
    return new_base, [new_base / p.name if p.parent == base else p for p in rec_paths]


class NullTranscriber:
    """Transcriber of the recorder mode: no ASR, the sources' tee tracks write the audio."""

    ready = True  # nothing to warm up
    # Audio is not consumed: an overflowing ASR queue must not be reported as "transcription
    # fell behind" — the tee writes the recording before the queue, so it is intact.
    consumes_audio = False

    def feed(self, pcm: bytes) -> None: ...

    def poll(self) -> LiveChunk:
        return LiveChunk()

    def finish(self) -> LiveChunk:
        return LiveChunk()


class PreparedLive:
    """A built session and its parts: sources, WAV tracks, dialog, transcribers.

    Not a pydantic model: it holds live runtime objects. make_session(view) builds the
    LiveSession; the UI layer supplies the view, stopping and dialog wrappers.
    """

    def __init__(
        self,
        *,
        config: LiveConfig,
        base: Path,
        sources: list[Any],
        writers: dict[str, Any],
        dialog: Any,
        session_dialog: Any,
        transcribers: dict[str, Any],
        lang: str | None,
        status: str,
    ) -> None:
        self.config = config
        self.base = base
        self.sources = sources
        self.writers = writers  # label → WavTrackWriter (tee of the source PCM)
        self.dialog = dialog  # LiveDialog | None, for the final translation
        self.session_dialog = session_dialog  # Dialog | Mapping | None, as passed to LiveSession
        self.transcribers = transcribers  # label → transcriber, one per source
        self.lang = lang  # language of the files (None with translation: auto)
        self.status = status  # for people: where ASR runs and what is warming up
        self.part: dict[str, float] | None = None  # a part of the recording: start, end (s)
        self.info: list[str] = []  # notes for the history (WAV repaired, part of the recording)
        self.recording_error: str | None = None
        self.run = _meta.Run()  # history entry of this processing (.mnema-meta.json)

    @property
    def labels(self) -> list[str]:
        return [s.name for s in self.sources]

    @property
    def recorders(self) -> list[Any]:
        return list(self.writers.values())

    def make_session(
        self,
        view: View | None,
        *,
        on_copy: Callable[[str], bool] | None = None,
        stop_after_silence: float = 0.0,
        save_identity: bool = True,
    ) -> LiveSession:
        from .session import LiveSession

        def started() -> None:
            from ..sync.scan import ensure_identity

            # The --no-save temporary path may already exist after another run.
            with FILES_LOCK:
                if save_identity and self.base.is_dir():
                    ensure_identity(self.base)

        transcribers = self.transcribers
        return LiveSession(
            self.sources,
            lambda s: transcribers[s.name],
            dialog=self.session_dialog,
            view=view,
            recorder=self.recorders or None,
            on_copy=on_copy,
            stop_after_silence=stop_after_silence,
            on_started=started,
        )

    def close(self) -> None:
        """Close the shared server translator after saving or cancelling."""
        close = getattr(self.dialog, "close", None)
        if close is not None:
            close()


def build_live(
    config: LiveConfig, *, base: Path, connection: Connection | None = None
) -> PreparedLive:
    """Build the sources, WAV tracks, dialog and transcribers from the configuration.

    The cmd_live contract: MediaError when sounddevice, the microphone or system audio is
    unavailable; speakers and the ASR language are per source.
    """
    try:
        import sounddevice  # noqa: F401  # a clear error before a source is created
    except (ImportError, OSError) as e:
        # No package or no system PortAudio (the Linux sounddevice wheel does not bundle it:
        # sudo apt install libportaudio2); the log says which.
        logger.warning("no audio recording: %s", e)
        raise MediaError(_(INCOMPLETE_INSTALL)) from e

    from .record import WavTrackWriter
    from .sources import MicSource, mic_missing_text
    from .translate import PinnedDialog

    if (config.stream or config.translate) and connection is None:
        raise MediaError(_("Live transcription and translation need a selected server"))

    lang = config.lang
    dialog = None
    if config.translate:
        from ..client import MnemaClient
        from .remote_translate import RemoteDialog

        assert connection is not None
        client = MnemaClient(connection.url, token=connection.token)
        dialog = RemoteDialog(client, my_lang=config.translate)

    # The recording comes first: one WAV track per source, a tee of the PCM stream ASR sees,
    # stored in the session folder as <label>.wav.
    writers: dict[str, Any] = {}

    def _tee(label: str) -> Any:
        if not config.record:
            return None
        writers[label] = WavTrackWriter(base / f"{label}.wav")
        return writers[label].write

    if not config.mic and not config.system:
        raise MediaError(
            _("Without the microphone (--no-mic), system audio is needed: add --system.")
        )
    sources: list[Any] = []
    src = None
    if config.mic:
        src = MicSource(label="mic", tee=_tee("mic"))
        if not src.available():
            raise MediaError(mic_missing_text())
        sources.append(src)
    system_src = None
    if config.system:
        # The other side's voices digitally, so it works with headphones too.
        from .system_audio import system_source, system_unavailable_hint

        system_src = system_source(label="system", tee=_tee("system"))
        if not system_src.available():
            raise MediaError(system_unavailable_hint())
        sources.append(system_src)

    # Lines are labelled by source name (session._flush); PinnedDialog stays for the
    # translation CONTEXT (one window of both sides' lines); direction follows the phrase
    # language. "я"/"они" are route ids, not interface text.
    session_dialog: Any = dialog
    if dialog is not None and system_src is not None and src is not None:
        session_dialog = {
            src.name: PinnedDialog(dialog, "я"),
            system_src.name: PinnedDialog(dialog, "они"),
        }

    # One transcriber PER SOURCE: each has its own speech stream, ASR state and websocket.
    # The ASR language is NOT pinned in translation mode: "my language" is not what I speak
    # on a given call (an English call with --translate ru lost the user's lines), and on the
    # runner the language is part of the engine config — ru+en kept TWO large-v3 copies in
    # VRAM and hit CUDA OOM (the track froze silently).
    transcribers: dict[str, Any] = {}
    if not config.stream:
        for s in sources:
            transcribers[s.name] = NullTranscriber()
        status = _("recording without live text — the transcript is built when you stop")
    else:
        # ASR on the runner: a larger model (large-v3), loaded on the first connection.
        from ..config import Settings
        from .remote import RemoteStreamingTranscriber

        settings = Settings()
        assert connection is not None
        for s in sources:
            transcribers[s.name] = RemoteStreamingTranscriber(
                runner=connection.url,
                token=connection.token,
                ca=settings.ca,
                lang=lang,
                label=s.name,
            )
        status = _("ASR on server {url}, connecting in the background", url=connection.url)

    prepared = PreparedLive(
        config=config,
        base=base,
        sources=sources,
        writers=writers,
        dialog=dialog,
        session_dialog=session_dialog,
        transcribers=transcribers,
        lang=lang,
        status=status,
    )
    return prepared


# Silence gate of the offline pass: like live (sources.EnergyChronicle), but on the WAV
# itself, where batch timestamps are exact; when in doubt (broken wav, empty interval) the
# segment is kept.
_GATE_RMS = 300
_GATE_MARGIN = 0.3  # a small margin around the interval


def _wav_speech_rms(wav: Path, start: float, end: float) -> float | None:
    """Peak RMS of 100 ms windows in a WAV interval (int16 mono); None when unknown."""
    import array
    import contextlib
    import wave

    with contextlib.suppress(Exception):
        with wave.open(str(wav)) as w:
            rate = w.getframerate()
            a = max(0, int(start * rate))
            b = min(int(end * rate), w.getnframes())
            if b <= a:
                return None
            w.setpos(a)
            raw = w.readframes(b - a)
        try:  # numpy comes with the live extra; bare CI falls back to pure Python
            import numpy as np

            data = np.frombuffer(raw, dtype="<i2").astype(np.float64)
            if not len(data):
                return None
            win = max(1, rate // 10)
            pad = (-len(data)) % win
            chunks = np.pad(data, (0, pad)).reshape(-1, win)
            return float(np.sqrt((chunks**2).mean(axis=1)).max())
        except ImportError:
            data = array.array("h", raw[: len(raw) // 2 * 2])
            if not data:
                return None
            win = max(1, rate // 10)
            best = 0.0
            for i in range(0, len(data), win):
                c = data[i : i + win]
                best = max(best, (sum(x * x for x in c) / len(c)) ** 0.5)
            return best
    return None


def _wav_is_silent(wav: Path, *, chunk_sec: float = 60.0, on: Progress = noop) -> bool:
    """Is the whole recording physical silence? `on` between chunks lets processing stop."""
    import wave as wave_mod

    try:
        with wave_mod.open(str(wav)) as w:
            rate = w.getframerate() or 1
            total = w.getnframes() / rate
    except (OSError, EOFError, wave_mod.Error):
        return False
    if total <= 0:
        return False
    t = 0.0
    while t < total:
        on(None, _("Checking audio…"))
        rms = _wav_speech_rms(wav, t, min(t + chunk_sec, total))
        if rms is None or rms >= _GATE_RMS:
            return False
        t += chunk_sec
    return True


def _refine_track(
    prepared: PreparedLive, track: Track, wav: Path, on: Progress, lang: str | None
) -> tuple[list[Segment], Transcript]:
    """Offline transcription of a WAV track → (segments replacing the live text, the core
    transcript).

    The cmd_transcribe pipeline, always local here (MNEMA_RUNNER does not override). The
    language is decided before recognition (None: the engine finds it). The speaker is the
    track label.
    """
    choice = prepared.config.asr
    params: dict[str, Any] = {"lang": lang} if lang else {}
    from ..core.backends import WhisperBackend, engine

    chosen = engine(choice)
    if chosen is not None:  # an engine without this language: Whisper, which has them all
        params["backend"] = choice if chosen.supports(lang) else WhisperBackend.id
    tr = None
    if "/" in choice:  # cloud, from this machine: the keys are here
        from ..llm import asr

        tr = asr.transcribe(wav, choice, lang=lang, on=on)
    if tr is None:
        result = dispatch("transcribe", wav, params, runner=None, local=True, on=on)
        tr = Transcript.model_validate(result)

    # Service markers ("[skipped ~N s]" and the like) are not speech: a track of markers only
    # is empty, and no offline text on it is not an error.
    def _spoken(s: Segment) -> bool:
        t = s.text.strip()
        return bool(t) and not (t.startswith("[") and t.endswith("]"))

    if not tr.segments and any(_spoken(s) for s in track.committed):
        raise _NoSpeech(_("recognition found no speech"))
    segs: list[Segment] = []
    dropped = 0
    for seg in tr.segments:
        rms = _wav_speech_rms(wav, seg.start - _GATE_MARGIN, seg.end + _GATE_MARGIN)
        if rms is not None and rms < _GATE_RMS:
            dropped += 1  # "speech" from physical silence: a hallucination of the decoder
            continue
        segs.append(Segment(start=seg.start, end=seg.end, text=seg.text, speaker=track.label))
    if dropped:
        logger.debug("offline silence gate [%s]: segments dropped: %d", track.label, dropped)
    if not segs and any(s.text.strip() for s in track.committed):
        raise _NoSpeech(_("all detected speech turned out to be silence"))
    return segs, tr


def _recognizing(label: str) -> str:
    """Progress line of one track, naming its source."""
    if label == "mic":
        return _("Recognizing the microphone…")
    if label == "system":
        return _("Recognizing system audio…")
    return _("Recognizing track {label}…", label=label)


def _took(t0: float) -> float:
    return round(time.monotonic() - t0, 1)


def _wav_length(wav: Path) -> float | None:
    try:
        return round(wav_seconds(wav), 1)
    except Exception:  # noqa: BLE001 - not a WAV (or broken): length unknown
        return None


def _refine_tracks(
    prepared: PreparedLive,
    tracks: list[Track],
    on_asr: Progress,
    on_transcript: Callable[[Path], None] | None = None,
    on_lang: Callable[[dict[str, str]], None] | None = None,
) -> tuple[list[str], list[Remark], dict[str, str], Path | None]:
    """Second pass: rebuild the tracks by offline transcription of the recorded WAVs.

    The live stream commits words without future context (broken punctuation, fragments); an
    offline run over the same recording gives batch quality. Segments are replaced only on
    success; any failure (no engine, broken wav) leaves a warning and the live text.
    The language is decided before recognition, and on_lang is called then (or after
    recognition when nothing decides it earlier). on_asr is this pass's OWN slice of the
    finalization scale (see finalize_live): each track gets a share of it, so the bar never
    jumps back between phases. Once recognized, transcript.txt is written at once
    (on_transcript); the title and summary come later.
    Returns (refined track labels, warnings, languages of recognized tracks, the file written).
    """
    refined: list[str] = []
    warnings: list[Remark] = []
    languages: dict[str, str] = {}
    run = prepared.run
    # Only recorded tracks can be refined: without a recording (--no-save) there is nothing.
    candidates: list[tuple[Track, Path]] = []
    for track in tracks:
        writer = prepared.writers.get(track.label)
        wav = writer.path if writer is not None else None
        if wav is None or not wav.exists() or wav.stat().st_size <= WAV_HEADER:
            continue  # empty recording: keep the live text
        if _wav_is_silent(wav, on=on_asr):
            # A track without a single sound (system silent all call): ASR would waste minutes.
            # The processing side decides silence; clients send the tracks as they are.
            logger.debug("offline pass [%s]: the whole recording is silent, skipping", track.label)
            warnings.append(_remarks.track_silent(track.label))
            continue
        candidates.append((track, wav))
    if not candidates:
        on_asr(1.0, "")
        return refined, warnings, languages, None
    # A chosen language applies to every track, unchecked. "Automatic" is not detected up
    # front: Parakeet recognizes without a hint and names the language from the text, Whisper
    # and the cloud detect it themselves. If they get it wrong, the user picks the language.
    chosen = prepared.config.lang or None
    langs = {track.label: chosen for track, _wav in candidates}
    if on_lang is not None and chosen:
        on_lang(dict.fromkeys(langs, chosen))
    slices = phased(on_asr, *([1.0] * len(candidates)))
    t_asr = time.monotonic()
    for (track, wav), sl in zip(candidates, slices, strict=True):
        lang = langs[track.label]
        entry = run.tracks[track.label] = _meta.TrackRun(lang=lang, seconds=_wav_length(wav))
        sl(None, _recognizing(track.label))

        def track_progress(
            fraction: float | None, message: str, *, _sl=sl, _label=track.label
        ) -> None:
            generic = message in (RECOGNIZING, _(RECOGNIZING))
            _sl(fraction, _recognizing(_label) if generic else message)

        t0 = time.monotonic()
        try:
            segs, tr = _refine_track(prepared, track, wav, track_progress, lang)
            # Step timings go to the log (bar.log), so slow steps are easy to find.
            logger.info(
                "recognition %s: %.0f s for ~%.0f s of audio",
                track.label, time.monotonic() - t0, wav.stat().st_size / 32000,
            )  # fmt: skip
        except Exception as e:  # noqa: BLE001 - fall back to the live text, finalization goes on
            live_kept = any(s.text.strip() for s in track.committed)
            # The reason in the user's words (cloud: "request limit exceeded"); details go to
            # the log, since engine text may point to the terminal.
            entry.error = str(e) if isinstance(e, _NoSpeech) else _remarks.node_asr_reason(e)
            logger.warning("could not recognize track %s", track.label, exc_info=True)
            if isinstance(e, _NoSpeech):
                warnings.append(_remarks.asr_no_speech(track.label, live_kept=live_kept))
            else:
                warnings.append(_remarks.asr_failed(track.label, e, live_kept=live_kept))
            sl(1.0, "")
            continue
        entry.engine = tr.engine
        entry.lang = tr.language or lang
        if entry.lang:
            languages[track.label] = entry.lang
        track.committed = segs
        # An empty result over empty live text (rec mode: NullTranscriber) is NOT refined:
        # otherwise the UI would take emptiness for success (_refine_track's guard is silent
        # without live text).
        if segs:
            refined.append(track.label)
        sl(1.0, "")
    run.steps["asr"] = _took(t_asr)
    if on_lang is not None and not chosen and languages:
        on_lang(dict(languages))
    if languages:  # one language for all tracks goes to the files, else per line
        spoken = set(languages.values())
        prepared.lang = spoken.pop() if len(spoken) == 1 else None
    # The transcript is written AT ONCE, before the title and summary: on a long call the
    # model takes minutes.
    txt: Path | None = None
    if refined:
        from .persist import write_transcript

        try:
            txt = write_transcript(tracks, prepared.base, prepared.lang)
        except OSError:
            logger.warning("could not write the transcript to %s", prepared.base, exc_info=True)
        else:
            if txt is not None and on_transcript is not None:
                on_transcript(txt)
    return refined, warnings, languages, txt


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _model_of(engine: str, note: LightNote | None) -> str | None:
    """The model that made a step: from the note, else the active cloud model."""
    if note is not None and note.model:
        return note.model
    if engine != "cloud":
        return None
    from ..llm.config import LLMSettings

    return LLMSettings().model


class _NoSpeech(MediaError):
    """The offline pass heard no speech (or all "speech" was silence): an empty track."""


def finalize_live(
    prepared: PreparedLive,
    tracks: list[Track],
    *,
    retitle: bool = True,
    user_title: Callable[[], str | None] | None = None,
    on_transcript: Callable[[Path], None] | None = None,  # transcript.txt written
    on_lang: Callable[[dict[str, str]], None] | None = None,  # track languages: label → code
    on_base: Callable[[Path], None] | None = None,  # the folder was renamed: its new path
    on: Progress = noop,
) -> tuple[LiveResult, str]:
    """Session finalization: the offline pass over the recording, the final translation
    (dialog), a title, a summary and the saved files.

    Returns (result, full transcript text); the UI uses the text to decide whether to suggest
    transcribing the recording. retitle=False (an explicit -o) keeps the name.
    on_lang reports the track languages (label → code) once: before recognition when chosen,
    else after it. The result has them in LiveResult.languages.
    `on` is ONE progress scale for the whole finalization: phases get slices through phased,
    so the bar grows monotonically from 0 to 1.
    Every processing — stopped and failed too — is appended to the folder's .mnema-meta.json.
    """
    prepared.run = _meta.begin(prepared.config, part=prepared.part)
    recording_error = prepared.recording_error or next(
        (w.error for w in prepared.writers.values() if getattr(w, "error", None)), None
    )
    recording_note = _remarks.recording_failed(recording_error) if recording_error else None
    if recording_note is not None:
        prepared.run.notes.append(recording_note)
        prepared.run.remarks.append(recording_note.text)
    try:
        result, full = _finalize_live(
            prepared, tracks, retitle=retitle, user_title=user_title,
            on_transcript=on_transcript, on_lang=on_lang, on_base=on_base, on=on,
        )  # fmt: skip
    except BaseException as e:
        if isinstance(e, FinalizeCancelled | KeyboardInterrupt):
            _record(prepared, "stopped")
        else:
            _record(prepared, "failed", (str(e).strip().splitlines() or [type(e).__name__])[-1])
        raise
    if recording_note is not None:
        result.remarks.insert(0, recording_note)
        prepared.run.notes = [
            recording_note,
            *[r for r in prepared.run.notes if r.step != Step.RECORD],
        ]
    prepared.run.remarks = [r.text for r in result.remarks]
    _record(prepared, "ok")
    return result, full


def _record(
    prepared: PreparedLive, status: Literal["ok", "stopped", "failed"], error: str | None = None
) -> None:
    """Append this processing to the folder's history (the folder may have been renamed)."""
    run = prepared.run
    run.finished, run.status, run.error = _meta.now(), status, error
    _meta.append(prepared.base, run)


def _finalize_live(
    prepared: PreparedLive,
    tracks: list[Track],
    *,
    retitle: bool = True,
    user_title: Callable[[], str | None] | None = None,
    on_transcript: Callable[[Path], None] | None = None,
    on_lang: Callable[[dict[str, str]], None] | None = None,
    on_base: Callable[[Path], None] | None = None,
    on: Progress = noop,
) -> tuple[LiveResult, str]:
    from .persist import dialogue_text, save, write_summary

    p_refine, p_translate, p_title, p_save = phased(on, *[1.0] * len(FINALIZE_STEPS))
    cfg = prepared.config
    run = prepared.run
    # Two passes: the live output is only a draft; the file is rebuilt by offline
    # transcription of the recorded WAV tracks (full context gives coherent text).
    p_refine(None, _step(STEP_MESSAGES["asr"]))
    refined, warnings, languages, early = _refine_tracks(
        prepared, tracks, p_refine, on_transcript=on_transcript, on_lang=on_lang
    )
    full = " ".join(seg.text for t in tracks for seg in t.committed).strip()
    if refined:  # translation, title and summary use the labelled transcript, as in the file
        full = dialogue_text(tracks, prepared.lang).strip()
    final = None
    title_hint: str | None = None
    translate_failed = False
    cloud_ready = claude.llm_available()
    if prepared.dialog is not None and full:
        from ..config import Settings
        from .translate import DeepLTranslator, detect_lang

        to = prepared.dialog.my_lang
        t0 = time.monotonic()
        # The whole call translated into MY language. A call already in my language would
        # come back unchanged (a duplicate translation.md that looks like a failure): skip it.
        if detect_lang(full) != to:
            if cloud_ready:
                p_translate(None, _step(STEP_MESSAGES["translate"]))
                # The title comes in the same request (titled).
                final, title_hint, failed = _final_translate(
                    full,
                    to_lang=to,
                    titled=retitle,
                    on=p_translate,
                )
                translate_failed = translate_failed or failed
            elif Settings().deepl_api_key:
                # No model, but live translation went through DeepL: translate the final text
                # with it too, or the saved file would lose the translation. Best effort.
                p_translate(None, _step(STEP_MESSAGES["translate"]))
                try:
                    final = DeepLTranslator(to, timeout=30.0).push(full)
                except Exception:  # noqa: BLE001 - translation is best effort
                    final = None
                    translate_failed = True
        run.steps["translate"] = _took(t0)
    p_translate(1.0, "")

    base = prepared.base
    rec_paths = [w.path for w in prepared.writers.values() if w.path.exists()]
    # A title from the content: from the final translation (title_hint) or the model.
    points: list[str] = []
    about = ""
    refused: LightNote | None = None  # no summary from the model: no title from it either
    # A couple of lines are not worth a summary (the model invents "greetings" and a
    # "positive tone"); the title is made the usual way.
    long = prepared.config.summary and len(unstamped(full)) >= _SUMMARY_MIN_CHARS
    here = cloud_ready
    least = _SUMMARY_MIN_CHARS if prepared.config.summary else len(unstamped(full)) + 1
    if here and long:
        p_title(None, _step(STEP_MESSAGES["summary"]))
        lang = prepared.config.summary_lang if prepared.config.summary else None
        # Short text only gets a title (least); without a summary, only the title.
        summary = summarize_text(full, run, warnings, p_title, lang=lang, min_chars=least)
        logger.info("summary and title: %.0f s", run.steps["summary"])
        points, about = summary.points, summary.about
        title_hint = title_hint or summary.title or None
        # A title-only request: its refusal is already in the title remark.
        if summary.engine == "none" and summary.note is not None and len(unstamped(full)) >= least:
            refused = summary.note
    chosen = chosen_title(user_title(), part_label(base.name)) if user_title is not None else None
    t0 = time.monotonic()
    if chosen:
        # The user set a title ("Set title…" in the app): the model is not asked.
        warnings[:] = [w for w in warnings if w.step != Step.TITLE]
        new_base = rename_session(base, chosen)
        rec_paths = [new_base / p.name for p in rec_paths]
        base = prepared.base = new_base
    elif retitle and refused is not None and not title_hint and _same_refusal(refused):
        # The same model would refuse the same way: no extra request, the folder keeps
        # its time name.
        if remark := _remarks.light(Step.TITLE, refused):
            warnings.append(remark)
    elif retitle and (title_hint or here):
        p_title(None, _step(TITLE_MESSAGE))
        base, rec_paths = _retitle(
            base,
            rec_paths,
            full,
            title=title_hint,
            warnings=warnings,
            # With the summary off its language is not chosen: the title follows the call.
            lang=prepared.config.summary_lang if prepared.config.summary else None,
        )
        # The folder may have been renamed: the UI (Ctrl-C cleanup, result panel) needs the
        # current path.
        prepared.base = base
        run.steps["title"] = _took(t0)
    if on_base is not None:  # the UI tracks the folder being processed by its path
        on_base(base)
    p_title(1.0, "")
    if translate_failed and prepared.dialog is not None:
        warnings.append(_remarks.translation_failed())
    p_save(None, _step(STEP_MESSAGES["save"]))
    t0 = time.monotonic()
    result = save(
        tracks,
        base=base,
        recordings=rec_paths,
        translation=final,
        language=prepared.lang,
        keep_transcript=early is not None,
    )
    result.refined_labels = refined
    result.silent_labels = [t for t in prepared.writers if _remarks.track_silent(t) in warnings]
    result.languages = languages
    if points:
        result.summary_md = write_summary(base, about, points)
    mp3 = (
        _mix_recording(rec_paths, base, warnings, silent=result.silent_labels, on=p_save)
        if cfg.mix_audio
        else None
    )
    if mp3 is not None:
        result.recordings.append(mp3)
    run.notes = [w.model_copy() for w in warnings]  # with steps: "Summarize" replaces its own
    run.info = list(prepared.info)
    result.remarks = _remarks.merged(warnings)  # one cause shared by several steps, once
    run.steps["save"] = _took(t0)
    p_save(1.0, "")
    return result, full


def _same_refusal(note: LightNote) -> bool:
    """Would any request be refused the same way: no model or key, or a refusal not caused by
    the text. A cloud OTHER (context overflow) is about length: a title from the start may work.
    """
    return note.cloud == Cloud.NOT_SET or note.kind is not ErrorKind.OTHER


def summarize_text(
    text: str,
    run: _meta.Run,
    warnings: list[Remark],
    on: Progress,
    *,
    lang: str | None = None,
    min_chars: int = 0,
) -> claude.Summary:
    """The shared step: title, gist and summary by the active cloud model."""
    t0 = time.monotonic()
    summary = _summarize(text, warnings, on, lang=lang)
    model = _model_of(summary.engine, summary.note)
    run.steps["summary"] = _took(t0)
    if len(unstamped(text)) >= min_chars:  # a summary was asked for, not only a title
        run.summary = _meta.LightRun(
            engine=summary.engine, model=model, lang=_lang_code(text, lang)
        )
    return summary


def _summarize(
    text: str, warnings: list[Remark], on: Progress, *, lang: str | None = None
) -> claude.Summary:
    """Title, gist and summary of the whole text by the cloud model, in lang (ru/en) or the
    call's language. A failure gives a remark and nothing else: saving must go on."""
    lang = _lang_name(text, lang)
    try:
        summary = claude.title_summary_ex(text, lang=lang, on=on)
    except Exception as e:  # noqa: BLE001 - a model failure must not stop saving
        warnings.append(_remarks.summary_failed(human_reason(e)))
        return claude.Summary()
    if remark := _remarks.light(Step.SUMMARY, summary.note):
        warnings.append(remark)
    if summary.engine != "none" and not summary.points:  # an answer without readable points
        warnings.append(_remarks.summary_garbled())
    return summary.model_copy(update={"title": summary.title.strip()})


def summarize_folder(
    base: Path,
    *,
    lang: str | None,
    on: Progress = noop,
    on_base: Callable[[Path], None] | None = None,
) -> LiveResult:
    """ "Summarize": title and summary of the folder's transcript.txt by this process's model,
    without the _SUMMARY_MIN_CHARS threshold. On success writes summary.md and renames a
    default name, keeping a part mark (on_base gets the new folder). Remarks: the previous ones
    minus title and summary, plus the new ones; the history gets Run(kind="summary"). No
    transcript.txt raises MediaError; without a model ops.admit refuses the job before this;
    cancelling before the files are written changes nothing."""
    from .persist import write_summary

    text = (_read(base / "transcript.txt") or "").strip()
    if not text:
        raise MediaError(_("No transcript. Process the recording first"))
    run = _meta.begin(LiveConfig(summary_lang=lang))
    run.kind = "summary"
    warnings: list[Remark] = []
    summary = summarize_text(text, run, warnings, on, lang=lang)  # cancelling raises here
    if not summary.title and summary.engine == "none" and summary.note is not None:
        if remark := _remarks.light(Step.TITLE, summary.note):
            warnings.append(remark)
    new = base
    if summary.title and is_default_name(base):  # "2026-10-21_08-35 (0.10–1.35)" too
        try:
            new = rename_session(base, summary.title)
        except OSError:
            logger.warning("could not rename %s", base, exc_info=True)
        if new != base and on_base is not None:
            on_base(new)
    result = LiveResult(transcript_txt=new / "transcript.txt", base=new)
    if summary.points:
        result.summary_md = write_summary(new, summary.about, summary.points)
    before = _meta.last(new)
    kept = [r for r in before.notes if r.step not in (Step.TITLE, Step.SUMMARY)] if before else []
    run.notes = [*kept, *warnings]
    run.info = list(before.info if before else [])
    result.remarks = _remarks.merged(run.notes)
    result.info = run.info
    run.finished, run.status = _meta.now(), "ok"
    _meta.append(new, run)
    return result


def _mix_recording(
    rec_paths: list[Path],
    base: Path,
    warnings: list[Remark],
    *,
    silent: Iterable[str] = (),
    on: Progress = noop,
) -> Path | None:
    """Two or more tracks with sound → one recording.mp3 (mono 64 kbit/s, ~30 MB/hour) to
    listen to the whole call. The WAV tracks stay for reprocessing. silent: tracks without
    sound as the recognizing side decided (LiveResult.silent_labels). A previous recording.mp3
    is removed: the mix has only this processing's tracks. A failure is a remark, not a crash:
    the text matters more."""
    from ..core.audio import mix

    quiet = set(silent)
    dst = base / "recording.mp3"
    dst.unlink(missing_ok=True)
    tracks = [
        p for p in rec_paths
        if p.suffix == ".wav" and p.exists() and p.stat().st_size > WAV_HEADER
        and p.stem not in quiet
    ]  # fmt: skip
    if len(tracks) < 2:
        return None
    try:
        return mix(tracks, dst, args=["-ac", "1", "-c:a", "libmp3lame", "-b:a", "64k"], on=on)
    except Exception as e:  # noqa: BLE001
        warnings.append(_remarks.mix_failed(str(e) or type(e).__name__))
        return None


_SUMMARY_MIN_CHARS = 600  # ~a minute of speech; shorter gets no summary.md
PROCESSING_MARK = ".mnema-processing"  # running work: the node request and the user's title


def processing_interrupted(marker: str) -> bool:
    """Does the processing mark mean interrupted work: it has a node request or is unreadable.
    Without a request it only holds the user's title: processing never reached the node."""
    try:
        data = json.loads(marker)
    except ValueError:
        return True
    return not isinstance(data, dict) or "node" in data


class FinalizeCancelled(BaseException):  # noqa: N818
    """The user stopped processing. A BaseException like KeyboardInterrupt: best-effort steps
    (title, summary) catch Exception and must not swallow the cancellation."""


def keep_audio_only(base: Path, *, started: str | None = None) -> None:
    """Processing was stopped: the audio stays as is, to cut and transcribe later. The history
    gets a stopped run (started: when it began), so a recording without an earlier outcome is
    "audio only" and an earlier outcome stays; the work file is removed."""
    finished = _meta.now()
    with FILES_LOCK:
        _meta.append(
            base, _meta.Run(started=started or finished, finished=finished, status="stopped")
        )
        try:
            (base / PROCESSING_MARK).unlink(missing_ok=True)
        except OSError:
            logger.warning("could not remove the work file of %s", base, exc_info=True)


def finish_command(base: Path) -> str:
    """The terminal command that fully processes a recording folder."""
    import shlex

    return f"mnema finish {shlex.quote(str(base))}"


def work_of(folder: Path, files: Iterable[str]) -> _meta.Work:
    """The recording's work file as this computer sees it (the caller excludes the one being
    processed now): a node request or an unreadable mark means interrupted work; a title alone
    means processing never reached the node, so there is no work."""
    if PROCESSING_MARK not in files:
        return _meta.Work.NONE
    try:
        marker = (folder / PROCESSING_MARK).read_text(encoding="utf-8")
    except FileNotFoundError:
        return _meta.Work.NONE
    except (OSError, UnicodeDecodeError):
        return _meta.Work.INTERRUPTED
    return _meta.Work.INTERRUPTED if processing_interrupted(marker) else _meta.Work.NONE


def folder_state(
    folder: Path, files: Mapping[str, os.stat_result], data: Any = None
) -> _meta.RecordState:
    """Recording folder state by the one rule (meta.record_state): its files, history (data is
    meta already read; None reads it) and work."""
    if data is None:
        try:
            data = json.loads((folder / _meta.META_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
    sizes = {name: st.st_size for name, st in files.items()}
    return _meta.record_state(sizes, data, work_of(folder, files))


def unfinished_sessions(root: Path, *, limit: int = 3, exclude: Path | None = None) -> list[Path]:
    """Recordings with audio waiting for "Process…" (meta.UNFINISHED): not processed (cut off
    before or during processing) or the last processing failed. Newest first by last change;
    exclude is the one being processed now. Section recordings count too (library/walk); a
    section holding recording files is not a recording."""
    from ..library.layout import Kind
    from ..library.walk import walk
    from ..sync.state import known_sections

    found: list[tuple[float, Path]] = []
    for folder in walk(root, known=known_sections()):
        files = folder.files
        if folder.kind is not Kind.RECORD or folder.path == exclude:
            continue
        sizes = {name: st.st_size for name, st in files.items()}
        if _meta.has_sound(sizes) and folder_state(folder.path, files) in _meta.UNFINISHED:
            found.append((max(st.st_mtime for st in files.values()), folder.path))
    return [d for _mtime, d in sorted(found, reverse=True)[:limit]]


# Parses user input, Russian units included. After "час", "мин", "сек" only letters follow, so
# "1час30мин" keeps its minutes (fixture offsets-v1).
_UNITS = re.compile(
    r"^(?:([0-9]+)\s*(?:ч|час[^\W\d_]*|h))?\s*(?:([0-9]+)\s*(?:м|мин[^\W\d_]*|m|min))?\s*"
    r"(?:([0-9]+)\s*(?:с|сек[^\W\d_]*|s|sec))?$"
)


def parse_offset(value: str) -> float:
    """A place in the recording → seconds. Accepts "1:05:30" (h:mm:ss), "1:35" (h:mm), "1h 35m",
    "35 min", "90m", "20 sec"; a bare number is minutes ("90" is an hour and a half). Hours and
    minutes, not minutes and seconds: ranges are cut from long, forgotten recordings, and the
    window fills in the full "0:00:00" form, so the format is visible."""
    v = value.strip().lower().replace(",", ".")
    if not v:
        raise MediaError(_("No time given: use hours:minutes:seconds, such as 1:05:30"))
    units = _UNITS.match(v)
    if units and any(units.groups()):
        h, m, sec = (int(g or 0) for g in units.groups())
        return h * 3600.0 + m * 60.0 + sec
    parts = v.replace(".", ":").split(":")
    if not all(p.isascii() and p.isdigit() for p in parts) or len(parts) > 3:
        raise MediaError(
            _(
                "Couldn't read the time “{value}”: use hours:minutes:seconds (1:05:30),"
                " “1h 35m” or “35 min”",
                value=value,
            )
        )
    nums = [int(p) for p in parts]
    if len(nums) == 1:
        return nums[0] * 60.0
    h, m, sec = (nums + [0])[:3]
    if m >= 60 or sec >= 60:
        raise MediaError(
            _("Couldn't read the time “{value}”: minutes and seconds go up to 59", value=value)
        )
    return h * 3600.0 + m * 60.0 + sec


def fmt_clock(seconds: float) -> str:
    """Seconds → "h:mm:ss", always the full form so the range window shows the format."""
    total = int(seconds)
    return f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"


def recording_length(folder: Path) -> float:
    """Recording length in the folder (the longest WAV track), seconds; 0 without audio."""
    return max((wav_seconds(w) for w in folder.glob("*.wav") if w.is_file()), default=0.0)


def repair_wav(path: Path) -> bool:
    """A process killed mid-recording may leave the WAV header length unwritten, so the file
    reads shorter (or empty). Recompute it from the file size. True when fixed.

    Only the layout WavTrackWriter writes is repaired: RIFF/WAVE, PCM "fmt " at byte 12, "data"
    right after it at byte 36, nothing after the audio. Other WAVs (tags after the audio,
    metadata before it, over 4 GB) are left alone: rewriting someone else's file in place could
    turn a LIST tag into noise at the end of the recording."""
    import struct

    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            head = f.read(44)
        if (
            len(head) < 44
            or head[:4] != b"RIFF"
            or head[8:16] != b"WAVEfmt "
            or struct.unpack("<I", head[16:20])[0] != 16
            or struct.unpack("<H", head[20:22])[0] != 1  # PCM
            or head[36:40] != b"data"
            or size - 8 > 0xFFFFFFFF
        ):
            return False
        align = max(1, struct.unpack("<H", head[32:34])[0])
        declared = struct.unpack("<I", head[40:44])[0]
        data = size - 44
        data -= data % align
        if declared >= data:
            return False  # not cut off (or claims more than there is: not ours)
        with path.open("rb") as f:  # a chunk after the declared audio: not our file
            if chunk_follows(f, 44 + declared + declared % 2, size):
                return False
        st = path.stat()
        with path.open("r+b") as f:
            f.seek(4)
            f.write(struct.pack("<I", 36 + data))
            f.seek(40)
            f.write(struct.pack("<I", data))
        os.utime(path, (st.st_atime, st.st_mtime))  # keep the recording's time, not the repair's
        return True
    except (OSError, struct.error):
        return False


def _slice_wav(src: Path, dst: Path, start: float, end: float | None) -> None:
    """A [start, end) slice of a WAV track in the same format; the source is untouched."""
    import contextlib
    import wave

    dst.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(wave.open(str(src), "rb")) as r:
        rate = r.getframerate()
        first = int(start * rate)
        last = r.getnframes() if end is None else min(int(end * rate), r.getnframes())
        r.setpos(min(first, r.getnframes()))
        with contextlib.closing(wave.open(str(dst), "wb")) as w:
            w.setparams(r.getparams())
            left = max(0, last - first)
            while left > 0:
                frames = r.readframes(min(left, rate * 60))
                if not frames:
                    break
                w.writeframes(frames)
                left -= len(frames) // (r.getsampwidth() * r.getnchannels())
    st = src.stat()
    os.utime(dst, (st.st_atime, st.st_mtime))  # the part dates from the recording


_RECORDING_FILES = {
    "mic.wav", "system.wav", "recording.mp3", "transcript.txt", "summary.md", "translation.md",
    ".DS_Store",
}  # fmt: skip


def is_recording_folder(path: Path) -> bool:
    """A Mnema recording folder: mic/system tracks and their results, no subfolders or other
    files. Only such a folder may go to the Trash whole when a part replaces it (picking
    Downloads with one .wav must not trash all of Downloads)."""
    try:
        items = list(path.iterdir())
    except OSError:
        return False
    names = {p.name for p in items}
    return bool(names & {"mic.wav", "system.wav"}) and all(
        p.is_file() and (p.name in _RECORDING_FILES or p.name.startswith(".mnema")) for p in items
    )


def _moved(path: Path | None, old: Path, new: Path) -> Path | None:
    if path is None or old == new or not path.is_relative_to(old):
        return path
    return new / path.relative_to(old)


def _keep_original_reason(result: LiveResult, writers: dict[str, Any], base: Path) -> str | None:
    """Why the full recording must NOT be removed although asked: the part is not transcribed
    (recognition failed, the server was unreachable and processing deferred, no text) or the
    folder is not a Mnema recording. None when it may go."""
    if not is_recording_folder(base):
        return _("the folder has other files")
    if result.defer_command or result.transcript_txt is None:
        return _("the part of the recording is not transcribed")
    missed = [
        label
        for label in writers
        if label not in result.refined_labels and label not in result.silent_labels
    ]
    return _("the part of the recording is only partly transcribed") if missed else None


def recover_session(
    base: Path,
    config: LiveConfig,
    *,
    retitle: bool | None = None,
    user_title: Callable[[], str | None] | None = None,
    recording_error: str | None = None,
    on: Progress = noop,
) -> tuple[LiveResult, str]:
    """Process a recording on the node with the usual finalization steps: recognition of the
    folder's WAV tracks, title, summary. The client cuts a part itself and sends only it.
    retitle: title from the content (None: only in place of a default name); user_title: the
    user's own title. On interruption the exception carries mnema_base, the current folder
    (it may have been renamed)."""
    prepared, label, work = prepare_recovery(base, config, recording_error=recording_error)
    if config.translate:
        prepared.dialog = _translate.LiveDialog(
            config.translate,
            make_translator=_translate.make_live_translator,
        )
    tracks = [Track(label=label_) for label_ in prepared.writers]
    try:
        result, full = finalize_live(
            prepared,
            tracks,
            retitle=is_default_name(base) if retitle is None else retitle,
            user_title=user_title,
            on=on,
        )
    except BaseException as e:
        setattr(e, "mnema_base", prepared.base)  # noqa: B010 - where to look after a stop
        raise
    return finish_recovery(base, work, label, prepared, result), full


def prepare_recovery(
    base: Path,
    config: LiveConfig,
    *,
    start: float | None = None,
    end: float | None = None,
    recording_error: str | None = None,
    on_base: Callable[[Path], None] | None = None,
) -> tuple[PreparedLive, str | None, Path]:
    """Audio files only: repair the WAVs and prepare a separate part of the recording."""
    from .record import WavTrackWriter

    wavs = [w for w in sorted(base.glob("*.wav")) if w.stat().st_size > WAV_HEADER]
    if not wavs:
        raise MediaError(_("{name} has no recorded audio", name=base.name))
    info: list[str] = []
    repaired = [w.name for w in wavs if repair_wav(w)]
    if repaired:
        info.append(
            _("The recording was cut off, length restored: {files}", files=", ".join(repaired))
        )
    work, label, part = base, None, None
    if start is not None or end is not None:
        length = max(wav_seconds(w) for w in wavs)
        a, b = start or 0.0, min(end, length) if end is not None else length
        if a >= length:
            raise MediaError(
                _(
                    "Start {start} is after the end of the recording ({length})",
                    start=fmt_clock(a),
                    length=fmt_clock(length),
                )
            )
        if b <= a:
            raise MediaError(_("The end of the range is before its start"))
        part = {"start": round(a, 1), "end": round(b, 1)}
        label = part_of(a, b)
        need = len(wavs) * int((b - a) * 16000 * 2) + 16 * 2**20  # PCM16 mono 16 kHz
        if shutil.disk_usage(base.parent).free < need:
            raise MediaError(
                _(
                    "Not enough disk space: the part of the recording needs ~{size} MB",
                    size=need // 2**20,
                )
            )
        work = free_name(base.with_name(f"{base.name} ({label})"))
        try:
            with FILES_LOCK:
                started = _meta.started(base) or started_iso(base)  # the original's start
                work.mkdir()
                if started is not None:
                    _meta.set_started(work, started)
                if on_base is not None:
                    on_base(work)
            for w in wavs:
                _slice_wav(w, work / w.name, a, b)
        except BaseException:  # Ctrl-C or a full disk: no half-written part
            shutil.rmtree(work, ignore_errors=True)
            raise
        wavs = sorted(work.glob("*.wav"))
        info.append(
            _(
                "Part of the recording {start}–{end} of {length}",
                start=fmt_clock(a),
                end=fmt_clock(b),
                length=fmt_clock(length),
            )
        )
    writers = {w.stem: WavTrackWriter(w) for w in wavs}
    prepared = PreparedLive(
        config=config.model_copy(update={"stream": False}),
        base=work,
        sources=[],
        writers=writers,
        dialog=None,
        session_dialog=None,
        transcribers={},
        lang=config.lang,
        status="",
    )
    prepared.part = part
    prepared.recording_error = recording_error
    # Client notes go into this processing's history; "Full recording moved…" comes after it.
    prepared.info = info
    return prepared, label, work


def finish_recovery(
    base: Path,
    work: Path,
    label: str | None,
    prepared: PreparedLive,
    result: LiveResult,
    *,
    replace_original: Callable[[Path], None] | None = None,
    on_base: Callable[[Path], None] | None = None,
) -> LiveResult:
    """After the result: the part mark and, when chosen, replacing the original via the Trash."""
    writers, info = prepared.writers, prepared.info
    if label:
        old = prepared.base
        name = old.name  # a rename keeps the part mark
        if replace_original is not None:
            keep = _keep_original_reason(result, writers, base)
            if keep is None:
                try:
                    replace_original(base)
                except Exception as e:  # noqa: BLE001 - the full one stays, the part keeps its mark
                    keep = (str(e).strip().splitlines() or [type(e).__name__])[-1][:120]
            if keep is None:
                info.append(_("Full recording moved to the Trash"))
                # The part takes the full one's place: no mark, and a "(2)" caused by it goes.
                name = base.name if old == work else PART_LABEL.sub("", old.name)
            else:
                info.append(_("Full recording kept: {reason}", reason=keep))
        with FILES_LOCK:
            new = free_name(old.with_name(name), own=old)
            if new != old:
                try:
                    old.rename(new)
                except OSError:
                    new = old
            result.transcript_txt = _moved(result.transcript_txt, old, new)
            result.summary_md = _moved(result.summary_md, old, new)
            result.translation_md = _moved(result.translation_md, old, new)
            result.recordings = [p for p in (_moved(r, old, new) for r in result.recordings) if p]
            prepared.base = new
            if on_base is not None:
                on_base(new)
    result.recordings = [p for p in result.recordings if p.exists()]
    result.info = info
    result.base = prepared.base
    return result
