"""One recognition interface for every local engine: words with times, speech spans.

Replies, track merging and paragraphs are one code above it: replies() here, Transcript in
models.py. The interface imports without numpy or onnxruntime.
"""

from __future__ import annotations

import wave
from pathlib import Path
from typing import Any, Protocol

from .base import Progress, noop
from .models import Segment, Word

SR = 16000
REPLY_GAP_S = 0.6  # a longer pause between words starts a new reply


class Recognizer(Protocol):
    id: str  # "parakeet": the saved choice (bar.json, the phone, history)
    model: str  # "parakeet-ultra"
    langs: frozenset[str]

    def available(self) -> bool:
        """Whether it works here, without loading the model."""
        ...

    def words(self, wav: Path, *, on: Progress = noop) -> list[Word]:
        """A PCM16 mono 16 kHz track → its words."""
        ...

    def speech(self, wav: Path, *, max_s: float) -> list[tuple[int, int]]:
        """Speech spans of the track in samples, each at most max_s long."""
        ...


def local_recognizer() -> Recognizer:
    """This machine's recognizer: on the Mac mnema-asr of Mnema.app (CoreML), elsewhere
    Parakeet Ultra on ONNX Runtime."""
    from . import base

    if base.STANDALONE:
        from .mac_helper import MacHelper

        return MacHelper()
    from .parakeet import OnnxParakeet

    return OnnxParakeet()


def replies(words: list[Word], *, gap: float = REPLY_GAP_S) -> list[Segment]:
    """Words → replies: a new one after a pause longer than gap."""
    out: list[Segment] = []
    for w in words:
        if out and w.start - out[-1].end <= gap:
            last = out[-1]
            out[-1] = last.model_copy(update={"end": w.end, "text": f"{last.text} {w.text}"})
            out[-1].words.append(w)
        else:
            out.append(Segment(start=w.start, end=w.end, text=w.text, words=[w]))
    return out


def group_speech(
    speech: list[tuple[int, int]], max_window: int, max_gap: int
) -> list[tuple[int, int]]:
    """Speech spans (samples) → windows; neighbours join only across a pause ≤ max_gap while the
    window stays ≤ max_window, a longer span stays whole."""
    windows: list[tuple[int, int]] = []
    for s, e in speech:
        if windows and e - windows[-1][0] <= max_window and s - windows[-1][1] <= max_gap:
            windows[-1] = (windows[-1][0], e)
        else:
            windows.append((s, e))
    return windows


def read_wav(wav: Path) -> Any:
    """PCM16 mono WAV (from our extract_audio) → float32 numpy in [-1..1]."""
    import numpy as np

    with wave.open(str(wav)) as w:
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32)
    audio *= 1 / 32768  # in place: a copy costs 4 more bytes per sample (1 GB for 4.7 h)
    return audio
