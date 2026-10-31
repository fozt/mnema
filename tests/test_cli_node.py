"""Where to process: one rule for the app, the CLI and MCP (node.selected_processing)."""

import argparse
import json

import pytest

import mnema.cli as cli_pkg
from mnema.client import MnemaClient
from mnema.core import base
from mnema.core.base import MediaError


@pytest.mark.parametrize("fails", [False, True])
def test_capture_identity_is_created_only_after_sources_start(monkeypatch, tmp_path, fails):
    import json

    from mnema.live.setup import LiveConfig, NullTranscriber, PreparedLive, live_base
    from tests.test_live_session import FakeSource

    class Source(FakeSource):
        def start(self):
            if fails:
                raise MediaError("microphone failed")
            super().start()

    base = live_base(tmp_path / "session")
    source = Source("mic", 0)
    prepared = PreparedLive(
        config=LiveConfig(stream=False),
        base=base,
        sources=[source],
        writers={},
        dialog=None,
        session_dialog=None,
        transcribers={"mic": NullTranscriber()},
        lang=None,
        status="",
    )

    def metadata():
        return json.loads((base / ".mnema-meta.json").read_text())

    assert "id" not in metadata()
    if fails:
        with pytest.raises(MediaError):
            prepared.make_session(None).run()
        assert "id" not in metadata()
    else:
        prepared.make_session(None).run()
        first = metadata()
        assert first["id"] and first["started"]
        prepared.make_session(None).run()
        assert metadata()["id"] == first["id"]


def _cli_runner(monkeypatch, tmp_path, **flags):
    """Where the CLI sends transcription (dispatch is the boundary): (address, token) or None."""
    from mnema.cli import commands
    from mnema.cli.parser import build_parser

    src = tmp_path / "a.mp3"
    src.write_bytes(b"audio")
    args = vars(build_parser().parse_args(["transcribe", str(src), "-o", str(tmp_path / "a.txt")]))
    args.update(flags)
    seen = []

    def dispatch(op, src, params, *, runner, local, **kw):
        seen.append(None if local else (runner.url, runner.token))
        return {"text": "текст", "segments": [], "language": "ru"}

    monkeypatch.setattr(cli_pkg, "dispatch", dispatch)
    monkeypatch.setattr(cli_pkg, "run_streaming", lambda label, fn, **kw: fn(on=lambda *a: None))
    monkeypatch.setattr(
        cli_pkg, "run_with_progress", lambda label, fn, **kw: fn(on=lambda *a: None)
    )
    commands.cmd_transcribe(argparse.Namespace(**args))
    [where] = seen
    return where


SERVER = ("http://vps:8000", "vps-token")


@pytest.mark.parametrize(
    ("standalone", "saved", "server", "expected"),
    [
        (True, None, "http://vps:8000", None),  # a Mac recognizes itself by default
        (True, {"recognition": "mac"}, "http://vps:8000", None),  # "On this Mac"
        (True, {"recognition": "server"}, "http://vps:8000", SERVER),  # "On the server"
        (True, {"recognition": "cloud"}, "http://vps:8000", SERVER),  # the server's cloud
        (True, {"recognition": "server"}, "", None),  # no server connected: this Mac
        (True, {"recognition": "cloud"}, "", None),  # the Mac's own cloud engine
        (True, {"where": "server"}, "http://vps:8000", SERVER),  # an earlier version's file
        (True, {"recognition": "x"}, "http://vps:8000", None),  # an unknown value: this Mac
        (False, None, "http://vps:8000", SERVER),  # Linux processes on the server
        (False, {"recognition": "mac"}, "http://vps:8000", SERVER),  # Linux has no Mac
        (False, None, "", None),  # Linux without a server: here (recording says not connected)
    ],
    ids=[
        "mac",
        "mac-here",
        "mac-on-server",
        "mac-cloud-on-server",
        "mac-on-server-without-server",
        "mac-cloud-without-server",
        "mac-older-where",
        "mac-unknown-value",
        "linux",
        "linux-ignores-choice",
        "linux-without-server",
    ],
)
def test_app_cli_and_mcp_process_in_one_place(
    tmp_path, monkeypatch, standalone, saved, server, expected
):
    """The app, the CLI and MCP process in one place: the app's choice in bar.json holds for the
    CLI and MCP too."""
    from mnema.bar.runtime import BarRuntime
    from mnema.config import bar_prefs_file
    from mnema.dispatch import resolve_runner
    from mnema.mcp import tools

    monkeypatch.setattr(base, "STANDALONE", standalone)
    monkeypatch.setenv("MNEMA_LIVE_DIR", str(tmp_path / "notes"))
    monkeypatch.setenv("MNEMA_RUNNER", server)
    monkeypatch.setenv("MNEMA_TOKEN", "vps-token" if server else "")
    monkeypatch.setattr(MnemaClient, "health", lambda self, timeout=2.0: {"ok": True})
    monkeypatch.setattr(MnemaClient, "asr_choices", lambda self: [])
    if saved is not None:
        bar_prefs_file().parent.mkdir(parents=True, exist_ok=True)
        bar_prefs_file().write_text(json.dumps({**saved, "mic": True}), encoding="utf-8")
    app = BarRuntime(prefs_file=bar_prefs_file()).processing_connection()
    runners = []
    monkeypatch.setattr(
        tools,
        "dispatch",
        lambda op, src, params, *, runner, local, **k: (
            runners.append(None if local else runner) or {}
        ),
    )
    tools.transcribe_recording("/x/a.mp3")
    found = {None if c is None else (c.url, c.token) for c in (app, *runners)}
    assert found == {expected}
    assert _cli_runner(monkeypatch, tmp_path) == expected
    assert resolve_runner(None) == (expected[0] if expected else None)  # compress, mp3 and the like


@pytest.mark.parametrize("standalone", [True, False])
def test_cli_flags_go_over_the_rule(tmp_path, monkeypatch, standalone):
    """--runner is the given server, on a Mac too; --local is here even with a connected server
    and with "On the server" chosen on a Mac."""
    from mnema.config import bar_prefs_file

    monkeypatch.setattr(base, "STANDALONE", standalone)
    bar_prefs_file().parent.mkdir(parents=True, exist_ok=True)
    bar_prefs_file().write_text(json.dumps({"recognition": "server"}), encoding="utf-8")
    monkeypatch.setenv("MNEMA_RUNNER", "http://vps:8000")
    monkeypatch.setenv("MNEMA_TOKEN", "vps-token")
    chosen = _cli_runner(monkeypatch, tmp_path, runner="http://other:8000")
    assert chosen == ("http://other:8000", "vps-token")
    (tmp_path / "a.txt").unlink()
    assert _cli_runner(monkeypatch, tmp_path, local=True) is None


@pytest.mark.parametrize("local", [False, True])
def test_translation_on_a_connected_mac_uses_the_server_model(tmp_path, monkeypatch, local):
    """A Mac recognizes here by default, but translation needs the user's model: the CLI and MCP
    send it to the connected server; --local keeps it on this Mac."""
    from mnema.cli import commands
    from mnema.cli.parser import build_parser
    from mnema.mcp import tools

    monkeypatch.setattr(base, "STANDALONE", True)
    monkeypatch.setenv("MNEMA_RUNNER", SERVER[0])
    monkeypatch.setenv("MNEMA_TOKEN", SERVER[1])
    seen = []

    def dispatch(op, src, params, *, runner, local, **kw):
        seen.append(None if local else (runner.url, runner.token))
        return {"markdown": "# перевод"}

    monkeypatch.setattr(cli_pkg, "dispatch", dispatch)
    monkeypatch.setattr(tools, "dispatch", dispatch)
    monkeypatch.setattr(cli_pkg, "run_streaming", lambda label, fn, **kw: fn(on=lambda *a: None))
    monkeypatch.setattr(
        cli_pkg, "run_with_progress", lambda label, fn, **kw: fn(on=lambda *a: None)
    )
    src = tmp_path / "a.mp3"
    src.write_bytes(b"audio")
    flags = ["--local"] if local else []
    args = build_parser().parse_args(["translate", str(src), "-o", str(tmp_path / "a.md"), *flags])
    commands.cmd_translate(args)
    assert (tmp_path / "a.md").read_text(encoding="utf-8").startswith("# перевод")
    if not local:
        tools.translate_recording(str(src))
    assert seen == ([None] if local else [SERVER, SERVER])
