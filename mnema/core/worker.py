"""One recognition, one process: when it exits, the OS frees the models and libraries.

JSON travels over pipes, so secrets and paths never appear in the process arguments. Stdout
is reserved for events; library output goes to stderr. The Worker also serves streaming
sessions, whose module reads several requests before exiting.
"""

from __future__ import annotations

import atexit
import collections
import contextlib
import json
import logging
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import weakref
from pathlib import Path
from typing import Any

from .. import i18n
from ..i18n import _
from .base import MediaError, Progress, noop

logger = logging.getLogger("mnema.worker")
_OUTPUT = sys.stdout
_EOF = object()
_TAIL = 40  # child stderr lines kept for the error text
# child log line level (setup_child_logging format) → level in the parent's log
_LEVELS = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING,
           "ERROR": logging.ERROR, "CRITICAL": logging.CRITICAL}  # fmt: skip
_ACTIVE: weakref.WeakSet[Worker] = weakref.WeakSet()
_ACTIVE_LOCK = threading.Lock()


def close_workers() -> None:
    """On app exit, also stop jobs left in daemon queue threads."""
    with _ACTIVE_LOCK:
        workers = list(_ACTIVE)
    for worker in workers:
        with contextlib.suppress(Exception):
            worker.close()


atexit.register(close_workers)


def parent_gone() -> None:
    """EOF on stdin means the parent died; stop the child ffmpeg too."""
    if os.name == "posix":
        os.killpg(os.getpid(), signal.SIGKILL)
    os._exit(1)


def emit(event: dict) -> None:
    _OUTPUT.write(json.dumps(event, ensure_ascii=False) + "\n")
    _OUTPUT.flush()


def setup_child_logging() -> None:
    """Child log lines go to stderr as "LEVEL name: text"; the parent moves them into its own
    log (bar.log, the service journal) at the same level."""
    from .settings import RuntimeSettings

    level = RuntimeSettings().log_level.upper()
    logging.basicConfig(
        level=_LEVELS.get(level, logging.INFO),
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
        force=True,
    )


def isolate_stdout() -> None:
    """Call once in the child: even native printf output stays out of the JSON pipe."""
    global _OUTPUT
    _OUTPUT = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", buffering=1)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())


class Worker:
    """A separate interpreter with a bounded stop wait and cleanup of its descendants."""

    def __init__(self, module: str = "mnema.core.worker") -> None:
        self._temp = tempfile.TemporaryDirectory(prefix="mnema-asr-")
        self._tail: collections.deque[str] = collections.deque(maxlen=_TAIL)
        self._events: queue.Queue[Any] = queue.Queue()
        self._closed = False
        self._close_lock = threading.Lock()
        env = os.environ.copy()
        # Inside Mnema.app sys.executable is the app itself; the venv interpreter comes from
        # sys.prefix. The child needs neither PYTHONEXECUTABLE (set by Bridge.swift) nor
        # __PYVENV_LAUNCHER__.
        python = Path(sys.prefix) / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        executable = str(python) if python.is_file() else sys.executable
        for name in ("PYTHONEXECUTABLE", "__PYVENV_LAUNCHER__", "PYTHONHOME"):
            env.pop(name, None)
        # Works from sources and from the installed app, whatever the cwd.
        root = str(Path(__file__).resolve().parents[2])
        env["PYTHONPATH"] = os.pathsep.join(filter(None, (root, env.get("PYTHONPATH"))))
        env["TMPDIR"] = self._temp.name
        env["TEMP"] = self._temp.name
        env["TMP"] = self._temp.name
        env[i18n.ENV] = i18n.current()  # texts from the child follow the job's language
        try:
            self.process = subprocess.Popen(
                [executable, "-u", "-m", module],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                env=env,
                start_new_session=os.name == "posix",
            )
        except BaseException:
            self._temp.cleanup()
            raise
        self._reader = threading.Thread(target=self._read, name="asr-events", daemon=True)
        self._stderr = threading.Thread(target=self._read_errors, name="asr-stderr", daemon=True)
        with _ACTIVE_LOCK:
            _ACTIVE.add(self)
        try:
            self._reader.start()
            self._stderr.start()
        except BaseException:
            self.close()
            raise

    def _read(self) -> None:
        assert self.process.stdout is not None
        try:
            for line in self.process.stdout:
                try:
                    self._events.put(json.loads(line))
                except ValueError:
                    self._events.put(
                        {
                            "type": "error",
                            "message": _("The recognition process sent a corrupted reply"),
                        }
                    )
                    break
        finally:
            self._events.put(_EOF)

    def _read_errors(self) -> None:
        """Child stderr goes line by line to the parent's log (keys redacted); the tail feeds
        the error text, so a CUDA failure or native crash is not just "exit code None"."""
        assert self.process.stderr is not None
        from ..llm.redact import redact

        for raw in self.process.stderr:
            line = redact(raw.rstrip())
            if not line:
                continue
            self._tail.append(line)
            head, _sep, rest = line.partition(" ")
            if head in _LEVELS and rest:
                logger.log(_LEVELS[head], "%s", rest)
            else:  # library progress lines and warnings
                logger.debug("%s", line)

    def failure(self) -> str:
        """The last non-empty child stderr line (the failure reason), or empty."""
        return next((line for line in reversed(self._tail) if line.strip()), "")

    def send(self, request: dict) -> None:
        assert self.process.stdin is not None
        try:
            self.process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise MediaError(_("Could not pass the job to the recognition process")) from exc

    def receive(self, timeout: float = 0.25) -> dict | None:
        try:
            event = self._events.get(timeout=timeout)
        except queue.Empty:
            return None
        if event is _EOF:
            try:  # stdout closes before the process is a zombie: poll() would give None
                code = self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                code = None
            if self._stderr.ident is not None:
                self._stderr.join(timeout=2)
            if self._tail:
                logger.warning(
                    "recognition process crashed (exit code %s):\n%s", code, "\n".join(self._tail)
                )
            reason = self.failure()
            if reason:
                raise MediaError(
                    _(
                        "The recognition process exited without a result (exit code {code}): "
                        "{reason}",
                        code=code,
                        reason=reason,
                    )
                )
            raise MediaError(
                _("The recognition process exited without a result (exit code {code})", code=code)
            )
        if not isinstance(event, dict):
            raise MediaError(_("The recognition process sent a corrupted reply"))
        return event

    def _signal(self, *, kill: bool = False) -> None:
        with contextlib.suppress(ProcessLookupError):
            if os.name == "posix":
                os.killpg(self.process.pid, signal.SIGKILL if kill else signal.SIGTERM)
            elif self.process.poll() is None:
                self.process.kill() if kill else self.process.terminate()

    def close(self) -> None:
        with self._close_lock:
            self._close()

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            # The group includes ffmpeg, so a cancel during extraction stops it too.
            self._signal()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._signal(kill=True)
                self.process.wait()
            # A descendant may outlive the Python child (a native crash, say).
            self._signal(kill=True)
        finally:
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                if stream is not None:
                    with contextlib.suppress(OSError, ValueError):
                        stream.close()
            for thread in (self._reader, self._stderr):
                if thread.ident is not None:
                    thread.join(timeout=2)
            self._temp.cleanup()
            with _ACTIVE_LOCK:
                _ACTIVE.discard(self)

    def __enter__(self) -> Worker:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def run(operation: str, payload: dict, *, on: Progress = noop) -> Any:
    """Progress and errors go to the caller; an exception from the callback stops the worker."""
    status: tuple[float | None, str] = (None, _("Preparing recognition…"))
    on(*status)  # an already cancelled job never starts the process
    with Worker() as worker:
        worker.send({"operation": operation, "payload": payload})
        while True:
            event = worker.receive()
            if event is None:
                on(*status)  # cancellation also works while a model loads or a long window runs
                continue
            match event.get("type"):
                case "progress":
                    status = event["fraction"], event["message"]
                    on(*status)
                case "result":
                    return event["value"]
                case "error":
                    raise MediaError(event["message"])
                case _:
                    raise MediaError(_("Unknown reply from the recognition process"))


def _execute(operation: str, payload: dict, on: Progress) -> Any:
    from .settings import asr_thread_budget

    with asr_thread_budget(payload.pop("threads", None)):
        if operation == "transcribe":
            from .transcribe import _transcribe

            value = _transcribe(Path(payload.pop("src")), **payload, on=on)
        else:
            raise MediaError(
                _("Unknown recognition process operation: {operation}", operation=operation)
            )
    return value.model_dump() if value is not None else None


def main() -> None:
    isolate_stdout()
    setup_child_logging()
    try:
        request = json.loads(sys.stdin.readline())

        # An offline worker gets exactly one job. While it loads or infers, a separate
        # thread notices stdin closing, even after the parent crashed.
        def watch_parent() -> None:
            sys.stdin.read()
            parent_gone()

        threading.Thread(target=watch_parent, name="asr-parent", daemon=True).start()
        with contextlib.redirect_stdout(sys.stderr):
            value = _execute(
                request["operation"],
                request["payload"],
                lambda fraction, message: emit(
                    {"type": "progress", "fraction": fraction, "message": message}
                ),
            )
        emit({"type": "result", "value": value})
    except Exception as exc:
        from ..llm.redact import redact

        logger.exception("recognition failed")  # the traceback goes to the parent's log
        emit({"type": "error", "message": redact(str(exc) or type(exc).__name__)})


if __name__ == "__main__":
    main()
