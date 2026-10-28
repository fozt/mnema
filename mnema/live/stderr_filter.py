"""Filters PortAudio's native stderr noise (PaMacCore) during a live session.

When the audio device changes (headphones unplugged), CoreAudio rebuilds its graph and PortAudio
prints "||PaMacCore (AUHAL)|| Error..." straight to fd 2 from C code, bypassing Python logging.
So the filter works on the descriptor: fd 2 goes into a pipe and a background thread copies
everything back to the real stderr except PaMacCore lines. The recording is not affected (the
microphone reopens itself); the noise is cosmetic but alarming mid-recording.
"""

from __future__ import annotations

import contextlib
import os
import threading
from collections.abc import Iterator

_NOISE_MARK = b"||PaMacCore"


@contextlib.contextmanager
def suppress_pamaccore() -> Iterator[None]:
    """Context in which stderr works as usual but PaMacCore lines are dropped.

    A tail without a newline (rich frames with "\\r") is flushed at once unless it looks like the
    start of a PaMacCore line, so live output is not delayed.
    """
    import sys

    real_fd = os.dup(2)
    r_fd, w_fd = os.pipe()
    os.dup2(w_fd, 2)
    os.close(w_fd)
    # Python-level output (rich, logs) goes straight to the real terminal, not the pipe: with fd 2
    # swapped, sys.stderr.isatty() is False, rich Live decides there is no terminal and only
    # prints the final frame on exit. Only the native fd 2 (PortAudio's C code) is filtered.
    saved_stderr = sys.stderr
    sys.stderr = os.fdopen(os.dup(real_fd), "w", buffering=1, closefd=True)

    def pump() -> None:
        buf = b""
        with (
            os.fdopen(r_fd, "rb", buffering=0) as pipe_r,
            os.fdopen(os.dup(real_fd), "wb", buffering=0) as real,
        ):
            while True:
                chunk = pipe_r.read(4096)
                if not chunk:
                    break
                buf += chunk
                *lines, buf = buf.split(b"\n")
                for line in lines:
                    if _NOISE_MARK in line:
                        continue
                    real.write(line + b"\n")
                # tail without \n that is not the start of a noise line: pass it on now
                if buf and not _NOISE_MARK.startswith(buf[:12]) and _NOISE_MARK not in buf:
                    real.write(buf)
                    buf = b""
            if buf and _NOISE_MARK not in buf:
                real.write(buf)

    t = threading.Thread(target=pump, name="pamaccore-filter", daemon=True)
    t.start()
    try:
        yield
    finally:
        try:
            sys.stderr.flush()
        except Exception:  # noqa: BLE001
            pass
        sys.stderr = saved_stderr
        os.dup2(real_fd, 2)  # restores stderr; the last pipe writer is closed, so pump sees EOF
        t.join(timeout=2.0)
        os.close(real_fd)
