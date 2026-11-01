"""WavTrackWriter: the tee track of a source; ScreenRecorder: the screen video the app writes."""

from __future__ import annotations

import time


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


class _App:
    """Mnema.app's side of the screen recorder: every message the engine sent, and the app's
    answer to it right away (None — no answer)."""

    def __init__(self, recorder_answers: dict[str, tuple[str, str, float | None] | None]) -> None:
        self.sent: list[tuple[str, str | None]] = []
        self.answers = recorder_answers
        self.recorder = None

    def send(self, op: str, path=None) -> None:
        self.sent.append((op, None if path is None else str(path)))
        answer = self.answers.get(op)
        if answer is not None and self.recorder is not None:
            self.recorder.event(*answer)


def _screen(tmp_path, answers, **kw):
    from mnema.live.record import ScreenRecorder

    app = _App(answers)
    recorder = ScreenRecorder(tmp_path / "screen.mov", app.send, **kw)
    app.recorder = recorder
    return recorder, app


def test_screen_starts_pauses_and_stops_through_the_app(tmp_path) -> None:
    """start sends the file path and waits for the first frame: offset counts from the audio
    start; pause and resume go to the app; stop waits until the app has closed the file."""
    recorder, app = _screen(
        tmp_path, {"start": ("started", "", 0.25), "stop": ("stopped", "", None)}
    )
    recorder.origin = time.monotonic() - 1.0  # the audio started a second before
    recorder.start()
    assert recorder.running and recorder.error == ""
    recorder.pause()
    recorder.resume()
    recorder.stop()
    assert app.sent == [
        ("start", str(tmp_path / "screen.mov")),
        ("pause", None),
        ("resume", None),
        ("stop", None),
    ]
    assert not recorder.running and recorder.error == ""
    assert recorder.offset is not None and 1.25 <= recorder.offset < 60


def test_screen_refused_keeps_the_reason_and_sends_nothing_more(tmp_path) -> None:
    """No permission: the reason stays for the remark, pause and stop do not reach the app."""
    recorder, app = _screen(tmp_path, {"start": ("failed", "permission", None)})
    recorder.start()
    recorder.pause()
    recorder.stop()
    assert app.sent == [("start", str(tmp_path / "screen.mov"))]
    assert recorder.error == "permission" and recorder.offset is None


def test_screen_lost_mid_recording_is_not_stopped_again(tmp_path) -> None:
    recorder, app = _screen(tmp_path, {"start": ("started", "", 0.0)})
    recorder.start()
    recorder.event("failed", "display_lost", None)
    recorder.stop()
    assert [op for op, _ in app.sent] == ["start"]
    assert recorder.error == "display_lost" and not recorder.running


def test_screen_without_free_space_does_not_start(tmp_path, monkeypatch) -> None:
    """Under 2 GiB free the video does not start: the audio has the disk."""
    import shutil
    from collections import namedtuple

    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda p: usage(10 * 2**30, 9 * 2**30, 2**31 - 1))
    recorder, app = _screen(tmp_path, {"start": ("started", "", 0.0)})
    recorder.start()
    recorder.stop()
    assert app.sent == [] and recorder.error == "no_space"


def test_screen_app_silent_is_a_writer_failure(tmp_path) -> None:
    """The app answered neither start nor stop: stop gives up after its wait, a failure."""
    recorder, app = _screen(tmp_path, {}, stop_wait=0.0)
    recorder.start()
    recorder.stop()
    assert [op for op, _ in app.sent] == ["start", "stop"]
    assert recorder.error == "writer"


def test_screen_start_does_not_wait_for_the_first_frame(tmp_path) -> None:
    """start only tells the app: the first frame comes later, and a stop while the video is still
    starting goes to the app at once and ends it without a failure."""
    recorder, app = _screen(tmp_path, {"stop": ("stopped", "", None)})
    recorder.start()
    assert recorder.running and recorder.offset is None
    recorder.stop()
    assert [op for op, _ in app.sent] == ["start", "stop"]
    assert not recorder.running and recorder.error == "" and not recorder.ended


def test_screen_stopped_in_the_app_ends_the_video_without_a_failure(tmp_path) -> None:
    """The user stopped sharing from the menu bar indicator: the app says stopped by itself;
    the video ends there, keeps its offset and is not stopped again."""
    recorder, app = _screen(tmp_path, {"start": ("started", "", 0.25)})
    recorder.origin = time.monotonic()
    recorder.start()
    recorder.event("stopped")
    recorder.pause()
    recorder.stop()
    assert [op for op, _ in app.sent] == ["start"]
    assert recorder.ended and recorder.error == "" and recorder.offset is not None


def test_screen_app_gone_at_start_is_a_writer_failure(tmp_path) -> None:
    from mnema.live.record import ScreenRecorder

    def gone(op, path=None):
        raise OSError("broken pipe")

    recorder = ScreenRecorder(tmp_path / "screen.mov", gone)
    recorder.start()
    recorder.stop()
    assert recorder.error == "writer" and not recorder.running


def test_screen_paused_while_starting_is_paused_once_it_runs(tmp_path) -> None:
    """Paused before the first frame came: the video is paused as soon as it runs, in step with
    the audio."""
    from mnema.live.record import ScreenRecorder

    sent: list[str] = []

    def app(op, path=None):
        sent.append(op)
        if op == "start":
            recorder.pause()  # the user paused while the app was starting the video
            recorder.event("started", "", 0.0)

    recorder = ScreenRecorder(tmp_path / "screen.mov", app)
    recorder.start()
    assert sent == ["start", "pause"]
