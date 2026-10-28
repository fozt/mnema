"""Call language when processing a recording (live/setup) and the processing history
(.mnema-meta.json).

Contract: with "Automatic" the language is not detected in advance; recognition runs without a
language and names it itself (Parakeet from the text), on_lang fires after recognition; a chosen
language is used as is, unchecked. Every processing run is appended to the folder's history.

The external boundary is recognition (dispatch).
"""

from __future__ import annotations

import array
import json
import types
import wave
from pathlib import Path

import pytest
from test_live_setup import _no_llm, _patch_build

import mnema.core.audio as audio_mod
import mnema.live.setup as setup_mod
from mnema.live import meta
from mnema.live.meta import META_FILE
from mnema.live.models import Track
from mnema.live.setup import FinalizeCancelled, LiveConfig, build_live, finalize_live

SR = 16000
RU, EN = 1000, 2000  # sample values: whose voice


def write_track(path: Path, seconds: float, voice: list[tuple[float, float, int]]) -> Path:
    """A Mnema track (PCM16 mono 16 kHz): spans (start, end, value), silence elsewhere; a span
    past the end is cut (the track is still being written)."""
    samples = array.array("h", bytes(int(seconds * SR) * 2))
    for start, end, level in voice:
        a, b = int(start * SR), min(int(end * SR), len(samples))
        samples[a:b] = array.array("h", [level]) * max(0, b - a)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(samples.tobytes())
    return path


def _two_tracks(monkeypatch, tmp_path, config: LiveConfig, mic: list, system: list, name="rec"):
    """A session with mic and system tracks (audio from write_track); mp3 mixing is stubbed."""
    monkeypatch.setattr(audio_mod, "mix", lambda inputs, dst, **k: dst.write_bytes(b"ID3") and dst)
    prepared = build_live(config, base=tmp_path / name)
    prepared.writers["system"] = types.SimpleNamespace(path=tmp_path / name / "system.wav")
    write_track(prepared.writers["mic"].path, 60.0, mic)
    write_track(prepared.writers["system"].path, 60.0, system)
    return prepared


def _recognize(monkeypatch, events: list | None = None, **extra) -> list[dict]:
    """Recognition (dispatch) at the boundary: a line where the track has a voice; the language
    as requested (or extra)."""
    calls: list[dict] = []

    def fake(op, src, params, *, runner=None, local=False, on=None):
        calls.append({"track": Path(src).stem, **params})
        if events is not None:
            events.append(("asr", Path(src).stem))
        text = f"реплика {Path(src).stem}"
        seg = {"start": 3.0, "end": 4.0, "text": text}
        return {"text": text, "segments": [seg], "language": params.get("lang"), **extra}

    monkeypatch.setattr(setup_mod, "dispatch", fake)
    return calls


def _meta(folder: Path) -> dict:
    return json.loads((folder / META_FILE).read_text(encoding="utf-8"))


def test_auto_language_is_named_by_recognition(monkeypatch, tmp_path) -> None:
    """ "Automatic": the language is not detected in advance; tracks go to recognition without a
    language, the call language is the one recognition named; on_lang fires once, after it."""
    _patch_build(monkeypatch, {})
    _no_llm(monkeypatch)
    events: list = []
    calls = _recognize(monkeypatch, events, language="en")
    cfg = LiveConfig(stream=False, summary=False)
    prepared = _two_tracks(
        monkeypatch, tmp_path, cfg, mic=[(3.0, 8.0, RU)], system=[(2.0, 50.0, EN)]
    )
    tracks = [Track(label="mic"), Track(label="system")]

    result, _ = finalize_live(
        prepared, tracks, retitle=False, on_lang=lambda langs: events.append(("lang", langs))
    )

    assert calls == [{"track": "mic"}, {"track": "system"}]  # no language
    assert events == [("asr", "mic"), ("asr", "system"), ("lang", {"mic": "en", "system": "en"})]
    assert result.languages == {"mic": "en", "system": "en"}


def test_chosen_language_is_used_as_is(monkeypatch, tmp_path) -> None:
    """A chosen language (including a user's correction) applies to all tracks as chosen; it is
    not detected from the voice, even when the speech is in another language."""
    _patch_build(monkeypatch, {})
    _no_llm(monkeypatch)
    calls = _recognize(monkeypatch)
    langs: list = []
    cfg = LiveConfig(stream=False, lang="en", summary=False)
    prepared = _two_tracks(
        monkeypatch, tmp_path, cfg, mic=[(1.0, 40.0, RU)], system=[(2.0, 45.0, RU)]
    )
    tracks = [Track(label="mic"), Track(label="system")]
    result, _ = finalize_live(prepared, tracks, retitle=False, on_lang=langs.append)
    assert calls == [{"track": "mic", "lang": "en"}, {"track": "system", "lang": "en"}]
    assert langs == [{"mic": "en", "system": "en"}] and result.remarks == []


def test_history_appends_every_processing_without_secrets(monkeypatch, tmp_path) -> None:
    """Every run is appended to .mnema-meta.json: time, version, settings (server host only, no
    keys), language, tracks, steps, remarks, outcome; stopped and failed runs too."""
    _patch_build(monkeypatch, {})
    _no_llm(monkeypatch)
    _recognize(monkeypatch)
    cfg = LiveConfig(
        stream=False,
        summary=False,
        runner="http://user:tok-secret-123@203.0.113.10:8000",
    )
    prepared = _two_tracks(monkeypatch, tmp_path, cfg, mic=[(1.0, 40.0, RU)], system=[])
    finalize_live(prepared, [Track(label="mic")], retitle=False)
    text = (prepared.base / META_FILE).read_text(encoding="utf-8")
    assert "tok-secret-123" not in text and "user:" not in text
    data = json.loads(text)
    assert data["version"] == 1 and len(data["runs"]) == 1
    run = data["runs"][0]
    assert run["status"] == "ok" and run["started"] <= run["finished"] and run["mnema"]["version"]
    assert run["settings"] == {
        "lang": None, "asr": "auto", "summary": False,
        "summary_lang": None, "server": "203.0.113.10:8000",
        "model": None, "source": "unconfigured",
    }  # fmt: skip
    assert run["tracks"]["mic"] == {"engine": None, "lang": None, "seconds": 60.0, "error": None}
    assert {"asr", "save"} <= set(run["steps"]) and run["part"] is None
    assert not list(prepared.base.glob(f"{META_FILE}.*.tmp"))

    def cancelled(frac, msg):
        if msg.startswith("Распознаю "):
            raise FinalizeCancelled

    with pytest.raises(FinalizeCancelled):
        finalize_live(prepared, [Track(label="mic")], retitle=False, on=cancelled)
    monkeypatch.setattr(setup_mod, "_mix_recording", lambda *a, **kw: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        finalize_live(prepared, [Track(label="mic")], retitle=False)
    runs = _meta(prepared.base)["runs"]
    assert [r["status"] for r in runs] == ["ok", "stopped", "failed"]
    assert runs[2]["error"] == "division by zero" and runs[1]["error"] is None
    last = meta.last(prepared.base)
    assert last is not None and (last.kind, last.status) == ("process", "failed")
    assert meta.last(tmp_path / "нет такой") is None


def test_unreadable_history_is_kept_aside(tmp_path) -> None:
    """An unreadable history is not lost: it is kept next to it as .bad and a new one starts."""
    from mnema.live import meta

    (tmp_path / META_FILE).write_text("{оборвано", encoding="utf-8")
    meta.append(tmp_path, meta.Run(started="2026-10-01T10:00:00+02:00", status="ok"))
    assert (tmp_path / f"{META_FILE}.bad").read_text(encoding="utf-8") == "{оборвано"
    assert [r["status"] for r in _meta(tmp_path)["runs"]] == ["ok"]
    meta.append(tmp_path, meta.Run(status="failed"))
    assert [r["status"] for r in _meta(tmp_path)["runs"]] == ["ok", "failed"]
    assert sorted(p.name for p in tmp_path.iterdir()) == [META_FILE, f"{META_FILE}.bad"]
