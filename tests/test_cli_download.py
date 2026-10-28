"""Links in the CLI: the cookies file, flags without abbreviations, escaped links."""

from __future__ import annotations

import socket
import tempfile
import types
from pathlib import Path

import pytest
import yt_dlp

from mnema.cli.parser import build_parser
from mnema.cli.sources import _dispatch_sources

_COOKIES = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsecret\n"


@pytest.fixture
def ytdlp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[dict]:
    """External boundary: yt-dlp writes the file and rewrites cookiefile on exit."""
    calls: list[dict] = []

    class Downloader:
        def __init__(self, params: dict) -> None:
            self.params = params

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            if self.params.get("cookiefile"):
                Path(self.params["cookiefile"]).write_text("# rewritten by yt-dlp\n")

        def extract_info(self, url: str, download: bool = True) -> dict:
            cookie = self.params.get("cookiefile")
            calls.append(
                {"url": url, "cookiefile": cookie, "cookies": cookie and Path(cookie).read_text()}
            )
            path = Path(self.params["outtmpl"]).parent / "clip.m4a"
            path.write_bytes(b"a")
            return {"requested_downloads": [{"filepath": str(path)}]}

    monkeypatch.setattr(yt_dlp, "YoutubeDL", Downloader)
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))]
    )
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    return calls


@pytest.mark.parametrize(
    "argv",
    [
        ["transcribe", "x.mov", "--cookies-from", "chrome"],
        ["mp3", "x.mov", "--bit", "64k"],
        ["transcribe", "x.mov", "--cookies", "c.txt", "--cookies-from-browser", "chrome"],
    ],
)
def test_flag_typos_and_conflicts_are_errors(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(argv)


def test_cookies_file_reaches_ytdlp_and_stays_untouched(ytdlp: list[dict], tmp_path: Path) -> None:
    owner = tmp_path / "cookies.txt"
    owner.write_text(_COOKIES)
    ns = build_parser().parse_args(["transcribe", "https://youtu.be/abc", "--cookies", str(owner)])
    [media] = _dispatch_sources(ns, want_video=False, runner=None, local=True)
    assert isinstance(media, Path) and media.exists()
    assert ytdlp[0]["cookies"] == _COOKIES
    assert Path(ytdlp[0]["cookiefile"]) != owner
    assert owner.read_text() == _COOKIES


@pytest.mark.parametrize(
    "raw",
    [r"https://www.youtube.com/watch\?v\=abc", r'"https://www.youtube.com/watch\?v\=abc"'],
)
def test_shell_escaped_link_is_cleaned(ytdlp: list[dict], raw: str) -> None:
    clean = "https://www.youtube.com/watch?v=abc"
    ns = build_parser().parse_args(["transcribe", raw])
    assert _dispatch_sources(ns, want_video=False, runner="http://r") == [clean]
    _dispatch_sources(ns, want_video=False, runner=None, local=True)
    assert [call["url"] for call in ytdlp] == [clean]


def test_runner_gets_cookies_file_text_from_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a runner the client downloads the link: the cookies text goes to that download."""
    import mnema.cli.commands as commands

    owner = tmp_path / "cookies.txt"
    owner.write_text(_COOKIES)
    calls: list[tuple[object, str | None]] = []

    def run_op(label, op, src, params, **kw):
        calls.append((src, kw.get("cookies")))
        return {"text": "t", "segments": []}

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(commands, "_run_op", run_op)
    monkeypatch.setattr(
        commands.mc, "probe", lambda p: types.SimpleNamespace(has_audio=True, duration=1.0)
    )
    monkeypatch.setattr(commands, "info_panel", lambda *a, **k: "")
    monkeypatch.setattr(commands, "done_panel", lambda p, d: None)
    url = "https://youtu.be/abc"
    ns = build_parser().parse_args(
        ["transcribe", url, "--cookies", str(owner), "--runner", "http://r"]
    )
    commands.cmd_transcribe(ns)
    assert calls == [(url, _COOKIES)]
