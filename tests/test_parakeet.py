"""Parakeet (mnema/core/parakeet.py): features, greedy TDT, replies, cleanup — no model or network.

The match against transformers 5 on real audio was done during development; these tests pin
the contract of the parts we wrote ourselves.
"""

from __future__ import annotations

import pytest

from mnema.core import parakeet as pk


def test_mel_filters_match_librosa_slaney() -> None:
    """Slaney filters as librosa.filters.mel(sr=16000, n_fft=512, n_mels=128, norm="slaney")."""
    pytest.importorskip("numpy")
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


def test_features_mask_and_normalization() -> None:
    """The length is n // 160 speech frames (+1 zero centering frame); every band is normalized
    over the speech frames: mean 0, deviation 1."""
    torch = pytest.importorskip("torch")
    gen = torch.Generator().manual_seed(0)
    audio = torch.randn(16000, generator=gen) * 0.1
    feats, mask = pk.features(audio)
    assert feats.shape == (1, 101, 128) and mask.shape == (1, 101)
    assert int(mask.sum()) == 100
    valid = feats[0, :100]
    assert valid.mean(0).abs().max() < 1e-4
    assert (valid.std(0) - 1).abs().max() < 1e-3
    assert feats[0, 100].abs().max() == 0


def _scripted(script: list[tuple[int, int]], vocab: int = 5, blank: int = 4):
    """A joint returning (token, duration index) in turn; step records the tokens."""
    torch = pytest.importorskip("torch")
    calls: list[int] = []
    fed: list[int] = []
    it = iter(script)

    def joint(t: int):
        calls.append(t)
        token, dur = next(it)
        logits = torch.zeros(vocab + 5)
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


def test_split_replicas_on_pause_at_word_start() -> None:
    """A pause > 0.6 s before a word start begins a new reply; words are never split."""
    pieces = [
        ("▁При", 0.0, 0.16), ("вет", 0.16, 0.32),
        ("▁как", 0.4, 0.5),
        ("▁де", 1.5, 1.6), ("ла", 2.4, 2.5),  # a pause inside a word is no boundary
        ("▁угу", 3.5, 3.7),
    ]  # fmt: skip
    reps = pk.split_replicas(pieces)
    assert [[p for p, *_ in r] for r in reps] == [
        ["▁При", "вет", "▁как"],
        ["▁де", "ла"],
        ["▁угу"],
    ]


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


def test_banned_tokens_are_ukrainian_letters_only() -> None:
    vocab = {"▁та": 0, "▁ж": 1, "▁етапі": 2, "ї": 3, "▁API": 4, "▁документация": 5, "Є": 6}
    assert pk.banned_tokens(vocab) == [2, 3, 6]


def _tiny_checkpoint(path, *, drop: str = ""):
    """A small Ultra-like checkpoint: transformers encoder, LSTM, projectors, joint, vad_head;
    float16 weights, no convolution biases (as Ultra: convolution_bias=false)."""
    import json

    torch = pytest.importorskip("torch")
    pytest.importorskip("transformers.models.parakeet")
    safetensors = pytest.importorskip("safetensors.torch")
    tokenizers = pytest.importorskip("tokenizers")
    from transformers.models.parakeet import ParakeetEncoder, ParakeetEncoderConfig

    enc_cfg = {
        "hidden_size": 16, "num_hidden_layers": 1, "num_attention_heads": 2,
        "intermediate_size": 32, "conv_kernel_size": 3, "subsampling_conv_channels": 8,
        "num_mel_bins": 128, "attention_bias": False, "scale_input": False,
    }  # fmt: skip
    torch.manual_seed(0)
    enc = ParakeetEncoder(ParakeetEncoderConfig(**enc_cfg))
    lstm = torch.nn.LSTM(8, 8, num_layers=2, batch_first=True)
    vocab, durations = 5, [0, 1, 2]
    tensors = {f"encoder.{k}": v for k, v in enc.state_dict().items() if "conv.pointwise" not in k
               or not k.endswith(".bias")}  # fmt: skip
    tensors |= {f"decoder.lstm.{k}": v for k, v in lstm.state_dict().items()}
    tensors |= {
        "decoder.embedding.weight": torch.randn(vocab, 8),
        "decoder.decoder_projector.weight": torch.randn(12, 8),
        "decoder.decoder_projector.bias": torch.randn(12),
        "encoder_projector.weight": torch.randn(12, 16),
        "encoder_projector.bias": torch.randn(12),
        "joint.head.weight": torch.randn(vocab + len(durations), 12),
        "joint.head.bias": torch.randn(vocab + len(durations)),
        "vad_head.weight": torch.randn(2, 16),
    }
    tensors = {
        k: v.half() if v.is_floating_point() and v.dim() > 1 else v for k, v in tensors.items()
    }
    tensors.pop(drop, None)
    safetensors.save_file(
        {k: v.contiguous() for k, v in tensors.items()}, str(path / "model.safetensors")
    )
    config = {
        "encoder_config": {**enc_cfg, "model_type": "parakeet_encoder"},
        "decoder_hidden_size": 8, "num_decoder_layers": 2, "blank_token_id": vocab - 1,
        "vocab_size": vocab, "durations": durations,
    }  # fmt: skip
    (path / "config.json").write_text(json.dumps(config))
    words = {"▁да": 0, "▁нет": 1, "▁угу": 2, "<unk>": 3}
    tok = tokenizers.Tokenizer(tokenizers.models.WordLevel(words, unk_token="<unk>"))
    tok.save(str(path / "tokenizer.json"))
    return tensors


def test_model_loads_weights_from_file_exactly_without_holding_the_file(tmp_path) -> None:
    """Tensor-by-tensor loading: parameters hold the file's values (float16 → float32), missing
    convolution biases are zeros, vad_head is skipped; the model owns copies, not file slices."""
    torch = pytest.importorskip("torch")
    tensors = _tiny_checkpoint(tmp_path)
    m = pk.Model(tmp_path)
    params = dict(m.encoder.state_dict())
    for k, v in tensors.items():
        if k.startswith("encoder."):
            assert torch.equal(
                params[k.removeprefix("encoder.")], v.to(params[k.removeprefix("encoder.")].dtype)
            )
    missing = [k for k in params if k.endswith(".bias") and f"encoder.{k}" not in tensors]
    assert missing and all(not params[k].any() for k in missing)
    assert torch.equal(m.lstm.weight_hh_l1, tensors["decoder.lstm.weight_hh_l1"].float())
    assert "vad_head.weight" not in m.w
    for k, v in m.w.items():
        assert v.dtype == torch.float32 and torch.equal(v, tensors[k].float())
        assert (
            v.untyped_storage().nbytes() == v.numel() * 4
        )  # not a slice of the file's shared storage
    assert isinstance(m.transcribe(torch.zeros(16000), "ru"), list)


def test_model_refuses_weights_without_a_non_bias_parameter(tmp_path) -> None:
    from mnema.core.base import MediaError

    _tiny_checkpoint(tmp_path, drop="encoder.subsampling.layers.0.weight")
    with pytest.raises(MediaError, match="не подходят"):
        pk.Model(tmp_path)


@pytest.mark.parametrize(
    ("text", "slip"),
    [
        ("Ну да, на етапі тестування посмотрим, а потом закроем задачу и пойдём дальше", True),
        ("Да, это API-токен, а MCP-сервер поднимем завтра", False),  # Latin is not a slip
        ("Привіт, як справи? Що нового на роботі і вдома, коли зустрінемося?", False),  # Ukrainian
        ("Good luck with the release", False),
    ],
)
def test_ukrainian_slip_only_for_russian_with_stray_letters(text: str, slip: bool) -> None:
    """The second decode with Ukrainian letters banned happens only for Russian speech with
    slipped words; real Ukrainian and Latin text are left alone."""
    assert pk.ukrainian_slip(text) is slip


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
    from mnema.core.models import Segment

    segments = [Segment(start=0, end=1, text="Mm-hmm."), Segment(start=1, end=2, text="Uh")]
    assert [s.text for s in pk.tidy(segments, "ru")] == ["Угу."]
    assert [s.text for s in pk.tidy(segments, "en")] == ["Mm-hmm.", "Uh"]
