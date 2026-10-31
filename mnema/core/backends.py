"""Transcription engines: Parakeet Ultra on this machine and OpenAI in the cloud.

Heavy imports are lazy, inside methods, so the module imports on any platform. ENGINES is the
registry: automatic choice, CLI --backend, the app's and the server's Recognition list all
follow it.
"""

from __future__ import annotations

import logging
import tempfile
import threading
from pathlib import Path
from typing import Any, ClassVar, Protocol, runtime_checkable

from ..i18n import _
from . import asr_engine, parakeet, vad
from .asr_engine import REPLY_GAP_S, SR, group_speech
from .audio import extract_audio
from .base import MediaError, Progress, noop
from .models import Segment, Transcript

logger = logging.getLogger(__name__)


AUTO = "auto"


@runtime_checkable
class TranscriptionBackend(Protocol):
    """Backend contract: recognize a source file into a Transcript."""

    name: str
    model: str  # the engine's model or weights; the CLI panel shows the actual choice

    def available(self) -> bool:
        """Whether the backend works on this machine (platform, dependencies)."""
        ...

    def run(
        self,
        src: Path,
        *,
        lang: str | None = None,
        total_duration: float = 0.0,
        on: Progress = noop,
    ) -> Transcript: ...


class Engine:
    """A recognition engine. A new one is a subclass listed in ENGINES plus its pyproject
    extra; the CLI, the app's and the server's Recognition lists follow from ENGINES."""

    id: ClassVar[str]  # the choice in settings, bar.json and the CLI
    title: ClassVar[str]  # a product name, shown as is
    extra: ClassVar[str]  # the pyproject extra that installs it
    langs: ClassVar[frozenset[str] | None] = None  # None: any language
    names_lang: ClassVar[bool] = True  # recognizes without a given language and names it
    cloud: ClassVar[bool] = False  # sends the audio out: chosen only by name, never automatically
    name: str  # recorded in history as the track's engine
    model: str

    @classmethod
    def create(cls, model: str | None = None) -> TranscriptionBackend:
        """The backend for an explicit choice; model applies to engines with several."""
        return cls()

    @classmethod
    def supports(cls, lang: str | None) -> bool:
        return lang is None or cls.langs is None or lang in cls.langs

    @classmethod
    def automatic_for(cls, lang: str | None) -> bool:
        """Automatic choice takes it for this language (None: not given)."""
        if cls.cloud:
            return False
        return cls.names_lang if lang is None else cls.supports(lang)

    def available(self) -> bool:
        raise NotImplementedError

    def run(
        self,
        src: Path,
        *,
        lang: str | None = None,
        total_duration: float = 0.0,
        on: Progress = noop,
    ) -> Transcript:
        raise NotImplementedError


def _asr_cpu_threads() -> int:
    """auto uses every available CPU; MNEMA_ASR_THREADS sets a manual limit."""
    from .settings import available_cpu_threads, requested_asr_threads

    requested = requested_asr_threads()
    available = available_cpu_threads()
    return available if requested == "auto" else min(requested, available)


ONLY_CLOUD = "This language is recognized only in the cloud"


class OnlyCloud(MediaError):
    """A language no local engine has: the remark says so; the audio never goes out by itself."""

    def __init__(self) -> None:
        super().__init__(_(ONLY_CLOUD))


class ParakeetBackend(Engine):
    """Parakeet Ultra, the default: 25 European languages, the language named from the text.
    Recognition is this machine's Recognizer (asr_engine.local_recognizer)."""

    id = name = "parakeet"
    title, extra, langs = "Parakeet", "parakeet", parakeet.LANGS
    model = parakeet.MODEL

    def available(self) -> bool:
        return asr_engine.local_recognizer().available()

    def run(
        self,
        src: Path,
        *,
        lang: str | None = None,
        total_duration: float = 0.0,
        on: Progress = noop,
    ) -> Transcript:
        """A given language only checks the choice and cleans the replies: the model recognizes
        without a hint; without one the language is named from the recognized text."""
        if not self.supports(lang):
            raise OnlyCloud
        with tempfile.TemporaryDirectory() as tmp:
            wav = extract_audio(src, Path(tmp) / "audio.wav", pad_start_ms=300, on=on)
            words = asr_engine.local_recognizer().words(wav, on=on)
        segments = asr_engine.replies(words)
        if lang is None:
            lang = parakeet.guess_lang(" ".join(s.text for s in segments))
        segments = parakeet.tidy(segments, lang)
        text = " ".join(s.text for s in segments).strip()
        return Transcript(text=text, segments=segments, language=lang)


# OpenAI in the cloud: the user's choice is the model, the key is the user's recognition key
CLOUD_MODELS = ("openai/gpt-4o-transcribe", "openai/gpt-4o-mini-transcribe")
NO_CLOUD_KEY = "No OpenAI key for recognition. Set it in Mnema settings on the Mac"
_CLOUD_PART_S = 150.0  # the models answer up to 2,000 tokens: a longer part would be cut short
_CLOUD_PROMPT_WORDS = 200
_CLOUD_REQUESTS = 4


class NoCloudKey(MediaError):
    """Cloud recognition without the user's OpenAI key: the remark says so, not "an error"."""

    def __init__(self) -> None:
        super().__init__(_(NO_CLOUD_KEY))


def is_cloud(choice: str | None) -> bool:
    """Whether the choice is cloud recognition: the audio goes to OpenAI."""
    return choice in CLOUD_MODELS


def _is_track(src: Path) -> bool:
    """Already PCM16 mono 16 kHz (a Mnema track): no extraction needed."""
    import wave

    try:
        with wave.open(str(src)) as w:
            return (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, SR)
    except (OSError, EOFError, wave.Error):
        return False


def _wav_bytes(wav: Path, start: int, end: int) -> bytes:
    """Samples [start, end) of the track as a separate in-memory WAV."""
    import io
    import wave

    with wave.open(str(wav)) as src:
        src.setpos(start)
        frames = src.readframes(end - start)
        params = src.getparams()
    out = io.BytesIO()
    with wave.open(out, "wb") as dst:
        dst.setparams(params)
        dst.writeframes(frames)
    return out.getvalue()


def _utterances(speech: list[tuple[int, int]]) -> list[list[tuple[int, int]]]:
    """VAD speech (samples) → utterances split at pauses as replies are, each in parts of at most
    _CLOUD_PART_S; a part follows the previous one of its utterance without a pause."""
    gap = int(REPLY_GAP_S * SR)
    out: list[list[tuple[int, int]]] = []
    for part in group_speech(speech, int(_CLOUD_PART_S * SR), gap):
        if out and part[0] - out[-1][-1][1] <= gap:
            out[-1].append(part)
        else:
            out.append([part])
    return out


def _answer_text(answer: Any) -> str:
    text = answer.get("text") if isinstance(answer, dict) else getattr(answer, "text", None)
    return str(text or "").strip()


class CloudBackend(Engine):
    """OpenAI recognition: this machine's speech search cuts the track into utterances, each one
    request (a long one in parts) with the track's previous text as a prompt; several requests
    at once. A failure after retries propagates: no other engine takes over."""

    id = "cloud"
    title, extra, names_lang, cloud = "OpenAI", "parakeet", False, True

    def __init__(self, model: str = CLOUD_MODELS[0], key: str | None = None):
        self.name = self.model = model
        self.key = key  # comes with the job through the pipe, never from this process's files

    @classmethod
    def create(cls, model: str | None = None) -> TranscriptionBackend:
        return cls(model or CLOUD_MODELS[0])

    def available(self) -> bool:
        import importlib.util

        return importlib.util.find_spec("litellm") is not None and vad.available()

    def run(
        self,
        src: Path,
        *,
        lang: str | None = None,
        total_duration: float = 0.0,
        on: Progress = noop,
    ) -> Transcript:
        if not self.key:
            raise NoCloudKey
        with tempfile.TemporaryDirectory() as tmp:
            wav = src if _is_track(src) else extract_audio(src, Path(tmp) / "audio.wav", on=on)
            on(None, _("Looking for speech…"))
            speech = asr_engine.local_recognizer().speech(wav, max_s=_CLOUD_PART_S)
            segments = self._recognize(wav, _utterances(speech), lang, on)
        text = " ".join(s.text for s in segments)
        return Transcript(text=text, segments=segments, language=lang)

    def _recognize(
        self, wav: Path, utterances: list[list[tuple[int, int]]], lang: str | None, on: Progress
    ) -> list[Segment]:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        lock = threading.Lock()
        done: dict[int, list[Segment]] = {}

        def report(fraction: float | None, message: str) -> None:
            with lock:
                on(fraction, message)

        def heard_before(index: int) -> list[str]:
            """Words of the track so far: the utterances recognized in a row from the start."""
            with lock:
                words: list[str] = []
                for i in range(index):
                    if i not in done:
                        break
                    words += [w for s in done[i] for w in s.text.split()]
                return words

        def recognize(index: int) -> None:
            found: list[Segment] = []
            before = heard_before(index)
            for s, e in utterances[index]:
                words = before + [w for x in found for w in x.text.split()]
                prompt = " ".join(words[-_CLOUD_PROMPT_WORDS:]) or None
                text = self._ask(_wav_bytes(wav, s, e), lang, prompt, report)
                if text:
                    found.append(Segment(start=s / SR, end=e / SR, text=text))
            with lock:
                done[index] = found

        on(0.0, _("Recognizing…"))
        pool = ThreadPoolExecutor(_CLOUD_REQUESTS, thread_name_prefix="cloud-asr")
        try:
            futures = [pool.submit(recognize, i) for i in range(len(utterances))]
            for count, future in enumerate(as_completed(futures), 1):
                future.result()
                report(count / len(futures), _("Recognizing…"))
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        return [s for i in range(len(utterances)) for s in done[i]]

    def _ask(self, data: bytes, lang: str | None, prompt: str | None, on: Progress) -> str:
        """One request; overload, 5xx and timeouts are retried as for text models."""
        import litellm

        from ..llm.claude import _pause, _retry_wait

        litellm.suppress_debug_info = True
        retries = 0
        while True:
            try:
                answer = litellm.transcription(
                    model=self.model, file=("utterance.wav", data), api_key=self.key,
                    language=lang, prompt=prompt,
                )  # fmt: skip
            except Exception as e:
                wait = _retry_wait(e, retries, live=False, started=False)
                if wait is None:
                    raise
                retries += 1
                _pause(wait, on)
                continue
            return _answer_text(answer)


# The registry: the local engine first, then the cloud.
ENGINES: tuple[type[Engine], ...] = (ParakeetBackend, CloudBackend)
# Saved choices of earlier versions (bar.json, the phone, server jobs, an old CLI): exact ids.
_RETIRED = frozenset(
    {"whisper", "gigaam", "mlx", "whisperx", "groq/whisper-large-v3", "groq/whisper-large-v3-turbo"}
)


def retired(choice: str) -> str:
    """The choice as it works now: an engine or a model of an earlier version is automatic."""
    return AUTO if choice in _RETIRED else choice


def _no_engine() -> str:
    return _(
        "No transcription engine is installed: install the full extra (uv sync --extra full) "
        "or set a runner (--runner/MNEMA_RUNNER)."
    )


def engine(choice: str) -> type[Engine] | None:
    """The registered engine of this choice, or None (auto, unknown)."""
    if is_cloud(choice):
        return next((e for e in ENGINES if e.cloud), None)
    return next((e for e in ENGINES if not e.cloud and e.id == choice), None)


def installed_engines() -> list[type[Engine]]:
    """Registered local engines this machine can run (packages only, no models)."""
    return [e for e in ENGINES if not e.cloud and e().available()]


def backend_choices() -> list[str]:
    """CLI --backend: automatic, every local engine and the cloud models."""
    return [AUTO, *(e.id for e in ENGINES if not e.cloud), *CLOUD_MODELS]


def engines_available() -> bool:
    """Whether this machine has any transcription engine. Loads no models."""
    return bool(installed_engines())


def select_backend(
    name: str = AUTO,
    *,
    model: str | None = None,
    lang: str | None = None,
) -> TranscriptionBackend:
    """The backend by name, or by automatic choice.

    An engine named explicitly is returned as is (the caller checks available()). AUTO: the
    first installed local engine that takes the language; a language none of them has is
    recognized only in the cloud, and the audio never goes there by itself.
    """
    name = str(name)
    found = engine(name)
    if found is not None:
        return found.create(name if found.cloud else model)
    if name != AUTO:
        allowed = ", ".join(backend_choices())
        raise MediaError(_("Unknown backend: {name} ({allowed})", name=name, allowed=allowed))
    local = [e for e in ENGINES if not e.cloud]
    if not any(e.supports(lang) for e in local):
        raise OnlyCloud
    for candidate in local:
        if candidate.automatic_for(lang) and candidate().available():
            return candidate.create(model)
    raise MediaError(_no_engine())
