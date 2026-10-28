"""The menu bar app (mnema/bar): model and menu, settings, recording (runtime), app install.

No GUI in unit tests (CI is Linux without a display): the AppKit/pystray layers are checked only
for lazy imports; behaviour goes through BarRuntime and the pure functions of state.py. External
boundaries (sounddevice, LiveSession, finalization) are mocked as in the cmd_live contract."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest

import mnema.live.sources as sources_mod
import mnema.live.system_audio as system_audio_mod
from mnema.bar.runtime import BarRuntime
from mnema.bar.state import (
    BarPrefs,
    BarState,
    Job,
    MenuEnv,
    MenuItem,
    Phase,
    SourceMeter,
    load_prefs,
    menu,
    save_prefs,
    title,
)
from mnema.core import base
from mnema.core.base import MediaError
from mnema.core.models import Segment
from mnema.live import remarks
from mnema.live.models import FINALIZE_STEPS, STEP_MESSAGES, LiveResult, Track
from mnema.live.remarks import Step
from mnema.llm.errors import ErrorKind
from mnema.llm.notes import Cloud, LightNote


def _node_pipeline(monkeypatch, tmp_path):
    """The Mac processes itself (LocalNode); the server is the real recording HTTP API in
    this process (Linux tests set base.STANDALONE = False and MNEMA_RUNNER). ASR and models
    are the node's replaceable boundaries."""
    import shutil

    import httpx

    from mnema import node_settings
    from mnema.client import MnemaClient
    from mnema.core import audio, base
    from mnema.live import setup
    from mnema.server import create_app

    app = create_app(tmp_path / "test-node")
    transport = httpx.ASGITransport(app=app)
    monkeypatch.setattr(
        MnemaClient,
        "_make_async_client",
        lambda self: httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            transport=transport,
        ),
    )
    monkeypatch.setattr(MnemaClient, "health", lambda *a, **kw: {"ok": True, "llm": True})
    monkeypatch.setattr(base, "STANDALONE", True)
    monkeypatch.setattr(node_settings, "asr_problem", lambda *a, **kw: None)
    import importlib.util

    find = importlib.util.find_spec
    monkeypatch.setattr(  # this Mac has GigaAM and Whisper; recognition itself is replaced
        importlib.util,
        "find_spec",
        lambda n, *a, **kw: object() if n in ("gigaam", "whisperx") else find(n, *a, **kw),
    )
    monkeypatch.setattr(
        audio, "extract_audio", lambda src, dst, **kw: shutil.copyfile(src, dst) and dst
    )
    monkeypatch.setattr(audio, "mix", lambda inputs, dst, **kw: dst.write_bytes(b"ID3") and dst)
    monkeypatch.setattr(
        setup,
        "dispatch",
        lambda *a, **kw: {
            "text": "привет",
            "segments": [{"start": 0, "end": 1, "text": "привет"}],
            "language": "ru",
        },
    )


@pytest.fixture(autouse=True)
def _http_processing(monkeypatch, tmp_path):
    _node_pipeline(monkeypatch, tmp_path)


_BLOCKED = {"AppKit", "Foundation", "objc", "pystray", "gi", "PIL", "sounddevice", "torch"}

_BLOCKER_PRELUDE = (
    "import sys\n"
    f"blocked = {_BLOCKED!r}\n"
    "class Blocker:\n"
    "    def find_spec(self, name, path=None, target=None):\n"
    "        if name.split('.')[0] in blocked:\n"
    "            raise ImportError('заблокировано в тесте меню-бара: ' + name)\n"
    "        return None\n"
    "sys.meta_path.insert(0, Blocker())\n"
)


def test_bar_imports_without_gui_and_engines() -> None:
    script = (
        _BLOCKER_PRELUDE
        + "import mnema.bar.app, mnema.bar.headless, mnema.bar.app_linux\n"
        + "import mnema.bar.runtime, mnema.bar.state, mnema.bar.install, mnema.bar.icons\n"
        + "leaked = [m for m in blocked if sys.modules.get(m) is not None]\n"
        + "assert not leaked, 'просочилось: ' + repr(leaked)\n"
        + "print('OK')\n"
    )
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert proc.stdout.startswith("OK")


def _keys(items: list[MenuItem]) -> list[str]:
    out: list[str] = []
    for i in items:
        if i.key:
            out.append(i.key)
        out += _keys(i.children)
    return out


def _find(items: list[MenuItem], key: str) -> MenuItem:
    for i in items:
        if i.key == key:
            return i
        if i.children:
            try:
                return _find(i.children, key)
            except KeyError:
                pass
    raise KeyError(key)


def _job_key(rt: BarRuntime, action: str) -> str:
    """An action on the card of the recording being processed."""
    job = rt.state.running
    assert job is not None
    return f"record:{job.id}:{action}"


def test_start_disabled_without_sources() -> None:
    items = menu(BarState(), BarPrefs(mic=False, system=False), MenuEnv())
    assert not _find(items, "start").enabled


def test_idle_menu_toggles_sources_without_settings(tmp_path) -> None:
    """Sources of the next recording are toggles right above "Start recording"; a click
    changes and remembers the choice; while recording they are not in the menu."""
    rt = BarRuntime(prefs_file=tmp_path / "bar.json")
    rt.prefs = BarPrefs(system=False)
    top = menu(rt.state, rt.prefs, MenuEnv())
    mic, system = (next(i for i in top if i.key == k) for k in ("toggle_mic", "toggle_system"))
    assert (mic.title, mic.checked, system.title, system.checked) == (
        "Микрофон", True, "Системный звук", False,
    )  # fmt: skip
    rt.handle(system.key)
    assert load_prefs(tmp_path / "bar.json").system is True
    recording = menu(BarState(phase=Phase.RECORDING), rt.prefs, MenuEnv())
    assert not {"toggle_mic", "toggle_system"} & set(_keys(recording))


def test_recording_menu_shows_sources() -> None:
    """The timer is the caption next to the icon, the menu has no ticking seconds:
    AppIndicator rebuilds it whole."""
    state = BarState(
        phase=Phase.RECORDING,
        elapsed=65,
        meters=[
            SourceMeter(label="mic", history=[0.2, 0.8]),
            SourceMeter(label="system", muted=True),
        ],
    )
    items = menu(state, BarPrefs(), MenuEnv())
    titles = [i.title for i in items]
    assert titles[0] == "Идёт запись" and title(state).text == "01:05"
    assert "Микрофон   пишется" in titles and "Системный звук   выключен" in titles
    assert {"pause", "stop", "discard"} <= set(_keys(items))
    assert "start" not in _keys(items) and "toggle_mic" not in _keys(items)  # fixed meanwhile
    # stopping the recording and quitting the app use different words
    assert (_find(items, "stop").title, _find(items, "quit").title) == (
        "Остановить запись", "Завершить Mnema",
    )  # fmt: skip


def _light(step: Step, note: LightNote) -> remarks.Remark:
    remark = remarks.light(step, note)
    assert remark is not None
    return remark


def _title_without_model() -> remarks.Remark:
    """A title without a model: the cloud refused (out of credit)."""
    note = LightNote(engine="none", cloud=Cloud.FAILED, cloud_detail="закончились деньги на счёте")
    return _light(Step.TITLE, note)


def test_last_result_submenu_lists_warnings(tmp_path) -> None:
    """A processing remark goes in the last recording's submenu: why and what came out, with
    a ⚠︎ mark."""
    txt = tmp_path / "2026-09-25_Дейли" / "transcript.txt"
    state = BarState(last_result=txt, last_warnings=[_title_without_model()])
    items = menu(state, BarPrefs(), MenuEnv())
    last = next(i for i in items if i.title.startswith("Последняя запись"))
    assert "Дейли · 25.09" in last.title and "⚠︎" in last.title
    assert [(c.key, c.title) for c in last.children if c.key] == [
        ("open_last", "Открыть расшифровку"), ("reveal_last", "Открыть папку"),
        ("rename", "Переименовать…"), (f"summarize:{txt.parent}", "Составить пересказ"),
        ("open_notes", "Подробности"),
    ]  # fmt: skip
    assert last.children[2].separator  # open items first, actions after a separator
    titles = [c.title for c in last.children]
    assert "⚠︎ Модель не ответила" in titles and "⚠︎ Без названия" in titles


def test_tray_opens_record_folders_like_the_panel(tmp_path) -> None:
    """The Linux tray follows the card menu order: the last recording has summary,
    transcript, folder, then actions after a separator; an unprocessed one has "Open
    folder", then "Process…". "Open folder" opens the recording's own folder."""
    folder = tmp_path / "2026-09-25_Дейли"
    folder.mkdir()
    (folder / "transcript.txt").write_text("x", encoding="utf-8")
    raw = tmp_path / "2026-09-24_10-00"
    raw.mkdir()
    state = BarState(
        last_result=folder / "transcript.txt",
        last_summary=folder / "summary.md",
        unfinished=[raw],
    )
    items = menu(state, BarPrefs(), MenuEnv())
    last = next(i for i in items if i.title.startswith("Последняя запись"))
    assert [c.title for c in last.children] == [
        "Открыть пересказ", "Открыть расшифровку", "Открыть папку", "", "Переименовать…",
        "Переместить в раздел",
    ]  # fmt: skip
    pending = next(i for i in items if i.title.startswith("⚠︎ Не обработана"))
    assert [(c.key, c.title) for c in pending.children] == [
        (f"reveal:{raw}", "Открыть папку"), (None, ""), (f"recover:{raw}", "Обработать…"),
    ]  # fmt: skip

    opened: list[Path] = []
    rt = BarRuntime(
        platform=types.SimpleNamespace(open=opened.append), prefs_file=tmp_path / "bar.json"
    )
    rt.state = state
    rt.handle("reveal_last")
    rt.handle(f"reveal:{raw}")
    rt.handle(f"reveal:{tmp_path / 'нет'}")
    assert opened == [folder, raw]


def test_prefs_roundtrip_and_broken_file(tmp_path) -> None:
    f = tmp_path / "bar.json"
    save_prefs(BarPrefs(asr="whisper", system=False), f)
    assert load_prefs(f) == BarPrefs(asr="whisper", system=False)
    f.write_text("{не json", encoding="utf-8")
    assert load_prefs(f) == BarPrefs()  # a broken file gives the defaults, the app starts


def test_prefs_with_unknown_key_keep_user_settings(tmp_path) -> None:
    """An unknown key in bar.json does not reset the user's other settings."""
    f = tmp_path / "bar.json"
    f.write_text('{"system": false, "unknown": 1}', encoding="utf-8")
    assert load_prefs(f) == BarPrefs(system=False)


def test_handle_updates_and_persists_prefs(tmp_path) -> None:
    f = tmp_path / "bar.json"
    rt = BarRuntime(prefs_file=f)
    rt.handle("toggle_system")
    assert load_prefs(f) == BarPrefs(system=False)
    rt.state.phase = Phase.RECORDING
    rt.handle("toggle_mic")  # settings do not change while recording
    assert rt.prefs.mic is True


class _FakeWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.level = 0.5
        self.paused = False
        self.muted = False


def _patch_record(monkeypatch, tmp_path, captured: dict) -> None:
    """Boundary: audio sources and the session (as in test_live_cli); finalization is real."""
    import mnema.live.session as session_mod

    _node_pipeline(monkeypatch, tmp_path)
    monkeypatch.delenv("MNEMA_RUNNER", raising=False)
    monkeypatch.setenv("MNEMA_LIVE_DIR", str(tmp_path / "notes"))
    monkeypatch.setitem(sys.modules, "sounddevice", types.SimpleNamespace())
    monkeypatch.setattr("mnema.core.backends.engines_available", lambda: True)

    class FakeMic:
        def __init__(self, *a, **k) -> None:
            self.name = "mic"

        def available(self) -> bool:
            return True

    class FakeSystem:
        def __init__(self, *a, **k) -> None:
            self.name = k.get("label", "system")

        def available(self) -> bool:
            return True

    class FakeSession:
        def __init__(self, sources, factory, **k) -> None:
            self._stopped = threading.Event()
            captured["session"] = self
            self._writers = k.get("recorder") or []

        def stop(self) -> None:
            self._stopped.set()

        def run(self):
            captured["started"].set()
            assert self._stopped.wait(timeout=10)
            for writer in self._writers:
                if not writer.path.exists():
                    _wav(writer.path)
            return [
                Track(
                    label="mic", committed=[Segment(start=0, end=1, text="привет", speaker="mic")]
                )
            ]

    captured["started"] = threading.Event()
    monkeypatch.setattr(sources_mod, "MicSource", FakeMic)
    monkeypatch.setattr(
        system_audio_mod,
        "system_source",
        lambda *, label="system", tee=None: FakeSystem(label=label),
    )
    monkeypatch.setattr(session_mod, "LiveSession", FakeSession)


class _Notes:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Path | None]] = []
        self.settings: list[str] = []  # open settings tabs

    def notify(self, title: str, message: str, path: Path | None) -> None:  # noqa: A002
        self.calls.append((title, message, path))

    def open_settings(self, tab: str) -> None:
        self.settings.append(tab)

    def open(self, path: Path) -> None: ...

    def reveal(self, path: Path) -> None: ...

    def ask_title(self, current, on_done, **kwargs) -> None:
        on_done(None)  # Skip


@pytest.mark.parametrize("recording_error", [None, "mic.wav: No space left on device"])
def test_record_start_stop_saves_with_progress_and_notifies(
    monkeypatch, tmp_path, recording_error
) -> None:
    """Start → the phase shows at once → stop → finalization (progress grows) → the
    transcript in ~/mnema, a notification with the path, final remarks in the state."""

    captured: dict = {}
    _patch_record(monkeypatch, tmp_path, captured)
    progress: list[float | None] = []
    stages: list[str] = []

    def fake_finalize(prepared, tracks, *, on, **kw):
        assert prepared.recording_error == recording_error
        on(0.5, "Офлайн-проход…")
        assert rt.state.running is not None  # processing is queued, the recording has ended
        progress.append(rt.state.running.progress)
        on(None, STEP_MESSAGES["summary"])  # a step start is the caption of the card and the menu
        stages.append(rt.state.running.stage)
        txt = prepared.base / "transcript.txt"
        prepared.base.mkdir(parents=True, exist_ok=True)
        txt.write_text("mic: привет\n", encoding="utf-8")
        return LiveResult(transcript_txt=txt, remarks=remarks), "привет"

    remark = _title_without_model()
    remarks = [remark]
    if recording_error:
        from mnema.live.remarks import recording_failed

        remarks.insert(0, recording_failed(recording_error))
    monkeypatch.setattr("mnema.live.recording_client.finalize_on_node", fake_finalize)
    notes = _Notes()
    rt = BarRuntime(platform=notes, prefs_file=tmp_path / "bar.json")
    rt.handle("start")
    assert rt.state.phase == Phase.RECORDING
    assert captured["started"].wait(timeout=10)
    captured["session"].recording_error = recording_error
    rt.handle("stop")
    rt.join(timeout=10)

    assert rt.state.phase == Phase.IDLE and rt.state.error == ""
    assert progress == [0.5]
    assert stages == [STEP_MESSAGES["summary"]]
    assert (
        rt.state.last_result is not None
        and rt.state.last_result.parent.parent == tmp_path / "notes"
    )
    assert rt.state.last_warnings == remarks
    expected = "Обработана неполная запись" if recording_error else "Запись обработана"
    assert notes.calls and notes.calls[-1][0] == expected
    assert notes.calls[-1][1].endswith(", есть замечания")
    assert notes.calls[-1][2] == rt.state.last_result
    if recording_error:
        assert notes.calls[0][0] == "Запись остановлена"
        assert recording_error in notes.calls[0][1]


def test_record_cancel_removes_session_folder(monkeypatch, tmp_path) -> None:

    captured: dict = {}
    _patch_record(monkeypatch, tmp_path, captured)
    monkeypatch.setattr(
        "mnema.live.recording_client.finalize_on_node",
        lambda *a, **k: pytest.fail("отмена не финализирует"),
    )

    class Confirming(_Notes):
        def __init__(self) -> None:
            super().__init__()
            self.answer = False
            self.asked: list[tuple[str, str, str]] = []

        def confirm(self, title, message, ok, on_done, *, cancel="Отменить") -> None:
            self.asked.append((title, message, ok, cancel))
            on_done(self.answer)

    notes = Confirming()
    rt = BarRuntime(platform=notes, prefs_file=tmp_path / "bar.json")
    rt.handle("start")
    assert captured["started"].wait(timeout=10)
    base = rt._prepared.base
    rt.handle("discard")  # the system confirmation dialog; "Continue recording" keeps it going
    assert notes.asked[-1] == (
        "Отменить запись?", "Звук не сохранится.", "Отменить запись", "Продолжить запись"
    )  # fmt: skip
    assert base.exists() and rt.state.phase == Phase.RECORDING
    notes.answer = True
    rt.handle("discard")  # Discard recording
    rt.join(timeout=10)
    assert not base.exists() and rt.state.phase == Phase.IDLE
    assert notes.calls and notes.calls[0][0] == "Запись удалена"


def test_record_build_error_lands_in_menu(monkeypatch, tmp_path) -> None:
    import mnema.live.setup as setup_mod

    monkeypatch.setenv("MNEMA_LIVE_DIR", str(tmp_path / "notes"))
    monkeypatch.setattr("mnema.core.backends.engines_available", lambda: True)

    def boom(*a, **k):
        raise MediaError("нет доступа к микрофону")

    monkeypatch.setattr(setup_mod, "build_live", boom)
    rt = BarRuntime(prefs_file=tmp_path / "bar.json")
    rt.handle("start")
    rt.join(timeout=10)
    assert rt.state.phase == Phase.IDLE and "микрофону" in rt.state.error
    assert title(rt.state).icon == "warning"


NODE = "http://100.64.0.9:8123"


def test_summary_waits_for_the_down_server_and_retries_there(monkeypatch, tmp_path) -> None:
    """Summarize while the server does not answer: no error, the card says "<server> is
    unavailable" with Retry; a retry after the server is back summarizes there."""
    import litellm

    from mnema.bar.recordings import library

    root = tmp_path / "notes"
    monkeypatch.setenv("MNEMA_LIVE_DIR", str(root))
    monkeypatch.setattr(base, "STANDALONE", False)  # Linux: the server processes
    monkeypatch.setenv("MNEMA_RUNNER", NODE)
    folder = root / "2026-10-02_13-05"
    folder.mkdir(parents=True)
    (folder / "transcript.txt").write_text("mic: релиз в пятницу\n", encoding="utf-8")
    monkeypatch.setattr("mnema.client.MnemaClient.health", lambda *a, **kw: None)
    platform = _Notes()
    rt = BarRuntime(platform=platform, prefs_file=tmp_path / "bar.json")
    rt.handle(f"summarize:{folder}")
    rt.join(timeout=10)
    assert rt.state.unreachable == {folder: "100.64.0.9:8123"} and rt.state.error == ""
    assert platform.calls[-1] == (
        "100.64.0.9:8123 недоступен", "Пересказ не составлен", folder,
    )  # fmt: skip
    assert rt.state.phase == Phase.IDLE and not (folder / "summary.md").exists()

    answer = {"title": "Релиз", "about": "Договорились о релизе.", "summary": ["релиз в пятницу"]}
    reply = json.dumps(answer, ensure_ascii=False)
    monkeypatch.setattr(litellm, "completion", lambda **kw: _stream(reply))
    monkeypatch.setattr("mnema.client.MnemaClient.health", lambda *a, **kw: {"ok": True})
    [card] = library(root).cards
    rt.handle(f"record:{card.id}:retry")
    rt.join(timeout=10)
    assert (root / "2026-10-02_Релиз" / "summary.md").exists() and rt.state.unreachable == {}
    assert platform.calls[-1][0] == "Пересказ готов"


def test_retry_repeats_the_processing_that_did_not_reach_the_server(monkeypatch, tmp_path) -> None:
    """Retry repeats the processing that did not reach the server: part of the recording,
    language, title, recognition; on the server selected now."""
    from mnema.bar.recordings import library

    root = tmp_path / "notes"
    monkeypatch.setenv("MNEMA_LIVE_DIR", str(root))
    folder = root / "2026-10-02_13-05"
    _wav(folder / "mic.wav")
    seen: list[tuple] = []

    def fake_recover(base, config, connection, *, start, end, retitle, title, **kw):
        seen.append((base, start, end, config.lang, config.asr, retitle, title))
        if len(seen) == 1:
            down = remarks.server_down("srv", "mnema finish x")
            return LiveResult(base=base, defer_command="mnema finish x", remarks=[down]), ""
        return LiveResult(base=base), ""

    monkeypatch.setattr("mnema.live.recording_client.recover_on_node", fake_recover)
    rt = BarRuntime(platform=_Notes(), prefs_file=tmp_path / "bar.json")
    rt.recover(folder, start=10.0, end=50.0, title="Итоги", lang="en", asr="whisper")
    rt.join(timeout=10)
    assert list(rt.state.unreachable) == [folder]
    rt.prefs.asr = "parakeet"  # a retry does not change the settings of later recordings
    [card] = library(root).cards
    rt.handle(f"record:{card.id}:retry")
    rt.join(timeout=10)
    assert seen == [(folder, 10.0, 50.0, "en", "whisper", True, "Итоги")] * 2
    assert rt.state.unreachable == {}


def test_own_title_of_interrupted_processing_survives_a_restart(monkeypatch, tmp_path) -> None:
    """The own title lives in the running work file: after an app restart mid-processing,
    "Process…" offers it and the processing gets it."""
    from mnema.bar.range_pick import RangeAnswer
    from mnema.live.recording_client import own_title, set_own_title

    root = tmp_path / "notes"
    monkeypatch.setenv("MNEMA_LIVE_DIR", str(root))
    folder = root / "2026-10-02_13-05"
    _wav(folder / "mic.wav")
    set_own_title(folder, "Планёрка", create=True)  # a restart interrupted the processing
    offered: list[str] = []
    sent: list[str | None] = []

    class Whole(_Notes):
        def ask_range(self, req, on_done) -> None:
            offered.append(req.name)
            on_done(RangeAnswer(start="", end="", title=req.name))

    def fake_recover(base, config, connection, **kw):
        sent.append(own_title(base))
        return LiveResult(base=base), ""

    monkeypatch.setattr("mnema.live.recording_client.recover_on_node", fake_recover)
    rt = BarRuntime(platform=Whole(), prefs_file=tmp_path / "bar.json")
    rt.handle(f"recover:{folder}")
    rt.join(timeout=10)
    assert offered == ["Планёрка"] and sent == ["Планёрка"]


def test_down_node_keeps_the_recording_for_later(monkeypatch, tmp_path) -> None:
    """Recording while the node is unavailable: audio is kept, nothing is recognized here;
    the notification, the remark and the card say "<node> is unavailable", and the remark
    does not claim there is no recognition here. "Process" later goes through the node
    again, never to this computer; "Choose server" opens the processing settings; Retry
    after the node is back processes there."""
    import mnema.live.setup as setup_mod
    from mnema.bar.recordings import library

    captured: dict = {}
    _patch_record(monkeypatch, tmp_path, captured)
    monkeypatch.setattr(base, "STANDALONE", False)  # Linux: the server processes
    monkeypatch.setenv("MNEMA_RUNNER", NODE)
    monkeypatch.setattr("mnema.client.MnemaClient.health", lambda *a, **kw: None)
    monkeypatch.setattr("mnema.bar.runtime._system_available", lambda: False)
    # this computer has recognition
    monkeypatch.setattr("mnema.core.backends.engines_available", lambda: True)
    recognized = setup_mod.dispatch  # the test node recognizes in this process
    monkeypatch.setattr(
        setup_mod, "dispatch", lambda *a, **k: pytest.fail("на этом компьютере не распознаём")
    )
    notes = _Notes()
    rt = BarRuntime(platform=notes, prefs_file=tmp_path / "bar.json")
    rt.handle("start")
    assert captured["started"].wait(timeout=10)
    assert rt.state.notice == "100.64.0.9:8123 недоступен, сохранится только звук"
    folder = rt._prepared.base
    rt.handle("stop")
    rt.join(timeout=10)
    assert notes.calls[-1] == ("100.64.0.9:8123 недоступен", "Запись сохранена, обработайте позже",
                               folder)  # fmt: skip
    server, draft = rt.state.last_warnings
    assert server.cause == "100.64.0.9:8123 недоступен"
    assert draft.cause == "Итоговое распознавание не завершилось; сохранён живой черновик"
    assert "привет" in (folder / "transcript.txt").read_text(encoding="utf-8")
    assert rt.state.unreachable == {folder: "100.64.0.9:8123"}
    assert server.hint == "Нажмите «Повторить» в карточке записи" and not server.command
    opened: list[Path] = []
    monkeypatch.setattr(notes, "open", opened.append, raising=False)
    rt.handle("open_notes")  # Details are about this attempt: it is not in the history
    details = opened[-1].read_text(encoding="utf-8")
    assert "100.64.0.9:8123 недоступен" in details and "mnema " not in details

    _wav(folder / "mic.wav")  # the recording's audio (the session in the test does not write it)
    seen = len(notes.calls)
    rt.recover(folder)
    rt.join(timeout=10)
    assert notes.calls[seen:] == [
        ("100.64.0.9:8123 недоступен", "Запись сохранена, обработайте позже", folder)
    ]
    rt.handle("choose_server")
    assert notes.settings == ["server"]

    [card] = library(tmp_path / "notes").cards
    monkeypatch.setattr("mnema.client.MnemaClient.health", lambda *a, **kw: {"ok": True})
    monkeypatch.setattr(setup_mod, "dispatch", recognized)  # the node is back and recognizes
    rt.handle(f"record:{card.id}:retry")
    rt.join(timeout=30)
    assert notes.calls[-1][0] == "Запись обработана" and rt.state.unreachable == {}
    assert rt.state.error == ""


def test_tick_builds_meters_and_timer(monkeypatch, tmp_path) -> None:
    import time as time_mod

    rt = BarRuntime(prefs_file=tmp_path / "bar.json")
    now = time_mod.monotonic()
    mic, system = _FakeWriter(tmp_path / "mic.wav"), _FakeWriter(tmp_path / "system.wav")
    rt._prepared = types.SimpleNamespace(writers={"mic": mic, "system": system})
    rt.state.phase = Phase.RECORDING
    rt._started = now - 10
    rt.tick()
    rt.tick()
    m = {x.label: x for x in rt.state.meters}
    assert m["mic"].history == [0.5, 0.5] and set(m) == {"mic", "system"}
    assert 9 <= rt.state.elapsed <= 12


def test_wav_writer_meters_level(tmp_path) -> None:
    import array

    from mnema.live.record import WavTrackWriter

    w = WavTrackWriter(tmp_path / "mic.wav")
    w.start()
    w.write(array.array("h", [0] * 1600).tobytes())
    assert w.level == 0.0
    w.write(array.array("h", [8000, -8000] * 800).tobytes())
    assert w.level > 0.5
    w.stop()


def test_linux_desktop_entry_and_autostart(monkeypatch, tmp_path) -> None:
    from mnema.bar import install

    monkeypatch.setattr(install.platform, "system", lambda: "Linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    desktop = install.install_linux_desktop()
    text = desktop.read_text(encoding="utf-8")
    assert "Terminal=false" in text and "-m mnema.bar" in text and "PATH=" in text
    assert install.set_autostart(True) and install.autostart_enabled()
    assert (tmp_path / "config" / "autostart" / "mnema-bar.desktop").exists()
    assert not install.set_autostart(False) and not install.autostart_enabled()


def _assignments(args: list[str]) -> dict[str, str]:
    """Variables at the start of an `env K=V … program` command."""
    import itertools
    import re

    pairs = itertools.takewhile(lambda a: re.fullmatch(r"\w+=.*", a), args)
    return dict(a.split("=", 1) for a in pairs)


def test_linux_launchers_read_the_settings_dir_the_installer_wrote(monkeypatch, tmp_path) -> None:
    """XDG_CONFIG_HOME is set only in the installer's shell and the session does not know
    it: the app from the menu and autostart reads settings where the installer and the CLI
    wrote them."""
    import shlex

    from mnema.bar import install
    from mnema.config import config_dir

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    installed = config_dir()
    entry = install.install_linux_desktop().read_text(encoding="utf-8")
    exec_line = next(line for line in entry.splitlines() if line.startswith("Exec="))
    desktop = shlex.split(exec_line.removeprefix("Exec="))
    assert desktop[0] == "env"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    for key, value in _assignments(desktop[1:]).items():
        monkeypatch.setenv(key, value)
    assert config_dir() == installed


def test_autostart_checkmark_reflects_real_file(monkeypatch, tmp_path) -> None:
    """Autostart turned on by the install (the file exists) shows checked even when bar.json
    says false."""
    from mnema.bar import install

    monkeypatch.setattr(install, "autostart_enabled", lambda: True)
    save_prefs(BarPrefs(autostart=False), tmp_path / "bar.json")
    rt = BarRuntime(prefs_file=tmp_path / "bar.json")
    assert rt.prefs.autostart is True


def test_pause_stops_writers_and_timer(tmp_path) -> None:
    """Pause: all tracks stop writing at once and the timer stops; after the pause it runs
    again."""
    import time as time_mod

    rt = BarRuntime(prefs_file=tmp_path / "bar.json")
    mic, system = _FakeWriter(tmp_path / "mic.wav"), _FakeWriter(tmp_path / "system.wav")
    mic.paused = system.paused = False
    rt._prepared = types.SimpleNamespace(writers={"mic": mic, "system": system})
    rt.state.phase = Phase.RECORDING
    rt._started = time_mod.monotonic() - 10
    rt.handle("pause")
    assert rt.state.paused and mic.paused and system.paused
    assert title(rt.state).icon == "paused"
    menu_keys = _keys(menu(rt.state, rt.prefs, MenuEnv()))
    assert "resume" in menu_keys and "pause" not in menu_keys
    rt._paused_at -= 5  # the pause lasted 5 s
    rt.tick()
    assert 4 <= rt.state.elapsed <= 6  # 10 s since the start, 5 s of them paused
    rt.handle("resume")
    assert not rt.state.paused and not mic.paused and not system.paused


def test_toggle_source_mid_recording_writes_silence_and_shows_state(tmp_path) -> None:
    """A source is turned off and on mid-recording: an off source writes silence (to stay in
    sync), its menu row is checked while recording, and it raises no alarm."""
    import array
    import time as time_mod
    import wave

    from mnema.live.record import WavTrackWriter

    rt = BarRuntime(prefs_file=tmp_path / "bar.json")
    mic = WavTrackWriter(tmp_path / "mic.wav")
    mic.start()
    system = _FakeWriter(tmp_path / "system.wav")
    system.muted = True  # system audio was off at the start
    rt._prepared = types.SimpleNamespace(writers={"mic": mic, "system": system})
    rt.state.phase = Phase.RECORDING
    rt._started = time_mod.monotonic()
    rt.tick()
    items = menu(rt.state, rt.prefs, MenuEnv())
    assert _find(items, "source:mic").checked and not _find(items, "source:system").checked
    assert "выключен" in _find(items, "source:system").title
    assert title(rt.state).icon == "recording"  # an off source is not an alarm

    rt.handle("source:mic")
    loud = array.array("h", [9000] * 1600).tobytes()
    mic.write(loud)
    mic.stop()
    with wave.open(str(tmp_path / "mic.wav")) as w:
        frames = w.readframes(w.getnframes())
    assert len(frames) == len(loud) and set(frames) == {0}  # silence of the same length
    rt.handle("source:system")
    assert not system.muted


def _wav(path: Path, frames: int = 16000) -> None:
    import array
    import wave

    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(array.array("h", [3000] * frames).tobytes())


def test_unfinished_sessions_found_and_recovered(monkeypatch, tmp_path) -> None:
    """An interrupted recording (process killed): WAV without text shows as unfinished in
    the menu; "Process" runs the normal finalization over the saved tracks and gives the
    transcript."""
    import mnema.live.setup as setup_mod
    from mnema.live.setup import unfinished_sessions

    notes = tmp_path / "notes"
    _wav(notes / "live-1" / "system.wav")
    _wav(notes / "done" / "mic.wav")
    (notes / "done" / "transcript.txt").write_text("x", encoding="utf-8")
    (notes / "empty").mkdir()
    assert unfinished_sessions(notes) == [notes / "live-1"]

    monkeypatch.setenv("MNEMA_LIVE_DIR", str(notes))
    monkeypatch.delenv("MNEMA_RUNNER", raising=False)
    monkeypatch.setattr("mnema.bar.runtime._system_available", lambda: False)
    calls: list[dict] = []

    def fake_dispatch(op, src, params, *, runner=None, local=False, on=None):
        calls.append({"src": Path(src).name, "params": params})
        return {"text": "Итоги.", "segments": [{"start": 0.0, "end": 1.0, "text": "Итоги."}]}

    monkeypatch.setattr(setup_mod, "dispatch", fake_dispatch)
    notes_ = _WholeRecording()
    rt = BarRuntime(platform=notes_, prefs_file=tmp_path / "bar.json")
    items = menu(rt.state, rt.prefs, MenuEnv())
    unfinished = next(i for i in items if i.title.startswith("⚠︎ Не обработана"))
    assert "Без названия" in unfinished.title or "live-1" in unfinished.title
    rt.handle(next(c.key for c in unfinished.children if c.title == "Обработать…"))
    rt.join(timeout=10)
    assert calls and calls[0]["src"] == "system.wav"
    assert rt.state.last_result is not None and rt.state.last_result.name == "transcript.txt"
    assert "Итоги." in rt.state.last_result.read_text(encoding="utf-8")
    assert rt.state.unfinished == [] and notes_.calls[-1][0] == "Запись обработана"


class _WholeRecording(_Notes):
    """The "Process recording" window: the whole recording, as offered."""

    def ask_range(self, req, on_done) -> None:
        from mnema.bar.range_pick import RangeAnswer

        on_done(RangeAnswer(start="", end="", title=req.name))


def test_discreet_toggle_available_mid_recording(tmp_path) -> None:
    """Hiding the recording in the menu bar works during recording and processing, at once."""
    rt = BarRuntime(prefs_file=tmp_path / "bar.json")
    rt.state.phase = Phase.RECORDING
    rt.state.elapsed = 42
    assert title(rt.state, discreet=rt.prefs.discreet).icon == "recording"
    assert not _find(menu(rt.state, rt.prefs, MenuEnv()), "discreet").checked
    rt.handle("discreet")
    assert title(rt.state, discreet=rt.prefs.discreet).model_dump() == {"icon": "idle", "text": ""}
    assert _find(menu(rt.state, rt.prefs, MenuEnv()), "discreet").checked
    rt.state.phase = Phase.IDLE  # the recording ended and is being processed
    rt.state.jobs = [Job(id="r", folder=tmp_path / "r", running=True)]
    assert title(rt.state, discreet=rt.prefs.discreet).icon == "idle"
    rt.handle("discreet")  # turn it back (when idle — from the settings)
    assert title(rt.state, discreet=rt.prefs.discreet).icon == "saving"


class _AskPlatform(_Notes):
    def __init__(self, answer: str | None) -> None:
        super().__init__()
        self.answer = answer
        self.asked: list[str | None] = []
        self.cancels: list[str] = []

    def ask_title(self, current, on_done, **kwargs) -> None:
        self.asked.append(current)
        self.cancels.append(kwargs["cancel"])
        on_done(self.answer)


def test_title_set_during_processing_names_folder(monkeypatch, tmp_path) -> None:
    """Set title… during processing: the entered name replaces the model's one, the folder
    is "<date> <name>"; only the window the user opened shows."""
    import mnema.live.setup as setup_mod

    captured: dict = {}
    _patch_record(monkeypatch, tmp_path, captured)
    monkeypatch.setattr(setup_mod, "dispatch", lambda *a, **k: {"text": "", "segments": [
        {"start": 0.0, "end": 1.0, "text": "Привет."}]})  # fmt: skip
    monkeypatch.setattr(
        setup_mod.claude,
        "title_ex",
        lambda *a, **k: pytest.fail("название задано — модель не зовём"),
    )
    pressed: list[bool] = []
    rt: BarRuntime | None = None  # the recognition list may load before rt is assigned

    def on_change() -> None:  # the card showed processing and "Set title…" was clicked
        if rt is not None and rt.state.running is not None and not pressed:
            pressed.append(True)
            rt.handle(_job_key(rt, "title"))

    platform = _AskPlatform("Дейли с командой")
    rt = BarRuntime(on_change=on_change, platform=platform, prefs_file=tmp_path / "bar.json")
    rt.handle("start")
    assert captured["started"].wait(timeout=10)
    rt.handle("stop")
    rt.join(timeout=20)
    assert platform.asked == [None]
    assert rt.state.last_result is not None
    assert rt.state.last_result.parent.name.endswith("_Дейли с командой")


def test_title_set_after_the_result_is_applied_names_the_folder(monkeypatch, tmp_path) -> None:
    """Set title… clicked after the result was applied while processing still runs (no
    work file): the entered name names the folder, not the model, and no window opens after
    processing."""
    import mnema.live.setup as setup_mod

    captured: dict = {}
    _patch_record(monkeypatch, tmp_path, captured)
    monkeypatch.setattr(setup_mod.claude, "title_ex", lambda *a, **k: ("Модель", "cloud", None))

    class Late(_AskPlatform):
        def notify(self, title: str, message: str, path: Path | None) -> None:  # noqa: A002
            super().notify(title, message, path)
            if title == "Расшифровка готова":
                rt.handle(_job_key(rt, "title"))

    platform = Late("Дейли с командой")
    rt = BarRuntime(platform=platform, prefs_file=tmp_path / "bar.json")
    rt.prefs.summary = False
    rt.handle("start")
    assert captured["started"].wait(timeout=10)
    rt.handle("stop")
    rt.join(timeout=20)
    assert rt.state.last_result is not None
    assert rt.state.last_result.parent.name.endswith("_Дейли с командой")
    assert rt.state.last_result.exists() and len(platform.asked) == 1


@pytest.mark.parametrize("answer", [None, "", "Созвон по релизу", "Новая тема"])
def test_stop_prefills_cloud_title_and_allows_editing(monkeypatch, tmp_path, answer) -> None:
    """The window also opens after a cloud title; skip keeps the suggested name."""
    import litellm

    captured: dict = {}
    _patch_record(monkeypatch, tmp_path, captured)
    calls = []

    def completion(**kwargs):
        calls.append(kwargs)
        return iter(
            [
                types.SimpleNamespace(
                    choices=[
                        types.SimpleNamespace(
                            delta=types.SimpleNamespace(content="Созвон по релизу"),
                            finish_reason="stop",
                        )
                    ]
                )
            ]
        )

    monkeypatch.setattr(litellm, "completion", completion)
    platform = _AskPlatform(answer)
    rt = BarRuntime(platform=platform, prefs_file=tmp_path / "bar.json")
    rt.prefs.summary = False
    rt.handle("start")
    assert captured["started"].wait(timeout=10)
    rt.handle("stop")
    rt.join(timeout=20)
    assert calls and not rt.state.error
    assert platform.asked == ["Созвон по релизу"] and platform.cancels == ["Пропустить"]
    expected = answer or "Созвон по релизу"
    assert rt.state.last_result.parent.name.endswith("_" + expected)


def test_rename_last_recording_after_processing(tmp_path) -> None:
    """Rename… on the last recording: the folder moves and the menu path follows; a
    duplicate name does not merge folders."""
    base = tmp_path / "2026-09-26_23-13"
    base.mkdir()
    (base / "transcript.txt").write_text("x", encoding="utf-8")
    other = tmp_path / "2026-09-26_Итоги"
    other.mkdir()
    platform = _AskPlatform("Итоги")
    rt = BarRuntime(platform=platform, prefs_file=tmp_path / "bar.json")
    rt.state.last_result = base / "transcript.txt"
    rt.handle("rename")
    assert platform.asked == [None]  # an untitled recording has an empty field
    assert rt.state.last_result == tmp_path / "2026-09-26_Итоги (2)" / "transcript.txt"
    assert rt.state.last_result.exists() and other.exists()
    platform.answer = "Итоги спринта"
    rt.handle("rename")
    assert platform.asked[-1] == "Итоги"  # the current title is filled in
    # only the window after processing is skipped
    assert platform.cancels == [
        "Отменить",
        "Отменить",
    ]
    assert rt.state.last_result.parent.name == "2026-09-26_Итоги спринта"


def test_mic_start_refreshes_portaudio_and_retries(monkeypatch) -> None:
    """Headphones changed: the first microphone open fails (-9986), the PortAudio device
    list is reread, the retry on the default device works."""
    from mnema.live import sources

    events: list[str] = []

    class Stream:
        def __init__(self, device):
            self.device = device

        def start(self):
            events.append(f"start:{self.device}")

        def stop(self):
            pass

        def close(self):
            pass

    opens = {"n": 0}

    def raw_input_stream(**kw):
        opens["n"] += 1
        if opens["n"] == 1:
            raise RuntimeError("Internal PortAudio error [PaErrorCode -9986]")
        return Stream(kw.get("device"))

    fake_sd = types.SimpleNamespace(
        RawInputStream=raw_input_stream,
        _terminate=lambda: events.append("terminate"),
        _initialize=lambda: events.append("initialize"),
        query_devices=lambda *a, **k: [],
    )
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)
    mic = sources.MicSource(label="mic")
    mic.start()
    assert events == ["terminate", "initialize", "start:None"]

    opens["n"] = -100  # now it always fails
    fake_sd.RawInputStream = lambda **kw: (_ for _ in ()).throw(RuntimeError("-9986"))
    with pytest.raises(sources.MicUnavailable):
        sources.MicSource(label="mic").start()


def test_record_falls_back_to_system_when_mic_unavailable(monkeypatch, tmp_path) -> None:
    """The microphone does not open but system audio works: recording goes on without the
    microphone with a warning, and the empty folder of the failed attempt is not left as
    unprocessed."""
    import mnema.live.session as session_mod
    import mnema.live.setup as setup_mod
    from mnema.live.sources import MicUnavailable

    captured: dict = {}
    _patch_record(monkeypatch, tmp_path, captured)
    monkeypatch.setattr("mnema.bar.runtime._system_available", lambda: True)
    configs: list[bool] = []
    real_build = setup_mod.build_live

    def build(config, base):
        configs.append(config.mic)
        return real_build(config, base=base)

    monkeypatch.setattr(setup_mod, "build_live", build)
    Fake = session_mod.LiveSession

    class MicFails(Fake):
        def run(self):
            if configs[-1]:
                raise MicUnavailable("микрофон не открылся (-9986)")
            return super().run()

    monkeypatch.setattr(session_mod, "LiveSession", MicFails)
    monkeypatch.setattr(
        "mnema.live.recording_client.finalize_on_node", lambda p, t, **k: (LiveResult(), "")
    )
    rt = BarRuntime(platform=_Notes(), prefs_file=tmp_path / "bar.json")
    rt.handle("start")
    assert captured["started"].wait(timeout=10)
    assert "Микрофон недоступен" in rt.state.notice
    rt.handle("stop")
    rt.join(timeout=10)
    assert configs == [True, False] and rt.state.error == ""
    notes = tmp_path / "notes"
    assert len([d for d in notes.iterdir() if d.is_dir()]) == 1  # only the recording that happened


@pytest.mark.parametrize("cloud", [True, False])
def test_silent_recording_named_late_is_not_unfinished(monkeypatch, tmp_path, cloud) -> None:
    """A real case: 3 seconds of silence, "Set title…" opened during processing but the
    title entered after it. The recording has no text but is processed, not "Not processed",
    and the menu has no path to the old folder name."""
    import mnema.live.setup as setup_mod

    if not cloud:
        monkeypatch.setenv("MNEMA_MODEL", "")
        monkeypatch.setenv("MNEMA_API_KEY", "")
    captured: dict = {}
    _patch_record(monkeypatch, tmp_path, captured)
    monkeypatch.setattr(setup_mod, "dispatch", lambda *a, **k: {"text": "", "segments": []})

    class LatePlatform(_Notes):
        prompts = 0

        def ask_title(self, current, on_done, **kwargs) -> None:
            self.prompts += 1
            self.later = on_done  # the window is still open, processing has finished meanwhile

    # on_change is called before the constructor ends (the recognition list)
    made: list[BarRuntime] = []

    def on_change() -> None:  # the card showed processing and "Set title…" was clicked
        job = made[0].state.running if made else None
        if job is not None and not hasattr(platform, "later"):
            made[0].handle(f"record:{job.id}:title")

    platform = LatePlatform()
    rt = BarRuntime(on_change=on_change, platform=platform, prefs_file=tmp_path / "bar.json")
    made.append(rt)
    rt.handle("start")
    assert captured["started"].wait(timeout=10)
    base = rt._prepared.base
    base.mkdir(parents=True, exist_ok=True)
    _wav(base / "mic.wav")
    rt.handle("stop")
    rt.join(timeout=10)
    assert rt.state.unfinished == []  # processed, though there is no text
    assert platform.prompts == 1
    platform.later("Qwe")  # Save clicked later
    from mnema.bar.state import folder_of

    assert rt.state.last_result is not None
    assert folder_of(rt.state.last_result).name.endswith("_Qwe")
    assert rt.state.last_result.exists() and not base.exists()
    assert rt.state.unfinished == []


def test_summary_toggle_and_open_summary(tmp_path) -> None:
    from mnema.bar.settings_model import settings_model

    rt = BarRuntime(prefs_file=tmp_path / "bar.json")
    model = settings_model(rt.prefs, rt.menu_env())
    toggle = next(f for t in model.tabs for c in t.sections for f in c.fields if f.key == "summary")
    assert toggle.checked and rt._config().summary
    rt.handle("summary")
    assert rt._config().summary is False
    assert rt._config().summary_lang is None
    rt.prefs = BarPrefs(summary_lang="en")
    assert rt._config().summary_lang == "en"
    rt.state.last_result = tmp_path / "2026-09-27_Дейли" / "transcript.txt"
    rt.state.last_summary = tmp_path / "2026-09-27_Дейли" / "summary.md"
    assert "open_summary" in _keys(menu(rt.state, rt.prefs, MenuEnv()))


def test_stop_processing_keeps_audio_and_can_process_later(monkeypatch, tmp_path) -> None:
    """A recording forgotten for a day: "Stop processing" mid-transcription keeps the audio,
    no text, and it does not hang as "Not processed" in the menu or the panel; "Process" on
    the last recording is the normal finalization."""
    import mnema.live.setup as setup_mod
    from mnema.bar.panel import panel_model
    from mnema.bar.recordings import library
    from mnema.bar.settings_model import settings_model
    from mnema.live.setup import unfinished_sessions

    notes = tmp_path / "notes"
    wav = notes / "live-1" / "mic.wav"
    _wav(wav)
    size = wav.stat().st_size
    monkeypatch.setenv("MNEMA_LIVE_DIR", str(notes))
    monkeypatch.delenv("MNEMA_RUNNER", raising=False)
    monkeypatch.setattr("mnema.bar.runtime._system_available", lambda: False)
    stop_once = {"armed": True}
    transcribing, pressed = threading.Event(), threading.Event()

    def fake_dispatch(op, src, params, *, runner=None, local=False, on=None):
        if stop_once.pop("armed", False):  # transcription runs until the menu item is clicked
            transcribing.set()
            pressed.wait(timeout=10)
        if on is not None:
            on(0.5, "расшифровка…")
        return {"text": "Итоги.", "segments": [{"start": 0.0, "end": 1.0, "text": "Итоги."}]}

    monkeypatch.setattr(setup_mod, "dispatch", fake_dispatch)
    notes_ = _WholeRecording()
    rt = BarRuntime(platform=notes_, prefs_file=tmp_path / "bar.json")
    rt.handle(f"recover:{notes / 'live-1'}")
    assert transcribing.wait(timeout=10)
    assert rt.state.phase == Phase.IDLE and rt.state.running is not None
    keys = [i.key for i in menu(rt.state, rt.prefs, MenuEnv())]
    assert _job_key(rt, "stop") in keys
    rt.handle(_job_key(rt, "stop"))
    pressed.set()
    rt.join(timeout=10)

    assert rt.state.phase == Phase.IDLE and rt.state.error == ""
    assert wav.stat().st_size == size and not (notes / "live-1" / "transcript.txt").exists()
    assert unfinished_sessions(notes) == [] and rt.state.unfinished == []
    env = MenuEnv(mac=True)
    [card] = panel_model(
        rt.state, rt.prefs, env, library(notes).cards, settings_model(rt.prefs, env)
    ).cards
    assert card.status == "Не расшифрована"
    assert notes_.calls[-1][0] == "Обработка остановлена"
    assert rt.state.last_result == notes / "live-1"
    last = next(i for i in menu(rt.state, rt.prefs, MenuEnv()) if i.title.startswith("Последняя"))
    process = next(c for c in last.children if c.title == "Обработать…")
    rt.handle(process.key)
    rt.join(timeout=10)
    assert rt.state.last_result is not None and rt.state.last_result.name == "transcript.txt"


def test_language_chosen_during_processing_restarts_it(monkeypatch, tmp_path) -> None:
    """Processing decided Russian from speech, English was chosen: it is interrupted (no
    "Processing stopped") and starts over on the same recording strictly in English, titled
    by content; the languages of the previous pass are reset. The new pass renamed the
    folder, and that folder counts as being processed (it cannot go to the Trash)."""

    captured: dict = {}
    _patch_record(monkeypatch, tmp_path, captured)

    def fake_finalize(prepared, tracks, *, on, on_lang, **kw):
        on_lang({"mic": "ru"})
        assert rt.state.running is not None and rt.state.running.languages == {"mic": "ru"}
        rt.handle(_job_key(rt, "lang:en"))  # English chosen in the card menu
        on(0.3, "распознаю…")
        raise AssertionError("обработка не прервалась")

    again: list[tuple] = []

    def fake_recover(base, config, connection, *, start, end, retitle, on_base, **kw):
        again.append((base, config.lang, start, end, retitle))
        assert rt.state.running is not None and rt.state.running.languages == {}
        renamed = base.rename(base.with_name("2026-10-01_Созвон"))  # titled by content
        on_base(renamed)
        assert rt.state.running.folder == renamed
        rt.trash_folder(renamed)
        assert renamed.exists()
        txt = renamed / "transcript.txt"
        txt.write_text("mic: hello\n", encoding="utf-8")
        return LiveResult(transcript_txt=txt), "hello"

    monkeypatch.setattr("mnema.live.recording_client.finalize_on_node", fake_finalize)
    monkeypatch.setattr("mnema.live.recording_client.recover_on_node", fake_recover)
    notes = _Notes()
    rt = BarRuntime(platform=notes, prefs_file=tmp_path / "bar.json")
    rt.handle("start")
    assert captured["started"].wait(timeout=10)
    base = rt.active_folder
    rt.handle("stop")
    rt.join(timeout=10)
    assert again == [(base, "en", None, None, True)]
    assert [c[0] for c in notes.calls] == ["Запись обработана"] and rt.state.error == ""
    assert rt.state.phase == Phase.IDLE


def _process_window(monkeypatch, tmp_path, answer, fake_recover):
    """A 2-minute recording folder; the "Process recording" window answers answer(request),
    processing is fake_recover (the boundary: recording_client.recover_on_node)."""

    folder = tmp_path / "notes" / "Созвон"
    _wav(folder / "mic.wav", frames=16000 * 120)
    monkeypatch.setenv("MNEMA_LIVE_DIR", str(folder.parent))
    monkeypatch.setattr("mnema.live.recording_client.recover_on_node", fake_recover)

    class Window(_Notes):
        requests: list = []

        def ask_range(self, req, on_done) -> None:
            self.requests.append(req)
            on_done(answer(req))

        def trash(self, path) -> None: ...

    platform = Window()
    return BarRuntime(platform=platform, prefs_file=tmp_path / "bar.json"), platform, folder


def _process_key(folder: Path) -> str:
    """Process… on a recording card."""
    from mnema.bar.recordings import library

    [card] = library(folder.parent).cards
    return f"record:{card.id}:process"


def test_process_window_language_explicit_is_strict(monkeypatch, tmp_path) -> None:
    """Process… on a card opens the processing window: the language chosen there is
    strict, Automatic goes by speech."""
    from mnema.bar.range_pick import RangeAnswer

    seen: list[tuple] = []

    def fake_recover(base, config, connection, **kw):
        seen.append((base, config.lang))
        return LiveResult(), ""

    langs = ["ru", "auto"]
    rt, window, folder = _process_window(
        monkeypatch,
        tmp_path,
        lambda req: RangeAnswer(start="", end="", title=req.name, lang=langs.pop(0)),
        fake_recover,
    )
    for _ in range(2):
        rt.handle(_process_key(folder))
        rt.join(timeout=10)
    assert len(window.requests) == 2
    assert seen == [(folder, "ru"), (folder, None)]


def test_process_window_speech_model_is_for_this_recording_only(monkeypatch, tmp_path) -> None:
    """The "Process recording" window has a recognition choice (defaulting to the settings):
    the choice applies to this recording only and the setting stays; empty means as in the
    settings."""
    from mnema.bar.range_pick import RangeAnswer

    seen: list[str] = []

    def fake_recover(base, config, connection, **kw):
        seen.append(config.asr)
        return LiveResult(), ""

    choices = ["whisper", ""]
    rt, window, folder = _process_window(
        monkeypatch,
        tmp_path,
        lambda req: RangeAnswer(start="", end="", title=req.name, asr=choices.pop(0)),
        fake_recover,
    )
    rt.handle("asr:gigaam")
    rt._asr_choices = [
        ("auto", "Автоматически"),
        ("parakeet", "Parakeet"),
        ("gigaam", "GigaAM"),
        ("whisper", "Whisper"),
    ]
    for _ in range(2):
        rt.handle(_process_key(folder))
        rt.join(timeout=10)
    req = window.requests[0]
    assert req.asr == "gigaam"
    assert [k for k, _ in req.asrs] == ["auto", "parakeet", "gigaam", "whisper"]
    assert seen == ["whisper", "gigaam"] and rt.prefs.asr == "gigaam"


def test_language_chosen_while_processing_part_redoes_the_same_part(monkeypatch, tmp_path) -> None:
    """A part of a recording with "to the Trash": a language change during processing redoes
    the same part (same range, same checkbox), cut from the full recording; no copy of the
    interrupted pass remains."""
    from mnema.bar.range_pick import RangeAnswer

    calls: list[tuple] = []

    def fake_recover(base, config, connection, *, start, end, replace_original, on, **kw):
        part = base.with_name(f"{base.name} (0.00.30–0.01.30)")
        calls.append((config.lang, start, end, bool(replace_original)))
        assert not part.exists()
        part.mkdir()
        if len(calls) == 1:
            rt.handle(_job_key(rt, "lang:en"))
            try:
                on(0.3, "")
            except BaseException as e:
                e.mnema_base = (
                    part  # as in the real one: where the recording is after the interruption
                )
                raise
        txt = part / "transcript.txt"
        txt.write_text("mic: hello\n", encoding="utf-8")
        return LiveResult(transcript_txt=txt, base=part), "hello"

    rt, _window, folder = _process_window(
        monkeypatch,
        tmp_path,
        lambda req: RangeAnswer(start="0:00:30", end="0:01:30", title=req.name, replace=True),
        fake_recover,
    )
    rt.handle(_process_key(folder))
    rt.join(timeout=10)
    assert calls == [(None, 30.0, 90.0, True), ("en", 30.0, 90.0, True)]
    assert rt.state.last_result == folder.with_name("Созвон (0.00.30–0.01.30)") / "transcript.txt"
    assert (folder / "mic.wav").exists()


def test_applied_language_or_stop_does_not_restart_processing(monkeypatch, tmp_path) -> None:
    """Choosing the language already applied to all tracks lets processing go on; choosing
    another and clicking "Stop processing" just stops, the audio stays."""
    runs: list[str | None] = []

    def fake_recover(base, config, connection, *, on, on_lang, **kw):
        runs.append(config.lang)
        on_lang({"mic": "ru", "system": "ru"})
        rt.handle(_job_key(rt, "lang:ru"))
        on(0.5, "")
        rt.handle(_job_key(rt, "lang:en"))
        rt.handle(_job_key(rt, "stop"))
        on(0.6, "")
        raise AssertionError("обработка не остановилась")

    rt, window, folder = _process_window(monkeypatch, tmp_path, lambda req: None, fake_recover)
    rt.recover(folder)
    rt.join(timeout=10)
    assert runs == [None] and rt.state.error == ""
    assert window.calls[-1][0] == "Обработка остановлена"
    assert rt.state.last_result == folder


def test_second_bar_instance_does_not_start(monkeypatch, tmp_path) -> None:
    """A second launch (./install.sh, the menu, autostart) does not start a second Mnema: no
    second icon and no second recording over the running one."""
    import fcntl

    from mnema.bar import app

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(app, "_LOCK", None)
    lock = tmp_path / ".cache" / "mnema" / "bar.lock"
    lock.parent.mkdir(parents=True)
    other = lock.open("w")  # another Mnema holds the lock
    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert not app._single_instance()
    other.close()  # that one closed, so it is free
    assert app._single_instance()
    # the engine restarted in the same process: the lock is already ours
    assert app._single_instance()
    app._LOCK.close()  # ty: ignore[unresolved-attribute]


def test_linux_tray_menu_accepted_by_pystray(monkeypatch, tmp_path) -> None:
    """The whole menu (with submenus) is built by pystray: pystray rejected an item handler
    with a third positional parameter (ValueError in _assert_action), so the icon never
    showed in the GNOME panel. Without pystray (CI, a Mac without the extra) it is skipped."""
    pystray = pytest.importorskip("pystray")
    from mnema.bar import app_linux

    monkeypatch.setenv("HOME", str(tmp_path))
    tray = app_linux.TrayApp.__new__(app_linux.TrayApp)
    tray.runtime = BarRuntime(prefs_file=tmp_path / "bar.json")
    items = menu(tray.runtime.state, tray.runtime.prefs, tray.runtime.menu_env())
    pystray.Menu(*tray._to_pystray(items))


def test_processing_notes_are_short_and_full_text_opens_from_menu(tmp_path) -> None:
    """Menu remarks are one or two human lines (why and what came out), not cut technical
    logs; the full text comes from the recording's history, the Details item."""
    from mnema.bar.state import readable_notes

    # the model account ran out of money: no summary and no title
    refused = LightNote(
        engine="none",
        cloud=Cloud.FAILED,
        cloud_detail="Insufficient Balance",
        kind=ErrorKind.NO_MONEY,
        model="deepseek/deepseek-chat",
    )
    warnings = [_light(step, refused) for step in (Step.SUMMARY, Step.TITLE)]
    assert readable_notes(warnings) == [
        "Закончились деньги на счёте",
        "Без пересказа, без названия",
    ]
    assert readable_notes([remarks.mix_failed("ffmpeg exit 1")]) == [
        "Без общего mp3",
    ]

    folder = tmp_path / "2026-09-27_18-33"
    folder.mkdir()
    txt = folder / "transcript.txt"
    txt.write_text("x", encoding="utf-8")
    run = {"status": "ok", "notes": [w.model_dump(mode="json") for w in warnings]}
    (folder / ".mnema-meta.json").write_text(json.dumps({"version": 1, "runs": [run]}))

    class Opening(_Notes):
        opened: list[Path] = []

        def open(self, path: Path) -> None:
            self.opened.append(path)

    platform = Opening()
    rt = BarRuntime(platform=platform, prefs_file=tmp_path / "bar.json")
    rt._finish(LiveResult(transcript_txt=txt, remarks=warnings), folder, lambda **_: None)
    last = next(i for i in menu(rt.state, rt.prefs, MenuEnv()) if i.title.startswith("Последняя"))
    titles = [c.title for c in last.children]
    assert "⚠︎ Закончились деньги на счёте" in titles
    assert all(len(t) <= 60 for t in titles)
    details = next(c for c in last.children if c.title == "Подробности")
    rt.handle(details.key)
    [opened] = platform.opened
    full = opened.read_text(encoding="utf-8")
    assert "закончились деньги на счёте" in full and "Пополните счёт у провайдера" in full
    assert "Запись названа по времени" in full  # the effect in full


def test_linux_recording_menu_does_not_change_every_tick() -> None:
    """GNOME rebuilds the whole menu on every update: during a recording it must not change
    from tick to tick (the timer and levels made it blink)."""
    from mnema.bar.state import SourceMeter

    def rec_menu(elapsed: float, level: float) -> list[str]:
        state = BarState(
            phase=Phase.RECORDING,
            elapsed=elapsed,
            meters=[SourceMeter(label="mic", history=[level]), SourceMeter(label="system")],
        )
        return [i.title for i in menu(state, BarPrefs(), MenuEnv()) if not i.separator]

    assert rec_menu(61, 0.2) == rec_menu(62, 0.9)


def test_linux_panel_label_and_saving_menu_change_rarely() -> None:
    """GNOME redraws the indicator on every caption change and closes an open menu when it
    is rebuilt: the caption goes in minutes and 5% steps, the processing menu has no
    percentages."""
    from mnema.bar.state import title

    def rec(elapsed: float) -> str:
        return title(BarState(phase=Phase.RECORDING, elapsed=elapsed), coarse=True).text

    assert rec(61) == rec(119) == "1 мин" and rec(120) == "2 мин"
    # the Mac shows seconds
    assert title(BarState(phase=Phase.RECORDING, elapsed=61)).text != rec(61)

    def job(**kw) -> Job:
        return Job(id="r", folder=Path("/notes/2026-10-01_10-05"), running=True, **kw)

    def saving(step: float) -> tuple[str, list[str]]:  # share of the first step
        progress = step / len(FINALIZE_STEPS)
        state = BarState(jobs=[job(stage=STEP_MESSAGES["asr"], progress=progress)])
        items = [i.title for i in menu(state, BarPrefs(), MenuEnv(mac=False)) if not i.separator]
        return title(state, coarse=True).text, items

    assert saving(0.21) == saving(0.24)  # step shares of 21% and 24% are one 5% step
    assert saving(0.21)[0] == ""  # the icon has no percentage
    assert "Распознаю речь… 20\u00a0%" in saving(0.21)[1]  # the menu shows the running step's share
    state = BarState(jobs=[job(progress=0.5, elapsed=250)])
    assert any(i.title.endswith("· 4 мин") for i in menu(state, BarPrefs(), MenuEnv()))
    assert title(BarState(phase=Phase.RECORDING), discreet=True, coarse=True).text == ""


def test_menu_while_processing_shows_language_and_offers_the_other() -> None:
    """The tray menu during processing matches the panel: the language already applied to
    the recording and a choice of another one to redo it in."""

    def lang_item(**kw) -> tuple:
        state = BarState(jobs=[Job(id="r", folder=Path("/n/r"), running=True, **kw)])
        [item] = [i for i in menu(state, BarPrefs(), MenuEnv()) if i.title.startswith("Язык")]
        return item.title, [(c.key, c.checked) for c in item.children]

    ru, en = "record:r:lang:ru", "record:r:lang:en"
    assert lang_item() == ("Язык: определяется…", [(ru, False), (en, False)])
    assert lang_item(languages={"mic": "ru", "system": "ru"}) == (
        "Язык: русский", [(ru, True), (en, False)],
    )  # fmt: skip
    assert lang_item(languages={"mic": "de"})[0] == "Язык: немецкий"


def test_source_off_whole_recording_leaves_no_track(monkeypatch, tmp_path) -> None:
    """System audio is off in the settings and was never turned on: system.wav (silence for
    sync) is not kept or transcribed; turned on even once, the track stays."""

    for switch_on in (False, True):
        captured: dict = {}
        _patch_record(monkeypatch, tmp_path / str(switch_on), captured)
        monkeypatch.setattr("mnema.bar.runtime._system_available", lambda: True)
        seen: dict = {}

        def fake_finalize(prepared, tracks, *, on, seen=seen, **kw):
            seen["writers"] = sorted(prepared.writers)
            seen["files"] = sorted(p.name for p in prepared.base.glob("*.wav"))
            seen["tracks"] = [t.label for t in tracks]
            return LiveResult(), ""

        monkeypatch.setattr("mnema.live.recording_client.finalize_on_node", fake_finalize)
        rt = BarRuntime(prefs_file=tmp_path / f"bar-{switch_on}.json")
        rt.prefs.system = False
        rt.handle("start")
        assert captured["started"].wait(timeout=10)
        writers = rt._prepared.writers
        for w in writers.values():
            w.start()
            w.write(b"\x10\x00" * 160)
        if switch_on:
            rt.handle("source:system")  # turned on mid-recording
            writers["system"].write(b"\x10\x00" * 160)
        rt.handle("stop")
        rt.join(timeout=10)
        want = ["mic", "system"] if switch_on else ["mic"]
        assert seen["writers"] == want and seen["tracks"] == ["mic"]
        assert seen["files"] == [f"{label}.wav" for label in want]


def _range_setup(monkeypatch, tmp_path, answer):
    """Process recording…: a folder with two 2-minute tracks, the window answers
    answer(request), the Trash is the trash folder (Finder/gio is the external boundary)."""
    import mnema.live.setup as setup_mod

    folder = tmp_path / "Созвон из архива"
    _wav(folder / "mic.wav", frames=16000 * 120)
    _wav(folder / "system.wav", frames=16000 * 120)
    monkeypatch.delenv("MNEMA_RUNNER", raising=False)
    monkeypatch.setattr("mnema.bar.runtime._system_available", lambda: False)
    lengths: list[float] = []

    def fake_dispatch(op, src, params, *, runner=None, local=False, on=None):
        import wave

        with wave.open(str(src), "rb") as w:
            lengths.append(w.getnframes() / 16000)
        return {"text": "Итоги.", "segments": [{"start": 0.0, "end": 1.0, "text": "Итоги."}]}

    monkeypatch.setattr(setup_mod, "dispatch", fake_dispatch)

    class Picking(_Notes):
        requests: list = []

        def choose_folder(self, start, on_done) -> None:
            on_done(folder)

        def ask_range(self, req, on_done) -> None:
            self.requests.append(req)
            on_done(answer(req))

        def trash(self, path) -> None:
            (tmp_path / "trash").mkdir(exist_ok=True)
            path.rename(tmp_path / "trash" / path.name)

    platform = Picking()
    rt = BarRuntime(platform=platform, prefs_file=tmp_path / "bar.json")
    return rt, platform, folder, lengths


def _details(folder: Path) -> str:
    """A recording's Details are the full text from its processing history."""
    from mnema.live import meta

    data = json.loads((folder / meta.META_FILE).read_text(encoding="utf-8"))
    return remarks.details(meta.remarks_of(data), meta.info_of(data))


def test_process_part_keeping_full_recording(monkeypatch, tmp_path) -> None:
    """A part of a recording without "to the Trash": only it is processed (both tracks cut
    the same way), into its own folder next to the original with a mark; the full recording
    is untouched; the choice is remembered."""
    from mnema.bar.range_pick import RangeAnswer

    def answer(req):
        return RangeAnswer(start="0:00:00", end="0:01:00", title=req.name, replace=False)

    rt, platform, folder, lengths = _range_setup(monkeypatch, tmp_path, answer)
    assert any(i.key == "process_folder" for i in menu(rt.state, rt.prefs, MenuEnv()))
    rt.handle("process_folder")
    rt.join(timeout=10)

    req = platform.requests[0]
    assert (req.length, req.name, req.replace) == (120.0, "Созвон из архива", True)
    assert req.subtitle.endswith("0:02:00")  # the window sees the recording length
    assert lengths == [60.0, 60.0]  # one minute of two, both tracks
    part = tmp_path / "Созвон из архива (0.00–0.01)"
    assert rt.state.last_result == part / "transcript.txt"
    assert (folder / "mic.wav").exists() and not (folder / "transcript.txt").exists()
    assert "Часть записи 0:00:00–0:01:00 из 0:02:00" in _details(part)
    assert not any("Часть записи" in r.text for r in rt.state.last_warnings)  # info
    assert load_prefs(tmp_path / "bar.json").replace_original is False


def test_process_part_replaces_full_recording(monkeypatch, tmp_path) -> None:
    """The "Move the full recording to the Trash" checkbox: after a part is processed the
    full one is in the Trash and the part takes its place and name (without a mark)."""
    from mnema.bar.range_pick import RangeAnswer

    def answer(req):
        return RangeAnswer(start="0:00:30", end="0:01:30", title=req.name, replace=True)

    rt, _platform, folder, lengths = _range_setup(monkeypatch, tmp_path, answer)
    rt.handle("process_folder")
    rt.join(timeout=10)

    assert lengths == [60.0, 60.0]
    # the full one is in the Trash
    assert (tmp_path / "trash" / "Созвон из архива" / "mic.wav").exists()
    assert rt.state.last_result == folder / "transcript.txt"  # the part is in its place
    assert "Полная запись перемещена в Корзину" in _details(folder)
    assert not list(tmp_path.glob("Созвон из архива (*"))


def test_process_whole_recording_with_own_title_and_errors(monkeypatch, tmp_path) -> None:
    """The whole recording: the full one is not trashed (the checkbox does not matter); an
    own title names the folder. A wrong time is a clear error in the menu, without
    processing; Cancel does nothing."""
    from mnema.bar.range_pick import RangeAnswer

    answers: list = [RangeAnswer(start="0:00:00", end="1:75")]

    rt, _platform, folder, lengths = _range_setup(monkeypatch, tmp_path, lambda r: answers[0])
    rt.handle("process_folder")
    rt.join(timeout=10)
    assert lengths == [] and "Не понял время" in rt.state.error

    answers[0] = None
    rt.handle("process_folder")
    rt.join(timeout=10)
    assert lengths == [] and rt.state.phase == Phase.IDLE

    answers[0] = RangeAnswer(start="0:00:00", end="0:02:00", title="Разбор релиза", replace=True)
    rt.handle("process_folder")
    rt.join(timeout=10)
    assert lengths == [120.0, 120.0] and rt.state.error == ""
    assert not (tmp_path / "trash").exists()
    assert rt.state.last_result is not None and "Разбор релиза" in rt.state.last_result.parent.name


def test_wav_track_stops_before_header_limit(tmp_path, monkeypatch) -> None:
    """A forgotten recording: WAV has a 4 GB limit (~37 h); the track stops writing before
    the header breaks and the app stops the recording with a clear reason."""
    import mnema.live.record as record_mod

    monkeypatch.setattr(record_mod, "_WAV_LIMIT", 3200)
    w = record_mod.WavTrackWriter(tmp_path / "mic.wav")
    w.start()
    for _ in range(3):
        w.write(b"\x01\x00" * 800)  # 1600 bytes each
    w.stop()
    assert w.full and (tmp_path / "mic.wav").stat().st_size == 44 + 3200


def test_transcript_opens_only_after_node_result_and_follows_renamed_folder(
    monkeypatch, tmp_path
) -> None:
    """The recording API returns files in the terminal result; until then the UI shows no
    new text."""
    import mnema.live.setup as setup_mod
    from mnema.llm import claude

    notes = tmp_path / "notes"
    folder = notes / "2026-10-01_10-05"
    _wav(folder / "mic.wav")
    monkeypatch.setenv("MNEMA_LIVE_DIR", str(notes))
    monkeypatch.delenv("MNEMA_RUNNER", raising=False)
    monkeypatch.setattr("mnema.bar.runtime._system_available", lambda: False)
    monkeypatch.setattr(
        setup_mod,
        "dispatch",
        lambda *a, **k: {"text": "ну, итоги.", "segments": [
            {"start": 0.0, "end": 1.0, "text": "ну, итоги."}]},
    )  # fmt: skip
    naming: list[list[tuple]] = []

    def model(system, content, **k):
        naming.append([(i.key, i.title) for i in menu(rt.state, rt.prefs, MenuEnv())])
        return "Итоги недели"

    monkeypatch.setattr(claude, "_stream_text", model)
    shown: list[list[tuple]] = []

    class Opening(_Notes):
        def __init__(self) -> None:
            super().__init__()
            self.opened: list[Path] = []

        def notify(self, title: str, message: str, path: Path | None) -> None:  # noqa: A002
            super().notify(title, message, path)
            shown.append([(i.key, i.title) for i in menu(rt.state, rt.prefs, MenuEnv())])

        def open(self, path: Path) -> None:
            self.opened.append(path)

    platform = Opening()
    rt = BarRuntime(platform=platform, prefs_file=tmp_path / "bar.json")
    rt.recover(folder)
    rt.join(timeout=10)

    txt, renamed = folder / "transcript.txt", notes / "2026-10-01_Итоги недели"
    assert [c[:3] for c in platform.calls] == [
        ("Расшифровка готова", "Итоги недели · 01.10", renamed / "transcript.txt"),
        ("Запись обработана", "Итоги недели · 01.10", renamed / "transcript.txt"),
    ]  # fmt: skip
    id = json.loads((renamed / ".mnema-meta.json").read_text(encoding="utf-8"))["id"]
    assert (f"record:{id}:transcript", "Открыть расшифровку") in shown[0]
    assert not any(key and key.endswith(":transcript") for key, title in naming[0])
    assert not folder.exists()
    assert (renamed / "transcript.txt").read_text(encoding="utf-8") == "[0:00] mic: ну, итоги.\n"
    rt.handle(f"open:{txt}")  # a click on an early notification
    rt.handle(f"open:{folder}")  # a notification with the folder ("Processing stopped")
    assert platform.opened == [renamed / "transcript.txt", renamed]


def test_range_pick_sliders_and_typed_time() -> None:
    """The part window: the whole recording first; dragging the start past the end pulls the
    end along; a time still being typed ("0:5" on the way to "0:52") does not move the other
    edge; the caption says how much is selected."""
    from mnema.bar.range_pick import RangePick, typed

    pick = RangePick(length=5700.8)
    assert (pick.start, pick.end) == (0.0, 5700.8)
    assert pick.whole() and pick.caption() == "Выбрана вся запись"

    pick.set_start(600.0)
    pick.set_end(3130.0)
    assert not pick.whole() and pick.caption() == "Выбрано 0:42:10"
    pick.set_start(4000.0)  # the slider past the end pulls the end along
    assert (pick.start, pick.end) == (4000.0, 4000.0)
    pick.set_start(9999.0)
    assert pick.start == 5700.8  # cannot go past the end of the recording

    pick = RangePick(length=5700.8, start=600.0)
    pick.set_end(typed("0:5") or 0.0, push=False)  # typing "0:52:10", so far "0:5"
    assert (pick.start, pick.end) == (600.0, 5700.8)
    pick.set_end(typed("0:52:10") or 0.0, push=False)
    assert (pick.start, pick.end) == (600.0, 3130.0)
    assert typed("0:") is None and typed("") is None


def test_range_whole_recording_despite_fraction_of_second(monkeypatch, tmp_path) -> None:
    """A 2:00.8 recording: the window shows whole seconds ("0:02:00"); that is the whole
    recording, not a part in its own folder."""
    import mnema.live.setup as setup_mod
    from mnema.bar.range_pick import RangeAnswer

    folder = tmp_path / "Созвон"
    _wav(folder / "mic.wav", frames=16000 * 120 + 12800)
    monkeypatch.delenv("MNEMA_RUNNER", raising=False)
    monkeypatch.setattr("mnema.bar.runtime._system_available", lambda: False)
    monkeypatch.setattr(
        setup_mod,
        "dispatch",
        lambda *a, **k: {"text": "Итоги.", "segments": [{"start": 0, "end": 1, "text": "Итоги."}]},
    )

    class Picking(_Notes):
        def choose_folder(self, start, on_done) -> None:
            on_done(folder)

        def ask_range(self, req, on_done) -> None:
            on_done(RangeAnswer(start="0:00:00", end="0:02:00", title=req.name))

    rt = BarRuntime(platform=Picking(), prefs_file=tmp_path / "bar.json")
    rt.handle("process_folder")
    rt.join(timeout=10)
    assert rt.state.last_result is not None and rt.state.last_result.parent == folder


def test_linux_range_window_gtk_then_zenity(monkeypatch) -> None:
    """Linux: the window is a separate GTK process (request and answer are JSON, with the
    language); its Cancel does nothing; without GTK the zenity fields come one by one,
    filled with the start, the end and the title, the "to the Trash" checkbox as last time,
    the language Automatic."""
    import subprocess
    import threading

    from mnema.bar import app_linux
    from mnema.bar.range_pick import RangeAnswer, RangeRequest

    monkeypatch.setattr(threading.Thread, "start", lambda self: self.run())  # no thread
    replies: list = []
    seen: list[list[str]] = []

    def fake_run(cmd, **k):
        seen.append(cmd)
        code, out = replies.pop(0)
        return types.SimpleNamespace(returncode=code, stdout=out, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    got: list = []
    platform = app_linux.LinuxPlatform()
    req = RangeRequest(
        length=5700.0, subtitle="Дейли · 27.09 · 1:35:00", name="Дейли", replace=True
    )

    picked = RangeAnswer(start="0:10:00", end="0:52:10", title="Дейли", replace=False, lang="ru")
    replies[:] = [(0, picked.model_dump_json() + "\n")]
    platform.ask_range(req, got.append)
    assert got == [picked] and seen[0][1:3] == ["-m", "mnema.bar.range_pick"]
    assert RangeRequest.model_validate_json(seen[0][3]) == req

    replies[:] = [(3, "")]  # Cancel in the GTK window
    platform.ask_range(req, got.append)
    assert got[-1] is None and len(seen) == 2

    replies[:] = [(1, ""), (0, "0:10:00\n"), (0, "1:35:00\n"), (0, "Дейли\n")]  # GTK did not start
    platform.ask_range(req, got.append)
    assert got[-1] == RangeAnswer(start="0:10:00", end="1:35:00", title="Дейли", replace=True)
    assert got[-1].lang == "auto" and "--entry-text=1:35:00" in seen[-2]
    assert seen[-1][0] == "zenity"


def test_full_recording_stays_when_part_not_transcribed(monkeypatch, tmp_path) -> None:
    """The checkbox is on but the part was not transcribed (recognition failed): the full
    recording stays, the part sits next to it with its mark, the notes say why."""
    import mnema.live.setup as setup_mod
    from mnema.bar.range_pick import RangeAnswer

    def answer(req):
        return RangeAnswer(start="0:00:30", end="0:01:30", title=req.name, replace=True)

    rt, _platform, folder, _lengths = _range_setup(monkeypatch, tmp_path, answer)

    def broken(*a, **k):
        raise RuntimeError("движок упал")

    monkeypatch.setattr(setup_mod, "dispatch", broken)
    rt.handle("process_folder")
    rt.join(timeout=10)

    assert not (tmp_path / "trash").exists() and (folder / "mic.wav").exists()
    part = tmp_path / "Созвон из архива (0.00.30–0.01.30)"
    assert "Полная запись оставлена: часть записи не расшифрована" in _details(part)


def test_foreign_folder_is_never_trashed(monkeypatch, tmp_path) -> None:
    """The folder holds files besides the recording ("Downloads" with one .wav): the window
    has no checkbox, and even if the answer carries it the full folder stays."""
    from mnema.bar.range_pick import RangeAnswer

    def answer(req):
        return RangeAnswer(start="0:00:30", end="0:01:30", title=req.name, replace=True)

    rt, platform, folder, lengths = _range_setup(monkeypatch, tmp_path, answer)
    (folder / "договор.pdf").write_bytes(b"%PDF")
    rt.handle("process_folder")
    rt.join(timeout=10)

    assert platform.requests[0].can_replace is False
    assert lengths == [60.0, 60.0] and not (tmp_path / "trash").exists()
    assert (folder / "договор.pdf").exists()
    # the choice is not overwritten
    assert load_prefs(tmp_path / "bar.json").replace_original is True


def test_same_part_again_takes_full_recording_name(monkeypatch, tmp_path) -> None:
    """The same part processed a second time, now with the checkbox: the first copy of the
    part already took the name "… (0.00–0.01)", but the result still takes the full
    recording's place."""
    from mnema.bar.range_pick import RangeAnswer

    answers = [RangeAnswer(start="0:00:00", end="0:01:00", title="Созвон из архива")]
    rt, _platform, folder, _lengths = _range_setup(monkeypatch, tmp_path, lambda r: answers[0])
    rt.handle("process_folder")
    rt.join(timeout=10)
    first = tmp_path / "Созвон из архива (0.00–0.01)"
    assert first.is_dir()

    answers[0] = answers[0].model_copy(update={"replace": True})
    rt.handle("process_folder")
    rt.join(timeout=10)
    assert rt.state.last_result == folder / "transcript.txt"
    assert (tmp_path / "trash" / "Созвон из архива").is_dir() and first.is_dir()


def test_named_folder_without_date_keeps_recording_date(monkeypatch, tmp_path) -> None:
    """A folder without a date in its name (an archived call) and a new title: the date in
    the name is the recording's date (from its tracks), not today."""
    import os
    from datetime import datetime

    from mnema.bar.range_pick import RangeAnswer

    rt, _platform, folder, _lengths = _range_setup(
        monkeypatch, tmp_path, lambda r: RangeAnswer(start="", end="", title="Разбор релиза")
    )
    stamp = datetime(2026, 8, 19, 15, 0).timestamp()
    for wav in folder.glob("*.wav"):
        os.utime(wav, (stamp, stamp))
    rt.handle("process_folder")
    rt.join(timeout=10)
    assert rt.state.last_result == tmp_path / "2026-08-19_Разбор релиза" / "transcript.txt"


def test_recording_folder_detection(tmp_path) -> None:
    """A folder can be replaced by a part only if it is a Mnema recording: its tracks and
    results."""
    from mnema.live.setup import is_recording_folder

    rec = tmp_path / "2026-09-27_Дейли"
    _wav(rec / "mic.wav")
    for name in ("transcript.txt", "summary.md", "recording.mp3", ".mnema-meta.json"):
        (rec / name).write_text("x", encoding="utf-8")
    assert is_recording_folder(rec)
    (rec / "sub").mkdir()
    assert not is_recording_folder(rec)  # a subfolder is not a recording
    other = tmp_path / "Загрузки"
    _wav(other / "voice.wav")
    assert not is_recording_folder(other)
    assert not is_recording_folder(tmp_path / "нет такой")


def _stream(text: str) -> object:
    delta = types.SimpleNamespace(content=text)
    choice = types.SimpleNamespace(delta=delta, finish_reason="stop")
    return iter([types.SimpleNamespace(choices=[choice])])


class _NoMoney(Exception):
    status_code = 402


def test_summarize_card_writes_summary_and_names_default_folder(monkeypatch, tmp_path) -> None:
    """Summarize: the summary by the model active now, a default-named folder gets a
    title, the notification opens summary.md; of the remarks of the previous processing
    (also after a stopped redo) only the one about the title and summary is removed, the
    others and "About the recording" stay; when the model refuses there is no summary, the
    name stays and the notification gives the reason; outside idle nothing happens."""
    import httpx
    import litellm

    from mnema.llm import providers

    monkeypatch.setattr(providers, "TRANSPORT", httpx.MockTransport(lambda r: httpx.Response(404)))
    root = tmp_path / "notes"
    monkeypatch.setenv("MNEMA_LIVE_DIR", str(root))
    folder = root / "2026-10-02_13-05"
    folder.mkdir(parents=True)
    (folder / "transcript.txt").write_text("mic: релиз в пятницу\nsystem: да\n", encoding="utf-8")
    failed = remarks.asr_failed("system", "нет памяти", live_kept=False)
    asr = failed.text
    from mnema.live import meta

    meta.append(
        folder,
        meta.Run(
            status="ok",
            notes=[failed],
            info=["Часть записи 0:10–1:35 из 3:00", "Полная запись перемещена в Корзину"],
        ),
    )
    meta.append(folder, meta.Run(status="stopped"))  # the redo was stopped
    answer = {"title": "Релиз", "about": "Договорились о релизе.", "summary": ["релиз в пятницу"]}
    reply = json.dumps(answer, ensure_ascii=False)
    monkeypatch.setattr(litellm, "completion", lambda **kw: _stream(reply))
    platform = _Notes()
    rt = BarRuntime(platform=platform, prefs_file=tmp_path / "bar.json")
    rt.state.phase = Phase.RECORDING
    rt.handle(f"summarize:{folder}")
    assert rt.state.phase == Phase.RECORDING and not platform.calls
    rt.state.phase = Phase.IDLE

    rt.handle(f"summarize:{folder}")
    rt.join(timeout=10)
    named = root / "2026-10-02_Релиз"
    assert (named / "summary.md").exists() and not folder.exists()
    notes = _details(named)
    assert "Модель deepseek-chat не ответила" not in notes and asr in notes
    assert "Часть записи 0:10–1:35 из 3:00" in notes and "Полная запись перемещена" in notes
    rt.handle(f"summarize:{named}")
    rt.join(timeout=10)
    assert asr in _details(named)
    assert platform.calls[-1] == (
        "Пересказ готов",
        "Релиз · 02.10",
        named / "summary.md",
    )
    assert rt.state.phase == Phase.IDLE and rt.state.last_summary == named / "summary.md"

    def refused(**kw):
        raise _NoMoney("Insufficient Balance")

    monkeypatch.setattr(litellm, "completion", refused)
    other = root / "2026-10-02_14-05"
    other.mkdir()
    (other / "transcript.txt").write_text("mic: созвон\n", encoding="utf-8")
    rt.handle(f"summarize:{other}")
    rt.join(timeout=10)
    assert other.exists() and not (other / "summary.md").exists()
    title, reason, _ = platform.calls[-1]
    assert title == "Пересказ не составлен" and reason
    assert rt.state.phase == Phase.IDLE


@pytest.mark.parametrize("answer", [None, "", "Обсуждение проекта"])
def test_record_without_cloud_prompts_for_title_or_keeps_datetime(monkeypatch, tmp_path, answer):
    from mnema.bar.state import result_name

    monkeypatch.setenv("MNEMA_MODEL", "")
    monkeypatch.setenv("MNEMA_API_KEY", "")
    captured: dict = {}
    _patch_record(monkeypatch, tmp_path, captured)
    platform = _AskPlatform(answer)
    rt = BarRuntime(platform=platform, prefs_file=tmp_path / "bar.json")
    rt.handle("start")
    assert captured["started"].wait(10)
    rt.handle("stop")
    rt.join(20)
    assert rt.state.phase == Phase.IDLE and not rt.state.error
    assert platform.asked == [None]
    assert len(rt.state.last_warnings) == 1
    assert rt.state.last_warnings[0].cause == "Не настроена модель"
    assert rt.state.last_result is not None and rt.state.last_result.exists()
    assert "Без названия" not in result_name(rt.state.last_result)
    if answer:
        assert rt.state.last_result.parent.name.endswith("_" + answer)
    else:
        from mnema.live.setup import is_default_name

        assert is_default_name(rt.state.last_result.parent.name)
