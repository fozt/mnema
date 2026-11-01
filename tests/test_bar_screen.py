"""The screen video of a recording on the Mac: the app (Platform.screen) captures it, the runtime
starts it after the audio, pauses and stops it with the audio, and a refusal never costs the sound.
The boundary is the audio devices (fake sources feeding the real WAV tee) and the app; the session
and the recording thread are real."""

from __future__ import annotations

import json
import sys
import threading
import time
import types
import wave
from pathlib import Path

import pytest

from mnema.bar.panel import panel_model
from mnema.bar.runtime import BarRuntime
from mnema.bar.settings_model import settings_model
from mnema.bar.state import BarPrefs, MenuEnv, Phase, load_prefs
from mnema.core import base
from mnema.live import meta
from mnema.live.models import LiveResult


class _Source:
    """An audio device: start/stop go to the shared log, every read feeds the WAV tee."""

    reading = threading.Event()  # the session reads: everything has started

    def __init__(self, label: str, tee, log: list[str]) -> None:
        self.name = label
        self._tee = tee
        self._log = log

    def available(self) -> bool:
        return True

    def start(self) -> None:
        self._log.append(f"start {self.name}")

    def read(self) -> bytes:
        _Source.reading.set()
        time.sleep(0.005)
        pcm = b"\x10\x00" * 160
        if self._tee is not None:
            self._tee(pcm)
        return pcm

    def stop(self) -> None:
        self._log.append(f"stop {self.name}")


class _App:
    """Mnema.app: notifications, windows and the screen recorder; it answers the recorder at once
    with the answers given (None — it stays silent)."""

    def __init__(self, log: list[str], answers: dict[str, tuple] | None = None) -> None:
        self.log = log
        self.rt: BarRuntime | None = None
        self.paths: list[Path] = []
        self.answers = {
            "start": ("started", "", 0.5),
            "stop": ("stopped", "", None),
            **(answers or {}),
        }
        self.calls: list[tuple[str, str, Path | None]] = []

    def notify(self, title: str, message: str, path: Path | None) -> None:
        self.calls.append((title, message, path))

    def open(self, path: Path) -> None: ...

    def ask_title(self, current, on_done, **kwargs) -> None:
        on_done(None)

    def confirm(self, title, message, ok, on_done, *, cancel="") -> None:
        on_done(True)

    def screen(self, op: str, path: Path | None = None) -> None:
        self.log.append(f"{op} screen")
        if path is not None:
            self.paths.append(path)
        answer = self.answers.get(op)
        if answer is not None and self.rt is not None:
            self.rt.screen_event(*answer)


@pytest.fixture
def mac(monkeypatch, tmp_path):
    """A Mac with the screen switched on; processing is the node's result with its history."""
    import mnema.live.sources as sources_mod
    import mnema.live.system_audio as system_audio_mod

    monkeypatch.setattr(base, "STANDALONE", True)
    monkeypatch.delenv("MNEMA_RUNNER", raising=False)
    monkeypatch.setenv("MNEMA_LIVE_DIR", str(tmp_path / "notes"))
    monkeypatch.setitem(sys.modules, "sounddevice", types.SimpleNamespace())
    log: list[str] = []
    monkeypatch.setattr(_Source, "reading", threading.Event())
    monkeypatch.setattr(
        sources_mod, "MicSource", lambda *, label="mic", tee=None: _Source(label, tee, log)
    )
    monkeypatch.setattr(
        system_audio_mod,
        "system_source",
        lambda *, label="system", tee=None: _Source(label, tee, log),
    )

    def processed(prepared, tracks, **kw):
        meta.append(prepared.base, meta.Run(status="ok"))
        return LiveResult(base=prepared.base), ""

    monkeypatch.setattr("mnema.live.recording_client.finalize_on_node", processed)
    prefs = tmp_path / "bar.json"
    prefs.write_text(BarPrefs(screen=True).model_dump_json(), encoding="utf-8")

    def make(answers: dict[str, tuple] | None = None) -> tuple[BarRuntime, _App]:
        app = _App(log, answers)
        rt = BarRuntime(platform=app, prefs_file=prefs)
        app.rt = rt
        return rt, app

    return make


def _record(rt: BarRuntime, app: _App, *, then=None) -> Path:
    rt.handle("start")
    assert _Source.reading.wait(timeout=10)
    folder = rt.active_folder
    assert folder is not None
    if then is not None:
        then()
    rt.handle("stop")
    rt.join(timeout=20)
    assert rt.state.phase == Phase.IDLE
    return next((folder.parent).iterdir())


def _video(folder: Path) -> dict:
    runs = json.loads((folder / meta.META_FILE).read_text(encoding="utf-8"))["runs"]
    return runs[-1]


def _frames(wav: Path) -> int:
    with wave.open(str(wav)) as w:
        return w.getnframes()


def test_screen_starts_after_the_audio_and_stops_before_it(mac) -> None:
    rt, app = mac()
    folder = _record(rt, app)
    assert app.log == [
        "start mic",
        "start system",
        "start screen",
        "stop screen",
        "stop system",
        "stop mic",
    ]
    assert app.paths == [folder / "screen.mov"]
    run = _video(folder)
    assert run["video"]["error"] == "" and run["video"]["offset"] >= 0.5
    assert not [n for n in run["notes"] if n["code"].startswith("video")]


def test_pause_pauses_the_video_with_the_audio(mac) -> None:
    rt, app = mac()

    def pause_and_resume() -> None:
        rt.handle("pause")
        rt.handle("resume")

    _record(rt, app, then=pause_and_resume)
    assert [e for e in app.log if e.endswith("screen")] == [
        "start screen",
        "pause screen",
        "resume screen",
        "stop screen",
    ]


def test_no_permission_keeps_the_sound_and_leaves_a_remark(mac) -> None:
    """The app has no permission: the audio is recorded to the stop, the history says why
    there is no video, and the recording is not called incomplete."""
    rt, app = mac({"start": ("failed", "permission", None)})
    folder = _record(rt, app)
    assert "stop screen" not in app.log
    assert _frames(folder / "mic.wav") > 0 and _frames(folder / "system.wav") > 0
    run = _video(folder)
    assert run["video"] == {"offset": None, "error": "permission"}
    assert [n["code"] for n in run["notes"]] == ["video_permission"]
    assert "Нет разрешения на запись экрана" in run["remarks"][0]
    assert rt.state.notice == ""  # the audio is complete


def test_little_free_space_starts_no_video(mac, monkeypatch) -> None:
    import shutil
    from collections import namedtuple

    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda p: usage(2**40, 2**40 - 2**30, 2**30))
    rt, app = mac()
    folder = _record(rt, app)
    assert not [e for e in app.log if e.endswith("screen")]
    assert [n["code"] for n in _video(folder)["notes"]] == ["video_no_space"]


def test_quitting_mid_recording_stops_the_video(mac) -> None:
    rt, app = mac()
    rt.handle("start")
    assert _Source.reading.wait(timeout=10)
    rt.handle("quit")  # recording: a question, the app confirms
    rt.join(timeout=20)
    assert rt.can_exit
    assert app.log[-3:] == ["stop screen", "stop system", "stop mic"]


def test_screen_toggle_only_on_the_mac_and_only_idle(tmp_path, monkeypatch) -> None:
    prefs_file = tmp_path / "bar.json"
    monkeypatch.setattr(base, "STANDALONE", False)
    rt = BarRuntime(prefs_file=prefs_file)
    rt.handle("toggle_screen")
    assert rt.prefs.screen is False  # Linux: no screen video
    sources = panel_model(rt.state, rt.prefs, MenuEnv(), [], settings_model(rt.prefs, MenuEnv()))
    assert sources.idle is not None and [s.label for s in sources.idle.sources] == ["mic", "system"]

    monkeypatch.setattr(base, "STANDALONE", True)
    rt.handle("toggle_screen")
    assert rt.prefs.screen is True and load_prefs(prefs_file).screen is True
    env = MenuEnv(mac=True)
    idle = panel_model(rt.state, rt.prefs, env, [], settings_model(rt.prefs, env)).idle
    assert idle is not None
    screen = next(s for s in idle.sources if s.label == "screen")
    assert (screen.title, screen.on, screen.key) == ("Экран", True, "toggle_screen")

    rt.state.phase = Phase.RECORDING
    rt.handle("toggle_screen")
    assert rt.prefs.screen is True  # not while recording
    recording = panel_model(rt.state, rt.prefs, env, [], settings_model(rt.prefs, env)).recording
    assert recording is not None
    row = next(s for s in recording.sources if s.label == "screen")
    assert row.levels == [] and not row.enabled


def test_settings_show_the_screen_with_the_sources_on_the_mac() -> None:
    prefs = BarPrefs(screen=True)
    mac = settings_model(prefs, MenuEnv(mac=True)).tabs[0].sections[0]
    linux = settings_model(prefs, MenuEnv()).tabs[0].sections[0]
    assert mac.title == "Источники" and [f.key for f in mac.fields][-1] == "toggle_screen"
    assert mac.fields[-1].checked is True
    assert "toggle_screen" not in [f.key for f in linux.fields]


def test_engine_drives_the_app_recorder_over_the_protocol(mac, tmp_path) -> None:
    """Mnema.app gets "screen" lines and answers with "screen_event": the engine waits for the
    first frame, stops the video with the recording and keeps the offset in the history."""
    import functools

    from test_bar_headless import _line, _Out, _start

    from mnema.bar.headless import Channel, Engine
    from mnema.bar.protocol import ActionMsg, QuitMsg, ScreenEventMsg, ScreenMsg

    mac()  # the devices and the processing; the engine makes its own runtime
    out = _Out()
    runtime = functools.partial(BarRuntime, prefs_file=tmp_path / "bar.json")
    engine = Engine(Channel(out), runtime=runtime, root=lambda: tmp_path / "notes")
    thread, app = _start(engine)
    app.write(_line(ActionMsg(key="start")))
    start = out.until(lambda m: isinstance(m, ScreenMsg))
    assert start.op == "start" and start.path.endswith("/screen.mov")
    app.write(_line(ScreenEventMsg(state="started", offset=0.25)))
    assert _Source.reading.wait(timeout=10)
    app.write(_line(ActionMsg(key="stop")))
    assert out.until(lambda m: isinstance(m, ScreenMsg)).op == "stop"
    app.write(_line(ScreenEventMsg(state="stopped")))
    engine.rt.join(timeout=20)
    app.write(_line(ActionMsg(key="quit")))
    out.until(lambda m: isinstance(m, QuitMsg))
    app.close()
    thread.join(timeout=10)
    folder = Path(start.path).parent
    assert _video(folder)["video"]["offset"] >= 0.25


def _processed_later(monkeypatch) -> None:
    """The "Process…" of a recording: the node appends its run."""

    def processed(base, config, connection, **kw):
        meta.append(base, meta.Run(status="ok"))
        return LiveResult(base=base), ""

    monkeypatch.setattr("mnema.live.recording_client.recover_on_node", processed)


def test_stopped_processing_keeps_the_video_remark(mac, monkeypatch) -> None:
    from mnema.live.setup import FinalizeCancelled

    def stopped(prepared, tracks, **kw):
        raise FinalizeCancelled

    rt, app = mac({"start": ("failed", "permission", None)})
    monkeypatch.setattr("mnema.live.recording_client.finalize_on_node", stopped)
    run = _video(_record(rt, app))
    assert run["status"] == "stopped"
    assert run["video"] == {"offset": None, "error": "permission"}
    assert [n["code"] for n in run["notes"]] == ["video_permission"]


def test_server_down_then_processed_the_history_tells_about_the_video(mac, monkeypatch) -> None:
    """The server did not answer, no processing in the history; "Retry" later processes it and
    the history says why there is no video."""

    def deferred(prepared, tracks, **kw):
        return LiveResult(base=prepared.base, defer_command="mnema finish x"), ""

    rt, app = mac({"start": ("failed", "permission", None)})
    monkeypatch.setattr("mnema.live.recording_client.finalize_on_node", deferred)
    folder = _record(rt, app)
    _processed_later(monkeypatch)
    rt.recover(folder)
    rt.join(timeout=20)
    run = _video(folder)
    assert run["video"] == {"offset": None, "error": "permission"}
    assert [n["code"] for n in run["notes"]] == ["video_permission"]


def test_quit_while_recording_then_processed_the_history_tells_about_the_video(
    mac, monkeypatch
) -> None:
    rt, app = mac({"start": ("failed", "permission", None)})
    rt.handle("start")
    assert _Source.reading.wait(timeout=10)
    folder = rt.active_folder
    assert folder is not None
    rt.handle("quit")
    rt.join(timeout=20)
    assert rt.can_exit
    _processed_later(monkeypatch)
    later, _app = mac()  # the next launch
    later.recover(folder)
    later.join(timeout=20)
    run = _video(folder)
    assert run["video"] == {"offset": None, "error": "permission"}
    assert [n["code"] for n in run["notes"]] == ["video_permission"]


def test_processing_again_tells_about_the_video_once_per_processing(mac, monkeypatch) -> None:
    rt, app = mac({"start": ("failed", "permission", None)})
    folder = _record(rt, app)
    _processed_later(monkeypatch)
    rt.recover(folder)
    rt.join(timeout=20)
    runs = json.loads((folder / meta.META_FILE).read_text(encoding="utf-8"))["runs"]
    assert [[n["code"] for n in r["notes"]] for r in runs] == [
        ["video_permission"],
        ["video_permission"],
    ]
