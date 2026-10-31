"""Contract of the mnema MCP tools: tool → dispatch(op, params) where the app processes (a Mac
by itself or on the server, as chosen in the app; Linux on the connected server), plus the shape
of the answer (the engine is mocked).

Mocks only at the outer boundary (dispatch, MCP SDK).
"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

import mnema.mcp.tools as tools
from mnema.core import base
from mnema.sync.scan import scan
from mnema.sync.state import Checkpoint, checkpoint_path, save

NODE = "http://node:8123"


@pytest.fixture
def local_root(monkeypatch, tmp_path):
    """No processing server selected; home and app settings in tmp, the library at tmp/mnema."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MNEMA_RUNNER", raising=False)
    monkeypatch.setenv("MNEMA_LIVE_DIR", str(tmp_path / "mnema"))
    return tmp_path


@pytest.fixture
def node_root(monkeypatch, local_root):
    """A processing server is selected, recordings/ → tmp."""
    monkeypatch.setenv("MNEMA_RUNNER", NODE)
    return local_root


def test_transcribe_dispatches_and_saves(node_root, monkeypatch):
    calls: dict = {}

    def fake_dispatch(op, src, params, *, runner, **k):
        calls.update(op=op, src=src, params=params, runner=runner)
        return {"text": "привет мир", "language": "ru", "segments": []}

    monkeypatch.setattr(tools, "dispatch", fake_dispatch)

    out = tools.transcribe_recording("/x/зумкол.mp4", lang="ru")

    assert calls["op"] == "transcribe"
    assert calls["src"] == "/x/зумкол.mp4"
    assert calls["params"] == {"lang": "ru"}
    assert calls["runner"].url == NODE  # the selected server transcribes
    p = Path(out["transcript_path"])
    assert p.is_file() and p.name == "transcript.txt"
    assert p.read_text(encoding="utf-8") == "привет мир"
    assert out["text"] == "привет мир" and out["language"] == "ru"


def test_translate_dispatches(node_root, monkeypatch):
    calls: dict = {}

    def fake(op, src, params, *, runner, **k):
        calls.update(op=op, params=params, runner=runner)
        return {"markdown": "# перевод"}

    monkeypatch.setattr(tools, "dispatch", fake)
    out = tools.translate_recording("/x/a.mp3", to_lang="en")
    assert calls["op"] == "translate" and calls["params"] == {"to_lang": "en"}
    assert calls["runner"].url == NODE
    assert out["markdown"] == "# перевод"


def _transcribe() -> None:
    tools.transcribe_recording("/x/a.mp3", lang="ru")


def _translate() -> None:
    tools.translate_recording("/x/a.mp3", to_lang="en")


@pytest.mark.parametrize(
    ("tool", "standalone", "server"),
    [(_transcribe, True, NODE), (_transcribe, False, ""), (_translate, False, "")],
)
def test_without_server_or_on_mac_works_here(local_root, monkeypatch, tool, standalone, server):
    """A Mac without "On the server" selected recognizes by itself even with a connected server
    (translation needs the user's model there: test_cli_node); Linux without a server processes
    here, like the CLI."""
    monkeypatch.setattr(base, "STANDALONE", standalone)
    monkeypatch.setenv("MNEMA_RUNNER", server)
    calls = []

    def fake(op, src, params, *, runner, local, **k):
        calls.append((runner, local))
        return {"text": "текст", "markdown": "текст"}

    monkeypatch.setattr(tools, "dispatch", fake)
    tool()
    assert calls == [(None, True)]


def test_main_reads_app_settings_before_serving(local_root, monkeypatch):
    """mnema-mcp starts with an empty environment: the server comes from ~/.config/mnema/.env."""
    settings = local_root / ".config/mnema/.env"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(f"MNEMA_RUNNER={NODE}\nMNEMA_TOKEN=app-token\n")
    # MNEMA_RUNNER is removed by the fixture; both come back after the test.
    monkeypatch.delenv("MNEMA_TOKEN")
    calls = []
    monkeypatch.setattr(
        tools, "dispatch", lambda op, src, params, *, runner, **k: calls.append(runner) or {}
    )

    class FastMCP:  # the MCP SDK is the outer boundary
        def __init__(self, name):
            self.tools = {}

        def tool(self):
            return lambda function: self.tools.setdefault(function.__name__, function)

        def run(self):
            self.tools["translate"]("/x/a.mp3")

    sdk = types.ModuleType("mcp.server.fastmcp")
    sdk.FastMCP = FastMCP
    for name in ("mcp", "mcp.server"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", sdk)
    importlib.import_module("mnema.mcp.server").main()
    assert [(r.url, r.token) for r in calls] == [(NODE, "app-token")]


@pytest.mark.parametrize("server", ["", NODE])
def test_list_recordings_are_the_library_records(local_root, monkeypatch, server):
    """This computer's library recordings by the same walk as the app feed, whatever the server:
    recordings in the root and in sections, without unrelated files or audio in a known section;
    the text is read by the path from the list."""
    monkeypatch.setenv("MNEMA_RUNNER", server)
    library = local_root / "mnema"
    (library / "Работа/Звонок").mkdir(parents=True)
    (library / "Работа/Звонок/transcript.txt").write_text("текст", encoding="utf-8")
    (library / "2026-10-21_08-35").mkdir()
    (library / "2026-10-21_08-35/mic.wav").write_bytes(b"x")
    (library / "Пустой").mkdir()
    (library / "Пустой/mic.wav").write_bytes(b"x")  # a section of the last sync, not a recording
    (library / "a.mp3").write_bytes(b"x")
    save(checkpoint_path(), Checkpoint(sections=["Пустой", "Работа"]))
    found = tools.list_recordings()
    assert [r["name"] for r in found] == ["2026-10-21_08-35", "Работа/Звонок"]
    assert tools.read_transcript(found[1]["path"]) == "текст"


def test_transcript_stays_outside_the_library(local_root, monkeypatch):
    """Raw MCP text stays outside the library: sync does not publish it as a root recording, it is
    not in the recording list, and the agent reads it by the path from the answer."""
    monkeypatch.setattr(tools, "dispatch", lambda *a, **k: {"text": "привет", "language": "ru"})
    library = local_root / "mnema"
    library.mkdir()
    out = tools.transcribe_recording("https://example.com/talk.mp4")
    assert tools.read_transcript(out["transcript_path"]) == "привет"
    found = scan(library, set())
    assert found.complete and found.manifest.records == [] and not found.blocked_paths
    assert list(library.iterdir()) == [] and tools.list_recordings() == []


def test_missing_transcript_error_is_english_in_an_english_interface(tmp_path):
    from mnema import i18n

    with i18n.using("en"), pytest.raises(FileNotFoundError) as error:
        tools.read_transcript(str(tmp_path / "gone"))
    assert str(error.value) == f"No such file: {tmp_path / 'gone'}"
