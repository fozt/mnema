"""A fake onnxruntime: the external boundary of Parakeet and silero. Each session is a handler
by file name that checks the inputs of the real graph and answers with scripted outputs."""

from __future__ import annotations

import sys
import types
from collections.abc import Callable
from pathlib import Path

import numpy as np

SR = 16000
Handler = Callable[[dict], dict]


class Options:
    def __init__(self) -> None:
        self.entries: dict[str, str] = {}

    def add_session_config_entry(self, key: str, value: str) -> None:
        self.entries[key] = value


class Session:
    def __init__(self, name: str, handler: Handler, options: Options | None) -> None:
        self.name, self.handler, self.options = name, handler, options

    def run(self, names, feeds):
        out = self.handler(feeds)
        return [out[n] for n in (names or out)]


def install(monkeypatch, **handlers: Handler) -> types.SimpleNamespace:
    """sys.modules["onnxruntime"] with sessions by file name: silero_vad, encoder, decoder.
    The sessions this process already opened are forgotten."""
    from mnema.core import parakeet, vad

    monkeypatch.setattr(vad, "_SESSION", None)
    monkeypatch.setattr(parakeet, "_MODEL", None)
    sessions: list[Session] = []

    def open_session(path, sess_options=None, providers=None):
        name = Path(path).name
        kind = next(k for k in handlers if k in name)
        session = Session(name, handlers[kind], sess_options)
        sessions.append(session)
        return session

    ort = types.SimpleNamespace(
        SessionOptions=Options, InferenceSession=open_session, sessions=sessions
    )
    monkeypatch.setitem(sys.modules, "onnxruntime", ort)
    return ort


def silero(probs: list[float], seen: list[dict] | None = None) -> Handler:
    """The silero graph: 64 samples of context + 512 new ones, state [2, 1, 128], sr 16000."""
    it = iter(probs)

    def run(feeds: dict) -> dict:
        x, state, sr = feeds["input"], feeds["state"], feeds["sr"]
        assert x.shape == (1, 576) and x.dtype == np.float32
        assert state.shape == (2, 1, 128) and state.dtype == np.float32
        assert sr.dtype == np.int64 and int(sr) == SR
        if seen is not None:
            seen.append({"input": x.copy(), "state": state.copy()})
        return {"output": np.array([[next(it)]], np.float32), "stateN": state + 1}

    return run


def speech_probs(spans: list[tuple[float, float]], seconds: float) -> list[float]:
    """Window probabilities of speech in the given spans (seconds)."""
    windows = -(-int(seconds * SR) // 512)
    return [
        0.9 if any(s * SR <= i * 512 < e * SR for s, e in spans) else 0.05 for i in range(windows)
    ]


def out_len(n: int) -> int:
    for _layer in range(3):
        n = (n - 1) // 2 + 1
    return n


def encoder(script: list[dict[int, int]], seen: list[dict] | None = None) -> Handler:
    """Encoder [1, 128, T] → [1, 1024, T']: call i puts token+1 into channel 0 of the frames of
    script[i] (zero there: nothing but blank)."""
    it = iter(script)

    def run(feeds: dict) -> dict:
        x, length = feeds["audio_signal"], feeds["length"]
        assert x.ndim == 3 and x.shape[:2] == (1, 128) and x.dtype == np.float32
        assert length.dtype == np.int64 and length.shape == (1,)
        if seen is not None:
            seen.append({"frames": x.shape[2], "length": int(length[0])})
        frames = out_len(x.shape[2])
        out = np.zeros((1, 1024, frames), np.float32)
        for frame, token in next(it).items():
            out[0, 0, frame] = token + 1
        return {"outputs": out, "encoded_lengths": np.array([out_len(int(length[0]))])}

    return run


def decoder(vocab: int, seen: list[dict] | None = None) -> Handler:
    """decoder_joint: the token from channel 0 of the frame (else blank = vocab - 1) with
    duration index 1; new states are the old ones + 1."""

    def run(feeds: dict) -> dict:
        enc, targets = feeds["encoder_outputs"], feeds["targets"]
        assert enc.shape == (1, 1024, 1) and enc.dtype == np.float32
        assert targets.shape == (1, 1) and targets.dtype == np.int32
        assert feeds["target_length"].dtype == np.int32 and list(feeds["target_length"]) == [1]
        states = feeds["input_states_1"], feeds["input_states_2"]
        assert all(s.shape == (2, 1, 640) and s.dtype == np.float32 for s in states)
        if seen is not None:
            seen.append({"target": int(targets[0, 0]), "state": float(states[0][0, 0, 0])})
        value = int(enc[0, 0, 0])
        logits = np.zeros((1, 1, 1, vocab + 5), np.float32)
        logits[0, 0, 0, value - 1 if value else vocab - 1] = 1
        logits[0, 0, 0, vocab + 1] = 1
        return {
            "outputs": logits,
            "output_states_1": states[0] + 1,
            "output_states_2": states[1] + 1,
        }

    return run
