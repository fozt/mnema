"""Parakeet Ultra on ONNX Runtime (mnema/core/parakeet.py) and the recognizer interface
(mnema/core/asr_engine.py): features, greedy TDT, the decoder contract, windows, words,
replies, cleanup, model files. The sessions are fakes of the real graphs; no model or network.
"""

from __future__ import annotations

import hashlib
import wave
from pathlib import Path

import numpy as np
import pytest

from mnema.core import asr_engine
from mnema.core import parakeet as pk
from mnema.core.asr_engine import Word
from mnema.core.base import MediaError
from mnema.core.models import Segment
from tests import onnx_stubs

FIXTURES = Path(__file__).parent / "fixtures"
SR = 16000
PIECES = ["<unk>", "▁При", "вет", ",", "▁как", "▁де", "ла", "▁угу", "<blk>"]
TOKEN = {p: i for i, p in enumerate(PIECES)}
VOCAB = len(PIECES)


def test_mel_filters_match_librosa_slaney() -> None:
    """Slaney filters as librosa.filters.mel(sr=16000, n_fft=512, n_mels=128, norm="slaney")."""
    w = pk.mel_filters()
    assert w.shape == (128, 257)
    ref = {  # (argmax, sum, max) of the rows from librosa 0.11
        0: (1, 0.02837754227221012, 0.02837754227221012),
        64: (55, 0.030684133991599083, 0.018818283453583717),
        127: (250, 0.03194368630647659, 0.005223188549280167),
    }
    for row, (peak, total, top) in ref.items():
        assert int(w[row].argmax()) == peak
        assert float(w[row].sum()) == pytest.approx(total, rel=1e-6)
        assert float(w[row].max()) == pytest.approx(top, rel=1e-6)


@pytest.mark.parametrize("name", ["long", "short"])
def test_features_match_the_torch_reference(name) -> None:
    """numpy features = the torch features() the model was matched with (fixture of task 0)."""
    ref = np.load(FIXTURES / "parakeet-features-v1.npz")
    feats, length = pk.features(ref[f"{name}_pcm"].astype(np.float32) / 32768)
    assert feats.dtype == np.float32 and feats.shape == (1, *ref[f"{name}_features"].shape)
    assert length == int(ref[f"{name}_length"])
    assert float(np.abs(feats[0] - ref[f"{name}_features"]).max()) < 1e-3


def _scripted(script: list[tuple[int, int]], vocab: int = 5):
    """A joint returning (token, duration index) in turn; step records the tokens."""
    calls: list[int] = []
    fed: list[int] = []
    it = iter(script)

    def joint(t: int):
        calls.append(t)
        token, dur = next(it)
        logits = np.zeros(vocab + 5, np.float32)
        logits[token] = 1.0
        logits[vocab + dur] = 1.0
        return logits

    return joint, fed.append, calls, fed


def test_greedy_tdt_follows_durations() -> None:
    """A token with duration d advances d frames; a blank with 0 still advances 1; a token with 0
    stays on the frame (the next token is there too)."""
    joint, step, calls, fed = _scripted([(1, 2), (4, 0), (2, 0), (3, 1), (4, 4)])
    out = pk.greedy_tdt(7, joint=joint, step=step, blank=4, vocab=5, durations=[0, 1, 2, 3, 4])
    assert calls == [0, 2, 3, 3, 4]
    assert [(e.token, e.frame, e.duration) for e in out] == [(1, 0, 2), (2, 3, 1), (3, 3, 1)]
    assert fed == [1, 2, 3]


def test_greedy_tdt_forces_progress_after_max_symbols() -> None:
    """The model loops on a frame (zero-duration tokens): after max_symbols it advances by 1."""
    joint, step, calls, _fed = _scripted([(1, 0), (1, 0), (1, 0), (4, 4)])
    out = pk.greedy_tdt(
        2, joint=joint, step=step, blank=4, vocab=5, durations=[0, 1, 2, 3, 4], max_symbols=3
    )
    assert calls == [0, 0, 0, 1]
    assert len(out) == 3


# ─────────────────────────────────────────── the recognizer on fake sessions


@pytest.fixture
def model_dir(monkeypatch, tmp_path):
    """Ready model files (MNEMA_PARAKEET_ONNX_DIR): fakes of the graphs, a small vocabulary."""
    folder = tmp_path / "onnx"
    folder.mkdir()
    for name in pk.ONNX_FILES:
        (folder / name).write_bytes(b"graph")
    (folder / pk.VOCAB).write_text(
        "".join(f"{p} {i}\n" for i, p in enumerate(PIECES)), encoding="utf-8"
    )
    monkeypatch.setenv("MNEMA_PARAKEET_ONNX_DIR", str(folder))
    return folder


def _wav(path: Path, seconds: float) -> Path:
    rng = np.random.default_rng(0)
    pcm = (rng.standard_normal(int(seconds * SR)) * 3000).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setparams((1, 2, SR, 0, "NONE", "not compressed"))
        w.writeframes(pcm.tobytes())
    return path


def _install(monkeypatch, script, *, speech=(), seconds=0.0, encoded=None, decoded=None):
    return onnx_stubs.install(
        monkeypatch,
        silero_vad=onnx_stubs.silero(onnx_stubs.speech_probs(list(speech), seconds)),
        encoder=onnx_stubs.encoder(script, encoded),
        decoder=onnx_stubs.decoder(VOCAB, decoded),
    )


def test_a_short_recording_is_one_window_of_timed_words(monkeypatch, tmp_path, model_dir):
    """≤ 15 s: one window, no speech search; a word is its pieces from "▁" to the next one,
    timed by encoder frames (80 ms); service tokens are dropped."""
    encoded: list[dict] = []
    t = TOKEN
    script = [{0: t["▁При"], 1: t["вет"], 3: t[","], 4: t["<unk>"], 20: t["▁как"]}]
    _install(monkeypatch, script, encoded=encoded)
    words = pk.OnnxParakeet().words(_wav(tmp_path / "a.wav", 8.0))
    assert [(w.text, w.start, w.end) for w in words] == [
        ("Привет,", 0.0, pytest.approx(0.32)),
        ("как", pytest.approx(1.6), pytest.approx(1.68)),
    ]
    assert encoded == [{"frames": 801, "length": 800}]


def test_new_decoder_states_only_after_an_emitted_token(monkeypatch, tmp_path, model_dir):
    """decoder_joint starts from blank and zero states; a blank keeps the target and the
    states, a token becomes the target and its states are taken."""
    decoded: list[dict] = []
    _install(monkeypatch, [{1: TOKEN["▁угу"]}], decoded=decoded)
    pk.OnnxParakeet().words(_wav(tmp_path / "a.wav", 0.5))
    blank, token = VOCAB - 1, TOKEN["▁угу"]
    assert [(d["target"], d["state"]) for d in decoded[:4]] == [
        (blank, 0.0), (blank, 0.0), (token, 1.0), (token, 1.0),
    ]  # fmt: skip


def test_a_long_recording_is_recognized_in_windows_of_up_to_15s(monkeypatch, tmp_path, model_dir):
    """Longer than 15 s: windows of speech joined across any pause up to 15 s (on a short chunk
    the model mistakes the language); words are timed from the recording start."""
    encoded: list[dict] = []
    speech = [(0, 1), (4, 5), (9, 14), (16, 18)]
    script = [{0: TOKEN["▁как"]}, {0: TOKEN["▁угу"], 10: TOKEN["▁де"], 11: TOKEN["ла"]}]
    _install(monkeypatch, script, speech=speech, seconds=30, encoded=encoded)
    steps: list[tuple[float | None, str]] = []
    words = pk.OnnxParakeet().words(
        _wav(tmp_path / "a.wav", 30.0), on=lambda f, m: steps.append((f, m))
    )
    assert [round(e["length"] / 100, 1) for e in encoded] == [14.0, 2.1]
    assert [w.text for w in words] == ["как", "угу", "дела"]
    assert words[0].start == pytest.approx(0.0, abs=0.05)
    assert words[1].start == pytest.approx(16.0, abs=0.05)
    assert words[2].start - words[1].start == pytest.approx(0.8)
    assert ("Ищу речь…" in [m for _f, m in steps]) and steps[-1] == (1.0, "Распознаю…")


def test_sessions_use_the_job_threads(monkeypatch, tmp_path, model_dir):
    """The encoder gets the job's CPU budget; the decoder, one thread; no spinning."""
    from mnema.core.settings import asr_thread_budget

    ort = _install(monkeypatch, [{}])
    with asr_thread_budget(6):
        pk.OnnxParakeet().words(_wav(tmp_path / "a.wav", 1.0))
    options = {s.name: s.options for s in ort.sessions}
    enc, dec = options[pk.ENCODER], options[pk.DECODER]
    assert (enc.intra_op_num_threads, enc.inter_op_num_threads) == (6, 1)
    assert (dec.intra_op_num_threads, dec.inter_op_num_threads) == (1, 1)
    assert enc.entries["session.intra_op.allow_spinning"] == "0"


def test_speech_spans_come_from_silero(monkeypatch, tmp_path, model_dir):
    """Recognizer.speech: the raw silero spans in samples, cut at the given length."""
    _install(monkeypatch, [], speech=[(1, 3)], seconds=5)
    spans = pk.OnnxParakeet().speech(_wav(tmp_path / "a.wav", 5.0), max_s=30)
    assert len(spans) == 1
    start, end = spans[0]
    assert start == pytest.approx(SR, abs=1024) and end == pytest.approx(3 * SR, abs=1024)


def test_local_recognizer_is_parakeet_ultra() -> None:
    rec = asr_engine.local_recognizer()
    assert (rec.id, rec.model) == ("parakeet", "parakeet-ultra")
    assert {"ru", "en", "uk"} <= rec.langs and "ja" not in rec.langs


# ─────────────────────────────────────────── model files


def test_ready_folder_without_a_file_is_refused(monkeypatch, model_dir):
    (model_dir / pk.DECODER).unlink()
    with pytest.raises(MediaError, match=pk.DECODER):
        pk.model_dir()


@pytest.fixture
def hub(monkeypatch, tmp_path):
    """huggingface_hub.hf_hub_download: the cache in tmp; a download writes `content`."""
    import huggingface_hub
    from huggingface_hub.errors import LocalEntryNotFoundError

    monkeypatch.delenv("MNEMA_PARAKEET_ONNX_DIR", raising=False)
    state = {"downloads": [], "content": {}}
    snapshot = tmp_path / "hub" / "snapshots" / pk.ONNX_REVISION

    def download(repo_id, filename, *, revision, local_files_only=False, **kw):
        assert (repo_id, revision) == (pk.ONNX_REPO, pk.ONNX_REVISION)
        path = snapshot / filename
        if local_files_only:
            if not path.is_file():
                raise LocalEntryNotFoundError("not cached")
            return str(path)
        state["downloads"].append(filename)
        blob = tmp_path / "hub" / "blobs" / filename
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_bytes(state["content"].get(filename, b"weights"))
        snapshot.mkdir(parents=True, exist_ok=True)
        path.symlink_to(blob)
        return str(path)

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", download)
    return state


def test_downloaded_files_are_checked_once_and_then_read_from_the_cache(monkeypatch, hub):
    monkeypatch.setattr(
        pk, "ONNX_FILES", {name: hashlib.sha256(b"weights").hexdigest() for name in pk.ONNX_FILES}
    )
    steps: list[str] = []
    folder = pk.model_dir(lambda f, m: steps.append(m))
    assert sorted(p.name for p in folder.iterdir()) == sorted(pk.ONNX_FILES)
    assert sorted(hub["downloads"]) == sorted(pk.ONNX_FILES)
    assert "Скачиваю модель распознавания, только в первый раз…" in steps
    assert pk.model_dir() == folder
    assert len(hub["downloads"]) == len(pk.ONNX_FILES)


def test_a_damaged_download_is_refused_and_removed(monkeypatch, hub):
    monkeypatch.setattr(
        pk, "ONNX_FILES", {name: hashlib.sha256(b"weights").hexdigest() for name in pk.ONNX_FILES}
    )
    hub["content"][pk.ENCODER] = b"cut short"
    with pytest.raises(MediaError, match="скачалась с ошибкой"):
        pk.model_dir()
    with pytest.raises(MediaError):  # not taken from the cache next time
        pk.model_dir()
    assert hub["downloads"].count(pk.ENCODER) == 2


# ─────────────────────────────────────────── replies and cleanup


def test_replies_break_at_a_pause_longer_than_06s() -> None:
    words = [
        Word(text="Привет,", start=0.0, end=0.32), Word(text="как", start=0.4, end=0.5),
        Word(text="дела?", start=1.0, end=1.6),  # 0.5 s: the same reply
        Word(text="Угу.", start=2.3, end=2.5),  # 0.7 s: a new one
    ]  # fmt: skip
    assert asr_engine.replies(words) == [
        Segment(start=0.0, end=1.6, text="Привет, как дела?"),
        Segment(start=2.3, end=2.5, text="Угу."),
    ]
    assert len(asr_engine.replies(words, gap=2.0)) == 1
    assert asr_engine.replies([]) == []


@pytest.mark.parametrize(
    ("text", "tidy"),
    [
        ("Mm-hmm.", "Угу."),
        ("Mm-hmm. Mm-hmm.", "Угу."),
        ("Uh", ""),
        ("Um, uh...", ""),
        ("Да, это API-токен.", "Да, это API-токен."),
        ("Good luck.", "Good luck."),  # not a filler: kept as recognized
        ("Excel", "Excel"),
    ],
)
def test_tidy_ru_replaces_english_fillers_only(text: str, tidy: str) -> None:
    assert pk.tidy_ru(text) == tidy


@pytest.mark.parametrize(
    ("text", "lang"),
    [
        ("Да, это API-токен. Угу.", "ru"),
        ("Що нового на роботі і вдома, коли зустрінемося і поговоримо?", "uk"),
        ("Yes, we can ship it to the team this week.", "en"),
        ("Wir können das morgen besprechen.", None),  # Latin, but not English
        ("", None),
    ],
)
def test_language_is_named_by_the_recognized_text(text: str, lang: str | None) -> None:
    assert pk.guess_lang(text) == lang


def test_tidy_applies_russian_cleanup_only_to_russian_calls() -> None:
    segments = [Segment(start=0, end=1, text="Mm-hmm."), Segment(start=1, end=2, text="Uh")]
    assert [s.text for s in pk.tidy(segments, "ru")] == ["Угу."]
    assert [s.text for s in pk.tidy(segments, "en")] == ["Mm-hmm.", "Uh"]
