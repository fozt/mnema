"""Cloud recognition (llm/asr): chunks at pauses, segment times, the key is MNEMA_API_KEY of the
same provider or MNEMA_ASR_API_KEY. The boundary is litellm.transcription and silero-vad."""

from __future__ import annotations

import types
import wave

import pytest

from mnema import config
from mnema.core.base import MediaError
from mnema.llm import asr

SR = 16000


def _speech(*spans: tuple[float, float]) -> list[dict]:
    return [{"start": int(s * SR), "end": int(e * SR)} for s, e in spans]


def test_whisper_models_get_long_chunks_others_one_reply_each() -> None:
    """Whisper models get up to 5 minutes in a row (they return timed segments); others up to 30 s,
    with a new segment after a pause longer than 1.5 s."""
    speech = _speech((0, 10), (12, 20), (200, 250), (320, 330))
    assert asr.chunks(speech, timed=True) == [(0, 250 * SR), (320 * SR, 330 * SR)]
    short = _speech((0, 10), (10.5, 20), (22, 25), (25.5, 40))
    assert asr.chunks(short, timed=False) == [(0, 20 * SR), (22 * SR, 40 * SR)]
    long = _speech((0, 20), (20.5, 35))  # together longer than 30 s
    assert asr.chunks(long, timed=False) == [(0, 20 * SR), (20 * SR + SR // 2, 35 * SR)]


def test_replies_are_shifted_to_recording_time() -> None:
    """Chunk segments are in recording time (shifted to the chunk start, never past its end);
    without timed segments the whole chunk is one segment; an empty answer gives nothing."""
    answer = {"segments": [{"start": 0.5, "end": 2.0, "text": " Привет. "},
                           {"start": 2.0, "end": 9.0, "text": ""}], "text": "Привет."}  # fmt: skip
    [one] = asr.segments(answer, 60.0, 70.0, timed=True)
    assert (one.start, one.end, one.text) == (60.5, 62.0, "Привет.")
    obj = types.SimpleNamespace(start=1.0, end=99.0, text="Длинная")
    [late] = asr.segments({"segments": [obj]}, 10.0, 20.0, timed=True)
    assert (late.start, late.end) == (11.0, 20.0)
    [whole] = asr.segments({"text": "Да, давай."}, 5.0, 8.0, timed=False)
    assert (whole.start, whole.end, whole.text) == (5.0, 8.0, "Да, давай.")
    assert asr.segments({"text": " "}, 5.0, 8.0, timed=False) == []


GROQ_KEY = "gsk_" + "g" * 48 + "Gq09"


def test_key_of_the_model_provider_or_the_asr_key(monkeypatch) -> None:
    """The recognition provider of MNEMA_MODEL uses MNEMA_API_KEY; another one uses
    MNEMA_ASR_API_KEY (from .env without a restart); none gives "No Groq key"."""
    with pytest.raises(MediaError, match="^Нет ключа Groq. Выберите другое распознавание$"):
        asr.credentials("groq/whisper-large-v3")  # the model is Claude, no Groq key set
    monkeypatch.setenv("MNEMA_MODEL", "groq/openai/gpt-oss-120b")
    monkeypatch.setenv("MNEMA_API_KEY", GROQ_KEY)
    assert asr.credentials("groq/whisper-large-v3") == ("groq/whisper-large-v3", GROQ_KEY, None)
    with pytest.raises(MediaError, match="^Нет ключа OpenAI. Выберите другое распознавание$"):
        asr.credentials("openai/whisper-1")  # a Groq key never goes to OpenAI
    monkeypatch.setenv("MNEMA_MODEL", "anthropic/claude-sonnet-5")
    monkeypatch.setenv("MNEMA_API_KEY", "sk-ant-not-for-groq")
    env = config.user_env_file()
    env.parent.mkdir(parents=True, exist_ok=True)
    env.write_text(f"MNEMA_ASR_API_KEY={GROQ_KEY}\n")
    assert asr.credentials("groq/whisper-large-v3")[1] == GROQ_KEY
    with pytest.raises(MediaError, match="^Нет ключа OpenAI. Выберите другое распознавание$"):
        asr.credentials("openai/whisper-1")  # a Groq-looking key is for Groq only
    with pytest.raises(MediaError, match="Неизвестная модель распознавания"):
        asr.credentials("gemini/gemini-flash-latest")


def test_track_is_sent_in_pieces_and_comes_back_in_order(monkeypatch, tmp_path) -> None:
    """The track goes as WAV chunks one by one in the decided language; segments in record time."""
    pytest.importorskip("torch")
    import silero_vad

    wav = tmp_path / "mic.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(b"\0\0" * SR * 400)
    monkeypatch.setattr(silero_vad, "load_silero_vad", lambda: object())
    monkeypatch.setattr(
        silero_vad, "get_speech_timestamps", lambda *a, **k: _speech((1, 4), (350, 352))
    )
    sent: list[dict] = []

    def transcription(**kw):
        name, data = kw["file"]
        with wave.open(__import__("io").BytesIO(data)) as w:
            sent.append(
                {
                    "seconds": w.getnframes() / SR,
                    **{k: kw[k] for k in ("model", "language", "api_key")},
                }
            )
        return {"segments": [{"start": 0.0, "end": 1.0, "text": f"кусок {len(sent)}"}]}

    import litellm

    monkeypatch.setattr(litellm, "transcription", transcription)
    tr = asr._transcribe(wav, "groq/whisper-large-v3-turbo", key="gsk_test", lang="ru")
    assert [x["seconds"] for x in sent] == [3.0, 2.0]
    assert {x["model"] for x in sent} == {"groq/whisper-large-v3-turbo"}
    assert {x["language"] for x in sent} == {"ru"}
    assert {x["api_key"] for x in sent} == {"gsk_test"}
    assert [(s.start, s.text) for s in tr.segments] == [(1.0, "кусок 1"), (350.0, "кусок 2")]
    assert tr.engine == "groq/whisper-large-v3-turbo"
