"""Real child processes: progress, stopping, collecting the result and freeing memory.

A separate executable module stands in for the heavy model, so transport and life cycle stay
real. No weights, user recordings or network are used.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from mnema.core import worker as w
from mnema.core.base import MediaError

_SCRIPT = r"""
import json, os, signal, subprocess, sys, tempfile, time
from pathlib import Path
from mnema.core.worker import emit, isolate_stdout
isolate_stdout()
request = json.loads(sys.stdin.readline())
p = request['payload']
mode = p.get('mode', 'success')
memory = bytearray(32 * 1024 * 1024)
temp = Path(tempfile.gettempdir()) / 'scratch'
temp.write_text('temporary audio')
if mode == 'crash':
    os._exit(17)
if mode == 'error':
    emit({'type': 'error', 'message': 'ошибка распознавания'})
    sys.exit()
if mode == 'stubborn':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if mode == 'descendant':
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])
    Path(p['pid_file']).write_text(str(child.pid))
os.write(1, b'native library output is not JSON\n')
emit({'type': 'progress', 'fraction': 0.25, 'message': 'начал'})
if mode in ('hang', 'stubborn', 'descendant'):
    time.sleep(300)
operation = request['operation']
if operation == 'language':
    result = {'lang': 'ru', 'prob': 0.99}
elif operation == 'transcribe':
    result = {'text': json.dumps(p), 'language': p.get('lang'), 'engine': 'fixture',
              'segments': [{'start': 1.0, 'end': 2.0, 'text': 'привет'}]}
else:
    result = {'pid': os.getpid(), 'temp': str(temp), 'echo': p}
emit({'type': 'result', 'value': result})
"""


@pytest.fixture
def subprocess_engine(monkeypatch, tmp_path):
    (tmp_path / "fixture_asr_worker.py").write_text(_SCRIPT)
    monkeypatch.setenv(
        "PYTHONPATH", os.pathsep.join(filter(None, (str(tmp_path), os.getenv("PYTHONPATH"))))
    )
    actual = w.Worker
    workers = []

    def start():
        child = actual("fixture_asr_worker")
        workers.append(child)
        return child

    monkeypatch.setattr(w, "Worker", start)
    yield workers
    for child in workers:
        child.close()
        assert child.process.poll() is not None
        assert not child._reader.is_alive()
        assert not Path(child._temp.name).exists()


def test_success_returns_progress_and_stops_process_before_return(subprocess_engine):
    events = []
    result = w.run("fixture", {"threads": 4}, on=lambda *e: events.append(e))
    assert (0.25, "начал") in events
    assert result["echo"] == {"threads": 4}
    assert result["pid"] != os.getpid()
    assert not Path(result["temp"]).exists()
    assert subprocess_engine[0].process.poll() is not None


def test_embedded_mac_app_uses_venv_python(subprocess_engine, monkeypatch):
    monkeypatch.setattr(sys, "executable", "/Applications/Mnema.app/Contents/MacOS/Mnema")
    monkeypatch.setenv("PYTHONEXECUTABLE", "/old/environment/bin/python")
    monkeypatch.setenv("__PYVENV_LAUNCHER__", "/old/environment/bin/python")
    result = w.run("fixture", {})
    assert result["pid"] != os.getpid()
    assert subprocess_engine[0].process.args[0] == str(Path(sys.prefix) / "bin/python")


def test_reader_thread_start_failure_reaps_started_process(monkeypatch):
    created = []
    launch = subprocess.Popen

    def record(*args, **kwargs):
        process = launch(*args, **kwargs)
        created.append(process)
        return process

    def no_thread(_):
        raise RuntimeError("потоки недоступны")

    monkeypatch.setattr(subprocess, "Popen", record)
    monkeypatch.setattr(threading.Thread, "start", no_thread)
    with pytest.raises(RuntimeError, match="потоки недоступны"):
        w.Worker()
    assert len(created) == 1 and created[0].poll() is not None


@pytest.mark.parametrize("mode", ["hang", "stubborn"])
def test_callback_cancels_during_silent_work_and_process_is_reaped(subprocess_engine, mode):
    began = False

    def cancel(fraction, message):
        nonlocal began
        if began:
            raise MediaError("Отменено пользователем")
        began = message == "начал"

    start = time.monotonic()
    with pytest.raises(MediaError, match="Отменено пользователем"):
        w.run("fixture", {"mode": mode}, on=cancel)
    assert time.monotonic() - start < 6
    assert subprocess_engine[0].process.poll() is not None


@pytest.mark.parametrize(
    ("mode", "message"), [("error", "ошибка распознавания"), ("crash", "без результата")]
)
def test_error_and_native_crash_cleanup(subprocess_engine, mode, message):
    with pytest.raises(MediaError, match=message):
        w.run("fixture", {"mode": mode})
    assert subprocess_engine[0].process.poll() is not None


def test_cancel_before_start_does_not_create_process(subprocess_engine):
    def cancel(*args):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        w.run("fixture", {}, on=cancel)
    assert subprocess_engine == []


@pytest.mark.skipif(sys.platform != "linux", reason="проверка состояния потомка через /proc")
def test_cancel_stops_ffmpeg_like_descendant(subprocess_engine, tmp_path):
    pid_file = tmp_path / "pid"

    def cancel(fraction, message):
        if message == "начал":
            raise MediaError("stop")

    with pytest.raises(MediaError, match="stop"):
        w.run("fixture", {"mode": "descendant", "pid_file": str(pid_file)}, on=cancel)
    stat = Path(f"/proc/{pid_file.read_text()}/stat")
    deadline = time.monotonic() + 2
    while stat.exists() and stat.read_text().split()[2] != "Z" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not stat.exists() or stat.read_text().split()[2] == "Z"


def test_public_transcribe_forwards_request_and_inherited_thread_budget(
    subprocess_engine, tmp_path, monkeypatch
):
    from mnema.core import transcribe
    from mnema.core.backends import ParakeetBackend
    from mnema.core.settings import asr_thread_budget

    monkeypatch.setattr(ParakeetBackend, "available", lambda self: True)  # installed here

    with asr_thread_budget(8):
        result = transcribe(tmp_path / "audio.wav", backend="parakeet", lang="ru")
    request = json.loads(result.text)
    assert request["threads"] == 8
    assert request["src"] == str(tmp_path / "audio.wav")
    assert request["backend"] == "parakeet" and request["lang"] == "ru"
    assert result.segments[0].start == 1 and result.engine == "fixture"


def test_cloud_recognition_key_reaches_the_process_through_the_pipe(
    subprocess_engine, monkeypatch, tmp_path
):
    """The recognition key from this machine's .env goes with the job, never in the process
    arguments; without a key nothing starts."""
    from mnema import config
    from mnema.core import transcribe
    from mnema.core.backends import CloudBackend

    monkeypatch.setattr(CloudBackend, "available", lambda self: True)  # installed here
    monkeypatch.delenv("MNEMA_ASR_API_KEY", raising=False)
    with pytest.raises(MediaError) as refused:
        transcribe(tmp_path / "mic.wav", backend="openai/gpt-4o-transcribe")
    assert str(refused.value).startswith("Нет ключа OpenAI для распознавания")
    assert subprocess_engine == []
    env = config.user_env_file()
    env.parent.mkdir(parents=True, exist_ok=True)
    env.write_text("MNEMA_ASR_API_KEY=sk-proj-fixture-key-for-worker\n")
    result = transcribe(tmp_path / "mic.wav", backend="openai/gpt-4o-transcribe", lang="de")
    request = json.loads(result.text)
    assert request["backend"] == "openai/gpt-4o-transcribe" and request["lang"] == "de"
    assert request["key"] == "sk-proj-fixture-key-for-worker"
    assert len(subprocess_engine) == 1
    assert "sk-proj-fixture-key-for-worker" not in " ".join(subprocess_engine[0].process.args)


def test_real_worker_error_does_not_import_heavy_libraries_in_parent(tmp_path):
    # A new interpreter: other tests may legitimately have imported torch.
    code = """
import sys
from pathlib import Path
from mnema.core import transcribe, MediaError
from mnema.core.parakeet import available
available()
try:
    transcribe(Path(sys.argv[1]), lang='ru')
except MediaError:
    pass
else:
    raise AssertionError('missing file must fail')
assert not any(x in sys.modules for x in ('torch', 'transformers', 'mlx.core', 'silero_vad'))
"""
    subprocess.run(
        [sys.executable, "-c", code, str(tmp_path / "missing.wav")], check=True, timeout=15
    )


def test_real_worker_redacts_secret_before_returning_error(monkeypatch):
    secret = "fixture-secret-never-send-12345"
    monkeypatch.setenv("MNEMA_TOKEN", secret)
    with pytest.raises(MediaError) as caught:
        w.run(f"unknown-{secret}", {})
    assert secret not in str(caught.value)
    assert "…2345" in str(caught.value)
