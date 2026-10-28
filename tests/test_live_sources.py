"""Live mode sources. sounddevice is mocked at the boundary (no real device)."""

from __future__ import annotations

import struct
import sys
import types

import pytest

from mnema.live.sources import MicSource


def test_micsource_available_false_without_sounddevice(monkeypatch) -> None:
    # simulate a missing package: the import fails
    monkeypatch.setitem(sys.modules, "sounddevice", None)
    assert MicSource().available() is False


def _started_mic(monkeypatch, **kwargs) -> tuple[MicSource, list]:
    """MicSource with a fake sounddevice; the test gets the stream callback to feed frames."""
    captured: list = []

    class FakeStream:
        def __init__(self, *a, callback=None, **k) -> None:
            captured.append(callback)

        def start(self) -> None: ...
        def stop(self) -> None: ...
        def close(self) -> None: ...

    fake_sd = types.SimpleNamespace(RawInputStream=FakeStream)
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)
    src = MicSource(**kwargs)
    src.start()
    return src, captured


def test_micsource_overflow_drops_oldest_and_counts(monkeypatch) -> None:
    """The queue is bounded (~30 s): on overflow the oldest frames are dropped and counted in
    dropped_seconds, so the lag does not grow forever."""
    # blocksize 10 s gives a queue of only 3 blocks: overflow without thousands of puts
    src, (cb,) = _started_mic(monkeypatch, samplerate=16000, blocksize=160000)
    frame_bytes = 160000 * 2  # PCM16: 2 bytes per sample, a frame is 10 s of audio
    frames = [bytes([i]) * frame_bytes for i in range(5)]
    for f in frames:
        cb(f, 160000, None, None)

    assert src.read() == frames[2] + frames[3] + frames[4]  # the oldest dropped, the rest at once
    assert src.dropped_seconds == 20.0  # 2 frames of 10 s


def test_micsource_stop_does_not_block_on_full_queue(monkeypatch) -> None:
    """The end sentinel does not block on a full queue (Ctrl-C would hang otherwise)."""
    src, (cb,) = _started_mic(monkeypatch, samplerate=16000, blocksize=160000)
    for i in range(3):  # fill the queue to maxsize
        cb(bytes([i]) * 4, 160000, None, None)

    src.stop()  # must not hang

    reads = [src.read() for _ in range(3)]
    assert reads[-1] is None  # the sentinel arrived (one frame made room)


# ── reopening the microphone (Bluetooth headphones died mid-call) ──


class _FlakyStream:
    """A fake RawInputStream that can be killed (finished_callback) or made unopenable (the
    device left the system)."""

    fail_open = False  # class flag: the constructor fails like PortAudio without a device

    def __init__(self, *a, callback=None, finished_callback=None, **k) -> None:
        if type(self).fail_open:
            raise RuntimeError("||PaMacCore (AUHAL)|| err=-50")
        self.cb = callback
        self.finished = finished_callback
        self.active = False
        self.stopped = True

    def start(self) -> None:
        self.active = True
        self.stopped = False

    def stop(self) -> None:
        self.active = False
        self.stopped = True

    def close(self) -> None: ...

    def die(self) -> None:
        """The device is gone: the stream stops and PortAudio calls finished_callback."""
        self.stop()
        if self.finished is not None:
            self.finished()


def _flaky_mic(monkeypatch, **kwargs):
    """MicSource on _FlakyStream with a manual clock; returns (source, clock, stream class)."""

    class Stream(_FlakyStream):  # a subclass per test so fail_open does not leak between tests
        pass

    fake_sd = types.SimpleNamespace(RawInputStream=Stream)
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)
    clock = {"t": 0.0}
    src = MicSource(clock=lambda: clock["t"], **kwargs)
    src.start()
    return src, clock, Stream


def test_micsource_survives_stream_death_and_reopens(monkeypatch) -> None:
    """A dead stream does not end the source: read() returns b'', once the device is back the
    stream reopens, frames keep coming and `notice` reports the reopening."""
    src, clock, stream_cls = _flaky_mic(monkeypatch)
    first = src._stream
    first.cb(b"\x01\x02" * 1600, 1600, None, None)
    assert src.read() == b"\x01\x02" * 1600

    stream_cls.fail_open = True  # the device is gone: reopening is impossible for now
    first.die()
    assert src.read() == b""  # alive, no data (the reopen attempt failed)

    clock["t"] = 4.0  # the interval passed; a device is back (the built-in microphone)
    stream_cls.fail_open = False
    assert src.read() == b""  # this read reopened the stream
    second = src._stream
    assert second is not first and second.active
    assert "переоткрыт" in src.notice

    second.cb(b"\x03\x04" * 1600, 1600, None, None)  # the new stream feeds the same queue
    assert src.read() == b"\x03\x04" * 1600

    src.stop()
    assert src.read() is None


def test_micsource_reopen_attempts_are_rate_limited(monkeypatch) -> None:
    """Attempts are at most every _REOPEN_INTERVAL: the device is not retried without a pause."""
    src, clock, stream_cls = _flaky_mic(monkeypatch)
    stream_cls.fail_open = True
    src._stream.die()

    assert src.read() == b""  # the first attempt is immediate
    stream_cls.fail_open = False
    assert src.read() == b""  # the clock did not move: too early, no attempt
    assert src._stream is None  # still no stream

    clock["t"] = 2.5
    assert src.read() == b""  # the interval passed: reopened
    assert src._stream is not None and src._stream.active


# ── energy chronicle (silence hallucination filter) ────────────────────────────


def _pcm(amp: int, n: int = 1600) -> bytes:
    """A PCM16 block of constant amplitude: RMS == amp (1600 samples = 100 ms at 16 kHz)."""
    return struct.pack("<h", amp) * n


def test_micsource_speech_energy_by_interval(monkeypatch) -> None:
    """Energy chronicle: quiet and loud blocks show in the RMS of their intervals, the peak over
    an interval; beyond the delivered stream it is None (nothing for the filter to decide)."""
    src, (cb,) = _started_mic(monkeypatch)
    for amp in (10, 3000, 10):  # 100 ms of silence / "speech" / silence
        cb(_pcm(amp), 1600, None, None)
    for _ in range(3):
        src.read()

    assert src.speech_energy(0.0, 0.1) == pytest.approx(10, rel=0.01)
    assert src.speech_energy(0.1, 0.2) == pytest.approx(3000, rel=0.01)
    assert src.speech_energy(0.0, 0.3) == pytest.approx(3000, rel=0.01)  # peak of the interval
    assert src.speech_energy(5.0, 6.0) is None


def test_micsource_chronicle_counts_only_delivered_stream(monkeypatch) -> None:
    """Frames dropped by backpressure do not advance the chronicle: its time matches the stream
    fed to ASR (ASR never saw those frames either)."""
    src, (cb,) = _started_mic(monkeypatch, samplerate=16000, blocksize=160000)
    # a queue of 3 blocks of 10 s; five blocks drop the two oldest (quiet) before delivery
    for amp in (10, 10, 3000, 20, 20):
        cb(struct.pack("<h", amp) * 160000, 160000, None, None)
    for _ in range(3):
        src.read()

    # the delivered stream starts with the loud block: [0, 10) is "speech", then silence
    assert src.speech_energy(0.0, 10.0) == pytest.approx(3000, rel=0.01)
    assert src.speech_energy(10.0, 30.0) == pytest.approx(20, rel=0.01)


def test_micsource_reopen_falls_back_to_default_device(monkeypatch) -> None:
    """The configured device is gone for good (headphones): reopen on the default."""
    opened: list = []

    class Stream(_FlakyStream):
        dead_device = None  # the unplugged device: opening it fails

        def __init__(self, *a, device=None, **k) -> None:
            if device is not None and device == type(self).dead_device:
                raise RuntimeError("Invalid device")
            opened.append(device)
            super().__init__(*a, **k)

    fake_sd = types.SimpleNamespace(RawInputStream=Stream)
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)
    clock = {"t": 0.0}
    src = MicSource(device="AirPods", clock=lambda: clock["t"])
    src.start()

    Stream.dead_device = "AirPods"  # the headphones died and left the system
    src._stream.die()

    assert src.read() == b""  # attempt: AirPods fails, the default opens
    assert src._stream is not None and src._stream.active
    assert opened == ["AirPods", None]  # started on the configured device, reopened on the default


def test_micsource_read_drains_all_queued_blocks(monkeypatch) -> None:
    """read() returns everything queued at once, like the system source. Regression: one 100 ms
    block per visit did not drain the mic queue while the session loop waited in the other
    source's blocking read() (up to 0.5 s of system silence), so rec --system reported false gaps
    on a complete recording."""
    src, (cb,) = _started_mic(monkeypatch, samplerate=16000, blocksize=1600)
    sys.modules["sounddevice"].query_devices = lambda *a, **k: [{"max_input_channels": 1}]
    assert src.available() is True  # a device with an input channel exists
    frame = 1600 * 2  # 100 ms of PCM16
    for i in range(5):
        cb(bytes([i]) * frame, 1600, None, None)

    assert src.read() == b"".join(bytes([i]) * frame for i in range(5))
    assert src.dropped_seconds == 0.0  # nothing lost, all delivered in one visit


def test_mic_callback_records_first_tee_error(monkeypatch):
    errors = iter([OSError("disk full"), OSError("later error")])

    def tee(pcm):
        raise next(errors)

    mic, (callback,) = _started_mic(monkeypatch, tee=tee)
    callback(b"\x10\x27", 1, None, None)
    callback(b"\x10\x27", 1, None, None)
    assert mic.recording_error == "микрофон перестал записываться"
    mic.stop()


# ── texts in both interface languages ─────────────────────────────────────────


def _cyrillic(text: str) -> bool:
    return any("Ѐ" <= ch <= "ӿ" for ch in text)


def test_micsource_notices_render_in_english(monkeypatch) -> None:
    """With an English interface the reopen and give-up notices have no Russian in them."""
    from mnema.i18n import using

    src, clock, stream_cls = _flaky_mic(monkeypatch)
    src._stream.stop()
    with using("en"):
        assert src.read() == b""
        clock["t"] = 2.5
        assert src.read() == b""
        assert src.notice == "[microphone reopened: default device, some speech may be missing]"
        src._stream.stop()
        clock["t"] = 5.0
        src.read()
        clock["t"] = 7.5
        src.read()
        assert src.notice.startswith("[microphone reopened ×2: ")
        stream_cls.fail_open = True
        src._stream.stop()
        clock["t"] = 10.0
        src.read()
        clock["t"] = 100.0
        assert src.read() is None
    assert not _cyrillic(src.notice) and "did not come back" in src.notice


@pytest.mark.parametrize(
    ("system", "in_app"), [("Linux", False), ("Darwin", True), ("Darwin", False)]
)
def test_mic_missing_text_in_both_languages(monkeypatch, system, in_app) -> None:
    from mnema.i18n import using
    from mnema.live import sources, system_audio

    monkeypatch.setattr(sources.platform, "system", lambda: system)
    monkeypatch.setattr(system_audio, "in_mnema_app", lambda: in_app)
    with using("en"):
        english = sources.mic_missing_text()
    assert english.startswith("Microphone not found") and not _cyrillic(english)
    russian = sources.mic_missing_text()
    expected = {
        ("Linux", False): "Микрофон не найден. Подключите его и повторите",
        ("Darwin", True): "Микрофон не найден. Подключите его или разрешите Mnema доступ к "
        "микрофону в Системных настройках",
        ("Darwin", False): "Микрофон не найден. Подключите его или разрешите терминалу доступ к "
        "микрофону в Системных настройках",
    }
    assert russian == expected[(system, in_app)]
