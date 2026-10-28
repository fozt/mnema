# tests/test_cli_remote.py
"""The CLI remote and local paths (--runner): MnemaClient is mocked, no engines run."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def server_answers(monkeypatch):
    # HTTP boundary only: CLI tests never start a server process.
    from mnema.client import MnemaClient

    monkeypatch.setattr(MnemaClient, "health", lambda self: {"ok": True})


def _make_args(**kwargs) -> argparse.Namespace:
    """A transcribe Namespace from the parser itself, so keys and defaults match the CLI
    (cmd_* read the attributes directly)."""
    if "input" in kwargs:  # input=... → inputs=[...]
        inp = kwargs.pop("input")
        kwargs.setdefault("inputs", [inp] if inp else [])
    from mnema.cli.parser import build_parser

    base = vars(build_parser().parse_args(["transcribe", "_placeholder_"]))
    base["inputs"] = []
    base.update(kwargs)
    return argparse.Namespace(**base)


# Progress wrapper for tests: call func with a no-op callback.
def _passthrough(label, fn, **kw):
    return fn(on=lambda f, m: None)


# ─────────────────────── _resolve_sources: files and folders ────────────────────────


def test_resolve_sources_merge_preserves_arg_order(tmp_path: Path, monkeypatch):
    """The explicit file order is kept, not re-sorted."""
    from mnema import cli

    v1 = tmp_path / "talk_v1_compressed.mov"
    v1.write_bytes(b"x")
    v2 = tmp_path / "talk_v2_compressed.mov"
    v2.write_bytes(b"x")
    captured: list[list[Path]] = []

    def fake_merge(files, dest, *, on=None):
        captured.append(list(files))
        Path(dest).write_bytes(b"m")
        return MagicMock(duration=1.0, size=1)

    monkeypatch.setattr("mnema.cli.mc.merge", fake_merge)
    # v2 first: the order must stay, not become [v1, v2]
    out = cli._resolve_sources([str(v2), str(v1)], want_video=True, merge=True)

    assert captured == [[v2, v1]]  # one join
    assert len(out) == 1


def test_resolve_sources_sorts_folder_despite_filesystem_order(tmp_path, monkeypatch):
    from mnema import cli

    first, second = tmp_path / "first.wav", tmp_path / "second.wav"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    actual_rglob = Path.rglob

    def reversed_entries(path, pattern):
        if path == tmp_path:
            return iter([second, first])
        return actual_rglob(path, pattern)

    monkeypatch.setattr(Path, "rglob", reversed_entries)
    assert cli._resolve_sources([str(tmp_path)], want_video=False) == [first, second]


# ── merge (audio mix), concat (back to back), transcribe --merge (interleave) / --concat ──


def test_transcribe_translate_is_boolean_and_keeps_all_inputs() -> None:
    """--translate is a boolean flag, not nargs='?': the first positional path is not eaten as
    a language (`--translate a.wav b.wav` keeps both inputs)."""
    from mnema.cli.parser import build_parser

    ns = build_parser().parse_args(["transcribe", "--translate", "a.wav", "b.wav"])
    assert ns.translate is True
    assert ns.inputs == ["a.wav", "b.wav"]  # both inputs intact
    assert build_parser().parse_args(["transcribe", "x.wav"]).translate is False


def test_cmd_merge_mixes_audio(tmp_path: Path, monkeypatch):
    """merge mic.wav system.wav → one audio track (mc.mix), no transcription."""
    import mnema.cli.commands as commands
    from mnema.cli.parser import build_parser

    mic = tmp_path / "mic.wav"
    mic.write_bytes(b"m")
    system = tmp_path / "system.wav"
    system.write_bytes(b"s")
    monkeypatch.setattr(commands, "_sources", lambda *a, **k: [mic, system])
    calls: list = []

    def fake_mix(files, dst, *, on=None):
        calls.append((list(files), Path(dst)))
        Path(dst).write_bytes(b"mixed")
        return Path(dst)

    monkeypatch.setattr(commands.mc, "mix", fake_mix)
    monkeypatch.setattr(commands, "done_panel", lambda p, d: None)
    monkeypatch.setattr(
        commands._cli, "run_with_progress", lambda label, op, **k: op(lambda *a: None)
    )

    commands.cmd_merge(build_parser().parse_args(["merge", str(mic), str(system)]))

    out = tmp_path / "mic+system.m4a"
    assert calls == [([mic, system], out)] and out.exists()


def test_cmd_merge_needs_at_least_two_tracks(tmp_path: Path, monkeypatch):
    """merge with one track is a clear error."""
    import mnema.cli.commands as commands
    from mnema.cli.parser import build_parser

    one = tmp_path / "mic.wav"
    one.write_bytes(b"m")
    monkeypatch.setattr(commands, "_sources", lambda *a, **k: [one])
    with pytest.raises(SystemExit):
        commands.cmd_merge(build_parser().parse_args(["merge", str(one)]))


def test_transcribe_merge_interleaves_tracks_by_time(tmp_path: Path, monkeypatch):
    """transcribe --merge mic.wav system.wav: each track is transcribed, the segments merged by
    time and labelled with the file name → one transcript interleaving mic: and system:."""
    import types

    import mnema.cli.commands as commands
    from mnema.cli.parser import build_parser

    mic = tmp_path / "mic.wav"
    mic.write_bytes(b"m")
    system = tmp_path / "system.wav"
    system.write_bytes(b"s")
    monkeypatch.setattr(commands, "_dispatch_sources", lambda *a, **k: [mic, system])
    monkeypatch.setattr(commands.mc, "probe", lambda p: types.SimpleNamespace(has_audio=True))

    per_file = {
        "mic": {
            "text": "раз три",
            "segments": [
                {"start": 0.0, "end": 1.0, "text": "раз"},
                {"start": 4.0, "end": 5.0, "text": "три"},
            ],
            "language": "ru",
        },
        "system": {
            "text": "два",
            "segments": [{"start": 2.0, "end": 3.0, "text": "два"}],
            "language": "ru",
        },
    }
    captured: list = []

    def fake_op_once(op, src, params, *, runner=None, local=False, on=None, cookies=None):
        captured.append(params.get("lang"))
        return per_file[src.stem]

    monkeypatch.setattr(commands, "_op_once", fake_op_once)
    monkeypatch.setattr(
        commands, "_run_batch", lambda label, items, **kw: [it(lambda *a: None) for it in items]
    )
    monkeypatch.setattr(commands, "done_panel", lambda p, d: None)

    commands.cmd_transcribe(
        build_parser().parse_args(["transcribe", str(mic), str(system), "--merge", "--lang", "ru"])
    )

    out = tmp_path / "mic+system.txt"
    lines = [ln for ln in out.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines == [
        "mic+system",
        "mic: раз",
        "system: два",
        "mic: три",
    ]  # the first line is the title
    assert captured == ["ru", "ru"]  # --lang reached the recognition of each track


def test_transcribe_merge_and_concat_conflict(tmp_path: Path):
    """transcribe --merge with --concat is a clear error (parallel or back to back)."""
    from mnema.cli import cmd_transcribe
    from mnema.cli.parser import build_parser

    args = build_parser().parse_args(["transcribe", "a.wav", "b.wav", "--merge", "--concat"])
    with pytest.raises(SystemExit):
        cmd_transcribe(args)


def _fake_transcribe_env(monkeypatch, result: dict) -> None:
    """cmd_transcribe without engines or ffmpeg: the op is mocked, the result lines are silent."""
    import types

    import mnema.cli.commands as commands

    monkeypatch.setattr(commands, "_run_op", lambda label, op, src, params, **kw: result)
    monkeypatch.setattr(
        commands.mc, "probe", lambda p: types.SimpleNamespace(has_audio=True, duration=10.0)
    )
    monkeypatch.setattr(commands, "info_panel", lambda *a, **k: "")
    monkeypatch.setattr(commands, "done_panel", lambda p, d: None)


def test_unavailable_selected_node_error_reaches_the_user(monkeypatch):
    import mnema.cli.commands as commands
    from mnema.core.base import MediaError

    calls = []

    def failed(op, src, params, **kw):
        calls.append(kw)
        raise MediaError("selected node down")

    monkeypatch.setattr(commands._cli, "dispatch", failed)
    monkeypatch.setattr(commands._cli, "run_with_progress", _passthrough)
    with pytest.raises(MediaError, match="selected node down"):
        commands._run_op("x", "transcribe", Path("a.mp3"), {}, runner="http://selected")
    assert len(calls) == 1 and calls[0]["runner"] == "http://selected" and not calls[0]["local"]


def test_result_output_has_no_frames_and_path_is_one_line(monkeypatch) -> None:
    """Results and summaries have no boxes: a path wider than the console prints as one logical
    line (soft_wrap) and copies whole."""
    import io

    from rich.console import Console

    import mnema.cli.ui as ui
    from mnema.core.probe import MediaInfo

    buf = io.StringIO()
    monkeypatch.setattr(ui, "console", Console(file=buf, width=40))
    long_path = Path("/home/user/projects/media-tools/долгое длинное имя файла [abc123].txt")
    ui.done_panel(long_path, "3586 слов · язык: ru")
    ui.warn_panel(long_path, "запись сохранена без транскрипта", "расшифровка не дала текста")
    ui.console.print(
        ui.info_panel(
            MediaInfo(path=long_path, duration=10.0, size=3, audio_codec="opus", video_codec=None)
        )
    )
    out = buf.getvalue()
    assert not any(ch in out for ch in "╭╮╰╯│─┏┓┗┛┃━")
    assert str(long_path) in out  # the path is not broken by a wrap
    assert "✓ готово" in out
    assert "opus" not in out  # the audio codec is noise, not shown
    video = MediaInfo(
        path=long_path, duration=10.0, size=3, audio_codec="aac", video_codec="h264", width=640
    )
    buf.truncate(0)
    buf.seek(0)
    ui.console.print(ui.info_panel(video))
    assert "h264" in buf.getvalue()  # compress, mp3: the video row is shown


def test_resolve_sources_threads_cookies_to_download(tmp_path: Path, monkeypatch):
    """--cookies-from-browser reaches download_url (cookies only on explicit opt-in)."""
    import mnema.cli.sources as sources
    from mnema.cli.sources import _resolve_sources

    seen: list[str | None] = []

    def fake_download(
        url, *, want_video, dest_dir, cookies_browser=None, cookies_file=None, on=None
    ):
        seen.append(cookies_browser)
        f = Path(dest_dir) / "clip.m4a"
        f.write_bytes(b"a")
        return f

    monkeypatch.setattr(sources.mc, "download_url", fake_download)
    monkeypatch.setattr(sources.tempfile, "gettempdir", lambda: str(tmp_path / "cache"))
    _resolve_sources(["https://x.test/v/1"], want_video=False, cookies_browser="safari")
    _resolve_sources(
        ["https://x.test/v/2"], want_video=False
    )  # another link: the cache does not interfere
    assert seen == ["safari", None]


def test_cmd_mp3_local_honors_output_flag(tmp_path: Path, monkeypatch):
    """-o: the file the op wrote next to the input moves to the given path."""
    import mnema.cli.commands as commands
    from mnema.cli.parser import build_parser

    src = tmp_path / "talk.mov"
    src.write_bytes(b"v")
    produced = tmp_path / "talk.mp3"
    produced.write_bytes(b"ID3-audio")
    _fake_transcribe_env(monkeypatch, {"path": str(produced)})
    custom = tmp_path / "куда-надо.mp3"

    commands.cmd_mp3(build_parser().parse_args(["mp3", str(src), "-o", str(custom), "--local"]))

    assert custom.read_bytes() == b"ID3-audio"
    assert not produced.exists()  # moved, not copied


def test_cmd_mp3_remote_runs_op_and_downloads_result(tmp_path: Path, monkeypatch):
    """--runner: the file is uploaded, the mp3 op runs there, the result is downloaded."""
    monkeypatch.delenv("MNEMA_TOKEN", raising=False)
    import mnema.cli.commands as commands
    from mnema.cli.parser import build_parser

    src = tmp_path / "talk.mov"
    src.write_bytes(b"v")
    monkeypatch.chdir(tmp_path)

    mock_client = MagicMock()
    mock_client.upload.return_value = "/data/uploads/u1/talk.mov"
    mock_client.run.return_value = {"path": "/data/outputs/j1/talk.mp3"}

    def fake_download(server_path, dest):
        Path(dest).write_bytes(b"remote-audio")
        return Path(dest)

    mock_client.download.side_effect = fake_download

    with (
        patch("mnema.client.MnemaClient", return_value=mock_client),
        patch("mnema.cli.run_with_progress", side_effect=_passthrough),
    ):
        commands.cmd_mp3(build_parser().parse_args(["mp3", str(src), "--runner", "http://x"]))

    assert mock_client.run.call_args[0][0] == "mp3"
    dl_args = mock_client.download.call_args[0]
    assert dl_args[0] == "/data/outputs/j1/talk.mp3"  # the server's result path
    assert Path(dl_args[1]) == tmp_path / "talk.mp3"
    assert (tmp_path / "talk.mp3").read_bytes() == b"remote-audio"


def test_transcribe_txt_starts_with_title_line(tmp_path: Path, monkeypatch):
    """The transcript starts with the title (the result name), a blank line, then the text."""
    import mnema.cli.commands as commands
    from mnema.cli.parser import build_parser

    rec = tmp_path / "Дейли про релиз.mov"
    rec.write_bytes(b"v")
    _fake_transcribe_env(monkeypatch, {"text": "привет команда", "segments": [], "language": "ru"})

    commands.cmd_transcribe(build_parser().parse_args(["transcribe", str(rec), "--local"]))

    out = tmp_path / "Дейли про релиз.txt"
    assert out.read_text(encoding="utf-8") == "Дейли про релиз\n\nпривет команда\n"


def test_transcribe_no_save_copies_to_clipboard_no_file(tmp_path: Path, monkeypatch):
    """--no-save: no file; the body (without the title line) goes to the clipboard."""
    import mnema.cli.commands as commands
    from mnema.cli.parser import build_parser

    rec = tmp_path / "голосовое.mov"
    rec.write_bytes(b"v")
    _fake_transcribe_env(monkeypatch, {"text": "привет команда", "segments": [], "language": "ru"})
    copied: list[str] = []
    monkeypatch.setattr(
        "mnema.live.clipboard.copy_to_clipboard", lambda t: copied.append(t) or True
    )

    commands.cmd_transcribe(
        build_parser().parse_args(["transcribe", str(rec), "--local", "--no-save"])
    )

    assert not (tmp_path / "голосовое.txt").exists()  # no file created
    assert copied == ["привет команда"]  # the clipboard gets the body without the title


def test_transcribe_no_save_with_output_dies(tmp_path: Path, monkeypatch):
    """--no-save with -o is a clear refusal, not a silent priority."""
    import mnema.cli.commands as commands
    from mnema.cli.parser import build_parser

    rec = tmp_path / "x.mov"
    rec.write_bytes(b"v")
    with pytest.raises(SystemExit):
        commands.cmd_transcribe(
            build_parser().parse_args(
                ["transcribe", str(rec), "--local", "--no-save", "-o", str(tmp_path / "a.txt")]
            )
        )


def test_transcribe_concat_title_lists_each_part(tmp_path: Path, monkeypatch):
    """--concat: each part's name comes before its piece of the transcript (by duration
    offsets), not all names on top."""
    import mnema.cli.commands as commands
    from mnema.cli.parser import build_parser

    p1 = tmp_path / "часть1.mov"
    p1.write_bytes(b"a")
    p2 = tmp_path / "часть2.mov"
    p2.write_bytes(b"b")

    def fake_merge(sources, dest, on=None):
        Path(dest).write_bytes(b"ab")
        return dest

    monkeypatch.setattr("mnema.cli.mc.merge", fake_merge)
    result = {
        "text": "начало разговора продолжение",
        "segments": [
            {"start": 0.0, "end": 3.0, "text": "начало разговора"},
            {
                "start": 11.0,
                "end": 14.0,
                "text": "продолжение",
            },  # after the offset of part 2 (10 s)
        ],
        "language": "ru",
    }
    _fake_transcribe_env(monkeypatch, result)

    commands.cmd_transcribe(
        build_parser().parse_args(["transcribe", str(p1), str(p2), "--concat", "--local"])
    )

    out = tmp_path / "часть_merged.txt"
    assert out.read_text(encoding="utf-8") == (
        "часть1\n\nначало разговора\n\nчасть2\n\nпродолжение\n"
    )


def test_transcribe_translate_md_keeps_title_untranslated(tmp_path: Path, monkeypatch):
    """--translate: the title is metadata and never goes to the LLM; .ru.md starts with the same
    first line as .txt, then the translation."""
    import mnema.cli.commands as commands
    from mnema.cli.parser import build_parser

    rec = tmp_path / "standup.mov"
    rec.write_bytes(b"v")
    _fake_transcribe_env(monkeypatch, {"text": "hello team", "segments": [], "language": "en"})
    sent_to_llm: list[str] = []

    def fake_run_with_progress(label, fn, **kw):
        return fn(lambda *a: None)

    monkeypatch.setattr(commands._cli, "run_with_progress", fake_run_with_progress)
    monkeypatch.setattr(
        commands,
        "_op_once",
        lambda op, path, params, **kw: {
            "markdown": sent_to_llm.append(path.read_text()) or "привет команда"
        },
    )

    commands.cmd_transcribe(
        build_parser().parse_args(["transcribe", str(rec), "--translate", "--local"])
    )

    assert sent_to_llm == ["hello team"]  # without the title
    md = (tmp_path / "standup.ru.md").read_text(encoding="utf-8")
    assert md == "standup\n\nпривет команда\n"


def test_out_path_downloaded_goes_to_cwd(tmp_path: Path):
    """The result of a downloaded link goes to the current folder, not next to the temp file."""
    from mnema import cli

    dl = tmp_path / "video.webm"  # as if downloaded to a temporary folder
    dl.write_bytes(b"x")
    cli._DOWNLOADED.add(dl)
    try:
        out = cli._out_path(dl, _make_args(), 1, ".txt")
    finally:
        cli._DOWNLOADED.discard(dl)
    assert out == Path.cwd() / "video.txt"


def test_resolve_sources_missing_explicit_input_dies(tmp_path: Path):
    """A named file that does not exist stops the command instead of being skipped: a batch
    with a gap (a --concat join above all) is worse. The usual cause is an unescaped space."""
    from mnema.cli.sources import _resolve_sources

    existing = tmp_path / "a.mp3"
    existing.write_bytes(b"a")
    with pytest.raises(SystemExit):
        _resolve_sources([str(existing), "обрывок_имени"], want_video=False)


def test_resolve_sources_expands_folder_and_keeps_files(tmp_path: Path):
    """A file and a folder → the file and the folder's media; non-media and cwd are ignored."""
    from mnema.cli import _resolve_sources

    a = tmp_path / "a.mp3"
    a.write_bytes(b"x")
    sub = tmp_path / "sub"
    sub.mkdir()
    b = sub / "b.mov"
    b.write_bytes(b"x")
    (sub / "note.md").write_bytes(b"x")  # not media: left out

    srcs = _resolve_sources([str(a), str(sub)], want_video=False)

    assert a in srcs
    assert b in srcs
    assert all(p.suffix != ".md" for p in srcs)


# ─────────────────────── cmd_transcribe: remote path ──────────────────────────


def test_dispatch_text_uploads_to_selected_node_without_media_probe(tmp_path, monkeypatch):
    from mnema import dispatch as module

    txt = tmp_path / "transcript.txt"
    txt.write_text("текст")
    monkeypatch.setattr(module.mc, "probe", lambda *a: pytest.fail("text probe"))
    client = MagicMock()
    client.upload.return_value = "/uploaded/text.txt"
    client.run.return_value = {"markdown": "text"}
    with patch("mnema.client.MnemaClient", return_value=client):
        result = module.dispatch("translate", txt, {"to_lang": "en"}, runner="https://selected")
    assert result["markdown"] == "text"
    client.upload.assert_called_once()
    assert client.run.call_args.args[:2] == ("translate", "/uploaded/text.txt")


def test_transcribe_remote_uses_client_upload_and_run(tmp_path: Path, monkeypatch):
    """With --runner dispatch uploads the file and runs the op; the result goes to .txt."""
    monkeypatch.delenv("MNEMA_TOKEN", raising=False)
    src = tmp_path / "meeting.mp3"
    src.write_bytes(b"audio")
    out = tmp_path / "meeting.txt"

    args = _make_args(input=str(src), output=str(out), runner="http://x")

    fake_result = {"text": "расшифровка текст", "segments": [], "language": "ru"}
    mock_client = MagicMock()
    mock_client.upload.return_value = "/server/uploads/meeting.mp3"
    mock_client.run.return_value = fake_result

    with (
        patch("mnema.client.MnemaClient", return_value=mock_client) as mock_cls,
        patch("mnema.cli.run_with_progress", side_effect=_passthrough),
    ):
        from mnema.cli import cmd_transcribe

        cmd_transcribe(args)

    mock_cls.assert_called_once_with("http://x", token=None)
    assert mock_client.upload.call_args[0][0] == src and mock_client.upload.call_count == 1
    mock_client.run.assert_called_once()
    # the first argument of run is the op name
    assert mock_client.run.call_args[0][0] == "transcribe"
    assert out.exists()
    assert "расшифровка текст" in out.read_text(encoding="utf-8")


@pytest.mark.parametrize("lang", ["ru", None])
def test_transcribe_remote_params_forwarded(tmp_path: Path, lang):
    """backend, model and lang reach run(); --lang pins the language, without it no lang is sent."""
    src = tmp_path / "a.mp3"
    src.write_bytes(b"x")
    out = tmp_path / "a.txt"

    args = _make_args(
        input=str(src),
        output=str(out),
        runner="http://srv",
        backend="whisperx",
        model="large-v3",
        lang=lang,
    )

    mock_client = MagicMock()
    mock_client.upload.return_value = "/srv/a.mp3"
    mock_client.run.return_value = {"text": "hello", "segments": [], "language": "ru"}

    with (
        patch("mnema.client.MnemaClient", return_value=mock_client),
        patch("mnema.cli.run_with_progress", side_effect=_passthrough),
    ):
        from mnema.cli import cmd_transcribe

        cmd_transcribe(args)

    params = mock_client.run.call_args[0][2]
    assert params.get("backend") == "whisperx"
    assert params.get("model") == "large-v3"
    assert params.get("lang") == lang and ("lang" in params) == (lang is not None)


# ─────────────────────── cmd_transcribe: link routing ──────────────────────────


@pytest.mark.parametrize("flag", [True, False])
def test_transcribe_remote_url_sends_url_no_local_download(tmp_path: Path, monkeypatch, flag):
    """transcribe <url> on the selected server (--runner X or the connected one): the link goes
    to the server (run url=), nothing is downloaded locally."""
    monkeypatch.setenv("MNEMA_RUNNER", "" if flag else "http://srv")
    monkeypatch.chdir(tmp_path)  # the result goes to the current folder

    url = "https://example.com/videos/talk.mp4"
    args = _make_args(input=url, runner="http://srv" if flag else None)

    from mnema import core as mc

    mock_client = MagicMock()
    mock_client.health.return_value = {"ok": True}
    mock_client.run.return_value = {"text": "речь по ссылке", "segments": [], "language": "ru"}

    with (
        patch("mnema.client.MnemaClient", return_value=mock_client),
        patch("mnema.cli.run_with_progress", side_effect=_passthrough),
        patch.object(mc, "download_url") as mock_dl,  # the local downloader must not run
    ):
        from mnema.cli import cmd_transcribe

        cmd_transcribe(args)

    mock_dl.assert_not_called()  # the server downloads via client.run(url=)
    mock_client.upload.assert_not_called()  # the link is not uploaded
    # run got url= (the remote link branch of dispatch), no path
    assert mock_client.run.call_args[0][0] == "transcribe"
    assert mock_client.run.call_args[1].get("url") == url
    # the result is named after the link's last path segment
    assert (tmp_path / "talk.txt").exists()


def test_transcribe_remote_url_names_result_by_source_title(tmp_path: Path, monkeypatch):
    """The runner returned source_name (yt-dlp knows the video title): the .txt is named after
    it, not after the URL tail (abc123.txt)."""
    monkeypatch.delenv("MNEMA_RUNNER", raising=False)
    monkeypatch.chdir(tmp_path)

    url = "https://www.youtube.com/watch?v=abc123"
    args = _make_args(input=url, runner="http://srv")

    mock_client = MagicMock()
    mock_client.run.return_value = {
        "text": "речь",
        "segments": [],
        "language": "ru",
        "source_name": "Видео о релизе [abc123]",
    }

    with (
        patch("mnema.client.MnemaClient", return_value=mock_client),
        patch("mnema.cli.run_with_progress", side_effect=_passthrough),
    ):
        from mnema.cli import cmd_transcribe

        cmd_transcribe(args)

    out = tmp_path / "Видео о релизе.txt"  # the title without the [id] tail
    assert out.exists()
    assert out.read_text(encoding="utf-8").startswith("Видео о релизе\n")


def test_translate_remote_runs_whole_operation_on_node(tmp_path: Path, monkeypatch):
    """With --runner the server translates and the client writes .<lang>.md."""
    from test_dispatch import _cloud_completion

    cloud = _cloud_completion(monkeypatch, answer="translated text")
    src = tmp_path / "meet.mp3"
    src.write_bytes(b"audio")
    out = tmp_path / "meet.en.md"

    args = _make_args(input=str(src), output=str(out), runner="http://x", lang="en")

    mock_client = MagicMock()
    mock_client.upload.return_value = "/srv/meet.mp3"
    mock_client.run.return_value = {"markdown": "translated text"}

    with (
        patch("mnema.client.MnemaClient", return_value=mock_client),
        patch("mnema.cli.run_with_progress", side_effect=_passthrough),
    ):
        from mnema.cli import cmd_translate

        cmd_translate(args)

    assert mock_client.upload.call_args[0][0] == src and mock_client.upload.call_count == 1
    mock_client.run.assert_called_once()
    assert mock_client.run.call_args[0][0] == "translate"
    assert mock_client.run.call_args[0][2]["to_lang"] == "en"
    assert cloud == []
    assert out.exists()
    assert "translated text" in out.read_text(encoding="utf-8")


# ─────────────────────── serve: service arguments ──────────────────────────────


def test_serve_parses_service_args():
    """The server service command (install_setup, update) parses: host, its port and folder."""
    from mnema.cli import build_parser

    args = build_parser().parse_args(
        ["serve", "--host", "0.0.0.0", "--saved-port", "--dir", "/srv/mnema"]
    )
    assert args.host == "0.0.0.0"
    assert args.saved_port is True
    assert args.dir == "/srv/mnema"


# ─────────────────────── serve: token guard on a public bind ────────────────


def _serve_args(host: str, port: int | None = 8000, dir_: str | None = None) -> argparse.Namespace:
    from mnema.cli.parser import build_parser

    base = vars(build_parser().parse_args(["serve"]))
    base.update(host=host, port=port, dir=dir_)
    return argparse.Namespace(**base)


def test_serve_public_bind_without_token_dies(monkeypatch):
    """serve on 0.0.0.0 without MNEMA_TOKEN is unsafe → die (SystemExit), uvicorn never starts."""
    monkeypatch.delenv("MNEMA_TOKEN", raising=False)

    with (
        patch("uvicorn.run") as mock_run,
        pytest.raises(SystemExit),
    ):
        from mnema.cli.commands import cmd_serve

        cmd_serve(_serve_args("0.0.0.0"))

    mock_run.assert_not_called()  # the server never started


def test_serve_does_not_load_speech_engines_at_start(monkeypatch, tmp_path):
    """Starting with NVIDIA imports no engines and loads no weights."""
    import builtins
    import importlib.machinery
    import importlib.util
    import logging
    import threading

    from mnema.cli.commands import cmd_serve

    attempted = []
    real_import = builtins.__import__
    real_find = importlib.util.find_spec
    real_exists = Path.exists

    def guarded_import(name, *args, **kwargs):
        if name.split(".")[0] in {"whisperx", "whisperlivekit"}:
            attempted.append(name)
            raise ImportError("движок не должен загружаться до запроса")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(
        importlib.util, "find_spec",
        lambda name, *a: importlib.machinery.ModuleSpec(name, None)
        if name == "whisperx" else real_find(name, *a),
    )  # fmt: skip
    monkeypatch.setattr(
        Path, "exists",
        lambda path: str(path) == "/proc/driver/nvidia/version" or real_exists(path),
    )  # fmt: skip
    # A preload, should it come back, finishes before the check (no race with a thread).
    monkeypatch.setattr(threading.Thread, "start", lambda thread: thread.run())
    logger = logging.getLogger("mnema")
    handlers = logger.handlers[:]
    try:
        with patch("uvicorn.run") as run:
            cmd_serve(_serve_args("127.0.0.1", dir_=str(tmp_path)))
        run.assert_called_once()
        assert attempted == []
    finally:
        for handler in logger.handlers[:]:
            if handler not in handlers:
                logger.removeHandler(handler)
                handler.close()


@pytest.mark.parametrize("service", [False, True], ids=["by-hand", "service"])
def test_serve_takes_this_computers_server_token_not_the_client_token(
    tmp_path, monkeypatch, service
):
    """`mnema serve` on a PC connected to a VPS: the MNEMA_TOKEN line of ~/.config/mnema/.env is
    this device's token at the VPS; its own server admits by its own token (server.env). Only the
    service (--saved-port) takes this computer's server port (server.json): `task serve` in the
    repository does not take the app server's port."""
    from fastapi.testclient import TestClient

    from mnema import config
    from mnema.cli.commands import cmd_serve

    settings = config.user_env_file().parent
    settings.mkdir(parents=True)
    (settings / ".env").write_text("MNEMA_RUNNER=http://vps:8000\nMNEMA_TOKEN=vps-token\n")
    (settings / "server.env").write_text('MNEMA_TOKEN="own-token"\n')
    (settings / "server.json").write_text('{"port": 45001}\n')
    monkeypatch.delenv("MNEMA_TOKEN")
    monkeypatch.delenv("MNEMA_RUNNER")
    config.load_env()  # like any console command
    args = _serve_args("0.0.0.0", port=None, dir_=str(tmp_path / "serve"))
    args.saved_port = service
    with patch("uvicorn.run") as run:
        cmd_serve(args)
    assert (run.call_args.kwargs["port"] == 45001) is service
    client = TestClient(run.call_args.args[0])

    def status(token: str) -> int:
        return client.get(
            "/api/capabilities", headers={"Authorization": f"Bearer {token}"}
        ).status_code

    assert (status("own-token"), status("vps-token")) == (200, 401)


def test_service_without_its_port_file_does_not_start_on_another_port(monkeypatch):
    """The server service without ~/.config/mnema/server.json does not silently take another
    port the clients do not know: a clear error instead of a start."""
    monkeypatch.delenv("MNEMA_TOKEN", raising=False)
    args = _serve_args("127.0.0.1", port=None)
    args.saved_port = True
    with patch("uvicorn.run") as run, pytest.raises(SystemExit):
        from mnema.cli.commands import cmd_serve

        cmd_serve(args)
    run.assert_not_called()


def test_serve_without_token_warns_even_on_loopback(monkeypatch, capsys):
    """Without MNEMA_TOKEN the warning is always printed: a proxy in front of a local bind is
    invisible to the host guard."""
    monkeypatch.delenv("MNEMA_TOKEN", raising=False)

    with patch("uvicorn.run"):
        from mnema.cli.commands import cmd_serve

        cmd_serve(_serve_args("127.0.0.1"))

    assert "MNEMA_TOKEN не задан" in capsys.readouterr().out


# ─────────────────────────── _url_stem: different links → different names ──────────


@pytest.mark.parametrize(
    "url, stem",
    [
        # youtube.com/watch?v=ID: every video has the path "watch", ?v= tells them apart
        ("https://youtube.com/watch?v=AAA", "AAA"),
        ("https://youtube.com/watch?v=BBB", "BBB"),
        ("https://host/talk.mp4", "talk"),  # a file name in the path gives the stem
    ],
)
def test_url_stem_distinguishes_watch_urls_by_video_id(url, stem):
    from mnema.cli.sources import _url_stem

    assert _url_stem(url) == stem


def test_url_stem_hashes_when_no_query_id():
    """A generic path segment without ?v=: a hash of the whole link tells them apart."""
    from mnema.cli.sources import _url_stem

    a = _url_stem("https://example.com/watch")
    b = _url_stem("https://example.com/watch?t=5")
    assert a != b  # different links, different results, no overwrite


def test_transcribe_batch_continues_after_file_error(tmp_path: Path, monkeypatch):
    """One file's error does not stop the batch: it goes to the summary and the next files are
    transcribed."""
    import types

    import mnema.cli.commands as commands
    from mnema.cli.parser import build_parser

    files = []
    for name in ["a.mov", "b.mov", "c.mov"]:
        f = tmp_path / name
        f.write_bytes(b"v")
        files.append(f)

    calls: list[str] = []

    def fake_op_once(op, src, params, *, runner=None, local=False, on=None, cookies=None):
        calls.append(src.stem)
        if src.stem == "b":
            raise commands.mc.MediaError("'NoneType' object has no attribute 'sot_sequence'")
        return {"text": "текст", "segments": [], "language": "ru"}

    monkeypatch.setattr(commands, "_op_once", fake_op_once)
    monkeypatch.setattr(
        commands.mc, "probe", lambda p: types.SimpleNamespace(has_audio=True, duration=10.0)
    )
    monkeypatch.setattr(
        commands, "_run_batch", lambda label, items, **kw: [it(lambda *a: None) for it in items]
    )
    panels: list = []
    monkeypatch.setattr(commands, "done_panel", lambda p, d: panels.append(p))
    printed: list[str] = []
    monkeypatch.setattr(commands._cli.console, "print", lambda *a, **k: printed.append(str(a[0])))

    commands.cmd_transcribe(
        build_parser().parse_args(["transcribe", str(files[0]), str(files[1]), str(files[2])])
    )

    assert calls == ["a", "b", "c"]  # the batch reached the end, it did not stop at "b"
    assert {p.name for p in panels} == {"a.txt", "c.txt"}
    assert (tmp_path / "a.txt").exists() and (tmp_path / "c.txt").exists()
    assert any("sot_sequence" in m for m in printed)  # the error is not lost
    assert any("Не расшифровано: 1 из 3" in m for m in printed)


def test_transcribe_batch_skips_already_done(tmp_path: Path, monkeypatch):
    """A batch rerun skips files that already have a .txt next to them and continues where it
    stopped."""
    import types

    import mnema.cli.commands as commands
    from mnema.cli.parser import build_parser

    done = tmp_path / "a.mov"
    done.write_bytes(b"v")
    (tmp_path / "a.txt").write_text("a\n\nготово\n", encoding="utf-8")
    fresh = tmp_path / "b.mov"
    fresh.write_bytes(b"v")

    calls: list[str] = []

    def fake_op_once(op, src, params, *, runner=None, local=False, on=None, cookies=None):
        calls.append(src.stem)
        return {"text": "текст", "segments": [], "language": "ru"}

    monkeypatch.setattr(commands, "_op_once", fake_op_once)
    monkeypatch.setattr(
        commands.mc, "probe", lambda p: types.SimpleNamespace(has_audio=True, duration=10.0)
    )
    monkeypatch.setattr(
        commands, "_run_batch", lambda label, items, **kw: [it(lambda *a: None) for it in items]
    )
    panels: list[tuple] = []
    monkeypatch.setattr(commands, "done_panel", lambda p, d: panels.append((p, d)))

    commands.cmd_transcribe(build_parser().parse_args(["transcribe", str(done), str(fresh)]))

    assert calls == ["b"]  # the finished "a" was not redone
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "a\n\nготово\n"  # not overwritten
    assert any("пропущено" in d for _p, d in panels)


def test_out_path_batch_output_is_directory(tmp_path: Path, monkeypatch):
    """-o for a batch is the output folder, created, with default file names inside. For one
    input it is the exact file path."""
    import argparse

    from mnema.cli.sources import _out_path

    monkeypatch.chdir(tmp_path)
    a = tmp_path / "a.mov"
    b = tmp_path / "b.mov"
    args = argparse.Namespace(output=str(tmp_path / "streams"))

    out_a = _out_path(a, args, 2, ".mp3")
    out_b = _out_path(b, args, 2, ".mp3")
    assert out_a == tmp_path / "streams" / "a.mp3"
    assert out_b == tmp_path / "streams" / "b.mp3"
    assert (tmp_path / "streams").is_dir()  # the folder is created

    single = _out_path(a, argparse.Namespace(output=str(tmp_path / "точный.mp3")), 1, ".mp3")
    assert single == tmp_path / "точный.mp3"  # one input: the exact path

    # one input, but -o is an existing folder → the file goes inside
    inside = _out_path(a, argparse.Namespace(output=str(tmp_path / "streams")), 1, ".mp3")
    assert inside == tmp_path / "streams" / "a.mp3"


@pytest.mark.parametrize(
    "names, want_video, expected",
    [
        # the common prefix of the inputs, without the version tail
        (
            ["talk_25_june_v1_compressed.mov", "talk_25_june_v2_compressed.mov"],
            True,
            "talk_25_june_merged.mov",
        ),
        (["a.mov", "b.mov"], True, "merged.mov"),  # no common prefix: no doubled "merged"
        # different audio extensions: the first one (.webm from YouTube) would not take aac
        (["a.webm", "b.mp3", "c.aac"], False, "merged.m4a"),
        (["a.mp3", "b.mp3"], False, "merged.mp3"),
        (["a.webm", "b.mov"], True, "merged.webm"),
    ],
)
def test_merged_name_mixed_audio_gets_m4a(names, want_video, expected):
    from mnema.cli.sources import _merged_name

    assert _merged_name([Path(n) for n in names], want_video=want_video) == expected


@pytest.mark.parametrize(
    "jar_spec, url, sent, kept",
    [
        (
            [
                (".youtube.com", "yt_sid", "y1"),
                (".google.com", "g_sid", "g1"),  # youtube's login companion
                (".mybank.example", "bank", "NO"),  # an unrelated domain must not leave
            ],
            "https://www.youtube.com/watch?v=x",
            ["yt_sid", "g_sid"],
            ["mybank", "NO"],
        ),
        (  # for a.co.uk the base is a.co.uk, not the whole co.uk zone
            [(".target.co.uk", "sid", "ok"), (".otherbank.co.uk", "sid", "LEAK")],
            "https://www.target.co.uk/video",
            ["target.co.uk", "ok"],
            ["otherbank", "LEAK"],
        ),
    ],
)
def test_browser_cookies_netscape_filters_by_url_domains(monkeypatch, jar_spec, url, sent, kept):
    """Cookies go to the runner only for the link domains (plus login companions:
    youtube → google), not the whole browser."""
    import sys
    import types
    from http.cookiejar import Cookie

    from mnema.cli.sources import browser_cookies_netscape

    def _cookie(domain, name, value):
        return Cookie(
            0,
            name,
            value,
            None,
            False,
            domain,
            True,
            domain.startswith("."),
            "/",
            True,
            True,
            0,
            False,
            None,
            None,
            {},
        )

    jar = [_cookie(*spec) for spec in jar_spec]
    fake_cookies_mod = types.SimpleNamespace(
        SUPPORTED_BROWSERS=frozenset({"chrome"}),
        extract_cookies_from_browser=lambda name, profile=None: jar,
    )
    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(cookies=fake_cookies_mod))
    monkeypatch.setitem(sys.modules, "yt_dlp.cookies", fake_cookies_mod)

    txt = browser_cookies_netscape("chrome", [url])

    assert all(part in txt for part in sent)
    assert not any(part in txt for part in kept)  # unrelated cookies stay
    assert txt.startswith("# Netscape HTTP Cookie File")


def test_transcribe_concat_remote_urls_single_server_job(tmp_path: Path, monkeypatch):
    """--concat over several links with a runner is one multi-URL recognition job, not a job
    per link."""
    import mnema.cli.commands as commands
    from mnema.cli.parser import build_parser

    monkeypatch.chdir(tmp_path)
    sent: dict = {}

    def fake_run_op(label, op, src, params, *, runner=None, local=False, cookies=None):
        sent.update(op=op, src=src, cookies=cookies)
        return {"text": "весь текст", "segments": [], "language": "ru"}

    monkeypatch.setattr(commands, "_run_op", fake_run_op)
    monkeypatch.setattr(commands, "done_panel", lambda p, d: None)
    monkeypatch.setenv("MNEMA_RUNNER", "http://r")

    urls = ["https://youtu.be/aaa", "https://youtu.be/bbb"]
    commands.cmd_transcribe(build_parser().parse_args(["transcribe", *urls, "--concat"]))

    assert sent["op"] == "transcribe"
    assert sent["src"] == urls  # the links as one input: one join
    out = tmp_path / "aaa+bbb.txt"
    assert out.exists() and "весь текст" in out.read_text(encoding="utf-8")


def test_translate_failure_keeps_transcript(tmp_path: Path, monkeypatch):
    """The LLM is unavailable: --translate fails, but the transcript is already on disk and
    the command does not fail."""
    import mnema.cli.commands as commands
    from mnema.cli.parser import build_parser

    rec = tmp_path / "standup.mov"
    rec.write_bytes(b"v")
    _fake_transcribe_env(monkeypatch, {"text": "hello team", "segments": [], "language": "en"})
    monkeypatch.setattr(
        commands._cli, "run_with_progress", lambda label, fn, **kw: fn(on=lambda *a: None)
    )

    def boom(*a, **kw):
        raise RuntimeError("LLM недоступен")

    monkeypatch.setattr(commands, "_op_once", boom)

    commands.cmd_transcribe(
        build_parser().parse_args(["transcribe", str(rec), "--translate", "--local"])
    )  # does not raise

    assert (tmp_path / "standup.txt").exists()  # the transcript is intact
    assert not (tmp_path / "standup.ru.md").exists()  # no translation, which is fine


def test_download_cache_requires_done_mark(tmp_path: Path, monkeypatch):
    """A cache without the completion marker is not a cache: an interrupted download (yt-dlp
    leaves an intermediate file without .part) never blocks downloading again."""
    import mnema.cli.sources as sources
    from mnema.cli.sources import _resolve_sources

    monkeypatch.setattr(sources.tempfile, "gettempdir", lambda: str(tmp_path / "cache"))
    calls = {"n": 0}

    def fake_download(
        url, *, want_video, dest_dir, cookies_browser=None, cookies_file=None, on=None
    ):
        calls["n"] += 1
        f = Path(dest_dir) / "clip.m4a"
        f.write_bytes(b"full")
        return f

    monkeypatch.setattr(sources.mc, "download_url", fake_download)
    url = "https://x.test/v/9"

    # an "interrupted" download: a file in the cache folder, no marker
    d = sources._url_cache_dir(url, False)
    (d / "clip.m4a").write_bytes(b"partial")

    _resolve_sources([url], want_video=False)
    assert calls["n"] == 1  # downloaded again, the leftover not taken

    _resolve_sources([url], want_video=False)
    assert calls["n"] == 1  # now the marker is there: from the cache


def test_mp3_concat_urls_with_runner_dies(tmp_path: Path, monkeypatch):
    """mp3 --concat over links with a runner is an error, not N files in silence."""
    import mnema.cli.commands as commands
    from mnema.cli.parser import build_parser

    monkeypatch.setenv("MNEMA_RUNNER", "http://127.0.0.1:1")
    args = build_parser().parse_args(
        ["mp3", "https://youtu.be/a", "https://youtu.be/b", "--concat"]
    )
    with pytest.raises(SystemExit):
        commands.cmd_mp3(args)


def test_translate_flag_does_not_leak_whisper_model(tmp_path: Path, monkeypatch):
    """transcribe --translate --model large-v3: --model is the recognition model and must not
    reach the LLM translation ("unknown model large-v3")."""
    monkeypatch.delenv("MNEMA_RUNNER", raising=False)
    monkeypatch.chdir(tmp_path)

    from mnema.cli import commands as cmds

    seen = {}

    def fake_translate(op, path, params, **kwargs):
        seen.update(params)
        return {"markdown": "перевод"}

    monkeypatch.setattr(cmds, "_op_once", fake_translate)
    monkeypatch.setattr(
        "mnema.cli.run_with_progress", lambda label, fn, **kw: fn(on=lambda *a: None)
    )

    out = tmp_path / "meeting.txt"
    args = _make_args(input=str(out), runner=None, translate=True, model="large-v3")
    result = cmds._maybe_translate(args, out, "текст встречи", title=None)

    assert "model" not in seen  # translation uses the server's active model
    assert result is None or result  # the translation went through


def test_merge_with_downloaded_inputs_writes_to_cwd(tmp_path: Path, monkeypatch):
    """merge over links: the inputs are in the download cache, so the result goes to the
    current folder, not the cache."""
    import mnema.cli.sources as sources_mod
    from mnema.cli import commands as cmds

    monkeypatch.chdir(tmp_path)
    cache = tmp_path / "fake-dl-cache"
    cache.mkdir()
    a, b = cache / "mic.wav", cache / "system.wav"
    a.write_bytes(b"wav")
    b.write_bytes(b"wav")
    monkeypatch.setattr(sources_mod, "_DOWNLOADED", {a, b})
    monkeypatch.setattr(cmds, "_sources", lambda *ar, **kw: [a, b])

    mixed = {}
    monkeypatch.setattr(cmds.mc, "mix", lambda files, out, on=None: mixed.update(out=out) or out)
    monkeypatch.setattr(
        "mnema.cli.run_with_progress", lambda label, fn, **kw: fn(on=lambda *a: None)
    )
    monkeypatch.setattr(cmds, "done_panel", lambda *a, **k: None)

    import argparse

    args = argparse.Namespace(output=None, inputs=["url1", "url2"], runner=None, local=False)
    (tmp_path / "mic+system.m4a").write_bytes(b"")  # done_panel is mocked; the file is harmless
    cmds.cmd_merge(args)

    assert mixed["out"].parent == tmp_path  # cwd, not the cache
    assert mixed["out"].parent != cache


def test_transcribe_merge_single_track_is_valid(tmp_path: Path, monkeypatch):
    """--merge with one track (a rec session without system audio) is a valid input: the same
    pipeline without interleaving, not a "give at least two" error."""
    import types

    import mnema.cli.commands as commands
    from mnema.cli.parser import build_parser

    mic = tmp_path / "mic.wav"
    mic.write_bytes(b"m")
    monkeypatch.setattr(commands, "_dispatch_sources", lambda *a, **k: [mic])
    monkeypatch.setattr(commands.mc, "probe", lambda p: types.SimpleNamespace(has_audio=True))

    def fake_op_once(op, src, params, *, runner=None, local=False, on=None, cookies=None):
        return {
            "text": "привет",
            "segments": [{"start": 0.0, "end": 1.0, "text": "привет"}],
            "language": "ru",
        }

    monkeypatch.setattr(commands, "_op_once", fake_op_once)
    monkeypatch.setattr(
        commands, "_run_batch", lambda label, items, **kw: [it(lambda *a: None) for it in items]
    )
    monkeypatch.setattr(commands, "done_panel", lambda p, d: None)

    commands.cmd_transcribe(
        build_parser().parse_args(["transcribe", str(mic), "--merge", "--lang", "ru"])
    )

    lines = (tmp_path / "mic.txt").read_text(encoding="utf-8").splitlines()
    assert [ln for ln in lines if ln.strip()] == ["mic", "mic: привет"]


@pytest.mark.parametrize("clipboard_ok", [True, False])
@pytest.mark.parametrize("command", ["transcribe", "translate"])
def test_batch_no_save_delivers_one_combined_result(
    tmp_path, monkeypatch, capsys, clipboard_ok, command
):
    import mnema.cli.commands as commands
    from mnema.cli.parser import build_parser

    sources = [tmp_path / "second.wav", tmp_path / "first.wav"]
    for source in sources:
        source.write_bytes(b"synthetic")
    copied = []
    monkeypatch.setattr(
        "mnema.live.clipboard.copy_to_clipboard", lambda text: copied.append(text) or clipboard_ok
    )

    def op(name, src, *a, **kw):
        return {"text": f"RESULT_{src.stem}", "segments": [], "markdown": f"RESULT_{src.stem}"}

    monkeypatch.setattr(commands, "_op_once", op)
    monkeypatch.setattr(commands, "_run_op", lambda label, name, src, *a, **kw: op(name, src))
    args = build_parser().parse_args(
        [command, *map(str, sources), "--runner", "http://srv", "--no-save"]
    )
    getattr(commands, f"cmd_{command}")(args)
    out = capsys.readouterr().out
    assert len(copied) == 1
    assert [line for line in copied[0].splitlines() if line.startswith("RESULT_")] == [
        f"RESULT_{source.stem}" for source in sources
    ]
    assert set(tmp_path.iterdir()) == set(sources)
    assert all(source.read_bytes() == b"synthetic" for source in sources)
    if not clipboard_ok:
        assert "RESULT_first" in out and "RESULT_second" in out


def test_batch_no_save_partial_failure_prints_available_text(tmp_path, monkeypatch, capsys):
    import mnema.cli.commands as commands
    from mnema.cli.parser import build_parser
    from mnema.core.base import MediaError

    sources = [tmp_path / "first.wav", tmp_path / "second.wav"]
    for source in sources:
        source.write_bytes(b"synthetic")
    monkeypatch.setattr(
        "mnema.live.clipboard.copy_to_clipboard", lambda text: pytest.fail("неполный пакет")
    )

    def op(name, src, *a, **kw):
        if src == sources[1]:
            raise MediaError("ASR unavailable")
        return {"text": "AVAILABLE_FIRST", "segments": []}

    monkeypatch.setattr(commands, "_op_once", op)
    args = build_parser().parse_args(
        ["transcribe", *map(str, sources), "--runner", "http://srv", "--no-save"]
    )
    with pytest.raises(MediaError, match="Не расшифровано: 1 из 2"):
        commands.cmd_transcribe(args)
    out = capsys.readouterr().out
    assert "AVAILABLE_FIRST" in out and "ASR unavailable" in out
    assert set(tmp_path.iterdir()) == set(sources)
    assert all(source.read_bytes() == b"synthetic" for source in sources)


# ─────────────────────────── interface language ───────────────────────────────

_CYRILLIC = re.compile("[А-Яа-яЁё]")


def _parsers(parser: argparse.ArgumentParser):
    """The parser and every subparser below it."""
    yield parser
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                yield from _parsers(sub)


def test_help_of_every_command_is_english_without_cyrillic() -> None:
    """The CLI help is built in the process language: English has no Russian left and no
    stray % that argparse would choke on."""
    from mnema import i18n
    from mnema.cli.parser import build_parser

    i18n.set_language("en")
    helps = [p.format_help() for p in _parsers(build_parser())]
    assert len(helps) > 10
    assert not [h for h in helps if _CYRILLIC.search(h)]
    root = helps[0]
    assert "Process meeting recordings." in root and "transcribe to text" in root


def test_help_stays_russian_in_russian() -> None:
    from mnema.cli.parser import build_parser

    help_text = build_parser().format_help()
    assert "Обработка записей встреч." in help_text and "расшифровать в текст" in help_text


def test_result_lines_are_english(monkeypatch) -> None:
    import io

    from rich.console import Console

    import mnema.cli.ui as ui
    from mnema import i18n
    from mnema.core.base import fmt_size
    from mnema.core.probe import MediaInfo

    i18n.set_language("en")
    buf = io.StringIO()
    monkeypatch.setattr(ui, "console", Console(file=buf, width=100))
    ui.banner()
    ui.done_panel(Path("/tmp/talk.txt"), "3 words")
    ui.console.print(
        ui.info_panel(
            MediaInfo(
                path=Path("talk.mp4"),
                duration=10.0,
                size=3,
                audio_codec="aac",
                video_codec="h264",
                width=640,
                height=480,
            )
        )
    )
    with pytest.raises(SystemExit):
        ui.die("broken")
    out = buf.getvalue()
    assert "✓ done" in out and "Error: broken" in out and "duration" in out
    assert not _CYRILLIC.search(out.replace(fmt_size(3), ""))  # sizes are the core's texts


def test_word_counts_follow_the_language() -> None:
    from mnema import i18n
    from mnema.cli import commands

    assert commands._words_lang("раз", "ru") == "1 слово · язык: ru"
    assert commands._words_lang("раз два", "ru") == "2 слова · язык: ru"
    assert commands._words_lang("раз два три четыре пять", None) == "5 слов · язык: —"
    i18n.set_language("en")
    assert commands._words_lang("one", "en") == "1 word · language: en"
    assert commands._words_to("one two", "ru") == "2 words · ru"


def test_batch_result_is_english(tmp_path, monkeypatch, capsys) -> None:
    import mnema.cli.commands as commands
    from mnema import i18n
    from mnema.cli.parser import build_parser

    i18n.set_language("en")
    sources = [tmp_path / "first.wav", tmp_path / "second.wav"]
    for source in sources:
        source.write_bytes(b"synthetic")
    monkeypatch.setattr("mnema.live.clipboard.copy_to_clipboard", lambda text: True)
    monkeypatch.setattr(
        commands, "_op_once", lambda name, src, *a, **kw: {"text": "hi", "segments": []}
    )
    args = build_parser().parse_args(
        ["transcribe", *map(str, sources), "--runner", "http://srv", "--no-save"]
    )
    commands.cmd_transcribe(args)
    out = capsys.readouterr()
    assert "📋 in the clipboard · 2 files" in out.out
    assert not _CYRILLIC.search(out.out + out.err)
