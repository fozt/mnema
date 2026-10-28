"""Parakeet Ultra on MLX: the original HF checkpoint in stock parakeet-mlx layers.

Ultra uses transformers names and axes, the library uses NeMo's; weights are converted in
memory. Features, greedy TDT, the Russian filter and reply splitting are shared with CPU/CUDA.
Heavy libraries are imported only when the model is created.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..i18n import _
from . import parakeet as pk
from .base import MediaError
from .models import Segment


def encoder_config(config: dict) -> dict:
    """HF Conformer → arguments of the same parakeet-mlx layers; sizes come from the checkpoint."""
    enc = config["encoder_config"]
    if (
        enc.get("hidden_act", "silu") != "silu"
        or enc.get("subsampling_conv_kernel_size", 3) != 3
        or enc.get("subsampling_conv_stride", 2) != 2
        or enc.get("num_key_value_heads", enc["num_attention_heads"]) != enc["num_attention_heads"]
        or enc.get("convolution_bias", False) != enc.get("attention_bias", False)
        or enc["intermediate_size"] % enc["hidden_size"]
    ):
        raise MediaError(_("The MLX engine does not support this Parakeet configuration"))
    return {
        "feat_in": enc["num_mel_bins"],
        "n_layers": enc["num_hidden_layers"],
        "d_model": enc["hidden_size"],
        "n_heads": enc["num_attention_heads"],
        "ff_expansion_factor": enc["intermediate_size"] // enc["hidden_size"],
        "subsampling_factor": enc["subsampling_factor"],
        "self_attention_model": "rel_pos",
        "subsampling": "dw_striding",
        "conv_kernel_size": enc["conv_kernel_size"],
        "subsampling_conv_channels": enc["subsampling_conv_channels"],
        "pos_emb_max_len": enc["max_position_embeddings"],
        "use_bias": enc.get("attention_bias", False),
        "xscaling": enc.get("scale_input", False),
    }


def weight_name(name: str) -> str | None:
    """HF names → NeMo/MLX; the training counter and the auxiliary VAD are not needed for ASR."""
    if name.startswith("vad_head.") or name.endswith(".num_batches_tracked"):
        return None
    if match := re.fullmatch(r"decoder.lstm.(weight_ih|weight_hh|bias_ih|bias_hh)_l(\d+)", name):
        kind, layer = match.groups()
        if kind == "bias_hh":
            return None  # MLX keeps one bias: the sum of the two PyTorch biases
        field = {"weight_ih": "Wx", "weight_hh": "Wh", "bias_ih": "bias"}[kind]
        return f"decoder.prediction.dec_rnn.lstm.{layer}.{field}"
    for old, new in (
        ("encoder.subsampling.layers.", "encoder.pre_encode.conv."),
        ("encoder.subsampling.linear.", "encoder.pre_encode.out."),
        (".conv.norm.", ".conv.batch_norm."),
        (".self_attn.q_proj.", ".self_attn.linear_q."),
        (".self_attn.k_proj.", ".self_attn.linear_k."),
        (".self_attn.v_proj.", ".self_attn.linear_v."),
        (".self_attn.o_proj.", ".self_attn.linear_out."),
        (".self_attn.relative_k_proj.", ".self_attn.linear_pos."),
        (".self_attn.bias_u", ".self_attn.pos_bias_u"),
        (".self_attn.bias_v", ".self_attn.pos_bias_v"),
        ("decoder.embedding.", "decoder.prediction.embed."),
        ("decoder.decoder_projector.", "joint.pred."),
        ("encoder_projector.", "joint.enc."),
        ("joint.head.", "joint.joint_net.2."),
    ):
        name = name.replace(old, new)
    return name


def convert_weight(name: str, value: Any, read: Any) -> Any:
    """Convolution axes and the summed LSTM bias; works on numpy without loading MLX."""
    if name.startswith("decoder.lstm.bias_ih_"):
        return value + read(name.replace("bias_ih_", "bias_hh_"))
    if name.startswith("encoder.subsampling.layers.") and name.endswith(".weight"):
        return value.transpose(0, 2, 3, 1)  # OIHW → OHWI
    if ".conv." in name and name.endswith(".weight") and value.ndim == 3:
        return value.transpose(0, 2, 1)  # OIK → OKI
    return value


class Model:
    """The parakeet.Model interface; the encoder and TDT run on the MLX GPU.

    device is set only for numeric checks on the MLX CPU; the app uses mx.gpu.
    """

    def __init__(self, path: Path, *, device: Any = None) -> None:
        import mlx.core as mx  # ty: ignore[unresolved-import]
        from parakeet_mlx.conformer import Conformer, ConformerArgs  # ty: ignore[unresolved-import]
        from parakeet_mlx.rnnt import (  # ty: ignore[unresolved-import]
            JointArgs,
            JointNetwork,
            JointNetworkArgs,
            PredictArgs,
            PredictNetwork,
            PredictNetworkArgs,
        )
        from safetensors import safe_open
        from tokenizers import Tokenizer

        self.device = mx.gpu if device is None else device
        config = json.loads((path / "config.json").read_text(encoding="utf-8"))
        self.blank, self.vocab = config["blank_token_id"], config["vocab_size"]
        self.durations = list(config["durations"])
        self.max_symbols = int(config.get("max_symbols_per_step", 10))
        self.tokenizer = Tokenizer.from_file(str(path / "tokenizer.json"))
        self.banned = pk.banned_tokens(self.tokenizer.get_vocab())
        self.frame_s = pk._HOP / pk.SR * config["encoder_config"]["subsampling_factor"]
        hidden = config["decoder_hidden_size"]
        with mx.stream(self.device), safe_open(str(path / "model.safetensors"), "np") as weights:
            joint_hidden = weights.get_slice("encoder_projector.weight").get_shape()[0]
            self.encoder = Conformer(ConformerArgs(**encoder_config(config)))
            self.decoder = PredictNetwork(
                PredictArgs(
                    True, self.vocab - 1, PredictNetworkArgs(hidden, config["num_decoder_layers"])
                )
            )
            self.joint = JointNetwork(
                JointArgs(
                    self.vocab - 1,
                    [],  # the HF tokenizer stays the only vocabulary source
                    JointNetworkArgs(
                        joint_hidden,
                        config.get("hidden_act", "relu"),
                        config["encoder_config"]["hidden_size"],
                        hidden,
                    ),
                    num_extra_outputs=len(self.durations),
                )
            )
            converted: dict[str, list[tuple[str, Any]]] = {
                "encoder": [],
                "decoder": [],
                "joint": [],
            }
            for source in weights.keys():
                target = weight_name(source)
                if target is None:
                    continue
                group, key = target.split(".", 1)
                if group not in converted:
                    raise MediaError(_("Unknown Parakeet weight: {name}", name=source))
                value = convert_weight(source, weights.get_tensor(source), weights.get_tensor)
                array = mx.array(value).astype(mx.float32)
                mx.eval(array)  # do not keep the whole file and conversion graph alive
                converted[group].append((key, array))
            for group, values in converted.items():
                module = getattr(self, group)
                module.load_weights(values, strict=True)
                module.eval()

    def _encode(self, features: Any, length: int) -> Any:
        """Library layers with the HF length mask: window padding does not affect the last audio."""
        import mlx.core as mx  # ty: ignore[unresolved-import]
        import mlx.nn as nn  # ty: ignore[unresolved-import]

        pre = self.encoder.pre_encode
        x = mx.expand_dims(features, axis=-1)  # B,T,F,C
        for layer in pre.conv:
            x = layer(x)
            if isinstance(layer, nn.Conv2d):
                if layer.stride[0] > 1:
                    length = (
                        length + 2 * layer.padding[0] - layer.weight.shape[1]
                    ) // layer.stride[0] + 1
                valid = mx.arange(x.shape[1]) < length
                x = mx.where(valid[None, :, None, None], x, 0)
        x = pre.out(x.transpose(0, 1, 3, 2).reshape(x.shape[0], x.shape[1], -1))
        # One window, no batch: trimming keeps the padding out of attention and block convolutions.
        x, pos = self.encoder.pos_enc(x[:, :length])
        for layer in self.encoder.layers:
            x = layer(x, pos_emb=pos)
        return x[0]

    def transcribe(self, audio: Any, lang: str | None = None) -> list[Segment]:
        """As the torch path (parakeet.Model.transcribe): no hint without a language; slipped
        Ukrainian letters mean a second decode of the same encoder output with them banned."""
        import mlx.core as mx  # ty: ignore[unresolved-import]

        features, mask = pk.features(audio)
        with mx.stream(self.device):
            encoded = self.joint.enc(self._encode(mx.array(features.numpy()), int(mask.sum())))
            mx.eval(encoded)
        segments = self._decode(encoded, ban=lang == "ru")
        if lang is None and pk.ukrainian_slip(" ".join(s.text for s in segments)):
            segments = self._decode(encoded, ban=True)
        return pk.tidy(segments, lang)

    def _decode(self, encoded: Any, *, ban: bool) -> list[Segment]:
        import mlx.core as mx  # ty: ignore[unresolved-import]

        with mx.stream(self.device):
            bias = mx.zeros(self.vocab + len(self.durations))
            if ban and self.banned:
                bias[mx.array(self.banned)] = pk._BAN
            state: list[Any] = [None, None]

            def step(token: int) -> None:
                output, state[1] = self.decoder(mx.array([[token]]), state[1])
                state[0] = self.joint.pred(output[0, 0])
                mx.eval(state[0], state[1])

            def joint(t: int) -> Any:
                value = encoded[t] + state[0]
                for layer in self.joint.joint_net:
                    value = layer(value)
                return value + bias

            step(self.blank)
            emitted = pk.greedy_tdt(
                encoded.shape[0],
                joint=joint,
                step=step,
                blank=self.blank,
                vocab=self.vocab,
                durations=self.durations,
                max_symbols=self.max_symbols,
            )
        pieces = [
            (
                self.tokenizer.id_to_token(e.token) or "",
                e.frame * self.frame_s,
                (e.frame + e.duration) * self.frame_s,
            )
            for e in emitted
        ]
        segments = []
        for replica in pk.split_replicas(pieces):
            text = self.tokenizer.decode(
                [self.tokenizer.token_to_id(p) for p, *_rest in replica]
            ).strip()
            if text:
                segments.append(Segment(start=replica[0][1], end=replica[-1][2], text=text))
        return segments
