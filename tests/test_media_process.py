"""ffmpeg cancellation, checked with real processes and no media or system ffmpeg."""

from __future__ import annotations

import signal
import subprocess
import sys
from pathlib import Path

import pytest

from mnema.core import audio, base, video
from mnema.live.setup import FinalizeCancelled


@pytest.fixture
def ffmpeg_process(monkeypatch, tmp_path):
    ready = tmp_path / "ready"
    monkeypatch.setenv("MNEMA_TEST_MEDIA_READY", str(ready))
    monkeypatch.setenv("PATH", str(tmp_path))
    script = (
        f"#!{sys.executable}\n"
        + r"""
import os, signal, sys, time
from pathlib import Path
if Path(sys.argv[0]).name == "ffprobe":
    field = sys.argv[sys.argv.index("-show_entries") + 1]
    if field == "format=duration": print("10")
    elif field == "stream=width,height": print("640x480")
    elif field == "stream=r_frame_rate": print("30/1")
    else: print("h264" if sys.argv[sys.argv.index("-select_streams") + 1] == "v" else "aac")
    sys.exit()
if "-encoders" in sys.argv:
    print(" V..... libx265")
    sys.exit()
if os.environ.get("MNEMA_TEST_MEDIA_IGNORE_TERM") == "1":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(os.environ["MNEMA_TEST_MEDIA_READY"]).touch()
mode = os.environ.get("MNEMA_TEST_MEDIA_MODE", "progress")
if mode == "progress": print("out_time_us=1000000", flush=True)
elif mode == "closed": os.close(sys.stdout.fileno())
time.sleep(15)
"""
    )
    for name in ("ffmpeg", "ffprobe"):
        path = tmp_path / name
        path.write_text(script)
        path.chmod(0o700)
    popen, processes = subprocess.Popen, []

    def start(cmd, **kwargs):
        process = popen(cmd, **kwargs)
        if Path(cmd[0]).name == "ffmpeg" and "-encoders" not in cmd:
            processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", start)
    video._ffmpeg_encoders.cache_clear()
    yield ready, processes
    for process in processes:
        if process.poll() is None:
            process.kill()
        process.wait()
    video._ffmpeg_encoders.cache_clear()


@pytest.mark.parametrize("mode", ["progress", "silent", "closed"])
@pytest.mark.parametrize("ignore_term", [False, True])
def test_progress_ffmpeg_cancellation_reaps_child_including_silent_and_stubborn_process(
    ffmpeg_process, monkeypatch, mode, ignore_term
):
    ready, processes = ffmpeg_process
    monkeypatch.setenv("MNEMA_TEST_MEDIA_MODE", mode)
    monkeypatch.setenv("MNEMA_TEST_MEDIA_IGNORE_TERM", str(int(ignore_term)))
    cancelled = FinalizeCancelled()

    def on(fraction, message):
        if ready.exists() and (mode != "progress" or fraction is not None):
            raise cancelled

    with pytest.raises(FinalizeCancelled) as caught:
        video._ffmpeg_with_progress(["ffmpeg", "out.mp4"], total=10, on=on)
    assert caught.value is cancelled
    assert len(processes) == 1
    assert processes[0].returncode == -(signal.SIGKILL if ignore_term else signal.SIGTERM)
    assert processes[0].stdout.closed


@pytest.mark.parametrize("operation", ["extract", "mix", "merge"])
def test_audio_and_copy_merge_cancel_during_silent_ffmpeg(
    ffmpeg_process, monkeypatch, tmp_path, operation
):
    ready, processes = ffmpeg_process
    monkeypatch.setenv("MNEMA_TEST_MEDIA_MODE", "silent")
    cancelled = FinalizeCancelled()

    def on(fraction, message):
        if ready.exists():
            raise cancelled

    sources = [tmp_path / "a.mp4", tmp_path / "b.mp4"]
    for source in sources:
        source.touch()
    with pytest.raises(FinalizeCancelled) as caught:
        if operation == "extract":
            audio.extract_audio(sources[0], tmp_path / "out.wav", on=on)
        elif operation == "mix":
            audio.mix(sources, tmp_path / "out.mp3", on=on)
        else:
            video.merge(sources, tmp_path / "out.mp4", on=on)
    assert caught.value is cancelled
    assert len(processes) == 1 and processes[0].returncode == -signal.SIGTERM


def test_compress_api_cancel_does_not_start_fallback_process(ffmpeg_process, tmp_path):
    ready, processes = ffmpeg_process
    source = tmp_path / "source.mp4"
    source.touch()
    cancelled = base.MediaError("Задача отменена клиентом.")

    def on(fraction, message):
        if ready.exists():
            raise cancelled

    with pytest.raises(base.MediaError) as caught:
        video.compress(source, tmp_path / "out.mp4", on=on)
    assert caught.value is cancelled
    assert len(processes) == 1 and processes[0].returncode == -signal.SIGTERM


@pytest.mark.parametrize("code", [0, 2])
def test_run_with_callback_preserves_stdout_and_process_errors(code):
    command = [sys.executable, "-c", f"import sys; print('готово'); sys.exit({code})"]
    if code:
        with pytest.raises(base.MediaError, match="код 2"):
            base.run(command, on=lambda *_: None)
    else:
        assert base.run(command, on=lambda *_: None).strip() == "готово"
