"""Backend selection and engine routing, without loading models."""

from __future__ import annotations

import pytest

from mnema.core.backends import (
    MlxBackend,
    WhisperXBackend,
    select_backend,
)
from mnema.core.base import MediaError
from mnema.i18n import using


@pytest.mark.parametrize(
    ("name", "cls"),
    [
        ("mlx", MlxBackend),
        ("whisperx", WhisperXBackend),
    ],
)
def test_select_explicit_names(name, cls) -> None:
    assert isinstance(select_backend(name), cls)


def test_unknown_backend_raises() -> None:
    with pytest.raises(MediaError):
        select_backend("does-not-exist")


# ── phased: one progress bar over several phases ──


def test_phased_maps_local_progress_into_weighted_slices() -> None:
    """Each phase maps its local 0..1 into its slice; the bar is monotonic across phases."""
    from mnema.core.base import phased

    calls: list[tuple] = []
    p1, p2 = phased(lambda f, m: calls.append((f, m)), 3.0, 1.0)  # 75% / 25%
    p1(0.0, "a")
    p1(1.0, "a")  # end of phase 1 = 0.75
    p2(0.0, "b")  # start of phase 2 = 0.75 (no gap)
    p2(0.5, "b")
    p2(1.0, "b")  # end = 1.0
    assert calls == [(0.0, "a"), (0.75, "a"), (0.75, "b"), (0.875, "b"), (1.0, "b")]


def test_phased_none_fraction_is_message_only() -> None:
    """fraction=None passes through as is (message only, the bar stays)."""
    from mnema.core.base import phased

    calls: list[tuple] = []
    (p,) = phased(lambda f, m: calls.append((f, m)), 1.0)
    p(None, "loading")
    p(0.5, "half")
    assert calls == [(None, "loading"), (0.5, "half")]


@pytest.mark.parametrize("device", ["cpu", "cuda"])
def test_whisperx_limits_cuda_memory_before_loading_vad(monkeypatch, device) -> None:
    """Without a warm-up the CUDA cap still applies before the VAD pipeline first loads."""
    import sys
    import types

    from mnema.core import backends as b

    events = []
    cuda = types.SimpleNamespace(
        is_available=lambda: True,
        set_per_process_memory_fraction=lambda fraction: events.append(("cap", fraction)),
    )
    monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(cuda=cuda))
    monkeypatch.setattr(b, "_torch_capped", False)
    monkeypatch.setattr(b, "_WHISPERX_ASR", {})

    def load_model(*args, **kwargs):
        events.append(("load", device))
        return object()

    whisperx = types.SimpleNamespace(load_model=load_model)
    compute = "float16" if device == "cuda" else "int8"
    first = b._whisperx_model(whisperx, "small", device, compute)
    assert b._whisperx_model(whisperx, "small", device, compute) is first
    assert events == (
        [("cap", b._TORCH_VRAM_FRACTION), ("load", "cuda")]
        if device == "cuda" else [("load", "cpu")]
    )  # fmt: skip


def test_select_backend_auto_prefers_parakeet_then_gigaam_for_russian(monkeypatch) -> None:
    """AUTO: no language or a Parakeet language → Parakeet; Russian without it → GigaAM; the
    rest → the platform's whisper. An explicit name always works."""
    from mnema.core import backends as b

    monkeypatch.setattr(b.ParakeetBackend, "available", lambda self: True)
    monkeypatch.setattr(b.GigaAmBackend, "available", lambda self: True)
    monkeypatch.setattr(b.MlxBackend, "available", lambda self: True)

    assert isinstance(b.select_backend("auto", lang="ru"), b.ParakeetBackend)
    assert isinstance(b.select_backend("auto"), b.ParakeetBackend)  # no language: Parakeet
    assert isinstance(b.select_backend("auto", lang="en"), b.ParakeetBackend)  # its language
    assert isinstance(
        b.select_backend("auto", lang="ja"), b.MlxBackend
    )  # not its language: whisper
    monkeypatch.setattr(b.ParakeetBackend, "available", lambda self: False)
    assert isinstance(b.select_backend("auto", lang="ru"), b.GigaAmBackend)
    assert isinstance(b.select_backend("gigaam"), b.GigaAmBackend)  # explicit: unconditional
    # GigaAM only for an explicit Russian; otherwise whisper detects the language itself
    assert isinstance(b.select_backend("auto"), b.MlxBackend)
    assert isinstance(b.select_backend("auto", lang="en"), b.MlxBackend)
    monkeypatch.setattr(b.GigaAmBackend, "available", lambda self: False)
    assert isinstance(b.select_backend("auto", lang="ru"), b.MlxBackend)


def test_whisper_choice_skips_gigaam_even_for_russian(monkeypatch) -> None:
    """Choosing Whisper: Russian speech goes to this platform's whisper."""
    from mnema.core import backends as b

    monkeypatch.setattr(b.GigaAmBackend, "available", lambda self: True)
    monkeypatch.setattr(b.MlxBackend, "available", lambda self: True)
    assert isinstance(b.select_backend("whisper", lang="ru"), b.MlxBackend)
    monkeypatch.setattr(b.MlxBackend, "available", lambda self: False)
    monkeypatch.setattr(b.WhisperXBackend, "available", lambda self: True)
    assert isinstance(b.select_backend("whisper", lang="ru"), b.WhisperXBackend)


@pytest.mark.parametrize("installed", [True, False])
def test_apple_silicon_whisper_needs_installed_mlx_whisper(monkeypatch, installed) -> None:
    """Apple Silicon without mlx-whisper: Whisper gives a clear "no engine" error before
    uploads are claimed, not an ImportError inside the recognition process."""
    import importlib.util

    from mnema.core import backends as b

    find = importlib.util.find_spec
    monkeypatch.setattr(b.sys, "platform", "darwin")
    monkeypatch.setattr(b.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda n, *a, **kw: (
            (object() if installed else None)
            if n == "mlx_whisper"
            else None
            if n == "whisperx"
            else find(n, *a, **kw)
        ),
    )
    if installed:
        assert isinstance(b.select_backend("whisper"), b.MlxBackend)
    else:
        with pytest.raises(MediaError, match="Нет установленного движка"):
            b.select_backend("whisper")
        with using("en"), pytest.raises(MediaError, match="^No transcription engine is installed"):
            b.select_backend("whisper")


def test_selected_backend_reports_name_and_model(monkeypatch) -> None:
    """The chosen engine reports its name and model, so history records the actual choice, not
    auto: GigaAM has one model (--model does not apply), whisper the given or default one."""
    from mnema.core import backends as b
    from mnema.core.base import DEFAULT_LOCAL_MODEL

    monkeypatch.setattr(b.ParakeetBackend, "available", lambda self: True)
    monkeypatch.setattr(b.GigaAmBackend, "available", lambda self: True)
    monkeypatch.setattr(b.MlxBackend, "available", lambda self: True)

    be = b.select_backend("auto", lang="ru", model="ignored-for-parakeet")
    assert (be.name, be.model) == ("parakeet", "moondream/parakeet-ultra")
    be = b.select_backend("gigaam", model="ignored-for-gigaam")
    assert (be.name, be.model) == ("gigaam", "v3_e2e_rnnt")
    be = b.select_backend("auto", lang="ja")  # not a Parakeet language: the platform's whisper
    assert (be.name, be.model) == ("mlx", DEFAULT_LOCAL_MODEL)
    assert b.select_backend("mlx", model="mlx-community/whisper-small").model == (
        "mlx-community/whisper-small"
    )


def test_mlx_language_is_code_not_verbose_name(monkeypatch) -> None:
    """Without --lang mlx-whisper prints "Detected language: Russian" but returns the code
    "ru": Transcript.language is always a code."""
    import sys
    import types
    from pathlib import Path

    from mnema.core import backends as b

    def fake_transcribe(path, **kw):
        print("Detected language: Russian")
        print("[00:00.000 --> 00:01.000] привет")
        return {
            "text": "привет",
            "segments": [{"start": 0.0, "end": 1.0, "text": "привет"}],
            "language": "ru",
        }

    monkeypatch.setitem(
        sys.modules, "mlx_whisper", types.SimpleNamespace(transcribe=fake_transcribe)
    )
    monkeypatch.setattr(b, "resolve_model_path", lambda model, on: model)
    monkeypatch.setattr(b, "extract_audio", lambda src, dest, **kw: dest)

    tr = b.MlxBackend().run(Path("a.mp3"), total_duration=1.0)
    assert tr.language == "ru"
    assert tr.text == "привет"


@pytest.fixture
def torch_thread_budget(monkeypatch):
    """The torch boundary for routing tests: tensors and engines are faked here."""
    import sys
    import types

    count = 16

    def set_num_threads(value):
        nonlocal count
        count = value

    monkeypatch.setitem(
        sys.modules,
        "torch",
        types.SimpleNamespace(get_num_threads=lambda: count, set_num_threads=set_num_threads),
    )


@pytest.mark.usefixtures("torch_thread_budget")
def test_gigaam_run_short_and_longform(monkeypatch, tmp_path) -> None:
    """Short audio → one piece; longer than 25 s → our long-form windows (silero-vad, see
    _windowed_longform); the text is as the model returned it."""
    from pathlib import Path

    from mnema.core import backends as b

    wav = tmp_path / "audio.wav"

    def fake_extract(src, dest, **kw):
        # short vs long form follows the ACTUAL WAV size (PCM16 mono 16 kHz):
        # the test sets the length in seconds through the size
        wav.write_bytes(b"\x00" * int(fake_extract.secs * 32000))
        return wav

    monkeypatch.setattr(b, "extract_audio", fake_extract)
    monkeypatch.setattr(b, "_gigaam_model", lambda: object())
    # the engine boundary: tensor inference and WAV reading
    monkeypatch.setattr(b, "_read_wav_tensor", lambda w: object())
    monkeypatch.setattr(b, "_gigaam_infer", lambda model, chunk: "Привет, как дела?")

    be = b.GigaAmBackend()
    fake_extract.secs = 10.3  # 10 s + padding
    tr = be.run(Path("voice.ogg"), lang="ru", total_duration=10.0)
    assert tr.text == "Привет, как дела?"
    assert tr.language == "ru"
    assert [(s.start, s.end) for s in tr.segments] == [(0.0, 10.0)]

    long_segments = [
        b.Segment(start=0.0, end=2.0, text="Привет."),
        b.Segment(start=2.5, end=30.0, text="Как дела."),
    ]
    seen = {}
    monkeypatch.setattr(
        b,
        "_windowed_longform",
        lambda engine, model, wav, lang, on: seen.update(wav=wav) or long_segments,
    )
    fake_extract.secs = 31.3
    tr = be.run(Path("long.mp3"), lang="ru", total_duration=31.0)
    assert [s.text for s in tr.segments] == ["Привет.", "Как дела."]
    assert seen["wav"] == wav  # the long path went to our windowing

    # A BORDERLINE file: probe says 24 s, but the actual WAV (with 0.3 s of padding) is over
    # the limit and must go long-form, not to the package's short inference ("Too long wav")
    seen.clear()
    fake_extract.secs = 24.3
    tr = be.run(Path("borderline.ogg"), lang="ru", total_duration=24.0)
    assert seen.get("wav") == wav  # decided by the actual WAV length, not the probe


@pytest.mark.parametrize(
    ("backend", "lang", "match"),
    [("GigaAmBackend", "en", "русский"), ("ParakeetBackend", "ja", "ja")],
)
def test_gigaam_rejects_foreign_lang(backend, lang, match) -> None:
    """An unsupported language is a clear error right away, not a crash deep in the package."""
    from pathlib import Path

    from mnema.core import backends as b

    with pytest.raises(b.MediaError, match=match):
        getattr(b, backend)().run(Path("a.ogg"), lang=lang)


def test_whisperx_available_without_cuda_and_device_auto(monkeypatch) -> None:
    """available() means the package, not CUDA: a Linux laptop without NVIDIA transcribes
    locally (int8 on CPU). device is chosen lazily: cuda when CUDA works, else cpu."""
    import importlib.util
    import sys
    import types

    from mnema.core import backends as b

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    assert b.WhisperXBackend().available() is True
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    assert b.WhisperXBackend().available() is False

    fake_torch = types.SimpleNamespace(cuda=types.SimpleNamespace(is_available=lambda: False))
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    assert b.WhisperXBackend().device == "cpu"
    fake_torch.cuda.is_available = lambda: True
    assert b.WhisperXBackend().device == "cuda"
    assert b.WhisperXBackend(device="cpu").device == "cpu"  # an explicit choice is kept


SR = 16000
_PAUSED = [
    {"start": 0, "end": 2 * SR},
    {"start": int(2.3 * SR), "end": 3 * SR},  # the same breath: joined
    {"start": 6 * SR, "end": 7 * SR},  # after a 3 s pause: a new reply
]


@pytest.mark.parametrize(
    ("speech", "limit", "kw", "expected"),
    [
        # neighbours join while they fit the window; going over starts a new window
        (
            [
                {"start": 0, "end": 100},
                {"start": 150, "end": 300},
                {"start": 350, "end": 450},
                {"start": 500, "end": 600},
            ],
            400,
            {},
            [(0, 300), (350, 600)],
        ),
        ([], 400, {}, []),
        (_PAUSED, 22 * SR, {}, [(0, 3 * SR), (6 * SR, 7 * SR)]),
        (_PAUSED, 22 * SR, {"max_gap": 10 * SR}, [(0, 7 * SR)]),  # joined with the given max_gap
    ],
)
def test_gigaam_group_vad_windows_splits_on_pause(speech, limit, kw, expected) -> None:
    """Replies across a pause > 0.6 s are separate windows with their own times (otherwise the
    other track's reply in the pause landed after the whole chunk)."""
    from mnema.core.backends import _group_vad_windows

    assert _group_vad_windows(speech, limit, **kw) == expected


def test_whisper_segments_split_on_word_pauses() -> None:
    """Whisper with word times: a reply breaks at a pause > 0.6 s, so the other track's reply
    lands between the pieces, not after the whole 30 s window."""
    from mnema.core.backends import _split_by_pauses

    raw = [
        {"start": 0.0, "end": 9.0, "text": " Yes I agree. So what next?", "words": [
            {"word": " Yes", "start": 0.0, "end": 0.4},
            {"word": " I", "start": 0.5, "end": 0.6},
            {"word": " agree.", "start": 0.7, "end": 1.2},
            {"word": " So", "start": 6.0, "end": 6.3},  # a 4.8 s pause: a new reply
            {"word": " what", "start": 6.4, "end": 6.7},
            {"word": " next?", "start": 6.8, "end": 9.0},
        ]},
        {"start": 9.2, "end": 10.0, "text": " Okay.", "words": [
            {"word": " Okay.", "start": 9.3, "end": 10.0},  # 0.3 s later: the same reply
        ]},
    ]  # fmt: skip
    segs = _split_by_pauses(raw)
    assert [(s.start, s.text) for s in segs] == [
        (0.0, "Yes I agree."),
        (6.0, "So what next? Okay."),
    ]
    assert segs[1].end == 10.0


def test_gigaam_segment_per_window_with_model_text(monkeypatch) -> None:
    """A long recording: one reply per VAD window with its own time, the text as the model gave
    it (decode returns (text, ids, frames)); a window without text gives no reply. Boundaries:
    ffmpeg, silero-vad and the gigaam model."""
    import sys
    import types
    import wave
    from pathlib import Path

    torch = pytest.importorskip("torch")
    pytest.importorskip("numpy")
    from mnema.core import backends as b

    sr = 16000

    def fake_extract(src, dest, **kw):
        with wave.open(str(dest), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(b"\x00\x00" * 30 * sr)
        return dest

    texts = [" Привет, как дела? ", "Нормально.", ""]

    class Decoding:
        def decode(self, head, encoded, encoded_len):
            return [(texts.pop(0), [7, 8], [0, 1])]

    class Model:
        _device = torch.device("cpu")
        _dtype = torch.float32
        head = None
        decoding = Decoding()

        def forward(self, wav, length):
            return wav, length

    speech = [(1, 3), (10, 12), (20, 21)]
    vad = types.SimpleNamespace(
        get_speech_timestamps=lambda *a, **kw: [{"start": s * sr, "end": e * sr} for s, e in speech]
    )
    monkeypatch.setattr(b, "extract_audio", fake_extract)
    monkeypatch.setattr(b, "_gigaam_model", Model)
    monkeypatch.setattr(b, "_silero_vad", lambda: object())
    monkeypatch.setitem(sys.modules, "silero_vad", vad)

    tr = b.GigaAmBackend().run(Path("call.wav"), lang="ru")
    assert [(s.start, s.end, s.text) for s in tr.segments] == [
        (1.0, 3.0, "Привет, как дела?"),
        (10.0, 12.0, "Нормально."),
    ]
    assert tr.text == "Привет, как дела? Нормально."


@pytest.mark.usefixtures("torch_thread_budget")
def test_parakeet_run_short_and_long_by_wav_length(monkeypatch, tmp_path) -> None:
    """A short recording (≤ 15 s) is one window without VAD; longer ones are VAD windows.
    Replies are as the model gave them (times from the recording start)."""
    from pathlib import Path

    from mnema.core import backends as b

    wav = tmp_path / "audio.wav"

    def fake_extract(src, dest, **kw):
        wav.write_bytes(b"\x00" * int(fake_extract.secs * 32000))
        return wav

    class FakeModel:
        def transcribe(self, chunk, lang):
            return [b.Segment(start=0.1, end=1.0, text=f"кусок {lang}")]

    monkeypatch.setattr(b, "extract_audio", fake_extract)
    monkeypatch.setattr(b.parakeet, "model", lambda on=None: FakeModel())
    monkeypatch.setattr(b, "_read_wav_tensor", lambda w: [0] * 10)
    seen = {}
    monkeypatch.setattr(
        b,
        "_windowed_longform",
        lambda engine, model, wav, lang, on: seen.update(engine=engine.name, lang=lang) or [],
    )
    be = b.ParakeetBackend()
    fake_extract.secs = 8.3
    tr = be.run(Path("voice.ogg"), lang="ru")
    assert (tr.text, tr.language, seen) == ("кусок ru", "ru", {})
    fake_extract.secs = 40.3
    be.run(Path("call.wav"), lang="en")
    assert seen == {"engine": "parakeet", "lang": "en"}


def test_parakeet_windows_join_across_pauses_up_to_15s() -> None:
    """Parakeet windows span up to 15 s across any pause (on a short chunk the model mistakes
    the language); GigaAM windows end at a 0.6 s pause."""
    from mnema.core import backends as b

    sr = 16000
    speech = [
        {"start": 0, "end": 1 * sr},
        {"start": 4 * sr, "end": 5 * sr},  # a 3 s pause
        {"start": 9 * sr, "end": 14 * sr},
        {"start": 16 * sr, "end": 18 * sr},  # does not fit 15 s from the window start
    ]
    assert b._group_window_speech(b.WINDOWED["parakeet"], speech) == [
        (0, 14 * sr),
        (16 * sr, 18 * sr),
    ]
    assert len(b._group_window_speech(b.WINDOWED["gigaam"], speech)) == 4
