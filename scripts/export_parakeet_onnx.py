"""Export Parakeet Ultra to ONNX: the encoder in fp32 and int8, decoder_joint in fp32, vocab.txt.

moondream/parakeet-ultra is a fine-tuned nvidia/parakeet-tdt-0.6b-v3 published only as
transformers safetensors. The base .nemo gives the NeMo graph, config and tokenizer; every
learned weight comes from Ultra by the name table below and loads with strict=True, then NeMo's
own model.export() writes the ONNX files (the layout of onnx-asr and istupakov's v3 export).

A one-off run in a throwaway environment, not a project dependency:

    uv venv /tmp/nemo-env --python 3.12
    VIRTUAL_ENV=/tmp/nemo-env uv pip install "nemo_toolkit[asr]" onnx onnxruntime
    /tmp/nemo-env/bin/python scripts/export_parakeet_onnx.py /tmp/parakeet-ultra-onnx

Output: encoder-model.onnx (+ .data), encoder-model.int8.onnx, decoder_joint-model.onnx,
vocab.txt, config.json; the sizes and sha256 are printed at the end.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tempfile
from pathlib import Path

ULTRA_REPO = "moondream/parakeet-ultra"
ULTRA_REVISION = "73175eb7aeb0d82f1e2a6b53b3aabc10a90bcd0b"
BASE_REPO = "nvidia/parakeet-tdt-0.6b-v3"
BASE_REVISION = "541d1f99c6b0c3cd0b11a95167540bb8edefd82b"
BASE_FILE = "parakeet-tdt-0.6b-v3.nemo"

# NeMo name → transformers name (as transformers' convert_nemo_to_hf.py), applied in order.
NEMO_TO_HF = (
    (r"^encoder\.pre_encode\.conv\.", "encoder.subsampling.layers."),
    (r"^encoder\.pre_encode\.out\.", "encoder.subsampling.linear."),
    (r"\.conv\.batch_norm\.", ".conv.norm."),
    (r"\.self_attn\.linear_q\.", ".self_attn.q_proj."),
    (r"\.self_attn\.linear_k\.", ".self_attn.k_proj."),
    (r"\.self_attn\.linear_v\.", ".self_attn.v_proj."),
    (r"\.self_attn\.linear_out\.", ".self_attn.o_proj."),
    (r"\.self_attn\.linear_pos\.", ".self_attn.relative_k_proj."),
    (r"\.self_attn\.pos_bias_u$", ".self_attn.bias_u"),
    (r"\.self_attn\.pos_bias_v$", ".self_attn.bias_v"),
    (r"^decoder\.prediction\.embed\.", "decoder.embedding."),
    (r"^decoder\.prediction\.dec_rnn\.lstm\.", "decoder.lstm."),
    (r"^joint\.enc\.", "encoder_projector."),
    (r"^joint\.pred\.", "decoder.decoder_projector."),
    (r"^joint\.joint_net\.2\.", "joint.head."),
)
# Kept from the base: the deterministic mel filterbank and window (not in the export graph) and
# the BatchNorm step counters. Ultra's VAD head has no place in the NeMo graph.
KEEP_BASE = re.compile(r"^preprocessor\.featurizer\.(window|fb)$|\.num_batches_tracked$")
SKIP_ULTRA = re.compile(r"^vad_head\.|\.num_batches_tracked$")
ENCODER_TOLERANCE = 1e-3


def hf_name(nemo_key: str) -> str:
    for pattern, repl in NEMO_TO_HF:
        nemo_key = re.sub(pattern, repl, nemo_key)
    return nemo_key


def ultra_state(model, weights: Path) -> dict:
    """The base model's state dict with every learned tensor taken from Ultra (float32)."""
    import torch
    from safetensors import safe_open

    state = model.state_dict()
    with safe_open(str(weights), "pt") as f:
        left = {k for k in f.keys() if not SKIP_ULTRA.search(k)}  # noqa: SIM118 - no __iter__
        for key, base in state.items():
            if KEEP_BASE.search(key):
                continue
            name = hf_name(key)
            if name not in left:
                raise SystemExit(f"Ultra has no tensor for {key} ({name})")
            left.discard(name)
            tensor = f.get_tensor(name).to(torch.float32)
            if tensor.shape != base.shape:
                raise SystemExit(f"{name}: shape {tuple(tensor.shape)}, NeMo {tuple(base.shape)}")
            state[key] = tensor
    if left:
        raise SystemExit(f"Ultra tensors without a NeMo place: {sorted(left)[:10]}")
    return state


def check_tokenizer(model, tokenizer_json: Path) -> list[str]:
    """Ultra's tokenizer.json and the .nemo tokenizer are the same pieces in the same order."""
    from tokenizers import Tokenizer

    pieces = list(model.tokenizer.vocab)
    hf = Tokenizer.from_file(str(tokenizer_json))
    mismatched = [i for i, piece in enumerate(pieces) if hf.id_to_token(i) != piece]
    if mismatched:
        raise SystemExit(f"tokenizer.json differs from the .nemo one at ids {mismatched[:10]}")
    return pieces


def check_encoder(model, encoder: Path) -> float:
    """The fp32 encoder ONNX against the NeMo torch encoder on 10 s of random features."""
    import numpy as np
    import onnxruntime as ort
    import torch

    rng = np.random.default_rng(0)
    feats = rng.standard_normal((1, 128, 1000), dtype=np.float32)
    length = np.array([1000], dtype=np.int64)
    with torch.inference_mode():
        ref, _ref_len = model.encoder(
            audio_signal=torch.from_numpy(feats), length=torch.from_numpy(length)
        )
    session = ort.InferenceSession(str(encoder), providers=["CPUExecutionProvider"])
    out, _out_len = session.run(None, {"audio_signal": feats, "length": length})
    return float(np.abs(out - ref.numpy()).max())


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("out", type=Path, help="output folder")
    parser.add_argument("--ultra", type=Path, help="local Ultra folder (default: download)")
    parser.add_argument("--base", type=Path, help=f"local {BASE_FILE} (default: download)")
    args = parser.parse_args()

    import onnx
    import torch
    from huggingface_hub import hf_hub_download, snapshot_download
    from nemo.collections.asr.models import ASRModel
    from onnxruntime.quantization import QuantType, quantize_dynamic

    ultra = args.ultra or Path(
        snapshot_download(
            ULTRA_REPO,
            revision=ULTRA_REVISION,
            allow_patterns=["model.safetensors", "config.json", "tokenizer.json"],
        )
    )
    base = args.base or Path(hf_hub_download(BASE_REPO, BASE_FILE, revision=BASE_REVISION))
    cfg = json.loads((ultra / "config.json").read_text(encoding="utf-8"))

    model = ASRModel.restore_from(str(base), map_location="cpu")
    model.load_state_dict(ultra_state(model, ultra / "model.safetensors"), strict=True)
    model.eval()
    pieces = check_tokenizer(model, ultra / "tokenizer.json")
    if len(pieces) != cfg["blank_token_id"] or cfg["vocab_size"] != len(pieces) + 1:
        raise SystemExit(f"{len(pieces)} pieces, blank {cfg['blank_token_id']}")

    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out) as tmp, torch.inference_mode():
        model.export(str(Path(tmp) / "model.onnx"))
        # the exporter spills the >2 GB encoder into a file per tensor: one .data file instead
        encoder = onnx.load(str(Path(tmp) / "encoder-model.onnx"), load_external_data=True)
        onnx.save_model(
            encoder,
            str(out / "encoder-model.onnx"),
            save_as_external_data=True,
            all_tensors_to_one_file=True,
            location="encoder-model.onnx.data",
        )
        del encoder
        decoder = onnx.load(str(Path(tmp) / "decoder_joint-model.onnx"), load_external_data=True)
        onnx.save_model(decoder, str(out / "decoder_joint-model.onnx"))
        del decoder

    # int8 only for the encoder: the decoder's logits must stay exact for greedy TDT. Only
    # MatMul, per channel: with convolutions too, 14% of words on real calls differed from fp32.
    quantize_dynamic(
        out / "encoder-model.onnx",
        out / "encoder-model.int8.onnx",
        weight_type=QuantType.QInt8,
        op_types_to_quantize=["MatMul"],
        per_channel=True,
    )
    (out / "vocab.txt").write_text(
        "".join(f"{piece} {i}\n" for i, piece in enumerate([*pieces, "<blk>"])), encoding="utf-8"
    )
    config = {
        "model_type": "nemo-conformer-tdt",
        "features_size": 128,
        "subsampling_factor": 8,
        "blank_token_id": cfg["blank_token_id"],
        "vocab_size": cfg["vocab_size"],
        "durations": cfg["durations"],
        "max_tokens_per_step": cfg["max_symbols_per_step"],
        "source": {
            "ultra": f"{ULTRA_REPO}@{ULTRA_REVISION}",
            "base": f"{BASE_REPO}@{BASE_REVISION}",
        },
    }
    (out / "config.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    diff = check_encoder(model, out / "encoder-model.onnx")
    print(f"encoder fp32 ONNX vs NeMo torch: max |diff| {diff:.2e}")
    if diff > ENCODER_TOLERANCE:
        raise SystemExit(f"the encoder ONNX differs from torch by {diff:.2e}")
    for path in sorted(p for p in out.iterdir() if p.is_file()):
        print(f"{path.name}\t{path.stat().st_size}\t{sha256(path)}")


if __name__ == "__main__":
    main()
