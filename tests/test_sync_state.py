"""The small atomic checkpoint holds no files and no queue of intents."""

import json
import os
from pathlib import Path

import pytest

from mnema.library.models import Record
from mnema.sync.state import Checkpoint, load, save

FIXTURE = json.loads((Path(__file__).parent / "fixtures/library-v1.json").read_text())


def test_checkpoint_roundtrip_uses_shared_wire_fixture(tmp_path):
    record = Record.model_validate(FIXTURE["manifest"]["records"][0])
    checkpoint = Checkpoint(server="http://node", records={record.id: record}, sections=["Работа"])
    path = tmp_path / "state/sync.json"
    save(path, checkpoint)
    assert load(path) == checkpoint
    assert set(json.loads(path.read_text())) == {
        "server",
        "library",
        "foreign",
        "records",
        "sections",
    }
    assert "text" not in json.loads(path.read_text())["records"][record.id]
    assert not list(path.parent.glob(".sync.json.*"))


def test_lost_or_corrupt_checkpoint_becomes_full_sync_but_read_error_is_not_hidden(
    tmp_path, monkeypatch
):
    path = tmp_path / "sync.json"
    assert load(path) == Checkpoint()
    path.write_text("broken")
    assert load(path) == Checkpoint()

    def unreadable(self):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "read_bytes", unreadable)
    with pytest.raises(PermissionError):
        load(path)


def test_checkpoint_replace_failure_preserves_previous_scan(tmp_path, monkeypatch):
    path = tmp_path / "sync.json"
    initial = Checkpoint(server="http://old", sections=["Старый"])
    save(path, initial)

    def full(source, destination):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", full)
    with pytest.raises(OSError):
        save(path, Checkpoint(server="http://new"))
    assert load(path) == initial
    assert list(tmp_path.iterdir()) == [path]
