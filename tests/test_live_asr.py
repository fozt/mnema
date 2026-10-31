"""Live text phrase by phrase: silero closes a phrase, Parakeet decodes it with the tail of the
track before it. Boundaries: the ONNX sessions (in this process) and the live process."""

from __future__ import annotations

import pytest

from mnema.core.base import MediaError
from mnema.live.asr import PhraseTranscriber, StreamingTranscriber
from tests import onnx_stubs

SR = 16000
PIECES = ["▁раз", "▁два", "▁три", "▁эхо", "▁Mm", "-", "hmm", ".", "<blk>"]
RAZ, DVA, TRI, EHO = 0, 1, 2, 3


def _models(monkeypatch, tmp_path, probs, script, seen=None):
    """Parakeet files of the test and the sessions: silero gives probs window after window,
    the encoder gives the tokens of script[i] on the i-th decoded phrase."""
    from mnema.core import parakeet

    folder = tmp_path / "onnx"
    folder.mkdir(exist_ok=True)
    for name in parakeet.ONNX_FILES:
        (folder / name).write_bytes(b"graph")
    (folder / parakeet.VOCAB).write_text("".join(f"{p} {i}\n" for i, p in enumerate(PIECES)))
    monkeypatch.setenv("MNEMA_PARAKEET_ONNX_DIR", str(folder))
    onnx_stubs.install(
        monkeypatch,
        silero_vad=onnx_stubs.silero(probs),
        encoder=onnx_stubs.encoder(script, seen),
        decoder=onnx_stubs.decoder(len(PIECES)),
    )


_speech = onnx_stubs.speech_probs


def _pcm(seconds: float) -> bytes:
    return b"\x00\x00" * int(seconds * SR)


def _texts(chunk) -> list[str]:
    return [s.text for s in chunk.committed]


def _session(lang=None, label="mic") -> PhraseTranscriber:
    tr = PhraseTranscriber(lang=lang, label=label)
    tr.warmup()
    return tr


def test_a_phrase_shows_only_its_words_cut_at_the_middle_of_the_pause(monkeypatch, tmp_path):
    """The second phrase is decoded with the first one as its tail; the words of the tail and
    of the first half of the pause are not shown again."""
    seen: list[dict] = []
    script = [{15: RAZ}, {15: RAZ, 37: EHO, 39: DVA, 55: TRI}]
    _models(monkeypatch, tmp_path, _speech([(1.0, 2.0), (4.0, 5.0)], 8), script, seen)
    tr = _session(label="system")

    tr.feed(_pcm(8))
    chunk = tr.poll()

    assert _texts(chunk) == [" раз", " два три"]  # a leading space: the session joins as is
    assert {s.speaker for s in chunk.committed} == {"system"}
    second = chunk.committed[1]
    assert second.start == pytest.approx(39 * 0.08) and second.end == pytest.approx(56 * 0.08)
    assert seen[1]["length"] > 4.9 * 100  # decoded from the start of the track, with the tail
    assert tr.poll().committed == [] and chunk.draft == ""


def test_the_tail_is_at_most_ten_seconds_and_times_count_from_the_stream(monkeypatch, tmp_path):
    seen: list[dict] = []
    speech = _speech([(1.0, 2.0), (20.0, 21.0)], 24)
    _models(monkeypatch, tmp_path, speech, [{15: RAZ}, {130: DVA}], seen)
    tr = _session()

    for _second in range(24):  # fed in pieces, as the process gets them
        tr.feed(_pcm(1))
    chunk = tr.poll()

    assert _texts(chunk) == [" раз", " два"]
    start = 20.0 - 0.03 - 10  # the speech start padded by silero, minus the tail
    assert seen[1]["length"] / 100 == pytest.approx(10 + 1.08, abs=0.05)
    assert chunk.committed[1].start == pytest.approx(start + 130 * 0.08)


def test_long_speech_is_cut_at_its_quietest_point_before_fifteen_seconds(monkeypatch, tmp_path):
    from mnema.core import vad

    seen: list[dict] = []
    probs = _speech([(0.5, 20.0)], 22)
    dip = 437  # 13.98 s: quieter, still speech
    probs[dip] = 0.6
    _models(monkeypatch, tmp_path, probs, [{100: RAZ}, {50: RAZ, 120: DVA, 130: TRI}], seen)
    tr = _session()

    tr.feed(_pcm(22))
    chunk = tr.poll()

    cut = dip * vad.WINDOW / SR
    assert seen[0]["length"] / 100 == pytest.approx(cut, abs=0.02)
    assert _texts(chunk) == [" раз", " три"]
    assert chunk.committed[1].start == pytest.approx(cut - 10 + 130 * 0.08)


def test_speech_shows_a_draft_and_finish_recognizes_the_open_phrase(monkeypatch, tmp_path):
    _models(monkeypatch, tmp_path, _speech([(1.0, 3.0)], 2.5), [{20: RAZ}])
    tr = _session()

    tr.feed(_pcm(0.5))
    assert tr.poll().draft == ""
    tr.feed(_pcm(1.5))
    assert tr.poll().draft == "…"
    tr.feed(_pcm(0.5))
    middle = tr.poll()
    assert middle.committed == [] and middle.draft == "…"

    tail = tr.finish()

    assert _texts(tail) == [" раз"] and tail.draft == ""


@pytest.mark.parametrize(("lang", "text"), [("ru", " Угу."), (None, " Mm-hmm.")])
def test_a_phrase_is_cleaned_by_the_given_language(monkeypatch, tmp_path, lang, text):
    _models(monkeypatch, tmp_path, _speech([(1.0, 2.0)], 3), [{15: 4, 16: 5, 17: 6, 18: 7}])
    tr = _session(lang=lang)

    tr.feed(_pcm(3))

    assert _texts(tr.poll()) == [text]


def test_live_refuses_until_the_model_is_downloaded(monkeypatch):
    monkeypatch.delenv("MNEMA_PARAKEET_ONNX_DIR", raising=False)

    with pytest.raises(MediaError, match="ещё не скачана"):
        PhraseTranscriber().warmup()


# ── the live process ──


def _tone(seconds: float) -> bytes:
    import numpy as np

    t = np.arange(int(seconds * SR)) / SR
    return (np.sin(2 * np.pi * 220 * t) * 0.3 * 32767).astype("<i2").tobytes()


def test_streaming_process_returns_the_phrase_and_exits(live_process, monkeypatch):
    import builtins

    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        assert name.split(".")[0] not in {"onnxruntime", "numpy"}, name
        return original(name, *args, **kwargs)

    tone = _tone(1) + _pcm(0.5)
    monkeypatch.setattr(builtins, "__import__", guarded)
    tr = StreamingTranscriber(label="system")
    try:
        tr.warmup()
        tr.feed(tone)
        result = tr.finish()
    finally:
        tr.close()
    assert [(s.text, s.speaker) for s in result.committed] == [(" hello", "system")]
    assert tr.poll().committed == []
    live_process.stopped()


def test_streaming_process_close_interrupts_warmup(live_process, monkeypatch):
    monkeypatch.setenv("MNEMA_TEST_LIVE_MODE", "wait")
    tr = StreamingTranscriber()
    tr.start_warmup()
    try:
        live_process.started()
    finally:
        tr.close()
    live_process.stopped()


def test_streaming_process_error_redacts_secrets(live_process, monkeypatch):
    monkeypatch.setenv("MNEMA_TEST_LIVE_MODE", "fail-secret")
    tr = StreamingTranscriber()
    with pytest.raises(MediaError, match="движок не собрался") as error:
        tr.warmup()
    assert "sk-private-test-secret" not in str(error.value)
    assert "…7890" in str(error.value)
    live_process.stopped()


@pytest.mark.parametrize("threads", [2, 6, "auto"])
def test_the_encoder_uses_the_cpu_budget_of_the_session(live_process, threads):
    from mnema.core.settings import available_cpu_threads

    tr = StreamingTranscriber(threads=threads)
    try:
        tr.warmup()
        row = live_process.started()[0]
    finally:
        tr.close()
    available = available_cpu_threads()
    assert row["threads"] == (available if threads == "auto" else min(threads, available))
    live_process.stopped()


# ── PcmPrebuffer: PCM during warmup ──


def test_prebuffer_is_bounded_and_drops_oldest() -> None:
    """Limited to about a second of audio: older audio is dropped (the recording stays complete;
    only the transcript of the beginning is lost on a very long warmup)."""
    from mnema.live.asr import PcmPrebuffer

    buf = PcmPrebuffer(seconds=1.0)  # limit 32000 bytes (16 kHz * 2 bytes)
    chunks = [bytes([i]) * 16000 for i in range(5)]  # 5 half-second pieces
    for c in chunks:
        buf.push(c)
    assert buf.drain() == chunks[-2:]  # only the last second or so remains, in order
    assert buf.drain() == []  # empty after draining
    # dropped audio is counted: the stream fed to ASR starts this much later
    assert buf.dropped_seconds == 1.5  # 3 pieces of 0.5 s


def test_prebuffer_drops_whole_samples_of_odd_chunks() -> None:
    """Odd chunks overflow the prebuffer: what stays starts on a sample, so the rest of the
    PCM16 stream is not shifted by a byte."""
    from mnema.live.asr import PcmPrebuffer

    buf = PcmPrebuffer(seconds=1.0)
    stream = bytes(i % 251 for i in range(100_001))
    for at in range(0, len(stream), 3001):
        buf.push(stream[at : at + 3001])
    kept = b"".join(buf.drain())

    assert stream.endswith(kept) and len(kept) <= 32000
    dropped = len(stream) - len(kept)
    assert dropped % 2 == 0
    assert buf.dropped_seconds == dropped / 32000


def _decoded(monkeypatch, tmp_path, pieces):
    seen: list[dict] = []
    _models(
        monkeypatch, tmp_path, _speech([(1.0, 2.0), (4.0, 5.0)], 6), [{15: RAZ}, {55: DVA}], seen
    )
    tr = _session()
    for piece in pieces:
        tr.feed(piece)
    return _texts(tr.finish()), [s["length"] for s in seen]


def test_a_chunk_split_in_the_middle_of_a_sample_loses_nothing(monkeypatch, tmp_path):
    """The client sends a byte stream: an odd chunk keeps its last byte for the next one."""
    pcm = _pcm(6)
    odd = [pcm[at : at + 4001] for at in range(0, len(pcm), 4001)]

    assert _decoded(monkeypatch, tmp_path, odd) == _decoded(monkeypatch, tmp_path, [pcm])
    assert _decoded(monkeypatch, tmp_path, odd)[0] == [" раз", " два"]
