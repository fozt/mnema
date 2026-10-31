"""An external onnxruntime stub for the live process tests: no models.

silero hears speech in loud windows; the encoder puts token 0 on the last frame of every window
it gets, decoder_joint gives that token once. MNEMA_TEST_LIVE_MODE breaks the model loading:
fail, fail-secret, wait. Every encoder session writes a row to MNEMA_TEST_LIVE_LOG.
"""

import json
import os
import time
from pathlib import Path

import numpy as np


class SessionOptions:
    def __init__(self):
        self.intra_op_num_threads = 0
        self.inter_op_num_threads = 0

    def add_session_config_entry(self, key, value):
        pass


def _out_len(n):
    for _layer in range(3):
        n = (n - 1) // 2 + 1
    return n


class InferenceSession:
    def __init__(self, path, sess_options=None, providers=None):
        name = Path(path).name
        self.kind = next(k for k in ("silero", "encoder", "decoder") if k in name)
        if self.kind != "encoder":
            return
        with Path(os.environ["MNEMA_TEST_LIVE_LOG"]).open("a") as file:
            row = {"pid": os.getpid(), "threads": sess_options.intra_op_num_threads}
            file.write(json.dumps(row) + "\n")
        mode = os.environ.get("MNEMA_TEST_LIVE_MODE")
        if mode == "fail":
            raise RuntimeError("движок не собрался")
        if mode == "fail-secret":
            raise RuntimeError("движок не собрался: sk-private-test-secret-1234567890")
        if mode == "wait":
            while True:
                time.sleep(0.1)

    def run(self, names, feeds):
        if self.kind == "silero":
            loud = float(np.abs(feeds["input"][0, 64:]).mean()) > 0.01
            return [np.array([[0.9 if loud else 0.05]], np.float32), feeds["state"]]
        if self.kind == "encoder":
            frames = _out_len(feeds["audio_signal"].shape[2])
            out = np.zeros((1, 1024, frames), np.float32)
            out[0, 0, frames - 1] = 1
            return [out, np.array([frames], np.int64)]
        folder = Path(os.environ["MNEMA_PARAKEET_ONNX_DIR"])
        vocab = len((folder / "vocab.txt").read_text(encoding="utf-8").splitlines())
        logits = np.zeros((1, 1, 1, vocab + 5), np.float32)
        logits[0, 0, 0, 0 if feeds["encoder_outputs"][0, 0, 0] else vocab - 1] = 1
        logits[0, 0, 0, vocab + 1] = 1
        return [logits, feeds["input_states_1"], feeds["input_states_2"]]
