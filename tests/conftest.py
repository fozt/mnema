"""Shared test fixtures."""

import os

import pytest

import mnema.config as _config_mod
from mnema import i18n, users

# Tests assert today's Russian texts: the interface language is ru, subprocesses included.
os.environ[i18n.ENV] = "ru"


@pytest.fixture(autouse=True)
def _russian_interface(monkeypatch):
    """Every test starts in ru; a language a test or a CLI main() set does not leak further."""
    monkeypatch.setenv(i18n.ENV, "ru")
    monkeypatch.setattr(i18n, "_process", None)


@pytest.fixture(autouse=True)
def _isolated_user_settings(monkeypatch, tmp_path):
    """The real settings folder (~/.config/mnema: keys, server, bar.json, sync.json) is never read
    or written by tests: config.config_dir() is tmp_path/.config/mnema and home is tmp_path; what
    load_env carried over from a file does not leak into the next test."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setattr(_config_mod, "_FROM_FILE", {})


@pytest.fixture(autouse=True)
def _process_state(monkeypatch):
    """The server mode `serve` turns on and the umask of the server commands stay in the test."""
    monkeypatch.setattr(users, "_strict", False)
    mask = os.umask(0o022)
    os.umask(mask)
    yield
    os.umask(mask)


@pytest.fixture(autouse=True)
def _isolated_server_root(monkeypatch, tmp_path):
    """This computer's server folder (jobs, devices) is in the test's tmp: tests never touch the
    owner's ~/mnema-server."""
    monkeypatch.setenv("MNEMA_SERVER_DIR", str(tmp_path / "mnema-server"))


@pytest.fixture(autouse=True)
def _unknown_free_memory(monkeypatch):
    """The server queue never reads this machine's /proc/meminfo: free memory is unknown and
    holds no job; queue tests give their own."""
    from mnema import job_queue

    monkeypatch.setattr(job_queue, "available_memory", lambda: None)


@pytest.fixture(autouse=True)
def _client_platform(monkeypatch):
    """The default platform is a server client (CI on Linux, the local gate on a Mac): tests of
    the standalone Mac set base.STANDALONE = True themselves."""
    from mnema.core import base

    monkeypatch.setattr(base, "STANDALONE", False)


@pytest.fixture(autouse=True)
def _no_auth_by_default(monkeypatch):
    """The server is open by default: an empty MNEMA_TOKEN turns authorization off.

    create_app() calls load_dotenv(override=False); a real .env with MNEMA_TOKEN would otherwise
    fail every server test with 401. An empty value already in the environment is not overwritten
    by load_dotenv; auth tests set a token themselves.
    """
    monkeypatch.setenv("MNEMA_TOKEN", "")


@pytest.fixture(autouse=True)
def _no_runner_by_default(monkeypatch):
    """Unit tests stay off the network: an empty MNEMA_RUNNER means local dispatch.

    Same trick as MNEMA_TOKEN: serve tests call load_dotenv through create_app(), and a real .env
    with MNEMA_RUNNER would send later tests to a live runner. Remote-path tests set MNEMA_RUNNER
    themselves."""
    monkeypatch.setenv("MNEMA_RUNNER", "")


@pytest.fixture(autouse=True)
def _default_test_models(monkeypatch):
    """The model comes from the process environment or .env lines (there is no built-in default),
    so test values go into the environment.

    claude.* functions resolve the model before the (mocked) _stream_text call and fail without
    one. Tests of the .env model call monkeypatch.delenv and write the file lines themselves.
    """
    monkeypatch.setenv("MNEMA_MODEL", "anthropic/claude-sonnet-5")
    monkeypatch.setenv("MNEMA_API_KEY", "sk-test")
    monkeypatch.delenv("MNEMA_API_BASE", raising=False)
    monkeypatch.delenv("MNEMA_ASR_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def _media_tools_by_name(monkeypatch):
    """ffmpeg and ffprobe are called by bare name, as PATH finds them: tests stub the subprocess
    or put stubs on PATH, and never depend on an ffmpeg next to the test Python."""
    monkeypatch.setenv("MNEMA_FFMPEG", "ffmpeg")
    monkeypatch.setenv("MNEMA_FFPROBE", "ffprobe")
    monkeypatch.delenv("MNEMA_APP_BUNDLE", raising=False)


@pytest.fixture(autouse=True)
def _no_llm_network(monkeypatch):
    """Unit tests stay off the network: the real litellm.completion is blocked.

    A model is configured (fixture above), so best-effort LLM features would reach litellm; they
    are stopped at the outer boundary. Streaming tests install their own litellm.completion mock;
    claude.* tests mock higher up.
    """
    import litellm

    def _blocked(*a, **k):
        raise RuntimeError("litellm.completion is blocked in unit tests; mock claude.* or litellm")

    monkeypatch.setattr(litellm, "completion", _blocked)


@pytest.fixture(autouse=True, scope="session")
def _no_ytdlp_network():
    """yt-dlp network access is blocked for the WHOLE session: function mocks are undone at the
    end of a test, while a runner job's background thread may outlive it and call the real yt-dlp.
    Tests mock download_url."""
    import yt_dlp

    def _blocked(self, *a, **k):
        raise RuntimeError("yt-dlp network is blocked in unit tests; mock mnema.core.download_url")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(yt_dlp.YoutubeDL, "extract_info", _blocked)
        yield


@pytest.fixture(autouse=True, scope="session")
def _no_real_clipboard(tmp_path_factory):
    """Tests never write the owner's clipboard: pbcopy, wl-copy, xclip and xsel are stubs that
    drop their input. MNEMA_CLIPBOARD_LOG, when set, records which test reached the clipboard."""
    stubs = tmp_path_factory.mktemp("clipboard-bin")
    for name in ("pbcopy", "wl-copy", "xclip", "xsel"):
        stub = stubs / name
        stub.write_text(
            '#!/bin/sh\ncat > /dev/null\n[ -n "$MNEMA_CLIPBOARD_LOG" ] && '
            'echo "$PYTEST_CURRENT_TEST" >> "$MNEMA_CLIPBOARD_LOG"\nexit 0\n'
        )
        stub.chmod(0o755)
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("PATH", f"{stubs}{os.pathsep}{os.environ.get('PATH', '')}")
        yield


@pytest.fixture(autouse=True, scope="session")
def _no_real_network():
    """Real network is blocked in unit tests (loopback and unix sockets are allowed): the refusal
    is instant rather than a timeout waiting on an unreachable address."""
    import socket

    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def local(address) -> bool:
        host = address[0] if isinstance(address, tuple) else address
        return not isinstance(address, tuple) or host in ("127.0.0.1", "::1", "localhost")

    def connect(self, address):
        if not local(address):
            raise ConnectionRefusedError(f"network is blocked in unit tests: {address}")
        return real_connect(self, address)

    def connect_ex(self, address):
        if not local(address):
            return 111  # ECONNREFUSED
        return real_connect_ex(self, address)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(socket.socket, "connect", connect)
        mp.setattr(socket.socket, "connect_ex", connect_ex)
        yield


@pytest.fixture(autouse=True)
def _no_restart_wait(monkeypatch):
    """The client waits up to 90 s for a runner restart (a deploy); unit tests fail at once, and
    tests of the wait set their own window."""
    import mnema.client as client_mod

    monkeypatch.setattr(client_mod, "_RESTART_WAIT_S", 0.0)


@pytest.fixture(autouse=True)
def _plain_console_env(monkeypatch):
    """rich colour and terminal detection must not depend on the launching environment:
    FORCE_COLOR (set by some shells and agents) makes any StringIO a colour terminal, and render
    tests would see ANSI codes."""
    for var in ("FORCE_COLOR", "TTY_COMPATIBLE", "TTY_INTERACTIVE", "CLICOLOR_FORCE", "NO_COLOR"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _no_real_autostart(monkeypatch, tmp_path):
    """mnema-bar autostart and app files go to tmp: tests never see or touch the owner's real
    ~/Library/LaunchAgents and ~/.config/autostart (a real Mnema may live there), nor the install
    log and the panel build cache (~/.cache/mnema)."""
    from mnema.bar import install

    monkeypatch.setattr(
        install, "_launch_agent_path", lambda: tmp_path / "LaunchAgents" / "a.plist"
    )
    monkeypatch.setattr(install, "mac_app_path", lambda: tmp_path / "Applications" / "Mnema.app")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))


@pytest.fixture(autouse=True)
def _no_real_cli_link(monkeypatch, tmp_path):
    """The ~/.local/bin/mnema link goes to tmp: tests never repoint the owner's real command."""
    from mnema import install_setup

    monkeypatch.setattr(install_setup, "cli_link", lambda: tmp_path / ".local/bin/mnema")


@pytest.fixture(autouse=True)
def _no_swift_build(monkeypatch):
    """The Mnema panel (Swift) is never built in unit tests: `swift` is not found, other tools are
    unchanged. Otherwise every install test on a Mac would run a real swift build."""
    import shutil

    real_which = shutil.which
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name, *a, **k: None if name == "swift" else real_which(name, *a, **k),
    )


@pytest.fixture(autouse=True)
def _no_update_network(monkeypatch):
    """The bar's background update check never reaches the real release channel."""
    import httpx

    from mnema import update

    monkeypatch.setattr(update, "TRANSPORT", httpx.MockTransport(lambda _: httpx.Response(404)))


@pytest.fixture(autouse=True)
def _reset_shared_whisper():
    """The shared whisper model is a process singleton: reset it between tests."""
    import mnema.core.backends as backends_mod

    backends_mod._SHARED_WHISPER = None
    yield
    backends_mod._SHARED_WHISPER = None


@pytest.fixture
def wlk_process(monkeypatch, tmp_path):
    """Real child processes with the external WhisperLiveKit stub."""
    import json
    import os
    import time
    from pathlib import Path

    log = tmp_path / "wlk-processes.jsonl"
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).parent / "stubs"))
    monkeypatch.setenv("MNEMA_TEST_WLK_LOG", str(log))

    def started(count=1):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            rows = (
                [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
            )
            if len(rows) >= count:
                return rows
            time.sleep(0.01)
        pytest.fail("the WLK child process did not start")

    def stopped(count=1):
        rows = started(count)
        for row in rows:
            with pytest.raises(ProcessLookupError):
                os.kill(row["pid"], 0)
        return rows

    return started, stopped


@pytest.fixture
def gtk_process(tmp_path):
    """Real GTK 3 on a separate Broadway display, without windows on the desktop."""
    import importlib.util
    import os
    import shutil
    import subprocess
    import sys
    import tempfile
    import textwrap
    import time
    from pathlib import Path

    daemon = shutil.which("broadwayd")
    if sys.platform != "linux" or not daemon or importlib.util.find_spec("gi") is None:
        pytest.skip("GTK 3 and Broadway are not installed")
    # A unix socket path is limited to 108 bytes; pytest's tmp_path is often much longer.
    temporary = tempfile.TemporaryDirectory(prefix="mnema-gtk-")
    runtime = Path(temporary.name)
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("MNEMA_") and key not in {"DISPLAY", "WAYLAND_DISPLAY"}
    }
    env.update(
        GDK_BACKEND="broadway",
        BROADWAY_DISPLAY=":0",
        XDG_RUNTIME_DIR=str(runtime),
        HOME=str(tmp_path),
        XDG_CONFIG_HOME=str(tmp_path / ".config"),
        PYTHONPATH=str(Path(__file__).resolve().parents[1]),
        **{i18n.ENV: "ru"},  # MNEMA_* are dropped above; the windows still render in ru
    )
    server = subprocess.Popen(
        [daemon, "--unixsocket=" + str(runtime / "http.socket"), ":0"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    prelude = """
import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gtk, Gdk, GLib
"""
    try:
        deadline = time.monotonic() + 5
        while not (runtime / "http.socket").exists():
            if server.poll() is not None or time.monotonic() >= deadline:
                pytest.fail("the test Broadway did not start")
            time.sleep(0.01)
        probe = subprocess.run(
            [sys.executable, "-c", prelude],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if probe.returncode:
            pytest.skip("GTK 3 typelib is unavailable: " + probe.stderr)

        def run(script: str):
            result = subprocess.run(
                [sys.executable, "-c", prelude + textwrap.dedent(script)],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            return result

        yield run
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)
        if server.stderr:
            server.stderr.close()
        temporary.cleanup()
