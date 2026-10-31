"""silero VAD on ONNX Runtime: speech spans of a 16 kHz track.

silero_vad.onnx (and its MIT LICENSE) is silero-vad 6.2.1's, shipped in the package: no network.
speech_timestamps() is a numpy port of silero's get_speech_timestamps with its defaults (the
fixture tests/fixtures/silero-timestamps-v1.json holds the original's answers).
"""

from __future__ import annotations

import math
import threading
from pathlib import Path
from typing import Any

SR = 16000
MODEL = Path(__file__).with_name("silero_vad.onnx")
WINDOW, _CONTEXT = 512, 64  # samples of one probability and of the context before it
_THRESHOLD = 0.5
_MIN_SPEECH_MS, _MIN_SILENCE_MS, _PAD_MS, _MIN_SILENCE_AT_MAX_MS = 250, 100, 30, 98

_LOCK = threading.Lock()
_SESSION: Any = None


def available() -> bool:
    """Whether ONNX Runtime and numpy are installed (without importing them)."""
    import importlib.util

    return all(importlib.util.find_spec(m) is not None for m in ("onnxruntime", "numpy"))


def _session() -> Any:
    """One session per process; the model state goes with each call, so calls may overlap."""
    global _SESSION
    with _LOCK:
        if _SESSION is None:
            import onnxruntime as ort

            options = ort.SessionOptions()
            options.intra_op_num_threads = 1
            options.inter_op_num_threads = 1
            _SESSION = ort.InferenceSession(str(MODEL), options, providers=["CPUExecutionProvider"])
        return _SESSION


class Stream:
    """Speech probabilities of a stream, window after window: the model state and context go
    from one window to the next, so a track fed in pieces gets what probabilities() gives."""

    def __init__(self) -> None:
        import numpy as np

        self._session = _session()
        self._state = np.zeros((2, 1, 128), np.float32)
        self._context = np.zeros(_CONTEXT, np.float32)
        self._sr = np.array(SR, np.int64)

    def window(self, piece: Any) -> float:
        """The probability of the next ≤ WINDOW samples (a shorter piece is padded with zeros)."""
        import numpy as np

        chunk = np.zeros(WINDOW, np.float32)
        chunk[: len(piece)] = piece
        x = np.concatenate([self._context, chunk])[None]
        out, self._state = self._session.run(
            None, {"input": x, "state": self._state, "sr": self._sr}
        )
        self._context = chunk[-_CONTEXT:]
        return float(np.float32(out[0, 0]))


def probabilities(audio: Any) -> list[float]:
    """Speech probability of every WINDOW-sample window (the last one padded with zeros)."""
    stream = Stream()
    return [stream.window(audio[start : start + WINDOW]) for start in range(0, len(audio), WINDOW)]


def speech_timestamps(audio: Any, *, max_speech_duration_s: float = math.inf) -> list[dict]:
    """Speech spans in samples, [{"start", "end"}], as silero's get_speech_timestamps."""
    return timestamps(probabilities(audio), len(audio), max_speech_duration_s=max_speech_duration_s)


def timestamps(
    probs: list[float], samples: int, *, max_speech_duration_s: float = math.inf
) -> list[dict]:
    """Window probabilities → spans; silero 6.2.1 line by line (use_max_poss_sil_at_max_speech)."""
    min_speech = SR * _MIN_SPEECH_MS / 1000
    pad = SR * _PAD_MS / 1000
    max_speech = SR * max_speech_duration_s - WINDOW - 2 * pad
    min_silence = SR * _MIN_SILENCE_MS / 1000
    min_silence_at_max = SR * _MIN_SILENCE_AT_MAX_MS / 1000
    neg_threshold = max(_THRESHOLD - 0.15, 0.01)

    triggered = False
    speeches: list[dict] = []
    current: dict = {}
    temp_end = prev_end = next_start = 0
    possible_ends: list[tuple[int, int]] = []

    for i, prob in enumerate(probs):
        cur = WINDOW * i
        if prob >= _THRESHOLD and temp_end:
            silence = cur - temp_end
            if silence > min_silence_at_max:
                possible_ends.append((temp_end, silence))
            temp_end = 0
            if next_start < prev_end:
                next_start = cur
        if prob >= _THRESHOLD and not triggered:
            triggered = True
            current["start"] = cur
            continue
        if triggered and cur - current["start"] > max_speech:
            if possible_ends:  # cut at the longest silence of this speech
                prev_end, dur = max(possible_ends, key=lambda x: x[1])
                current["end"] = prev_end
                speeches.append(current)
                current = {}
                next_start = prev_end + dur
                if next_start < prev_end + cur:
                    current["start"] = next_start
                else:
                    triggered = False
                prev_end = next_start = temp_end = 0
                possible_ends = []
            elif prev_end:
                current["end"] = prev_end
                speeches.append(current)
                current = {}
                if next_start < prev_end:
                    triggered = False
                else:
                    current["start"] = next_start
                prev_end = next_start = temp_end = 0
                possible_ends = []
            else:  # no silence at all: cut right here
                current["end"] = cur
                speeches.append(current)
                current = {}
                prev_end = next_start = temp_end = 0
                triggered = False
                possible_ends = []
                continue
        if prob < neg_threshold and triggered:
            if not temp_end:
                temp_end = cur
            if cur - temp_end < min_silence:
                continue
            current["end"] = temp_end
            if current["end"] - current["start"] > min_speech:
                speeches.append(current)
            current = {}
            prev_end = next_start = temp_end = 0
            triggered = False
            possible_ends = []
            continue

    if current and samples - current["start"] > min_speech:
        current["end"] = samples
        speeches.append(current)

    for i, speech in enumerate(speeches):
        if i == 0:
            speech["start"] = int(max(0, speech["start"] - pad))
        if i != len(speeches) - 1:
            silence = speeches[i + 1]["start"] - speech["end"]
            if silence < 2 * pad:
                speech["end"] += int(silence // 2)
                speeches[i + 1]["start"] = int(max(0, speeches[i + 1]["start"] - silence // 2))
            else:
                speech["end"] = int(min(samples, speech["end"] + pad))
                speeches[i + 1]["start"] = int(max(0, speeches[i + 1]["start"] - pad))
        else:
            speech["end"] = int(min(samples, speech["end"] + pad))
    return speeches
