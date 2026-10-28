"""A recording on the node goes through the shared pipeline; models and audio are stubbed."""

import importlib.util
import json
import threading
import time
import types
import wave
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from mnema import config, core, node_settings, ops
from mnema.core import audio
from mnema.live import meta, remarks, setup
from mnema.llm import limits, providers
from mnema.server import create_app

STARTED = "2026-10-22T00:30:00+03:00"
NAME = "2026-10-22_00-30"
WORDS = "Обсудили подготовку выпуска и проверку ошибок перед демонстрацией. " * 12
ANSWER = json.dumps(
    {
        "title": "Подготовка выпуска",
        "about": "Готовим выпуск.",
        "summary": ["[0:00] Проверить ошибки"],
    },
    ensure_ascii=False,
)


def _model(monkeypatch, answer=lambda: ANSWER):
    import litellm

    calls = []

    def completion(**kw):
        calls.append(kw)
        text = answer()
        choice = types.SimpleNamespace(
            delta=types.SimpleNamespace(content=text), finish_reason="stop"
        )
        return iter([types.SimpleNamespace(choices=[choice])])

    monkeypatch.setattr(litellm, "completion", completion)
    return calls


QUIET = b"quiet"  # a silent upload: the extracted track is zeros


def _wav(path, sample=b"\x00\x10"):
    with wave.open(str(path), "wb") as out:
        out.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        out.writeframes(sample * 32000)


@pytest.fixture(autouse=True)
def boundaries(monkeypatch):
    find = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda n, *a, **kw: object() if n == "whisperx" else find(n, *a, **kw),
    )
    monkeypatch.setattr(providers, "TRANSPORT", httpx.MockTransport(lambda r: httpx.Response(404)))
    monkeypatch.setattr(limits, "_FETCHED", {})
    monkeypatch.setattr(core, "probe", lambda p: types.SimpleNamespace(duration=2))
    name = node_settings.node_file()
    name.parent.mkdir(parents=True, exist_ok=True)
    name.write_text(json.dumps({"name": "Мой узел"}, ensure_ascii=False), encoding="utf-8")

    def extract(src, dest, *, on, **kw):
        on(0.0, "Извлекаю звук…")
        _wav(dest, b"\x00\x00" if Path(src).read_bytes() == QUIET else b"\x00\x10")
        on(1.0, "")
        return dest

    def dispatch(op, path, params, **kw):
        assert op == "transcribe" and kw["runner"] is None and kw["local"] is True
        for p in (0.0, 0.5, 1.0):
            kw["on"](p, "Распознаю…")
        return {
            "text": WORDS,
            "segments": [{"start": 0, "end": 1, "text": WORDS}],
            "language": "ru",
            "engine": "whisperx",
        }

    monkeypatch.setattr(audio, "extract_audio", extract)
    monkeypatch.setattr(setup, "dispatch", dispatch)


def _upload(client, name="mic.ogg", body=b"audio"):
    return client.post("/api/upload", files={"file": (name, body)}).json()["path"]


def _wait(client, job_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "error"):
            return job
        time.sleep(0.005)
    pytest.fail("The job did not finish")


def _start(client, *, labels=("mic", "system"), quiet=(), **params):
    tracks = {
        label: _upload(client, f"{label}.ogg", QUIET if label in quiet else b"audio")
        for label in labels
    }
    response = client.post(
        "/api/jobs",
        json={
            "op": "recording",
            "tracks": tracks,
            "params": {"started": STARTED, "name": NAME, "lang": "ru", **params},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _no_model(monkeypatch):
    monkeypatch.delenv("MNEMA_MODEL", raising=False)
    monkeypatch.delenv("MNEMA_API_KEY", raising=False)


def _clean(root):
    assert not list((root / ".mnema/work").glob("*"))


@pytest.mark.parametrize("user_title", [None, "Мой выпуск"])
def test_recording_result_date_part_progress_and_cleanup(tmp_path, monkeypatch, user_title):
    monkeypatch.setenv("TZ", "UTC")
    calls = _model(monkeypatch)
    app = create_app(tmp_path)
    client = TestClient(app)
    suffix = " (0.10–1.35)"
    job_id = _start(
        client, name=NAME + suffix, user_title=user_title, part={"start": 10, "end": 95}
    )
    job = _wait(client, job_id)
    assert job["status"] == "done", job
    result = job["result"]
    folder_title = user_title or "Подготовка выпуска"
    assert result["folder"] == f"2026-10-22_{folder_title}{suffix}"
    assert "[0:00] mic: " in result["files"]["transcript.txt"]
    assert "[0:00] system: " in result["files"]["transcript.txt"]
    assert "- [" in result["files"]["summary.md"]
    run = result["run"]
    assert run["status"] == "ok" and run["settings"]["node"] == "Мой узел"
    assert run["part"] == {"start": 10, "end": 95}
    assert len(calls) == 1
    _clean(tmp_path)
    assert remarks.PLACE.get().node == ""


def test_recording_progress_is_monotonic_and_names_both_tracks(tmp_path, monkeypatch):
    _model(monkeypatch)
    tracks = {}
    for label in ("mic", "system"):
        (tmp_path / f"{label}.ogg").write_bytes(b"audio")
        tracks[label] = str(tmp_path / f"{label}.ogg")
    seen: list[tuple[float | None, str]] = []
    result = ops.get("recording").fn(
        tmp_path,
        {"started": STARTED, "name": NAME, "lang": "ru", "tracks": tracks},
        lambda fraction, message: seen.append((fraction, message)),
    )
    assert result["run"]["status"] == "ok"
    progress = [fraction for fraction, _ in seen if fraction is not None]
    assert progress and progress[-1] == 1.0
    assert progress == sorted(progress)
    messages = [message for _, message in seen]
    assert "Распознаю микрофон…" in messages and "Распознаю системный звук…" in messages
    assert not any(
        bad in m for m in messages for bad in ("LLM", "VAD", "whisperX", "GigaAM", "Parakeet", "(")
    )
    _clean(tmp_path)
    assert remarks.PLACE.get().node == ""


def test_recording_without_model_and_recording_notices(tmp_path, monkeypatch):
    _no_model(monkeypatch)
    client = TestClient(create_app(tmp_path))
    result = _wait(
        client,
        _start(client, quiet=("mic",), recording_error="Запись оборвалась"),
    )["result"]
    assert result["folder"] is None
    assert result["files"]["transcript.txt"] and result["files"].get("summary.md") is None
    run = result["run"]
    assert run["notes"][0]["step"] == "record"
    assert "Запись оборвалась" in run["remarks"][0]
    assert any(
        "микрофон" in text.lower() and "нет звука" in text.lower() for text in run["remarks"]
    )
    assert any(
        text.startswith("Не настроена модель") and "Задайте модель на сервере «Мой узел»" in text
        for text in run["remarks"]
    )
    _clean(tmp_path)


SILENT_REASONS = json.loads(
    (Path(__file__).parent / "fixtures/silent-reason-v1.json").read_text(encoding="utf-8")
)["cases"]


@pytest.mark.parametrize(
    "case", [c for c in SILENT_REASONS if c["reason"]], ids=lambda c: c["name"]
)
def test_silent_other_side_remark_says_why(tmp_path, monkeypatch, case):
    """The server decides silence from the uploaded track; the phone's reason refines the
    remark."""
    _no_model(monkeypatch)
    client = TestClient(create_app(tmp_path))
    job = _start(client, quiet=("system",), silent_reason={"system": case["reason"]})
    run = _wait(client, job)["result"]["run"]
    notes = [n for n in run["notes"] if n["step"] == "asr"]
    assert [(n["cause"], n["short"], n["code"]) for n in notes] == [
        (case["cause"], case["short"], case["code"])
    ]
    assert any(text.startswith(case["cause"]) for text in run["remarks"])
    _clean(tmp_path)


def test_reason_of_silence_does_not_silence_a_sounding_track(tmp_path, monkeypatch):
    """The phone's reason does not decide silence: the other side has sound, so no remark."""
    _no_model(monkeypatch)
    client = TestClient(create_app(tmp_path))
    run = _wait(client, _start(client, silent_reason={"system": "blocked"}))["result"]["run"]
    assert not [n for n in run["notes"] if n["step"] == "asr"]
    _clean(tmp_path)


def test_both_silent_tracks_are_a_recording_without_speech(tmp_path, monkeypatch):
    """The server decides silence: with both tracks silent, processing finishes without text,
    the history has a remark per track, and the recording is "no speech" and can be processed
    again."""
    _no_model(monkeypatch)
    client = TestClient(create_app(tmp_path))
    result = _wait(client, _start(client, quiet=("mic", "system")))["result"]
    run = result["run"]
    assert (result["files"], run["status"]) == ({}, "ok")
    assert [n["cause"] for n in run["notes"]] == [
        remarks.track_silent("mic").cause,
        remarks.track_silent("system").cause,
    ]
    sizes = {"mic.wav": 64044, "system.wav": 64044}
    assert meta.record_state(sizes, {"runs": [run]}) is meta.RecordState.NO_SPEECH
    _clean(tmp_path)


def test_default_server_name_is_not_repeated_in_remarks(tmp_path, monkeypatch):
    _no_model(monkeypatch)
    node_settings.node_file().unlink()
    monkeypatch.setattr("socket.gethostname", lambda: "studio.local")
    client = TestClient(create_app(tmp_path))
    result = _wait(client, _start(client, labels=("system",)))["result"]
    assert result["run"]["settings"]["node"] == "studio"
    assert any(
        "Задайте модель на сервере." in text and "«" not in text.split(".")[1]
        for text in result["run"]["remarks"]
        if text.startswith("Не настроена модель")
    )
    _clean(tmp_path)


def test_cancel_during_model_is_not_a_model_failure(tmp_path, monkeypatch):
    _no_model(monkeypatch)
    env = config.user_env_file()
    env.parent.mkdir(parents=True, exist_ok=True)
    env.write_text("MNEMA_MODEL=anthropic/claude-sonnet-5\nMNEMA_API_KEY=test-node-key\n")
    entered, resume = threading.Event(), threading.Event()

    def answer():
        entered.set()
        assert resume.wait(5)
        return ANSWER

    _model(monkeypatch, answer)
    client = TestClient(create_app(tmp_path))
    job_id = _start(client)
    try:
        assert entered.wait(5)
        assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 200
    finally:
        resume.set()
    job = _wait(client, job_id)
    assert (job["status"], job["error_kind"]) == ("error", "cancelled")
    _clean(tmp_path)


def test_asr_failure_does_not_expose_node_paths(tmp_path, monkeypatch):
    def fail(*a, **kw):
        raise OSError("broken /private/node/model.bin")

    monkeypatch.setattr(setup, "dispatch", fail)
    client = TestClient(create_app(tmp_path))
    result = _wait(client, _start(client))["result"]
    assert result["files"] == {}
    assert result["run"]["status"] == "failed"
    assert result["run"]["error"]
    assert all(track["error"] for track in result["run"]["tracks"].values())
    rendered = json.dumps(result, ensure_ascii=False)
    assert "/private" not in rendered and "mnema finish" not in rendered
    assert result["run"]["remarks"]
    _clean(tmp_path)


def test_auto_with_only_gigaam_validates_language_before_claim(tmp_path, monkeypatch):
    find = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda n, *a, **kw: (
            object()
            if n == "gigaam"
            else None
            if n in ("whisperx", "mlx_whisper", "silero_vad")
            else find(n, *a, **kw)
        ),
    )
    _model(monkeypatch)
    app = create_app(tmp_path)
    client = TestClient(app)
    path = _upload(client)
    request = {
        "op": "recording",
        "tracks": {"mic": path},
        "params": {"started": STARTED, "name": NAME, "asr": "auto"},
    }
    assert client.post("/api/jobs", json=request).status_code == 422
    app.state.uploads.claim(path)
    app.state.uploads.release(path, delete=False)
    assert Path(path).is_file()
    assert node_settings.asr_problem("gigaam") is None
    request["params"]["lang"] = "ru"
    response = client.post("/api/jobs", json=request)
    assert response.status_code == 200, response.text
    assert _wait(client, response.json()["id"])["status"] == "done"


def test_summary_without_model_refused_before_claim(tmp_path, monkeypatch):
    _no_model(monkeypatch)
    app = create_app(tmp_path)
    client = TestClient(app)
    path = _upload(client, "transcript.txt", WORDS.encode())
    response = client.post(
        "/api/jobs",
        json={
            "op": "recording_summary",
            "path": path,
            "params": {"started": STARTED, "name": NAME},
        },
    )
    assert response.status_code == 400
    assert "На сервере не настроена модель" in response.text
    app.state.uploads.claim(path)
    app.state.uploads.release(path, delete=False)


@pytest.mark.parametrize("long", [False, True])
def test_live_text_finalization_uses_normal_summary_threshold_without_asr(
    tmp_path, monkeypatch, long
):
    calls = _model(monkeypatch, lambda: ANSWER if long else "Приветствие")
    monkeypatch.setattr(setup, "dispatch", lambda *a, **k: pytest.fail("ASR already done"))
    client = TestClient(create_app(tmp_path))
    path = _upload(client, "transcript.txt", (WORDS if long else "Привет.").encode())
    response = client.post(
        "/api/jobs",
        json={
            "op": "recording_summary",
            "path": path,
            "params": {
                "started": STARTED,
                "name": NAME,
                "finalize": True,
            },
        },
    )
    assert response.status_code == 200, response.text
    result = _wait(client, response.json()["id"])["result"]
    assert result["run"]["status"] == "ok" and result["run"]["kind"] == "process"
    assert "transcript.txt" not in result["files"]
    assert bool(result["files"].get("summary.md")) is long
    assert len(calls) == 1
    assert not long or "summary" in calls[0]["messages"][0]["content"]


def test_live_text_without_model_still_finalizes_and_explains_missing_summary(
    tmp_path, monkeypatch
):
    _no_model(monkeypatch)
    monkeypatch.setattr(setup, "dispatch", lambda *a, **k: pytest.fail("ASR already done"))
    client = TestClient(create_app(tmp_path))
    path = _upload(client, "transcript.txt", WORDS.encode())
    response = client.post(
        "/api/jobs",
        json={
            "op": "recording_summary",
            "path": path,
            "params": {
                "started": STARTED,
                "name": NAME,
                "finalize": True,
            },
        },
    )
    assert response.status_code == 200, response.text
    result = _wait(client, response.json()["id"])["result"]
    assert result["run"]["status"] == "ok"
    assert "transcript.txt" not in result["files"] and result["run"]["notes"]
    assert result["folder"] is None


def test_live_text_translation_without_summary_uses_existing_finalization(tmp_path, monkeypatch):
    _model(monkeypatch)
    called = []

    def translate(text, *, to_lang, titled, on):
        called.append((text, to_lang, titled))
        return "Готовый перевод", "Переведённый звонок", False

    monkeypatch.setattr(setup, "_final_translate", translate)
    monkeypatch.setattr(setup, "dispatch", lambda *a, **k: pytest.fail("ASR already done"))
    client = TestClient(create_app(tmp_path))
    text = "We discussed the release schedule and agreed on the next steps."
    path = _upload(client, "transcript.txt", text.encode())
    response = client.post(
        "/api/jobs",
        json={
            "op": "recording_summary",
            "path": path,
            "params": {
                "started": STARTED,
                "name": NAME,
                "finalize": True,
                "translate": "ru",
                "summary": False,
            },
        },
    )
    assert response.status_code == 200, response.text
    result = _wait(client, response.json()["id"])["result"]
    assert called == [(text, "ru", True)]
    assert result["files"]["translation.md"].strip() == "Готовый перевод"
    assert result["files"]["summary.md"] is None and result["run"]["status"] == "ok"
    assert result["folder"] == "2026-10-22_Переведённый звонок"


def test_summary_write_failure_keeps_past_asr_notice_after_rename(tmp_path, monkeypatch):
    _model(monkeypatch)
    client = TestClient(create_app(tmp_path))
    note = remarks.track_silent("system")
    path = _upload(client, "transcript.txt", WORDS.encode())
    write = Path.write_text

    def fail_summary(file, *a, **kw):
        if file.name == "summary.md":
            raise OSError("Disk full at /private/node/summary.md")
        return write(file, *a, **kw)

    monkeypatch.setattr(Path, "write_text", fail_summary)
    response = client.post(
        "/api/jobs",
        json={
            "op": "recording_summary",
            "path": path,
            "params": {
                "started": STARTED,
                "name": NAME,
                "past": {"kind": "process", "status": "failed", "notes": [note.model_dump()]},
            },
        },
    )
    assert response.status_code == 200
    job = _wait(client, response.json()["id"])
    assert job["status"] == "done"
    result = job["result"]
    assert (result["run"]["kind"], result["run"]["status"]) == ("summary", "failed")
    assert result["files"] == {} and result["folder"] is None
    assert note.text in result["run"]["remarks"]
    assert "/private" not in json.dumps(result)
    assert not list((tmp_path / "uploads").glob("**/transcript.txt"))


@pytest.mark.parametrize(
    "bad",
    [
        {"started": "2026-10-22T00:30:00"},
        {"name": "../x"},
        {"name": ".."},
        {"name": "a\nb"},
        {"name": "a/b"},
        {"name": "a\\b"},
        {"user_title": "a\nb"},
        {"user_title": "x" * 501},
        {"lang": "unknown"},
        {"part": {"start": 2, "end": 1}},
        {"part": {"start": 0, "end": float("inf")}},
        {"recording_error": "x" * 501},
        {"threads": 3},
    ],
)
def test_recording_parameters_reject_invalid_input(bad):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ops.RecordingParams.model_validate({"started": STARTED, "name": NAME, **bad})


def test_startup_removes_abandoned_work(tmp_path):
    old = tmp_path / ".mnema/work/abandoned/recording"
    old.mkdir(parents=True)
    (old / "mic.wav").write_bytes(b"audio")
    create_app(tmp_path)
    _clean(tmp_path)


def test_preparation_failure_returns_failed_run_and_cleans_work(tmp_path, monkeypatch):
    def broken(*a, **kw):
        raise OSError("/private/node/input.ogg: broken")

    monkeypatch.setattr(audio, "extract_audio", broken)
    client = TestClient(create_app(tmp_path))
    job = _wait(client, _start(client))
    assert job["status"] == "done"
    result = job["result"]
    assert result["run"]["status"] == "failed" and result["run"]["remarks"]
    assert result["files"] == {} and result["folder"] is None
    assert "/private" not in json.dumps(result)
    _clean(tmp_path)


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({}, None),
        ({"retitle": True}, "2026-10-22_Подготовка выпуска"),
        ({"user_title": "Новое название"}, "2026-10-22_Новое название"),
    ],
)
def test_explicit_retitle_and_user_title_for_previously_named_folder(
    tmp_path, monkeypatch, params, expected
):
    _model(monkeypatch)
    client = TestClient(create_app(tmp_path))
    result = _wait(client, _start(client, name="2026-10-22_Своё название", **params))["result"]
    assert result["folder"] == expected
    _clean(tmp_path)


def test_timestamps_do_not_inflate_summary_threshold(tmp_path, monkeypatch):
    short = "Проверим ошибки завтра. " * 20

    def transcribe(op, path, params, **kw):
        return {
            "text": short,
            "segments": [
                {"start": i * 2, "end": i * 2 + 1, "text": "Проверим ошибки завтра."}
                for i in range(20)
            ],
            "language": "ru",
        }

    monkeypatch.setattr(setup, "dispatch", transcribe)
    calls = _model(monkeypatch, lambda: "Проверка ошибок")
    client = TestClient(create_app(tmp_path))
    result = _wait(client, _start(client, labels=("mic",)))["result"]
    from mnema.core.base import unstamped

    transcript = result["files"]["transcript.txt"]
    assert len(unstamped(transcript)) < 600 <= len(transcript)
    assert result["files"]["summary.md"] is None
    assert result["folder"] == "2026-10-22_Проверка ошибок"
    assert len(calls) == 1 and "summary" not in calls[0]["messages"][0]["content"]


@pytest.mark.parametrize("failure", ["balance", "garbled"])
@pytest.mark.parametrize("manual", [False, True])
def test_summary_failure_keeps_files_and_name_then_retries_without_asr(
    tmp_path, monkeypatch, failure, manual
):
    class NoMoney(Exception):
        status_code = 402

    recovered = False

    def answer():
        if recovered:
            return ANSWER
        if failure == "balance":
            raise NoMoney("Insufficient balance")
        return '{"title": "Unusable title", "about": "", "summary": []}'

    _model(monkeypatch, answer)
    asr_calls = []

    def unexpected_asr(*args, **kwargs):
        asr_calls.append(args)
        raise AssertionError("Redoing the summary must not transcribe audio")

    monkeypatch.setattr(setup, "dispatch", unexpected_asr)
    client = TestClient(create_app(tmp_path))
    prior = remarks.track_silent("system")
    params = {
        "started": STARTED,
        "name": "2026-10-22_Мой выпуск" if manual else NAME,
        "past": {"notes": [prior.model_dump()]},
    }

    def summarize():
        response = client.post(
            "/api/jobs",
            json={
                "op": "recording_summary",
                "path": _upload(client, "transcript.txt", WORDS.encode()),
                "params": params,
            },
        )
        assert response.status_code == 200, response.text
        job = _wait(client, response.json()["id"])
        assert job["status"] == "done"
        return job["result"]

    failed = summarize()
    assert failed["files"] == {}  # a missing key keeps the client's files
    assert failed["folder"] is None
    run = failed["run"]
    assert (run["kind"], run["status"]) == ("summary", "failed")
    cause = "закончились деньги" if failure == "balance" else "не по формату"
    assert cause in run["error"]
    assert any(n["step"] == "summary" and cause in n["cause"] for n in run["notes"])
    assert prior.model_dump(mode="json") in run["notes"]

    recovered = True
    params["past"] = run
    retry = summarize()
    assert retry["run"]["status"] == "ok" and retry["run"]["error"] is None
    assert set(retry["files"]) == {"summary.md"}
    assert retry["files"]["summary.md"].startswith("Готовим выпуск.\n")
    assert retry["folder"] == (None if manual else "2026-10-22_Подготовка выпуска")
    assert prior.model_dump(mode="json") in retry["run"]["notes"]
    assert not any(n["step"] in ("summary", "title") for n in retry["run"]["notes"])
    assert asr_calls == []


def test_full_recording_model_failure_keeps_successful_asr_with_retry_notice(tmp_path, monkeypatch):
    class NoMoney(Exception):
        status_code = 402

    def answer():
        raise NoMoney("Insufficient balance")

    _model(monkeypatch, answer)
    client = TestClient(create_app(tmp_path))
    result = _wait(client, _start(client))["result"]
    assert result["run"]["status"] == "ok"
    assert result["files"]["transcript.txt"]
    assert result["files"]["summary.md"] is None
    assert result["run"]["summary"]["engine"] == "none"
    assert any(
        n["step"] == "summary" and "закончились деньги" in n["cause"]
        for n in result["run"]["notes"]
    )


def _system_track_fails(monkeypatch):
    recognize = setup.dispatch

    def partial(op, path, params, **kwargs):
        if Path(path).stem == "system":
            raise OSError("failed /private/node/model.bin")
        return recognize(op, path, params, **kwargs)

    monkeypatch.setattr(setup, "dispatch", partial)


def test_failed_track_without_partial_keeps_every_client_file(tmp_path, monkeypatch):
    class NoMoney(Exception):
        status_code = 402

    def answer():
        raise NoMoney("Insufficient balance")

    _model(monkeypatch, answer)
    _system_track_fails(monkeypatch)
    client = TestClient(create_app(tmp_path))
    # Params without partial, as the phone sends them: half a conversation replaces nothing
    result = _wait(client, _start(client))["result"]
    assert result["files"] == {} and result["folder"] is None
    assert result["run"]["status"] == "failed" and result["run"]["error"]
    assert result["run"]["tracks"]["mic"]["error"] is None
    assert result["run"]["tracks"]["system"]["error"]
    failure = next(n for n in result["run"]["notes"] if "«Системный звук»" in n["cause"])
    assert failure["step"] == "asr" and failure["effect"] == "Новая расшифровка не сохранена"
    assert not any(n["step"] in ("summary", "title") for n in result["run"]["notes"])
    assert result["run"]["remarks"] == [
        r.text for r in remarks.merged(meta.Run.model_validate(result["run"]).notes)
    ]
    assert "/private" not in json.dumps(result)
    _clean(tmp_path)


def test_titles_with_slashes_are_accepted_by_both_operations(tmp_path, monkeypatch):
    _model(monkeypatch)
    for model in (ops.RecordingParams, ops.RecordingSummaryParams):
        model.model_validate(
            {
                "started": STARTED,
                "name": "2026-10-22_Обсуждение-CI-CD",
                "user_title": "Созвон 1/2 \\ итог",
            }
        )
    client = TestClient(create_app(tmp_path))
    result = _wait(client, _start(client, user_title="Созвон 1/2"))["result"]
    assert result["run"]["status"] == "ok"
    assert result["folder"] == "2026-10-22_Созвон 1 2"


def test_successful_asr_without_speech_is_not_a_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(
        setup,
        "dispatch",
        lambda *a, **kw: {
            "text": "",
            "segments": [],
            "language": "ru",
            "engine": "whisperx",
        },
    )
    client = TestClient(create_app(tmp_path))
    result = _wait(client, _start(client))["result"]
    assert result["run"]["status"] == "ok"
    assert result["run"]["error"] is None
    assert all(not track["error"] for track in result["run"]["tracks"].values())
    assert not result["files"].get("transcript.txt")


def test_failed_final_translation_remark_has_no_command_or_node_path(tmp_path, monkeypatch):
    """The translation failure remark goes into the history and to the phone, without commands
    or node paths."""
    _model(monkeypatch)
    monkeypatch.setattr(setup, "_final_translate", lambda text, **kw: (None, None, True))
    client = TestClient(create_app(tmp_path))
    run = _wait(client, _start(client, translate="en"))["result"]["run"]
    rendered = json.dumps(run, ensure_ascii=False)
    assert "Перевод не получился" in rendered
    assert "mnema " not in rendered and str(tmp_path) not in rendered
    _clean(tmp_path)


def test_recording_final_translation_uses_server_pipeline(tmp_path, monkeypatch):
    calls = _model(monkeypatch)
    seen = []

    def translate(text, **kwargs):
        seen.append((text, kwargs["to_lang"]))
        return "Final translation", "Translated call", False

    monkeypatch.setattr(setup, "_final_translate", translate)
    client = TestClient(create_app(tmp_path))
    out = _wait(client, _start(client, translate="en"))["result"]
    assert len(seen) == 1 and seen[0][1] == "en" and WORDS.strip() in seen[0][0]
    assert out["files"]["translation.md"] == "Final translation\n"
    assert out["files"]["transcript.txt"] and out["run"]["status"] == "ok"
    assert calls  # the summary is still made by the shared server model


def test_failed_track_texts_follow_the_client_language(tmp_path, monkeypatch):
    """The submitter's Accept-Language decides the language of the run's own texts."""
    _model(monkeypatch)
    _system_track_fails(monkeypatch)
    client = TestClient(create_app(tmp_path), headers={"Accept-Language": "en-US,en;q=0.9"})
    run = _wait(client, _start(client))["result"]["run"]
    assert run["status"] == "failed"
    assert run["error"] == "Could not transcribe the recording"
    effects = {n["effect"] for n in run["notes"] if n["step"] == "asr" and n["effect"]}
    assert effects == {"The new transcript was not saved"}
    _clean(tmp_path)
