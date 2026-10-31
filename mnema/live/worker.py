"""Child process of one live session: Parakeet on ONNX, phrase by phrase (live/asr.py)."""

from __future__ import annotations

import base64
import contextlib
import json
import queue
import sys
import threading

from ..core.worker import emit, isolate_stdout, parent_gone, setup_child_logging


def main() -> None:
    isolate_stdout()
    setup_child_logging()
    try:
        options = json.loads(sys.stdin.readline())
        incoming: queue.Queue[str] = queue.Queue()

        def read_input() -> None:
            for line in sys.stdin:
                incoming.put(line)
            # The parent is gone and will never close us, even while the model is loading:
            # remove ourselves now.
            parent_gone()

        threading.Thread(target=read_input, name="live-input", daemon=True).start()
        with contextlib.redirect_stdout(sys.stderr):
            from .asr import PhraseTranscriber

            transcriber = PhraseTranscriber(**options)
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
