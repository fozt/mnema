"""The recording's screen video and its sound become one video.mp4 right after the recording."""

import subprocess
from pathlib import Path

import pytest

from mnema.live import meta, video
from mnema.live.naming import WAV_HEADER

SOUND = b"\0" * (WAV_HEADER + 3200)


@pytest.fixture
def ffmpeg(monkeypatch):
    """ffmpeg at its process boundary: the commands it got; an answer writes the output file
    (the last argument) or fails with exit code 1 and stderr."""
    monkeypatch.setenv("MNEMA_FFMPEG", "/opt/ffmpeg")
    calls: list[list[str]] = []
    fail: list[str] = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if fail:
            Path(cmd[-1]).write_bytes(b"half")
            return subprocess.CompletedProcess(cmd, 1, "", fail[0])
        Path(cmd[-1]).write_bytes(b"mp4 with sound")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
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
    assert "amix=inputs=2" in mix and "normalize=0" in mix
    tail = " ".join(cmd)
    for part in ("-c:v copy", "-c:a aac", "-b:a 96k", "-movflags +faststart", "-shortest"):
        assert part in tail


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
    assert (base / "video.mp4").exists() and not (base / "screen.mov").exists()


def test_without_sound_the_video_is_the_picture_alone(tmp_path, ffmpeg):
    calls, _ = ffmpeg
    base = folder(tmp_path)
    video.finish(base, run_with(1.5))
    [cmd] = calls
    assert cmd.count("-i") == 1 and "-c:a" not in cmd
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
