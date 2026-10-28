"""Ultra HF → stock MLX layers; the numeric check also runs on the official MLX CPU.

Without MLX only the weight conversions and the lazy import are checked. Metal speed is not
measured here; no model or recordings are downloaded.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from mnema.core import parakeet_mlx as pm


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("encoder.subsampling.layers.2.weight", "encoder.pre_encode.conv.2.weight"),
        ("encoder.subsampling.linear.bias", "encoder.pre_encode.out.bias"),
        ("encoder.layers.0.self_attn.q_proj.weight", "encoder.layers.0.self_attn.linear_q.weight"),
        (
            "encoder.layers.0.self_attn.relative_k_proj.weight",
            "encoder.layers.0.self_attn.linear_pos.weight",
        ),
        ("encoder.layers.0.self_attn.bias_u", "encoder.layers.0.self_attn.pos_bias_u"),
        ("encoder.layers.0.conv.norm.running_var", "encoder.layers.0.conv.batch_norm.running_var"),
        ("decoder.lstm.weight_ih_l1", "decoder.prediction.dec_rnn.lstm.1.Wx"),
        ("decoder.lstm.weight_hh_l1", "decoder.prediction.dec_rnn.lstm.1.Wh"),
        ("decoder.lstm.bias_ih_l1", "decoder.prediction.dec_rnn.lstm.1.bias"),
        ("decoder.lstm.bias_hh_l1", None),
        ("encoder_projector.weight", "joint.enc.weight"),
        ("decoder.decoder_projector.bias", "joint.pred.bias"),
        ("joint.head.weight", "joint.joint_net.2.weight"),
        ("encoder.layers.0.conv.norm.num_batches_tracked", None),
        ("vad_head.weight", None),
    ],
)
def test_ultra_weight_names(source, target):
    assert pm.weight_name(source) == target


def test_convolution_axes_and_two_lstm_biases_preserve_values():
    conv2 = np.arange(2 * 3 * 4 * 5).reshape(2, 3, 4, 5)
    converted = pm.convert_weight("encoder.subsampling.layers.0.weight", conv2, None)
    assert converted.shape == (2, 4, 5, 3)
    assert converted[1, 2, 3, 0] == conv2[1, 0, 2, 3]
    conv1 = np.arange(2 * 3 * 4).reshape(2, 3, 4)
    converted = pm.convert_weight("encoder.layers.0.conv.pointwise_conv1.weight", conv1, None)
    assert converted.shape == (2, 4, 3)
    assert converted[1, 2, 0] == conv1[1, 0, 2]
    called = []

    def read(key):
        called.append(key)
        return np.array([4, 5, 6])

    converted = pm.convert_weight("decoder.lstm.bias_ih_l1", np.array([1, 2, 3]), read)
    np.testing.assert_array_equal(converted, [5, 7, 9])
    assert called == ["decoder.lstm.bias_hh_l1"]


def test_apple_silicon_loader_uses_same_ultra_checkpoint_on_mlx(monkeypatch, tmp_path):
    from mnema.core import backends, parakeet

    seen = []
    loaded = object()
    monkeypatch.setattr(parakeet, "_MODEL", None)
    monkeypatch.setattr(parakeet, "_use_mlx", lambda: True)
    monkeypatch.setattr(parakeet, "snapshot_dir", lambda on: tmp_path)
    monkeypatch.setattr(pm, "Model", lambda path: seen.append(path) or loaded)
    monkeypatch.setattr(
        backends, "_torch_device", lambda: pytest.fail("Apple Silicon must use MLX")
    )
    assert parakeet.model() is loaded
    assert seen == [tmp_path]


@pytest.fixture
def tiny_models(tmp_path):
    mx = pytest.importorskip("mlx.core")
    pytest.importorskip("parakeet_mlx")
    torch = pytest.importorskip("torch")
    from test_parakeet import _tiny_checkpoint
    from transformers.models.parakeet import ParakeetEncoderConfig

    from mnema.core import parakeet as pk

    before = torch.get_num_threads()
    torch.set_num_threads(1)
    try:
        tensors = _tiny_checkpoint(tmp_path)
        # Old transformers adds a depthwise bias even with convolution_bias=false; the published
        # Ultra has no bias in any Conformer block convolution.
        tensors = {
            k: v
            for k, v in tensors.items()
            if not (".conv." in k and k.endswith(".bias") and ".norm." not in k)
        }
        from safetensors.torch import save_file

        save_file(tensors, str(tmp_path / "model.safetensors"))
        config = json.loads((tmp_path / "config.json").read_text())
        config["encoder_config"] = ParakeetEncoderConfig(**config["encoder_config"]).to_dict()
        config["encoder_config"]["convolution_bias"] = False
        (tmp_path / "config.json").write_text(json.dumps(config))
        yield pk.Model(tmp_path), pm.Model(tmp_path, device=mx.cpu)
    finally:
        torch.set_num_threads(before)


@pytest.mark.parametrize("length", [7, 8, 9, 16, 17, 100])
def test_encoder_matches_torch_with_padding(tiny_models, length):
    import mlx.core as mx  # ty: ignore[unresolved-import]
    import torch

    from mnema.core.parakeet import _out_len

    cpu, mlx = tiny_models
    features = torch.randn(1, length + 1, 128)
    features[:, length:] = 0
    mask = torch.arange(length + 1)[None] < length
    with torch.inference_mode():
        expected = cpu.encoder(input_features=features, attention_mask=mask).last_hidden_state
    actual = mlx._encode(mx.array(features.numpy()), length)
    np.testing.assert_allclose(
        np.array(actual), expected[0, : _out_len(length)].numpy(), rtol=2e-4, atol=2e-5
    )


def test_decoder_and_joint_match_torch_across_tokens(tiny_models):
    import mlx.core as mx  # ty: ignore[unresolved-import]
    import torch

    cpu, mlx = tiny_models
    cpu_state = mlx_state = None
    encoded = torch.randn(16)
    with torch.inference_mode():
        for token in [cpu.blank, 0, 1, 2, cpu.blank]:
            embedded = cpu.w["decoder.embedding.weight"][token].view(1, 1, -1)
            expected, cpu_state = cpu.lstm(embedded, cpu_state)
            actual, mlx_state = mlx.decoder(mx.array([[token]]), mlx_state)
            np.testing.assert_allclose(np.array(actual), expected.numpy(), rtol=2e-5, atol=1e-6)
            cpu_joint = cpu._linear(
                "joint.head",
                torch.relu(
                    cpu._linear("encoder_projector", encoded)
                    + cpu._linear("decoder.decoder_projector", expected[0, 0])
                ),
            )
            mlx_joint = mlx.joint(mx.array(encoded.numpy()[None, None]), actual)
            np.testing.assert_allclose(
                np.array(mlx_joint[0, 0, 0]), cpu_joint.numpy(), rtol=2e-5, atol=1e-6
            )


def test_full_window_uses_same_tokens_and_timestamps(tiny_models):
    import torch

    cpu, mlx = tiny_models
    audio = torch.sin(torch.arange(16000) * 0.1) * 0.1
    assert mlx.transcribe(audio, "ru") == cpu.transcribe(audio, "ru")


def test_russian_filter_masks_ukrainian_tokens_and_keeps_timing(tiny_models):
    import mlx.core as mx  # ty: ignore[unresolved-import]
    import torch
    from tokenizers import Tokenizer, models

    from mnema.core.parakeet import banned_tokens

    _, model = tiny_models
    model.tokenizer = Tokenizer(
        models.WordLevel({"▁да": 0, "▁ї": 1, "Mm-hmm": 2, "<unk>": 3}, unk_token="<unk>")
    )
    model.banned = banned_tokens(model.tokenizer.get_vocab())
    head = model.joint.joint_net[2]
    head.weight = mx.zeros_like(head.weight)
    head.bias = mx.array([9.0, 10.0, -100.0, -100.0, -100.0, -100.0, 10.0, -100.0])
    audio = torch.zeros(16000)
    foreign = model.transcribe(audio, "uk")
    russian = model.transcribe(audio, "ru")
    assert foreign and russian
    assert all("ї" in segment.text for segment in foreign)
    assert all("ї" not in segment.text and "да" in segment.text for segment in russian)
    assert [(s.start, s.end) for s in foreign] == [(s.start, s.end) for s in russian]
    assert russian[0].start == 0 and russian[-1].end == pytest.approx(1.04)

    head.bias = mx.array([-100.0, -100.0, 10.0, -100.0, -100.0, -100.0, 10.0, -100.0])
    assert [s.text for s in model.transcribe(audio, "ru")] == ["Угу."]
