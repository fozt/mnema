"""Child process of one WhisperLiveKit streaming session."""

from __future__ import annotations

import base64
import contextlib
import json
import os
import queue
import sys
import threading

from ..core.worker import emit, isolate_stdout, parent_gone, setup_child_logging


def _cap_vram() -> None:
    """Each live session is its own process with its own model: they split the torch budget
    evenly so batch recognition on the same GPU keeps its memory."""
    from ..config import Settings
    from ..core.backends import _TORCH_VRAM_FRACTION, cap_torch_vram

    cap_torch_vram(_TORCH_VRAM_FRACTION / max(1, Settings().live_connections))


def main() -> None:
    isolate_stdout()
    setup_child_logging()
    try:
        options = json.loads(sys.stdin.readline())
        incoming: queue.Queue[str] = queue.Queue()

        def read_input() -> None:
            for line in sys.stdin:
                incoming.put(line)
            # The parent is gone and will never close us, even while warmup is loading
            # the model: remove ourselves and the ffmpeg children now.
            parent_gone()

        threading.Thread(target=read_input, name="live-input", daemon=True).start()
        with contextlib.redirect_stdout(sys.stderr):
            from ..core.backends import _asr_cpu_threads
            from ..core.settings import asr_thread_budget

            with asr_thread_budget(options.get("threads")):
                # WLK does not pass cpu_threads to faster-whisper; CT2 takes its budget
                # from OMP_NUM_THREADS. Set only in this child, before the heavy imports.
                os.environ["OMP_NUM_THREADS"] = str(_asr_cpu_threads())
                from .asr import _InProcessTranscriber

                _cap_vram()
                transcriber = _InProcessTranscriber(**options)
                transcriber.warmup()
                emit({"type": "ready"})
                while True:
                    request = json.loads(incoming.get())
                    if request["type"] == "finish":
                        result = transcriber.finish()
                        emit({"type": "done", "value": result.model_dump()})
                        return
                    transcriber.feed(base64.b64decode(request["pcm"], validate=True))
                    emit({"type": "chunk", "value": transcriber.poll().model_dump()})
    except Exception as exc:
        import logging

        from ..llm.redact import redact

        logging.getLogger("mnema.live").exception("live session crashed")
        emit({"type": "error", "message": redact(str(exc) or type(exc).__name__)})


if __name__ == "__main__":
    main()
