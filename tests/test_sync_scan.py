"""Full client walk: read errors never count as vanished recordings."""

import json
import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from mnema.library.models import FileName
from mnema.sync.scan import ensure_identity, scan


def recording(root, name, *, id=None):
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "transcript.txt").write_text("слова")
    (folder / FileName.META).write_text(
        json.dumps(
            {
                "id": id or str(uuid4()),
                "started": "2020-01-02T03:04:05+03:00",
                "device": "Original",
                "runs": [{"remarks": ["human"]}],
                "unknown": {"value": 1.5},
            }
        )
    )
    return folder


LAYOUT = json.loads((Path(__file__).parent / "fixtures/library-layout-v1.json").read_text())


def build_layout(root, case):
    """The shared fixture tree: recordings with meta and text, unrelated files and folders."""
    root.mkdir(parents=True, exist_ok=True)
    for path in case["records"]:
        folder = recording(root, path)
        if "record_files" in case:
            (folder / "transcript.txt").unlink()
        for name in case.get("record_files", []):
            (folder / name).write_bytes(b"\0" * 32044)
    for path in case["tree"]:
        target = root / path
        if path.endswith("/"):
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"\0" * 32044 if target.suffix in {".wav", ".m4a"} else b"x")


def meta_folders(root):
    return sorted(p.parent.relative_to(root).as_posix() for p in root.rglob(FileName.META))


@pytest.mark.parametrize("case", LAYOUT["cases"], ids=lambda case: case["name"])
def test_shared_layout_sections_records_and_hidden_folders(tmp_path, case):
    build_layout(tmp_path, case)
    result = scan(
        tmp_path,
        set(),
        sections=set(case.get("known_sections", [])),
        records=set(case.get("known_records", [])),
    )
    assert meta_folders(tmp_path) == sorted(case["records"])
    if not case.get("complete", True):
        assert not result.complete and result.blocked_paths
        return
    assert result.complete and result.blocked_paths == []
    assert sorted(r.path for r in result.manifest.records) == sorted(case["records"])
    assert result.manifest.sections == case["sections"]


def test_record_in_a_section_subfolder_holds_the_scan_and_is_named(tmp_path):
    recording(tmp_path, "Работа/Другая")
    recording(tmp_path, "Работа/Старые/Встреча")
    (tmp_path / "Работа/Архив/Старое").mkdir(parents=True)
    result = scan(tmp_path, set())
    assert not result.complete and result.blocked_paths == ["Работа/Старые"]
    assert result.deep == ["Работа/Старые/Встреча"] and result.nested == []
    assert [r.path for r in result.manifest.records] == ["Работа/Другая"]
    assert not (tmp_path / "Работа/Старые" / FileName.META).exists()


def test_scan_all_records_sections_and_identity_without_audio(tmp_path):
    for i in range(35):
        recording(tmp_path, f"Работа/Запись{i}")
    (tmp_path / "Пустой").mkdir()
    audio = tmp_path / "Без текста"
    audio.mkdir()
    (audio / "mic.wav").write_bytes(b"audio")
    handmade = tmp_path / "Собрана вручную"
    handmade.mkdir()
    (handmade / "summary.txt").write_text("пересказ")
    result = scan(tmp_path, set())
    assert result.complete and len(result.manifest.records) == 36
    assert result.manifest.sections == ["Пустой", "Работа"]
    assert result.blocked_paths == ["Без текста"]
    assert not (audio / FileName.META).exists()
    assert json.loads((handmade / FileName.META).read_text())["id"]
    old = recording(tmp_path, "old")
    before = (old / FileName.META).read_bytes()
    assert ensure_identity(old) == json.loads(before)["id"]
    assert (old / FileName.META).read_bytes() == before


def test_busy_by_id_or_path_does_not_change_meta(tmp_path):
    a, b = recording(tmp_path, "a"), recording(tmp_path, "b")
    id = json.loads((a / FileName.META).read_text())["id"]
    before = {p: (p / FileName.META).read_bytes() for p in (a, b)}
    result = scan(tmp_path, {id, str(b)})
    assert result.complete and result.manifest.records == []
    assert set(result.blocked_paths) == {"a", "b"}
    assert {p: (p / FileName.META).read_bytes() for p in (a, b)} == before


def test_unreadable_invalid_and_outside_links_do_not_become_missing(tmp_path, monkeypatch):
    root = tmp_path / "root"
    folder = recording(root, "record")
    (folder / FileName.META).write_text("invalid")
    result = scan(root, set())
    assert not result.complete and result.blocked_paths == ["record"]
    shutil.rmtree(folder)
    outside = recording(tmp_path, "outside")
    (root / "linked").symlink_to(outside, target_is_directory=True)
    assert not scan(root, set()).complete
    (root / "linked").unlink()
    folder = recording(root, "record")
    (folder / FileName.META).unlink()
    (folder / FileName.META).symlink_to(outside / FileName.META)
    before = (outside / FileName.META).read_bytes()
    assert not scan(root, set()).complete
    assert (outside / FileName.META).read_bytes() == before
    real_stat = Path.stat

    def disappearing(path, *args, **kwargs):
        if path == folder:
            raise FileNotFoundError("after listing")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", disappearing)
    assert not scan(root, set()).complete


def test_first_scan_identity_uses_recording_date_not_copy_time(tmp_path):
    folder = tmp_path / "2020-01-02_03-04"
    folder.mkdir()
    (folder / "transcript.txt").write_text("Сохранённый текст")
    result = scan(tmp_path, set())
    assert result.complete
    data = json.loads((folder / ".mnema-meta.json").read_text())
    assert data["started"].startswith("2020-01-02T03:04")
    assert (folder / "transcript.txt").read_text() == "Сохранённый текст"


def test_library_root_through_a_link_scans_fully_but_links_out_stay_blocked(tmp_path):
    real = tmp_path / "disk" / "mnema"
    recording(real, "Работа/Встреча")
    link = tmp_path / "mnema"
    link.symlink_to(real, target_is_directory=True)
    result = scan(link, set())
    assert result.complete and result.blocked_paths == []
    assert [r.path for r in result.manifest.records] == ["Работа/Встреча"]
    (real / "Работа/Чужая").symlink_to(recording(tmp_path, "outside"), target_is_directory=True)
    result = scan(link, set())
    assert not result.complete and result.blocked_paths == ["Работа/Чужая"]
