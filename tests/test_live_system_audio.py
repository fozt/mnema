"""System audio: PCM conversion checked by numbers, the _BufferedSource base on its queues.

Real capture (a Core Audio tap) is an external boundary and is not called (CI runs Linux with
neither CoreAudio nor permissions); numpy tests skip without numpy, since CI installs the core
without extras and the module must still import there. Real capture is checked by hand on a Mac.
"""

from __future__ import annotations

import struct
import sys
import types

import pytest

from mnema.live.system_audio import CoreAudioTapSource, to_pcm16_mono


def _f32(*vals: float) -> bytes:
    import numpy as np

    return np.array(vals, dtype=np.float32).tobytes()


def _i16(data: bytes | None) -> list[int]:
    import numpy as np

    assert data is not None
    return np.frombuffer(data, dtype=np.int16).tolist()


# ── conversion (pure function, checked by numbers) ─────────────────────────────


@pytest.mark.parametrize(
    ("samples", "planar"),
    [
        ([0.25] * 6 + [0.75] * 6, True),  # planar: channel 0 (0.25), then channel 1 (0.75)
        ([0.25, 0.75] * 6, False),  # the same channels interleaved
    ],
)
def test_convert_planar_stereo_48k_to_mono_16k(samples, planar) -> None:
    pytest.importorskip("numpy")
    # mono 0.5; 48k -> 16k averages triples
    out = to_pcm16_mono(_f32(*samples), rate=48000, channels=2, planar=planar)
    assert _i16(out) == [16384, 16384]  # 0.5 * 32768


def test_convert_clips_out_of_range_and_scales() -> None:
    pytest.importorskip("numpy")
    out = to_pcm16_mono(_f32(1.5, -1.5, 0.25, -0.25), rate=16000, channels=1, planar=False)
    assert _i16(out) == [32767, -32768, 8192, -8192]


def test_convert_non_integer_factor_interpolates() -> None:
    pytest.importorskip("numpy")
    import numpy as np

    ramp = np.linspace(-0.5, 0.5, 441, dtype=np.float32)
    out = to_pcm16_mono(ramp.tobytes(), rate=44100, channels=1, planar=False)
    vals = _i16(out)
    assert len(vals) == round(441 * 16000 / 44100)  # 160 samples
    assert vals == sorted(vals)  # linear interpolation of a ramp is monotonic
    assert vals[0] == -16384  # the ends are kept: -0.5 * 32768


def test_convert_empty_is_empty() -> None:
    pytest.importorskip("numpy")
    assert to_pcm16_mono(b"", rate=48000, channels=2, planar=True) == b""


# ── source contract ─────────────────────────────────────────────────────────


def test_available_requires_macos_14_2(monkeypatch) -> None:
    """CATap appeared in macOS 14.2: older is False, and so is anything but a Mac."""
    monkeypatch.setattr("platform.system", lambda: "Linux")
    assert CoreAudioTapSource().available() is False
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setitem(sys.modules, "objc", types.SimpleNamespace())
    monkeypatch.setattr("platform.mac_ver", lambda: ("13.6", ("", "", ""), "arm64"))
    assert CoreAudioTapSource().available() is False
    monkeypatch.setattr("platform.mac_ver", lambda: ("14.1", ("", "", ""), "arm64"))
    assert CoreAudioTapSource().available() is False
    monkeypatch.setattr("platform.mac_ver", lambda: ("14.2", ("", "", ""), "arm64"))
    assert CoreAudioTapSource().available() is True
    monkeypatch.setattr("platform.mac_ver", lambda: ("26.6.2", ("", "", ""), "arm64"))
    assert CoreAudioTapSource().available() is True


def test_system_source_read_drains_all_queued_frames() -> None:
    """Tap frames are ~10-20 ms, so read() returns everything queued at once. Regression: one
    frame per visit did not drain the queue while the session loop shared turns with mic (100 ms
    per visit), dropping up to 80% of system audio before ASR."""
    src = CoreAudioTapSource()
    for pcm in (b"aa", b"bb", b"cc"):
        src._put_dropping_oldest(pcm)
    assert src.read() == b"aabbcc"
    # sentinel mid-drain: what was queued is delivered, the next read returns None
    src._put_dropping_oldest(b"dd")
    src._put_dropping_oldest(None)
    assert src.read() == b"dd"
    assert src.read() is None


def test_system_source_speech_energy_by_interval() -> None:
    """The energy chronicle is kept per delivered frame in read(); time is seconds since the first
    delivered frame (the same as the stream fed to ASR)."""
    src = CoreAudioTapSource()
    quiet = struct.pack("<h", 10) * 320  # 20 ms at 16 kHz
    loud = struct.pack("<h", 3000) * 320
    src._put_dropping_oldest(quiet)
    src._put_dropping_oldest(loud)
    assert src.read() == quiet + loud

    assert src.speech_energy(0.0, 0.02) == pytest.approx(10, rel=0.01)
    assert src.speech_energy(0.02, 0.04) == pytest.approx(3000, rel=0.01)
    assert src.speech_energy(1.0, 2.0) is None  # beyond the delivered stream


# ── texts in both interface languages ─────────────────────────────────────────


def _cyrillic(text: str) -> bool:
    return any("Ѐ" <= ch <= "ӿ" for ch in text)


@pytest.mark.parametrize("in_app", [True, False])
def test_system_audio_denied_text_in_both_languages(monkeypatch, in_app) -> None:
    from mnema.i18n import using
    from mnema.live import system_audio

    monkeypatch.setattr(system_audio, "in_mnema_app", lambda: in_app)
    with using("en"):
        english = system_audio.system_audio_denied_text()
    assert "Privacy & Security" in english and not _cyrillic(english)
    who, again = ("Mnema", "Mnema") if in_app else ("терминалу", "терминал")
    assert system_audio.system_audio_denied_text() == (
        f"Нет доступа к системному звуку. Разрешите {who} запись системного звука в Системных "
        f"настройках, раздел «Конфиденциальность и безопасность», и перезапустите {again}"
    )


@pytest.mark.parametrize("system", ["Linux", "Darwin"])
def test_system_audio_hints_render_in_english(monkeypatch, system) -> None:
    from mnema.i18n import _, using
    from mnema.live import system_audio

    monkeypatch.setattr(system_audio.platform, "system", lambda: system)
    with using("en"):
        texts = [system_audio.system_unavailable_hint(), _(system_audio.SYSTEM_AUDIO_FAILED)]
    assert all(text and not _cyrillic(text) for text in texts)
    assert _(system_audio.SYSTEM_AUDIO_FAILED) == (
        "Не удалось записать системный звук. Подробности в журнале Mnema"
    )
