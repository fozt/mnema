"""The recording's screen video and its sound become one video.mp4 when it is processed."""

import subprocess
from pathlib import Path

import pytest

from mnema.live import meta, video
from mnema.live.naming import WAV_HEADER

SOUND = b"\0" * (WAV_HEADER + 3200)


@pytest.fixture
def ffmpeg(monkeypatch):
    """ffmpeg at its process boundary (subprocess.run, or Popen when the caller can stop it):
    the commands it got; an answer writes the output file (the last argument) or fails with
    exit code 1 and stderr."""
    monkeypatch.setenv("MNEMA_FFMPEG", "/opt/ffmpeg")
    calls: list[list[str]] = []
    fail: list[str] = []

    def answer(cmd) -> tuple[int, str]:
        calls.append(cmd)
        if fail:
            Path(cmd[-1]).write_bytes(b"half")
            return 1, fail[0]
        Path(cmd[-1]).write_bytes(b"mp4 with sound")
        return 0, ""

    def run(cmd, **kwargs):
        code, err = answer(cmd)
        return subprocess.CompletedProcess(cmd, code, "", err)

    class Ffmpeg:
        def __init__(self, cmd, **kwargs) -> None:
            self.cmd, self.returncode = cmd, None

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> None:
            return None

        def communicate(self, timeout=None):
            self.returncode, err = answer(self.cmd)
            return "", err

        def poll(self):
            return self.returncode

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "Popen", Ffmpeg)
    return calls, fail


def folder(tmp_path, *tracks: str) -> Path:
    base = tmp_path / "2026-11-01_10-00"
    base.mkdir()
    (base / "screen.mov").write_bytes(b"hevc frames")
    for label in tracks:
        (base / f"{label}.wav").write_bytes(SOUND)
    return base


def run_with(offset: float | None) -> meta.Run:
    return meta.Run(video=meta.VideoRun(offset=offset))


def test_the_screen_and_both_tracks_become_one_video(tmp_path, ffmpeg):
    """Both tracks are mixed into AAC next to the copied video, which starts offset seconds
    into the sound; the result replaces screen.mov, with nothing left half-written."""
    calls, _ = ffmpeg
    base = folder(tmp_path, "mic", "system")
    seen = []
    run = run_with(0.4)
    video.finish(base, run, on=lambda frac, msg: seen.append((frac, msg)))
    assert sorted(p.name for p in base.iterdir()) == ["mic.wav", "system.wav", "video.mp4"]
    assert (base / "video.mp4").read_bytes() == b"mp4 with sound"
    assert run.video == meta.VideoRun(offset=0.4) and not run.notes and not run.remarks
    assert (None, "Собираю видео…") in seen
    [cmd] = calls
    assert cmd[0] == "/opt/ffmpeg"
    screen = cmd.index(str(base / "screen.mov"))
    assert cmd[screen - 3 : screen] == ["-itsoffset", "0.4", "-i"]
    assert str(base / "mic.wav") in cmd and str(base / "system.wav") in cmd
    mix = cmd[cmd.index("-filter_complex") + 1]
    assert "amix=inputs=2" in mix and "normalize=0" in mix and mix.endswith("[a]")
    tail = " ".join(cmd)
    for part in (
        "-map 0:v -map [a]",
        "-c:v copy -tag:v hvc1",  # QuickTime plays HEVC in mp4 only as hvc1
        "-c:a aac",
        "-b:a 96k",
        "-movflags +faststart",
    ):
        assert part in tail
    assert "-shortest" not in cmd  # the whole sound stays: the video ends a moment before it


@pytest.mark.parametrize("tracks", [("mic",), ("system",)])
def test_one_track_is_the_sound_of_the_video_as_is(tmp_path, ffmpeg, tracks):
    calls, _ = ffmpeg
    base = folder(tmp_path, *tracks)
    (base / "mic.wav" if tracks == ("system",) else base / "system.wav").write_bytes(
        b"\0" * WAV_HEADER
    )  # the other track has no sound at all
    video.finish(base, run_with(None))
    [cmd] = calls
    assert "-itsoffset" not in cmd and "-filter_complex" not in cmd
    assert cmd.count("-i") == 2 and str(base / f"{tracks[0]}.wav") in cmd
    assert "-map 0:v -map 1:a -c:v copy -tag:v hvc1" in " ".join(cmd)
    assert (base / "video.mp4").exists() and not (base / "screen.mov").exists()


def test_without_sound_the_video_is_the_picture_alone(tmp_path, ffmpeg):
    calls, _ = ffmpeg
    base = folder(tmp_path)
    video.finish(base, run_with(1.5))
    [cmd] = calls
    assert cmd.count("-i") == 1 and "-c:a" not in cmd
    assert cmd.count("-map") == 1 and "-map 0:v -c:v copy -tag:v hvc1" in " ".join(cmd)
    assert (base / "video.mp4").exists() and not (base / "screen.mov").exists()


def test_a_failed_assembly_keeps_the_screen_recording_and_says_so(tmp_path, ffmpeg):
    calls, fail = ffmpeg
    fail.append("Invalid data found when processing input")
    base = folder(tmp_path, "mic")
    (base / "video.mp4").write_bytes(b"earlier video")
    run = run_with(0.2)
    video.finish(base, run)
    assert len(calls) == 1
    assert sorted(p.name for p in base.iterdir()) == ["mic.wav", "screen.mov", "video.mp4"]
    assert (base / "screen.mov").read_bytes() == b"hevc frames"
    assert (base / "video.mp4").read_bytes() == b"earlier video"
    assert run.video is not None and run.video.offset == 0.2 and run.video.error
    [note] = run.notes
    assert note.code == "video_mux_failed" and run.remarks == [note.text]
    assert "screen.mov" in note.text


def test_no_screen_recording_means_nothing_to_assemble(tmp_path, ffmpeg):
    calls, _ = ffmpeg
    base = tmp_path / "2026-11-01_10-00"
    base.mkdir()
    (base / "mic.wav").write_bytes(SOUND)
    run = meta.Run()
    video.finish(base, run)
    (base / "screen.mov").write_bytes(b"")  # the capture never wrote a frame
    video.finish(base, run)
    assert calls == [] and run == meta.Run()
    assert sorted(p.name for p in base.iterdir()) == ["mic.wav"]


def test_a_crash_leftover_goes_at_the_next_assembly(tmp_path, ffmpeg):
    """A temporary file from an assembly cut short (quit, crash) is removed before the next."""
    base = folder(tmp_path, "mic")
    (base / ".mnema-video.mp4").write_bytes(b"half")
    video.finish(base, run_with(None))
    assert sorted(p.name for p in base.iterdir()) == ["mic.wav", "video.mp4"]


def test_screen_recording_that_cannot_be_removed_is_no_failure(tmp_path, ffmpeg, monkeypatch):
    """video.mp4 is whole: a screen.mov the disk would not remove is only a log line."""
    base = folder(tmp_path, "mic")
    unlink = Path.unlink

    def stuck(self, missing_ok=False):
        if self.name == "screen.mov":
            raise PermissionError("operation not permitted")
        unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", stuck)
    run = run_with(0.1)
    video.finish(base, run)
    assert (base / "video.mp4").read_bytes() == b"mp4 with sound"
    assert run.video == meta.VideoRun(offset=0.1) and not run.notes


def test_assembling_again_drops_the_earlier_failure_and_keeps_the_capture_one(tmp_path, ffmpeg):
    """A retried assembly: its earlier remark goes; the capture's own failure (the screen was
    disconnected) stays, whatever the assembly does."""
    _, fail = ffmpeg
    base = folder(tmp_path, "mic")
    fail.append("Invalid data found when processing input")
    run = meta.Run(video=meta.VideoRun(offset=0.3, error="display_lost"))
    video.finish(base, run)
    assert run.video is not None and run.video.error == "display_lost"
    assert [n.code for n in run.notes] == ["video_mux_failed"] and len(run.remarks) == 1
    fail.clear()
    video.finish(base, run)
    assert run.video.error == "display_lost" and not run.notes and not run.remarks
    assert (base / "video.mp4").exists() and not (base / "screen.mov").exists()


def test_a_later_processing_assembles_with_the_offset_the_recording_left(tmp_path, ffmpeg):
    """screen.mov left by a quit: the history has the offset; assembling there writes what
    happened back, so every later processing gets it."""
    calls, fail = ffmpeg
    base = folder(tmp_path, "mic")
    video.keep(base, run_with(0.7))
    fail.append("moov atom not found")
    video.assemble(base)
    kept = meta.recorded_video(base)
    assert kept is not None and kept.offset == 0.7 and kept.error == "moov atom not found"
    assert [n.code for n in kept.notes] == ["video_mux_failed"]
    fail.clear()
    video.assemble(base)
    kept = meta.recorded_video(base)
    assert kept == meta.RecordedVideo(offset=0.7)
    assert all(c[c.index("-itsoffset") + 1] == "0.7" for c in calls)
    assert (base / "video.mp4").exists() and not video.pending(base)


def test_a_stopped_assembly_kills_ffmpeg_and_leaves_the_screen_recording(tmp_path, monkeypatch):
    """Processing stopped (on raises) while ffmpeg runs: it is stopped, the half-written file
    goes, screen.mov stays for the next processing and the stop goes on to the caller."""
    monkeypatch.setenv("MNEMA_FFMPEG", "/opt/ffmpeg")
    stopped: list[str] = []

    class Ffmpeg:
        def __init__(self, cmd, **kwargs) -> None:
            self.cmd, self.returncode = cmd, None
            Path(cmd[-1]).write_bytes(b"half")

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> None:
            return None

        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired(self.cmd, timeout or 0)  # still writing

        def poll(self):
            return self.returncode

        def terminate(self) -> None:
            stopped.append("terminate")
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

    class Stop(BaseException):
        pass

    monkeypatch.setattr(subprocess, "Popen", Ffmpeg)
    base = folder(tmp_path, "mic")
    ticks: list[str] = []

    def on(fraction, message) -> None:
        ticks.append(message)
        if len(ticks) > 3:  # a few ticks of a running ffmpeg, then "Stop processing"
            raise Stop

    with pytest.raises(Stop):
        video.assemble(base, run_with(0.4), on)
    assert stopped == ["terminate"]
    assert sorted(p.name for p in base.iterdir()) == ["mic.wav", "screen.mov"]
