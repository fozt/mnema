"""Updates from published releases: each release gets its own folder and environment from its
lock file, under data_dir()/releases/<version>-*/.venv.

Neither a developer's checkout nor the running version's environment is touched. The new
version becomes the app only after it is installed and checked; the previous one stays on disk.
The server is a Docker image and is updated with a new one on its host (`mnema update` there).

The self-contained Mac app (Mnema.app from the DMG) is replaced as a whole bundle from the
manifest's `mac` block instead: the new bundle is checked next to the old one and swapped in.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field, field_validator

from . import __version__
from . import extras as roles
from .core import base
from .core.base import MediaError
from .i18n import _

TRANSPORT: httpx.BaseTransport | None = None
logger = logging.getLogger(__name__)
_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_MAX_ARCHIVE = 64 * 1024 * 1024
_MAX_MAC_ARCHIVE = 4 * 1024 * 1024 * 1024
_MACOS = re.compile(r"^[0-9]+(\.[0-9]+){0,2}$")
RELEASES_PAGE = "https://github.com/fozt/mnema/releases/latest"
PREVIOUS_BUNDLE = ".Mnema-previous.app"
_ATTEMPT = ".mnema-update-"


def _version(value: str) -> tuple[int, ...]:
    if not _VERSION.fullmatch(value):
        raise ValueError(_("Invalid Mnema version"))
    return tuple(map(int, value.split(".")))


def _https(value: str) -> str:
    url = urlparse(value)
    if url.scheme != "https" or not url.netloc or url.username or url.password:
        raise ValueError(_("The release archive must be served over HTTPS"))
    return value


def _macos(value: str) -> tuple[int, ...]:
    parts = tuple(map(int, value.split(".")))
    return parts + (0,) * (3 - len(parts))


class MacRelease(BaseModel):
    """The self-contained Mnema.app of the release: a zip made by ditto --keepParent."""

    archive: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size: int = Field(gt=0, le=_MAX_MAC_ARCHIVE)
    min_macos: str = Field(default="14.0", pattern=_MACOS.pattern)

    @field_validator("archive")
    @classmethod
    def secure_archive(cls, value: str) -> str:
        return _https(value)


class Release(BaseModel):
    version: str
    archive: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    mac: MacRelease | None = None

    @field_validator("version")
    @classmethod
    def valid_version(cls, value: str) -> str:
        _version(value)
        return value

    @field_validator("archive")
    @classmethod
    def secure_archive(cls, value: str) -> str:
        return _https(value)


class Available(BaseModel):
    current: str = __version__
    release: Release | None = None

    @property
    def any(self) -> bool:
        return self.release is not None and _version(self.release.version) > _version(self.current)

    def text(self) -> str:
        return f"Mnema {self.release.version}" if self.any and self.release else ""


class Result(BaseModel):
    version: str = __version__
    python: str | None = None  # a source release: its interpreter
    bundle: str | None = None  # the Mac bundle that now holds the new version

    @property
    def changed(self) -> bool:
        return self.python is not None or self.bundle is not None


def app_bundle() -> Path | None:
    """The self-contained Mnema.app this process runs from (the DMG install), else None."""
    from .bar.bundle import app_bundle

    return app_bundle()


def _insecure() -> MediaError:
    return MediaError(_("The update channel redirected to an insecure address"))


def _client() -> httpx.Client:
    """Follows redirects (GitHub sends release assets on to its asset host), every hop HTTPS."""

    def require_https(request: httpx.Request) -> None:
        # The hook runs before every request, intermediate redirects included: checking only
        # the final address would let HTTPS → HTTP → HTTPS through.
        if request.url.scheme != "https":
            raise _insecure()

    return httpx.Client(
        transport=TRANSPORT,
        timeout=30,
        follow_redirects=True,
        event_hooks={"request": [require_https]},
    )


def _get(client: httpx.Client, url: str) -> httpx.Response:
    response = client.get(url)
    if response.status_code in (401, 403, 404):
        # nothing published yet, or the channel is closed: not "no new versions"
        raise MediaError(_("Updates are unavailable right now"))
    response.raise_for_status()
    if response.url.scheme != "https":
        raise _insecure()
    return response


def check(*, current: str = __version__) -> Available:
    """The latest release from the channel's manifest (mnema-release.json, Settings.update_url);
    git is never consulted."""
    try:
        from .config import Settings

        with _client() as client:
            url = Settings().update_url
            if urlparse(url).scheme != "https":
                raise MediaError(_("The update channel address must start with https://"))
            release = Release.model_validate(_get(client, url).json())
            if _version(release.version) <= _version(current):
                return Available(current=current)
            if release.mac is None and app_bundle() is not None:
                return Available(current=current)  # a release without the Mac app
            return Available(current=current, release=release)
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise MediaError(_("Couldn't check for updates. Try again later")) from exc


def _env() -> dict[str, str]:
    extra = [str(Path.home() / ".local/bin"), "/opt/homebrew/bin", "/usr/local/bin"]
    env: dict[str, str] = dict(os.environ)
    env["PATH"] = os.pathsep.join([*extra, os.environ.get("PATH", "")])
    # uv must create the new release's own environment, not reuse the running one.
    for name in (
        "VIRTUAL_ENV",
        "UV_PROJECT_ENVIRONMENT",
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONEXECUTABLE",
        "__PYVENV_LAUNCHER__",
    ):
        env.pop(name, None)
    return env


def _run(*cmd: str, cwd: Path, what: str, said: bool = False) -> str:
    """said: the process states the reason itself, one stderr line (SystemExit with a text)."""
    try:
        result = subprocess.run(
            cmd, cwd=cwd, env=_env(), capture_output=True, text=True, check=False,
            stdin=subprocess.DEVNULL,
        )  # fmt: skip
    except OSError as exc:
        raise MediaError(what) from exc
    if result.returncode:
        # Technical details go to the log; the UI never shows paths of the installation.
        logger.warning("%s: %s", what, result.stderr[-4000:])
        lines = result.stderr.strip().splitlines()
        raise MediaError(lines[0] if said and len(lines) == 1 else what)
    return result.stdout


def _release_root() -> Path:
    from .config import data_dir

    return data_dir() / "releases"


def _fetch(url: str, sha256: str, destination: Path, *, limit: int) -> None:
    digest = hashlib.sha256()
    size = 0
    with _client() as client, client.stream("GET", url) as response:
        response.raise_for_status()
        if response.url.scheme != "https":
            raise MediaError(_("The update package isn't available over a secure connection"))
        with destination.open("wb") as out:
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > limit:
                    raise MediaError(_("The update package is larger than allowed"))
                digest.update(chunk)
                out.write(chunk)
    if digest.hexdigest() != sha256:
        raise MediaError(_("The update didn't download correctly. Try again"))


def _download(release: Release, destination: Path) -> None:
    _fetch(release.archive, release.sha256, destination, limit=_MAX_ARCHIVE)


def _unpack(archive: Path, destination: Path, version: str) -> None:
    with tarfile.open(archive) as package:
        members = []
        size = 0
        for member in package:
            size += member.size
            if size > _MAX_ARCHIVE * 4 or len(members) >= 10000:
                raise MediaError(_("The update package is too large"))
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts or not (member.isfile() or member.isdir()):
                raise MediaError(_("Invalid update package contents"))
            members.append(member)
        package.extractall(destination, members=members, filter="data")
    project = tomllib.loads((destination / "pyproject.toml").read_text())
    if project["project"]["version"] != version or not (destination / "uv.lock").is_file():
        raise MediaError(_("The update package has no locked Mnema version"))


def _app_installed() -> bool:
    """An update installs the app; a bare CLI has none."""
    return any(importlib.util.find_spec(m) for m in ("pystray", "AppKit"))


_UNFINISHED = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)-")


def _drop_unfinished(root: Path) -> None:
    """Remove folders of interrupted attempts: no installed marker and not in use."""
    keep = {Path(sys.prefix).parent.resolve()}
    for path in root.iterdir():
        if (
            path.is_dir()
            and not path.is_symlink()
            and _UNFINISHED.match(path.name)
            and not (path / ".mnema-installed.json").exists()
            and path.resolve() not in keep
        ):
            shutil.rmtree(path, ignore_errors=True)


def _extras() -> list[str]:
    # The app as ./install.sh client installs it: a Mac processes itself, a Linux client sends
    # recordings to the server
    return list(roles.MAC if base.STANDALONE else roles.LINUX_CLIENT)


def _prune_releases(root: Path, active: Path) -> None:
    """Keep the new and the previous release; never remove the running Python's."""
    previous = Path(sys.prefix).parent.resolve()
    candidates = sorted(
        (
            path
            for path in root.iterdir()
            if path.is_dir()
            and not path.is_symlink()
            and (path / ".mnema-installed.json").is_file()
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    keep = {active.resolve(), previous}
    if previous not in {path.resolve() for path in candidates}:
        # Running from outside the releases (the app env of ./install.sh client): one previous
        # release is enough.
        keep.update([path.resolve() for path in candidates if path != active][:1])
    for path in candidates:
        if path.resolve() not in keep:
            shutil.rmtree(path, ignore_errors=True)


def _release_page(archive: str) -> str:
    """The GitHub page of the release an asset belongs to."""
    found = re.match(r"^(https://[^?#]+/releases)/download/([^/?#]+)/", archive)
    return f"{found[1]}/tag/{found[2]}" if found else RELEASES_PAGE


def _system_tool(*cmd: str, what: str) -> None:
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL
        )
    except OSError as exc:
        raise MediaError(what) from exc
    if result.returncode:
        logger.warning("%s: %s", what, result.stderr[-4000:])
        raise MediaError(what)


def _checked_bundle(app: Path, version: str) -> None:
    """The unpacked bundle is Mnema of the expected version and its signature holds."""
    from .bar.install import BUNDLE_ID

    try:
        info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException) as exc:
        raise MediaError(_("Invalid update package contents")) from exc
    if not isinstance(info, dict) or info.get("CFBundleIdentifier") != BUNDLE_ID:
        raise MediaError(_("The update package isn't the Mnema app"))
    if info.get("CFBundleShortVersionString") != version:
        raise MediaError(_("The update package holds another Mnema version"))
    _system_tool(
        "/usr/bin/codesign", "--verify", "--deep", "--strict",
        f"-R=identifier \"{BUNDLE_ID}\"", str(app),
        what=_("The new Mnema version isn't signed correctly"),
    )  # fmt: skip


def _app_not_running() -> None:
    """A running app keeps loading code and starting workers from its bundle: a command must not
    swap the bundle under it."""
    from .bar.app import running_elsewhere

    if running_elsewhere():
        raise MediaError(
            _(
                "Mnema is running. Quit it and update again, or update from the app: Settings → "
                "General → Check for updates"
            )
        )


def _apply_bundle(bundle: Path, release: Release, on: Callable[[str], None]) -> Result:
    """Replace the running Mnema.app: download and check the new bundle next to it, then swap
    the folders; the old bundle stays as .Mnema-previous.app until the next start."""
    mac = release.mac
    if mac is None:
        raise MediaError(_("This Mnema version has no Mac app"))
    page = _release_page(mac.archive)
    from .bar.bundle import translocated

    if translocated(bundle):
        raise MediaError(
            _(
                "Move Mnema to the Applications folder to update it. The new version is on the "
                "release page: {url}",
                url=page,
            )
        )
    system = platform.mac_ver()[0]
    if system and _MACOS.fullmatch(system) and _macos(system) < _macos(mac.min_macos):
        raise MediaError(
            _(
                "Mnema {version} needs macOS {macos} or later",
                version=release.version,
                macos=mac.min_macos,
            )
        )
    _app_not_running()
    folder = bundle.parent
    if not (os.access(folder, os.W_OK | os.X_OK) and os.access(bundle, os.W_OK)):
        raise MediaError(
            _(
                "Mnema can't replace itself in this folder. Download the new version from the "
                "release page: {url}",
                url=page,
            )
        )
    work = Path(tempfile.mkdtemp(prefix=_ATTEMPT, dir=folder))  # same volume: rename works
    try:
        on(_("Downloading Mnema {version}…", version=release.version))
        archive = work / "Mnema.zip"
        _fetch(mac.archive, mac.sha256, archive, limit=mac.size)
        on(_("Checking the new version…"))
        unpacked = work / "unpacked"
        _system_tool(
            "/usr/bin/ditto", "-x", "-k", str(archive), str(unpacked),
            what=_("Invalid update package contents"),
        )  # fmt: skip
        archive.unlink()
        app = unpacked / "Mnema.app"
        if app.is_symlink() or not app.is_dir():
            raise MediaError(_("Invalid update package contents"))
        # httpx sets no quarantine, but a copy may carry it: a missing attribute is no failure
        subprocess.run(
            ["/usr/bin/xattr", "-dr", "com.apple.quarantine", str(app)],
            capture_output=True, check=False, stdin=subprocess.DEVNULL,
        )  # fmt: skip
        _checked_bundle(app, release.version)
        on(_("Installing the app…"))
        _app_not_running()
        previous = folder / PREVIOUS_BUNDLE
        if previous.is_dir() and not previous.is_symlink():
            shutil.rmtree(previous)
        os.rename(bundle, previous)
        try:
            os.rename(app, bundle)
        except OSError:
            os.rename(previous, bundle)
            raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return Result(version=release.version, bundle=str(bundle))


def drop_previous_bundle() -> None:
    """At the app start: remove the bundle an update replaced and interrupted attempts."""
    import fcntl

    bundle = app_bundle()
    if bundle is None or bundle.name == PREVIOUS_BUNDLE:
        return
    root = _release_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        with (root / ".update.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return  # an update is running: its folders are in use
            folder = bundle.parent
            for path in [folder / PREVIOUS_BUNDLE, *folder.glob(f"{_ATTEMPT}*")]:
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path, ignore_errors=True)
    except OSError:
        logger.info("previous app bundle not removed", exc_info=True)


def apply(on: Callable[[str], None] = lambda _m: None, *, terminal: bool = False) -> Result:
    """Prepare one locked release and switch the app to it.
    terminal: a command runs the update, so a refusal may name the command that helps; otherwise
    it is an app notice without commands, and the command goes to the log."""
    import fcntl

    root = _release_root()
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".update.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MediaError(_("A Mnema update is already running")) from exc
        current = __version__
        bundle = app_bundle()
        if bundle is not None:
            found = check(current=current)
            if not found.any or found.release is None:
                return Result(version=current)
            try:
                return _apply_bundle(bundle, found.release, on)
            except MediaError:
                raise
            except Exception as exc:
                raise MediaError(
                    _("Couldn't update Mnema. The previous version is in place")
                ) from exc
        if not _app_installed():
            raise MediaError(
                _(
                    "The update installs the Mnema app, and there is none here. The server is "
                    "updated on its machine: mnema update"
                )
            )
        _drop_unfinished(root)
        found = check(current=current)
        if not found.any or found.release is None:
            return Result(version=current)
        release = found.release
        if sys.platform == "darwin":
            from .bar.install import NO_BUILD_TOOLS, UPDATE_NO_BUILD_TOOLS, has_build_tools

            if not has_build_tools():  # swift builds Mnema.app: say so before downloading
                if terminal:
                    raise MediaError(NO_BUILD_TOOLS)
                logger.warning("update: %s", NO_BUILD_TOOLS)
                raise MediaError(UPDATE_NO_BUILD_TOOLS)
        uv = shutil.which("uv", path=_env()["PATH"])
        if uv is None:
            logger.warning("update: uv not found")
            raise MediaError(_("Couldn't update. Reinstall Mnema"))
        # The folder gets its final name at once: a venv cannot be moved after installing.
        destination = Path(tempfile.mkdtemp(prefix=f"{release.version}-", dir=root))
        try:
            on(_("Downloading Mnema {version}…", version=release.version))
            with tempfile.TemporaryDirectory() as temp:
                archive = Path(temp) / "mnema.tar.gz"
                _download(release, archive)
                _unpack(archive, destination, release.version)
            on(_("Preparing the new version…"))
            extras = [arg for extra in _extras() for arg in ("--extra", extra)]
            _run(uv, "sync", "--locked", "--no-dev", "--python", "3.12", *extras,
                 cwd=destination, what=_("Couldn't prepare the new Mnema version"))  # fmt: skip
            python = str(destination / ".venv/bin/python")
            smoke = "import mnema.bar.runtime; import mnema.bar.protocol"
            failed = _("The new Mnema version failed the start check")
            _run(python, "-c", smoke, cwd=destination, what=failed)
            (destination / ".mnema-installed.json").write_text(
                json.dumps({"version": release.version})
            )
            on(_("Installing the app…"))
            _run(
                python,
                "-c",
                "from mnema.bar.install import install_release; "
                f"install_release(terminal={terminal})",
                cwd=destination,
                what=_("Couldn't install the new app version"),
                said=True,
            )
            from .install_setup import link_cli

            # The command link and the cleanup never undo a switched installation.
            try:
                link_cli(destination / ".venv")
            except OSError:
                pass
            try:
                _prune_releases(root, destination)
            except OSError:
                pass
            return Result(version=release.version, python=python)
        except BaseException as exc:  # Ctrl-C too: no half-made folder is left
            shutil.rmtree(destination, ignore_errors=True)
            if isinstance(exc, MediaError) or not isinstance(exc, Exception):
                raise
            raise MediaError(_("Couldn't update Mnema. The previous version is in place")) from exc


def main() -> None:
    from .config import load_env

    load_env()
    try:
        result = apply(on=lambda message: print(message, flush=True), terminal=True)
    except MediaError as exc:
        print(str(exc) or _("Couldn't update Mnema"), file=sys.stderr)
        raise SystemExit(1) from None
    if result.changed:
        print(_("Mnema {version} installed. Restart the app.", version=result.version))
    else:
        print(_("Mnema {version}: no new versions", version=result.version))


if __name__ == "__main__":
    main()
