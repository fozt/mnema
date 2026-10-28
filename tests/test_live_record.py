"""WavTrackWriter: the tee track of a source."""

from __future__ import annotations


def test_wav_track_writer_writes_valid_wav_with_all_fed_bytes(tmp_path) -> None:
    """The track is a valid PCM16 mono 16 kHz WAV whose length is exactly the bytes written."""
    import wave

    from mnema.live.record import WavTrackWriter

    w = WavTrackWriter(tmp_path / "live-x.mic.wav")
    w.start()
    w.write(b"\x01\x02" * 1600)  # 100 ms
    w.write(b"\x03\x04" * 800)  # 50 ms more
    w.stop()

    with wave.open(str(tmp_path / "live-x.mic.wav"), "rb") as r:
        assert (r.getnchannels(), r.getsampwidth(), r.getframerate()) == (1, 2, 16000)
        assert r.getnframes() == 2400
        assert r.readframes(2400) == b"\x01\x02" * 1600 + b"\x03\x04" * 800


def test_wav_track_writer_ignores_writes_outside_lifecycle(tmp_path) -> None:
    """write() before start() and after stop() is ignored (the capture callback may outlive it)."""
    import wave

    from mnema.live.record import WavTrackWriter

    w = WavTrackWriter(tmp_path / "t.wav")
    w.write(b"\x00\x00" * 100)  # before start there is no file yet
    assert not (tmp_path / "t.wav").exists()
    w.start()
    w.write(b"\x05\x06" * 100)
    w.stop()
    w.write(b"\x00\x00" * 100)  # after stop the length does not change
    w.stop()  # a second stop is safe

    with wave.open(str(tmp_path / "t.wav"), "rb") as r:
        assert r.getnframes() == 100


def test_write_failure_remains_visible_after_close(monkeypatch, tmp_path) -> None:
    """ENOSPC on the audio thread is kept: a successful close does not clear the first error."""
    import errno
    from unittest.mock import Mock

    from mnema.live.record import WavTrackWriter

    disk = Mock()
    disk.writeframes.side_effect = [None, OSError(errno.ENOSPC, "disk full")]
    monkeypatch.setattr("mnema.live.record.wave.open", lambda *a: disk)
    writer = WavTrackWriter(tmp_path / "mic.wav")
    writer.start()
    writer.write(b"\x01\x02" * 800)
    writer.write(b"\x03\x04" * 800)
    writer.write(b"\x05\x06" * 800)
    writer.stop()
    assert writer.error == "микрофон перестал записываться"
    assert writer.written == 1600 and disk.writeframes.call_count == 2
    assert not writer.full
    disk.close.assert_called_once()


def test_close_failure_is_reported(monkeypatch, tmp_path) -> None:
    from unittest.mock import Mock

    from mnema.live.record import WavTrackWriter

    disk = Mock()
    disk.close.side_effect = OSError("header write failed")
    monkeypatch.setattr("mnema.live.record.wave.open", lambda *a: disk)
    writer = WavTrackWriter(tmp_path / "system.wav")
    writer.start()
    writer.stop()
    assert writer.error == "файлы записи не сохранились до конца"
    writer.stop()
    disk.close.assert_called_once()
