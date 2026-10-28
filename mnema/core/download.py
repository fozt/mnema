"""Media download by link with yt-dlp (audio or video, as the job needs)."""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import shutil
import socket
import sysconfig
import tempfile
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import urlparse

from ..i18n import _
from .base import MediaError, Progress, noop, tool

logger = logging.getLogger(__name__)

# Downloads force IPv4: media CDNs are always reachable over IPv4, while a broken IPv6 route
# (Tailscale-only Mac, dual-stack server with dead v6) stalls connects for tens of seconds.
# For IPv6-only hosts set MNEMA_DOWNLOAD_SOURCE_ADDR=:: (or empty to not force a family).
# yt-dlp: source_address='0.0.0.0' is --force-ipv4.
_DOWNLOAD_SOURCE_ADDR = os.environ.get("MNEMA_DOWNLOAD_SOURCE_ADDR", "0.0.0.0")


def is_url(s: str) -> bool:
    """Whether this looks like an http(s) link rather than a local path."""
    return s.strip().lower().startswith(("http://", "https://"))


_SHELL_ESCAPE = re.compile(r"\\([?=&#])")


def clean_url(url: str) -> str:
    """Undo shell escaping: "watch\\?v\\=ID" → "watch?v=ID" (URLs have no backslashes)."""
    return _SHELL_ESCAPE.sub(r"\1", url)


DENO_HINT = "YouTube needs deno: reinstall Mnema (./install.sh client or ./install.sh server)"
BOT_HINT = "YouTube asks to sign in: add --cookies FILE or --cookies-from-browser chrome"
LOGIN_HINT = (
    "The content seems to need a login: add --cookies FILE or --cookies-from-browser chrome"
)
# how yt-dlp reports that nothing can solve YouTube's JS challenges
_JS_SIGNS = ("javascript runtime", "n challenge", "signature solving")


def deno_path() -> str | None:
    """deno for YouTube: from the Mnema environment (deno package), then PATH and ~/.deno/bin."""
    exe = "deno" + (sysconfig.get_config_var("EXE") or "")
    for candidate in (
        os.path.join(sysconfig.get_path("scripts"), exe),
        shutil.which(exe),
        os.path.join(Path.home(), ".deno", "bin", exe),
    ):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


class _YtdlpLog:
    """yt-dlp output: nothing is printed; warnings and errors explain a failure."""

    def __init__(self) -> None:
        self.problems: list[str] = []

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        self._keep(msg)

    def error(self, msg: str) -> None:
        self._keep(msg.removeprefix("ERROR: "))

    def _keep(self, msg: str) -> None:
        logger.debug("yt-dlp: %s", msg)
        self.problems.append(msg)


def _hints(text: str, *, deno: str | None, cookies: bool) -> list[str]:
    """Hints for a yt-dlp error: what this machine or request is missing."""
    low = text.lower()
    hints = []
    if deno is None and any(sign in low for sign in _JS_SIGNS):
        hints.append(_(DENO_HINT))
    if not cookies and "not a bot" in low:
        hints.append(_(BOT_HINT))
    elif not cookies and "cookie" in low:
        hints.append(_(LOGIN_HINT))
    return hints


def validate_url(url: str | None) -> None:
    """SSRF guard: only http/https to a host that resolves to a public address.

    The runner downloads on the server's behalf; without this a user could point it at the
    internal network or cloud metadata (loopback, 169.254.169.254) or file://.

    Limitation: a pre-check of the host's resolution. yt-dlp resolves DNS again and follows
    redirects, so DNS rebinding and 3xx to a private address are not covered. Requires a
    token holder, not an anonymous caller.
    """
    if not url:
        raise MediaError(_("Empty download link"))
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise MediaError(_("Unsupported link scheme: {scheme}", scheme=repr(parsed.scheme or url)))
    host = parsed.hostname
    if not host:
        raise MediaError(_("Could not parse the link's host: {url}", url=url))
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as e:
        raise MediaError(_("Could not resolve host {host}: {error}", host=host, error=e)) from e
    for info in infos:
        raw_addr = info[4][0]
        ip = ipaddress.ip_address(str(raw_addr).split("%")[0])  # drop the IPv6 link-local zone id
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise MediaError(
                _(
                    "The link points to a forbidden address: {host} → {address}",
                    host=host,
                    address=raw_addr,
                )
            )


def fetch(
    url: str,
    *,
    want_video: bool,
    dest_dir: Path,
    cookies_browser: str | None = None,
    cookies_file: Path | str | None = None,
    on: Progress = noop,
) -> Path:
    """Download a link into dest_dir and return the file path.

    want_video=False downloads audio only (small, for transcripts and summaries); True — video
    in the best quality (for compression and merging).
    cookies_browser takes the cookies of a signed-in browser on THIS machine (`chrome`,
    `safari`, `firefox`, or `chrome:Profile 1`) for content behind a login. An explicit CLI
    opt-in; cookies are never touched otherwise.
    cookies_file is a Netscape cookies.txt on this machine; yt-dlp gets a copy because it
    rewrites the file on exit.
    """
    cookies_src = Path(cookies_file).expanduser() if cookies_file else None
    if cookies_src is not None and not cookies_src.is_file():
        raise MediaError(_("Cookies file not found: {path}", path=cookies_src))
    url = clean_url(url)
    validate_url(url)
    try:
        import yt_dlp
    except ImportError as e:
        raise MediaError(
            _("Links need yt-dlp: reinstall Mnema (./install.sh client or ./install.sh server)")
        ) from e

    def hook(d: dict) -> None:
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes")
            idict = d.get("info_dict") or {}
            if idict.get("vcodec") not in (None, "none"):
                message = _("Downloading video…")
            elif idict.get("acodec") not in (None, "none"):
                message = _("Downloading audio…")
            else:
                message = _("Downloading the file…")
            on(done / total if total and done else None, message)

    log = _YtdlpLog()
    deno = deno_path()
    opts = {
        "format": "bv*+ba/b" if want_video else "ba/b",
        "outtmpl": str(dest_dir / "%(title).150B [%(id)s].%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "color": "no_color",
        "logger": log,
        "progress_hooks": [hook],
        # YouTube (EJS) serves formats only through a JS runtime: deno at the found path,
        # node and bun from PATH.
        "js_runtimes": {"deno": {"path": deno} if deno else {}, "node": {}, "bun": {}},
    }
    if _DOWNLOAD_SOURCE_ADDR:  # IPv4 by default, see _DOWNLOAD_SOURCE_ADDR
        opts["source_address"] = _DOWNLOAD_SOURCE_ADDR
    try:
        opts["ffmpeg_location"] = tool("ffmpeg")  # ffprobe is taken from the same folder
    except MediaError:
        pass  # a single format downloads without ffmpeg
    if cookies_browser:
        from yt_dlp.cookies import SUPPORTED_BROWSERS

        name, _sep, profile = cookies_browser.partition(":")
        name, profile = name.strip().lower(), profile.strip()
        if name not in SUPPORTED_BROWSERS:
            # fail fast with the list (yt-dlp cannot read Arc, for one)
            raise MediaError(
                _(
                    "yt-dlp does not support the browser “{name}”. Available: {browsers}",
                    name=name,
                    browsers=", ".join(sorted(SUPPORTED_BROWSERS)),
                )
            )
        opts["cookiesfrombrowser"] = (name, profile) if profile else (name,)
    on(None, _("Downloading from the link…"))
    with_cookies = bool(cookies_browser or cookies_src)
    try:
        with ExitStack() as stack:
            if cookies_src is not None:
                folder = stack.enter_context(tempfile.TemporaryDirectory(prefix="mnema-cookies-"))
                copy = Path(folder) / "cookies.txt"
                copy.touch(mode=0o600)
                copy.write_bytes(cookies_src.read_bytes())
                opts["cookiefile"] = str(copy)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True) or {}
                reqs = info.get("requested_downloads") or []
                if reqs and reqs[0].get("filepath"):
                    path = Path(reqs[0]["filepath"])  # final path after post-processing
                else:
                    path = Path(ydl.prepare_filename(info)) if info else None
    except MediaError:
        raise
    except Exception as e:
        reason = str(e).removeprefix("ERROR: ")
        hints = _hints("\n".join([*log.problems, reason]), deno=deno, cookies=with_cookies)
        raise MediaError(
            "\n".join([_("Could not download {url}: {reason}", url=url, reason=reason), *hints])
        ) from e
    if path is None or not path.exists():
        reason = log.problems[-1] if log.problems else ""
        hints = _hints("\n".join(log.problems), deno=deno, cookies=with_cookies)
        lines = [
            _("The download produced no file: {url}", url=url),
            *([f"yt-dlp: {reason}"] if reason else []),
        ]
        raise MediaError("\n".join([*lines, *hints]))
    return path
