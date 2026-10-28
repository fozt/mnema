"""validate_url SSRF guard, browser cookies for links behind a login, yt-dlp failure hints."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from mnema.core import download
from mnema.core.base import MediaError
from mnema.i18n import _, using


def _fake_getaddrinfo(addr: str):
    def _impl(host, port, *a, **kw):
        return [(None, None, None, None, (addr, 0))]

    return _impl


def test_rejects_non_http_scheme() -> None:
    with pytest.raises(MediaError):
        download.validate_url("file:///etc/passwd")


@pytest.mark.parametrize(
    ("addr", "url"),
    [
        ("127.0.0.1", "https://localhost/video"),
        ("10.0.0.5", "https://internal.example.com/video"),
        ("169.254.169.254", "http://metadata.internal/latest/meta-data"),  # cloud metadata
    ],
)
def test_rejects_loopback_ip(monkeypatch: pytest.MonkeyPatch, addr: str, url: str) -> None:
    monkeypatch.setattr(download.socket, "getaddrinfo", _fake_getaddrinfo(addr))
    with pytest.raises(MediaError):
        download.validate_url(url)


def test_allows_public_https(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(download.socket, "getaddrinfo", _fake_getaddrinfo("142.250.180.14"))
    download.validate_url("https://www.youtube.com/watch?v=abc")  # does not raise


def test_rejects_unresolvable_host(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(host, port, *a, **kw):
        raise OSError("name or service not known")

    monkeypatch.setattr(download.socket, "getaddrinfo", _raise)
    with pytest.raises(MediaError):
        download.validate_url("https://does-not-resolve.invalid/x")


# ── browser cookies (--cookies-from-browser: content behind a login) ──


def _fake_yt_dlp(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    error: str | None = None,
    warnings: tuple[str, ...] = (),
    no_file: bool = False,
    urls: list[str] | None = None,
) -> list[dict]:
    """A fake yt_dlp (fetch imports it lazily): records opts and returns a ready file.

    Like the real one, it sends warnings to the logger and rewrites cookiefile on exit."""
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"v")
    captured: list[dict] = []

    class FakeYDL:
        def __init__(self, opts: dict) -> None:
            captured.append(opts)
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a: object) -> bool:
            if self.opts.get("cookiefile"):
                Path(self.opts["cookiefile"]).write_text("# rewritten by yt-dlp\n")
            return False

        def extract_info(self, url: str, download: bool) -> dict:
            if urls is not None:
                urls.append(url)
            if self.opts.get("cookiefile"):
                self.opts["cookies_seen"] = Path(self.opts["cookiefile"]).read_text()
            for message in warnings:
                self.opts["logger"].warning(message)
            if error is not None:
                raise RuntimeError(error)
            path = tmp_path / "missing.m4a" if no_file else media
            return {"requested_downloads": [{"filepath": str(path)}]}

    fake_cookies = types.SimpleNamespace(
        SUPPORTED_BROWSERS=frozenset({"chrome", "safari", "firefox"})
    )
    fake = types.SimpleNamespace(YoutubeDL=FakeYDL, cookies=fake_cookies)
    monkeypatch.setitem(sys.modules, "yt_dlp", fake)
    monkeypatch.setitem(sys.modules, "yt_dlp.cookies", fake_cookies)
    monkeypatch.setattr(download.socket, "getaddrinfo", _fake_getaddrinfo("142.250.180.14"))
    return captured


def test_fetch_without_cookies_flag_leaves_browser_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Browser cookies are NOT touched by default (the flag is an explicit opt-in)."""
    opts = _fake_yt_dlp(monkeypatch, tmp_path)
    download.fetch("https://www.instagram.com/reel/abc/", want_video=False, dest_dir=tmp_path)
    assert "cookiesfrombrowser" not in opts[0]


@pytest.mark.parametrize(
    ("browser", "expected"),
    [("chrome", ("chrome",)), ("chrome:Profile 1", ("chrome", "Profile 1"))],
)
def test_fetch_passes_browser_cookies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, browser: str, expected: tuple
) -> None:
    opts = _fake_yt_dlp(monkeypatch, tmp_path)
    download.fetch(
        "https://www.instagram.com/stories/u/1/",
        want_video=False,
        dest_dir=tmp_path,
        cookies_browser=browser,
    )
    assert opts[0]["cookiesfrombrowser"] == expected


@pytest.mark.parametrize("browser", [None, "chrome"])
def test_fetch_login_error_hints_cookies_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, browser: str | None
) -> None:
    """A yt-dlp cookie error without the flag → a --cookies-from-browser hint; with the flag
    already set the hint is not repeated (it would not help)."""
    _fake_yt_dlp(monkeypatch, tmp_path, error="empty media response ... use --cookies")
    with pytest.raises(MediaError) as exc:
        download.fetch(
            "https://www.instagram.com/reel/abc/",
            want_video=False,
            dest_dir=tmp_path,
            cookies_browser=browser,
        )
    assert ("--cookies-from-browser" in str(exc.value)) == (browser is None)


def test_fetch_rejects_unsupported_browser(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An unsupported browser (arc has its own keychain yt-dlp cannot read) fails fast with
    the supported list instead of an obscure yt-dlp error."""
    _fake_yt_dlp(monkeypatch, tmp_path)
    with pytest.raises(MediaError, match="arc.*не поддерживает|не поддерживает.*arc"):
        download.fetch(
            "https://www.instagram.com/reel/abc/",
            want_video=False,
            dest_dir=tmp_path,
            cookies_browser="arc",
        )


@pytest.mark.parametrize(
    ("want_video", "expected"),
    [
        (True, ["v2160", "audio"]),
        (False, ["audio"]),
    ],
)
def test_fetch_selects_best_video_and_keeps_audio(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    want_video: bool,
    expected: list[str],
) -> None:
    """Checked against real yt-dlp: video is the best available together with an audio track."""
    from yt_dlp import YoutubeDL

    opts = _fake_yt_dlp(monkeypatch, tmp_path)
    download.fetch("https://youtu.be/abc", want_video=want_video, dest_dir=tmp_path)
    formats = [
        {"format_id": "audio", "vcodec": "none", "acodec": "aac", "ext": "m4a"},
        *[
            {
                "format_id": f"v{height}",
                "height": height,
                "vcodec": "avc1",
                "acodec": "none",
                "ext": "mp4",
            }
            for height in (480, 720, 2160)
        ],
    ]
    for fmt in formats:
        fmt.update(url="https://example.com/media", protocol="https")
    with YoutubeDL({"quiet": True}) as ydl:
        selector = ydl.build_format_selector(opts[0]["format"])
        selected = ydl._select_formats(formats, selector)
    tracks = selected[0].get("requested_formats", selected)
    assert [fmt["format_id"] for fmt in tracks] == expected


# ── cookies file, deno, escaped links, failure reasons ──

_JS_WARNING = (
    "[youtube] abc: n challenge solving failed: Some formats may be missing. "
    "Ensure you have a supported JavaScript runtime and challenge solver script distribution"
)
_BOT_ERROR = (
    "ERROR: [youtube] abc: Sign in to confirm you\u2019re not a bot. "
    "Use --cookies-from-browser or --cookies for the authentication."
)


@pytest.fixture
def no_deno(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> types.SimpleNamespace:
    """A machine without deno: empty Python scripts folder, PATH and home folder."""
    scripts = tmp_path / "venv-bin"
    home = tmp_path / "home"
    scripts.mkdir()
    home.mkdir()
    monkeypatch.setattr(download.sysconfig, "get_path", lambda name, *a, **k: str(scripts))
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("HOME", str(home))
    return types.SimpleNamespace(scripts=scripts, home=home)


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def test_fetch_finds_deno_in_home_install(
    no_deno: types.SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The stock deno install (~/.deno/bin) is off PATH, so its path goes to yt-dlp explicitly."""
    deno = _executable(no_deno.home / ".deno" / "bin" / "deno")
    opts = _fake_yt_dlp(monkeypatch, tmp_path)
    download.fetch("https://youtu.be/abc", want_video=False, dest_dir=tmp_path)
    assert opts[0]["js_runtimes"]["deno"] == {"path": str(deno)}


def test_fetch_prefers_deno_installed_with_mnema(
    no_deno: types.SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    own = _executable(no_deno.scripts / "deno")
    _executable(no_deno.home / ".deno" / "bin" / "deno")
    opts = _fake_yt_dlp(monkeypatch, tmp_path)
    download.fetch("https://youtu.be/abc", want_video=False, dest_dir=tmp_path)
    assert opts[0]["js_runtimes"]["deno"] == {"path": str(own)}


@pytest.mark.parametrize("has_deno", [False, True])
def test_fetch_without_js_runtime_names_deno_once(
    no_deno: types.SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, has_deno: bool
) -> None:
    if has_deno:
        _executable(no_deno.scripts / "deno")
    _fake_yt_dlp(
        monkeypatch,
        tmp_path,
        warnings=(_JS_WARNING,),
        error="ERROR: [youtube] abc: Requested format is not available",
    )
    with pytest.raises(MediaError) as exc:
        download.fetch("https://youtu.be/abc", want_video=False, dest_dir=tmp_path)
    lines = str(exc.value).splitlines()
    hints = 0 if has_deno else 1
    assert lines.count(_(download.DENO_HINT)) == hints
    assert sum("deno" in line for line in lines) == hints


@pytest.mark.parametrize("browser", [None, "chrome"])
def test_fetch_bot_check_suggests_cookies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, browser: str | None
) -> None:
    _fake_yt_dlp(monkeypatch, tmp_path, error=_BOT_ERROR)
    with pytest.raises(MediaError) as exc:
        download.fetch(
            "https://youtu.be/abc", want_video=False, dest_dir=tmp_path, cookies_browser=browser
        )
    lines = str(exc.value).splitlines()
    assert lines[0].startswith("Не удалось скачать https://youtu.be/abc: [youtube] abc: Sign in")
    assert (_(download.BOT_HINT) in lines) == (browser is None)
    assert _(download.LOGIN_HINT) not in lines


def test_fetch_failure_reads_in_english(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _fake_yt_dlp(monkeypatch, tmp_path, error=_BOT_ERROR)
    with using("en"), pytest.raises(MediaError) as exc:
        download.fetch("https://youtu.be/abc", want_video=False, dest_dir=tmp_path)
    lines = str(exc.value).splitlines()
    assert lines[0].startswith("Could not download https://youtu.be/abc: [youtube] abc: Sign in")
    assert download.BOT_HINT in lines
    assert not any("\u0400" <= ch <= "\u04ff" for ch in str(exc.value))


def test_fetch_without_file_reports_ytdlp_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_yt_dlp(
        monkeypatch, tmp_path, warnings=("Requested format is not available",), no_file=True
    )
    with pytest.raises(MediaError) as exc:
        download.fetch("https://youtu.be/abc", want_video=False, dest_dir=tmp_path)
    lines = str(exc.value).splitlines()
    assert lines[0] == "Скачивание не дало файла: https://youtu.be/abc"
    assert "yt-dlp: Requested format is not available" in lines


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        (r"https://www.youtube.com/watch\?v\=abc", "https://www.youtube.com/watch?v=abc"),
        (r"https://x.test/a\?b\=1\&c\=2\#t", "https://x.test/a?b=1&c=2#t"),
        ("https://youtu.be/abc?t=5", "https://youtu.be/abc?t=5"),
    ],
)
def test_fetch_drops_shell_escapes_from_link(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw: str, clean: str
) -> None:
    urls: list[str] = []
    _fake_yt_dlp(monkeypatch, tmp_path, urls=urls)
    download.fetch(raw, want_video=False, dest_dir=tmp_path)
    assert urls == [clean]


def test_fetch_cookies_file_stays_untouched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """yt-dlp gets a copy of cookies.txt: it rewrites its copy on exit, the owner's file stays."""
    owner = tmp_path / "cookies.txt"
    owner.write_text("# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tx\n")
    before = owner.read_text()
    opts = _fake_yt_dlp(monkeypatch, tmp_path)
    download.fetch("https://youtu.be/abc", want_video=False, dest_dir=tmp_path, cookies_file=owner)
    copy = Path(opts[0]["cookiefile"])
    assert opts[0]["cookies_seen"] == before
    assert copy != owner and not copy.exists()
    assert owner.read_text() == before


def test_fetch_missing_cookies_file_fails_before_download(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    opts = _fake_yt_dlp(monkeypatch, tmp_path)
    with pytest.raises(MediaError, match="Нет файла cookies"):
        download.fetch(
            "https://youtu.be/abc",
            want_video=False,
            dest_dir=tmp_path,
            cookies_file=tmp_path / "nope.txt",
        )
    assert not opts


def test_fetch_gives_yt_dlp_the_bundled_ffmpeg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """yt-dlp merges video and audio with the same ffmpeg as Mnema, not one from PATH."""
    bundled = tmp_path / "Mnema.app" / "Contents" / "Resources" / "bin" / "ffmpeg"
    bundled.parent.mkdir(parents=True)
    bundled.write_text("#!/bin/sh\n")
    bundled.chmod(0o755)
    monkeypatch.delenv("MNEMA_FFMPEG")
    monkeypatch.setenv("MNEMA_APP_BUNDLE", str(tmp_path / "Mnema.app"))
    opts = _fake_yt_dlp(monkeypatch, tmp_path)
    download.fetch("https://youtu.be/abc", want_video=True, dest_dir=tmp_path)
    assert opts[0]["ffmpeg_location"] == str(bundled)
