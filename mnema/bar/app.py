"""mnema-bar entry point.

`mnema-bar` — the icon in the GNOME top bar (on macOS the app is Mnema.app); `mnema-bar install`
installs it as an app (Mnema.app or an app menu shortcut) with autostart, no terminal needed
afterwards; `mnema-bar uninstall` removes it; `--headless` — the engine inside Mnema.app
(headless.py).
"""

from __future__ import annotations

import logging
import platform
import sys
from pathlib import Path


def _load_env() -> None:
    """The same ~/.config/mnema/.env as the CLI (config.load_env)."""
    from ..config import load_env

    load_env()


def _setup_logging() -> None:
    """Without a terminal output goes to ~/.cache/mnema/bar.log, where Mnema.app writes too."""
    from .install import log_path

    handlers: list[logging.Handler] = []
    if sys.stderr is not None and sys.stderr.isatty():
        handlers.append(logging.StreamHandler())
    else:
        log_path().parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path(), encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


_LOCK: object = None  # kept open for the whole run: the flock lives as long as the file


def _lock_path() -> Path:
    return Path.home() / ".cache" / "mnema" / "bar.lock"


def running_elsewhere() -> bool:
    """Mnema runs in another process (the app, while a terminal command asks)."""
    import fcntl

    if _LOCK is not None:
        return False
    try:
        fh = _lock_path().open("r")
    except OSError:
        return False
    with fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            return True
    return False


def _single_instance() -> bool:
    """One Mnema per user: another launch (app menu, ./install.sh, autostart) adds no second icon
    and does not disturb a running recording. False — already running."""
    import fcntl

    global _LOCK
    if _LOCK is not None:  # the engine restarted inside the same process (the macOS app)
        return True
    path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = path.open("w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return False
    _LOCK = fh
    return True


def main() -> None:
    from ..i18n import _, detect, set_language

    set_language(detect())  # Mnema.app passes its language in MNEMA_UI_LANG
    args = sys.argv[1:]
    if args[:1] in (["install"], ["uninstall"]):
        from ..core.base import MediaError
        from . import install

        try:
            print(install.install() if args[0] == "install" else install.uninstall())
        except MediaError as e:
            raise SystemExit(str(e)) from None
        return
    if args[:1] == ["notify-test"]:
        if platform.system() == "Darwin":  # Mnema.app sends the notifications
            print(_("Test it with: ~/Applications/Mnema.app/Contents/MacOS/Mnema notify-test"))
            return
        _load_env()
        from .app_linux import LinuxPlatform

        LinuxPlatform().notify("Mnema", _("Notifications work"), None)
        print(_("Test notification sent (notify-send)."))
        return
    if args == ["--headless"]:  # the macOS app's engine: the protocol is stdin/stdout
        _load_env()
        _setup_logging()
        from . import headless

        if _single_instance():
            headless.run()
        else:
            headless.refuse()
        return
    if args:
        raise SystemExit(_("Usage: mnema-bar [install | uninstall | notify-test]"))
    if platform.system() == "Darwin":  # on a Mac Mnema is only Mnema.app
        raise SystemExit(
            _(
                "Install the Command Line Tools: xcode-select --install, then {command} install",
                command=Path(sys.executable).parent / "mnema-bar",
            )
        )
    if platform.system() != "Linux":
        raise SystemExit(_("mnema-bar runs on macOS and Linux (GNOME)."))
    _load_env()
    _setup_logging()
    if not _single_instance():
        logging.getLogger(__name__).info("Mnema is already running, no second instance")
        return
    from .app_linux import run

    run()


if __name__ == "__main__":
    main()
