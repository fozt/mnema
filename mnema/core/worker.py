"""One recognition, one process: when it exits, the OS frees the models and libraries.

JSON travels over pipes, so secrets and paths never appear in the process arguments. Stdout
is reserved for events; library output goes to stderr. The Worker also serves streaming
sessions, whose module reads several requests before exiting.
"""

from __future__ import annotations

import atexit
import collections
import contextlib
import contextvars
import json
import logging
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
import weakref
from collections.abc import Iterator
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
# the environment of a program that is not Python (Worker argv): nothing else is inherited
HELPER_ENV = ("HOME", "PATH", "TMPDIR", "LANG", "TDT_EMISSION_DELAY_FRAMES")
WARM_MEMORY_LIMIT = 3 * 1024**3  # above this after a job the warm process is replaced


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
    """A separate process (a Python interpreter or mnema-asr) with a bounded stop wait and
    cleanup of its descendants."""

    def __init__(self, module: str = "mnema.core.worker", *, argv: list[str] | None = None) -> None:
        """A Python module, or argv: another program on the same pipes (mnema-asr), which
        gets only the HELPER_ENV variables (FluidAudio reads REGISTRY_URL and proxies)."""
        self._temp = tempfile.TemporaryDirectory(prefix="mnema-asr-")
        self._tail: collections.deque[str] = collections.deque(maxlen=_TAIL)
        self._events: queue.Queue[Any] = queue.Queue()
        self._closed = False
        self._close_lock = threading.Lock()
        if argv is not None:
            env = {k: os.environ[k] for k in HELPER_ENV if k in os.environ}
            env["TMPDIR"] = self._temp.name
            command = argv
        else:
            env = self._python_env()
            # Inside Mnema.app sys.executable is the app itself; the venv interpreter comes
            # from sys.prefix.
            python = Path(sys.prefix) / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            command = [str(python) if python.is_file() else sys.executable, "-u", "-m", module]
        try:
            self.process = subprocess.Popen(
                command,
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

    def _python_env(self) -> dict[str, str]:
        env = os.environ.copy()
        # The child needs neither PYTHONEXECUTABLE (set by Bridge.swift) nor __PYVENV_LAUNCHER__.
        for name in ("PYTHONEXECUTABLE", "__PYVENV_LAUNCHER__", "PYTHONHOME"):
            env.pop(name, None)
        # Works from sources and from the installed app, whatever the cwd.
        root = str(Path(__file__).resolve().parents[2])
        env["PYTHONPATH"] = os.pathsep.join(filter(None, (root, env.get("PYTHONPATH"))))
        env["TMPDIR"] = self._temp.name
        env["TEMP"] = self._temp.name
        env["TMP"] = self._temp.name
        env[i18n.ENV] = i18n.current()  # texts from the child follow the job's language
        return env

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
        except (ValueError, OSError):  # close() shut the pipe mid-read
            pass
        finally:
            self._events.put(_EOF)

    def _read_errors(self) -> None:
        """Child stderr goes line by line to the parent's log (keys redacted); the tail feeds
        the error text, so a CUDA failure or native crash is not just "exit code None"."""
        assert self.process.stderr is not None
        from ..llm.redact import redact

        with contextlib.suppress(ValueError, OSError):  # close() shut the pipe mid-read
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
        # macOS answers EPERM for a group left with only zombies
        with contextlib.suppress(ProcessLookupError, PermissionError):
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


class _JobFailed(MediaError):
    """The process answered with an error: the job failed, the process is fine."""


def _serve(worker: Worker, request: dict, status: tuple[float | None, str], on: Progress) -> dict:
    """One request to a running process; the result event, or its error raised."""
    worker.send(request)
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
                return event
            case "error":
                raise _JobFailed(event["message"])
            case _:
                raise MediaError(_("Unknown reply from the recognition process"))


class WarmProcess:
    """The server's one recognition process that outlives its jobs, so short jobs skip the
    start and the model load. Jobs take turns. A cancel or a crash ends it, and it reports its
    memory after every job: past WARM_MEMORY_LIMIT it is replaced before the next one."""

    def __init__(self, threads: int) -> None:
        self.threads = threads  # the ONNX sessions keep the count they were made with
        self._lock = threading.Lock()
        self._worker: Worker | None = None
        self._jobs = 0

    def _drop(self) -> None:
        if self._worker is not None:
            self._worker.close()
        self._worker, self._jobs = None, 0

    def _running(self) -> Worker:
        if self._worker is None or self._worker.process.poll() is not None:
            self._drop()
            self._worker = Worker()
        return self._worker

    def _serve(self, operation: str, payload: dict, on: Progress, status: Any) -> Any:
        request = {
            "operation": operation,
            "payload": {**payload, "threads": self.threads},
            "keep": True,
            "language": i18n.current(),  # texts follow the job, not the process's first one
        }
        started = time.monotonic()
        try:
            event = _serve(self._running(), request, status, on)
        except _JobFailed:
            raise
        except BaseException:  # a cancel: the job is still running in there
            self._drop()
            raise
        self._jobs += 1
        memory = int(event.get("memory") or 0)
        logger.info(
            "warm recognition: %s took %.1f s, job %d, %d MB",
            operation, time.monotonic() - started, self._jobs, memory // 1024**2,
        )  # fmt: skip
        if memory > WARM_MEMORY_LIMIT:
            logger.warning("warm recognition process replaced at %d MB", memory // 1024**2)
            self._drop()
        return event["value"]

    def run(self, operation: str, payload: dict, status: Any, on: Progress) -> Any:
        while not self._lock.acquire(timeout=0.25):
            on(*status)  # waiting for its turn, the job can be cancelled
        try:
            return self._serve(operation, payload, on, status)
        finally:
            self._lock.release()

    def preload(self) -> None:
        """The model is ready before the first job."""
        with self._lock:
            try:
                self._serve("preload", {}, noop, (None, ""))
            except Exception:  # noqa: BLE001 - the first job loads it then
                logger.warning("the recognition model did not load ahead", exc_info=True)

    def release(self) -> None:
        """Give the memory back to a job that needs it; a working process stays."""
        if self._lock.acquire(blocking=False):
            try:
                if self._worker is not None:
                    logger.info("warm recognition process stopped: memory is short")
                self._drop()
            finally:
                self._lock.release()


_WARM: contextvars.ContextVar[WarmProcess | None] = contextvars.ContextVar("warm", default=None)


@contextlib.contextmanager
def warm(process: WarmProcess | None) -> Iterator[None]:
    """This job's recognition goes to the warm process, when there is one."""
    token = _WARM.set(process)
    try:
        yield
    finally:
        _WARM.reset(token)


def run(operation: str, payload: dict, *, on: Progress = noop) -> Any:
    """Progress and errors go to the caller; an exception from the callback stops the worker."""
    status: tuple[float | None, str] = (None, _("Preparing recognition…"))
    on(*status)  # an already cancelled job never starts the process
    process = _WARM.get()
    if process is not None and "key" not in payload:  # a user's key never stays in a process
        return process.run(operation, payload, status, on)
    with Worker() as worker:
        return _serve(worker, {"operation": operation, "payload": payload}, status, on)["value"]


def process_memory() -> int:
    """This process's resident memory in bytes (the peak where /proc is missing)."""
    try:
        return int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except OSError:
        import resource

        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


def _execute(operation: str, payload: dict, on: Progress) -> Any:
    from .settings import asr_thread_budget

    with asr_thread_budget(payload.pop("threads", None)):
        if operation == "transcribe":
            from .transcribe import _transcribe

            value = _transcribe(Path(payload.pop("src")), **payload, on=on)
        elif operation == "preload":
            from .parakeet import model

            model()
            value = None
        else:
            raise MediaError(
                _("Unknown recognition process operation: {operation}", operation=operation)
            )
    return value.model_dump() if value is not None else None


def _answer(line: str) -> bool:
    """One request from the pipe; whether the process stays for the next one."""
    keep = False
    try:
        request = json.loads(line)
        keep = bool(request.get("keep"))
        with contextlib.redirect_stdout(sys.stderr), i18n.using(request.get("language")):
            value = _execute(
                request["operation"],
                request["payload"],
                lambda fraction, message: emit(
                    {"type": "progress", "fraction": fraction, "message": message}
                ),
            )
        emit({"type": "result", "value": value, "memory": process_memory()})
    except Exception as exc:
        from ..llm.redact import redact

        logger.exception("recognition failed")  # the traceback goes to the parent's log
        emit({"type": "error", "message": redact(str(exc) or type(exc).__name__)})
    return keep


def main() -> None:
    isolate_stdout()
    setup_child_logging()
    lines: queue.Queue[str] = queue.Queue()

    # A separate thread notices stdin closing while the model loads or infers, even after the
    # parent crashed.
    def read_parent() -> None:
        for line in sys.stdin:
            lines.put(line)
        parent_gone()

    threading.Thread(target=read_parent, name="asr-parent", daemon=True).start()
    while _answer(lines.get()):
        pass


if __name__ == "__main__":
    main()
