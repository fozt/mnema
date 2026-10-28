"""The operation registry (@op / OpSpec) and the operations themselves."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from mnema import ops


def test_op_raises_on_duplicate_name():
    """Registering the same name twice is an error (catches accidental clashes on import)."""

    class P(BaseModel):
        pass

    @ops.op("dup-op", params=P, input="media", output="json")
    def _first(p: Path, params: dict, on) -> dict:
        return {}

    with pytest.raises(ValueError):

        @ops.op("dup-op", params=P, input="media", output="json")
        def _second(p: Path, params: dict, on) -> dict:
            return {}


def _fn(name: str):
    return ops.registry()[name].fn


def _mock_mp3_ffmpeg(monkeypatch) -> list[list[str]]:
    """The ffmpeg boundary of the mp3 op: probe (duration for the bar) and conversion."""
    import types

    from mnema.core import video

    captured: list[list[str]] = []
    monkeypatch.setattr(ops._mc, "probe", lambda p: types.SimpleNamespace(duration=10.0))
    monkeypatch.setattr(
        video, "_ffmpeg_with_progress", lambda cmd, *, total, on: captured.append(cmd)
    )
    return captured


def test_mp3_op_honors_bitrate(tmp_path: Path, monkeypatch):
    """mp3 uses the given bitrate."""
    captured = _mock_mp3_ffmpeg(monkeypatch)

    res = _fn("mp3")(tmp_path / "a.mov", {"bitrate": "256k"}, lambda f, m: None)
    assert res["path"].endswith("a.mp3")
    assert "256k" in captured[0]


def test_translate_op_uses_sidecar_then_translate(tmp_path: Path, monkeypatch):
    """translate: transcript (sidecar or transcribe) → claude.translate → {"markdown": ...}."""
    captured: dict = {}

    def fake_translate(text, *, on=None, **kw):
        captured["text"] = text
        captured.update(kw)
        return "translated"

    monkeypatch.setattr(ops._claude, "translate", fake_translate)
    sidecar = tmp_path / "a.txt"
    sidecar.write_text("исходный текст", encoding="utf-8")

    res = _fn("translate")(tmp_path / "a.mov", {"to_lang": "en"}, lambda f, m: None)
    assert res["markdown"] == "translated"
    assert captured["text"] == "исходный текст"
    assert captured.get("to_lang") == "en"


def test_transcript_text_reads_markdown_directly(tmp_path: Path, monkeypatch):
    """A .md/.markdown input is read directly, without transcription (no ffprobe)."""

    def boom(*a, **kw):
        raise AssertionError("transcribe must not run for a text input")

    monkeypatch.setattr(ops._mc, "transcribe", boom)

    for name in ("meeting.md", "notes.markdown", "plain.txt"):
        f = tmp_path / name
        f.write_text("готовый транскрипт", encoding="utf-8")
        assert ops._transcript_text(f, lambda fr, m: None) == "готовый транскрипт"


# Importing the registry without engines, and its completeness: test_thin_client.py.
