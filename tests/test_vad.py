"""silero VAD on ONNX Runtime (mnema/core/vad.py): the same speech spans as the original
get_speech_timestamps, the model's input contract, the bundled model."""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

from mnema.core import vad
from tests import onnx_stubs

FIXTURE = Path(__file__).parent / "fixtures" / "silero-timestamps-v1.json"


_RUNS = [
    pytest.param(case, run, id=f"{case['name']}-{run['max_s']}")
    for case in json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
    for run in case["runs"]
]


@pytest.mark.parametrize(("case", "run"), _RUNS)
def test_spans_match_silero_get_speech_timestamps(monkeypatch, case, run) -> None:
    """The same probabilities give the same spans as silero-vad 6.2.1: thresholds with
    hysteresis, short pauses and bursts, cuts of long speech, padding."""
    onnx_stubs.install(monkeypatch, silero_vad=onnx_stubs.silero(case["probs"]))
    max_s = math.inf if run["max_s"] is None else run["max_s"]
    audio = np.zeros(case["samples"], np.float32)
    assert vad.speech_timestamps(audio, max_speech_duration_s=max_s) == run["expected"]


def test_each_window_carries_the_previous_context_and_state(monkeypatch) -> None:
    """512 new samples after the previous 64 (zeros at the start), the last window padded with
    zeros, the state from the previous answer; one session for the process."""
    seen: list[dict] = []
    ort = onnx_stubs.install(monkeypatch, silero_vad=onnx_stubs.silero([0.1] * 6, seen))
    audio = np.arange(1100, dtype=np.float32) / 1100
    assert vad.speech_timestamps(audio) == []
    assert len(seen) == 3
    assert not seen[0]["input"][0, :64].any()
    np.testing.assert_array_equal(seen[0]["input"][0, 64:], audio[:512])
    np.testing.assert_array_equal(seen[1]["input"][0, :64], audio[448:512])
    np.testing.assert_array_equal(seen[1]["input"][0, 64:], audio[512:1024])
    np.testing.assert_array_equal(seen[2]["input"][0, 64:140], audio[1024:])
    assert not seen[2]["input"][0, 140:].any()
    assert [float(s["state"].max()) for s in seen] == [0.0, 1.0, 2.0]
    vad.speech_timestamps(audio[:600])
    assert len(ort.sessions) == 1
    options = ort.sessions[0].options
    assert (options.intra_op_num_threads, options.inter_op_num_threads) == (1, 1)


def test_bundled_model_hears_no_speech_in_silence(monkeypatch) -> None:
    """The silero_vad.onnx shipped in the package opens and answers."""
    pytest.importorskip("onnxruntime")
    monkeypatch.setattr(vad, "_SESSION", None)  # not a fake session of another test
    assert vad.available()
    assert vad.speech_timestamps(np.zeros(3 * vad.SR, np.float32)) == []
