"""Building a live session from its configuration (live/setup.py), the menu bar entry point.

Paths shared with cmd_live (sources, dialog, transcribers, recordings) are covered by the CLI
contract tests (test_live_cli); here is what only the configuration gives: recording without
live text, finalization without a UI, and the two-pass result (an offline pass over the
recording).
"""

from __future__ import annotations

import json
import re
import sys
import types
from functools import partial
from pathlib import Path

import pytest

import mnema.live.session as session_mod
import mnema.live.setup as setup_mod
import mnema.live.sources as sources_mod
from mnema.connection import Connection
from mnema.core.base import MediaError
from mnema.core.models import Segment
from mnema.i18n import _, using
from mnema.live.models import STEP_MESSAGES, Track
from mnema.live.remarks import Step, menu_lines
from mnema.live.setup import LiveConfig, build_live, finalize_live, live_base

build_live = partial(build_live, connection=Connection(url="http://selected", token="token"))


def _no_llm(mp) -> None:
    """Drop the LLM config: without a model llm_available() is False (best-effort steps skip)."""
    mp.delenv("MNEMA_MODEL", raising=False)


def _patch_build(monkeypatch, captured: dict) -> None:
    monkeypatch.delenv("MNEMA_RUNNER", raising=False)
    # runner health is network: the session runner answers (offline cases set their own)
    # sounddevice is an external boundary: CI (Linux) lacks it, build checks it by importing
    monkeypatch.setitem(sys.modules, "sounddevice", types.SimpleNamespace())

    class FakeMic:
        def __init__(self, *a, **k) -> None:
            self.name = "mic"

        def available(self) -> bool:
            return True

    class FakeSession:
        def __init__(self, sources, factory, *, dialog=None, view=None, recorder=None, **k):
            captured["sources"] = list(sources)
            captured["factory"] = factory
            captured["recorder"] = recorder

        def run(self):
            return []

    monkeypatch.setattr(sources_mod, "MicSource", FakeMic)
    monkeypatch.setattr(session_mod, "LiveSession", FakeSession)


def test_live_base_prefers_output_then_live_dir(monkeypatch, tmp_path) -> None:
    """live_base: an explicit output wins; otherwise a timestamped folder in MNEMA_LIVE_DIR.
    Session files live in the folder, so live_base creates it at once."""
    monkeypatch.setenv("MNEMA_LIVE_DIR", str(tmp_path / "notes"))
    explicit = live_base(tmp_path / "мой-путь")
    assert explicit == tmp_path / "мой-путь" and explicit.is_dir()
    base = live_base(None)
    assert base.parent == tmp_path / "notes"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}", base.name)  # «2026-09-27_13-05»
    again = live_base()  # a second recording in the same minute gets its own folder
    assert again != base and again.name.startswith(base.name)
    assert base.is_dir()  # the session folder exists

    # an explicit root (CLI: recordings/ in the working folder) instead of the library
    with_root = live_base(None, root=tmp_path / "bar-notes")
    assert with_root.parent == tmp_path / "bar-notes"


# ── two-pass result: the offline pass over the recording in finalize_live ──

_OFFLINE = {
    "text": "Всё обдумано и решено.",
    "segments": [{"start": 0.0, "end": 2.0, "text": "Всё обдумано и решено."}],
    "language": "ru",
}


def _fake_dispatch(monkeypatch, result=None, calls: list | None = None) -> None:
    """dispatch is the offline pipeline boundary (engine/runner), mocked in unit tests."""

    def fake(op, src, params, *, runner=None, local=False, on=None):
        if calls is not None:
            calls.append({"op": op, "src": src, "params": params, "runner": runner, "local": local})
        if isinstance(result, Exception):
            raise result
        return dict(result or _OFFLINE)

    monkeypatch.setattr(setup_mod, "dispatch", fake)


def _runner_answers(monkeypatch, capabilities: dict) -> None:
    """The runner at the HTTP boundary: GET /api/capabilities returns capabilities."""
    import httpx

    import mnema.client as client_mod

    real = client_mod.MnemaClient
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=capabilities))
    monkeypatch.setattr(
        client_mod,
        "MnemaClient",
        lambda url, *, token=None: real(url, token=token, transport=transport),
    )


def _cloud_model(monkeypatch, answer) -> None:
    """The cloud model at the external boundary (streaming litellm.completion):
    answer(system, content) → reply text or an exception; no waiting before retries."""
    import litellm

    def completion(**kw):
        out = answer(kw["messages"][0]["content"], kw["messages"][1]["content"])
        delta = types.SimpleNamespace(content=out)
        choice = types.SimpleNamespace(delta=delta, finish_reason="stop")
        return iter([types.SimpleNamespace(choices=[choice])])

    monkeypatch.setattr(litellm, "completion", completion)
    monkeypatch.setattr(setup_mod.claude.time, "sleep", lambda s: None)


def _live_tracks() -> list[Track]:
    return [
        Track(
            label="mic",
            committed=[Segment(start=0, end=1, text="В... Обдумывании", speaker="mic")],
        )
    ]


def _write_wav(prepared, label: str = "mic", *, size: int = 4000) -> None:
    path = prepared.writers[label].path
    path.parent.mkdir(parents=True, exist_ok=True)  # in production live_base creates the folder
    path.write_bytes(b"\x00" * size)


def test_finalize_two_pass_replaces_live_segments(monkeypatch, tmp_path) -> None:
    """With a WAV track the offline transcription replaces the live segments (the txt has the
    offline text), the speaker is the track label, and the track is in refined_labels."""
    _patch_build(monkeypatch, {})
    _no_llm(monkeypatch)
    calls: list = []
    _fake_dispatch(monkeypatch, calls=calls)

    prepared = build_live(LiveConfig(), base=tmp_path / "live-x")
    _write_wav(prepared)
    result, full = finalize_live(prepared, _live_tracks(), retitle=False)

    txt = result.transcript_txt.read_text(encoding="utf-8")
    assert "Всё обдумано и решено." in txt and "Обдумывании" not in txt
    assert "обдумано" in full
    assert result.tracks[0].committed[0].speaker == "mic"
    assert result.refined_labels == ["mic"]
    assert result.remarks == []  # no model is not an offline pass failure
    assert calls[0]["op"] == "transcribe" and calls[0]["src"] == prepared.writers["mic"].path


@pytest.mark.parametrize("partial", [False, True])
def test_recover_replaces_transcript_keeping_edits(monkeypatch, tmp_path, partial) -> None:
    """Reprocessing keeps the old lines and manual edits even when a track fails."""
    import wave

    base = tmp_path / "meeting"
    base.mkdir()
    for label in ("mic", "system"):
        with wave.open(str(base / f"{label}.wav"), "wb") as wav:
            wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            wav.writeframes((1000).to_bytes(2, "little", signed=True) * 16000)
    previous = "mic: исправленная старая реплика\nsystem: прежний полный ответ\n"
    (base / "transcript.txt").write_text(previous, encoding="utf-8")

    def recognize(op, src, params, **kwargs):
        if partial and src.stem == "system":
            raise MediaError("сбой распознавания system")
        text = f"Новая реплика {src.stem}"
        return {
            "text": text,
            "segments": [{"start": 0, "end": 1, "text": text}],
            "language": "ru",
        }

    monkeypatch.setattr(setup_mod, "dispatch", recognize)
    config = LiveConfig(lang="ru", summary=False, mix_audio=False)
    result, _ = setup_mod.recover_session(base, config, retitle=False)

    assert result.transcript_txt is not None
    current = result.transcript_txt.read_text(encoding="utf-8")
    assert "Новая реплика mic" in current
    assert ("Новая реплика system" in current) is not partial
    assert not any("Прежн" in note for note in result.info)
    if partial:
        assert result.refined_labels == ["mic"]
        assert any(r.step == Step.ASR for r in result.remarks)
    else:
        assert result.refined_labels == ["mic", "system"]


def test_server_finalize_uses_own_asr_and_client_language(monkeypatch, tmp_path) -> None:
    """The offline pass goes the cmd_transcribe way: the runner from the session config (None
    forces local, env does not override), the language is config.lang as is; "Automatic" has
    no language (recognition names it)."""
    _patch_build(monkeypatch, {})
    _no_llm(monkeypatch)
    calls: list = []
    _fake_dispatch(monkeypatch, calls=calls)

    prepared = build_live(
        LiveConfig(lang="ru", runner="https://203.0.113.10"), base=tmp_path / "live-r"
    )
    _write_wav(prepared)
    finalize_live(prepared, _live_tracks(), retitle=False)
    assert calls[0]["runner"] is None and calls[0]["local"] is True
    assert calls[0]["params"] == {"lang": "ru"}

    calls.clear()
    prepared = build_live(LiveConfig(), base=tmp_path / "live-l")
    _write_wav(prepared)
    finalize_live(prepared, _live_tracks(), retitle=False)
    # MNEMA_RUNNER in the environment does not decide
    assert calls[0]["runner"] is None and calls[0]["local"] is True
    assert calls[0]["params"] == {}


@pytest.mark.parametrize(
    ("offline", "reason"),
    [
        (MediaError("нет движка — задайте --runner"), "«Микрофон»: сбой на сервере"),
        ({"text": "", "segments": [], "language": None}, None),  # broken or empty audio
    ],
)
def test_finalize_refine_failure_keeps_live_text(monkeypatch, tmp_path, offline, reason) -> None:
    """An offline pass failure (no engine/runner, broken wav) or an empty result over live
    text → the live text is kept, with a warning."""
    _patch_build(monkeypatch, {})
    _no_llm(monkeypatch)
    _fake_dispatch(monkeypatch, result=offline)

    prepared = build_live(LiveConfig(), base=tmp_path / "live-x")
    _write_wav(prepared)
    result, full = finalize_live(prepared, _live_tracks(), retitle=False)

    assert "Обдумывании" in result.transcript_txt.read_text(encoding="utf-8")
    assert full == "В... Обдумывании"
    assert not result.refined_labels
    [asr] = result.remarks
    assert asr.step is Step.ASR
    if reason:
        assert f"Не удалось распознать дорожку {reason}" in asr.text
        assert "остался черновой текст" in asr.text  # the live text is kept, and the user is told


def _en_tracks() -> list[Track]:
    """An English line: detect_lang != "ru" → the final translation into my language runs."""
    return [
        Track(
            label="mic",
            committed=[
                Segment(start=0, end=1, text="We will run the migration today.", speaker="mic")
            ],
        )
    ]


def test_finalize_without_llm_saves_transcript_without_translation(monkeypatch, tmp_path) -> None:
    """Without a model the final translation does not break finalization: the transcript is
    saved, there is no translation file."""
    _patch_build(monkeypatch, {})
    _no_llm(monkeypatch)  # llm_available() → False

    prepared = build_live(LiveConfig(translate="ru"), base=tmp_path / "live-x")
    result, _full = finalize_live(prepared, _en_tracks(), retitle=False)

    assert result.transcript_txt.exists()
    assert not (prepared.base / "translation.md").exists()


def test_finalize_empty_wav_keeps_live_text(monkeypatch, tmp_path) -> None:
    """An empty recording (WAV header only): the live text, no engine run."""
    _patch_build(monkeypatch, {})
    _no_llm(monkeypatch)
    calls: list = []
    _fake_dispatch(monkeypatch, calls=calls)

    prepared = build_live(LiveConfig(), base=tmp_path / "live-x")
    _write_wav(prepared, size=44)  # the wave module writes a 44-byte header before the first frame
    result, _full = finalize_live(prepared, _live_tracks(), retitle=False)

    assert not calls and not [r for r in result.remarks if r.step is not Step.SUMMARY]
    assert "Обдумывании" in result.transcript_txt.read_text(encoding="utf-8")


def test_finalize_dialog_pins_offline_speaker_labels(monkeypatch, tmp_path) -> None:
    """With a dialog (--translate --system) offline segments are labelled by SOURCE name
    (mic/system, not "я"/"они"), and the final translation uses the offline text."""
    import mnema.live.system_audio as system_audio_mod

    _patch_build(monkeypatch, {})

    class FakeSystem:
        def __init__(self, *a, **k) -> None:
            self.name = k.get("label", "system")

        def available(self) -> bool:
            return True

    monkeypatch.setattr(  # the factory, not the macOS class: Linux CI takes the parec source
        system_audio_mod,
        "system_source",
        lambda *, label="system", tee=None: FakeSystem(label=label),
    )
    # the offline text is in the other side's language: the en→ru final translation must run
    # (an all-Russian call skips translation, see test_finalize_skips_noop_translation)
    _fake_dispatch(
        monkeypatch,
        result={
            "text": "Considered offline.",
            "segments": [{"start": 0.0, "end": 2.0, "text": "Considered offline."}],
            "language": "en",
        },
    )
    translated: dict = {}

    def fake_translate(text, **_kw) -> str:
        translated["input"] = text
        return "перевод"

    monkeypatch.setattr(setup_mod.claude, "translate", fake_translate)

    prepared = build_live(LiveConfig(system=True, translate="ru"), base=tmp_path / "live-x")
    _write_wav(prepared)
    result, _full = finalize_live(prepared, _live_tracks(), retitle=False)

    mic = next(t for t in result.tracks if t.label == "mic")
    assert all(s.speaker == "mic" for s in mic.committed)  # the source name, not "я"
    # Translated from the offline text, not the live one
    assert "Considered offline." in translated["input"]
    assert result.translation_md is not None


def test_refine_gate_drops_segments_from_wav_silence(monkeypatch, tmp_path) -> None:
    """The offline pass drops segments whose WAV interval is physical silence (batch decode
    hallucinations like "Thank you" on long pauses); speech segments stay."""
    import math
    import wave

    from mnema.live import setup as setup_mod

    wav = tmp_path / "mic.wav"
    rate = 16000
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        silence = b"\x00\x00" * (rate * 4)  # 0–4 s: silence
        tone = b"".join(  # 4–6 s: "speech" (a 220 Hz tone, amplitude 6000)
            int(6000 * math.sin(2 * math.pi * 220 * i / rate)).to_bytes(2, "little", signed=True)
            for i in range(rate * 2)
        )
        w.writeframes(silence + tone)

    assert setup_mod._wav_speech_rms(wav, 0.5, 3.5) < 300  # silence
    assert setup_mod._wav_speech_rms(wav, 4.2, 5.8) > 300  # speech
    assert setup_mod._wav_speech_rms(wav, 100.0, 101.0) is None  # outside the file: unknown


@pytest.mark.parametrize(
    ("text", "foreign"), [("Let us start the daily.", True), ("Привет всем.", False)]
)
def test_finalize_translates_foreign_conversation(monkeypatch, tmp_path, text, foreign) -> None:
    """A call in another language with an LLM → final translation into my language via Claude.
    A call fully in my language → no ru→ru translation and no translation.md (a copy of the
    transcript looked like a failed translation)."""
    _patch_build(monkeypatch, {})
    # an LLM is set (conftest default): finalization uses Claude; claude.translate is mocked
    called: list = []
    monkeypatch.setattr(
        setup_mod.claude, "translate", lambda *a, **k: called.append(1) or "перевод всего"
    )

    prepared = build_live(LiveConfig(translate="ru", record=False), base=tmp_path / "live-y")
    tracks = [Track(label="mic", committed=[Segment(start=0, end=1, text=text, speaker="они")])]
    result, _full = finalize_live(prepared, tracks, retitle=False)

    if foreign:
        assert result.translation_md is not None
        assert "перевод всего" in result.translation_md.read_text(encoding="utf-8")
    else:
        assert called == [] and result.translation_md is None


def test_wav_is_silent_detects_speech_anywhere(monkeypatch, tmp_path) -> None:
    """The scan is complete: a line in the middle of long silence makes the file not silent."""
    import math
    import wave

    from mnema.live import setup as setup_mod

    wav = tmp_path / "t.wav"
    rate = 16000
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        silence = b"\x00\x00" * (rate * 5)
        tone = b"".join(
            int(6000 * math.sin(2 * math.pi * 220 * i / rate)).to_bytes(2, "little", signed=True)
            for i in range(rate)
        )
        w.writeframes(silence + tone + silence)  # speech surrounded by silence
    assert setup_mod._wav_is_silent(wav) is False

    quiet = tmp_path / "q.wav"
    with wave.open(str(quiet), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * (rate * 11))
    assert setup_mod._wav_is_silent(quiet) is True


def test_silence_scan_can_be_cancelled_between_chunks(tmp_path) -> None:
    import wave

    wav = tmp_path / "quiet.wav"
    with wave.open(str(wav), "wb") as recording:
        recording.setnchannels(1)
        recording.setsampwidth(2)
        recording.setframerate(16000)
        recording.writeframes(b"\x00\x00" * 16000 * 3)
    callbacks = 0

    def cancel(_fraction, _message):
        nonlocal callbacks
        callbacks += 1
        if callbacks == 2:
            raise setup_mod.FinalizeCancelled()

    with pytest.raises(setup_mod.FinalizeCancelled):
        setup_mod._wav_is_silent(wav, chunk_sec=1.0, on=cancel)
    assert callbacks == 2


def test_finalize_title_comes_with_translation_no_extra_call(monkeypatch, tmp_path) -> None:
    """With a final translation the title comes in the same request: no separate
    claude.title_ex call, the folder is renamed by the translation's title."""
    _patch_build(monkeypatch, {})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(
        setup_mod.claude,
        "translate_titled",
        lambda text, **k: ("Перевод всего.", "Дейли про релиз"),
    )
    monkeypatch.setattr(setup_mod.claude, "translate", lambda *a, **k: "не должно")
    title_calls: list = []
    monkeypatch.setattr(
        setup_mod.claude,
        "title_ex",
        lambda *a, **k: title_calls.append(1) or ("не должно", "cloud", None),
    )

    (tmp_path / "live-x").mkdir()  # in production live_base creates the folder
    prepared = build_live(LiveConfig(translate="ru", record=False), base=tmp_path / "live-x")
    tracks = [
        Track(
            label="mic",
            committed=[Segment(start=0, end=1, text="Let us start the daily.", speaker="они")],
        )
    ]
    result, _full = finalize_live(prepared, tracks)

    assert title_calls == []  # no separate title call
    assert result.transcript_txt.parent.name.endswith("_Дейли про релиз")


def test_finalize_progress_monotonic_across_phases(monkeypatch, tmp_path) -> None:
    """Finalization progress is ONE 0..1 scale across all phases (offline ASR per track,
    translation, title, saving): shares never decrease and never exceed 1.

    Regression: each phase sent its own raw 0..1 to the shared callback, so the bar reached
    100% and reset on every phase and track."""
    import mnema.live.system_audio as system_audio_mod

    _patch_build(monkeypatch, {})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    class FakeSystem:
        def __init__(self, *a, **k) -> None:
            self.name = k.get("label", "system")

        def available(self) -> bool:
            return True

    monkeypatch.setattr(  # the factory, not the macOS class: Linux CI takes the parec source
        system_audio_mod,
        "system_source",
        lambda *, label="system", tee=None: FakeSystem(label=label),
    )

    def fake(op, src, params, *, runner=None, local=False, on=None):
        # the engine sends ITS OWN raw 0..1 progress; finalize must map it into the phase slice
        if on is not None:
            on(0.3, "распознаю")
            on(1.0, "готово")
        return dict(_OFFLINE)

    monkeypatch.setattr(setup_mod, "dispatch", fake)

    prepared = build_live(LiveConfig(system=True), base=tmp_path / "live-x")
    _write_wav(prepared, "mic")
    _write_wav(prepared, "system")
    tracks = [
        Track(label="mic", committed=[Segment(start=0, end=1, text="раз", speaker="mic")]),
        Track(label="system", committed=[Segment(start=1, end=2, text="два", speaker="system")]),
    ]
    fractions: list[float] = []
    steps: list[str] = []

    def on(fraction, message):
        if fraction is not None:
            fractions.append(fraction)
        if message in {_(m) for m in STEP_MESSAGES.values()}:
            steps.append(message)

    finalize_live(prepared, tracks, retitle=False, on=on)

    assert fractions, "финализация обязана слать числовые доли (прогресс-бар не пустой)"
    assert all(a <= b + 1e-9 for a, b in zip(fractions, fractions[1:], strict=False)), (
        f"прогресс сбрасывается: {fractions}"
    )
    assert max(fractions) <= 1.0 + 1e-9
    assert fractions[-1] > 0.99  # finalization takes the bar to the end
    # a step is announced by a message at its start: the panel labels from it, not from the share
    # (the bar used to jump from 74 % to 90 % through a translation the bar does not have). With no
    # dialog there is no translation and a few words get no summary, so neither is announced
    assert steps == [_(STEP_MESSAGES[s]) for s in ("asr", "save")]


def test_finalize_empty_transcript_no_files_but_base_kept(monkeypatch, tmp_path) -> None:
    """A transcription without text: no transcript.txt (transcript_txt=None), the recording
    folder stays."""
    _patch_build(monkeypatch, {})
    _no_llm(monkeypatch)
    _fake_dispatch(monkeypatch, result={"text": "", "segments": [], "language": None})

    prepared = build_live(LiveConfig(), base=tmp_path / "live-x")
    _write_wav(prepared)
    result, full = finalize_live(prepared, [Track(label="mic")], retitle=False)

    assert full == ""
    assert result.transcript_txt is None
    assert result.refined_labels == []  # emptiness is not success (the UI shows a yellow panel)
    assert not (prepared.base / "transcript.txt").exists()
    assert result.recordings  # the recording is intact


def test_finalize_retitle_updates_prepared_base(monkeypatch, tmp_path) -> None:
    """After the folder is renamed (retitle) prepared.base points to the NEW path: Ctrl-C
    after retitle used to delete the old, missing folder and leave the saved one."""
    _patch_build(monkeypatch, {})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    _fake_dispatch(monkeypatch)
    monkeypatch.setattr(
        setup_mod.claude, "title_ex", lambda *a, **k: ("Дейли про релиз", "cloud", None)
    )

    prepared = build_live(LiveConfig(), base=tmp_path / "live-x")
    _write_wav(prepared)
    result, _full = finalize_live(prepared, _live_tracks(), retitle=True)

    assert result.transcript_txt.parent.name.endswith("_Дейли про релиз")
    assert prepared.base == result.transcript_txt.parent  # the UI sees the current path


def test_finalize_title_cloud_failure_keeps_timestamp_with_note(monkeypatch, tmp_path) -> None:
    """The cloud model refused (balance) → the folder keeps its timestamp; the remarks say which
    model, why, what came out and what to do."""
    _patch_build(monkeypatch, {})
    _fake_dispatch(monkeypatch)

    def cloud_down(*a, **k):
        raise RuntimeError("DeepseekException - Insufficient Balance")

    _cloud_model(monkeypatch, cloud_down)

    prepared = build_live(LiveConfig(), base=tmp_path / "live-x")
    _write_wav(prepared)
    result, _full = finalize_live(prepared, _live_tracks(), retitle=True)

    assert result.transcript_txt is not None
    assert result.transcript_txt.parent.name == "live-x"  # no model: no invented name
    cause = "Модель claude-sonnet-5 не ответила: закончились деньги на счёте"
    [model] = result.remarks
    assert model.text.count(cause) == 1
    assert "Запись названа по времени" in model.text
    assert model.text.count("Пополните счёт у провайдера") == 1
    assert (model.short, model.short_effect) == ("Закончились деньги на счёте", "без названия")


def test_finalize_two_tracks_make_recording_mp3(monkeypatch, tmp_path) -> None:
    """Two tracks with sound → a shared recording.mp3 (mixing by ffmpeg, the external boundary)."""
    import mnema.core.audio as audio_mod

    _patch_build(monkeypatch, {})
    _no_llm(monkeypatch)
    _fake_dispatch(monkeypatch)
    mixed: list = []

    def fake_mix(inputs, dst, *, on=None, args=None):
        mixed.append(([p.name for p in inputs], dst.name, args))
        dst.write_bytes(b"ID3")
        return dst

    monkeypatch.setattr(audio_mod, "mix", fake_mix)
    monkeypatch.setattr(setup_mod, "_wav_is_silent", lambda wav, **kwargs: False)
    prepared = build_live(LiveConfig(), base=tmp_path / "live-m")
    prepared.writers["system"] = types.SimpleNamespace(path=tmp_path / "live-m" / "system.wav")
    _write_wav(prepared, "mic")
    _write_wav(prepared, "system")
    result, _ = finalize_live(prepared, _live_tracks(), retitle=False)
    assert mixed and mixed[0][0] == ["mic.wav", "system.wav"] and mixed[0][1] == "recording.mp3"
    assert "-b:a" in mixed[0][2]
    assert any(p.name == "recording.mp3" for p in result.recordings)

    mixed.clear()  # one track: no mp3 needed
    prepared = build_live(LiveConfig(), base=tmp_path / "live-1")
    _write_wav(prepared)
    finalize_live(prepared, _live_tracks(), retitle=False)
    assert mixed == []


def test_finalize_summary_whole_text_names_folder_and_writes_summary_md(
    monkeypatch, tmp_path
) -> None:
    """A summary of the WHOLE text: summary.txt next to it, the folder title from the same pass
    (no separate title call); a title the user entered wins."""
    _patch_build(monkeypatch, {})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    long_text = "Обсудили сроки релиза и кто что берёт. " * 20 + "Всё обдумано и решено."
    _fake_dispatch(
        monkeypatch,
        {"text": long_text, "segments": [{"start": 0.0, "end": 60.0, "text": long_text}]},
    )
    seen: list[str] = []

    def fake_summary(text, **k):
        seen.append(text)
        return setup_mod.claude.Summary(
            title="Созвон по релизу",
            about="Созвон команды о сроках релиза.",
            points=["Решили выпустить в пятницу.", "Аня готовит заметки."],
            engine="cloud",
        )

    monkeypatch.setattr(setup_mod.claude, "title_summary_ex", fake_summary)
    monkeypatch.setattr(
        setup_mod.claude, "title_ex", lambda *a, **k: pytest.fail("название уже есть")
    )
    prepared = build_live(LiveConfig(), base=tmp_path / "live-s")
    _write_wav(prepared)
    result, _ = finalize_live(prepared, _live_tracks())
    # The whole text, not the first 4000 characters
    assert seen and "Всё обдумано и решено." in seen[0]
    assert result.transcript_txt.parent.name.endswith("_Созвон по релизу")
    md = result.summary_md.read_text(encoding="utf-8")
    assert md.startswith("Созвон команды о сроках релиза.\n\n- Решили")

    prepared = build_live(LiveConfig(summary=False), base=tmp_path / "live-off")
    _write_wav(prepared)
    monkeypatch.setattr(
        setup_mod.claude, "title_summary_ex", lambda *a, **k: pytest.fail("выключено")
    )
    monkeypatch.setattr(setup_mod.claude, "title_ex", lambda *a, **k: ("Имя", "cloud", None))
    result, _ = finalize_live(prepared, _live_tracks())
    assert result.summary_md is None


def test_finalize_short_recording_has_no_summary(monkeypatch, tmp_path) -> None:
    """A couple of lines get no summary (the model invented "greetings" and a "friendly tone");
    the title is made the usual way."""
    _patch_build(monkeypatch, {})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    _fake_dispatch(monkeypatch)  # "Всё обдумано и решено.": one short line
    monkeypatch.setattr(
        setup_mod.claude, "title_summary_ex", lambda *a, **k: pytest.fail("слишком коротко")
    )
    monkeypatch.setattr(setup_mod.claude, "title_ex", lambda *a, **k: ("Проверка", "cloud", None))
    prepared = build_live(LiveConfig(), base=tmp_path / "2026-09-27_18-33")
    _write_wav(prepared)
    result, _ = finalize_live(prepared, _live_tracks())
    assert result.summary_md is None
    assert result.transcript_txt.parent.name == "2026-09-27_Проверка"


def test_finalize_cloud_failure_with_asr_runner_keeps_client_notes(monkeypatch, tmp_path):
    """ASR on the server, summary on the client. The cloud refusal shows, no repeated title."""
    _patch_build(monkeypatch, {})
    speech = "Обсудили план релиза, сроки и задачи на следующую неделю. " * 12
    calls = []
    _fake_dispatch(
        monkeypatch,
        {
            "text": speech,
            "segments": [{"start": 0, "end": 2, "text": speech}],
            "language": "ru",
        },
        calls=calls,
    )
    monkeypatch.setenv("MNEMA_MODEL", "deepseek/deepseek-chat")
    sent = _summary_model(monkeypatch, lambda: RuntimeError("402 Insufficient Balance"))
    _runner_answers(monkeypatch, {"ok": True, "llm": False})  # a node without a model
    prepared = build_live(LiveConfig(runner="https://runner"), base=tmp_path / "meeting")
    _write_wav(prepared)
    result, _ = finalize_live(prepared, _live_tracks())
    assert result.transcript_txt.exists() and result.summary_md is None
    assert [call["op"] for call in calls] == ["transcribe"]
    assert len(sent) == 1
    [notice] = [r for r in result.remarks if r.step in (Step.TITLE, Step.SUMMARY)]
    assert "закончились деньги" in notice.cause
    assert "Пересказа нет" in notice.text


def test_transcript_written_right_after_recognition(monkeypatch, tmp_path) -> None:
    """transcript.txt is written as soon as recognized: it is there while the model writes the
    summary, and it is the final transcript. Edits made during the summary are not overwritten
    at the end. Nothing recognized: no early file."""
    _patch_build(monkeypatch, {})
    speech = _long_speech(monkeypatch, "en")
    on_disk: list[str] = []
    edited = "mic: We agreed on the release date with Grisha.\n"

    def summary_model(system, content, **k):
        on_disk.append((base / "transcript.txt").read_text(encoding="utf-8"))
        (base / "transcript.txt").write_text(edited, encoding="utf-8")  # the names were edited
        answer = {"title": "Call", "about": "About the release.", "summary": ["Point."]}
        return json.dumps(answer, ensure_ascii=False)

    _cloud_model(monkeypatch, summary_model)
    prepared = build_live(LiveConfig(), base=tmp_path / "live-x")
    base = prepared.base
    _write_wav(prepared)
    got: list[Path] = []
    result, _ = finalize_live(prepared, _live_tracks(), retitle=False, on_transcript=got.append)

    line = f"[0:00] mic: {speech}\n"
    assert got == [base / "transcript.txt"] and on_disk == [line]  # ready before the summary
    assert result.transcript_txt.read_text(encoding="utf-8") == edited
    assert result.summary_md is not None

    _fake_dispatch(monkeypatch, result=RuntimeError("нет движка"))
    prepared = build_live(LiveConfig(summary=False), base=tmp_path / "live-f")
    _write_wav(prepared)
    got.clear()
    finalize_live(prepared, _live_tracks(), retitle=False, on_transcript=got.append)
    assert got == []


def test_model_from_unsupported_country_is_asked_once_per_step(monkeypatch, tmp_path) -> None:
    """The active model refuses requests from this country or network (Gemini 400
    FAILED_PRECONDITION): no retries, no separate title request (same refusal); one remark,
    suggesting another provider."""

    class Region(Exception):
        status_code = 400

    asked: list[str] = []

    def cloud(system, content, **k):
        asked.append("summary" if "JSON" in system else "title")
        raise Region(
            "litellm.BadRequestError: Vertex_ai_betaException BadRequestError - b'{\\n  "
            '"error": {\\n    "code": 400,\\n    "message": "User location is not supported '
            'for the API use.",\\n    "status": "FAILED_PRECONDITION"\\n  }\\n}\\n\''
        )

    _patch_build(monkeypatch, {})
    _long_speech(monkeypatch)
    _cloud_model(monkeypatch, cloud)
    prepared = build_live(LiveConfig(), base=tmp_path / "live-x")
    _write_wav(prepared)
    result, _ = finalize_live(prepared, _live_tracks())

    assert asked == ["summary"]
    assert result.transcript_txt.parent.name == "live-x" and result.summary_md is None
    [remark] = result.remarks
    assert remark.cause == (
        "Модель claude-sonnet-5 не ответила: сервис модели не принимает запросы из вашей страны "
        "или сети"
    )
    assert remark.hint.startswith("Выберите другого провайдера на сервере")
    assert "Запись названа по времени" in remark.effect
    assert menu_lines(result.remarks)[0] == "Модель недоступна в вашей стране"


def _long_speech(monkeypatch, lang: str = "ru") -> str:
    """Recognition (the dispatch boundary) gives speech longer than the summary threshold."""
    phrase = {
        "ru": "Обсудили сроки релиза и кто что берёт.",
        "en": "We agreed on the release date and who takes what.",
    }[lang]
    speech = " ".join([phrase] * 20)
    segments = [{"start": 0.0, "end": 60.0, "text": speech}]
    _fake_dispatch(monkeypatch, {"text": speech, "segments": segments, "language": lang})
    return speech


def test_interrupted_processing_stays_unfinished_until_stopped_or_done(tmp_path) -> None:
    """Interrupted processing (the work file holds a node request, the computer shut down): the
    folder is among the unprocessed and "Not processed" in the feed, except the one processing
    now. Stopped processing keeps the earlier outcome: text without an outcome in the history
    is processed and not unprocessed; without text it is "audio only"."""
    from mnema.bar.recordings import library
    from mnema.live import meta

    folder = tmp_path / "2026-10-01_10-05"
    folder.mkdir()
    (folder / "mic.wav").write_bytes(b"\0" * 32044)
    (folder / "transcript.txt").write_text("mic: релиз\n", encoding="utf-8")
    work = {"node": "http://node:8000", "op": "recording", "params": {}, "inputs": {}}
    (folder / setup_mod.PROCESSING_MARK).write_text(json.dumps(work), encoding="utf-8")

    assert setup_mod.unfinished_sessions(tmp_path) == [folder]
    assert setup_mod.unfinished_sessions(tmp_path, exclude=folder) == []
    [card] = library(tmp_path).cards
    assert card.state is meta.RecordState.UNPROCESSED and card.has_text

    setup_mod.keep_audio_only(folder, started="2026-10-01T10:10:00+03:00")
    assert setup_mod.unfinished_sessions(tmp_path) == []
    [card] = library(tmp_path).cards
    assert card.state is meta.RecordState.PROCESSED

    (folder / "transcript.txt").unlink()
    assert setup_mod.unfinished_sessions(tmp_path) == []
    [card] = library(tmp_path).cards
    assert card.state is meta.RecordState.AUDIO_ONLY


@pytest.mark.parametrize("missing", ["model", "key"])
def test_no_model_skips_summary_without_failure_in_history(monkeypatch, tmp_path, missing) -> None:
    """Without a configured model or key the automatic steps do not start or record a refusal."""
    _patch_build(monkeypatch, {})
    _long_speech(monkeypatch)
    monkeypatch.delenv("MNEMA_MODEL" if missing == "model" else "MNEMA_API_KEY")
    _cloud_model(monkeypatch, lambda *a: pytest.fail("без модели нет сетевого запроса"))
    prepared = build_live(LiveConfig(), base=tmp_path / "2026-10-02_13-05")
    _write_wav(prepared)
    messages = []
    result, full = finalize_live(
        prepared, _live_tracks(), on=lambda _f, message: messages.append(message)
    )
    assert full and result.summary_md is None and prepared.writers["mic"].path.exists()
    assert result.transcript_txt.parent.name == "2026-10-02_13-05"
    assert result.remarks == []
    assert STEP_MESSAGES["summary"] not in messages and setup_mod.TITLE_MESSAGE not in messages
    [run] = json.loads(
        (result.transcript_txt.parent / ".mnema-meta.json").read_text(encoding="utf-8")
    )["runs"]
    assert run["settings"]["source"] == ("unconfigured" if missing == "model" else "env")
    assert "local_model" not in run["settings"]
    assert run["summary"] is None and not {"summary", "title"}.intersection(run["steps"])
    assert run["status"] == "ok" and run["notes"] == [] and run["remarks"] == []


def test_no_model_accepts_manual_title_without_cloud(monkeypatch, tmp_path) -> None:
    _patch_build(monkeypatch, {})
    _no_llm(monkeypatch)
    _long_speech(monkeypatch)
    _cloud_model(monkeypatch, lambda *a: pytest.fail("ручное имя не требует облачной модели"))
    prepared = build_live(LiveConfig(), base=tmp_path / "2026-10-02_13-05")
    _write_wav(prepared)
    result, _ = finalize_live(prepared, _live_tracks(), user_title=lambda: "Сроки релиза")
    assert result.transcript_txt.parent == tmp_path / "2026-10-02_Сроки релиза"
    assert result.remarks == [] and result.summary_md is None


def _processed(tmp_path, name: str, text: str = "mic: Обсудили сроки релиза.\n") -> Path:
    """A processed recording folder: transcript and a history where the shared track did not
    mix and the model did not answer for the title and summary."""
    from mnema.live import meta, remarks
    from mnema.llm.errors import ErrorKind
    from mnema.llm.notes import Cloud, LightNote

    base = tmp_path / name
    base.mkdir()
    (base / "transcript.txt").write_text(text, encoding="utf-8")
    note = LightNote(
        engine="none", cloud=Cloud.FAILED, kind=ErrorKind.DAILY_LIMIT, model="gemini-flash-latest"
    )
    notes = [
        remarks.mix_failed("ffmpeg упал"),
        remarks.light(Step.TITLE, note),
        remarks.light(Step.SUMMARY, note),
    ]
    run = meta.Run(status="ok", notes=[n for n in notes if n], info=["Запись обрывалась"])
    meta.append(base, run)
    return base


def _summary_model(monkeypatch, answer) -> list[str]:
    """The active model at the boundary (litellm.completion): answer() → reply or an exception."""
    import litellm

    sent: list[str] = []

    def completion(**kw):
        sent.append(kw["messages"][1]["content"])
        out = answer()
        if isinstance(out, BaseException):
            raise out
        delta = types.SimpleNamespace(content=out)
        choice = types.SimpleNamespace(delta=delta, finish_reason="stop")
        return iter([types.SimpleNamespace(choices=[choice])])

    monkeypatch.setattr(litellm, "completion", completion)
    return sent


_SUMMARY = json.dumps(
    {"title": "Созвон по релизу", "about": "О сроках.", "summary": ["Релиз в пятницу."]},
    ensure_ascii=False,
)


def test_summarize_folder_writes_summary_and_renames_default_folder(monkeypatch, tmp_path) -> None:
    """Short text (below the processing threshold) still gets a summary: summary.txt, the default
    folder name renamed, title and summary remarks gone, the others kept; the history gets
    Run(kind="summary")."""
    from mnema.live import meta

    base = _processed(tmp_path, "2026-10-02_13-05")
    sent = _summary_model(monkeypatch, lambda: _SUMMARY)
    moved: list[Path] = []
    result = setup_mod.summarize_folder(base, lang="ru", on_base=moved.append)

    new = tmp_path / "2026-10-02_Созвон по релизу"
    assert result.base == new and moved == [new] and not base.exists()
    assert sent == ["Я: Обсудили сроки релиза."]
    md = (new / "summary.txt").read_text(encoding="utf-8")
    assert md.startswith("О сроках.\n\n- Релиз в пятницу.")
    assert [r.step for r in result.remarks] == [Step.MIX] and result.info == ["Запись обрывалась"]
    last = meta.last(new)
    assert last is not None and (last.kind, last.status) == ("summary", "ok")
    assert [r.step for r in last.notes] == [Step.MIX]


def test_summarize_folder_model_refused_keeps_folder_and_replaces_remark(
    monkeypatch, tmp_path
) -> None:
    class Money(Exception):
        status_code = 402

    base = _processed(tmp_path, "2026-10-02_13-05")
    _summary_model(monkeypatch, lambda: Money("Insufficient Balance"))
    result = setup_mod.summarize_folder(base, lang="ru")
    assert result.base == base and not (base / "summary.txt").exists()
    assert result.summary_md is None
    assert [r.step for r in result.remarks if r.step is Step.MIX] == [Step.MIX]
    [money] = [r for r in result.remarks if r.step is not Step.MIX]
    assert money.cause == "Модель claude-sonnet-5 не ответила: закончились деньги на счёте"
    assert "Запись названа по времени" in money.text


def test_summary_without_points_is_a_remark_not_silence(monkeypatch, tmp_path) -> None:
    """The model answered not in JSON, so no points could be read: no summary.txt, and a remark
    says so; the summary does not vanish silently."""
    base = _processed(tmp_path, "2026-10-02_13-05")
    _summary_model(monkeypatch, lambda: "{")
    result = setup_mod.summarize_folder(base, lang="ru")
    assert result.summary_md is None and not (result.base / "summary.txt").exists()
    [remark] = [r for r in result.remarks if r.step is Step.SUMMARY]
    assert remark.text == (
        "Модель ответила не по формату. Пересказа нет. Нажмите «Составить пересказ» в меню записи."
    )
    assert "без пересказа" in menu_lines(result.remarks)[-1]


def test_summarize_folder_without_transcript_and_when_stopped(monkeypatch, tmp_path) -> None:
    """No transcript: a clear error; stopped before writing: the folder is unchanged."""
    empty = tmp_path / "2026-10-02_10-00"
    empty.mkdir()
    with pytest.raises(MediaError, match="Расшифровки нет"):
        setup_mod.summarize_folder(empty, lang="ru")

    base = _processed(tmp_path, "2026-10-02_13-05")
    before = sorted(p.name for p in base.iterdir())
    meta_before = (base / ".mnema-meta.json").read_text(encoding="utf-8")
    _summary_model(monkeypatch, lambda: _SUMMARY)

    def stop(fraction, message):
        raise setup_mod.FinalizeCancelled

    with pytest.raises(setup_mod.FinalizeCancelled):
        setup_mod.summarize_folder(base, lang="ru", on=stop)
    assert base.exists() and sorted(p.name for p in base.iterdir()) == before
    assert (base / ".mnema-meta.json").read_text(encoding="utf-8") == meta_before


def test_summarize_folder_names_part_of_recording_with_its_mark(monkeypatch, tmp_path) -> None:
    """A part of a recording with a time name gets a title as in processing, keeping the range
    mark; a part with its own name is not renamed."""
    base = _processed(tmp_path, "2026-10-02_13-05 (0.10–1.35)")
    _summary_model(monkeypatch, lambda: _SUMMARY)
    result = setup_mod.summarize_folder(base, lang="ru")
    assert result.base == tmp_path / "2026-10-02_Созвон по релизу (0.10–1.35)"
    assert result.base.is_dir() and not base.exists()
    named = _processed(tmp_path, "Звонок с Аней (0.10–1.35)")
    assert setup_mod.summarize_folder(named, lang="ru").base == named


def test_cloud_context_failure_still_names_folder(monkeypatch, tmp_path) -> None:
    """The summary does not fit the cloud model's context; a title from the start still may."""
    _patch_build(monkeypatch, {})
    speech = " ".join(["Обсудили сроки релиза и кто что берёт."] * 160)
    segments = [{"start": 0.0, "end": 60.0, "text": speech}]
    _fake_dispatch(monkeypatch, {"text": speech, "segments": segments, "language": "ru"})
    asked: list[int] = []

    def answer(system, user):
        asked.append(len(user))
        if len(user) > 4000:
            raise RuntimeError("maximum context length exceeded")
        return "Созвон о релизе"

    _cloud_model(monkeypatch, answer)
    prepared = build_live(LiveConfig(), base=tmp_path / "2026-10-02_13-05")
    _write_wav(prepared)
    result, _ = finalize_live(prepared, _live_tracks())
    assert result.summary_md is None and len(asked) == 2 and asked[1] < asked[0]
    assert result.transcript_txt.parent.name == "2026-10-02_Созвон о релизе"


@pytest.mark.parametrize(
    ("asr", "lang", "backend"),
    [
        ("auto", "ru", None),
        ("whisper", "ru", "whisper"),
        ("gigaam", "ru", "gigaam"),
        ("gigaam", "en", "whisper"),
        ("parakeet", "ru", "parakeet"),
        ("parakeet", "en", "parakeet"),
        ("parakeet", "ja", "whisper"),
    ],  # fmt: skip
)
def test_chosen_speech_model_goes_to_recognition(monkeypatch, tmp_path, asr, lang, backend) -> None:
    """The chosen recognition model goes to the engine: Whisper for any language, GigaAM only
    for Russian, Parakeet for its 25 languages (another language: Whisper), "Automatic" with
    no choice (the core decides)."""
    _patch_build(monkeypatch, {})
    calls: list = []
    _fake_dispatch(monkeypatch, calls=calls)
    prepared = build_live(LiveConfig(asr=asr, lang=lang), base=tmp_path / "live-x")
    _write_wav(prepared)
    finalize_live(prepared, _live_tracks(), retitle=False)
    [call] = [c for c in calls if c["op"] == "transcribe"]
    assert call["params"].get("backend") == backend


def test_cloud_speech_model_without_key_is_a_remark(monkeypatch, tmp_path) -> None:
    """A cloud recognition model without a key gives a track remark (the server refuses such a
    job earlier: ops.admit); no local engine is silently substituted."""
    _patch_build(monkeypatch, {})
    monkeypatch.delenv("MNEMA_ASR_API_KEY", raising=False)
    import litellm

    monkeypatch.setattr(litellm, "transcription", lambda **kw: pytest.fail("no key, no request"))
    prepared = build_live(
        LiveConfig(asr="openai/gpt-4o-transcribe", lang="ru"), base=tmp_path / "x"
    )
    _write_wav(prepared)
    result, _ = finalize_live(prepared, _live_tracks(), retitle=False)
    [remark] = [r for r in result.remarks if r.step == Step.ASR]
    assert remark.cause == "Не удалось распознать дорожку «Микрофон»: сбой на сервере"


def test_reprocessing_preserves_old_summary(monkeypatch, tmp_path):
    from mnema.bar.recordings import library

    _patch_build(monkeypatch, {})
    _long_speech(monkeypatch)
    _no_llm(monkeypatch)
    prepared = build_live(LiveConfig(lang="ru"), base=tmp_path / "meeting")
    _write_wav(prepared)
    (prepared.base / "transcript.txt").write_text("old transcript")
    previous = "Old summary\n\n- Old decision\n"
    (prepared.base / "summary.txt").write_text(previous)
    result, full = finalize_live(prepared, _live_tracks(), retitle=False)
    assert full and result.transcript_txt.read_text() != "old transcript"
    assert (prepared.base / "summary.txt").read_text() == previous
    assert result.summary_md is None
    [card] = library(tmp_path).cards
    assert card.has_summary


def test_recording_failure_is_visible_in_saved_result(monkeypatch, tmp_path):
    _patch_build(monkeypatch, {})
    _fake_dispatch(monkeypatch)
    _no_llm(monkeypatch)
    prepared = build_live(LiveConfig(lang="ru"), base=tmp_path / "meeting")
    _write_wav(prepared)
    prepared.recording_error = "mic.wav: No space left on device"
    result, text = finalize_live(prepared, _live_tracks(), retitle=False)
    assert text and result.transcript_txt.exists()
    [notice] = [r for r in result.remarks if r.step == Step.RECORD]
    assert notice.short == "Запись неполная" and "No space" in notice.cause
    saved = json.loads((prepared.base / ".mnema-meta.json").read_text())
    assert any("No space" in remark for remark in saved["runs"][-1]["remarks"])


def test_recording_failure_survives_cancelled_processing(monkeypatch, tmp_path):
    _patch_build(monkeypatch, {})
    _no_llm(monkeypatch)
    prepared = build_live(LiveConfig(lang="ru", runner="https://runner"), base=tmp_path / "meeting")
    _write_wav(prepared)
    prepared.recording_error = "mic.wav: No space left on device"

    def cancel(*args):
        raise setup_mod.FinalizeCancelled()

    with pytest.raises(setup_mod.FinalizeCancelled):
        finalize_live(prepared, _live_tracks(), retitle=False, on=cancel)
    saved = json.loads((prepared.base / ".mnema-meta.json").read_text())["runs"][-1]
    assert any("No space" in remark for remark in saved["remarks"])
    assert any(n["step"] == "record" for n in saved["notes"])


@pytest.mark.parametrize("runner", [None, "https://unavailable-runner.invalid"])
def test_cloud_asr_does_not_depend_on_runner_or_local_language_model(monkeypatch, tmp_path, runner):
    """Cloud recognition is an engine run where the recording is processed, with that
    machine's key: never sent on to a runner."""
    _patch_build(monkeypatch, {})
    _no_llm(monkeypatch)
    calls: list = []
    _fake_dispatch(monkeypatch, calls=calls)
    prepared = build_live(
        LiveConfig(asr="openai/gpt-4o-transcribe", runner=runner, summary=False),
        base=tmp_path / "recording",
    )
    _write_wav(prepared)
    result, full = finalize_live(prepared, _live_tracks(), retitle=False)
    assert [(c["op"], c["params"], c["runner"], c["local"]) for c in calls] == [
        ("transcribe", {"backend": "openai/gpt-4o-transcribe"}, None, True)
    ]
    assert result.transcript_txt.is_file()
    assert not result.defer_command
    assert not any(r.step is Step.SERVER for r in result.remarks)


def test_processed_recording_is_still_a_mnema_folder(tmp_path) -> None:
    """Tracks and processing results are Mnema's own files: a part may replace the full
    recording; a foreign file in the folder prevents it."""
    from mnema.live.setup import is_recording_folder

    for name in ("mic.wav", "transcript.txt", "summary.txt", ".mnema-meta.json"):
        (tmp_path / name).write_text("x")
    assert is_recording_folder(tmp_path)
    (tmp_path / "заметки.txt").write_text("чужое")
    assert not is_recording_folder(tmp_path)


def test_no_save_counts_only_real_recognition_failures(tmp_path) -> None:
    """rec --no-save: a track without speech (noise, music) is not a failure, the text is
    returned; a recognition failure or deferred processing is a failure."""
    from pathlib import Path

    from mnema.cli.commands import _no_save_failure
    from mnema.live import remarks
    from mnema.live.models import LiveResult

    done = Path("transcript.txt")
    quiet = LiveResult(
        transcript_txt=done, remarks=[remarks.asr_no_speech("system", live_kept=False)]
    )
    assert _no_save_failure(quiet) is None
    failed = LiveResult(
        transcript_txt=done,
        remarks=[remarks.asr_failed("system", "нет памяти", live_kept=False)],
    )
    from mnema.live import meta

    failed.base = tmp_path
    meta.append(
        tmp_path, meta.Run(status="failed", tracks={"system": meta.TrackRun(error="failed")})
    )
    assert _no_save_failure(failed) == "часть записи не распознана"
    assert _no_save_failure(LiveResult(transcript_txt=done, defer_command="mnema finish x"))


_CYRILLIC = re.compile("[А-Яа-яЁё]")


def _silent_wav(path: Path, seconds: float) -> None:
    import wave

    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * int(16000 * seconds))


def test_session_texts_in_english(monkeypatch, tmp_path) -> None:
    """In English the session status and the errors people see have no Russian."""
    _patch_build(monkeypatch, {})
    with using("en"):
        prepared = build_live(LiveConfig(stream=False), base=tmp_path / "live-x")
        texts = [prepared.status]
        for call in (
            lambda: build_live(LiveConfig(mic=False), base=tmp_path / "y"),
            lambda: build_live(LiveConfig(), base=tmp_path / "z", connection=None),
            lambda: setup_mod.parse_offset(""),
            lambda: setup_mod.parse_offset("полчаса"),
            lambda: setup_mod.parse_offset("1:75"),
            lambda: setup_mod.summarize_folder(tmp_path / "live-x", lang="en"),
        ):
            with pytest.raises(MediaError) as e:
                call()
            texts.append(str(e.value))
        with pytest.raises(ValueError) as bad:
            LiveConfig(translate="xx")
        texts.append(str(bad.value))
    assert prepared.status.startswith("recording without live text")
    assert texts[4] == (
        "Couldn't read the time “полчаса”: use hours:minutes:seconds (1:05:30),"
        " “1h 35m” or “35 min”"
    )
    assert not [t for t in texts if _CYRILLIC.search(t.replace("полчаса", ""))]


def test_part_of_recording_in_english(tmp_path) -> None:
    """A part of a recording in English: its history notes and range errors have no Russian."""
    base = tmp_path / "2026-10-21_08-35"
    base.mkdir()
    _silent_wav(base / "mic.wav", 6.0)
    with using("en"):
        prepared, label, work = setup_mod.prepare_recovery(base, LiveConfig(), start=1.0, end=4.0)
        errors = []
        for start, end in ((10.0, None), (4.0, 2.0)):
            with pytest.raises(MediaError) as e:
                setup_mod.prepare_recovery(base, LiveConfig(), start=start, end=end)
            errors.append(str(e.value))
        with pytest.raises(MediaError) as empty:
            setup_mod.prepare_recovery(tmp_path / "nothing", LiveConfig())
    assert label and work != base
    assert prepared.info == ["Part of the recording 0:00:01–0:00:04 of 0:00:06"]
    assert errors == [
        "Start 0:00:10 is after the end of the recording (0:00:06)",
        "The end of the range is before its start",
    ]
    assert str(empty.value) == "nothing has no recorded audio"


def test_track_progress_in_english(monkeypatch, tmp_path) -> None:
    """In English the offline pass names the track it recognizes, and an engine's generic
    "Recognizing…" becomes that name."""
    _patch_build(monkeypatch, {})
    _no_llm(monkeypatch)

    def fake(op, src, params, *, runner=None, local=False, on=None):
        if on is not None:
            on(0.5, "Recognizing…")
        return dict(_OFFLINE)

    monkeypatch.setattr(setup_mod, "dispatch", fake)
    prepared = build_live(LiveConfig(), base=tmp_path / "live-x")
    _write_wav(prepared)
    messages: list[str] = []
    with using("en"):
        finalize_live(prepared, _live_tracks(), retitle=False, on=lambda f, m: messages.append(m))
    assert "Recognizing the microphone…" in messages
    assert "Recognizing…" not in messages
