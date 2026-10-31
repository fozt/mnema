"""Weights of the engines that are gone are removed once, by their exact folders; other models
and other programs' caches stay."""

from pathlib import Path

import pytest

from mnema import cleanup

OLD_HUB = ("models--moondream--parakeet-ultra", "models--mlx-community--whisper-large-v3-turbo")
# whisperX models: Mnema downloaded them only on Linux
WHISPERX_HUB = (
    "models--Systran--faster-whisper-large-v3",
    "models--mobiuslabsgmbh--faster-whisper-large-v3-turbo",
)
KEPT_HUB = ("models--mlx-community--whisper-small", "models--someone--parakeet-ultra-onnx")


@pytest.fixture
def cache(monkeypatch, tmp_path) -> Path:
    for name in ("HF_HOME", "HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "TORCH_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    return tmp_path / "cache"


def _weights(folder: Path) -> Path:
    folder.mkdir(parents=True)
    (folder / "weights.bin").write_bytes(b"w")
    return folder


def test_old_weights_go_once_and_the_rest_stays(cache):
    hub = cache / "huggingface" / "hub"
    old = [_weights(hub / name) for name in OLD_HUB] + [_weights(cache / "mnema" / "gigaam")]
    kept = [_weights(hub / name) for name in (*KEPT_HUB, *WHISPERX_HUB)]
    kept += [_weights(cache / "torch" / "hub"), _weights(cache / "mnema" / "other")]
    cleanup.remove_old_weights(server=False)
    assert [p for p in old if p.exists()] == []
    assert all(p.is_dir() for p in kept)
    again = _weights(hub / OLD_HUB[1])  # put back later by another program
    cleanup.remove_old_weights(server=False)
    assert again.is_dir()


def test_the_server_also_clears_whisperx_torch_and_whisper_caches(cache, monkeypatch, tmp_path):
    monkeypatch.setenv("HF_HOME", str(tmp_path / "data" / "hf"))
    hub = tmp_path / "data" / "hf" / "hub"
    old = [_weights(hub / name) for name in (*OLD_HUB, *WHISPERX_HUB)]
    old += [_weights(cache / "torch"), _weights(cache / "whisper")]
    cleanup.remove_old_weights(server=True)
    assert [p for p in old if p.exists()] == []


def test_a_folder_that_could_not_be_removed_is_tried_again_next_time(cache, monkeypatch):
    stuck = _weights(cache / "mnema" / "gigaam")
    with monkeypatch.context() as busy:  # the files are held open on the disk
        busy.setattr(cleanup.shutil, "rmtree", lambda path, **kw: None)
        cleanup.remove_old_weights(server=False)
    assert stuck.exists()
    cleanup.remove_old_weights(server=False)
    assert not stuck.exists()
