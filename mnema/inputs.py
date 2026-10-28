"""Preparing media on the client: links and cookies never reach the runner."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from . import core as mc
from .core.base import MediaError, Progress, noop, phased
from .i18n import _


@contextmanager
def local_input(
    src: Path | str | list[str],
    *,
    cookies: str | None = None,
    want_video: bool = True,
    on: Progress = noop,
) -> Iterator[Path]:
    """A local file is left alone; downloaded files and cookies are deleted on exit.

    A list of links is downloaded into separate folders (names may clash) and joined end to
    end. Cookies are Netscape cookies.txt content, only in a temporary file on the client.
    """
    if isinstance(src, Path) or (isinstance(src, str) and not mc.is_url(src)):
        yield Path(src)
        return
    links = src if isinstance(src, list) else [src]
    if not links:
        raise MediaError(_("The list of links is empty"))
    with TemporaryDirectory(prefix="mnema-input-") as folder:
        root = Path(folder)
        cookies_file = None
        if cookies:
            cookies_file = root / "cookies.txt"
            cookies_file.touch(mode=0o600)
            cookies_file.write_text(cookies, encoding="utf-8")
        download_on, merge_on = phased(on, 0.9, 0.1) if len(links) > 1 else (on, on)
        files = []
        callbacks = phased(download_on, *([1.0] * len(links)))
        for i, (link, progress) in enumerate(zip(links, callbacks, strict=True), 1):
            dest = root / str(i)
            dest.mkdir()
            fetch_on, audio_on = (progress, noop) if want_video else phased(progress, 0.9, 0.1)
            path = mc.download_url(
                link, want_video=want_video, dest_dir=dest, cookies_file=cookies_file, on=fetch_on
            )
            # ba/b sometimes yields video when a site has no audio-only format: extract the
            # sound before joining or uploading, mixed containers do not join as audio.
            if not want_video and mc.probe(path).has_video:
                audio = dest / "audio"
                audio.mkdir()
                path = mc.extract_audio(
                    path, audio / (path.stem + ".mp3"), compressed=True, on=audio_on
                )
            if not want_video:
                audio_on(1.0, "")
            files.append(path)
        if len(files) == 1:
            yield files[0]
        else:
            suffixes = {f.suffix.lower() for f in files}
            fallback = ".mp4" if want_video else ".m4a"
            merged = root / ("merged" + (files[0].suffix if len(suffixes) == 1 else fallback))
            mc.merge(files, merged, on=merge_on)
            yield merged
