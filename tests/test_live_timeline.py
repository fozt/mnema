"""Every track of a recording starts at the session's origin and follows real time. Core Audio,
sounddevice and parec are replaced at the boundary; the WAV files are real."""

from __future__ import annotations

import ctypes
import io
import struct
import sys
import types
import wave

import numpy as np
import pytest

from mnema.live import session as session_mod
from mnema.live.models import LiveChunk
from mnema.live.record import WavTrackWriter
from mnema.live.session import LiveSession
from mnema.live.sources import MicSource
from mnema.live.system_audio import CoreAudioTapSource, PulseMonitorSource, valid_times

BLOCK_SIZES = (480, 512, 1024, 470)
SAMPLE_VALID, HOST_VALID = 1, 2


def _tap(rate: float = 48000.0, **kwargs) -> tuple[CoreAudioTapSource, list[bytes]]:
    written: list[bytes] = []
    src = CoreAudioTapSource(tee=written.append, **kwargs)
    src.set_format(rate, 2)
    return src, written


def _block(frames: int, value: float = 0.5) -> bytes:
    return np.full(frames, value, dtype=np.float32).tobytes()


def _samples(written: list[bytes]) -> np.ndarray:
    return np.frombuffer(b"".join(written), dtype=np.int16)


@pytest.mark.parametrize("rate", [48000.0, 44100.0, 96000.0, 32000.0, 24000.0, 22050.0, 16000.0])
@pytest.mark.parametrize("sizes", [(s,) for s in BLOCK_SIZES] + [BLOCK_SIZES, (1, 2, 3, 7, 4099)])
def test_output_length_is_exact_for_any_blocks(rate: float, sizes: tuple[int, ...]) -> None:
    src, written = _tap(rate)
    stream = list(sizes) * (int(rate * 2) // sum(sizes))  # two seconds of audio
    at = 0.0
    for frames in stream:
        src.on_input(_block(frames), at, None)
        at += frames

    assert len(_samples(written)) == int(at * 16000 / rate)


@pytest.mark.parametrize("rate", [48000.0, 44100.0])
def test_a_long_stream_stays_exact(rate: float) -> None:
    """Ten minutes on, in blocks that do not divide evenly, the count is still exact."""
    src, written = _tap(rate)
    block = 1021
    silence = _block(block, 0.0)
    src.on_input(silence, 0.0, None)
    total = block + 600 * int(rate)  # lost frames go through the resampler like audio
    for _ in range(100):
        src.on_input(silence, float(total), None)
        total += block

    assert sum(map(len, written)) // 2 == int(total * 16000 / rate)


@pytest.mark.parametrize("rate", [48000.0, 44100.0])
def test_blocks_do_not_change_the_sound(rate: float) -> None:
    n = int(rate)
    t = np.arange(n) / rate
    tone = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)

    whole, whole_written = _tap(rate)
    whole.on_input(tone.tobytes(), 0.0, None)
    cut, cut_written = _tap(rate)
    at = 0
    sizes = iter(BLOCK_SIZES * n)
    while at < n:
        size = next(sizes)
        cut.on_input(tone[at : at + size].tobytes(), float(at), None)
        at += size

    assert (
        np.abs(_samples(whole_written).astype(int) - _samples(cut_written).astype(int)).max() <= 1
    )


def test_lost_frames_become_silence_at_their_place() -> None:
    src, written = _tap()
    src.on_input(_block(480), 1000.0, None)
    src.on_input(_block(480), 1480.0 + 4800, None)  # 100 ms lost
    src.on_input(_block(480), 1480.0 + 4800 + 480, None)

    samples = _samples(written)
    assert len(samples) == 160 + 1600 + 160 + 160
    assert (samples[:160] == 16384).all()
    assert (samples[160:1760] == 0).all()
    assert (samples[1760:] == 16384).all()


@pytest.mark.parametrize("rate", [48000.0, 44100.0])
def test_track_keeps_length_of_the_full_stream_when_frames_are_lost(rate: float) -> None:
    src, written = _tap(rate)
    at = 0.0
    for i in range(200):
        size = BLOCK_SIZES[i % 4]
        if i % 50 != 7:  # one block in fifty never reaches Python
            src.on_input(_block(size), at, None)
        at += size

    assert len(_samples(written)) == int(at * 16000 / rate)


def test_a_long_gap_is_all_silence() -> None:
    src, written = _tap()
    src.on_input(_block(480), 0.0, None)
    src.on_input(_block(480), 480.0 + 48000 * 3.5, None)

    assert len(_samples(written)) == 160 + 56000 + 160


def test_stamps_without_a_valid_sample_time_add_nothing() -> None:
    src, written = _tap()
    for _ in range(10):
        src.on_input(_block(480), None, None)

    assert len(_samples(written)) == 1600


def test_a_gap_is_measured_after_stamps_were_missing() -> None:
    src, written = _tap()
    src.on_input(_block(480), 0.0, None)
    src.on_input(_block(480), None, None)  # counted without a stamp
    src.on_input(_block(480), 960.0, None)

    assert len(_samples(written)) == 480


def test_a_counter_that_went_back_is_no_loss_and_catches_up_with_the_clock() -> None:
    """The device restarted: its sample counter starts over, the time it was away is real."""
    now = {"t": 10.02}
    src, written = _tap(clock=lambda: now["t"])
    src.origin = 10.0
    src.on_input(_block(960), 500000.0, None)  # 0.00 .. 0.02
    now["t"] = 12.02
    src.on_input(_block(960), 0.0, None)  # the device came back, this block began at 12.00
    now["t"] = 12.04
    src.on_input(_block(960), 960.0, None)

    samples = _samples(written)
    assert len(samples) == int(2.04 * 16000)
    assert (samples[320 : 2 * 16000] == 0).all()


def test_first_block_delay_from_the_host_clock() -> None:
    src, written = _tap()
    src.timebase = (125, 3)  # Apple silicon: 24 MHz ticks
    src.origin = 100.0
    src.on_input(_block(480), 0.0, 2_408_400_000)  # 100.35 s in ticks

    samples = _samples(written)
    assert len(samples) == 5600 + 160
    assert (samples[:5600] == 0).all()
    assert (samples[5600:] == 16384).all()


def test_first_block_delay_counts_only_once() -> None:
    src, written = _tap()
    src.timebase = (1, 1)
    src.origin = 100.0
    src.on_input(_block(480), 0.0, 100_500_000_000)
    src.on_input(_block(480), 480.0, 200_000_000_000)

    assert len(_samples(written)) == 8000 + 320


def test_first_block_without_a_host_time_uses_the_clock() -> None:
    src, written = _tap(clock=lambda: 100.52)
    src.origin = 100.0
    src.on_input(_block(960), 0.0, None)  # 20 ms long, so it began at 100.5

    assert len(_samples(written)) == 8000 + 320


def test_a_block_before_the_origin_adds_no_silence() -> None:
    src, written = _tap()
    src.timebase = (1, 1)
    src.origin = 100.0
    src.on_input(_block(480), 0.0, 99_000_000_000)

    assert len(_samples(written)) == 160


def test_silence_reaches_the_live_stream_too() -> None:
    src, written = _tap()
    src.timebase = (1, 1)
    src.origin = 100.0
    src.on_input(_block(480), 0.0, 100_100_000_000)
    src.on_input(_block(480), 480.0 + 480, None)

    assert src.read() == b"".join(written)


def test_valid_times_follow_the_flags() -> None:
    assert valid_times(SAMPLE_VALID | HOST_VALID, 5.0, 7) == (5.0, 7)
    assert valid_times(SAMPLE_VALID, 5.0, 7) == (5.0, None)
    assert valid_times(HOST_VALID, 5.0, 7) == (None, 7)
    assert valid_times(0, 5.0, 7) == (None, None)


class _CoreAudio:
    """What the IOProc is handed to; `proc` is the C callback Core Audio would call."""

    def __init__(self) -> None:
        self.proc = None
        self.AudioDeviceCreateIOProcID = self._Create(self)

    class _Create:
        argtypes = None

        def __init__(self, owner: _CoreAudio) -> None:
            self.owner = owner

        def __call__(self, device, proc, client, proc_id) -> int:
            self.owner.proc = proc
            proc_id._obj.value = 1
            return 0


def _call_ioproc(proc, data: bytes, stamp: bytes | None, channels: int = 1) -> int:
    """Calls the C callback with the memory Core Audio gives it: AudioBufferList, AudioTimeStamp."""
    samples = ctypes.create_string_buffer(data, len(data))
    head = struct.pack("<I4xIIQ", 1, channels, len(data), ctypes.addressof(samples))
    buffers = ctypes.create_string_buffer(head + bytes(16 * 7), 8 + 16 * 8)
    stamps = ctypes.create_string_buffer(stamp, 64) if stamp is not None else None
    in_data = ctypes.cast(buffers, proc.argtypes[2])
    in_time = ctypes.cast(stamps, proc.argtypes[3]) if stamps is not None else None
    return proc(1, None, in_data, in_time, None, None, None)


def _stamp(sample_time: float, host_time: int, flags: int) -> bytes:
    return struct.pack("<dQdQ24xII", sample_time, host_time, 1.0, 0, flags, 0)


def _installed() -> tuple[CoreAudioTapSource, list[bytes], object]:
    src, written = _tap()
    ca = _CoreAudio()
    src._install_ioproc(ca, 1)
    return src, written, ca.proc


def test_ioproc_reads_the_time_stamp_of_the_block() -> None:
    src, written, proc = _installed()
    src.timebase = (1, 1)
    src.origin = 100.0
    both = SAMPLE_VALID | HOST_VALID

    assert _call_ioproc(proc, _block(480), _stamp(0.0, 100_100_000_000, both)) == 0
    assert _call_ioproc(proc, _block(480), _stamp(480.0 + 480, 0, SAMPLE_VALID)) == 0

    assert len(_samples(written)) == 1600 + 160 + 160 + 160


def test_ioproc_ignores_fields_not_marked_valid() -> None:
    src, written, proc = _installed()
    src.timebase = (1, 1)
    src.origin = None

    _call_ioproc(proc, _block(480), _stamp(0.0, 0, 0))
    _call_ioproc(proc, _block(480), _stamp(99999.0, 0, 0))

    assert len(_samples(written)) == 320


def test_ioproc_works_without_a_time_stamp() -> None:
    src, written, proc = _installed()

    assert _call_ioproc(proc, _block(480), None) == 0

    assert len(_samples(written)) == 160


def test_ioproc_never_raises_into_core_audio() -> None:
    src, written, proc = _installed()

    assert _call_ioproc(proc, b"\x00\x01\x02", _stamp(0.0, 0, SAMPLE_VALID)) == 0

    assert written == []


class _Transcriber:
    def feed(self, pcm: bytes) -> None: ...

    def poll(self) -> LiveChunk:
        return LiveChunk()

    def finish(self) -> LiveChunk:
        return LiveChunk()


def _frozen_audio_start(monkeypatch, at: float) -> None:
    monkeypatch.setattr(session_mod, "time", types.SimpleNamespace(monotonic=lambda: at))


def _fake_sounddevice(monkeypatch, blocks: int) -> None:
    class Stream:
        def __init__(self, *a, callback=None, **k) -> None:
            self.callback = callback
            self.stopped = False
            self.active = True

        def start(self) -> None:
            for _ in range(blocks):
                self.callback(b"\x01\x02" * 1600, 1600, None, None)

        def stop(self) -> None: ...
        def close(self) -> None: ...

    monkeypatch.setitem(sys.modules, "sounddevice", types.SimpleNamespace(RawInputStream=Stream))


class _StartedTap(CoreAudioTapSource):
    """A tap whose stream begins `delay` seconds after the origin, without Core Audio."""

    delay = 0.0
    blocks = 0

    def start(self) -> None:
        assert self.origin is not None
        self.timebase = (1, 1)
        began = self.origin + self.delay
        at = 0.0
        for _ in range(self.blocks):
            self.on_input(_block(960), at, round(began * 1e9))
            at += 960

    def stop(self) -> None:
        self._stopped = True
        self._put_dropping_oldest(None)


def _wav_frames(path) -> tuple[int, np.ndarray]:
    with wave.open(str(path), "rb") as w:
        return w.getnframes(), np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def test_tracks_of_a_session_begin_together_and_end_together(monkeypatch, tmp_path) -> None:
    """Both sources stop at origin + 1 s; each began at its own moment after the origin."""
    _frozen_audio_start(monkeypatch, 100.0)
    _fake_sounddevice(monkeypatch, blocks=5)
    mic_track = WavTrackWriter(tmp_path / "mic.wav")
    system_track = WavTrackWriter(tmp_path / "system.wav")
    # the first microphone block arrives at 100.6: it began half a second after the origin
    mic = MicSource(label="mic", tee=mic_track.write, clock=lambda: 100.6)
    tap = _StartedTap(label="system", tee=system_track.write)
    tap.set_format(48000.0, 2)
    tap.delay, tap.blocks = 0.3, 35  # 700 ms of audio

    session = LiveSession(
        [mic, tap],
        lambda s: _Transcriber(),
        recorder=[mic_track, system_track],
        on_started=lambda: session.stop(),
    )
    session.run()

    mic_frames, mic_pcm = _wav_frames(tmp_path / "mic.wav")
    system_frames, system_pcm = _wav_frames(tmp_path / "system.wav")
    assert mic_frames == system_frames == 16000
    assert (mic_pcm[:8000] == 0).all() and mic_pcm[8000] != 0
    assert (system_pcm[:4800] == 0).all() and system_pcm[4800] != 0


def test_parec_track_begins_at_the_origin(monkeypatch) -> None:
    import mnema.live.system_audio as sa

    pcm = b"\x01\x02" * 1600  # 100 ms
    proc = types.SimpleNamespace(
        stdout=io.BytesIO(pcm * 2), terminate=lambda: None, wait=lambda timeout=None: 0
    )
    sink = types.SimpleNamespace(returncode=0, stdout="output\n", stderr="")
    monkeypatch.setattr(sa.subprocess, "run", lambda *a, **k: sink)
    monkeypatch.setattr(sa.subprocess, "Popen", lambda *a, **k: proc)
    written: list[bytes] = []
    src = PulseMonitorSource(tee=written.append, clock=lambda: 50.35)
    src.origin = 50.0

    src.start()
    while src.read() is not None:
        pass

    assert b"".join(written) == bytes(4000 * 2) + pcm * 2  # began at 50.25


def test_sources_without_an_origin_keep_their_stream() -> None:
    src, written = _tap()
    src.on_input(_block(480), 0.0, 10**15)

    assert len(_samples(written)) == 160
