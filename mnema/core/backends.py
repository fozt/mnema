"""Local transcription engines: Parakeet, GigaAM, Whisper (mlx-whisper on Apple Silicon,
whisperX elsewhere).

Heavy imports (mlx_whisper, whisperx, torch) are lazy, inside methods, so the module imports
on any platform. ENGINES is the registry: automatic choice, CLI --backend, the app's and the
server's Recognition list all follow it.
"""

from __future__ import annotations

import contextlib
import logging
import os
import platform
import re
import sys
import tempfile
import threading
import warnings
from pathlib import Path
from typing import Any, ClassVar, Protocol, runtime_checkable

from ..i18n import _
from . import parakeet
from .audio import extract_audio
from .base import (
    DEFAULT_LOCAL_MODEL,
    DEFAULT_WHISPERX_MODEL,
    MediaError,
    Progress,
    noop,
    ts_to_sec,
    vram_status,
)
from .models import Segment, Transcript

logger = logging.getLogger(__name__)
# torch C++ warnings (silero's "quantize_per_tensor … deprecated") bypass Python and print
# to stderr over the UI; keep errors only.
os.environ.setdefault("TORCH_CPP_LOG_LEVEL", "ERROR")


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
    """A local recognition engine. A new one is a subclass listed in ENGINES plus its pyproject
    extra; the CLI, the app's and the server's Recognition lists follow from ENGINES."""

    id: ClassVar[str]  # the choice in settings, bar.json and the CLI
    title: ClassVar[str]  # a product name, shown as is
    extra: ClassVar[str]  # the pyproject extra that installs it
    langs: ClassVar[frozenset[str] | None] = None  # None: any language
    names_lang: ClassVar[bool] = True  # recognizes without a given language and names it
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


def _hf_cache_dir() -> Path:
    base = os.environ.get("HF_HOME")
    return (Path(base) / "hub") if base else (Path.home() / ".cache" / "huggingface" / "hub")


def local_snapshot_dir(repo: str) -> Path | None:
    """The local snapshot folder with weights, or None. File checks only, no network."""
    folder = _hf_cache_dir() / ("models--" + repo.replace("/", "--"))
    candidates: list[Path] = []
    ref = folder / "refs" / "main"
    if ref.is_file():
        candidates.append(folder / "snapshots" / ref.read_text().strip())
    snaps = folder / "snapshots"
    if snaps.is_dir():
        candidates.extend(snaps.iterdir())
    for snap in candidates:
        w = snap / "weights.safetensors"
        if w.exists() and w.stat().st_size > 0:
            return snap
    return None


def resolve_model_path(repo: str, on: Progress = noop) -> str:
    """The local model path for mlx, downloading it if needed.

    Passing a folder instead of a repo id avoids HF's network check on every call, which
    could hang transcription.
    """
    p = Path(repo)
    if p.is_dir() and (p / "weights.safetensors").exists():
        return str(p)
    snap = local_snapshot_dir(repo)
    if snap is not None:
        return str(snap)
    on(None, _("Downloading the recognition model (only once)…"))
    try:
        from huggingface_hub import snapshot_download
    except ImportError as e:
        raise MediaError(_("huggingface_hub is missing — reinstall mlx-whisper.")) from e
    snapshot_download(repo_id=repo)
    snap = local_snapshot_dir(repo)
    if snap is None:
        raise MediaError(
            _(
                "The model {repo} did not finish downloading — check the network and try again.",
                repo=repo,
            )
        )
    return str(snap)


class _SegmentParser:
    """Parses mlx_whisper verbose output line by line and reports progress."""

    _SEG = re.compile(r"^\[([\d:.]+)\s*-->\s*([\d:.]+)\]\s?(.*)$")

    def __init__(self, total: float, on: Progress):
        self.total, self.on = total, on
        self.buf = ""
        self.segments: list[Segment] = []
        self.language: str | None = None

    def write(self, s: str) -> int:
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self._handle(line.strip())
        return len(s)

    def _handle(self, line: str) -> None:
        if not line:
            return
        if line.startswith("Detected language:"):
            self.language = line.split(":", 1)[1].strip()
            return
        m = self._SEG.match(line)
        if m:
            start, end = ts_to_sec(m.group(1)), ts_to_sec(m.group(2))
            text = m.group(3).strip()
            self.segments.append(Segment(start=start, end=end, text=text))
            self.on(min(end / self.total, 1.0) if self.total else None, _("Recognizing…"))

    def flush(self) -> None:
        pass


class MlxBackend:
    name = "mlx"

    def __init__(self, model: str = DEFAULT_LOCAL_MODEL):
        self.model = model

    def available(self) -> bool:
        import importlib.util

        return (
            sys.platform == "darwin"
            and platform.machine() == "arm64"
            and importlib.util.find_spec("mlx_whisper") is not None
        )

    def run(
        self,
        src: Path,
        *,
        lang: str | None = None,
        total_duration: float = 0.0,
        on: Progress = noop,
    ) -> Transcript:
        model_path = resolve_model_path(self.model, on)
        import mlx_whisper

        with tempfile.TemporaryDirectory() as tmp:
            # 0.3 s of leading silence protects the first word, as on the whisperx path
            wav = extract_audio(src, Path(tmp) / "audio.wav", pad_start_ms=300, on=on)
            # Weights load inside transcribe() before the first segment; name the phase so 0%
            # does not look stuck.
            on(None, _("Loading the recognition model…"))
            parser = _SegmentParser(total_duration, on)
            with contextlib.redirect_stdout(parser):
                # condition_on_previous_text=False: otherwise Whisper loops in repeats
                # ("Hello ×3") and drags mixed speech towards the first language it heard.
                # word_timestamps: segments are cut at pauses, so the two tracks' replies
                # interleave in exact time order (see _split_by_pauses).
                result: dict[str, Any] = mlx_whisper.transcribe(
                    str(wav),
                    path_or_hf_repo=model_path,
                    language=lang,
                    verbose=True,
                    condition_on_previous_text=False,
                    word_timestamps=True,
                )
            text = str(result["text"]).strip()
            # result["language"] is a code ("ru"); the verbose line gives a name ("Russian"),
            # used only as a fallback
            language = lang or result.get("language") or parser.language
            raw_segments = result.get("segments", [])
            if any(s.get("words") for s in raw_segments):
                segments = _split_by_pauses(raw_segments)
            else:
                segments = parser.segments or [
                    Segment(
                        start=float(s["start"]), end=float(s["end"]), text=str(s["text"]).strip()
                    )
                    for s in raw_segments
                ]
            tr = Transcript(text=text, segments=segments, language=language)
        return tr


def _split_by_pauses(raw_segments: list[dict], max_gap: float = 0.6) -> list[Segment]:
    """Whisper segments with word times → replies, a new reply at a pause > max_gap.

    Whisper glues continuous speech into windows of up to 30 s with one start time, so in a
    two-track dialogue the other side's reply landed after the whole window. Words without a
    time join the current reply.
    """
    out: list[Segment] = []
    cur: list[dict] = []

    def flush() -> None:
        text = "".join(str(w.get("word", "")) for w in cur).strip()
        if text:
            out.append(Segment(start=float(cur[0]["start"]), end=float(cur[-1]["end"]), text=text))
        cur.clear()

    for seg in raw_segments:
        words = seg.get("words") or []
        if not words:
            if cur:
                flush()
            text = str(seg.get("text", "")).strip()
            if text:
                out.append(Segment(start=float(seg["start"]), end=float(seg["end"]), text=text))
            continue
        for w in words:
            if "start" not in w or "end" not in w:
                if cur:
                    cur.append({**w, "start": cur[-1]["end"], "end": cur[-1]["end"]})
                continue
            if cur and float(w["start"]) - float(cur[-1]["end"]) > max_gap:
                flush()
            cur.append(w)
    if cur:
        flush()
    return out


# One lock for LOADING models on CUDA (gigaam/torch, ct2 large-v3, whisperx): initializing two
# CUDA stacks concurrently can poison the process's CUDA context ("out of memory" with free
# VRAM, cured only by a restart). Inference does not take it. RLock: _whisperx_model →
# shared_whisper_model takes it twice on one thread.
_CUDA_LOAD_LOCK = threading.RLock()

# GigaAM (Conformer) is strong on conversational Russian; v3_e2e_rnnt adds punctuation and
# casing itself. Russian only. One lock for the model: calls are short, no shared state.
_GIGAAM_LOCK = threading.Lock()
_GIGAAM_MODEL: Any = None
# The package's short-inference limit (LONGFORM_THRESHOLD) is 25.0; ours keeps 1 s of margin,
# since the package's behaviour right at the limit is not guaranteed.
_GIGAAM_MODEL_NAME = "v3_e2e_rnnt"
_GIGAAM_SHORT_MAX_S = 24.0


@contextlib.contextmanager
def _quiet_third_party():
    """Silence library noise during the call: gigaam and torch warn into the root logger and
    tqdm prints weight-download progress (~450 MB) to stderr over the terminal UI. Errors are
    kept; our on() shows the phase."""
    import io
    import warnings as _warnings

    with _warnings.catch_warnings(), contextlib.redirect_stderr(io.StringIO()):
        _warnings.simplefilter("ignore")
        logging.disable(logging.WARNING)
        try:
            yield
        finally:
            logging.disable(logging.NOTSET)


def _gigaam_model() -> Any:
    """Lazy GigaAM (v3_e2e_rnnt), cached per process; the package downloads the weights
    (~450 MB) and tokenizer and verifies the checksum."""
    global _GIGAAM_MODEL
    if _GIGAAM_MODEL is None:
        import gigaam

        # weights go to the Mnema XDG cache, not ~/.cache/gigaam
        cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "mnema"
        with _CUDA_LOAD_LOCK, _quiet_third_party():
            if _GIGAAM_MODEL is None:  # re-check: another thread may have loaded it
                _GIGAAM_MODEL = gigaam.load_model(
                    _GIGAAM_MODEL_NAME, download_root=str(cache / "gigaam")
                )
    return _GIGAAM_MODEL


_GIGAAM_SR = 16000
# Windows ≤ 22 s (margin below the 25 s short-inference limit).
_GIGAAM_WINDOW_S = 22.0
_SILERO_VAD: Any = None


@contextlib.contextmanager
def _torch_threads(count: int) -> Any:
    """A temporary thread budget under _GIGAAM_LOCK; the previous one is restored after errors.

    Silero sets torch to one thread on first import. Its speech search stays single-threaded,
    but that choice must not stick to the next recognition.
    """
    import torch

    before = torch.get_num_threads()
    try:
        if before != count:
            torch.set_num_threads(count)
        yield
    finally:
        if torch.get_num_threads() != before:
            torch.set_num_threads(before)


def _asr_cpu_threads() -> int:
    """auto uses every available CPU; MNEMA_ASR_THREADS sets a manual limit."""
    from .settings import available_cpu_threads, requested_asr_threads

    requested = requested_asr_threads()
    available = available_cpu_threads()
    return available if requested == "auto" else min(requested, available)


def _silero_vad() -> Any:
    """Lazy silero-vad (weights ship in the pip package: no network, tokens or gated models)."""
    global _SILERO_VAD
    if _SILERO_VAD is None:
        from silero_vad import load_silero_vad

        with _quiet_third_party():  # torch.jit.load inside prints a FutureWarning
            _SILERO_VAD = load_silero_vad()
    return _SILERO_VAD


def _speech_timestamps(audio: Any, *, max_speech_duration_s: float = float("inf")) -> list[dict]:
    """Speech search under the caller's _GIGAAM_LOCK: the shared VAD keeps state across windows."""
    with _torch_threads(1):
        from silero_vad import get_speech_timestamps

        return get_speech_timestamps(
            audio, _silero_vad(), sampling_rate=_GIGAAM_SR,
            max_speech_duration_s=max_speech_duration_s,
        )  # fmt: skip


_GIGAAM_MAX_GAP_S = 0.6  # a longer pause starts a new reply


def _group_vad_windows(
    speech: list[dict], max_window: int, max_gap: int | None = None
) -> list[tuple[int, int]]:
    """Join VAD chunks (samples, each ≤ max_window) into inference windows ≤ max_window.

    Neighbours join only across a pause ≤ max_gap (one breath), so each window is one reply
    with its own start and end. Joining across pauses gave a window one start time, and the
    other track's reply in the middle landed after the whole window.
    """
    gap = max_gap if max_gap is not None else int(_GIGAAM_MAX_GAP_S * _GIGAAM_SR)
    windows: list[tuple[int, int]] = []
    cur: tuple[int, int] | None = None
    for seg in speech:
        s, e = int(seg["start"]), int(seg["end"])
        if cur is not None and e - cur[0] <= max_window and s - cur[1] <= gap:
            cur = (cur[0], e)
        else:
            if cur is not None:
                windows.append(cur)
            cur = (s, e)
    if cur is not None:
        windows.append(cur)
    return windows


def _read_wav_tensor(wav: Path) -> Any:
    """PCM16 mono WAV (from our extract_audio) → torch.FloatTensor in [-1..1].

    Our own reader instead of silero read_audio / torchaudio.load: torchaudio ≥ 2.9 delegates to
    torchcodec, which fails to load without a matching ffmpeg. extract_audio guarantees the
    format, so no third-party decoder is needed.
    """
    import wave

    import numpy as np
    import torch

    with wave.open(str(wav)) as w:
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32)
    audio *= 1 / 32768  # in place: a copy costs 4 more bytes per sample (1 GB for 4.7 h)
    return torch.from_numpy(audio)


def _gigaam_infer(model: Any, chunk: Any) -> str:
    """Direct tensor inference: windows are slices of the loaded recording, no file per window."""
    import torch

    chunk = chunk.to(model._device).unsqueeze(0).to(model._dtype)
    length = torch.tensor([chunk.shape[-1]], device=model._device)
    with torch.inference_mode():
        encoded, encoded_len = model.forward(chunk, length)
        text = model.decoding.decode(model.head, encoded, encoded_len)[0][0]  # (text, ids, frames)
    return text.strip()


class _Windowed:
    """A windowed engine (GigaAM, Parakeet): silero-vad cuts the recording into windows that are
    recognized independently. One lock for both engines and the shared stateful VAD."""

    name = ""
    title = ""
    window_s = 0.0
    max_gap_s: float | None = None  # max pause when joining VAD chunks; None — any

    def load(self, on: Progress) -> Any:
        raise NotImplementedError

    def infer(self, model: Any, chunk: Any, lang: str | None) -> list[Segment]:
        """A window → replies timed from the window start."""
        raise NotImplementedError


class _GigaAmWindows(_Windowed):
    name, title, window_s, max_gap_s = "gigaam", "GigaAM", _GIGAAM_WINDOW_S, _GIGAAM_MAX_GAP_S

    def load(self, on: Progress) -> Any:
        return _gigaam_model()

    def infer(self, model: Any, chunk: Any, lang: str | None) -> list[Segment]:
        text = _gigaam_infer(model, chunk)
        return [Segment(start=0.0, end=len(chunk) / _GIGAAM_SR, text=text)] if text else []


class _ParakeetWindows(_Windowed):
    # ~15 s windows with no pause limit: on a short chunk the multilingual model mixes up languages
    name, title, window_s, max_gap_s = "parakeet", "Parakeet", parakeet.WINDOW_S, None

    def load(self, on: Progress) -> Any:
        return parakeet.model(on)

    def infer(self, model: Any, chunk: Any, lang: str | None) -> list[Segment]:
        return model.transcribe(chunk, lang)


WINDOWED: dict[str, _Windowed] = {e.name: e for e in (_GigaAmWindows(), _ParakeetWindows())}


def _group_window_speech(engine: _Windowed, speech: list[dict]) -> list[tuple[int, int]]:
    window = int(engine.window_s * _GIGAAM_SR)
    gap = window if engine.max_gap_s is None else int(engine.max_gap_s * _GIGAAM_SR)
    return _group_vad_windows(speech, window, gap)


def _infer_windows(
    engine: _Windowed,
    model: Any,
    audio: Any,
    windows: list[tuple[int, int]],
    *,
    base: int,
    lang: str | None,
    on: Progress,
) -> list[Segment]:
    """Windows (samples from base) → replies with absolute times."""
    segments: list[Segment] = []
    for i, (s, e) in enumerate(windows, 1):
        offset = (base + s) / _GIGAAM_SR
        segments += [
            x.model_copy(update={"start": x.start + offset, "end": x.end + offset})
            for x in engine.infer(model, audio[s:e], lang)
        ]
        on(i / len(windows), _("Recognizing…"))
    return segments


def _windowed_longform(
    engine: _Windowed, model: Any, wav: Path, lang: str | None, on: Progress
) -> list[Segment]:
    """A long recording: silero-vad → windows → direct tensor inference.

    Instead of gigaam.transcribe_longform, which needs pyannote VAD (a gated model, HF_TOKEN);
    silero-vad works offline and gives real per-window progress.
    """
    audio = _read_wav_tensor(wav)
    on(None, _("Looking for speech…"))
    speech = _speech_timestamps(audio, max_speech_duration_s=engine.window_s)
    windows = _group_window_speech(engine, speech)
    return _infer_windows(engine, model, audio, windows, base=0, lang=lang, on=on)


class GigaAmBackend(Engine):
    id = name = "gigaam"
    title, extra, langs, names_lang = "GigaAM", "gigaam", frozenset({"ru"}), False
    model = _GIGAAM_MODEL_NAME  # a single model: --model does not apply to GigaAM

    def available(self) -> bool:
        import importlib.util

        return importlib.util.find_spec("gigaam") is not None

    def run(
        self,
        src: Path,
        *,
        lang: str | None = None,
        total_duration: float = 0.0,
        on: Progress = noop,
    ) -> Transcript:
        if lang not in (None, "ru"):
            raise MediaError(_("GigaAM recognizes only Russian ({lang} requested).", lang=lang))
        with tempfile.TemporaryDirectory() as tmp:
            # 0.3 s of leading silence protects the first word, as on the whisper paths
            wav = extract_audio(src, Path(tmp) / "audio.wav", pad_start_ms=300, on=on)
            # Short vs long form by the ACTUAL extracted WAV length (PCM16 mono 16 kHz →
            # bytes / 32000), not the source's probed duration: probe plus padding pushed a
            # borderline 24 s voice message past the package limit ("Too long wav file").
            wav_secs = wav.stat().st_size / 32000.0
            if total_duration <= 0:  # input without a probe (a bare wav)
                total_duration = wav_secs
            on(None, _("Loading the recognition model…"))
            with _GIGAAM_LOCK, _torch_threads(_asr_cpu_threads()):
                model = _gigaam_model()
                if wav_secs > _GIGAAM_SHORT_MAX_S:
                    segments = _windowed_longform(WINDOWED["gigaam"], model, wav, "ru", on)
                else:
                    on(None, _("Recognizing…"))
                    text = _gigaam_infer(model, _read_wav_tensor(wav))
                    segments = [Segment(start=0.0, end=total_duration, text=text)] if text else []
        text = " ".join(s.text for s in segments).strip()
        return Transcript(text=text, segments=segments, language="ru")


class ParakeetBackend(Engine):
    """Parakeet Ultra, the default for Russian: writes Latin terms (API, MCP) better than
    GigaAM. 25 European languages."""

    id = name = "parakeet"
    title, extra, langs = "Parakeet", "parakeet", parakeet.LANGS
    model = parakeet.REPO

    def available(self) -> bool:
        return parakeet.available()

    def run(
        self,
        src: Path,
        *,
        lang: str | None = None,
        total_duration: float = 0.0,
        on: Progress = noop,
    ) -> Transcript:
        if lang is not None and lang not in parakeet.LANGS:
            raise MediaError(
                _("Parakeet does not support the language {lang} — choose Whisper.", lang=lang)
            )
        engine = WINDOWED["parakeet"]
        with tempfile.TemporaryDirectory() as tmp:
            wav = extract_audio(src, Path(tmp) / "audio.wav", pad_start_ms=300, on=on)
            with _GIGAAM_LOCK, _torch_threads(_asr_cpu_threads()):
                model = engine.load(on)
                if wav.stat().st_size / 32000.0 > parakeet.WINDOW_S:
                    segments = _windowed_longform(engine, model, wav, lang, on)
                else:
                    on(None, _("Recognizing…"))
                    segments = engine.infer(model, _read_wav_tensor(wav), lang)
        if lang is None:  # no language given: guess it from the recognized text
            lang = parakeet.guess_lang(" ".join(s.text for s in segments))
            segments = parakeet.tidy(segments, lang)
        text = " ".join(s.text for s in segments).strip()
        return Transcript(text=text, segments=segments, language=lang)


# whisperX pipelines built in this job process (tracks of one recording), under their own lock.
_WHISPERX_CACHE_LOCK = threading.RLock()
_WHISPERX_ASR: dict[tuple[str, str, str, int], Any] = {}

# ONE large-v3 (CT2, fp16) per process for batch whisperX. Live does not share it: mixing the
# torch and ct2 CUDA allocators in one session caused "out of memory" with gigabytes free and
# poisoned the process's CUDA context. A copy of the weights is cheaper than that.
_SHARED_WHISPER_LOCK = threading.Lock()
_SHARED_WHISPER: Any = None
_SHARED_WHISPER_THREADS: int | None = None

# The torch caching allocator's share of VRAM (live engine + batch VAD). torch caches what it
# takes, so without a cap it eats the memory ctranslate2 allocates from directly. 0.6 of 22 GB
# ≈ 13 GB fits live large-v3 (0.5 was too tight) and leaves ~9 GB for the ct2 batch.
_TORCH_VRAM_FRACTION = 0.6
_torch_capped = False


def cap_torch_vram(fraction: float = _TORCH_VRAM_FRACTION) -> None:
    """Cap the torch caching allocator at a share of VRAM (idempotent, before heavy allocations).

    torch and ctranslate2 do not see each other; the cap keeps memory for the ct2 batch. Hitting
    it gives a recoverable torch OOM in one live session instead of a poisoned CUDA context.
    Each live session is its own process with a share of the budget (live/worker.py)."""
    global _torch_capped
    if _torch_capped:
        return
    _torch_capped = True
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.set_per_process_memory_fraction(fraction)
            logger.info("torch: VRAM cap %.0f%% of the device", fraction * 100)
    except Exception:  # noqa: BLE001 - no torch/CUDA (Mac, thin client): nothing to cap
        pass


def shared_whisper_model() -> Any:
    """One large-v3 (fp16, CUDA) with the current request's CPU budget."""
    global _SHARED_WHISPER, _SHARED_WHISPER_THREADS
    threads = _asr_cpu_threads()
    with _SHARED_WHISPER_LOCK:
        if _SHARED_WHISPER is None or _SHARED_WHISPER_THREADS != threads:
            import torch
            from whisperx.asr import WhisperModel  # ty: ignore[unresolved-import]

            torch.set_num_threads(threads)
            logger.info(
                "shared whisper: loading %s (fp16); %s", DEFAULT_WHISPERX_MODEL, vram_status()
            )
            with _CUDA_LOAD_LOCK:  # ct2 init does not race torch loads
                _SHARED_WHISPER = WhisperModel(
                    DEFAULT_WHISPERX_MODEL,
                    device="cuda",
                    compute_type="float16",
                    cpu_threads=threads,
                )
                _SHARED_WHISPER_THREADS = threads
            logger.info("shared whisper: ready; %s", vram_status())
        return _SHARED_WHISPER


def _whisperx_model(whisperx: Any, model: str, device: str, compute_type: str) -> Any:
    """A cached whisperX pipeline; ASR weights are the process-wide shared_whisper_model.

    load_model(model=<instance>) skips loading its own weights, so VRAM holds one large-v3. A
    non-default model loads its own weights (the shared instance is fixed to large-v3).
    """
    threads = _asr_cpu_threads()
    key = (model, device, compute_type, threads)
    with _WHISPERX_CACHE_LOCK:
        cached = _WHISPERX_ASR.get(key)
        if cached is not None:
            return cached
        # CT2 sizes its CPU pool at load time: a new budget means a new engine, and old ones are
        # dropped from the cache (a running request keeps its own reference).
        for previous in [k for k in _WHISPERX_ASR if k[:3] == key[:3]]:
            del _WHISPERX_ASR[previous]
        logger.info(
            "batch whisperx: building pipeline %s (%s); %s", model, compute_type, vram_status()
        )
        # the shared CUDA instance is a server optimization; on CPU each pipeline loads its own
        # weights (a CPU client runs one call at a time)
        preloaded = (
            shared_whisper_model() if model == DEFAULT_WHISPERX_MODEL and device == "cuda" else None
        )
        # threads matters for a non-default model (load_model builds its own WhisperModel); the
        # preloaded default already has cpu_threads.
        with _CUDA_LOAD_LOCK:  # the pipeline's VAD model is torch on CUDA
            if device == "cuda":
                cap_torch_vram()  # before the VAD loads
            cached = whisperx.load_model(
                model, device, compute_type=compute_type, model=preloaded, threads=threads
            )
        logger.info("batch whisperx: pipeline %s ready; %s", model, vram_status())
        _WHISPERX_ASR[key] = cached
        return cached


def _torch_device() -> str:
    """cuda when CUDA works, else cpu: the default whisperX device."""
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:  # noqa: BLE001 - no torch: run() is never reached (available=False)
        return "cpu"


class WhisperXBackend:
    name = "whisperx"

    def __init__(self, model: str = DEFAULT_WHISPERX_MODEL, device: str | None = None):
        self.model = model
        # None: CUDA if present, else CPU (ct2 int8, an order of magnitude slower but working on
        # a Linux laptop without NVIDIA)
        self._device = device

    @property
    def device(self) -> str:
        if self._device is None:
            self._device = _torch_device()
        return self._device

    def available(self) -> bool:
        import importlib.util

        # the package, NOT CUDA: whisperx works on CPU (int8), just slower
        return importlib.util.find_spec("whisperx") is not None

    def run(
        self,
        src: Path,
        *,
        lang: str | None = None,
        total_duration: float = 0.0,
        on: Progress = noop,
    ) -> Transcript:
        import whisperx  # ty: ignore[unresolved-import]

        # ctranslate2 has no fp16 on CPU; int8 is the fastest CPU option without losing
        # recognition quality (weights are quantized, not activations)
        compute_type = "float16" if self.device == "cuda" else "int8"
        # pyannote is noisy with warnings.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            # ffmpeg decoding is long and reports no fraction, so name the phase.
            on(None, _("Extracting audio…"))
            audio = whisperx.load_audio(str(src))
            # 0.3 s of leading silence: voice messages start with speech, and VAD swallowed the
            # first fraction of a second. Segment times shift by 0.3 s, which is negligible.
            # Best effort: audio of an unusual type (not an ndarray) is left unpadded.
            try:
                import numpy as _np

                audio = _np.concatenate([_np.zeros(int(0.3 * 16000), dtype=audio.dtype), audio])
            except Exception:  # noqa: BLE001
                pass
            on(None, _("Loading the recognition model…"))
            model = _whisperx_model(whisperx, self.model, self.device, compute_type)
            result = model.transcribe(
                audio,
                language=lang,
                progress_callback=lambda pct: on(pct / 100, _("Recognizing…")),
            )
            language = result.get("language", lang)

        segments: list[Segment] = []
        for s in result["segments"]:
            segments.append(
                Segment(
                    start=float(s["start"]),
                    end=float(s["end"]),
                    text=str(s["text"]).strip(),
                )
            )
        text = " ".join(s.text for s in segments)
        return Transcript(text=text, segments=segments, language=language)


class WhisperBackend(Engine):
    """This platform's whisper, any language: mlx-whisper on Apple Silicon, else whisperX. The
    variant runs and is recorded in history (mlx, whisperx)."""

    id = name = "whisper"
    title, extra = "Whisper", "whisper"
    model = DEFAULT_LOCAL_MODEL

    @staticmethod
    def variants(model: str | None = None) -> list[TranscriptionBackend]:
        return [_variant(name, model) for name in _VARIANTS]

    @classmethod
    def create(cls, model: str | None = None) -> TranscriptionBackend:
        for variant in cls.variants(model):
            if variant.available():
                return variant
        raise MediaError(_no_engine())

    def available(self) -> bool:
        return any(variant.available() for variant in self.variants())

    def run(
        self,
        src: Path,
        *,
        lang: str | None = None,
        total_duration: float = 0.0,
        on: Progress = noop,
    ) -> Transcript:
        return self.create().run(src, lang=lang, total_duration=total_duration, on=on)


# The registry, in the order automatic choice tries them.
ENGINES: tuple[type[Engine], ...] = (ParakeetBackend, GigaAmBackend, WhisperBackend)
# Whisper variants, also chosen by name (CLI --backend mlx | whisperx)
_VARIANTS: dict[str, type[MlxBackend] | type[WhisperXBackend]] = {
    "mlx": MlxBackend,
    "whisperx": WhisperXBackend,
}


def _variant(name: str, model: str | None) -> TranscriptionBackend:
    variant = _VARIANTS[name]
    return variant(model) if model else variant()


def _no_engine() -> str:
    return _(
        "No transcription engine is installed: install the full extra (./install.sh client "
        "or ./install.sh server) or set a runner (--runner/MNEMA_RUNNER)."
    )


def engine(choice: str) -> type[Engine] | None:
    """The registered engine with this id, or None (auto, a variant, cloud, unknown)."""
    return next((e for e in ENGINES if e.id == choice), None)


def installed_engines() -> list[type[Engine]]:
    """Registered engines this machine can run (packages only, no models)."""
    return [e for e in ENGINES if e().available()]


def backend_choices() -> list[str]:
    """CLI --backend: automatic, every registered engine and the whisper variants."""
    return [AUTO, *(e.id for e in ENGINES), *_VARIANTS]


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

    An engine named explicitly is returned as is (the caller checks available()), except
    whisper, which needs an installed variant. AUTO: the first installed engine of ENGINES that
    takes the language (Engine.automatic_for): no language or a Parakeet language → Parakeet;
    Russian without it → GigaAM; otherwise Whisper. --model applies to whisper models only.
    """
    name = str(name)
    if name in _VARIANTS:
        return _variant(name, model)
    found = engine(name)
    if found is not None:
        return found.create(model)
    if name != AUTO:
        allowed = ", ".join(backend_choices())
        raise MediaError(_("Unknown backend: {name} ({allowed})", name=name, allowed=allowed))
    for candidate in ENGINES:
        if candidate.automatic_for(lang) and candidate().available():
            return candidate.create(model)
    raise MediaError(_no_engine())
