"""The ASR adapter: pure FrontData -> LiveChunk mapping (no whisperlivekit)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from mnema.live.asr import StreamingTranscriber


def test_streaming_process_returns_tail_and_exits(wlk_process, monkeypatch):
    import builtins

    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        assert name.split(".")[0] not in {"torch", "whisperlivekit", "mlx"}
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    tr = StreamingTranscriber(label="system")
    try:
        tr.warmup()
        tr.feed(b"audio")
        result = tr.finish()
    finally:
        tr.close()
    assert [(s.text, s.speaker) for s in result.committed] == [("hello", "system")]
    assert tr.poll().committed == []
    wlk_process[1]()


def test_streaming_process_close_interrupts_warmup(wlk_process):
    tr = StreamingTranscriber(model="wait")
    tr.start_warmup()
    try:
        wlk_process[0]()
    finally:
        tr.close()
    wlk_process[1]()


def test_streaming_process_error_redacts_secrets(wlk_process):
    import pytest

    from mnema.core.base import MediaError

    tr = StreamingTranscriber(model="fail-secret")
    with pytest.raises(MediaError, match="движок не собрался") as error:
        tr.warmup()
    assert "sk-private-test-secret" not in str(error.value)
    assert "…7890" in str(error.value)
    wlk_process[1]()


@pytest.mark.parametrize("threads", [2, 6, "auto"])
def test_faster_whisper_uses_child_cpu_budget(wlk_process, monkeypatch, threads):
    import os

    from mnema.core.settings import available_cpu_threads

    monkeypatch.setenv("OMP_NUM_THREADS", "17")
    tr = StreamingTranscriber(backend="faster-whisper", threads=threads)
    try:
        tr.warmup()
        row = wlk_process[0]()[0]
        available = available_cpu_threads()
        expected = available if threads == "auto" else min(threads, available)
        assert row["threads"] == expected
        assert row["omp_at_import"] == row["torch_omp_at_import"] == str(expected)
        assert row["omp_at_engine"] == str(expected)
        assert os.environ["OMP_NUM_THREADS"] == "17"
    finally:
        tr.close()
    wlk_process[1]()


def _seg(text, start, end, speaker=-1):
    return SimpleNamespace(start=start, end=end, text=text, speaker=speaker)


def _front(lines, buffer=""):
    return SimpleNamespace(lines=lines, buffer_transcription=buffer)


def test_to_chunk_appends_only_new_text() -> None:
    tr = StreamingTranscriber(label="mic")
    c1 = tr._to_chunk(_front([_seg(" It is working", 0.1, 0.96)]))
    # the text is kept raw (with WLK's leading space), so compare after .strip()
    assert [s.text.strip() for s in c1.committed] == ["It is working"]
    assert c1.committed[0].speaker == "mic" and c1.committed[0].end == 0.96

    # the same segment grew (AlignAtt is append-only): the delta is only the new suffix
    c2 = tr._to_chunk(_front([_seg(" It is working correctly.", 0.1, 1.66)]))
    assert [s.text.strip() for s in c2.committed] == ["correctly."]
    assert c2.committed[0].start == 0.96 and c2.committed[0].end == 1.66
    # the same front again: no new text
    assert tr._to_chunk(_front([_seg(" It is working correctly.", 0.1, 1.66)])).committed == []


def test_to_chunk_draft_from_buffer() -> None:
    tr = StreamingTranscriber(label="mic")
    c = tr._to_chunk(_front([_seg("", 0.0, 0.0, speaker=-2)], buffer=" черновик "))
    assert c.committed == []
    assert c.draft == "черновик"

    front = _front(
        [
            _seg(" It is working", 0.1, 0.96, speaker=-1),
            _seg("", 6.4, 6.976, speaker=-2),  # SilentSegment is skipped
        ],
        buffer="хвост",
    )
    c = StreamingTranscriber(label="mic")._to_chunk(front)
    assert [s.text.strip() for s in c.committed] == ["It is working"]
    assert c.committed[0].end == 0.96  # end of the last real segment, not the silent one
    assert c.draft == "хвост"


_SESSION = " ".join(f"предложение номер {i} про дейли." for i in range(300))


@pytest.mark.parametrize(
    ("emitted", "full", "delta"),
    [
        ("", " привет", " привет"),  # first delta
        (" привет", " привет как дела", " как дела"),  # growth with a shared start
        # WLK trimmed its context: full starts with the tail of emitted, return the continuation
        ("Привет, как твои дела? Что ты делаешь", "Что ты делаешь сейчас?", " сейчас?"),
        # regression: after trimming, the tail is longer than any window; the delta is still
        # only the new words, not the whole session
        (_SESSION, _SESSION[-4000:] + " и новые слова", " и новые слова"),
    ],
)
def test_delta_text_survives_long_context_trim(emitted, full, delta) -> None:
    from mnema.live.asr import _delta_text

    assert _delta_text(emitted, full) == delta
    # and it does not loop: after emitting, the same front gives an empty delta
    assert _delta_text(emitted + delta, full) == ""


def test_delta_text_desync_returns_none() -> None:
    """No overlap (WLK rewrote its history) or a session-sized "delta" gives None, so the caller
    resyncs instead of emitting the whole text again."""
    from mnema.live.asr import _delta_text

    assert _delta_text("абв", "где") is None  # no overlap at all
    # a tiny accidental overlap plus a huge "tail" is out of sync too, not speech
    assert _delta_text("xyz", "z" + "ш" * 3000) is None
    assert _delta_text("что-то было", "") == ""  # an empty front (silence) is not out of sync


def test_to_chunk_resyncs_without_reemitting_on_desync() -> None:
    tr = StreamingTranscriber(label="mic")
    tr._to_chunk(_front([_seg("Привет, как дела?", 0, 1)]))
    # WLK rewrote its history (full is no continuation): nothing is emitted, resync
    c = tr._to_chunk(_front([_seg("Совсем другой текст.", 0, 2)]))
    assert c.committed == []
    # after the resync, append-only works again
    c2 = tr._to_chunk(_front([_seg("Совсем другой текст. И дальше.", 0, 3)]))
    assert [s.text.strip() for s in c2.committed] == ["И дальше."]


def test_serialize_decode_runs_one_at_a_time() -> None:
    """The wrapped process_iter of two online processors never runs concurrently: decoding on
    the shared WLK engine goes one source at a time (--system, two sessions)."""
    import threading
    import time

    from mnema.live.asr import _serialize_decode

    inside = {"count": 0, "max": 0}
    gate = threading.Barrier(2, timeout=5)

    class Online:
        def process_iter(self, *a, **k):
            inside["count"] += 1
            inside["max"] = max(inside["max"], inside["count"])
            time.sleep(0.05)  # a window for the second thread: without the lock it would enter too
            inside["count"] -= 1
            return [], 0.0

    a, b = Online(), Online()
    _serialize_decode(a)
    _serialize_decode(b)

    def worker(online) -> None:
        gate.wait()  # start together
        online.process_iter()

    threads = [threading.Thread(target=worker, args=(o,)) for o in (a, b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert inside["max"] == 1  # never overlapped


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
