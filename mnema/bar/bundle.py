"""The self-contained Mnema.app (DMG, scripts/build_mac_app.py): setup at engine start.

Mnema.app sets MNEMA_APP_BUNDLE to its bundle path. Opened right from the disk image, or as a
quarantined download that macOS runs from a random read-only copy (App Translocation), the panel
asks to move it to Applications and nothing is set up. Otherwise app.json tells the mnema command
(a separate package) where the app and its mnema-asr are, and "Open at login" points at this
bundle. The app has no mnema command of its own: the link of an earlier version goes. The bundle
an earlier version built from the sources (~/Applications/Mnema.app) is never touched.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

from ..core.base import app_file, app_helper, is_mnema_app
from ..i18n import _

logger = logging.getLogger(__name__)

ENV = "MNEMA_APP_BUNDLE"
MOVE_TO_APPLICATIONS = "Move Mnema to the Applications folder and open it from there"


def app_bundle() -> Path | None:
    """The running bundle, None for a terminal run of a development build or the CLI of a
    venv."""
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


def _data_dir() -> Path:
    return app_file().parent


def write_app_json(bundle: Path) -> None:
    """app.json for the mnema command, atomically: the bundle, its mnema-asr and version."""
    from .. import __version__

    path = app_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    draft = path.with_name(f".{path.name}.{os.getpid()}")
    data = {"bundle": str(bundle), "asr": str(app_helper(bundle)), "version": __version__}
    draft.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(draft, path)


def _into_mnema_bundle(target: Path) -> bool:
    parts = target.parts
    for i, part in enumerate(parts):
        if part.endswith(".app") and parts[i + 1 : i + 2] == ("Contents",):
            return is_mnema_app(Path(*parts[: i + 1]))
    return False


def drop_command_link() -> None:
    """~/.local/bin/mnema of an earlier version goes when it leads into a Mnema bundle or
    nowhere; the command of uv tool, a venv or someone else's file stays."""
    from ..install_setup import cli_link

    link = cli_link()
    if not link.is_symlink():
        return
    if link.exists() and not _into_mnema_bundle(Path(os.path.realpath(link))):
        return
    link.unlink()


def _first_start_marker() -> Path:
    return _data_dir() / "app-started"


def point_autostart(bundle: Path) -> None:
    """ "Open at login" opens this bundle: an existing login item (also one of an earlier source
    build) is moved to it; on the very first start of the app it is turned on. Turned off later
    — it stays off."""
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
        write_app_json(bundle)
    except OSError:
        logger.warning("app.json not written", exc_info=True)
    try:
        drop_command_link()
    except OSError:
        logger.warning("the old mnema command link stays", exc_info=True)
    try:
        point_autostart(bundle)
    except (OSError, ValueError):
        logger.warning("login item not updated", exc_info=True)
    return ""
