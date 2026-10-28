"""The library stays plain files; errors and races never turn into deletions."""

import json
import os
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

from mnema.library.files import LibraryError, LibraryFiles
from mnema.library.models import FileBody, FileName

ID = "167619ea-d702-4d4f-95f9-339ebc86873e"
TIME = 1_700_000_000_123_456_789


def library_at(root):
    root.mkdir()  # the installer creates the library root, not the server
    return LibraryFiles(root)


def files(id=ID):
    return {
        FileName.META: FileBody(
            text=json.dumps({"id": id, "custom": {"score": 1.5}}), mtime_ns=TIME
        ),
        FileName.TRANSCRIPT: FileBody(text="Я: привет\n", mtime_ns=TIME + 1),
        FileName.SUMMARY: FileBody(text="Встреча.\n", mtime_ns=TIME + 2),
    }


def test_plain_files_per_file_update_and_external_rename(tmp_path):
    store = LibraryFiles(tmp_path)
    original = store.create(ID, "Работа/Разговор", files())
    folder = tmp_path / original.path
    assert (folder / "transcript.txt").read_text() == "Я: привет\n"
    assert (folder / "summary.txt").stat().st_mtime_ns == TIME + 2
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in folder.iterdir()}
    store.write(
        ID, FileName.SUMMARY, b"new summary", TIME + 5, original.files[FileName.SUMMARY].sha256
    )
    assert {
        p.name: (p.read_bytes(), p.stat().st_mtime_ns)
        for p in folder.iterdir()
        if p.name != "summary.txt"
    } == {name: value for name, value in before.items() if name != "summary.txt"}
    folder.rename(tmp_path / "Новое имя")
    record = LibraryFiles(tmp_path).manifest().records[0]
    assert record.id == ID and record.path == "Новое имя"
    assert record.files[FileName.SUMMARY].mtime_ns == TIME + 5
    assert store.read(ID, FileName.SUMMARY)[0] == b"new summary"
    assert store.manifest().sections == ["Работа"]


def test_server_renames_summary_md_of_an_earlier_version_when_it_lists_the_library(tmp_path):
    store = LibraryFiles(tmp_path)
    folder = tmp_path / store.create(ID, "Разговор", files()).path
    (folder / "summary.txt").rename(folder / "summary.md")
    [record] = store.manifest().records
    assert record.files[FileName.SUMMARY].mtime_ns == TIME + 2
    assert store.read(ID, FileName.SUMMARY)[0] == "Встреча.\n".encode()
    assert sorted(p.name for p in folder.iterdir()) == [
        ".mnema-meta.json", "summary.txt", "transcript.txt",
    ]  # fmt: skip


def test_empty_sections_collisions_and_conditional_move_delete(tmp_path):
    store = LibraryFiles(tmp_path)
    store.section("Пустой")
    assert store.manifest().sections == ["Пустой"]
    first = store.create(ID, "Имя", files())
    other = str(uuid4())
    second = store.create(other, "Имя", files(other))
    assert second.path == "Имя (2)"
    moved = store.move(ID, "Пустой/Имя", first.path)
    with pytest.raises(LibraryError) as conflict:
        store.move(ID, "Отставший клиент", first.path)
    assert conflict.value.status == 409
    with pytest.raises(LibraryError):
        store.section("Пустой", delete=True)
    with pytest.raises(LibraryError):
        store.remove(ID, first.tag)
    store.remove(ID, moved.tag)
    store.remove(ID, moved.tag)
    store.section("Пустой", delete=True)
    assert store.manifest().sections == []
    assert [r.id for r in store.manifest().records] == [other]


def test_conditional_concurrent_writes_never_overwrite_newer(tmp_path):
    store = LibraryFiles(tmp_path)
    record = store.create(ID, "Запись", files())
    barrier = threading.Barrier(2)

    def write(text):
        barrier.wait()
        try:
            return store.write(
                ID, FileName.TRANSCRIPT, text, TIME + 10, record.files[FileName.TRANSCRIPT].sha256
            )
        except LibraryError as error:
            return error.status

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(write, [b"first", b"second"]))
    assert results.count(409) == 1
    winner = next(result for result in results if result != 409)
    assert store.read(ID, FileName.TRANSCRIPT)[1] == winner
    with pytest.raises(LibraryError):
        store.remove(ID, record.tag)


def test_interrupted_creation_is_discoverable_and_completed_by_id(tmp_path):
    """A real process stop after meta, before the first text is published."""
    script = """
import json, os, sys
from pathlib import Path
from mnema.library.files import LibraryFiles
from mnema.library.models import FileBody, FileName
real_replace = os.replace
def replace(source, destination):
    if Path(destination).name == "transcript.txt":
        os._exit(19)
    return real_replace(source, destination)
os.replace = replace
LibraryFiles(Path(sys.argv[1])).create(sys.argv[2], "Запись", {
    FileName.META: FileBody(text=json.dumps({"id": sys.argv[2]}), mtime_ns=100),
    FileName.TRANSCRIPT: FileBody(text="слова", mtime_ns=200),
})
"""
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path), ID], timeout=15)
    assert result.returncode == 19
    store = LibraryFiles(tmp_path)
    (record,) = store.manifest().records
    assert record.id == ID and set(record.files) == {FileName.META}
    with pytest.raises(LibraryError) as duplicate:
        store.create(ID, "Запись", files())
    assert duplicate.value.status == 409
    store.write(ID, FileName.TRANSCRIPT, "слова".encode(), 200, None)
    assert store.read(ID, FileName.TRANSCRIPT)[0] == "слова".encode()
    assert [p.name for p in tmp_path.iterdir()] == ["Запись"]


def test_failed_file_replace_keeps_old_text_and_stamp(tmp_path, monkeypatch):
    store = LibraryFiles(tmp_path)
    record = store.create(ID, "Запись", files())
    before = store.read(ID, FileName.SUMMARY)
    real_replace = os.replace

    def disk_full(source, destination):
        if Path(destination).name == "summary.txt":
            raise OSError("disk full")
        return real_replace(source, destination)

    monkeypatch.setattr(os, "replace", disk_full)
    with pytest.raises(LibraryError) as error:
        store.write(ID, FileName.SUMMARY, b"new", TIME + 20, record.files[FileName.SUMMARY].sha256)
    assert error.value.status == 503
    assert store.read(ID, FileName.SUMMARY) == before
    assert len(list((tmp_path / "Запись").iterdir())) == 3


@pytest.mark.parametrize(
    "path",
    [
        "../outside",
        "/outside",
        "a/../../x",
        "a/b/c",
        "a//b",
        "",
        ".",
        ".obsidian/record",  # hidden folders are not created
        ".hidden",
        "Работа/.git",
    ],
)
def test_create_cannot_escape_or_exceed_one_section(tmp_path, path):
    store = library_at(tmp_path / "library")
    with pytest.raises(LibraryError) as error:
        store.create(ID, path, files())
    assert error.value.status == 409
    with pytest.raises(LibraryError) as error:
        store.section(".stfolder")
    assert error.value.status == 409
    assert list(store.root.iterdir()) == []


def test_links_outside_root_are_never_read_or_written(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    store = library_at(tmp_path / "library")
    (store.root / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(LibraryError):
        store.create(ID, "escape/record", files())
    with pytest.raises(LibraryError) as error:
        store.manifest()
    assert error.value.status == 503
    assert list(outside.iterdir()) == []
    (store.root / "escape").unlink()
    record = store.create(ID, "record", files())
    secret = outside / "secret"
    secret.write_text("secret")
    target = store.root / record.path / FileName.SUMMARY
    target.unlink()
    target.symlink_to(secret)
    with pytest.raises(LibraryError):
        store.write(ID, FileName.SUMMARY, b"replace", TIME, None)
    assert secret.read_text() == "secret"


def test_valid_names_and_internal_file_link_remain_usable(tmp_path):
    store = LibraryFiles(tmp_path)
    record = store.create(ID, "Имя: ? # \\", files())
    folder = tmp_path / record.path
    summary = folder / FileName.SUMMARY
    summary.rename(folder / "ordinary.txt")
    summary.symlink_to("ordinary.txt")
    stamp = store.read(ID, FileName.SUMMARY)[1]
    store.write(ID, FileName.SUMMARY, b"changed", TIME, stamp.sha256)
    assert summary.is_symlink()
    assert (folder / "ordinary.txt").read_bytes() == b"changed"


@pytest.mark.parametrize(
    "problem",
    [
        "invalid-meta",
        "missing-meta",
        "unreadable",
        "fifo",
        "symlinked-root",
    ],
)
def test_incomplete_existing_library_is_error_not_partial_manifest(tmp_path, monkeypatch, problem):
    store = library_at(tmp_path / "library")
    store.create(ID, "record", files())
    folder = store.root / "record"
    if problem == "invalid-meta":
        (folder / FileName.META).write_text("broken json")
    elif problem == "missing-meta":
        (folder / FileName.META).unlink()
    elif problem == "fifo":
        (folder / FileName.TRANSCRIPT).unlink()
        os.mkfifo(folder / FileName.TRANSCRIPT)
    elif problem == "symlinked-root":  # the root was swapped for a link pointing outside
        shutil.rmtree(store.root)
        outside = tmp_path / "outside"
        outside.mkdir()
        store.root.symlink_to(outside, target_is_directory=True)
    else:
        real_iterdir = Path.iterdir

        def unreadable(path):
            if path == folder:
                raise PermissionError("denied")
            return real_iterdir(path)

        monkeypatch.setattr(Path, "iterdir", unreadable)
    with pytest.raises(LibraryError) as error:
        store.manifest()
    assert error.value.status == 503


def test_identity_cannot_be_removed_or_reassigned(tmp_path):
    store = LibraryFiles(tmp_path)
    record = store.create(ID, "record", files())
    stamp = record.files[FileName.META]
    with pytest.raises(LibraryError):
        store.remove_file(ID, FileName.META, stamp.sha256)
    with pytest.raises(LibraryError):
        store.write(
            ID, FileName.META, json.dumps({"id": str(uuid4())}).encode(), TIME, stamp.sha256
        )
    assert store.manifest().records[0].id == ID


def test_failed_initial_meta_leaves_no_orphan_directory(tmp_path, monkeypatch):
    store = LibraryFiles(tmp_path)

    def disk_full(source, destination):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", disk_full)
    with pytest.raises(LibraryError):
        store.create(ID, "record", files())
    assert store.manifest().records == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("section", [False, True])
@pytest.mark.parametrize("error", [FileNotFoundError, PermissionError])
def test_listed_entry_stat_failure_cannot_become_partial_manifest(
    tmp_path, monkeypatch, section, error
):
    store = LibraryFiles(tmp_path)
    relative = "Работа/record" if section else "record"
    store.create(ID, relative, files())
    target = tmp_path / relative
    real_stat = Path.stat

    def changed_after_listing(path, *args, **kwargs):
        if path == target:
            raise error("entry disappeared or became unreadable after listing")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", changed_after_listing)
    with pytest.raises(LibraryError) as failure:
        store.manifest()
    assert failure.value.status == 503


LAYOUT = json.loads((Path(__file__).parent / "fixtures/library-layout-v1.json").read_text())


# known_sections and known_records are client history; on the server a section is just its folder.
@pytest.mark.parametrize(
    "case",
    [case for case in LAYOUT["cases"] if not {"known_sections", "known_records"} & case.keys()],
    ids=lambda case: case["name"],
)
def test_shared_layout_skips_foreign_and_hidden_folders(tmp_path, case):
    for path in case["records"]:
        folder = tmp_path / path
        folder.mkdir(parents=True)
        (folder / FileName.META).write_text(json.dumps({"id": str(uuid4())}))
        for name in case.get("record_files", [FileName.TRANSCRIPT]):
            (folder / name).write_bytes("слова".encode())
    for path in case["tree"]:
        target = tmp_path / path
        if path.endswith("/"):
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"x")
    store = LibraryFiles(tmp_path)
    if not case.get("complete", True):
        with pytest.raises(LibraryError) as error:
            store.manifest()
        assert error.value.status == 503
        return
    manifest = store.manifest()
    assert sorted(r.path for r in manifest.records) == sorted(case["records"])
    assert manifest.sections == case["sections"]


def test_unreadable_library_error_names_the_folder(tmp_path):
    store = LibraryFiles(tmp_path)
    store.create(ID, "Работа/Звонок", files())
    shutil.copytree(tmp_path / "Работа/Звонок", tmp_path / "Работа/Копия")
    with pytest.raises(LibraryError) as error:
        store.manifest()
    assert error.value.status == 503 and "Работа/Копия" in str(error.value)


def test_missing_root_is_unavailable_and_never_recreated(tmp_path):
    root = tmp_path / "library"
    store = LibraryFiles(root)
    assert not root.exists()
    root.mkdir()
    store.create(ID, "Работа/Звонок", files())
    shutil.rmtree(root)
    for action in (
        store.manifest,
        lambda: store.section("Работа"),
        lambda: store.create(str(uuid4()), "Новая", files()),
    ):
        with pytest.raises(LibraryError) as error:
            action()
        assert error.value.status == 503 and str(error.value) == "Библиотека недоступна"
    assert not root.exists()
    assert LibraryFiles(root) and not root.exists()
