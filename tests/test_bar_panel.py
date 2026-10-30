"""The macOS app panel: the recent recordings feed (mnema/bar/recordings.py) and the panel model
(mnema/bar/panel.py), finished strings for Swift; card actions go through BarRuntime.handle.
Recording files live in a temporary folder (a WAV is a sparse file of the needed length, without
data)."""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest
from test_bar import _patch_record

from mnema import i18n
from mnema.bar.panel import panel_model
from mnema.bar.protocol import PanelModel, Role, SettingsModel
from mnema.bar.recordings import RecordingCard, library
from mnema.bar.runtime import BarRuntime
from mnema.bar.settings_model import settings_model
from mnema.bar.state import (
    SUMMARIZING,
    BarPrefs,
    BarState,
    Job,
    JobKind,
    MenuEnv,
    Phase,
    SourceMeter,
    menu,
    title,
)
from mnema.live import remarks
from mnema.live.meta import RecordState
from mnema.live.models import FINALIZE_STEPS, STEP_MESSAGES, LiveResult
from mnema.live.remarks import Step
from mnema.live.setup import unfinished_sessions
from mnema.llm.notes import Cloud, LightNote

NOW = datetime(2026, 9, 30, 12, 0)


def _rec(
    root: Path,
    name: str,
    start: datetime,
    *,
    seconds: float = 60.0,
    text: bool = True,
    files: dict[str, str] | None = None,
) -> Path:
    """A recording folder: mic.wav of the needed length (16 kHz mono PCM16), recorded at
    start."""
    d = root / name
    d.mkdir(parents=True)
    wav = d / "mic.wav"
    with wav.open("wb") as f:
        f.truncate(44 + int(seconds * 32000))
    end = start.timestamp() + seconds
    os.utime(wav, (end, end))
    if text:
        (d / "transcript.txt").write_text("mic: привет\n", encoding="utf-8")
    for fname, body in (files or {}).items():
        (d / fname).write_text(body, encoding="utf-8")
    return d


def test_feed_newest_first_with_length_gist_and_remarks(tmp_path) -> None:
    root = tmp_path / "notes"
    old = _rec(
        root,
        "2026-09-28_Планёрка",
        datetime(2026, 9, 28, 9, 15),
        seconds=40,
        files={".mnema-meta.json": '{"runs": []}'},
    )
    summary = "Обсудили релиз\nи сроки.\n\n- релиз в пятницу\n- сроки\n"
    refused = remarks.Remark(
        step=Step.SUMMARY,
        cause="Модель не ответила: закончились деньги на счёте",
        effect="Пересказа нет",
    )
    meta = json.dumps(  # language of the last finished processing, not of a summary run
        {
            "runs": [
                {"status": "ok", "tracks": {"mic": {"lang": "en"}}},
                {"status": "ok", "tracks": {"mic": {"lang": "ru"}}},
                {"status": "stopped", "tracks": {"mic": {"lang": "de"}}},
                {
                    "kind": "summary",
                    "status": "ok",
                    "tracks": {},
                    "notes": [refused.model_dump(mode="json")],
                },
            ]
        }
    )
    day = _rec(
        root,
        "2026-09-30_Дейли",
        datetime(2026, 9, 30, 9, 15),
        seconds=72 * 60,
        files={
            "summary.txt": summary,
            ".mnema-meta.json": meta,
        },
    )
    unnamed = _rec(root, "2026-09-29_13-05", datetime(2026, 9, 29, 13, 5), text=False)
    _rec(root, ".Trash", datetime(2026, 9, 30, 11, 0))  # hidden
    (root / "Документы").mkdir()
    (root / "Документы" / "договор.pdf").write_bytes(b"%PDF")  # foreign: neither audio nor text
    (root / "заметка.md").write_text("x", encoding="utf-8")

    cards = library(root).cards
    assert [c.path for c in cards] == [day, unnamed, old]
    top = cards[0]
    assert (top.title, top.seconds, top.started) == ("Дейли", 72 * 60, datetime(2026, 9, 30, 9, 15))
    assert top.gist == "Обсудили релиз и сроки."  # the first paragraph, not a list item
    assert top.remarks == [refused.text]
    assert (top.has_text, top.has_summary, top.state) == (True, True, RecordState.PROCESSED)
    assert cards[1].title is None and cards[1].state is RecordState.UNPROCESSED
    assert cards[2].gist == ""
    assert [c.languages for c in cards] == [["ru"], [], []]  # no file or an empty one: no language
    assert [c.path for c in library(root, exclude=unnamed).cards] == [day, old]
    assert library(tmp_path / "нет такой").cards == []


def test_feed_gist_is_cut_on_a_word(tmp_path) -> None:
    about = " ".join(["слово"] * 100)
    _rec(tmp_path, "a", datetime(2026, 9, 30, 9, 0), files={"summary.txt": f"{about}\n"})
    [card] = library(tmp_path).cards
    assert len(card.gist) <= 281 and card.gist.endswith("слово…")
    assert set(card.gist.removesuffix("…").split()) == {"слово"}


def test_feed_offers_processing_only_for_sound_without_result(tmp_path) -> None:
    start = datetime(2026, 9, 30, 9, 0)
    _rec(tmp_path, "оборвана", start, text=False)
    done, stopped = '{"runs": [{"status": "ok"}]}', '{"runs": [{"status": "stopped"}]}'
    _rec(tmp_path, "без речи", start, text=False, files={".mnema-meta.json": done})
    _rec(tmp_path, "остановлена", start, text=False, files={".mnema-meta.json": stopped})
    _rec(tmp_path, "пустая", start, seconds=0, text=False)
    text_only = tmp_path / "только текст"
    text_only.mkdir()
    (text_only / "transcript.txt").write_text("x", encoding="utf-8")
    found = library(tmp_path).cards
    shown = {c.path.name: card for c, card in zip(found, _panel(cards=found).cards, strict=True)}
    assert {n: c.status for n, c in shown.items()} == {
        "оборвана": "Не обработана", "без речи": "Без речи", "остановлена": "Не расшифрована",
        "пустая": "Без речи", "только текст": "",
    }  # fmt: skip
    assert unfinished_sessions(tmp_path, limit=10) == [tmp_path / "оборвана"]


def test_feed_date_from_folder_name_when_track_was_rewritten(tmp_path) -> None:
    """A part and a repaired track are rewritten by processing, so the WAV would give the
    processing date. When the date in the folder name differs it wins, the time of day comes
    from the track (an untitled one also takes the time from the name); when it matches or
    is not a date, the time comes from the track."""
    processed = datetime(2026, 9, 30, 12, 0)
    part = _rec(tmp_path, "2026-09-27_Дейли (0.10–1.35)", processed)
    unnamed = _rec(tmp_path, "2026-09-26_13-05", processed)
    same = _rec(tmp_path, "2026-09-30_Планёрка", datetime(2026, 9, 30, 9, 15))
    odd = _rec(tmp_path, "2026-13-45_Звонок", processed)
    assert {c.path: (c.started, c.time_known) for c in library(tmp_path).cards} == {
        part: (datetime(2026, 9, 27, 12, 0), True),
        unnamed: (datetime(2026, 9, 26, 13, 5), True),
        same: (datetime(2026, 9, 30, 9, 15), True),
        odd: (processed, True),
    }


def test_feed_rereads_edited_summary(tmp_path) -> None:
    d = _rec(tmp_path, "a", datetime(2026, 9, 30, 9, 0), files={"summary.txt": "Первая.\n"})
    assert library(tmp_path).cards[0].gist == "Первая."
    summary = d / "summary.txt"
    summary.write_text("Вторая.\n", encoding="utf-8")
    later = summary.stat().st_mtime + 5  # the edit is later than the previous read
    os.utime(summary, (later, later))
    assert library(tmp_path).cards[0].gist == "Вторая."


def test_feed_skips_unreadable_folder(tmp_path) -> None:
    ok = _rec(tmp_path, "ok", datetime(2026, 9, 30, 9, 0))
    locked = _rec(tmp_path, "locked", datetime(2026, 9, 30, 10, 0))
    locked.chmod(0)
    try:
        if os.access(locked, os.R_OK):
            pytest.skip("root читает любую папку")
        assert [c.path for c in library(tmp_path).cards] == [ok]
    finally:
        locked.chmod(0o755)


def _panel(
    state: BarState | None = None,
    prefs: BarPrefs | None = None,
    env: MenuEnv | None = None,
    cards: list[RecordingCard] | None = None,
) -> PanelModel:
    prefs = prefs or BarPrefs()
    env = env or MenuEnv(mac=True)
    settings = settings_model(prefs, env)
    return panel_model(state or BarState(), prefs, env, cards or [], settings, now=NOW)


def _key(path: Path, action: str) -> str:
    return f"record:{uuid5(NAMESPACE_URL, str(path))}:{action}"


def _action(folder: Path, action: str) -> str:
    """The action key of the card for the folder in its root's feed."""
    [card] = [c for c in library(folder.parent).cards if c.path == folder]
    return f"record:{card.id}:{action}"


def _card(path: Path, **kw) -> RecordingCard:
    base = {
        "id": str(uuid5(NAMESPACE_URL, str(path))),
        "title": None,
        "started": datetime(2026, 9, 30, 9, 15),
        "seconds": 14 * 60,
        "gist": "",
        "has_text": True,
        "has_summary": False,
        "remarks": [],
        "state": RecordState.PROCESSED,
    }
    return RecordingCard(path=path, **(base | kw))


def test_idle_panel_start_sources_footer_notices() -> None:
    p = _panel(prefs=BarPrefs(hotkey="ctrl+opt+R"))
    assert p.screen == "idle" and p.recording is None
    assert (p.status.icon, p.status.text, p.status.tooltip) == ("idle", "", "Mnema")
    start = p.idle.start
    assert (start.key, start.title, start.role, start.enabled) == (
        "start", "Начать запись", Role.PRIMARY, True,
    )  # fmt: skip
    assert [(s.key, s.title, s.symbol, s.on) for s in p.idle.sources] == [
        ("toggle_mic", "Микрофон", "mic.fill", True),
        ("toggle_system", "Системный звук", "speaker.wave.2.fill", True),
    ]
    # the "More" menu: library commands, a separator, quit
    assert [(b.key, b.divider) for b in p.footer] == [
        ("open_folder", False), ("process_folder", False), ("quit", True),
    ]  # fmt: skip
    assert p.footer[2].title == "Завершить Mnema"  # as quitting an app on macOS
    assert (p.where, p.where_state, p.hotkey) == ("", "", "ctrl+opt+R")  # no server: empty
    assert p.settings.tabs[0].id == "record"

    off = _panel(prefs=BarPrefs(mic=False, system=False))
    assert not off.idle.start.enabled and [s.on for s in off.idle.sources] == [False, False]
    notices = _panel(BarState(error="нет доступа к папке", notice="сервер недоступен")).notices
    assert [n.text for n in notices] == ["Нет доступа к папке", "Сервер недоступен"]


def test_recording_panel_timer_levels_and_sources() -> None:
    meters = [SourceMeter(label="mic", history=[0.12345, 0.5]), SourceMeter(label="system")]
    state = BarState(phase=Phase.RECORDING, elapsed=754, meters=meters)
    p = _panel(state)
    rec = p.recording
    assert p.screen == "recording" and not rec.paused
    assert (rec.heading, rec.timer) == ("Идёт запись", "12:34")
    assert (p.status.icon, p.status.text) == ("recording", "12:34")
    assert p.status.tooltip == "Mnema: идёт запись"
    mic, system = rec.sources
    assert (mic.key, mic.on, mic.levels, mic.status) == ("source:mic", True, [0.123, 0.5], "")
    assert (system.status, system.symbol_off) == ("", "speaker.slash.fill")
    assert [(b.key, b.role) for b in rec.buttons] == [
        ("pause", Role.NORMAL), ("stop", Role.PRIMARY), ("discard", Role.DESTRUCTIVE),
        ("discreet", Role.NORMAL),
    ]  # fmt: skip
    assert rec.buttons[1].title == "Остановить"  # not "Quit": that is the app's exit
    discreet = rec.buttons[3]
    assert (discreet.checked, discreet.compact, discreet.symbol) == (False, True, "eye.slash")

    muted = [SourceMeter(label="mic", muted=True), SourceMeter(label="system")]
    paused = _panel(BarState(phase=Phase.RECORDING, paused=True, meters=muted))
    assert (paused.recording.heading, paused.status.tooltip) == ("Пауза", "Mnema: запись на паузе")
    assert [(s.on, s.status) for s in paused.recording.sources] == [(False, "Выключен"), (True, "")]
    assert paused.recording.buttons[0].key == "resume"
    # before the first tick
    fresh = _panel(BarState(phase=Phase.RECORDING), prefs=BarPrefs(system=False))
    assert [(s.key, s.on, s.status) for s in fresh.recording.sources] == [
        ("source:mic", True, ""), ("source:system", False, "Выключен"),
    ]  # fmt: skip
    hidden = _panel(state, prefs=BarPrefs(discreet=True))
    assert (hidden.status.icon, hidden.status.tooltip) == ("idle", "Mnema")  # discreet mode
    discreet = hidden.recording.buttons[3]  # on is shown by the icon's shape, not only its colour
    assert (discreet.checked, discreet.symbol) == (True, "eye.slash.circle.fill")


def _job(card: RecordingCard, **kw) -> Job:
    """Processing of the card's recording: running (running=True) or waiting."""
    return Job(id=card.id, folder=card.path, **({"running": True} | kw))


def test_processing_step_without_progress_runs_without_percent(tmp_path) -> None:
    """The step shows by the message at its start, the bar is that step's share. A model
    summary reports no share: the bar has no percentage instead of a frozen 90% (a real
    case: a four-hour call reached 60% in 10 s, then spent tens of minutes on the last 40%)."""
    rec = _card(tmp_path / "2026-09-30_09-15")

    def shown(**kw) -> tuple:
        [card] = _panel(BarState(jobs=[_job(rec, **kw)]), cards=[rec]).cards
        return card.detail, card.busy, card.progress

    summary = STEP_MESSAGES["summary"]
    assert shown(stage=summary, progress=0.5) == (summary, True, None)
    # the middle of the first step; no step announced means the first
    half = 0.5 / len(FINALIZE_STEPS)
    detail, busy, progress = shown(progress=half)
    assert (detail, busy) == (f"{STEP_MESSAGES['asr']} 50\u00a0%", True)
    assert progress == pytest.approx(0.5)


def test_processing_card_shows_step_and_offers_language_title_and_stop(tmp_path) -> None:
    """The recording being processed stays in its place in the feed: the step with its share
    in detail and a bar; the menu has open, language, own title, stop. The panel top is
    "Start recording"; the icon shows processing."""
    rec = _card(tmp_path / "2026-09-30_09-15", has_text=False, state=RecordState.PROCESSING)
    asr = STEP_MESSAGES["asr"]
    half = 0.5 / len(FINALIZE_STEPS)
    state = BarState(jobs=[_job(rec, stage=asr, progress=half, elapsed=200)])
    p = _panel(state, cards=[rec])
    [card] = p.cards
    assert p.screen == "idle" and p.idle is not None and p.idle.start.enabled
    assert (card.detail, card.busy, card.buttons) == (f"{asr} 50\u00a0%", True, [])
    key = f"record:{rec.id}"
    assert [(b.key, b.title, b.divider) for b in card.menu] == [
        (f"{key}:folder", "Открыть папку", False),
        (f"{key}:lang", "Язык: определяется…", True),
        (f"{key}:title", "Задать название…", False),
        (f"{key}:stop", "Остановить обработку", True),
    ]  # fmt: skip
    assert [(c.key, c.title, c.checked) for c in card.menu[1].children] == [
        (f"{key}:lang:ru", "Русский", False), (f"{key}:lang:en", "Английский", False),
    ]  # fmt: skip
    assert (p.status.icon, p.status.text) == ("saving", "")
    assert p.status.tooltip == "Mnema: обработка записи"
    assert p.footer[1].enabled  # "Process recording…" goes to the queue
    ready = state.model_copy(
        update={
            "jobs": [
                _job(rec, transcript_ready=rec.path / "transcript.txt", languages={"mic": "ru"})
            ]
        }
    )
    [card] = _panel(ready, cards=[rec]).cards
    assert [b.title for b in card.menu][:2] == ["Открыть расшифровку", "Открыть папку"]
    assert card.menu[2].title == "Язык: русский" and card.menu[2].children[0].checked
    summary = state.model_copy(
        update={"jobs": [_job(rec, kind=JobKind.SUMMARIZE, stage=SUMMARIZING)]}
    )
    # during the summary the language and title can no longer be chosen
    [card] = _panel(summary, cards=[rec]).cards
    assert [b.title for b in card.menu] == ["Открыть папку", "Остановить обработку"]


def test_waiting_card_shows_queue_and_can_be_taken_out(tmp_path) -> None:
    """The recording behind the processed one is "Queued", without a bar; it can be renamed,
    moved, removed from the queue and deleted."""
    rec = _card(tmp_path / "2026-09-30_09-15", has_text=False, state=RecordState.UNPROCESSED)
    [card] = _panel(BarState(jobs=[_job(rec, running=False)]), cards=[rec]).cards
    key = f"record:{rec.id}"
    assert (card.detail, card.detail_warn, card.busy, card.buttons) == (
        "В очереди",
        False,
        False,
        [],
    )
    assert [(b.key, b.divider) for b in card.menu] == [
        (f"{key}:folder", False), (f"{key}:rename", True), (f"{key}:move", False),
        (f"{key}:stop", False), (f"{key}:trash", True),
    ]  # fmt: skip
    assert card.menu[3].title == "Убрать из очереди"


def test_language_of_the_processing_card_is_the_applied_one(tmp_path) -> None:
    """The language of a processed recording is the one already applied (not decided yet:
    "detecting…"); the options are Russian and English."""
    rec = _card(tmp_path / "2026-09-30_09-15")

    def language(**kw) -> tuple:
        [card] = _panel(BarState(jobs=[_job(rec, **kw)]), cards=[rec]).cards
        item = next(b for b in card.menu if b.key.endswith(":lang"))
        return item.title, [c.key.rsplit(":", 1)[1] for c in item.children if c.checked]

    assert language() == ("Язык: определяется…", [])
    assert language(languages={"mic": "ru", "system": "ru"}) == ("Язык: русский", ["ru"])
    assert language(languages={"mic": "en", "system": "ru"}) == ("Язык: русский и английский", [])
    # chosen in the window: strictly that one
    assert language(lang="en") == (
        "Язык: английский",
        ["en"],
    )
    assert language(languages={"mic": "uk"}) == ("Язык: украинский", [])
    assert language(languages={"mic": "xx"}) == ("Язык: xx", [])  # no name: the code itself


def _menu(card) -> list[tuple[str, str, bool]]:
    """Card menu: (action, title, separator before the item)."""
    return [(b.key.rsplit(":", 1)[-1], b.title, b.divider) for b in card.menu]


def test_card_click_opens_its_folder_and_tooltip_names_language(tmp_path) -> None:
    """A click on any card opens the recording's folder; the language of the last processing
    is in the tooltip, not an inactive menu item."""
    cards = [
        _card(tmp_path / "a", title="Дейли", languages=["ru"], gist="Суть.", has_summary=True),
        _card(tmp_path / "b", title="Sync", languages=["en", "ru"]),
        _card(tmp_path / "c", has_text=False, state=RecordState.UNPROCESSED),
        _card(tmp_path / "d", has_text=False, state=RecordState.NO_SPEECH),
    ]
    shown = _panel(cards=cards).cards
    assert [c.open_key for c in shown] == [_key(tmp_path / n, "folder") for n in "abcd"]
    assert [c.tooltip for c in shown] == [
        "Суть.\nЯзык: русский", "Язык: русский и английский", "", "",
    ]  # fmt: skip
    assert not any(b.title.startswith("Язык") or not b.enabled for c in shown for b in c.menu)


def test_card_menus_by_record_state(tmp_path) -> None:
    """Card menu: open what exists; actions after a separator; delete after another. What
    cannot be done with the recording is not in the menu; nothing is inactive when idle."""
    daily, plain = tmp_path / "2026-09-30_Дейли", tmp_path / "2026-09-29_Планёрка"
    raw, silent = tmp_path / "2026-09-28_14-05", tmp_path / "2026-09-27_10-00"
    stopped, synced = tmp_path / "2026-09-26_10-00", tmp_path / "2026-09-25_Созвон"
    cards = [
        _card(daily, title="Дейли", gist="Суть.", has_summary=True),
        _card(plain, title="Планёрка"),
        _card(raw, has_text=False, state=RecordState.UNPROCESSED),
        _card(silent, has_text=False, state=RecordState.NO_SPEECH),
        _card(stopped, has_text=True, state=RecordState.AUDIO_ONLY),
        _card(synced, title="Созвон", has_summary=True, has_audio=False, has_sound=False),
    ]
    a, b, c, d, e, f = _panel(cards=cards).cards
    redo = ("process", "Обработать заново…", False)
    move = ("move", "Переместить в раздел", False)
    assert _menu(a) == [
        ("summary", "Открыть пересказ", False), ("transcript", "Открыть расшифровку", False),
        ("folder", "Открыть папку", False), ("rename", "Переименовать…", True), move, redo,
        ("trash", "Удалить запись", True),
    ]  # fmt: skip
    assert a.menu[-1].role == Role.DESTRUCTIVE and a.buttons == []
    assert _menu(b) == [
        ("transcript", "Открыть расшифровку", False), ("folder", "Открыть папку", False),
        ("rename", "Переименовать…", True), move, ("summarize", "Составить пересказ", False), redo,
        ("trash", "Удалить запись", True),
    ]  # fmt: skip
    unprocessed = [
        ("folder", "Открыть папку", False), ("process", "Обработать…", True),
        ("rename", "Переименовать…", False), move, ("trash", "Удалить запись", True),
    ]  # fmt: skip
    assert _menu(c) == unprocessed
    assert [(x.key, x.title, x.divider) for x in c.buttons] == [
        (_key(raw, "process"), "Обработать…", False),
    ]  # fmt: skip
    assert _menu(d) == [
        ("folder", "Открыть папку", False), ("rename", "Переименовать…", True), move, redo,
        ("trash", "Удалить запись", True),
    ]  # fmt: skip
    # the text is intact
    assert _menu(e) == [
        ("transcript", "Открыть расшифровку", False),
        *unprocessed,
    ]
    # no audio (texts came by sync): nothing to process again
    assert _menu(f) == [
        ("summary", "Открыть пересказ", False), ("transcript", "Открыть расшифровку", False),
        ("folder", "Открыть папку", False), ("rename", "Переименовать…", True), move,
        ("trash", "Удалить запись", True),
    ]  # fmt: skip
    assert all(x.enabled for card in (a, b, c, d, e, f) for x in [*card.menu, *card.buttons])

    # another recording is running: everything works on the rest, processing gets queued
    busy = _panel(BarState(phase=Phase.RECORDING), cards=cards).cards
    assert all(x.enabled for card in busy for x in [*card.menu, *card.buttons])


def test_record_in_processing_can_not_be_moved_while_app_is_idle(tmp_path) -> None:
    """The recording is being processed (its state is running) while the app is idle: "Move
    to section" is unavailable for it and available for its neighbour."""
    busy = _card(tmp_path / "2026-09-30_10-00", has_text=False, state=RecordState.PROCESSING)
    free = _card(tmp_path / "2026-09-29_Планёрка", title="Планёрка")
    a, b = _panel(cards=[busy, free]).cards

    def move(card) -> bool:
        return next(x for x in card.menu if x.title == "Переместить в раздел").enabled

    assert not move(a) and move(b)


def test_card_detail_line_by_priority(tmp_path) -> None:
    """The card's third line: what is wrong (with a triangle) or what the recording is
    about, briefly; the remark's reason is in the tooltip; the title gets a triangle only
    when the remark is not on this line."""
    note = ["Пересказ не составлен: дневной лимит"]
    cards = [
        _card(tmp_path / "raw", has_text=False, state=RecordState.UNPROCESSED),
        _card(tmp_path / "stop", has_text=False, state=RecordState.AUDIO_ONLY),
        _card(tmp_path / "quiet", has_text=False, state=RecordState.NO_SPEECH, remarks=note),
        _card(tmp_path / "gist", gist="Суть.", has_summary=True, remarks=note),
        _card(tmp_path / "note", remarks=note),
        _card(tmp_path / "bare"),
        _card(tmp_path / "empty", has_summary=True),
    ]
    shown = [(c.detail, c.detail_warn, c.warn) for c in _panel(cards=cards).cards]
    assert shown == [
        ("Не обработана", True, False),
        ("Не расшифрована", True, False),
        ("Без речи", False, True),  # not a problem: the triangle is at the title, for the remark
        ("Суть.", False, True),
        ("Без пересказа", True, True),
        ("Без пересказа", False, False),
        ("", False, False),
    ]


def test_card_of_unreachable_server_offers_retry_and_choice(tmp_path) -> None:
    """The selected server did not answer: this recording's card says "<server> is
    unavailable", the inline button is Retry, the menu starts with Retry and "Choose
    server…" (processing settings); other cards are unchanged; outside idle Retry is
    unavailable while the settings always open."""
    raw, redone = tmp_path / "2026-09-28_14-05", tmp_path / "2026-09-29_Планёрка"
    plain = tmp_path / "2026-09-27_Дейли"
    down_note = remarks.server_down("Сервер", "mnema finish x").text
    cards = [
        _card(raw, has_text=False, state=RecordState.UNPROCESSED, remarks=[down_note]),
        _card(redone, title="Планёрка"),  # "Process again…" did not reach the server
        _card(plain, title="Дейли"),
    ]
    state = BarState(unreachable={raw: "Сервер", redone: "srv:8000"})
    down, again, other = _panel(state, cards=cards).cards
    assert [(c.status, c.detail, c.detail_warn) for c in (down, again, other)] == [
        ("Сервер недоступен", "Сервер недоступен", True),
        ("srv:8000 недоступен", "srv:8000 недоступен", True),
        ("", "Без пересказа", False),
    ]
    for card, folder in ((down, raw), (again, redone)):
        assert [(b.key, b.title) for b in card.buttons] == [(_key(folder, "retry"), "Повторить")]
        # then the menu of its state
        assert [(b.key, b.title, b.divider) for b in card.menu[:3]] == [
            (_key(folder, "retry"), "Повторить", False),
            ("choose_server", "Выбрать сервер…", False),
            (_key(folder, "folder"), "Открыть папку", True)
            if folder == raw
            else (_key(folder, "transcript"), "Открыть расшифровку", True),
        ]
        assert card.menu[-1].title == "Удалить запись" and card.open_key == _key(folder, "folder")
    assert (down.title, down.subtitle) == ("Сегодня, 9:15", "14 мин")
    assert down.remarks == []  # the "server unavailable" remark is already on the line
    assert "choose_server" not in [b.key for b in [*other.buttons, *other.menu]]
    # another recording is running: Retry is available and gets queued
    busy = _panel(state.model_copy(update={"phase": Phase.RECORDING}), cards=cards).cards[0]
    assert all(b.enabled for b in [*busy.buttons, *busy.menu])


def test_cleanup_shown_while_library_has_records(tmp_path) -> None:
    """Delete audio of processed recordings… is in the More menu while there are
    recordings, even without audio: with nothing to delete a notice says so; available when
    idle."""
    texts_only = [_card(tmp_path / "a", has_audio=False), _card(tmp_path / "b", has_audio=False)]
    assert _panel().cleanup is None  # no recordings
    assert _panel(cards=texts_only).cleanup is not None
    mixed = [*texts_only, _card(tmp_path / "c")]
    cleanup = _panel(cards=mixed).cleanup
    assert (cleanup.key, cleanup.title, cleanup.enabled) == (
        "cleanup_audio", "Удалить звук обработанных записей…", True,
    )  # fmt: skip
    assert not _panel(BarState(phase=Phase.RECORDING), cards=mixed).cleanup.enabled


def test_card_remark_is_first_sentence_cut_on_a_word(tmp_path) -> None:
    remark = "Облачная модель недоступна: " + "очень " * 20 + "долго. Второе предложение."
    [card] = _panel(cards=[_card(tmp_path, title="Звонок", remarks=[remark])]).cards
    [short] = card.remarks
    assert len(short) <= 90 and short.endswith(" очень…") and "Второе" not in short


def test_last_recording_uses_card_history_instead_of_runtime_warnings(tmp_path) -> None:
    """The last recording's remarks are short lines from the processing remarks themselves,
    not a cut full text of a file."""
    folder = tmp_path / "2026-09-30_13-05"
    note = LightNote(engine="none", cloud=Cloud.FAILED, cloud_detail="закончились деньги на счёте")
    remark = remarks.light(Step.TITLE, note)
    assert remark is not None
    state = BarState(last_result=folder / "transcript.txt", last_warnings=[remark])
    [card] = _panel(state, cards=[_card(folder, remarks=[remark.text])]).cards
    assert card.warn and card.remarks == ["Модель не ответила: закончились деньги на счёте"]


def test_card_dates_and_lengths(tmp_path) -> None:
    def shown(
        started: datetime, seconds: float, title: str | None = "Звонок", **kw
    ) -> tuple[str, str]:
        [card] = _panel(
            cards=[_card(tmp_path, title=title, started=started, seconds=seconds, **kw)]
        ).cards
        return card.title, card.subtitle

    assert shown(datetime(2026, 9, 30, 9, 15), 72 * 60) == ("Звонок", "Сегодня, 9:15 · 1 ч 12 мин")
    assert shown(datetime(2026, 9, 29, 23, 5), 7200)[1] == "Вчера, 23:05 · 2 ч"
    assert shown(datetime(2026, 9, 28, 9, 15), 14 * 60 + 50)[1] == "28 сент., 9:15 · 14 мин"
    assert shown(datetime(2025, 9, 28, 9, 15), 40)[1] == "28 сент. 2025, 9:15 · 40 с"
    assert shown(datetime(2026, 5, 3, 7, 0), 60, title=None) == ("3 мая, 7:00", "1 мин")
    assert shown(datetime(2026, 9, 30, 9, 15), 0)[1] == "Сегодня, 9:15"  # unknown length
    # unknown time (the date comes from the folder name): the day alone
    assert shown(datetime(2026, 9, 27), 60, time_known=False)[1] == "27 сент. · 1 мин"
    assert shown(datetime(2026, 9, 29), 60, time_known=False)[1] == "Вчера · 1 мин"


@pytest.mark.parametrize("mac", [True, False])
def test_where_shows_connected_server_and_its_state(mac) -> None:
    """The header is the connected server by name, without a name by address, on both
    platforms: the Mac processes itself but the recordings server is shown; no server —
    empty."""
    env = MenuEnv(mac=mac, server="http://srv:8000", server_ok=True)
    assert (_panel(env=env).where, _panel(env=env).where_state) == ("srv:8000", "ok")
    named = _panel(env=env.model_copy(update={"server_name": "Дом"}))
    assert (named.where, named.where_state) == ("Дом", "ok")
    down = _panel(env=env.model_copy(update={"server_ok": False, "server_name": "Дом"}))
    assert (down.where, down.where_state) == ("Дом недоступен", "down")  # not only by colour
    gone = _panel(env=MenuEnv(mac=mac))  # the server was disconnected
    assert (gone.where, gone.where_state) == ("", "")


def test_notice_closes_with_its_button(monkeypatch, tmp_path) -> None:
    """An error or warning notice does not stay until the next recording: each has Close,
    and after a click both are read and the yellow icon is gone."""
    monkeypatch.setenv("MNEMA_LIVE_DIR", str(tmp_path / "notes"))
    rt = BarRuntime(prefs_file=tmp_path / "bar.json")
    rt.state.error, rt.state.notice = "нет доступа к папке", "сервер недоступен"
    p = _panel(rt.state)
    assert p.status.icon == "warning"
    buttons = [b for n in p.notices for b in n.buttons]
    assert [(b.key, b.title, b.symbol, b.compact) for b in buttons if b] == [
        ("dismiss_notice", "Закрыть", "xmark", True),
    ] * 2
    rt.handle("dismiss_notice")
    p = _panel(rt.state)
    assert (p.notices, p.status.icon) == ([], "idle")


class _Platform:
    def __init__(self, answer: str | None = None) -> None:
        self.answer = answer
        self.asked: list[str | None] = []
        self.opened: list[Path] = []

    def notify(self, title: str, message: str, path: Path | None) -> None: ...

    def open(self, path: Path) -> None:
        self.opened.append(path)

    def reveal(self, path: Path) -> None: ...

    def ask_title(self, current, on_done, **kwargs) -> None:
        self.asked.append(current)
        on_done(self.answer)


def test_open_key_opens_existing_file_in_any_phase(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MNEMA_LIVE_DIR", str(tmp_path / "notes"))
    platform = _Platform()
    rt = BarRuntime(platform=platform, prefs_file=tmp_path / "bar.json")
    summary = tmp_path / "summary.txt"
    summary.write_text("Суть.\n", encoding="utf-8")
    rt.handle(f"open:{summary}")
    rt.handle(f"open:{tmp_path / 'нет.md'}")
    rt.state.phase = Phase.RECORDING
    rt.handle(f"open:{summary}")
    assert platform.opened == [summary, summary]


def test_card_click_opens_record_folder_in_any_phase(monkeypatch, tmp_path) -> None:
    """A click on the card and "Open folder" open the recording's own folder (not its place
    in Finder); the summary and the transcript have their own items; a missing file is
    skipped."""
    notes = tmp_path / "notes"
    folder = _rec(notes, "2026-09-30_Дейли", datetime(2026, 9, 30, 9, 0))
    monkeypatch.setenv("MNEMA_LIVE_DIR", str(notes))
    platform = _Platform()
    rt = BarRuntime(platform=platform, prefs_file=tmp_path / "bar.json")
    rt.handle(_action(folder, "folder"))
    rt.handle(_action(folder, "transcript"))
    rt.handle(_action(folder, "summary"))  # no summary
    rt.state.phase = Phase.RECORDING
    rt.handle(_action(folder, "folder"))
    assert platform.opened == [folder, folder / "transcript.txt", folder]


def test_rename_key_renames_only_the_folder(monkeypatch, tmp_path) -> None:
    notes = tmp_path / "notes"
    folder = notes / "2026-09-27_13-05"
    folder.mkdir(parents=True)
    (folder / "transcript.txt").write_text("x", encoding="utf-8")
    (folder / "summary.txt").write_text("Суть.\n\n- пункт\n", encoding="utf-8")
    monkeypatch.setenv("MNEMA_LIVE_DIR", str(notes))
    platform = _Platform("Дейли")
    rt = BarRuntime(platform=platform, prefs_file=tmp_path / "bar.json")
    rt.state.last_result = folder / "transcript.txt"

    rt.handle(_action(folder, "rename"))
    new = notes / "2026-09-27_Дейли"
    assert platform.asked == [None]  # an untitled recording has an empty field
    assert not folder.exists()
    assert (new / "summary.txt").read_text(encoding="utf-8") == "Суть.\n\n- пункт\n"
    assert rt.state.last_result == new / "transcript.txt"
    assert rt.state.last_summary == new / "summary.txt"

    other = notes / "2026-09-26_Планёрка"
    other.mkdir()
    (other / "transcript.txt").write_text("x", encoding="utf-8")
    platform.answer = "Планёрка с командой"
    rt.handle(_action(other, "rename"))
    assert platform.asked[-1] == "Планёрка"  # the current title is filled in
    assert (notes / "2026-09-26_Планёрка с командой").is_dir()
    assert rt.state.last_result == new / "transcript.txt"  # the last recording is the previous one

    rt.state.jobs = [Job(id=_action(new, "x").split(":")[1], folder=new, running=True)]
    # the window is open, but the processed recording cannot be renamed
    rt.handle(_action(new, "rename"))
    assert (notes / "2026-09-27_Дейли").is_dir()
    rt.state.jobs, rt.state.phase = [], Phase.RECORDING  # another one is running: allowed
    platform.answer = "Дейли команды"
    rt.handle(_action(new, "rename"))
    assert (notes / "2026-09-27_Дейли команды").is_dir()


def test_active_folder_is_the_recording_being_made(monkeypatch, tmp_path) -> None:
    """The folder of a running recording is not in the feed while recording; when processed
    it is."""

    captured: dict = {}
    _patch_record(monkeypatch, tmp_path, captured)
    monkeypatch.setattr(
        "mnema.live.recording_client.finalize_on_node", lambda *a, **k: (LiveResult(), "")
    )
    rt = BarRuntime(platform=_Platform(), prefs_file=tmp_path / "bar.json")
    assert rt.active_folder is None
    rt.handle("start")
    assert captured["started"].wait(timeout=10)
    folder = rt.active_folder
    assert folder is not None and folder.parent == tmp_path / "notes"
    rt.handle("stop")
    rt.join(timeout=10)
    assert rt.active_folder is None


class _Trash:
    """The platform Trash (the OS boundary): what went there."""

    def __init__(self, fail: bool = False) -> None:
        self.trashed: list[Path] = []
        self.fail = fail

    def trash(self, path: Path) -> None:
        if self.fail:
            raise OSError("нет доступа")
        self.trashed.append(path)
        shutil.rmtree(path)

    def notify(self, *a) -> None: ...


def test_trash_moves_recording_and_forgets_it_as_last(monkeypatch, tmp_path) -> None:
    """Delete recording on a card: the folder goes to the platform Trash; if it is the
    last recording, the menu and the panel forget it. The one being recorded is untouched."""
    notes = tmp_path / "notes"
    monkeypatch.setenv("MNEMA_LIVE_DIR", str(notes))
    folder = notes / "2026-09-30_Дейли"
    folder.mkdir(parents=True)
    (folder / "transcript.txt").write_text("mic: привет\n", encoding="utf-8")
    platform = _Trash()
    rt = BarRuntime(platform=platform, prefs_file=tmp_path / "bar.json")
    rt.state.last_result = folder / "transcript.txt"
    rt.state.last_summary = folder / "summary.txt"
    rt.handle(_action(folder, "trash"))
    assert platform.trashed == [folder] and not folder.exists()
    assert rt.state.last_result is None and rt.state.last_summary is None

    active = notes / "2026-09-30_14-05"
    active.mkdir()
    (active / "mic.wav").write_bytes(b"")
    rt._active = active  # this folder is being processed
    rt.handle(_action(active, "trash"))
    assert active.exists() and platform.trashed == [folder]

    other = notes / "2026-09-29_Планёрка"
    other.mkdir()
    (other / "transcript.txt").write_text("mic: привет\n", encoding="utf-8")
    failing = BarRuntime(platform=_Trash(fail=True), prefs_file=tmp_path / "bar2.json")
    failing.handle(_action(other, "trash"))
    assert (
        other.exists()
        and failing.state.error == "Не удалось удалить запись. Подробности в журнале Mnema"
    )


def test_panel_offers_update_and_shows_its_progress() -> None:
    """An available update is news (not a warning) with an Update button that works only
    when idle; a running update shows its step without a button."""
    what = "0.3.0"
    [offer] = _panel(env=MenuEnv(mac=True, update=what)).notices
    assert (offer.kind, offer.text, offer.progress) == ("info", "Доступна Mnema 0.3.0", None)
    assert (offer.buttons[0].key, offer.buttons[0].enabled) == ("update", True)
    queued = BarState(jobs=[Job(id="r", folder=Path("/n/r"), running=True)])
    for state in (
        BarState(phase=Phase.RECORDING),
        queued,
    ):  # waits for both the recording and the queue
        [busy] = _panel(state, env=MenuEnv(mac=True, update=what)).notices
        assert not busy.buttons[0].enabled
    [going] = _panel(
        env=MenuEnv(mac=True, update=what, update_step="Устанавливаю Mnema 0.4.0…")
    ).notices
    assert (going.kind, going.text, going.buttons, going.progress) == (
        "info",
        "Устанавливаю Mnema 0.4.0…",
        [],
        None,
    )
    downloading = MenuEnv(update=what, update_step="Скачиваю Mnema 0.3.0…", update_progress=0.07)
    [bar] = _panel(env=downloading).notices
    assert (bar.text, bar.progress, bar.buttons) == ("Скачиваю Mnema 0.3.0… 7\u00a0%", 0.07, [])
    assert _panel().notices == []


# English interface: no Russian word in what the panel and the tray show

_CYRILLIC = re.compile("[А-Яа-яЁё]")


def _russian_in(model) -> list[str]:
    """Every string of the panel (recursively) that holds Cyrillic; the settings model is
    another module's."""
    found: list[str] = []

    def walk(value) -> None:
        if isinstance(value, str):
            if _CYRILLIC.search(value):
                found.append(value)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list | tuple):
            for item in value:
                walk(item)

    walk(model.model_dump(mode="json", exclude={"settings"}))
    return found


def _english_cards(tmp_path: Path) -> list[RecordingCard]:
    return [
        _card(tmp_path / "a", title="Team sync", gist="Agreed on the release date."),
        _card(tmp_path / "b", started=datetime(2026, 9, 29, 18, 5), seconds=40, has_text=False,
              state=RecordState.UNPROCESSED),
        _card(tmp_path / "c", started=datetime(2026, 9, 28, 9, 0), seconds=3 * 3600 + 60,
              section="Clients", remarks=["The model did not answer."]),
        _card(tmp_path / "d", started=datetime(2025, 3, 1, 9, 0), has_text=False, seconds=0,
              state=RecordState.NO_SPEECH, time_known=False),
    ]  # fmt: skip


def _english(state: BarState, cards: list[RecordingCard], env: MenuEnv) -> PanelModel:
    prefs = BarPrefs(record_section="Clients")
    return panel_model(
        state, prefs, env, cards, SettingsModel(tabs=[]), now=NOW, sections=["Clients"]
    )


def test_idle_panel_in_english_has_no_russian(tmp_path) -> None:
    cards = _english_cards(tmp_path)
    state = BarState(
        error="disk is full",
        sync_missing=["x"],
        sync_errors=["connection refused"],
        unreachable={cards[2].path: "Home"},
        jobs=[Job(id=cards[0].id, folder=cards[0].path)],
    )
    env = MenuEnv(mac=True, server="http://srv:8000", server_name="Home", update="0.4.0")
    with i18n.using("en"):
        p = _english(state, cards, env)
        missing_here = _english(state.model_copy(update={"sync_missing_on_server": True}), [], env)
        refused = _english(BarState(), [], env.model_copy(update={"server_auth": False}))
    assert _russian_in(p) == [] and _russian_in(missing_here) == [] and _russian_in(refused) == []
    assert p.idle.start.title == "Start recording" and p.empty == "No recordings yet"
    assert [c.subtitle for c in p.cards] == ["Today, 9:15 · 14 min", "40 s", "3 h 1 min", ""]
    assert [c.detail for c in p.cards] == [
        "Queued", "Not processed", "Home is unavailable", "No speech"
    ]  # fmt: skip
    assert p.cards[3].title == "Mar 1, 2025" and p.cards[1].title == "Yesterday, 18:05"
    assert p.notices[1].text == "Recordings missing on this Mac: 1. They are kept on the server."
    assert p.where == "Home is unavailable"


def test_recording_panel_in_english_has_no_russian() -> None:
    state = BarState(
        phase=Phase.RECORDING,
        elapsed=754,
        meters=[SourceMeter(label="mic"), SourceMeter(label="system", muted=True)],
    )
    with i18n.using("en"):
        p = _english(state, [], MenuEnv(mac=True))
    assert _russian_in(p) == []
    assert p.recording.heading == "Recording in progress"
    assert [(s.title, s.status) for s in p.recording.sources] == [
        ("Microphone", ""), ("System audio", "Off"),
    ]  # fmt: skip


def test_tray_menu_in_english_has_no_russian(tmp_path) -> None:
    folder = tmp_path / "2026-09-30_Team sync"
    folder.mkdir()
    (folder / "transcript.txt").write_text("text", encoding="utf-8")
    state = BarState(last_result=folder / "transcript.txt", unfinished=[tmp_path / "x"])
    prefs = BarPrefs()
    with i18n.using("en"):
        idle = menu(state, prefs, MenuEnv(), sections=["Clients"])
        recording = menu(
            BarState(phase=Phase.RECORDING, meters=[SourceMeter(label="mic")]), prefs, MenuEnv()
        )
        caption = title(BarState(phase=Phase.RECORDING, elapsed=600), coarse=True)

    def titles(items) -> list[str]:
        return [t for i in items for t in (i.title, *titles(i.children))]

    shown = titles(idle) + titles(recording)
    assert [t for t in shown if _CYRILLIC.search(t)] == []
    assert "Section: No section" in shown and "Microphone   recording" in shown
    assert caption.text == "10 min"


def test_summary_step_is_recognized_in_either_language(tmp_path) -> None:
    """A step message can come from a server rendering another language: it is shown in the
    interface language and still means the same step."""
    from mnema.bar.state import can_relang, stage_text

    for stage in (SUMMARIZING, "Составляю пересказ и название…"):
        job = Job(id="x", folder=tmp_path, running=True, stage=stage)
        assert not can_relang(job)
        with i18n.using("en"):
            assert stage_text(stage) == "Writing the summary and title…"
        assert stage_text(stage) == "Составляю пересказ и название…"
