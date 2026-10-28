"""Relaunching the updated app does not pick up code from the old installation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import venv
from pathlib import Path

import pytest

from mnema.bar import runtime
from mnema.bar.runtime import BarRuntime
from mnema.bar.state import Job, Phase


def test_update_relaunch_runs_new_installed_package_despite_old_cwd_and_pythonpath(
    monkeypatch, tmp_path
):
    old = tmp_path / "old-checkout"
    (old / "mnema").mkdir(parents=True)
    (old / "mnema/__init__.py").write_text('__version__ = "old"\n')
    (old / "mnema/bar.py").write_text('raise RuntimeError("old package launched")\n')
    new = tmp_path / "new-venv"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(new)
    package = (
        new / f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages/mnema"
    )
    package.mkdir()
    (package / "__init__.py").write_text('__version__ = "new"\n')
    result = tmp_path / "launched.json"
    (package / "bar.py").write_text(
        "import json, os\nfrom pathlib import Path\nfrom mnema import __version__\n"
        f"Path({str(result)!r}).write_text(json.dumps({{\n"
        "'version': __version__, 'token': os.environ['MNEMA_TOKEN'],\n"
        "'runner': os.environ['MNEMA_RUNNER'], 'threads': os.environ['MNEMA_ASR_THREADS']\n"
        "}))\n"
    )
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.chdir(old)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PYTHONPATH", str(old))
    monkeypatch.setenv("PYTHONHOME", str(old))
    monkeypatch.setenv("PYTHONEXECUTABLE", str(old / "python"))
    monkeypatch.setenv("MNEMA_TOKEN", "test-token")
    monkeypatch.setenv("MNEMA_RUNNER", "https://runner.test")
    monkeypatch.setenv("MNEMA_ASR_THREADS", "6")
    monkeypatch.setattr(runtime._platform, "system", lambda: "Linux")
    popen, children = subprocess.Popen, []

    def launch(*args, **kwargs):
        child = popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", launch)
    bar = BarRuntime(prefs_file=tmp_path / "bar.json")
    bar._relaunch(python=str(new / "bin/python"))
    try:
        assert children[0].wait(timeout=15) == 0
    finally:
        if children[0].poll() is None:
            children[0].kill()
            children[0].wait()
    assert json.loads(result.read_text()) == {
        "version": "new",
        "token": "test-token",
        "runner": "https://runner.test",
        "threads": "6",
    }
    assert bar.quit_requested
    assert os.environ["PYTHONPATH"] == str(old)  # the running process env stays


def test_mac_update_opens_bundle_using_its_new_python_and_preserves_settings(monkeypatch, tmp_path):
    app = tmp_path / "Mnema.app"
    app.mkdir()
    from mnema.bar import install

    monkeypatch.setattr(install, "mac_app_path", lambda: app)
    monkeypatch.setattr(runtime._platform, "system", lambda: "Darwin")
    monkeypatch.setattr(sys, "executable", str(app / "Contents/MacOS/Mnema"))
    for name in ("PYTHONEXECUTABLE", "MNEMA_PYTHON", "MNEMA_LIBPYTHON"):
        monkeypatch.setenv(name, "/old/interpreter")
    monkeypatch.setenv("MNEMA_TOKEN", "test-token")
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: calls.append((a, kw)))
    bar = BarRuntime(prefs_file=tmp_path / "bar.json")
    bar._relaunch(python="/new/.venv/bin/python")
    args, kwargs = calls[0]
    assert "/usr/bin/open" in args[0][-1] and str(app) in args[0][-1]
    assert "/new/.venv/bin/python" not in args[0][-1]
    assert kwargs["env"]["MNEMA_TOKEN"] == "test-token"
    assert not {"PYTHONEXECUTABLE", "MNEMA_PYTHON", "MNEMA_LIBPYTHON"} & kwargs["env"].keys()
    assert kwargs["cwd"] == Path.home() and bar.quit_requested


@pytest.mark.parametrize("busy", ["recording", "processing"])
def test_update_does_not_restart_busy_app_or_schedule_restart_when_idle(
    monkeypatch, tmp_path, busy
):
    """Recording or queued processing: the update is installed, the relaunch waits."""
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: calls.append((a, kw)))
    bar = BarRuntime(prefs_file=tmp_path / "bar.json")
    bar._update_at = time.monotonic()
    if busy == "recording":
        bar.state.phase = Phase.RECORDING
    else:
        bar.state.jobs = [Job(id="r", folder=tmp_path / "r", running=True)]
    bar._relaunch(python="/new/.venv/bin/python")
    assert not calls and not bar.quit_requested
    assert "Перезапустите Mnema после" in bar.state.notice
    bar.state.phase, bar.state.jobs = Phase.IDLE, []
    bar.tick()
    bar.tick()
    assert not calls and not bar.quit_requested


def test_quit_waits_for_running_update(monkeypatch, tmp_path):
    """Quit during an update install: the app exits when the install ends instead of cutting
    uv sync short (that left a half-made version and an orphan installer)."""
    import threading

    from mnema import update

    started, finish = threading.Event(), threading.Event()

    def apply(on):
        started.set()
        finish.wait(5)
        return update.Result()  # no new version, no relaunch

    monkeypatch.setattr(update, "apply", apply)
    bar = BarRuntime(prefs_file=tmp_path / "bar.json")
    bar.handle("update")
    assert started.wait(5)
    bar.handle("quit")
    assert bar.quit_requested and not bar.can_exit
    finish.set()
    deadline = time.monotonic() + 5
    while not bar.can_exit and time.monotonic() < deadline:
        time.sleep(0.01)
    assert bar.can_exit
