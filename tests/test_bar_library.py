"""The UI's file library: metadata, sections, local audio and the system Trash."""

import hashlib
import json
import shutil
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest

from mnema.bar.panel import card_subtitle, panel_model, visible_cards
from mnema.bar.protocol import SettingsModel
from mnema.bar.recordings import (
    cleanup_audio,
    cleanup_plan,
    find_recording,
    library,
    records_count,
    size_label,
)
from mnema.bar.state import BarPrefs, BarState, MenuEnv
from mnema.config import library_root
from mnema.core import base
from mnema.i18n import using
from mnema.library.disk import read_record
from mnema.library.models import FileName, Record, Stamp
from mnema.live.remarks import Remark, Step
from mnema.live.setup import PROCESSING_MARK, unfinished_sessions
from mnema.sync.state import Checkpoint, checkpoint_path, save

SERVER = "http://library.test:8000"
CONNECTION = f"{SERVER}#token=t"


def record(root, name, *, started="2026-10-01T05:00:00+03:00", text=True, processed=False):
    folder = root / name
    folder.mkdir(parents=True)
    runs = [{"status": "ok"}] if processed else []
    meta = {"id": str(uuid4()), "started": started, "runs": runs}
    (folder / ".mnema-meta.json").write_text(json.dumps(meta))
    if text:
        (folder / "transcript.txt").write_text("секретное слово из содержания")
    return folder, meta


LAYOUT = json.loads((Path(__file__).parent / "fixtures/library-layout-v1.json").read_text())
CLEANUP = json.loads((Path(__file__).parent / "fixtures/audio-cleanup-v1.json").read_text())


def build_layout(root, case):
    """The shared fixture's tree: recordings with meta and text (or record_files) plus unrelated
    files and folders."""
    root.mkdir(parents=True, exist_ok=True)
    for path in case["records"]:
        folder, _ = record(root, path, text="record_files" not in case)
        for name in case.get("record_files", []):
            (folder / name).write_bytes(b"\0" * 32044)
    for path in case["tree"]:
        target = root / path
        if path.endswith("/"):
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"\0" * 32044 if target.suffix in {".wav", ".m4a"} else b"x")


def synced(root, *folders, server=SERVER):
    """The folders' texts are confirmed by a sync with server: a checkpoint as SyncEngine leaves
    it."""
    records = [read_record(root, f.relative_to(root).as_posix()) for f in folders]
    save(checkpoint_path(), Checkpoint(server=server, records={r.id: r for r in records}))


def cleanup(root, **kw):
    return cleanup_audio(root, connection=CONNECTION, **kw)


def meta_folders(root):
    return sorted(p.parent.relative_to(root).as_posix() for p in root.rglob(".mnema-meta.json"))


def model(root, **kw):
    view = library(root, **kw)
    return panel_model(
        BarState(),
        BarPrefs(),
        MenuEnv(),
        view.cards,
        SettingsModel(tabs=[]),
        sections=view.sections,
    )


def test_library_reads_full_sections_and_only_title_search(tmp_path):
    for i in range(12):
        record(tmp_path, f"Работа/Звонок {i}")
    record(tmp_path, "Личное/Планы")
    (tmp_path / "Пустой").mkdir()
    panel = model(tmp_path)
    assert len(panel.cards) == 13
    assert "Пустой" in panel.sections  # an empty section is in the filter
    assert len(visible_cards(panel)) == 13
    assert visible_cards(panel, section="Пустой") == []
    assert len(visible_cards(panel, query="зВоНоК")) == 12
    assert visible_cards(panel, query="секретное") == []
    # search covers only the chosen section
    assert visible_cards(panel, query="Звонок", section="Личное") == []
    assert len(visible_cards(panel, query="Звонок", section="Работа")) == 12


def test_all_sections_are_one_dated_list_with_each_section_named(tmp_path):
    """All sections is one list by date: a recording from a section is labelled next to its date,
    one without a section is not; inside a chosen section the label is redundant."""
    record(tmp_path, "projects/2026-10-21_Дейли", started="2026-10-21T10:33:00+03:00")
    record(tmp_path, "2026-10-26_Созвон", started="2026-10-26T14:06:00+03:00")
    record(tmp_path, "projects/2026-10-27_Планёрка", started="2026-10-27T09:00:00+03:00")
    panel = model(tmp_path)
    shown = visible_cards(panel)
    assert [c.title for c in shown] == ["Планёрка", "Созвон", "Дейли"]
    lines = [card_subtitle(c, None) for c in shown]
    assert lines[0].endswith(" · projects") and lines[2].endswith(" · projects")
    assert "projects" not in lines[1]
    only = visible_cards(panel, section="projects")
    assert [c.title for c in only] == ["Планёрка", "Дейли"]
    assert all("projects" not in card_subtitle(c, "projects") for c in only)


def test_meta_dates_remarks_and_titles_survive_audio_absence_and_finder_move(tmp_path, monkeypatch):
    folder, data = record(tmp_path, "2026-10-01_Старая тема")
    old = Remark(step=Step.MIX, cause="Нет общего mp3").model_dump(mode="json")
    refused = Remark(step=Step.SUMMARY, cause="Нет пересказа").model_dump(mode="json")
    data["runs"] = [
        {"notes": [old]},
        {"notes": [refused], "error": "Ключ узла не подошёл"},
    ]
    (folder / ".mnema-meta.json").write_text(json.dumps(data))
    [card] = library(tmp_path).cards
    assert card.id == data["id"] and card.title == "Старая тема"
    assert card.started == datetime(2026, 10, 1, 5, 0)  # in the zone where it was recorded
    assert card.remarks == ["Нет пересказа.", "Ключ узла не подошёл"]  # of the last processing
    assert not card.has_audio
    (tmp_path / "Раздел").mkdir()
    moved = tmp_path / "Раздел/Имя из Finder"
    folder.rename(moved)
    assert find_recording(tmp_path, card.id) == moved
    [current] = library(tmp_path).cards
    assert current.title == "Имя из Finder" and current.started == card.started
    panel = model(tmp_path)
    keys = [b.key for b in panel.cards[0].menu]  # no audio, so no "Process again…" at all
    assert not any(k.endswith(":process") for k in keys) and keys[-2].endswith(":summarize")
    assert panel.cleanup is not None  # no audio, yet the command is there and says why
    plan = cleanup_plan(tmp_path, connection=None, busy=set)
    assert (plan.records, plan.reason) == (0, "На этом компьютере нет звука записей")
    monkeypatch.setattr(base, "STANDALONE", True)
    assert (
        cleanup_plan(tmp_path, connection=None, busy=set).reason == "На этом Mac нет звука записей"
    )


def test_summary_only_record_opens_summary_and_bare_audio_sorts_with_aware_meta(tmp_path):
    folder, _ = record(
        tmp_path, "Синхронизировано", started="2026-10-01T23:00:00-07:00", text=False
    )
    (folder / "summary.txt").write_text("Пересказ")
    bare = tmp_path / "2026-09-30_09-15"
    bare.mkdir()
    (bare / "mic.wav").write_bytes(b"\0" * 32044)
    cards = library(tmp_path).cards
    assert len(cards) == 2
    card = next(c for c in model(tmp_path).cards if c.title == "Синхронизировано")
    assert card.open_key == f"record:{card.id}:folder"
    assert [b.key for b in card.menu[:2]] == [
        f"record:{card.id}:summary", f"record:{card.id}:folder",
    ]  # fmt: skip


def test_summary_md_of_an_earlier_version_is_renamed_when_the_feed_reads_it(tmp_path):
    folder, _ = record(tmp_path, "Звонок")
    (folder / "summary.md").write_text("Суть звонка.\n\n- пункт\n")
    both, _ = record(tmp_path, "Обе")
    (both / "summary.md").write_text("старая")
    (both / "summary.txt").write_text("новая")
    section = tmp_path / "Работа"
    record(section, "Встреча")
    (section / "summary.md").write_text("чужой файл")
    card = next(c for c in library(tmp_path).cards if c.title == "Звонок")
    assert card.gist == "Суть звонка." and card.has_summary
    assert (folder / "summary.txt").read_text() == "Суть звонка.\n\n- пункт\n"
    assert not (folder / "summary.md").exists()
    assert (both / "summary.md").read_text() == "старая"  # summary.txt wins, nothing is lost
    assert (both / "summary.txt").read_text() == "новая"
    assert (section / "summary.md").exists()  # only a recording's summary is renamed


def test_cleanup_uses_native_trash_only_for_processed_idle_audio(tmp_path):
    ready, _ = record(tmp_path, "Готово", processed=True)
    busy, meta = record(tmp_path, "Занято", processed=True)
    unfinished, _ = record(tmp_path, "Не закончено", text=False, processed=True)
    for folder in (ready, busy, unfinished):
        (folder / "mic.wav").write_bytes(b"\0" * 32044)
    synced(tmp_path, ready, busy, unfinished)
    trashed = []

    def trash(path):
        trashed.append(path)
        path.rename(tmp_path.parent / path.parent.name)

    result = cleanup(tmp_path, busy=lambda: {meta["id"]}, trash=trash)
    assert result.removed == 1 and not result.errors
    assert trashed == [ready / "mic.wav"]
    assert (busy / "mic.wav").exists() and (unfinished / "mic.wav").exists()
    assert (ready / "transcript.txt").exists()
    assert not next(c for c in library(tmp_path).cards if c.path == ready).has_audio


@pytest.mark.parametrize("case", CLEANUP["cases"], ids=lambda case: case["name"])
def test_shared_cleanup_rule(tmp_path, case):
    """The shared fixture of the cleanup rule: audio goes only when the recording is processed and
    has text, and with a connected server the last sync with it confirmed those texts. The command
    shows while there is audio; it removes exactly what removed lists, and with nothing to remove
    it says why. Another library on the server since that sync confirms nothing."""
    connection = CONNECTION if case.get("selected", SERVER) else None
    folder, data = record(tmp_path, "Запись", text=False)
    (folder / ".mnema-meta.json").write_text(json.dumps({**data, "runs": case["runs"]}))
    for name, text in case["files"].items():
        (folder / name).write_text(text)
    if case["synced"] is not None:
        meta = read_record(tmp_path, "Запись").files[FileName.META]
        texts = {
            FileName(name): Stamp(sha256=hashlib.sha256(text.encode()).hexdigest(), mtime_ns=0)
            for name, text in case["synced"].items()
        }
        known = Record(id=data["id"], path="Запись", files={FileName.META: meta, **texts}, tag="")
        server = SERVER if case["server"] == "selected" else case["server"]
        foreign = case.get("foreign", "")
        save(
            checkpoint_path(),
            Checkpoint(server=server, foreign=foreign, records={known.id: known}),
        )
    assert model(tmp_path, connection=connection).cleanup is not None  # every case has audio
    plan = cleanup_plan(tmp_path, connection=connection, busy=lambda: set())
    assert plan.records == (1 if case["removed"] else 0) and bool(plan.reason)
    trashed = []
    cleanup_audio(tmp_path, connection=connection, busy=lambda: set(), trash=trashed.append)
    assert sorted(p.name for p in trashed) == case["removed"]


def test_cleanup_without_server_takes_only_processed_audio(tmp_path):
    """No server connected: a processed recording's audio goes without a sync, and an outdated sync
    with a former server does not get in the way; unprocessed and speechless recordings keep their
    audio."""
    done, _ = record(tmp_path, "Обработана", processed=True)
    raw, _ = record(tmp_path, "Не обработана")
    silent, _ = record(tmp_path, "Тишина", text=False, processed=True)
    for folder in (done, raw, silent):
        (folder / "mic.wav").write_bytes(b"\0" * 32044)
    synced(tmp_path, done, server="http://old:8000")
    (done / "transcript.txt").write_text("правка после сверки")
    plan = cleanup_plan(tmp_path, connection=None, busy=set)
    assert (plan.records, plan.bytes) == (1, 32044)
    trashed = []
    result = cleanup_audio(tmp_path, connection=None, busy=set, trash=trashed.append)
    assert result.removed == 1 and trashed == [done / "mic.wav"]


def test_cleanup_reason_when_only_unprocessed_records_have_audio(tmp_path):
    """Only an unprocessed recording and a running processing have audio: nothing to delete, and
    the reason is given; no audio at all is explained too."""
    reason = cleanup_plan(tmp_path, connection=CONNECTION, busy=set).reason
    assert reason == "На этом компьютере нет звука записей"
    raw, _ = record(tmp_path, "Не обработана", text=False)
    (raw / "mic.wav").write_bytes(b"\0" * 32044)
    going, _ = record(tmp_path, "Обрабатывается", processed=True)
    (going / "mic.wav").write_bytes(b"\0" * 32044)
    (going / PROCESSING_MARK).write_text("{}", encoding="utf-8")
    synced(tmp_path, raw, going)
    plan = cleanup_plan(tmp_path, connection=CONNECTION, busy=set)
    assert (plan.records, plan.reason) == (0, "Звук есть только у необработанных записей")


def test_cleanup_reason_never_contradicts_the_card(tmp_path):
    """Only speechless recordings have audio: said so, not "unprocessed"; text without processing
    history shows as processed on the card, so the reason is the general one."""
    silent, _ = record(tmp_path, "Тишина", text=False, processed=True)
    (silent / "mic.wav").write_bytes(b"\0" * 32044)
    synced(tmp_path, silent)
    [card] = model(tmp_path, connection=CONNECTION).cards
    assert card.status == "Без речи"
    plan = cleanup_plan(tmp_path, connection=CONNECTION, busy=set)
    assert (plan.records, plan.reason) == (0, "Звук есть только у записей без речи")
    old, _ = record(tmp_path, "Без истории")  # text, but no processing in history
    (old / "mic.wav").write_bytes(b"\0" * 32044)
    synced(tmp_path, silent, old)
    assert [c.status for c in model(tmp_path, connection=CONNECTION).cards] == ["", "Без речи"]
    plan = cleanup_plan(tmp_path, connection=CONNECTION, busy=set)
    assert (plan.records, plan.reason) == (0, "Нет записей, звук которых можно удалить")


def test_cleanup_knows_the_synced_server_however_its_address_is_written(tmp_path):
    """Sync remembers the server by its connection string; the same string written differently
    (case, no scheme, trailing slash, another token) is the same server: the texts are confirmed.
    """
    folder, _ = record(tmp_path, "Запись", processed=True)
    (folder / "mic.wav").write_bytes(b"\0" * 32044)
    synced(tmp_path, folder)
    trashed = []
    cleanup_audio(
        tmp_path,
        connection="LIBRARY.test:8000/#token=other",
        busy=lambda: set(),
        trash=trashed.append,
    )
    assert trashed == [folder / "mic.wav"]


def test_cleanup_failure_and_active_marker_leave_audio_in_place(tmp_path):
    folder, _ = record(tmp_path, "Запись", processed=True)
    (folder / "mic.wav").write_bytes(b"\0" * 32044)
    synced(tmp_path, folder)

    def unavailable(path):
        raise OSError("no native trash")

    result = cleanup(tmp_path, busy=lambda: set(), trash=unavailable)
    assert result.errors and not result.removed and (folder / "mic.wav").exists()
    (folder / PROCESSING_MARK).write_text(
        json.dumps({"node": "http://node:8000"}), encoding="utf-8"
    )
    result = cleanup(tmp_path, busy=lambda: set(), trash=unavailable)
    assert not result.errors and not result.removed


def test_ui_never_creates_root_or_follows_outside_symlink(tmp_path, monkeypatch):
    missing = tmp_path / "missing"
    monkeypatch.setenv("MNEMA_LIVE_DIR", str(missing))
    assert library_root() == missing
    assert not library(missing).cards and not missing.exists()
    root = tmp_path / "root"
    root.mkdir()
    outside, _ = record(tmp_path, "outside")
    (root / "link").symlink_to(outside, target_is_directory=True)
    assert library(root).cards == []


def test_finder_copy_becomes_new_record_without_touching_original(tmp_path):
    original, data = record(tmp_path, "z-original")
    before = (original / ".mnema-meta.json").read_bytes()
    time.sleep(0.02)  # the copy's ctime is surely later than the original's
    copy = tmp_path / "a-copy"
    shutil.copytree(original, copy)
    cards = {c.path: c.id for c in library(tmp_path).cards}
    assert cards[original] == data["id"] and cards[copy] != data["id"]
    assert (original / ".mnema-meta.json").read_bytes() == before
    copied = json.loads((copy / ".mnema-meta.json").read_text())
    assert copied == {**data, "id": cards[copy]}
    assert find_recording(tmp_path, data["id"]) == original
    assert find_recording(tmp_path, cards[copy]) == copy


@pytest.mark.parametrize("case", LAYOUT["cases"], ids=lambda case: case["name"])
def test_shared_layout_cards_and_sections(tmp_path, case):
    root = tmp_path / "mnema"  # sync.json is in tmp_path, outside the library
    build_layout(root, case)
    save(checkpoint_path(), Checkpoint(sections=case.get("known_sections", [])))
    view = library(root)
    assert meta_folders(root) == sorted(case["records"])
    if case.get("complete", True):
        assert sorted(c.path.relative_to(root).as_posix() for c in view.cards) == sorted(
            case["records"]
        )
        assert sorted(view.sections) == case["sections"]


@pytest.mark.parametrize("case", LAYOUT["cases"], ids=lambda case: case["name"])
def test_shared_layout_unfinished_sessions(tmp_path, case):
    """Unprocessed means recordings of the layout only: audio in a section (also a known empty
    one), in a section's subfolder or in an unrelated folder is not a recording and offers no
    Process."""
    root = tmp_path / "mnema"
    build_layout(root, case)
    for path in case["records"]:  # audio recorded, processing interrupted
        (root / path / "transcript.txt").unlink(missing_ok=True)
        (root / path / "mic.wav").write_bytes(b"\0" * 32044)
    for path in ("Работа/Подпапка/memo.wav", "Музыка/track.wav"):
        if (root / path.split("/")[0]).exists():
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_bytes(b"\0" * 32044)
    save(checkpoint_path(), Checkpoint(sections=case.get("known_sections", [])))
    found = sorted(p.relative_to(root).as_posix() for p in unfinished_sessions(root, limit=100))
    if case.get("complete", True):
        assert found == sorted(case["records"])
    else:
        assert set(found) <= set(case["records"])


def test_symlinked_library_root_still_shows_and_finds_records(tmp_path, monkeypatch):
    real = tmp_path / "Data" / "mnema"
    folder, data = record(real, "Звонок", processed=True)
    (folder / "mic.wav").write_bytes(b"\0" * 32044)
    synced(real, folder)
    link = tmp_path / "mnema"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv("MNEMA_LIVE_DIR", str(link))
    [card] = library(library_root()).cards
    assert card.id == data["id"] and card.path == link / "Звонок"
    assert find_recording(library_root(), data["id"]) == link / "Звонок"
    trashed = []
    result = cleanup(library_root(), busy=lambda: set(), trash=trashed.append)
    assert result.removed == 1 and not result.errors and len(trashed) == 1


@pytest.mark.parametrize("replace", [True, False])
def test_cleanup_rechecks_identity_and_texts_after_snapshot(tmp_path, replace):
    folder, _ = record(tmp_path, "Запись", processed=True)
    (folder / "mic.wav").write_bytes(b"\0" * 32044)
    synced(tmp_path, folder)
    changed = False

    def busy():
        nonlocal changed
        if not changed:
            changed = True
            if replace:
                folder.rename(tmp_path / "previous")
                record(tmp_path, "Запись", processed=True)
                (folder / "mic.wav").write_bytes(b"new audio")
            else:
                (folder / "transcript.txt").unlink()
        return set()

    result = cleanup(
        tmp_path, busy=busy, trash=lambda path: pytest.fail("changed recording must stay")
    )
    assert not result.removed and (folder / "mic.wav").exists()


def test_plain_folder_gets_identity_once_before_actions_without_changing_existing_files(tmp_path):
    from uuid import UUID

    folder = tmp_path / "Моё название"
    folder.mkdir()
    audio, text = folder / "mic.wav", folder / "transcript.txt"
    audio.write_bytes(b"\0" * 32044)
    text.write_text("Исходная расшифровка")
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in (audio, text)}
    first = model(tmp_path).cards[0]
    assert UUID(first.id)
    meta = folder / ".mnema-meta.json"
    snapshot = meta.read_bytes(), meta.stat().st_mtime_ns
    second = model(tmp_path).cards[0]
    assert first.id == second.id and first.title == second.title == "Моё название"
    assert (meta.read_bytes(), meta.stat().st_mtime_ns) == snapshot
    assert all(b.key.startswith(f"record:{first.id}:") for b in first.menu if b.enabled)
    assert {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in (audio, text)} == before


def test_first_ui_identity_uses_recording_date_not_copy_time(tmp_path):
    folder = tmp_path / "2020-01-02_Старый звонок"
    folder.mkdir()
    (folder / "transcript.txt").write_text("Сохранённый текст")
    [card] = library(tmp_path).cards
    data = json.loads((folder / ".mnema-meta.json").read_text())
    assert data["started"].startswith("2020-01-02")
    assert card.started.year == 2020 and card.title == "Старый звонок"
    assert (folder / "transcript.txt").read_text() == "Сохранённый текст"


def test_cleanup_texts_follow_the_interface_language(tmp_path, monkeypatch):
    with using("en"):
        assert cleanup_plan(tmp_path, connection=None, busy=set).reason == (
            "No recordings on this computer have audio"
        )
        monkeypatch.setattr(base, "STANDALONE", True)
        assert cleanup_plan(tmp_path, connection=None, busy=set).reason == (
            "No recordings on this Mac have audio"
        )
        en = [records_count(1), records_count(5), size_label(350 << 20), size_label(1258 << 20)]
    ru = [records_count(1), records_count(5), size_label(350 << 20), size_label(1258 << 20)]
    assert en == ["1 recording", "5 recordings", "350 MB", "1.2 GB"]
    assert ru == ["1 записи", "5 записей", "350 МБ", "1,2 ГБ"]
    assert [records_count(n) for n in (2, 11, 21, 101, 111)] == [
        "2 записей", "11 записей", "21 записи", "101 записи", "111 записей",
    ]  # fmt: skip
