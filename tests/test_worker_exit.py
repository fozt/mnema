"""A real parent exit leaves no ASR process and no ffmpeg/ffprobe descendants behind."""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


def _running(pid):
    path = Path(f"/proc/{pid}/stat")
    try:
        return path.read_text().split()[2] != "Z"
    except (FileNotFoundError, ProcessLookupError):
        return False


@pytest.mark.skipif(sys.platform != "linux", reason="checks descendants through /proc")
@pytest.mark.parametrize("abrupt", [False, True])
def test_parent_exit_stops_model_process_and_its_native_child(tmp_path, abrupt):
    ready = tmp_path / "probe-ready"
    probe = tmp_path / "ffprobe"
    probe.write_text(
        f"#!{sys.executable}\n"
        "import os, time\nfrom pathlib import Path\n"
        "Path(os.environ['PROBE_READY']).write_text(str(os.getpid()))\n"
        "time.sleep(300)\n"
    )
    probe.chmod(0o755)
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"audio")
    code = """
import json, sys
from mnema.core.worker import Worker
worker = Worker()
worker.send({'operation':'transcribe', 'payload':{'src':sys.argv[1], 'lang':'ru'}})
print(json.dumps({'pid':worker.process.pid, 'temp':worker._temp.name}), flush=True)
input()
"""
    env = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        "PROBE_READY": str(ready),
    }
    parent = subprocess.Popen(
        [sys.executable, "-c", code, str(audio)],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    child = None
    try:
        assert parent.stdout is not None and parent.stdin is not None
        child = json.loads(parent.stdout.readline())
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists()
        descendant = int(ready.read_text())
        if abrupt:
            parent.kill()
        else:
            parent.stdin.write("\n")
            parent.stdin.flush()
        parent.wait(timeout=6)
        deadline = time.monotonic() + 3
        while (_running(child["pid"]) or _running(descendant)) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not _running(child["pid"]) and not _running(descendant)
        if not abrupt:
            assert not Path(child["temp"]).exists()
    finally:
        parent.kill()
        parent.communicate(timeout=6)
        if child:
            try:
                os.killpg(child["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass
            shutil.rmtree(child["temp"], ignore_errors=True)


@pytest.mark.skipif(sys.platform != "linux", reason="checks the process through /proc")
def test_parent_kill_during_streaming_warmup_stops_model_process(tmp_path):
    log = tmp_path / "wlk.log"
    code = """
import json, time
from mnema.live.asr import StreamingTranscriber
transcriber = StreamingTranscriber(model='wait')
transcriber.start_warmup()
while transcriber._worker is None:
    time.sleep(0.01)
print(json.dumps({'temp': transcriber._worker._temp.name}), flush=True)
input()
"""
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(Path(__file__).parent / "stubs"), os.getcwd()]),
        "MNEMA_TEST_WLK_LOG": str(log),
    }
    parent = subprocess.Popen(
        [sys.executable, "-c", code],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    pid = None
    scratch = None
    try:
        assert parent.stdout is not None
        scratch = json.loads(parent.stdout.readline())["temp"]
        deadline = time.monotonic() + 10
        while (not log.exists() or not log.read_text().strip()) and time.monotonic() < deadline:
            time.sleep(0.02)
        pid = json.loads(log.read_text().splitlines()[0])["pid"]
        parent.kill()
        parent.wait(timeout=6)
        deadline = time.monotonic() + 3
        while _running(pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not _running(pid)
    finally:
        parent.kill()
        parent.communicate(timeout=6)
        if pid:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if scratch:
            shutil.rmtree(scratch, ignore_errors=True)


@pytest.mark.skipif(os.name != "posix", reason="crashes through os.abort")
def test_crashed_worker_reports_its_stderr_and_exit_code(tmp_path, monkeypatch, caplog):
    """A native child crash: the reason from its stderr is in the error text, its log lines
    reach the parent's log at the same level, and the exit code is real, not None."""
    import logging

    from mnema.core.base import MediaError
    from mnema.core.worker import Worker

    (tmp_path / "crashing_asr.py").write_text(
        "import os, sys\n"
        "sys.stderr.write('WARNING mnema.backends: мало VRAM\\n')\n"
        "sys.stderr.write('libcudnn_ops.so.9: cannot open shared object file\\n')\n"
        "sys.stderr.flush()\n"
        "os.abort()\n"
    )
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    caplog.set_level(logging.DEBUG, logger="mnema.worker")
    with Worker("crashing_asr") as worker, pytest.raises(MediaError) as err:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            worker.receive()
    assert f"код {-signal.SIGABRT}" in str(err.value)
    assert "libcudnn_ops.so.9" in str(err.value)
    assert any(
        r.levelno == logging.WARNING and "мало VRAM" in r.getMessage() for r in caplog.records
    )


@pytest.mark.parametrize(
    ("lang", "message"),
    [
        ("en", "Unknown recognition process operation: nope"),
        ("ru", "Неизвестная операция процесса распознавания: nope"),
    ],
)
def test_worker_child_speaks_the_callers_language(lang, message):
    """The child renders its errors and progress in the language of the job that started it."""
    from mnema.core.base import MediaError
    from mnema.core.worker import run
    from mnema.i18n import using

    seen = []
    with using(lang), pytest.raises(MediaError) as err:
        run("nope", {}, on=lambda fraction, text: seen.append(text))
    assert str(err.value) == message
    assert seen[0] == ("Preparing recognition…" if lang == "en" else "Подготавливаю распознавание…")
