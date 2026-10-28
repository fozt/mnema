"""Parakeet Ultra: an audio window → replies.

moondream/parakeet-ultra is a fine-tuned nvidia/parakeet-tdt-0.6b-v3 (same architecture and
tokenizer, 25 languages). ParakeetForTDT exists only in transformers 5, which conflicts with
whisperx on Linux (huggingface-hub), so the encoder is the installed transformers'
ParakeetEncoder, while the features and the greedy TDT decoder are our own (matched against
transformers 5.19 word for word).

The module imports without torch: heavy work happens inside functions.
"""

from __future__ import annotations

import json
import platform
import re
import sys
import threading
from pathlib import Path
from typing import Any, NamedTuple

from ..i18n import _
from .base import MediaError, Progress, noop
from .models import Segment

REPO = "moondream/parakeet-ultra"
_FILES = ("model.safetensors", "config.json", "tokenizer.json")
SR = 16000
FRAME_S = 0.08  # encoder frame: 10 ms feature hop × subsampling 8
# Recognition window (as in FluidAudio): on a short chunk the model mistakes the language.
WINDOW_S = 15.0
REPLICA_GAP_S = 0.6  # a longer pause between words starts a new reply (as with GigaAM)
# the model's languages (parakeet-tdt-0.6b-v3 model card)
LANGS = frozenset(
    "bg hr cs da nl en et fi fr de el hu it lv lt mt pl pt ro sk sl es sv ru uk".split()
)

_N_FFT, _HOP, _WIN, _MELS, _PREEMPH = 512, 160, 400, 128, 0.97
_LOG_GUARD, _EPS = 2.0**-24, 1e-5
_BAN = -1e4  # logit shift of a banned token

_UK = set("іїєґІЇЄҐ")  # Ukrainian letters: in a Russian call a model slip, not speech
_FILLER_YES = {"mm-hmm", "mhm", "uh-huh", "mm", "hmm", "mm-mm"}  # → "Угу."
_FILLER_DROP = {"uh", "um", "ah", "er", "erm"}


class Emitted(NamedTuple):
    """A greedy TDT token: id, encoder frame, duration in frames (≥ 1 for the end time)."""

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


def features(audio: Any) -> tuple[Any, Any]:
    """Audio [n] float32 16 kHz → (features [1, T, 128], length mask [1, T])."""
    import torch

    global _MEL
    if _MEL is None:
        _MEL = torch.from_numpy(mel_filters())
    x = audio.to(torch.float32)
    n = x.shape[0]
    x = torch.cat([x[:1], x[1:] - _PREEMPH * x[:-1]])
    spec = torch.stft(
        x,
        _N_FFT,
        hop_length=_HOP,
        win_length=_WIN,
        window=torch.hann_window(_WIN, periodic=False),
        return_complex=True,
        pad_mode="constant",
    )
    power = torch.view_as_real(spec).pow(2).sum(-1).sqrt().pow(2)
    mel = torch.log(_MEL @ power + _LOG_GUARD).T  # [T, 128]
    length = max(1, (n + _N_FFT // 2 * 2 - _N_FFT) // _HOP)
    mask = torch.arange(mel.shape[0]) < length
    valid = mel[mask]
    mean = valid.mean(0)
    std = ((valid - mean) ** 2).sum(0).div(max(length - 1, 1)).sqrt()
    mel = (mel - mean) / (std + _EPS) * mask[:, None]
    return mel[None], mask[None]


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
        out.append(Emitted(token=token, frame=t, duration=max(d, 1)))
        step(token)
        symbols += 1
        if d > 0 or symbols >= max_symbols:
            t += max(d, 1)
            symbols = 0
    return out


# ─────────────────────────────────────────── replies and cleanup


def split_replicas(
    pieces: list[tuple[str, float, float]], gap: float = REPLICA_GAP_S
) -> list[list[tuple[str, float, float]]]:
    """Window tokens (SentencePiece piece, start, end in s) → replies: a new one at a pause
    > gap, only at a word start ("▁") so words are never split."""
    out: list[list[tuple[str, float, float]]] = []
    for piece in pieces:
        if out and not (piece[0].startswith("▁") and piece[1] - out[-1][-1][2] > gap):
            out[-1].append(piece)
        else:
            out.append([piece])
    return out


def banned_tokens(vocab: dict[str, int]) -> list[int]:
    """Tokens with Ukrainian letters, banned in a Russian call (Latin stays allowed: API, MCP
    are real words in Russian speech)."""
    return sorted(i for piece, i in vocab.items() if _UK & set(piece))


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


def ukrainian_slip(text: str) -> bool:
    """Russian speech where the model, without a language hint, slipped in Ukrainian letters:
    Cyrillic text with few such words (real Ukrainian has them in every third or fourth)."""
    words = [w for w in re.findall(r"\w+", text.lower()) if _CYRILLIC.search(w)]
    slips = sum(1 for w in words if _UK & set(w))
    return 0 < slips <= max(1, len(words) * 0.15)


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


# ─────────────────────────────────────────── model


def _cached_snapshot() -> Path | None:
    """A complete Ultra weights snapshot in the HF cache, or None."""
    from .backends import _hf_cache_dir

    snaps = _hf_cache_dir() / ("models--" + REPO.replace("/", "--")) / "snapshots"
    for snap in sorted(snaps.iterdir()) if snaps.is_dir() else []:
        if all((snap / f).is_file() and (snap / f).stat().st_size > 0 for f in _FILES):
            return snap
    return None


def snapshot_dir(on: Progress = noop) -> Path:
    """The Ultra weights folder in the HF cache (downloaded once, ~1.3 GB)."""
    cached = _cached_snapshot()
    if cached is not None:
        return cached
    on(None, _("Downloading the recognition model (only once)…"))
    try:
        from huggingface_hub import snapshot_download
    except ImportError as e:
        raise MediaError(
            _(
                "huggingface_hub is missing — reinstall Mnema "
                "(./install.sh client or ./install.sh server)."
            )
        ) from e
    return Path(snapshot_download(repo_id=REPO, allow_patterns=list(_FILES)))


def available() -> bool:
    """Whether everything Parakeet needs is installed (without importing modules or weights)."""
    import importlib.util

    try:
        if not all(
            importlib.util.find_spec(m)
            for m in ("torch", "tokenizers", "safetensors", "silero_vad")
        ):
            return False
        if _use_mlx():
            return all(importlib.util.find_spec(m) for m in ("mlx", "parakeet_mlx"))
        # Finding a nested spec imports transformers, and it imports torch. Checking the
        # installed folder keeps engine selection in the parent truly light.
        spec = importlib.util.find_spec("transformers")
        return bool(
            spec and spec.origin and (Path(spec.origin).parent / "models/parakeet").is_dir()
        )
    except (ImportError, ValueError):
        return False


class Model:
    """Ultra on CPU/GPU: the transformers encoder plus our TDT decoder on the same weights."""

    def __init__(self, path: Path, device: str = "cpu") -> None:
        import contextlib
        import io

        import torch
        from safetensors import safe_open
        from tokenizers import Tokenizer

        with contextlib.redirect_stdout(io.StringIO()):  # transformers 4.57 prints "🚨 Config…"
            from transformers.models.parakeet import ParakeetEncoder, ParakeetEncoderConfig
        skip_init: Any = contextlib.nullcontext  # other transformers: random init, slower
        with contextlib.suppress(ImportError):
            from transformers.modeling_utils import no_init_weights as skip_init

        cfg = json.loads((path / "config.json").read_text(encoding="utf-8"))
        enc_cfg = {k: v for k, v in cfg["encoder_config"].items() if k != "model_type"}
        with skip_init():  # weights come from the file anyway: random init costs 5 s and a copy
            self.encoder = ParakeetEncoder(ParakeetEncoderConfig(**enc_cfg)).eval()
        h = cfg["decoder_hidden_size"]
        self.lstm = torch.nn.LSTM(h, h, num_layers=cfg["num_decoder_layers"], batch_first=True)
        self.w = {}
        # tensor by tensor straight into the parameters (float16 → float32 in place): the whole
        # file in float32 next to the model would add 2.4 GB of peak; the model keeps copies only
        # (a slice would keep the whole file alive)
        targets = {f"encoder.{k}": v for k, v in self.encoder.state_dict(keep_vars=True).items()}
        targets |= {f"decoder.lstm.{k}": v for k, v in self.lstm.state_dict(keep_vars=True).items()}
        with safe_open(str(path / "model.safetensors"), "pt") as f, torch.no_grad():
            for k in f.keys():  # noqa: SIM118 - safe_open has no __iter__
                if k in targets:
                    targets.pop(k).copy_(f.get_tensor(k))
                elif not k.startswith(("encoder.", "decoder.lstm.", "vad_head.")):
                    self.w[k] = f.get_tensor(k).to(torch.float32, copy=True)
            # convolution_bias=false, but transformers 4.x still has the bias
            for k, t in targets.items():
                if not k.endswith(".bias"):
                    raise MediaError(
                        _(
                            "The Parakeet weights do not match the encoder: {name} is missing",
                            name=k,
                        )
                    )
                t.zero_()
        self.blank, self.vocab = cfg["blank_token_id"], cfg["vocab_size"]
        self.durations = list(cfg["durations"])
        self.max_symbols = int(cfg.get("max_symbols_per_step", 10))
        self.tokenizer = Tokenizer.from_file(str(path / "tokenizer.json"))
        self.banned = torch.tensor(banned_tokens(self.tokenizer.get_vocab()), dtype=torch.long)
        self.device = device
        self.encoder.to(device)
        self.lstm.to(device)
        self.w = {k: v.to(device) for k, v in self.w.items()}

    def _linear(self, name: str, x: Any) -> Any:
        import torch

        return torch.nn.functional.linear(x, self.w[f"{name}.weight"], self.w[f"{name}.bias"])

    def transcribe(self, audio: Any, lang: str | None = None) -> list[Segment]:
        """An audio window (≤ ~15 s) → replies timed from the window start. Without a language
        there is no hint; if Ukrainian letters slip into Russian speech, the same encoder output
        (90% of the time) is decoded again with them banned."""
        import torch

        feats, mask = features(audio)
        with torch.inference_mode():
            hidden = self.encoder(
                input_features=feats.to(self.device), attention_mask=mask.to(self.device)
            ).last_hidden_state[0]
            frames = min(hidden.shape[0], int(_out_len(int(mask.sum()))))
            enc = self._linear("encoder_projector", hidden)
            segments = self._decode(enc, frames, ban=lang == "ru")
            if lang is None and ukrainian_slip(" ".join(s.text for s in segments)):
                segments = self._decode(enc, frames, ban=True)
        return tidy(segments, lang)

    def _decode(self, enc: Any, frames: int, *, ban: bool) -> list[Segment]:
        import torch

        with torch.inference_mode():
            bias = torch.zeros(self.vocab + len(self.durations), device=self.device)
            if ban and len(self.banned):
                bias[self.banned.to(self.device)] = _BAN
            state: list[Any] = [None, None]

            def step(token: int) -> None:
                emb = self.w["decoder.embedding.weight"][token].view(1, 1, -1)
                out, state[1] = self.lstm(emb, state[1])
                state[0] = self._linear("decoder.decoder_projector", out[0, 0])

            def joint(t: int) -> Any:
                return self._linear("joint.head", torch.relu(enc[t] + state[0])) + bias

            step(self.blank)
            emitted = greedy_tdt(
                frames,
                joint=joint,
                step=step,
                blank=self.blank,
                vocab=self.vocab,
                durations=self.durations,
                max_symbols=self.max_symbols,
            )
        pieces = [
            (
                self.tokenizer.id_to_token(e.token) or "",
                e.frame * FRAME_S,
                (e.frame + e.duration) * FRAME_S,
            )
            for e in emitted
        ]
        segments: list[Segment] = []
        for rep in split_replicas(pieces):
            text = self.tokenizer.decode(
                [self.tokenizer.token_to_id(p) for p, *_rest in rep]
            ).strip()
            if text:
                segments.append(Segment(start=rep[0][1], end=rep[-1][2], text=text))
        return segments


def _out_len(n_feats: int) -> int:
    """Encoder output length from the feature length: three 3×3 convolutions, stride 2."""
    for _layer in range(3):
        n = (n_feats + 2 * 1 - 3) // 2 + 1
        n_feats = n
    return n_feats


_LOCK = threading.Lock()
_MODEL: Any = None


def _use_mlx() -> bool:
    return sys.platform == "darwin" and platform.machine() == "arm64"


def model(on: Progress = noop) -> Any:
    """One model per process (lazy, under a lock, ~2.4 GB in float32); the memory goes away
    with the job process (core/worker.py)."""
    global _MODEL
    current = _MODEL
    if current is None:
        with _LOCK:
            if _MODEL is None:
                path = snapshot_dir(on)
                on(None, _("Loading the recognition model…"))
                from .backends import _CUDA_LOAD_LOCK, _quiet_third_party, _torch_device

                with _CUDA_LOAD_LOCK, _quiet_third_party():
                    if _use_mlx():
                        from .parakeet_mlx import Model as MlxModel

                        _MODEL = MlxModel(path)
                    else:
                        _MODEL = Model(path, device=_torch_device())
            current = _MODEL
    return current
