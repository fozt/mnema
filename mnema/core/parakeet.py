"""Parakeet Ultra on ONNX Runtime: a track → words with times.

moondream/parakeet-ultra is a fine-tuned nvidia/parakeet-tdt-0.6b-v3 (same architecture and
tokenizer, 25 languages). The graphs are a public ONNX export pinned by revision and sha256:
the encoder takes our numpy log-mel features, the greedy TDT decoder steps through
decoder_joint. Long tracks go in ~15 s windows of speech (silero, core/vad.py).

The module imports without numpy and onnxruntime: heavy work happens inside functions.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import logging
import re
import shutil
import threading
from pathlib import Path
from typing import Any, NamedTuple

from ..i18n import _
from . import vad
from .asr_engine import Word, group_speech, read_wav
from .base import MediaError, Progress, cache_home, noop
from .models import Segment

logger = logging.getLogger(__name__)

MODEL = "parakeet-ultra"


class OnnxFile(NamedTuple):
    repo: str
    revision: str
    sha256: str


# Public exports of Ultra (CC-BY-4.0), pinned by revision and sha256; the code does not depend on
# the file names. Encoder int8 (MatMul only) is not worse than fp32 on FLEURS and Golos; the int8
# decoder was, and it is only 72 MB in fp32.
_MARCOS = ("marcosremar2/parakeet-ultra-onnx", "b4508be0373bcd39598369c1473454f826b71815")
_MASTERX = ("Masterx/parakeet-tdt-0.6b-ultra-onnx", "99b09f030a5a6efeaa13cf2cf54592100ce2c3f1")
ENCODER = "encoder-model.int8.onnx"
DECODER = "decoder_joint-model.onnx"
VOCAB = "vocab.txt"
ONNX_FILES = {
    ENCODER: OnnxFile(*_MARCOS, "291fa5678048c8ee3007a5995fa9e817213fdb2e3d60ea8fed549dbeb154b33b"),
    DECODER: OnnxFile(
        *_MASTERX, "87cdaae0a1102f6be1cb2bb45d6a7d4d914b23b764d5fba704a82960f988f958"
    ),
    VOCAB: OnnxFile(*_MARCOS, "d58544679ea4bc6ac563d1f545eb7d474bd6cfa467f0a6e2c1dc1c7d37e3c35d"),
}
SR = 16000
FRAME_S = 0.08  # encoder frame: 10 ms feature hop × subsampling 8
# Recognition window (as in FluidAudio): on a short chunk the model mistakes the language.
WINDOW_S = 15.0
# the model's languages (parakeet-tdt-0.6b-v3 model card)
LANGS = frozenset(
    "bg hr cs da nl en et fi fr de el hu it lv lt mt pl pt ro sk sl es sv ru uk".split()
)

_N_FFT, _HOP, _WIN, _MELS, _PREEMPH = 512, 160, 400, 128, 0.97
_LOG_GUARD, _EPS = 2.0**-24, 1e-5
_DURATIONS = (0, 1, 2, 3, 4)  # TDT durations in frames: the last logits of decoder_joint
_MAX_SYMBOLS = 10  # tokens on one frame before the decoder is forced forward
_STATE = (2, 1, 640)  # LSTM state of decoder_joint

_UK = set("іїєґІЇЄҐ")  # Ukrainian letters: only Ukrainian has many of them
_FILLER_YES = {"mm-hmm", "mhm", "uh-huh", "mm", "hmm", "mm-mm"}  # → "Угу."
_FILLER_DROP = {"uh", "um", "ah", "er", "erm"}


class Emitted(NamedTuple):
    """A greedy TDT token: id, encoder frame, predicted duration in frames (0: the next token
    is on the same frame)."""

    token: int
    frame: int
    duration: int


# ─────────────────────────────────────────── features (log-mel, as ParakeetFeatureExtractor)


def mel_filters(sr: int = SR, n_fft: int = _N_FFT, n_mels: int = _MELS) -> Any:
    """Slaney mel filters (as librosa.filters.mel(norm="slaney")): [n_mels, n_fft//2+1]."""
    import numpy as np

    f_sp, min_log_hz = 200.0 / 3, 1000.0
    min_log_mel, logstep = min_log_hz / f_sp, np.log(6.4) / 27.0

    def hz_to_mel(f: Any) -> Any:
        f = np.asarray(f, dtype=np.float64)
        return np.where(
            f >= min_log_hz,
            min_log_mel + np.log(np.maximum(f, 1e-10) / min_log_hz) / logstep,
            f / f_sp,
        )

    def mel_to_hz(m: Any) -> Any:
        m = np.asarray(m, dtype=np.float64)
        return np.where(
            m >= min_log_mel, min_log_hz * np.exp(logstep * (m - min_log_mel)), f_sp * m
        )

    fft_freqs = np.linspace(0, sr / 2, 1 + n_fft // 2)
    mel_f = mel_to_hz(np.linspace(hz_to_mel(0.0), hz_to_mel(sr / 2), n_mels + 2))
    fdiff = np.diff(mel_f)
    ramps = mel_f[:, None] - fft_freqs[None, :]
    lower = -ramps[:-2] / fdiff[:-1, None]
    upper = ramps[2:] / fdiff[1:, None]
    weights = np.maximum(0, np.minimum(lower, upper))
    weights *= (2.0 / (mel_f[2 : n_mels + 2] - mel_f[:n_mels]))[:, None]
    return weights.astype(np.float32)


_MEL: Any = None


def features(audio: Any) -> tuple[Any, int]:
    """Audio [n] float32 16 kHz → (features [1, 128, T] float32, speech frames): the torch
    STFT of ParakeetFeatureExtractor in numpy (centered, symmetric hann(400) inside 512),
    every band normalized over the speech frames, the frames after them zero."""
    import numpy as np

    global _MEL
    if _MEL is None:
        _MEL = mel_filters()
    n = len(audio)
    x = np.asarray(audio, np.float32)
    x = np.concatenate([x[:1], x[1:] - _PREEMPH * x[:-1]])
    x = np.pad(x, _N_FFT // 2)
    window = np.zeros(_N_FFT, np.float32)
    left = (_N_FFT - _WIN) // 2
    window[left : left + _WIN] = np.hanning(_WIN)
    frames = 1 + (len(x) - _N_FFT) // _HOP
    index = np.arange(_N_FFT)[None, :] + _HOP * np.arange(frames)[:, None]
    spec = np.fft.rfft(x[index] * window, axis=1)
    power = (spec.real**2 + spec.imag**2).astype(np.float32)
    mel = np.log(power @ _MEL.T + _LOG_GUARD).T  # [128, T]
    length = max(1, n // _HOP)
    valid = mel[:, :length]
    mean = valid.mean(1, keepdims=True)
    std = np.sqrt(((valid - mean) ** 2).sum(1, keepdims=True) / max(length - 1, 1))
    mel = (mel - mean) / (std + _EPS)
    mel[:, length:] = 0
    return mel[None].astype(np.float32), length


# ─────────────────────────────────────────── greedy TDT


def greedy_tdt(
    frames: int,
    *,
    joint: Any,
    step: Any,
    blank: int,
    vocab: int,
    durations: list[int],
    max_symbols: int = 10,
) -> list[Emitted]:
    """Greedy TDT pass (as transformers 5 generate): at frame t, token and duration by argmax.
    A blank advances by the duration (0 → 1); a token goes to the decoder and advances by the
    duration (0 stays, at most max_symbols tokens in a row). joint(t) → logits
    [vocab + len(durations)]; step(token) updates the decoder state."""
    out: list[Emitted] = []
    t = symbols = 0
    while t < frames:
        logits = joint(t)
        token = int(logits[:vocab].argmax())
        d = durations[int(logits[vocab:].argmax())]
        if token == blank:
            t += max(d, 1)
            symbols = 0
            continue
        out.append(Emitted(token=token, frame=t, duration=d))
        step(token)
        symbols += 1
        if d > 0 or symbols >= max_symbols:
            t += max(d, 1)
            symbols = 0
    return out


# ─────────────────────────────────────────── cleanup


def tidy_ru(text: str) -> str:
    """A Russian-call reply made only of English fillers: "Mm-hmm" → "Угу.", "Uh" → ""."""
    words = [w.lower() for w in re.findall(r"[A-Za-z-]+", text)]
    if not words or re.search(r"[^A-Za-z\-\s.,!?…]", text):
        return text
    if all(w in _FILLER_YES | _FILLER_DROP for w in words):
        return "Угу." if any(w in _FILLER_YES for w in words) else ""
    return text


_CYRILLIC = re.compile(r"[а-яёіїєґ]", re.IGNORECASE)
_LATIN = re.compile(r"[a-z]", re.IGNORECASE)
# common English function words: Latin text without them is another language
_EN_COMMON = frozenset(
    "the a an and or but of to in on at for with is are was were be been it this that you i we "
    "they he she not do does did have has had will would can could so if as what yes no".split()
)


def guess_lang(text: str) -> str | None:
    """The recognized text's language by script: Cyrillic → ru (many Ukrainian letters → uk),
    Latin with English function words → en, otherwise None."""
    cyrillic, latin = len(_CYRILLIC.findall(text)), len(_LATIN.findall(text))
    if not cyrillic and not latin:
        return None
    if cyrillic >= latin:
        words = [w for w in re.findall(r"\w+", text.lower()) if _CYRILLIC.search(w)]
        uk = sum(1 for w in words if _UK & set(w))
        return "uk" if uk > len(words) * 0.15 else "ru"
    words = re.findall(r"[a-z']+", text.lower())
    common = sum(1 for w in words if w in _EN_COMMON)
    return "en" if words and common >= len(words) * 0.15 else None


def tidy(segments: list[Segment], lang: str | None) -> list[Segment]:
    """Clean the output by the call's language (given or guessed): for Russian, English fillers
    become "Угу." and empty replies are dropped."""
    if lang != "ru":
        return segments
    out = []
    for s in segments:
        text = tidy_ru(s.text)
        if text:
            out.append(s if text == s.text else s.model_copy(update={"text": text}))
    return out


# ─────────────────────────────────────────── model files


def available() -> bool:
    """Whether everything Parakeet needs is installed (without importing modules or weights)."""
    import importlib.util

    return all(
        importlib.util.find_spec(m) is not None for m in ("onnxruntime", "numpy", "huggingface_hub")
    )


DOWNLOAD_FAILED = "Could not download the recognition model. Check the connection and try again"
NO_SPACE = "Not enough disk space for the recognition model"
DOWNLOADING = "Downloading the recognition model, only the first time…"


def model_dir(on: Progress = noop) -> Path:
    """The folder with ONNX_FILES: MNEMA_PARAKEET_ONNX_DIR as is, otherwise the pinned revision
    in the cache (downloaded once, ~910 MB). A file gets there only after its sha256 matched:
    the download lands next to it first, so one cut short or never checked is not taken."""
    from .settings import RuntimeSettings

    given = RuntimeSettings().parakeet_onnx_dir
    if given is not None:
        folder = given.expanduser()
        for name in ONNX_FILES:
            if not (folder / name).is_file():
                raise MediaError(_("No {name} in {folder}", name=name, folder=folder))
        return folder
    folder = _cached()
    if all((folder / name).is_file() for name in ONNX_FILES):
        return folder
    on(None, _(DOWNLOADING))
    try:
        _fetch(folder)
    except OSError as e:  # the cache: no space, read-only, another owner after a reinstall
        logger.warning("could not place the recognition model in %s: %s", folder, e)
        raise MediaError(_(NO_SPACE) if e.errno == errno.ENOSPC else _(DOWNLOAD_FAILED)) from e
    return folder


def _fetch(folder: Path) -> None:
    staging = folder.with_name(f".{folder.name}.partial")
    staging.mkdir(parents=True, exist_ok=True)
    with (staging.parent / f".{folder.name}.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # one download at a time: jobs of the server, the Mac
        for name in ONNX_FILES:
            if (folder / name).is_file():  # checked earlier, maybe by another job
                continue
            path = _download(name, staging)
            if _sha256(path) != ONNX_FILES[name].sha256:
                path.unlink(missing_ok=True)
                raise MediaError(_("The recognition model did not download correctly. Try again"))
            folder.mkdir(parents=True, exist_ok=True)
            path.replace(folder / name)
        shutil.rmtree(staging, ignore_errors=True)


def _cached() -> Path:
    """The cache folder of exactly these pins: another file or revision is another folder."""
    pins = "".join(f"{name} {f.repo} {f.revision} {f.sha256}\n" for name, f in ONNX_FILES.items())
    return cache_home() / "mnema" / MODEL / hashlib.sha256(pins.encode()).hexdigest()[:16]


def downloaded() -> bool:
    """Whether model_dir() goes without a download: a given folder (model_dir checks it) or
    the pinned revision already in the cache."""
    from .settings import RuntimeSettings

    if RuntimeSettings().parakeet_onnx_dir is not None:
        return True
    return all((_cached() / name).is_file() for name in ONNX_FILES)


def _download(name: str, staging: Path) -> Path:
    """One file of the pinned revision into staging (huggingface_hub: proxy, token, resuming);
    the network, HTTP and offline mode refuse in the user's words."""
    try:
        import httpx
        import huggingface_hub
        from huggingface_hub.errors import EntryNotFoundError
    except ImportError as e:
        logger.warning("huggingface_hub is missing")
        raise MediaError(_("Mnema is not fully installed. Reinstall it")) from e
    try:
        return Path(
            huggingface_hub.hf_hub_download(
                ONNX_FILES[name].repo, name, revision=ONNX_FILES[name].revision, local_dir=staging
            )
        )
    except (OSError, EntryNotFoundError, httpx.HTTPError) as e:
        logger.warning("could not download %s of %s: %s", name, ONNX_FILES[name].repo, e)
        no_space = isinstance(e, OSError) and e.errno == errno.ENOSPC
        raise MediaError(_(NO_SPACE) if no_space else _(DOWNLOAD_FAILED)) from e


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


# ─────────────────────────────────────────── model


def _options(ort: Any, threads: int) -> Any:
    options = ort.SessionOptions()
    options.intra_op_num_threads = threads
    options.inter_op_num_threads = 1
    options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    return options


class OnnxModel:
    """The two Ultra graphs and the vocabulary: an audio window → timed words."""

    def __init__(self, folder: Path, threads: int) -> None:
        import onnxruntime as ort

        cpu = ["CPUExecutionProvider"]
        self.encoder: Any = ort.InferenceSession(
            str(folder / ENCODER), _options(ort, threads), providers=cpu
        )
        self.decoder: Any = ort.InferenceSession(
            str(folder / DECODER), _options(ort, 1), providers=cpu
        )
        pieces: dict[int, str] = {}
        for line in (folder / VOCAB).read_text(encoding="utf-8").splitlines():
            piece, _sep, index = line.rpartition(" ")
            pieces[int(index)] = piece
        self.pieces = [pieces[i] for i in range(len(pieces))]
        self.blank = self.pieces.index("<blk>")

    def words(self, audio: Any, offset: float = 0.0) -> list[Word]:
        """A window (≤ ~15 s) → its words, timed from offset (seconds)."""
        import numpy as np

        feats, length = features(audio)
        enc, enc_len = self.encoder.run(
            ["outputs", "encoded_lengths"],
            {"audio_signal": feats, "length": np.array([length], np.int64)},
        )
        frames = min(enc.shape[2], int(enc_len[0]))
        emitted = self._decode(enc[0, :, :frames])
        times = token_times([(self.pieces[e.token], e.frame, e.duration) for e in emitted])
        return timed_words(times, offset)

    def _decode(self, enc: Any) -> list[Emitted]:
        """Greedy TDT on decoder_joint: it starts from blank and zero states, and its new
        states are taken only after an emitted token."""
        import numpy as np

        state = [np.zeros(_STATE, np.float32), np.zeros(_STATE, np.float32)]
        target = [self.blank]
        pending: list[Any] = [None]

        def joint(t: int) -> Any:
            out, s1, s2 = self.decoder.run(
                ["outputs", "output_states_1", "output_states_2"],
                {
                    "encoder_outputs": np.ascontiguousarray(enc[None, :, t : t + 1]),
                    "targets": np.array([[target[0]]], np.int32),
                    "target_length": np.array([1], np.int32),
                    "input_states_1": state[0],
                    "input_states_2": state[1],
                },
            )
            pending[0] = (s1, s2)
            return out.reshape(-1)

        def step(token: int) -> None:
            state[0], state[1] = pending[0]
            target[0] = token

        return greedy_tdt(
            enc.shape[1],
            joint=joint,
            step=step,
            blank=self.blank,
            vocab=len(self.pieces),
            durations=list(_DURATIONS),
            max_symbols=_MAX_SYMBOLS,
        )


# One word-time rule with the Mac (mnema-asr, FluidAudio with TDT_EMISSION_DELAY_FRAMES=0),
# pinned by tests/fixtures/word-timings-v1.json on both sides.


def token_times(tokens: list[tuple[str, int, int]]) -> list[tuple[str, float, float]]:
    """Greedy TDT tokens (piece, frame, duration in frames) → (piece, start, end) in seconds, as
    FluidAudio times them: <unk> emissions are dropped first; a token ends after its duration,
    one of zero duration at the next token's start (at least a frame later)."""
    kept = sorted((t for t in tokens if t[0] != "<unk>"), key=lambda t: t[1])
    times: list[tuple[str, float, float]] = []
    for i, (piece, frame, duration) in enumerate(kept):
        start = frame * FRAME_S
        if duration > 0:
            end = start + duration * FRAME_S
        elif i + 1 < len(kept):
            end = max(kept[i + 1][1] * FRAME_S, start + FRAME_S)
        else:
            end = start + FRAME_S
        times.append((piece, start, end))
    return times


def timed_words(times: list[tuple[str, float, float]], offset: float = 0.0) -> list[Word]:
    """Timed SentencePiece pieces → words, from offset (seconds): a word starts at "▁" (or a
    leading space) and spans its pieces; service tokens (<unk>, <|…|>) are not words."""
    words: list[Word] = []
    text, start, end = "", 0.0, 0.0

    def flush() -> None:
        if word := text.strip(" \t"):
            words.append(Word(text=word, start=offset + start, end=offset + end))

    for piece, s, e in times:
        if piece.startswith("<") and piece.endswith(">"):
            continue
        token = piece.replace("▁", " ")
        if not token:
            continue
        new = token.startswith(" ") or not text
        if new and text:
            flush()
            text = ""
        if new:
            text, start = token.removeprefix(" "), s
        else:
            text += token
        end = e
    flush()
    return words


_LOCK = threading.Lock()
_MODEL: OnnxModel | None = None


def model(on: Progress = noop) -> OnnxModel:
    """One model per process (lazy, under a lock); the memory goes away with the job process
    (core/worker.py). The encoder gets this job's CPU budget."""
    global _MODEL
    with _LOCK:
        if _MODEL is None:
            folder = model_dir(on)
            on(None, _("Loading the recognition model…"))
            from .backends import _asr_cpu_threads

            _MODEL = OnnxModel(folder, _asr_cpu_threads())
        return _MODEL


class OnnxParakeet:
    """The Recognizer of the server and Linux: Parakeet Ultra on ONNX Runtime."""

    id: str = "parakeet"
    model: str = MODEL
    langs: frozenset[str] = LANGS

    def available(self) -> bool:
        return available()

    def words(self, wav: Path, *, on: Progress = noop) -> list[Word]:
        """Up to 15 s: one window; longer: windows of speech joined across any pause up to
        15 s (on a short chunk the model mistakes the language)."""
        recognizer = model(on)
        audio = read_wav(wav)
        window = int(WINDOW_S * SR)
        if len(audio) <= window:
            spans = [(0, len(audio))] if len(audio) else []
        else:
            on(None, _("Looking for speech…"))
            spans = group_speech(self._speech(audio, WINDOW_S), window, window)
        on(None, _("Recognizing…"))
        words: list[Word] = []
        for i, (s, e) in enumerate(spans, 1):
            words += recognizer.words(audio[s:e], offset=s / SR)
            on(i / len(spans), _("Recognizing…"))
        return words

    def speech(self, wav: Path, *, max_s: float) -> list[tuple[int, int]]:
        return self._speech(read_wav(wav), max_s)

    @staticmethod
    def _speech(audio: Any, max_s: float) -> list[tuple[int, int]]:
        found = vad.speech_timestamps(audio, max_speech_duration_s=max_s)
        return [(int(x["start"]), int(x["end"])) for x in found]
