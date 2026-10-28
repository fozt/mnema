"""The self-contained Mnema.app (DMG, scripts/build_mac_app.py): setup at engine start.

Mnema.app sets MNEMA_APP_BUNDLE to its bundle path. Opened right from the disk image, or as a
quarantined download that macOS runs from a random read-only copy (App Translocation), the panel
asks to move it to Applications and nothing is set up. Otherwise the mnema command
(~/.local/bin/mnema) and "Open at login" point at this bundle. A source build
(~/Applications/Mnema.app) is never touched.
"""

from __future__ import annotations

import logging
import os
import plistlib
import sys
from pathlib import Path

from ..i18n import _

logger = logging.getLogger(__name__)

ENV = "MNEMA_APP_BUNDLE"
MOVE_TO_APPLICATIONS = "Move Mnema to the Applications folder and open it from there"


def app_bundle() -> Path | None:
    """The running self-contained bundle, None for a source build or the CLI of a venv."""
    if value := os.environ.get(ENV, ""):
        return Path(value)
    prefix = Path(sys.prefix)  # <bundle>/Contents/Resources/python: the bundled CLI
    parts = (prefix.name, prefix.parent.name, prefix.parent.parent.name)
    if parts == ("python", "Resources", "Contents") and prefix.parents[2].suffix == ".app":
        return prefix.parents[2]
    return None


def translocated(bundle: Path) -> bool:
    """Run from a temporary place: macOS App Translocation or the read-only disk image itself."""
    if "/AppTranslocation/" in str(bundle):
        return True
    try:
        return bool(os.statvfs(bundle).f_flag & os.ST_RDONLY)
    except OSError:
        return False


def placed_bundle() -> Path | None:
    """The bundle when it runs from its real place: macOS translocation gives a temporary path
    that must not be written into links or the login item."""
    bundle = app_bundle()
    return None if bundle is None or translocated(bundle) else bundle


def command(bundle: Path) -> Path:
    return bundle / "Contents" / "Resources" / "bin" / "mnema"


def _data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "mnema"


def _is_mnema_bundle(path: Path) -> bool:
    from .install import BUNDLE_ID

    try:
        with open(path / "Contents" / "Info.plist", "rb") as f:
            return plistlib.load(f).get("CFBundleIdentifier") == BUNDLE_ID
    except (OSError, plistlib.InvalidFileException):
        return False


def _ours(target: Path) -> bool:
    """A mnema command Mnema installed: inside a Mnema bundle, or in the environments of
    ./install.sh client and of updates (~/.local/share/mnema)."""
    if target.is_relative_to(os.path.realpath(_data_dir())):
        return True
    parts = target.parts
    for i, part in enumerate(parts):
        if part.endswith(".app") and parts[i + 1 : i + 2] == ("Contents",):
            return _is_mnema_bundle(Path(*parts[: i + 1]))
    return False


def link_command(bundle: Path) -> bool:
    """Point ~/.local/bin/mnema at the bundle's command when the place is free, a broken link,
    or a link to a Mnema install. Someone else's file or link stays. True — the link is ours."""
    from ..install_setup import cli_link

    link, wanted = cli_link(), command(bundle)
    if link.is_symlink():
        target = Path(os.path.realpath(link))
        if target == Path(os.path.realpath(wanted)):
            return True
        if link.exists() and not _ours(target):
            return False
    elif link.exists():
        return False
    link.parent.mkdir(parents=True, exist_ok=True)
    draft = link.with_name(f".{link.name}.{os.getpid()}")
    draft.unlink(missing_ok=True)
    draft.symlink_to(wanted)
    os.replace(draft, link)
    return True


def _first_start_marker() -> Path:
    return _data_dir() / "app-started"


def point_autostart(bundle: Path) -> None:
    """ "Open at login" opens this bundle: an existing login item (also one of a source build)
    is moved to it; on the very first start of a self-contained app it is turned on, as the
    source installer does. Turned off later — it stays off."""
    from .install import autostart_enabled, launch_agent_target, set_autostart

    marker = _first_start_marker()
    if autostart_enabled():
        if launch_agent_target() != bundle:
            set_autostart(True)
    elif not marker.exists():
        set_autostart(True)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()


def prepare() -> str:
    """At engine start: a notice for the panel, "" — nothing to say."""
    bundle = app_bundle()
    if bundle is None:
        return ""
    if translocated(bundle):
        return _(MOVE_TO_APPLICATIONS)
    import threading

    from ..update import drop_previous_bundle

    # the bundle a past update replaced: removing ~2 GB takes a while
    threading.Thread(target=drop_previous_bundle, daemon=True, name="mnema-cleanup").start()
    try:
        if not link_command(bundle):
            logger.info("~/.local/bin/mnema is someone else's, left as is")
    except OSError:
        logger.warning("mnema command not linked", exc_info=True)
    try:
        point_autostart(bundle)
    except (OSError, ValueError):
        logger.warning("login item not updated", exc_info=True)
    return ""
