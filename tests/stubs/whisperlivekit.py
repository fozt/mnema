"""An external WLK stub for subprocess tests: no models, network or real ffmpeg."""

import asyncio
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import torch

_OMP_AT_IMPORT = os.environ.get("OMP_NUM_THREADS")


class TranscriptionEngine:
    def __init__(self, **kwargs):
        self.options = kwargs
        log = Path(os.environ["MNEMA_TEST_WLK_LOG"])
        with log.open("a") as file:
            file.write(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "threads": torch.get_num_threads(),
                        "omp_at_import": _OMP_AT_IMPORT,
                        "torch_omp_at_import": torch.omp_at_import,
                        "omp_at_engine": os.environ.get("OMP_NUM_THREADS"),
                        **kwargs,
                    }
                )
                + "\n"
            )
        # the test sets the behaviour: the transcriber model or MNEMA_TEST_WLK_MODE (the server
        # does not accept a model from the client)
        mode = os.environ.get("MNEMA_TEST_WLK_MODE") or kwargs["model_size"]
        if mode == "fail":
            raise RuntimeError("движок не собрался")
        if mode == "fail-secret":
            raise RuntimeError("движок не собрался: sk-private-test-secret-1234567890")
        if mode == "wait":
            while True:
                time.sleep(0.1)


class AudioProcessor:
    def __init__(self, **kwargs):
        self.transcription = SimpleNamespace(process_iter=lambda: None)
        self.done = asyncio.Event()

    async def create_tasks(self):
        async def results():
            await self.done.wait()
            yield SimpleNamespace(
                lines=[SimpleNamespace(text="hello", speaker=0, start=0, end=1)],
                buffer_transcription="",
            )

        return results()

    async def process_audio(self, data):
        if data is None:
            self.done.set()
        else:
            assert data.startswith(b"RIFF") or data == b"audio"

    async def cleanup(self):
        pass
