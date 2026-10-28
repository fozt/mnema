"""Shared recording pipeline: place of processing, recording start and the node job history."""

from __future__ import annotations

import json
import wave
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier

import pytest

from mnema.core import base
from mnema.live import meta, remarks, setup
from mnema.live.remarks import Step
from mnema.llm.errors import ErrorKind
from mnema.llm.notes import Cloud, LightNote

_WHERE = {True: "в настройках Mnema на Mac", False: "на сервере «Домашний»"}
_HINTS = {
    ErrorKind.INVALID_KEY: "Замените ключ модели {where}",
    ErrorKind.DISABLED: "Замените ключ модели {where}",
    ErrorKind.NO_MODEL: "Выберите другую модель {where}",
    ErrorKind.REGION: "Выберите другого провайдера {where}",
    ErrorKind.NO_MONEY: "Пополните счёт у провайдера модели",
    ErrorKind.PRIVACY: "Разрешите модель в настройках приватности: openrouter.ai/settings/privacy",
}


def _at(here: bool):
    """Processing on this Mac (LocalNode) or on the server."""
    return base.processing_here() if here else nullcontext()


@pytest.mark.parametrize("here", [True, False])
@pytest.mark.parametrize("kind", list(ErrorKind))
def test_model_failure_hint_names_where_to_fix_it(monkeypatch, kind, here) -> None:
    """A remark goes into the history and to the phone: the hint names where this recording is
    processed (the Mac, or the server by name), not the platform, and has no terminal
    commands."""
    monkeypatch.setattr(base, "STANDALONE", True)  # on a Mac too: the place decides
    note = LightNote(engine="none", cloud=Cloud.FAILED, kind=kind, cloud_detail="ошибка модели")
    with _at(here), remarks.at_place(remarks.Place(node="Домашний")):
        notice = remarks.light(Step.SUMMARY, note)
    assert notice is not None
    retry = "Когда модель заработает, нажмите «Составить пересказ» в меню записи"
    hint = _HINTS.get(kind, "").format(where=_WHERE[here])
    assert notice.hint == (f"{hint}. {retry}" if hint else retry)
    assert not any(word in notice.text for word in ("mnema ", "./install", "Настройки →"))


@pytest.mark.parametrize(
    ("here", "node", "where"),
    [
        (True, "Домашний", "в настройках Mnema на Mac"),
        (False, "Домашний", "на сервере «Домашний»"),
        (False, "", "на сервере"),  # no name, no empty quotes
        (False, "Сервер", "на сервере"),  # the default name is not repeated
    ],
)
def test_missing_model_says_where_to_set_it(monkeypatch, here, node, where) -> None:
    monkeypatch.setattr(base, "STANDALONE", True)  # on a Mac too: the place decides
    with _at(here), remarks.at_place(remarks.Place(node=node)):
        notice = remarks.light(Step.SUMMARY, LightNote(engine="none", cloud=Cloud.NOT_SET))
        without = remarks.node_no_model()
    assert notice is not None
    retry = "Когда модель заработает, нажмите «Составить пересказ» в меню записи"
    hint = f"Задайте модель {where}. {retry}"
    for remark in (notice, without):
        assert (remark.cause, remark.short, remark.hint) == ("Не настроена модель",) * 2 + (hint,)
        assert "mnema " not in remark.text and "«»" not in remark.text


def test_place_resets_after_nested_context_and_cancellation(tmp_path) -> None:
    assert setup.finish_command(tmp_path).startswith("mnema finish ")  # terminal only
    with remarks.at_place(remarks.Place(node="Внешний")):
        with pytest.raises(setup.FinalizeCancelled):
            with remarks.at_place(remarks.Place(node="Внутренний")):
                assert remarks.PLACE.get().node == "Внутренний"
                raise setup.FinalizeCancelled
        assert remarks.PLACE.get().node == "Внешний"
    assert remarks.PLACE.get().node == ""


def test_place_isolated_between_threads() -> None:
    barrier = Barrier(2)

    def worker(name):
        with remarks.at_place(remarks.Place(node=name)):
            barrier.wait(timeout=5)
            notice = remarks.light(Step.SUMMARY, LightNote(engine="none", cloud=Cloud.NOT_SET))
            assert notice is not None
            text = notice.text
        return text, remarks.PLACE.get().node

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = list(executor.map(worker, ("Первый", "Второй")))
    assert "Первый" in first[0] and "Второй" not in first[0] and first[1] == ""
    assert "Второй" in second[0] and "Первый" not in second[0] and second[1] == ""


def test_node_limits_use_recording_timezone() -> None:
    until = datetime(2026, 10, 21, 22, 30, tzinfo=UTC)
    now = until - timedelta(hours=1)
    with remarks.at_place(remarks.Place(tz=timezone(timedelta(hours=3)))):
        assert remarks.until_text(until, now) == "до 01:30"
        assert remarks.until_text(until, now, dated=True) == "до 22 окт. 01:30"
        note = LightNote(engine="none", cloud=Cloud.FAILED, kind=ErrorKind.DAILY_LIMIT, until=until)
        notice = remarks.light(Step.SUMMARY, note)
    assert notice is not None and "до 22 окт. 01:30" in notice.cause


def test_recording_started_and_job_survive_history_updates(tmp_path) -> None:
    started = "2026-10-22T00:30:00+03:00"
    (tmp_path / meta.META_FILE).write_text(json.dumps({"version": 1, "custom": {"kept": True}}))
    meta.set_started(tmp_path, started)
    meta.append(tmp_path, meta.Run(job="abc", started="2026-10-23T12:00:00Z"))
    assert meta.started(tmp_path) == started
    last = meta.last(tmp_path)
    assert last is not None and last.job == "abc" and last.started == "2026-10-23T12:00:00Z"
    data = json.loads((tmp_path / meta.META_FILE).read_text())
    assert data["started"] == started and data["custom"] == {"kept": True}
    assert meta.started(tmp_path / "missing") is None


def _wav(path: Path) -> None:
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\xe8\x03" * 16000)


def test_recover_keeps_recording_error_in_cancelled_history(monkeypatch, tmp_path):
    _wav(tmp_path / "mic.wav")
    monkeypatch.setattr(
        setup,
        "dispatch",
        lambda *a, **k: {
            "text": "Текст записи.",
            "segments": [{"start": 0, "end": 1, "text": "Текст записи."}],
            "language": "ru",
        },
    )

    def on(fraction, message):
        if message == "Распознаю микрофон…":
            raise setup.FinalizeCancelled

    with remarks.at_place(remarks.Place(node="Сервер")):
        with pytest.raises(setup.FinalizeCancelled):
            setup.recover_session(
                tmp_path,
                setup.LiveConfig(lang="ru", summary=False, mix_audio=False),
                retitle=False,
                recording_error="звук оборвался",
                on=on,
            )
    runs = json.loads((tmp_path / meta.META_FILE).read_text(encoding="utf-8"))["runs"]
    saved = meta.Run.model_validate(runs[-1])
    assert saved.notes[0].step is Step.RECORD
    assert "звук оборвался" in saved.remarks[0]
    assert saved.status == "stopped"
    assert remarks.PLACE.get().node == ""


@pytest.mark.parametrize(("here", "where"), [(False, "сервере"), (True, "Mac")])
@pytest.mark.parametrize("label", ["mic", "system", "другая"])
def test_node_asr_failure_hides_paths_and_names_track(
    monkeypatch, tmp_path, label, caplog, here, where
) -> None:
    monkeypatch.setattr(base, "STANDALONE", True)  # the place follows processing, not the platform
    _wav(tmp_path / f"{label}.wav")

    def dispatch(*a, **k):
        raise OSError("Не удалось прочитать /srv/x/mic.wav")

    monkeypatch.setattr(setup, "dispatch", dispatch)
    messages = []
    with _at(here), remarks.at_place(remarks.Place(node="Сервер")):
        result, _ = setup.recover_session(
            tmp_path,
            setup.LiveConfig(summary=False, mix_audio=False),
            retitle=False,
            on=lambda _, message: messages.append(message),
        )
    [notice] = [r for r in result.remarks if r.step is Step.ASR]
    assert "/srv" not in notice.text and "mnema " not in notice.text and not notice.command
    assert notice.hint == "Когда причина будет устранена, обработайте запись ещё раз"
    assert "/srv/x/mic.wav" in caplog.text
    saved = meta.last(tmp_path)
    assert saved is not None and saved.tracks[label].error == f"сбой на {where}"
    assert f": сбой на {where}" in notice.cause
    assert "/srv/x/mic.wav" not in (tmp_path / meta.META_FILE).read_text()
    expected = {"mic": "Распознаю микрофон…", "system": "Распознаю системный звук…"}
    assert expected.get(label, f"Распознаю дорожку {label}…") in messages


def test_node_asr_error_preserves_known_provider_reason() -> None:
    class Refused(Exception):
        status_code = 401

    error = Refused("Key rejected while processing /srv/x/mic.wav")
    with remarks.at_place(remarks.Place(node="Сервер")):
        notice = remarks.asr_failed("mic", error, live_kept=False)
    assert remarks.node_asr_reason(error) == "ключ модели не подошёл"
    assert "ключ модели не подошёл" in notice.cause and "/srv" not in notice.text
