"""`mnema finish`: finishing any recording folder, whole or a range, after an interruption.

The boundary is the offline pass (setup.dispatch) and the LLM (claude._stream_text / title_ex).
Tracks are real WAV files: a sample's value is its second, which shows which piece was cut.
"""

from __future__ import annotations

import argparse
import array
import struct
import wave
from pathlib import Path

import pytest

import mnema.cli as cli_pkg
import mnema.live.setup as setup_mod
from mnema.cli import commands
from mnema.core.base import MediaError


def _two_tracks(base: Path, seconds: int) -> None:
    """mic and system: a sample is 1000 + its second (5000 + second for system), loud enough not
    to count as silence."""
    base.mkdir(parents=True, exist_ok=True)
    for label, shift in (("mic", 1000), ("system", 5000)):
        with wave.open(str(base / f"{label}.wav"), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            for sec in range(seconds):
                w.writeframes(array.array("h", [sec + shift] * 16000).tobytes())


def _edges(path) -> tuple[int, int, float]:
    with wave.open(str(path), "rb") as w:
        n = w.getnframes()
        first = array.array("h", w.readframes(1))[0]
        w.setpos(n - 1)
        last = array.array("h", w.readframes(1))[0]
    return first, last, n / 16000


@pytest.fixture
def seen(monkeypatch) -> list:
    """mnema finish on a Mac: processing happens here (LocalNode), recognition is the dispatch
    boundary."""
    from mnema.core import base
    from tests.node_client import connect

    connect(monkeypatch)
    monkeypatch.setattr(base, "STANDALONE", True)
    got: list = []

    def fake(op, src, params, **k):
        if op == "transcribe":
            got.append((Path(src).stem, *_edges(src)))
            return {"text": "Итоги.", "segments": [{"start": 0.0, "end": 1.0, "text": "Итоги."}]}
        raise RuntimeError("нет LLM")

    monkeypatch.setattr(setup_mod, "dispatch", fake)
    monkeypatch.setattr(setup_mod.claude, "title_ex", lambda *a, **k: ("Созвон", "cloud", None))
    monkeypatch.setattr(setup_mod.claude, "_stream_text", lambda s, c, **k: c)
    monkeypatch.setattr(cli_pkg, "run_streaming", lambda label, fn, **kw: fn(on=lambda *a: None))
    return got


def _args(path, start=None, end=None) -> argparse.Namespace:
    return argparse.Namespace(
        path=str(path), start=start, end=end, lang="ru", runner=None, local=True
    )


def test_range_goes_to_sibling_folder_and_leaves_original(tmp_path, seen) -> None:
    """A range goes to a SEPARATE sibling folder with its own trimmed tracks (both cut at the same
    seconds); the original recording and its results are untouched; a second range is another
    folder."""
    base = tmp_path / "2026-09-27_13-05"
    _two_tracks(base, 180)
    for name in ("transcript.txt", "summary.md", "recording.mp3"):
        (base / name).write_text("весь звонок", encoding="utf-8")

    commands.cmd_finish(_args(base, "0:01", "0:02"))
    assert sorted(seen) == [("mic", 1060, 1119, 60.0), ("system", 5060, 5119, 60.0)]
    frag = tmp_path / "2026-09-27_Созвон (0.01–0.02)"
    assert (frag / "transcript.txt").exists() and (frag / "mic.wav").exists()
    for name in ("transcript.txt", "summary.md", "recording.mp3"):
        assert (base / name).read_text(encoding="utf-8") == "весь звонок"
    assert _edges(base / "mic.wav")[2] == 180.0

    seen.clear()
    commands.cmd_finish(_args(base, "0:00", "0:01"))
    assert (tmp_path / "2026-09-27_Созвон (0.00–0.01)" / "transcript.txt").exists()
    assert (frag / "transcript.txt").exists()


def test_own_folder_name_is_kept(tmp_path, seen) -> None:
    mine = tmp_path / "Звонок с Петей"
    _two_tracks(mine, 5)
    commands.cmd_finish(_args(mine))
    assert (mine / "transcript.txt").exists()  # the folder's own name is not replaced by a title


def test_crashed_wav_is_repaired_without_range(tmp_path, seen, capsys) -> None:
    """Interrupted: the length in a Mnema track header was never finalized (0 or stale); it is
    repaired and the WHOLE recording is transcribed, with a note in the info."""
    base = tmp_path / "2026-09-27_13-05"
    _two_tracks(base, 30)
    with (base / "mic.wav").open("r+b") as f:
        f.seek(40)
        f.write(struct.pack("<I", 0))
    with (base / "system.wav").open("r+b") as f:
        f.seek(40)
        f.write(struct.pack("<I", 16000 * 2 * 3))  # as if 3 s were recorded
    commands.cmd_finish(_args(base))
    assert sorted(seen) == [("mic", 1000, 1029, 30.0), ("system", 5000, 5029, 30.0)]
    assert "длина восстановлена" in capsys.readouterr().out


def test_out_of_range_is_a_clear_error_without_transcribing(tmp_path, seen) -> None:
    base = tmp_path / "rec"
    _two_tracks(base, 60)
    with pytest.raises(MediaError, match="позже конца записи"):
        commands.cmd_finish(_args(base, "0:05"))
    assert seen == [] and not list(tmp_path.glob("rec (*"))


def test_repair_wav_leaves_foreign_layouts_alone(tmp_path) -> None:
    """Foreign WAV layouts (a tag after the audio, metadata before it) stay as they are."""
    pcm = b"\x01\x00" * 16000
    fmt = b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16)
    tag = b"LIST" + struct.pack("<I", 12) + b"INFOINAM\x00\x00\x00\x00"
    after = b"RIFF" + struct.pack("<I", 4 + len(fmt) + 8 + len(pcm) + len(tag)) + b"WAVE"
    after += fmt + b"data" + struct.pack("<I", len(pcm)) + pcm + tag
    before = b"RIFF" + struct.pack("<I", 4 + len(tag) + len(fmt) + 8 + len(pcm)) + b"WAVE"
    before += tag + fmt + b"data" + struct.pack("<I", 0) + pcm
    for name, raw in (("after.wav", after), ("before.wav", before)):
        f = tmp_path / name
        f.write_bytes(raw)
        assert setup_mod.repair_wav(f) is False and f.read_bytes() == raw


def test_cli_and_menu_read_one_settings_file(tmp_path, monkeypatch) -> None:
    """The server and token set in the app are seen by the CLI too: settings are one file
    (~/.config/mnema/.env); a .env in the current folder is not read; disconnecting the server
    (an empty address) means no server in the CLI either; an environment variable beats the file."""
    import mnema.config as config_mod
    from mnema.config import Settings, load_env, set_env

    monkeypatch.delenv("MNEMA_RUNNER", raising=False)
    monkeypatch.delenv("MNEMA_TOKEN", raising=False)
    (tmp_path / "work").mkdir()
    (tmp_path / "work/.env").write_text("MNEMA_RUNNER=http://old:8000\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path / "work")
    set_env({"MNEMA_RUNNER": "http://srv:8000", "MNEMA_TOKEN": "из-меню"})
    monkeypatch.delenv("MNEMA_RUNNER")  # as in a new CLI process
    monkeypatch.delenv("MNEMA_TOKEN")
    load_env()
    assert Settings().runner == "http://srv:8000" and Settings().token == "из-меню"
    assert oct(config_mod.user_env_file().stat().st_mode & 0o777) == "0o600"

    set_env({"MNEMA_RUNNER": ""})
    monkeypatch.delenv("MNEMA_RUNNER")
    load_env()
    assert not Settings().runner  # disconnected in the app, so no server in the CLI either

    monkeypatch.setenv("MNEMA_RUNNER", "http://env:8000")
    load_env()
    assert Settings().runner == "http://env:8000"
