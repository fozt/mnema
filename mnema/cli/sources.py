"""Inputs (files, folders, links) and output paths."""

from __future__ import annotations

import atexit
import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from rich.markup import escape

from .. import core as mc
from ..core.base import phased
from ..dispatch import resolve_runner
from ..i18n import _
from .ui import console, die, run_with_progress

# Downloads from links are temporary: only the result (a transcript, ...) is kept.
_DOWNLOADED: set[Path] = set()
_DL_DIR: Path | None = None

# Parts of a --concat join: result → [(stem, start offset in seconds)], so the transcript
# can name each part before its piece.
_CONCAT_PARTS: dict[Path, list[tuple[str, float]]] = {}


def concat_parts(src: Path | str) -> list[tuple[str, float]] | None:
    """Parts of a --concat join as (stem, start offset), or None when src is not a join."""
    return _CONCAT_PARTS.get(src) if isinstance(src, Path) else None


def _download_dir() -> Path:
    """Temporary folder of the process for intermediate files (a --concat join)."""
    global _DL_DIR
    if _DL_DIR is None:
        _DL_DIR = Path(tempfile.mkdtemp(prefix="mnema-dl-"))
        atexit.register(shutil.rmtree, _DL_DIR, ignore_errors=True)
    return _DL_DIR


def input_token(raw: str) -> str:
    """A command-line input without surrounding quotes; a link without shell escaping."""
    s = raw.strip().strip('"').strip("'")
    return mc.clean_url(s) if mc.is_url(s) else s


def cookies_path(args) -> Path | None:
    """The --cookies file; a missing file is an error before any download."""
    raw = getattr(args, "cookies", None)
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_file():
        die(_("No cookies file: {path}", path=path))
    return path


# Companion login domains: youtube.com cookies alone do not log in, the session lives on
# google.com; the same for instagram and facebook.
_COOKIE_COMPANIONS = {
    "youtube.com": {"google.com"},
    "youtu.be": {"youtube.com", "google.com"},
    "instagram.com": {"facebook.com"},
}


def browser_cookies_netscape(spec: str, urls: list[str]) -> str:
    """Local browser cookies for the domains of the links, as Netscape cookies.txt.

    Only the link domains and their login companions, not the whole browser."""
    from urllib.parse import urlparse

    from yt_dlp.cookies import SUPPORTED_BROWSERS, extract_cookies_from_browser

    name, _sep, profile = spec.partition(":")
    name, profile = name.strip().lower(), profile.strip()
    if name not in SUPPORTED_BROWSERS:
        die(
            _(
                'yt-dlp does not support the browser "{name}". Available: {browsers}',
                name=name,
                browsers=", ".join(sorted(SUPPORTED_BROWSERS)),
            )
        )
    jar = extract_cookies_from_browser(name, profile or None)
    # Known two-level public suffixes: for a.b.co.uk the base is b.co.uk, not co.uk,
    # otherwise the cookies of every site in the zone would leave.
    two_level = {"co", "com", "org", "net", "gov", "ac", "edu", "or", "ne"}
    bases: set[str] = set()
    for u in urls:
        host = urlparse(u).hostname or ""
        host = host.encode("idna").decode("ascii") if any(ord(c) > 127 for c in host) else host
        parts = host.lower().split(".")
        if len(parts) >= 3 and parts[-2] in two_level and len(parts[-1]) == 2:
            bases.add(".".join(parts[-3:]))
        elif len(parts) >= 2:
            bases.add(".".join(parts[-2:]))
    for b in list(bases):
        bases |= _COOKIE_COMPANIONS.get(b, set())
    lines = ["# Netscape HTTP Cookie File"]
    for c in jar:
        dom = (c.domain or "").lstrip(".")
        if not any(dom == b or dom.endswith("." + b) for b in bases):
            continue
        lines.append(
            "\t".join(
                [
                    c.domain or "",
                    "TRUE" if (c.domain or "").startswith(".") else "FALSE",
                    c.path or "/",
                    "TRUE" if c.secure else "FALSE",
                    str(int(c.expires or 0)),
                    c.name or "",
                    c.value or "",
                ]
            )
        )
    return "\n".join(lines) + "\n"


def _url_cache_dir(url: str, want_video: bool) -> Path:
    """Download cache of a link: running the same link again does not download it again.

    Lives in the system tmp, which the OS cleans up. The key includes want_video: audio and
    video of one link differ. yt-dlp resumes an interrupted .part in the same folder."""
    key = hashlib.sha1(f"{int(want_video)}:{url}".encode()).hexdigest()[:16]
    d = Path(tempfile.gettempdir()) / "mnema-dl-cache" / key
    d.mkdir(parents=True, exist_ok=True)
    return d


_DONE_MARK = ".mnema-done"  # written only after yt-dlp succeeds


def _cached_download(url: str, want_video: bool) -> Path | None:
    """The cached file of a link, only with the completion marker.

    A missing .part proves nothing: yt-dlp downloads video and audio streams in turn and
    the intermediate .fNNN.ext has no .part."""
    d = _url_cache_dir(url, want_video)
    if not (d / _DONE_MARK).exists():
        return None
    files = [
        f
        for f in d.iterdir()
        if f.is_file() and f.suffix not in (".part", ".ytdl") and f.name != _DONE_MARK
    ]
    return files[0] if files else None


def _merged_name(files: list[Path], *, want_video: bool = True) -> str:
    """Name of a merge result from the common prefix of the inputs: talk_v1/talk_v2 →
    talk_merged.mov; no common prefix — "merged".

    Audio with different extensions → .m4a: the first file's container (say .webm from
    YouTube) may not accept the re-encoded stream."""
    base = os.path.commonprefix([f.stem for f in files]).rstrip("vV0123456789 _-.")
    base = base.strip(" _-.")
    suffixes = {f.suffix.lower() for f in files}
    suffix = files[0].suffix if (want_video or len(suffixes) == 1) else ".m4a"
    return f"{base}_merged{suffix}" if base else f"merged{suffix}"


def _resolve_sources(
    raw_inputs,
    *,
    want_video: bool,
    merge: bool = False,
    cookies_browser: str | None = None,
    cookies_file: Path | None = None,
) -> list[Path]:
    """Expand the inputs into files: download links, walk folders.

    Only what is named explicitly; the current folder is never scanned. merge=True joins the
    inputs into one file before processing.
    """
    items = [input_token(s) for s in (raw_inputs or []) if s and s.strip()]
    items = [s for s in items if s]
    exts = mc.VIDEO_EXT if want_video else mc.MEDIA_EXT
    # All links under one bar, each in its own slice ("2/5: ..."), not a stack of bars.
    urls = list(dict.fromkeys(u for u in items if mc.is_url(u)))
    downloaded: dict[str, Path] = {}
    if urls:

        def dl_all(on):
            slices = phased(on, *([1.0] * len(urls)))
            for i, (u, sl) in enumerate(zip(urls, slices, strict=True), 1):
                cached = _cached_download(u, want_video)
                if cached is not None:
                    sl(
                        1.0,
                        _("{i}/{n}: from the cache", i=i, n=len(urls))
                        if len(urls) > 1
                        else _("from the cache"),
                    )
                    _DOWNLOADED.add(cached)
                    downloaded[u] = cached
                    continue

                def on_i(frac, msg, _sl=sl, _i=i):
                    _sl(frac, f"{_i}/{len(urls)}: {msg}" if msg and len(urls) > 1 else msg)

                cache_dir = _url_cache_dir(u, want_video)
                dl = mc.download_url(
                    u,
                    want_video=want_video,
                    dest_dir=cache_dir,
                    cookies_browser=cookies_browser,
                    cookies_file=cookies_file,
                    on=on_i,
                )
                (cache_dir / _DONE_MARK).touch()
                sl(1.0, "")
                _DOWNLOADED.add(dl)
                downloaded[u] = dl

        run_with_progress(_("Downloading"), dl_all, total=1.0, live_preview=True)
    sources: list[Path] = []
    for raw in items:
        if mc.is_url(raw):
            sources.append(downloaded[raw])
            continue
        p = Path(raw).expanduser()
        if p.is_dir():
            found = sorted(f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in exts)
            if not found:
                note = _("No suitable files in the folder: {path}", path=escape(str(p)))
                console.print(f"[yellow]{note}[/]")
            sources.extend(found)
        elif p.exists():
            sources.append(p)
        else:
            # A named input is missing: stop rather than process a batch with a gap. The usual
            # cause is an unescaped space that split the name into two arguments.
            die(
                _(
                    "Not found: {path}\nCheck that spaces in the name are escaped "
                    "(the usual cause).",
                    path=p,
                )
            )
    if not sources:
        die(_("No file, folder or link given."))
    if merge and len(sources) > 1:
        # The user's order; files inside a folder are sorted by name.
        name = _merged_name(sources, want_video=want_video)
        if sources[0] in _DOWNLOADED:
            # The first input sits in its link's cache folder: a join there would poison
            # the cache, so it goes to the temporary folder.
            dest = _download_dir() / name
            _DOWNLOADED.add(dest)
        else:
            dest = sources[0].with_name(name)
        run_with_progress(_("Joining"), lambda on: mc.merge(sources, dest, on=on), total=None)
        # Part offsets from durations; unknown ones are zeros and the transcript falls back
        # to listing the names on top.
        offsets: list[float] = []
        acc = 0.0
        for f in sources:
            offsets.append(acc)
            try:
                acc += mc.probe(f).duration or 0.0
            except mc.MediaError:
                acc += 0.0
        _CONCAT_PARTS[dest] = [(s.stem, off) for s, off in zip(sources, offsets, strict=True)]
        return [dest]
    return sources


def _sources(args, *, want_video: bool) -> list[Path]:
    """Inputs of a command: args.inputs, --concat, --cookies and --cookies-from-browser."""
    return _resolve_sources(
        args.inputs,
        want_video=want_video,
        merge=getattr(args, "concat", False),
        cookies_browser=args.cookies_from_browser,
        cookies_file=cookies_path(args),
    )


_GENERIC_STEMS = {"watch", "embed", "video", "download"}


def _url_stem(url: str) -> str:
    """Result stem from a link: the last path segment without its extension.

    A generic segment (youtube.com/watch?v=ID) falls back to ?v= or a short hash of the
    link, otherwise different links would overwrite one file.
    """
    parsed = urlparse(url)
    name = Path(unquote(parsed.path)).name
    stem = Path(name).stem if name else ""
    if stem and stem.lower() not in _GENERIC_STEMS:
        return stem
    video_id = parse_qs(parsed.query).get("v", [None])[0]
    if video_id:
        return video_id
    return hashlib.sha1(url.encode()).hexdigest()[:8]


def _dispatch_sources(
    args, *, want_video: bool, runner: str | None, local: bool = False
) -> list[Path | str]:
    """Inputs for dispatch: links stay strings, files and folders are expanded.

    With a runner the client downloads links with its cookies and uploads the media; without
    one everything goes through _resolve_sources.
    """
    if resolve_runner(runner, local=local) is None:
        return list(_sources(args, want_video=want_video))
    raw_items = [input_token(s) for s in (args.inputs or []) if s and s.strip()]
    url_tokens: list[Path | str] = [s for s in raw_items if mc.is_url(s)]
    local_tokens = [s for s in raw_items if not mc.is_url(s)]
    local_sources: list[Path | str] = []
    if local_tokens:
        local_args = type(args)(**{**vars(args), "inputs": local_tokens})
        local_sources = list(_sources(local_args, want_video=want_video))
    sources = url_tokens + local_sources
    if not sources:
        die(_("No file, folder or link given."))
    return sources


def _out_path(src: Path | str, args, n_sources: int, default):
    """Output path. -o is the file for one input, and the output folder (created) for several
    inputs or an existing folder. Without -o a downloaded link goes to the current folder.
    default is a suffix ('.txt') or a function src -> Path."""
    if isinstance(src, str):  # a link passed to the runner
        base = Path.cwd() / _url_stem(src)
    elif src in _DOWNLOADED:
        base = Path.cwd() / src.name
    else:
        base = src
    named = base.with_suffix(default) if isinstance(default, str) else default(base)
    out_arg = getattr(args, "output", None)
    if not out_arg:
        return named
    target = Path(out_arg).expanduser()
    if n_sources > 1 or target.is_dir():
        target.mkdir(parents=True, exist_ok=True)
        return target / named.name
    return target
