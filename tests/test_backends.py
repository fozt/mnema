"""Backend selection and engine routing, without loading models."""

from __future__ import annotations

from pathlib import Path

import pytest

from mnema.core import backends as b
from mnema.core.backends import select_backend
from mnema.core.base import MediaError
from mnema.i18n import using
from tests import onnx_stubs


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


def test_automatic_choice_is_parakeet_for_its_languages_and_the_cloud_for_the_rest(
    monkeypatch,
) -> None:
    """AUTO: no language or one of Parakeet's 25 → Parakeet; another language is recognized only
    in the cloud, and the audio does not go there by itself. A cloud model is chosen by name."""
    monkeypatch.setattr(b.ParakeetBackend, "available", lambda self: True)
    for lang in (None, "ru", "en", "uk"):
        assert isinstance(select_backend("auto", lang=lang), b.ParakeetBackend)
    with pytest.raises(MediaError, match="^Этот язык распознаёт только облако$"):
        select_backend("auto", lang="ja")
    cloud = select_backend("openai/gpt-4o-mini-transcribe", lang="ja")
    assert isinstance(cloud, b.CloudBackend)
    assert (cloud.name, cloud.model) == ("openai/gpt-4o-mini-transcribe",) * 2
    be = select_backend("auto", lang="ru", model="large-v3")  # an old CLI's model
    assert (be.name, be.model) == ("parakeet", "parakeet-ultra")


def test_automatic_choice_without_parakeet_names_the_missing_engine(monkeypatch) -> None:
    monkeypatch.setattr(b.ParakeetBackend, "available", lambda self: False)
    with using("en"), pytest.raises(MediaError, match="^No transcription engine is installed"):
        select_backend("auto", lang="ru")


def test_parakeet_refuses_a_language_it_does_not_have(monkeypatch) -> None:
    monkeypatch.setattr(b, "extract_audio", lambda *a, **kw: pytest.fail("no audio is read"))
    with pytest.raises(b.OnlyCloud, match="^Этот язык распознаёт только облако$"):
        b.ParakeetBackend().run(Path("call.ogg"), lang="ja")


SR = 16000
_PAUSED = [
    (0, 2 * SR),
    (int(2.3 * SR), 3 * SR),  # the same breath: joined
    (6 * SR, 7 * SR),  # after a 3 s pause: a new reply
]
_BREATH = int(0.6 * SR)


@pytest.mark.parametrize(
    ("speech", "limit", "gap", "expected"),
    [
        # neighbours join while they fit the window; going over starts a new window
        ([(0, 100), (150, 300), (350, 450), (500, 600)], 400, _BREATH, [(0, 300), (350, 600)]),
        ([], 400, _BREATH, []),
        (_PAUSED, 22 * SR, _BREATH, [(0, 3 * SR), (6 * SR, 7 * SR)]),
        (_PAUSED, 22 * SR, 10 * SR, [(0, 7 * SR)]),  # joined across a longer allowed pause
    ],
)
def test_speech_groups_into_windows_split_on_pause(speech, limit, gap, expected) -> None:
    """Replies across a pause > 0.6 s are separate windows with their own times (otherwise the
    other track's reply in the pause landed after the whole chunk)."""
    from mnema.core.asr_engine import group_speech

    assert group_speech(speech, limit, gap) == expected


def test_parakeet_gives_replies_cleaned_by_the_language(monkeypatch, tmp_path) -> None:
    """Parakeet recognizes without a hint; replies break at pauses; a given language cleans
    them, without one the language is named from the text. Boundaries: ffmpeg and the ONNX
    sessions."""
    import wave

    from mnema.core import parakeet as pk

    pieces = ["▁Привет", ".", "▁Mm", "-", "hmm", ".", "<blk>"]
    folder = tmp_path / "onnx"
    folder.mkdir()
    for name in pk.ONNX_FILES:
        (folder / name).write_bytes(b"graph")
    (folder / pk.VOCAB).write_text("".join(f"{p} {i}\n" for i, p in enumerate(pieces)))
    monkeypatch.setenv("MNEMA_PARAKEET_ONNX_DIR", str(folder))

    def fake_extract(src, dest, **kw):
        with wave.open(str(dest), "wb") as w:
            w.setparams((1, 2, SR, 0, "NONE", "not compressed"))
            w.writeframes(b"\x01\x00" * 8 * SR)
        return dest

    monkeypatch.setattr(b, "extract_audio", fake_extract)
    script = {0: 0, 1: 1, 30: 2, 31: 3, 32: 4, 33: 5}  # «Привет.» at 0 s, «Mm-hmm.» at 2.4 s
    for lang, texts in (("ru", ["Привет.", "Угу."]), (None, ["Привет.", "Угу."]),
                        ("en", ["Привет.", "Mm-hmm."])):  # fmt: skip
        onnx_stubs.install(
            monkeypatch,
            silero_vad=onnx_stubs.silero([]),
            encoder=onnx_stubs.encoder([script]),
            decoder=onnx_stubs.decoder(len(pieces)),
        )
        tr = b.ParakeetBackend().run(Path("voice.ogg"), lang=lang)
        assert [s.text for s in tr.segments] == texts
        assert tr.segments[1].start == pytest.approx(2.4)
        assert tr.language == (lang or "ru")
