"""Cloud recognition is an engine: silero-vad cuts the track into utterances, each one is a single
OpenAI request with the track's previous text as a prompt. The boundaries are
litellm.transcription, silero-vad and torch."""

from __future__ import annotations

import importlib.util
import io
import sys
import threading
import types
import wave

import httpx
import litellm
import numpy as np
import pytest

from mnema.cli.parser import build_parser
from mnema.core import backends
from mnema.core.base import MediaError
from mnema.core.models import Transcript
from mnema.live import remarks

SR = 16000
KEY = "sk-proj-cloud-engine-test"


@pytest.fixture
def speech(monkeypatch):
    """silero-vad finds the given speech spans (seconds) in any audio."""
    spans: list[tuple[float, float]] = []
    torch = types.SimpleNamespace(
        from_numpy=np.asarray, get_num_threads=lambda: 1, set_num_threads=lambda n: None
    )
    vad = types.SimpleNamespace(
        load_silero_vad=lambda: object(),
        get_speech_timestamps=lambda *a, **k: [
            {"start": int(s * SR), "end": int(e * SR)} for s, e in spans
        ],
    )
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "silero_vad", vad)
    return spans


@pytest.fixture
def sent(monkeypatch):
    """Requests to OpenAI: their arguments and the length of the WAV sent; the answer text is
    the request number unless the test sets an answer by number or by the length sent."""
    calls: list[dict] = []
    answers: dict[int, object] = {}
    by_length: dict[float, object] = {}
    lock = threading.Lock()

    def transcription(**kw):
        _name, data = kw["file"]
        with wave.open(io.BytesIO(data)) as w:
            seconds = w.getnframes() / SR
        with lock:
            calls.append({"seconds": seconds, **kw})
            number = len(calls)
        answer = by_length.get(seconds, answers.get(number, f"реплика {number}"))
        if isinstance(answer, Exception):
            raise answer
        return answer(seconds) if callable(answer) else {"text": answer}

    monkeypatch.setattr(litellm, "transcription", transcription)
    return types.SimpleNamespace(calls=calls, answers=answers, by_length=by_length)


def _track(path, seconds: float):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(b"\0\0" * int(seconds * SR))
    return path


def _recognize(wav, *, model="openai/gpt-4o-transcribe", lang=None) -> Transcript:
    return backends.CloudBackend(model, key=KEY).run(wav, lang=lang)


def test_each_utterance_is_one_request_and_keeps_its_time(tmp_path, speech, sent) -> None:
    """Speech with pauses up to 0.6 s is one utterance sent whole, however long; requests go at
    the same time and the replies come back in the order of the recording."""
    speech += [(1, 20), (20.3, 45), (50, 52)]
    second_asked = threading.Event()

    def first(seconds):
        assert second_asked.wait(timeout=10), "the second utterance waited for the first"
        return {"text": "Первая длинная реплика."}

    def second(seconds):
        second_asked.set()
        return {"text": "Вторая."}

    sent.by_length.update({44.0: first, 2.0: second})
    tr = _recognize(_track(tmp_path / "mic.wav", 60))
    assert sorted(c["seconds"] for c in sent.calls) == [2.0, 44.0]
    assert {(c["model"], c["api_key"], c["language"]) for c in sent.calls} == {
        ("openai/gpt-4o-transcribe", KEY, None)
    }
    assert [(s.start, s.end, s.text) for s in tr.segments] == [
        (1.0, 45.0, "Первая длинная реплика."),
        (50.0, 52.0, "Вторая."),
    ]
    assert tr.text == "Первая длинная реплика. Вторая."


def test_a_long_utterance_goes_in_parts_with_the_text_before_as_prompt(
    tmp_path, speech, sent
) -> None:
    """A 12-minute utterance is two requests of at most 10 minutes split at a pause; the second
    one's prompt is the last 200 words of the first; the chosen language goes to every part."""
    speech += [(0, 300), (300.3, 720)]
    words = [f"слово{i}" for i in range(250)]
    sent.answers[1] = " ".join(words)
    wav = _track(tmp_path / "mic.wav", 721)
    tr = _recognize(wav, model="openai/gpt-4o-mini-transcribe", lang="ru")
    first, second = sent.calls
    assert (first["seconds"], second["seconds"]) == (300.0, 419.7)
    assert not first.get("prompt")
    assert second["prompt"] == " ".join(words[-200:])
    assert {c["language"] for c in sent.calls} == {"ru"}
    assert {c["model"] for c in sent.calls} == {"openai/gpt-4o-mini-transcribe"}
    assert [(s.start, s.end) for s in tr.segments] == [(0.0, 300.0), (300.3, 720.0)]
    assert tr.language == "ru"


def test_a_busy_service_is_asked_again(tmp_path, speech, sent) -> None:
    speech += [(1, 3)]
    busy = httpx.Response(503, headers={"retry-after": "0"}, request=httpx.Request("POST", "x"))
    sent.answers[1] = litellm.ServiceUnavailableError(
        message="overloaded", llm_provider="openai", model="gpt-4o-transcribe", response=busy
    )
    tr = _recognize(_track(tmp_path / "mic.wav", 4))
    assert len(sent.calls) == 2 and tr.text == "реплика 2"


def test_a_rejected_key_is_a_track_remark_in_the_users_words(tmp_path, speech, sent) -> None:
    """401: no retry, no other engine; the remark says why, also after the error crossed the
    recognition process pipe as text."""
    speech += [(1, 3)]
    sent.answers[1] = litellm.AuthenticationError(
        message="Incorrect API key provided", llm_provider="openai", model="gpt-4o-transcribe"
    )
    with pytest.raises(litellm.AuthenticationError) as refused:
        _recognize(_track(tmp_path / "mic.wav", 4))
    assert len(sent.calls) == 1
    expected = "Не удалось распознать дорожку «Микрофон»: ключ модели не подошёл"
    for error in (refused.value, MediaError(str(refused.value))):
        assert remarks.asr_failed("mic", error, live_kept=False).cause == expected


def test_no_key_is_refused_before_any_request(tmp_path, speech, sent) -> None:
    speech += [(1, 3)]
    with pytest.raises(MediaError) as refused:
        backends.select_backend("openai/gpt-4o-transcribe").run(_track(tmp_path / "mic.wav", 4))
    assert str(refused.value) == (
        "Нет ключа OpenAI для распознавания. Задайте его в настройках Mnema на Mac"
    )
    assert sent.calls == []


def test_automatic_choice_never_goes_to_the_cloud(monkeypatch) -> None:
    """Even as the only engine that works here, the cloud is never chosen automatically nor
    listed among this machine's engines; the CLI takes both OpenAI models by name."""
    find = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda n, *a, **k: object() if n == "silero_vad" else find(n)
    )
    monkeypatch.setattr(backends, "ENGINES", (backends.CloudBackend,))
    with pytest.raises(MediaError):
        backends.select_backend("auto", lang="ru")
    assert backends.installed_engines() == []
    for model in backends.CLOUD_MODELS:
        args = build_parser().parse_args(["transcribe", "call.mp3", "--backend", model])
        assert backends.select_backend(args.backend).name == model
