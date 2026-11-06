"""Recordings processed side by side: at a connected server up to three at once, on this Mac one
at a time. A real BarRuntime and recording folders; the server is httpx.MockTransport (a job runs
until the test ends it), recognition on this Mac the gate of test_bar_queue."""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_bar import NODE, _patch_record
from test_bar_queue import _App, _folder, _Gate, _id, _ids, _record, _runs, _state

import mnema.client as client_mod
import mnema.live.setup as setup_mod
from mnema.bar.panel import panel_model
from mnema.bar.recordings import library
from mnema.bar.runtime import BarRuntime
from mnema.bar.settings_model import settings_model
from mnema.bar.state import MenuEnv, menu, step_line
from mnema.live import meta
from mnema.live.meta import RecordState
from mnema.live.models import FINALIZE_STEPS, STEP_MESSAGES
from mnema.live.setup import PROCESSING_MARK

QUEUE_FULL = "Очередь сервера занята"
ASR_STEP = STEP_MESSAGES[FINALIZE_STEPS[0]]


class _Changes:
    """Waiting for the runtime and the fake server: every change wakes the waiter."""

    def __init__(self) -> None:
        self._changed = threading.Condition()

    def __call__(self) -> None:
        with self._changed:
            self._changed.notify_all()

    def until(self, check: Callable[[], bool]) -> None:
        with self._changed:
            assert self._changed.wait_for(check, timeout=10), "timed out"


class _Server:
    """The connected server: jobs by recording folder name, in the order they were submitted."""

    def __init__(self, changed: Callable[[], None]) -> None:
        self._changed = changed
        self._lock = threading.Lock()
        self.jobs: dict[str, str] = {}  # folder name → job id
        self.ops: dict[str, str] = {}  # job id → its operation
        self.answers: dict[str, dict] = {}  # job id → the answer to polling
        self.cancelled: list[str] = []
        self.uploads = 0
        self.full: set[str] = set()  # folders the server's queue refuses
        self.ending = False  # finish_all was called: new jobs end at once

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if path == "/api/uploads" and method == "POST":
            with self._lock:
                self.uploads += 1
            return httpx.Response(200, json={"id": uuid.uuid4().hex})
        if path.startswith("/api/uploads/"):
            return httpx.Response(200, json={"path": f"uploads/{path.rsplit('/', 1)[1]}"})
        if path == "/api/jobs":
            return self._submit(json.loads(request.content))
        if path.endswith("/cancel"):  # one that polling did not end stays cancelled
            job = path.split("/")[3]
            with self._lock:
                self.cancelled.append(job)
                self.answers[job] = {"status": "error", "error": "cancelled"}
            return httpx.Response(200, json={})
        if path.startswith("/api/jobs/") and method == "GET":
            with self._lock:
                return httpx.Response(200, json=self.answers[path.split("/")[3]])
        return httpx.Response(200, json={"choices": []})

    def _submit(self, body: dict) -> httpx.Response:
        name = body["params"]["name"]
        if name in self.full:
            return httpx.Response(429, json={"detail": QUEUE_FULL})
        with self._lock:
            job = self.jobs[name] = f"j{len(self.jobs)}"
            self.ops[job] = body["op"]
            self.answers[job] = {"status": "running", "progress": 0.0, "message": ""}
        if self.ending:
            self.finish(Path(name))
        self._changed()
        return httpx.Response(200, json={"id": job})

    def step(self, folder: Path, progress: float, message: str = ASR_STEP) -> None:
        with self._lock:
            self.answers[self.jobs[folder.name]] = {
                "status": "running",
                "progress": progress,
                "message": message,
            }

    def finish(self, folder: Path, *, title: str | None = None) -> None:
        job = self.jobs[folder.name]
        summary = self.ops[job] == "recording_summary"
        files = {"summary.txt": "Итог\n"} if summary else {"transcript.txt": "mic: привет\n"}
        run = meta.Run(kind="summary" if summary else "process", status="ok")
        result = {"files": files, "folder": title, "run": run.model_dump(mode="json")}
        with self._lock:
            self.answers[job] = {"status": "done", "result": result}

    def finish_all(self) -> None:
        self.ending = True
        with self._lock:
            names = list(self.jobs)
        for name in names:
            with self._lock:
                running = self.answers[self.jobs[name]]["status"] == "running"
            if running:
                self.finish(Path(name))


class _Bench:
    """A Mac with a connected server; it recognizes itself until "On the server" is chosen."""

    def __init__(self, monkeypatch, tmp_path: Path, app: _App) -> None:
        self.captured: dict = {}
        self.changes = _Changes()
        self.server = _Server(self.changes)
        self.gate = _Gate()
        self.app = app
        self.notes = tmp_path / "notes"
        _patch_record(monkeypatch, tmp_path, self.captured)
        monkeypatch.setattr("mnema.bar.runtime._system_available", lambda: False)
        monkeypatch.delenv("MNEMA_MODEL")
        monkeypatch.setattr(setup_mod, "dispatch", self.gate)
        monkeypatch.setenv("MNEMA_RUNNER", NODE)
        transport = httpx.MockTransport(self.server)
        monkeypatch.setattr(
            client_mod.MnemaClient,
            "_make_async_client",
            lambda client: httpx.AsyncClient(base_url=client._base_url, transport=transport),
        )
        monkeypatch.setattr(
            httpx, "post", lambda url, **kw: self.server(httpx.Request("POST", url))
        )
        self.rt = BarRuntime(on_change=self.changes, platform=app, prefs_file=tmp_path / "bar.json")
        self.rt.prefs.system = False  # one track per recording

    def on_the_server(self) -> None:
        self.rt.handle("recognition:server")

    def folders(self, n: int) -> list[Path]:
        return [_folder(self.notes, f"2026-10-{i + 10}_10-00") for i in range(n)]

    def texts(self, n: int) -> list[Path]:
        """Transcribed recordings for "Summarize"."""
        made = [_folder(self.notes, f"2026-09-{i + 10}_10-00") for i in range(n)]
        for folder in made:
            (folder / "transcript.txt").write_text("mic: привет\n", encoding="utf-8")
            meta.append(folder, meta.Run(status="ok"))
        return made

    def running(self) -> set[Path]:
        return {job.folder for job in self.rt.state.running}

    def until_running(self, *folders: Path) -> None:
        self.changes.until(lambda: self.running() == set(folders))

    def until_submitted(self, *folders: Path) -> None:
        self.changes.until(lambda: set(self.server.jobs) == {f.name for f in folders})

    def cards(self) -> dict[str, Any]:
        env = MenuEnv(mac=True)
        cards = library(self.notes).cards
        model = panel_model(
            self.rt.state, self.rt.prefs, env, cards, settings_model(self.rt.prefs, env)
        )
        return {card.id: card for card in model.cards}


@pytest.fixture
def bench(monkeypatch, tmp_path):
    made = _Bench(monkeypatch, tmp_path, _App())
    yield made
    made.server.finish_all()
    made.gate.open_all()
    made.rt.handle("quit_now")
    made.rt.join(timeout=10)


def test_three_recordings_run_at_the_server_and_the_fourth_waits(bench) -> None:
    """Three recordings at the server run side by side; the fourth starts when one ends, in
    queue order; every card and the tray act on their own recording."""
    rt, server = bench.rt, bench.server
    bench.on_the_server()
    a, b, c, d = bench.folders(4)
    for folder in (a, b, c, d):
        rt.recover(folder)
    bench.until_submitted(a, b, c)
    bench.until_running(a, b, c)
    assert _ids(rt) == [(_id(f), f != d) for f in (a, b, c, d)]
    assert bench.cards()[_id(d)].detail == "В очереди"
    items = menu(rt.state, rt.prefs, MenuEnv())
    stops = [i.key for i in items if (i.key or "").endswith(":stop")]
    assert stops == [f"record:{_id(f)}:stop" for f in (a, b, c)]
    assert "В очереди: 1" in [i.title for i in items]
    rt._update_next = time.time() + 3600
    rt.tick()
    assert all(job.elapsed > 0 for job in rt.state.running)

    server.finish(b)
    bench.until_submitted(a, b, c, d)
    bench.until_running(a, c, d)
    server.finish_all()
    rt.join(timeout=10)
    assert rt.state.jobs == [] and rt.state.error == ""
    assert {_state(f) for f in (a, b, c, d)} == {RecordState.PROCESSED}


def test_recordings_on_this_mac_run_one_after_another(bench) -> None:
    """Recognized here, with the summary at the server: the recording holds this Mac's place
    until its summary is done."""
    rt, gate, server = bench.rt, bench.gate, bench.server
    a, b = bench.folders(2)
    rt.recover(a)
    rt.recover(b)
    gate.entered()
    bench.until_running(a)
    assert _ids(rt) == [(_id(a), True), (_id(b), False)]
    gate.release(0)
    bench.until_submitted(a)
    assert _ids(rt) == [(_id(a), True), (_id(b), False)]
    server.finish(a)
    gate.entered()
    bench.until_running(b)
    gate.release(1)
    bench.until_submitted(a, b)
    server.finish(b)
    rt.join(timeout=10)
    assert {_state(a), _state(b)} == {RecordState.PROCESSED}
    assert set(server.ops.values()) == {"recording_summary"}


def test_a_recording_waiting_for_this_mac_does_not_hold_back_the_server(bench) -> None:
    """This Mac recognizes one, a second waits for it; "Summarize" behind them is the server's
    work and starts at once."""
    rt, gate, server = bench.rt, bench.gate, bench.server
    a, b = bench.folders(2)
    [text] = bench.texts(1)
    rt.recover(a)
    rt.recover(b)
    rt.summarize(text)
    gate.entered()
    bench.until_submitted(text)
    bench.until_running(a, text)
    server.finish(text)
    bench.until_running(a)
    assert _ids(rt) == [(_id(a), True), (_id(b), False)]  # still waits: this Mac is taken
    assert (text / "summary.txt").is_file()


def test_a_recording_waiting_for_the_server_does_not_hold_back_this_mac(bench) -> None:
    """The server's three places are taken and a fourth waits; a recording for this Mac behind
    it starts as soon as this Mac is free."""
    rt, gate, server = bench.rt, bench.gate, bench.server
    a, b = bench.folders(2)
    first, second, third, fourth = bench.texts(4)
    rt.recover(a)
    for folder in (first, second, third, fourth):
        rt.summarize(folder)
    rt.recover(b)
    gate.entered()
    bench.until_submitted(first, second, third)
    bench.until_running(a, first, second, third)
    gate.release(0)
    bench.changes.until(lambda: a.name in server.jobs)
    server.finish(a)
    gate.entered()
    bench.until_running(b, first, second, third)
    assert fourth.name not in server.jobs
    assert len(rt.state.waiting) == 1 and rt.state.waiting[0].folder == fourth


def test_stopping_one_leaves_the_others(bench) -> None:
    """ "Stop processing" cancels that recording's job on the server, whichever was polled last;
    the others go on and its place goes to the next one."""
    rt, server = bench.rt, bench.server
    bench.on_the_server()
    a, b, c, d = bench.folders(4)
    for folder in (a, b, c, d):
        rt.recover(folder)
    bench.until_running(a, b, c)
    bench.until_submitted(a, b, c)
    rt.handle(f"record:{_id(b)}:stop")
    bench.until_submitted(a, b, c, d)
    bench.until_running(a, c, d)
    assert server.cancelled == [server.jobs[b.name]]
    server.finish_all()
    rt.join(timeout=10)
    assert _state(b) is RecordState.AUDIO_ONLY
    assert {_state(f) for f in (a, c, d)} == {RecordState.PROCESSED}
    assert ("Обработка остановлена", "Звук сохранён, обработать можно позже") in bench.app.notes


def test_another_language_restarts_only_that_recording(bench) -> None:
    rt, server = bench.rt, bench.server
    bench.on_the_server()
    a, b = bench.folders(2)
    rt.recover(a)
    rt.recover(b)
    bench.until_submitted(a, b)
    bench.until_running(a, b)
    first = server.jobs[a.name]
    rt.handle(f"record:{_id(a)}:lang:en")
    bench.changes.until(lambda: server.jobs[a.name] != first)
    assert set(server.cancelled) == {first}
    assert rt.state.jobs[1].lang is None
    server.finish_all()
    rt.join(timeout=10)
    assert {_state(a), _state(b)} == {RecordState.PROCESSED}


def test_each_card_shows_its_own_progress(bench) -> None:
    rt, server = bench.rt, bench.server
    bench.on_the_server()
    a, b, c = bench.folders(3)
    for folder in (a, b, c):
        rt.recover(folder)
    bench.until_submitted(a, b, c)
    server.step(a, 0.1)
    server.step(b, 0.2)
    server.step(c, 0.0, STEP_MESSAGES["summary"])
    jobs = {job.folder: job for job in rt.state.jobs}
    bench.changes.until(
        lambda: (jobs[a].progress, jobs[b].progress) == (0.1, 0.2) and jobs[c].stage != ""
    )
    cards = bench.cards()
    shown = {f: step_line(jobs[f]) for f in (a, b, c)}
    assert len(set(shown.values())) == 3
    assert all(cards[_id(f)].detail == shown[f] for f in (a, b, c))
    assert shown[c] == "Составляю пересказ…"
    assert all(cards[_id(f)].busy for f in (a, b, c))


def test_quit_drops_the_queue_and_starts_nothing_more(bench) -> None:
    """Quitting needs no wait for the running ones; the waiting one leaves the queue and never
    reaches the server, even when a place frees up."""
    rt, server = bench.rt, bench.server
    bench.on_the_server()
    a, b, c, d = bench.folders(4)
    for folder in (a, b, c, d):
        rt.recover(folder)
    bench.until_submitted(a, b, c)
    bench.until_running(a, b, c)
    bench.app.yes = True
    rt.handle("quit")
    assert rt.can_exit and {job.folder for job in rt.state.jobs} == {a, b, c}
    server.finish_all()
    rt.join(timeout=10)
    assert rt.state.jobs == [] and d.name not in server.jobs
    assert _runs(d) == [] and _state(d) is RecordState.UNPROCESSED


def test_update_waits_for_every_running_recording(bench, monkeypatch) -> None:
    """The update starts only when the last running recording has ended."""
    import mnema.update as update_mod

    applied: list[bool] = []
    monkeypatch.setattr(update_mod, "apply", lambda on: applied.append(True))
    rt, server = bench.rt, bench.server
    bench.on_the_server()
    a, b = bench.folders(2)
    rt.recover(a)
    rt.recover(b)
    bench.until_submitted(a, b)
    server.finish(a)
    bench.until_running(b)
    rt.handle("update")
    assert applied == []
    server.finish(b)
    rt.join(timeout=10)
    assert rt.state.jobs == []
    rt.handle("update")
    bench.changes.until(lambda: applied == [True])


def test_full_server_queue_fails_only_that_recording(bench) -> None:
    """The server refuses one with 429: that recording keeps its uploads for the next try, as
    with a single job; the others go on, and the next try continues without uploading again."""
    rt, server = bench.rt, bench.server
    bench.on_the_server()
    a, b, c = bench.folders(3)
    server.full.add(b.name)
    for folder in (a, b, c):
        rt.recover(folder)
    bench.until_submitted(a, c)
    bench.changes.until(lambda: rt.state.error == QUEUE_FULL)
    bench.until_running(a, c)
    work = json.loads((b / PROCESSING_MARK).read_text(encoding="utf-8"))
    assert work["job"] is None and set(work["uploaded"]) == {"mic"}
    uploads = server.uploads

    server.full.clear()
    rt.recover(b)
    bench.until_submitted(a, b, c)
    assert server.uploads == uploads
    server.finish_all()
    rt.join(timeout=10)
    assert {_state(f) for f in (a, b, c)} == {RecordState.PROCESSED}


def test_closing_waits_for_every_video_assembly(bench, monkeypatch) -> None:
    """Videos are assembled here wherever the text is made, side by side: the end of one does
    not tell the app it can close while another still runs."""
    rt, server = bench.rt, bench.server
    bench.on_the_server()
    first, second = bench.folders(2)
    for folder in (first, second):
        (folder / "screen.mov").write_bytes(b"frames")
    both = threading.Barrier(2, timeout=10)
    ending = threading.Event()

    def ffmpeg(cmd: list[str], **kw: Any) -> None:
        part = Path(cmd[-1])
        both.wait()
        if part.parent == second:
            assert ending.wait(timeout=10)
        part.write_bytes(b"video")

    monkeypatch.setattr("mnema.live.video.run_tool", ffmpeg)
    rt.recover(first)
    rt.recover(second)
    bench.until_submitted(first)
    assert not rt._no_assembly.is_set()
    ending.set()
    bench.until_submitted(first, second)
    assert rt._no_assembly.is_set()
    server.finish_all()
    rt.join(timeout=10)
    assert (first / "video.mp4").is_file() and (second / "video.mp4").is_file()


def test_title_window_is_about_the_recording_that_ended(monkeypatch, tmp_path) -> None:
    """The next recording ends while the first one's result is being announced: the last
    recording is the later one, and the title window of the first still names the first."""

    class Holding(_App):
        """The first "Recording processed" waits until the other recording has ended."""

        hold: Callable[[], None] = staticmethod(lambda: None)

        def notify(self, title: str, message: str, path: Path | None) -> None:
            super().notify(title, message, path)
            if title == "Запись обработана":
                hold, self.hold = self.hold, lambda: None
                hold()

    app = Holding()
    made = _Bench(monkeypatch, tmp_path, app)
    rt, server = made.rt, made.server
    made.on_the_server()
    first = _record(rt, made.captured)
    made.until_submitted(first)
    second = _record(rt, made.captured)
    made.until_submitted(first, second)

    def other_ends() -> None:
        server.finish(second, title="2026-10-22_Второй")
        made.changes.until(lambda: rt.state.jobs and len(rt.state.jobs) == 1)

    app.hold = other_ends
    server.finish(first, title="2026-10-21_Первый")
    rt.join(timeout=10)
    assert sorted(app.asked) == ["Второй", "Первый"]
