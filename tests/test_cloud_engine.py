"""Cloud recognition is an engine: this machine's speech search cuts the track into windows, each
one is a single OpenAI request. The boundaries are litellm.transcription and this machine's
recognizer (on the server, the silero session)."""

from __future__ import annotations

import importlib.util
import io
import threading
import types
import wave

import httpx
import litellm
import pytest

from mnema.cli.parser import build_parser
from mnema.core import asr_engine, backends
from mnema.core.base import MediaError
from mnema.core.models import Transcript
from mnema.live import remarks
from tests import onnx_stubs

SR = 16000
KEY = "sk-proj-cloud-engine-test"


@pytest.fixture
def speech(monkeypatch):
    """This machine's speech search finds the given spans (seconds) in any track."""
    spans: list[tuple[float, float]] = []

    class Recognizer:
        def speech(self, wav, *, max_s):
            return [(int(s * SR), int(e * SR)) for s, e in spans]

    monkeypatch.setattr(asr_engine, "local_recognizer", Recognizer)
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


RUSSIAN = " ".join(["сегодня обсудили план"] * 15)
GERMAN = " ".join(["wir können das morgen besprechen"] * 15)


def test_each_window_is_one_request_and_keeps_its_time(tmp_path, speech, sent) -> None:
    """Speech across a short pause is one window sent whole; requests go at the same time and
    the windows come back in the order of the recording."""
    speech += [(1, 6), (7, 12), (20, 22)]
    second_asked = threading.Event()

    def first(seconds):
        assert second_asked.wait(timeout=10), "the second window waited for the first"
        return {"text": "Первая длинная реплика."}

    def second(seconds):
        second_asked.set()
        return {"text": "Вторая."}

    sent.by_length.update({11.0: first, 2.0: second})
    tr = _recognize(_track(tmp_path / "mic.wav", 30))
    assert sorted(c["seconds"] for c in sent.calls) == [2.0, 11.0]
    assert {(c["model"], c["api_key"], c["language"]) for c in sent.calls} == {
        ("openai/gpt-4o-transcribe", KEY, None)
    }
    assert [(s.start, s.end, s.text) for s in tr.segments] == [
        (1.0, 12.0, "Первая длинная реплика."),
        (20.0, 22.0, "Вторая."),
    ]
    assert tr.text == "Первая длинная реплика. Вторая."


@pytest.mark.parametrize(
    ("spans", "windows"),
    [
        ([(0, 1), (2.5, 3)], [(0, 3)]),  # a pause of exactly 1.5 s joins
        ([(0, 1), (2.6, 3)], [(0, 1), (2.6, 3)]),  # a longer one does not
        ([(0, 7), (8, 15)], [(0, 15)]),  # exactly 15 s
        ([(0, 7), (8, 15.5)], [(0, 7), (8, 15.5)]),  # a longer window is not made
        ([(0, 5), (6, 11), (12, 17)], [(0, 11), (12, 17)]),
        ([(0, 40), (41, 42), (43, 44)], [(0, 40), (41, 44)]),  # one long span stays whole
        ([(0, 1), (2, 40)], [(0, 1), (2, 40)]),
    ],
)
def test_neighbours_join_across_a_short_pause_within_the_window(
    tmp_path, speech, sent, spans, windows
) -> None:
    speech += spans
    tr = _recognize(_track(tmp_path / "mic.wav", 45))
    assert len(sent.calls) == len(windows)
    assert [(s.start, s.end) for s in tr.segments] == windows


def test_continuous_speech_goes_in_parts_in_the_chosen_language(tmp_path, speech, sent) -> None:
    """The parts of speech that never pauses are at most 2.5 minutes each, so an answer never
    reaches the model's output limit; the chosen language goes to every part, whatever the text
    says."""
    speech += [(0, 100), (100.3, 200), (200.3, 300)]
    sent.answers.update({1: RUSSIAN, 2: RUSSIAN, 3: RUSSIAN})
    wav = _track(tmp_path / "mic.wav", 301)
    tr = _recognize(wav, model="openai/gpt-4o-mini-transcribe", lang="en")
    assert {c["language"] for c in sent.calls} == {"en"}
    assert {c["model"] for c in sent.calls} == {"openai/gpt-4o-mini-transcribe"}
    assert [(s.start, s.end) for s in tr.segments] == [(0.0, 100.0), (100.3, 200.0), (200.3, 300.0)]
    assert tr.language == "en"


def test_the_model_never_hears_the_text_before(tmp_path, speech, monkeypatch) -> None:
    """A service that echoes whatever prompt it gets: every sentence comes out once."""
    speech += [(i * 3, i * 3 + 1) for i in range(20)]
    numbers = iter(range(1000))
    lock = threading.Lock()

    def transcription(**kw):
        with lock:
            own = f"фраза номер {next(numbers)}."
        return {"text": " ".join(filter(None, [kw.get("prompt"), own]))}

    monkeypatch.setattr(litellm, "transcription", transcription)
    tr = _recognize(_track(tmp_path / "mic.wav", 61))
    sentences = [s.text for s in tr.segments]
    assert len(sentences) == 20
    assert all(s.count("фраза") == 1 for s in sentences)
    assert len(set(sentences)) == 20


def test_later_requests_get_the_language_the_finished_text_names(tmp_path, speech, sent) -> None:
    """Requests already running stay without a language, the ones after them have it; the
    transcript does not claim a language nobody chose."""
    speech += [(i * 3, i * 3 + 1) for i in range(8)]
    running = threading.Barrier(backends._CLOUD_REQUESTS)

    def answer(seconds):
        running.wait(timeout=10)
        return {"text": RUSSIAN}

    sent.answers.update(dict.fromkeys(range(1, running.parties + 1), answer))
    tr = _recognize(_track(tmp_path / "mic.wav", 25))
    languages = [c["language"] for c in sent.calls]
    assert languages == [None] * running.parties + ["ru"] * (8 - running.parties)
    assert tr.language is None


def test_a_text_that_names_no_language_leaves_it_out(tmp_path, speech, sent) -> None:
    speech += [(i * 3, i * 3 + 1) for i in range(8)]
    sent.answers.update(dict.fromkeys(range(1, 9), GERMAN))
    _recognize(_track(tmp_path / "mic.wav", 25))
    assert {c["language"] for c in sent.calls} == {None}


def test_a_few_words_of_tiny_clips_do_not_decide_the_language(tmp_path, speech, sent) -> None:
    """False lines of tiny clips alone name nothing; the language comes from the speech after."""
    speech += [(i * 3, i * 3 + 1) for i in range(12)]
    sent.answers.update(dict.fromkeys(range(1, 7), "Thank you."))
    sent.answers.update(dict.fromkeys(range(7, 13), RUSSIAN))
    _recognize(_track(tmp_path / "mic.wav", 37))
    assert {c["language"] for c in sent.calls} <= {None, "ru"}
    assert sent.calls[-1]["language"] == "ru"


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
        importlib.util, "find_spec", lambda n, *a, **k: object() if n == "onnxruntime" else find(n)
    )
    monkeypatch.setattr(backends, "ENGINES", (backends.CloudBackend,))
    with pytest.raises(MediaError):
        backends.select_backend("auto", lang="ru")
    assert backends.installed_engines() == []
    for model in backends.CLOUD_MODELS:
        args = build_parser().parse_args(["transcribe", "call.mp3", "--backend", model])
        assert backends.select_backend(args.backend).name == model


def test_on_the_server_the_track_is_cut_where_silero_hears_speech(tmp_path, sent, monkeypatch):
    """Without a Mac helper the pauses come from the silero session: one request for the
    speech it heard, timed by its spans."""
    onnx_stubs.install(
        monkeypatch, silero_vad=onnx_stubs.silero(onnx_stubs.speech_probs([(1, 3)], 4))
    )
    tr = _recognize(_track(tmp_path / "mic.wav", 4))
    assert len(sent.calls) == 1 and sent.calls[0]["seconds"] == pytest.approx(2.0, abs=0.1)
    (segment,) = tr.segments
    assert segment.start == pytest.approx(1.0, abs=0.05)
    assert segment.end == pytest.approx(3.0, abs=0.05)


def test_on_the_mac_the_pauses_come_from_mnema_asr(tmp_path, sent, monkeypatch):
    """A Mac without a server: the cloud works where mnema-asr is, cut by its speech segments
    (its small speech model is downloaded the first time)."""
    from mnema.core import base
    from tests.mac_helper_stub import Helper

    monkeypatch.setattr(base, "STANDALONE", True)
    assert not backends.CloudBackend().available()  # no Mnema.app, onnxruntime or not
    helper = Helper(tmp_path / "app", speech=[[SR, 3 * SR]])
    helper.configure(installed={"parakeet": False, "vad": False})
    monkeypatch.setenv("MNEMA_ASR", str(helper.path))
    assert backends.CloudBackend().available()
    tr = _recognize(_track(tmp_path / "mic.wav", 4))
    assert [c["seconds"] for c in sent.calls] == [2.0]
    assert [(s.start, s.end) for s in tr.segments] == [(1.0, 3.0)]
    assert helper.config()["installed"] == {"parakeet": False, "vad": True}
