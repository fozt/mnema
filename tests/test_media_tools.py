"""Where ffmpeg and ffprobe come from: an explicit path, the app bundle, PATH."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from mnema.core import audio, base
from mnema.i18n import using


def _binary(folder: Path, name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def no_tools(monkeypatch, tmp_path):
    """Nothing is configured: no MNEMA_FFMPEG, an empty PATH, a Python with no ffmpeg beside it."""
    for name in base.TOOL_ENV.values():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python" / "bin" / "python3.12"))
    return tmp_path


def test_bundle_copy_wins_over_path(no_tools, monkeypatch):
    on_path = _binary(no_tools / "path", "ffprobe")
    bundled = _binary(no_tools / "Mnema.app" / "Contents" / "Resources" / "bin", "ffprobe")
    monkeypatch.setenv("PATH", str(on_path.parent))
    monkeypatch.setenv("MNEMA_APP_BUNDLE", str(no_tools / "Mnema.app"))
    assert base.tool("ffprobe") == str(bundled)


def test_copy_next_to_python_wins_over_path(no_tools, monkeypatch):
    on_path = _binary(no_tools / "path", "ffmpeg")
    beside = _binary(no_tools / "python" / "bin", "ffmpeg")
    monkeypatch.setenv("PATH", str(on_path.parent))
    assert base.tool("ffmpeg") == str(beside)


def test_explicit_path_wins_over_bundle(no_tools, monkeypatch):
    _binary(no_tools / "Mnema.app" / "Contents" / "Resources" / "bin", "ffmpeg")
    monkeypatch.setenv("MNEMA_APP_BUNDLE", str(no_tools / "Mnema.app"))
    monkeypatch.setenv("MNEMA_FFMPEG", "/opt/media/ffmpeg")
    assert base.tool("ffmpeg") == "/opt/media/ffmpeg"


def test_path_is_the_last_place(no_tools, monkeypatch):
    on_path = _binary(no_tools / "path", "ffmpeg")
    (no_tools / "Mnema.app" / "Contents" / "Resources" / "bin").mkdir(parents=True)
    monkeypatch.setenv("MNEMA_APP_BUNDLE", str(no_tools / "Mnema.app"))
    monkeypatch.setenv("PATH", str(on_path.parent))
    assert base.tool("ffmpeg") == str(on_path)


def test_missing_ffmpeg_is_a_user_error_without_commands(no_tools, tmp_path):
    with pytest.raises(base.MediaError) as ru:
        audio.extract_audio(tmp_path / "in.wav", tmp_path / "out.wav")
    with using("en"), pytest.raises(base.MediaError) as en:
        base.tool("ffprobe")
    assert str(ru.value) == "Не найден ffmpeg. Установите ffmpeg или переустановите Mnema"
    assert str(en.value) == "ffprobe not found. Install ffmpeg or reinstall Mnema"


def test_operations_run_the_bundled_ffmpeg(no_tools, monkeypatch, tmp_path):
    """extract_audio runs the bundle's binary: the stub records that it was the one called."""
    bundle_bin = no_tools / "Mnema.app" / "Contents" / "Resources" / "bin"
    bundle_bin.mkdir(parents=True)
    calls = tmp_path / "calls"
    stub = bundle_bin / "ffmpeg"
    stub.write_text(f'#!/bin/sh\necho "$0" >> "{calls}"\n')
    stub.chmod(0o755)
    monkeypatch.setenv("MNEMA_APP_BUNDLE", str(no_tools / "Mnema.app"))
    audio.extract_audio(tmp_path / "in.wav", tmp_path / "out.wav")
    assert calls.read_text().split() == [str(stub)]
