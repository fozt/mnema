"""Install mnema-bar as a regular app that runs without a terminal.

macOS: Mnema.app comes from the DMG (scripts/build_mac_app.py writes its Info.plist here); there
is no build from the sources. The login item is a LaunchAgent. Linux (GNOME): .desktop files for
the app menu (~/.local/share/applications) and autostart (~/.config/autostart), the icon in
hicolor. The Linux shortcut fixes PATH and the config dir (XDG_CONFIG_HOME) at install time: the
session does not know the shell's values, and the app must read .env and bar.json where the
installer and the CLI wrote them.
"""

from __future__ import annotations

import json
import os
import platform
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .. import __version__
from ..core.base import APP_ID, MediaError
from ..i18n import _

# English sources of the refusals below; the texts are read in the current language
_FROM_DMG = "Mnema for Mac installs from the DMG: {url}"
_NOT_IN_UV_TOOL = "The mnema command has no app. Install Mnema: {url}"
INSTALL_PAGE = "https://github.com/fozt/mnema#install"
_EXTRA_PATH = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin")


def from_dmg() -> str:
    """The refusal of every source install of the Mac app."""
    from ..update import RELEASES_PAGE

    return _(_FROM_DMG, url=RELEASES_PAGE)


def _path_env() -> str:
    parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    for p in _EXTRA_PATH:
        if p not in parts:
            parts.append(p)
    return os.pathsep.join(parts)


def log_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "mnema" / "bar.log"


def launch_command() -> list[str]:
    """This interpreter (the venv with the engines) and the module."""
    return [sys.executable, "-m", "mnema.bar"]


def _config_home() -> str:
    """The app's XDG_CONFIG_HOME: the parent of the installer's config_dir()."""
    from ..config import config_dir

    return str(config_dir().parent)


# ── macOS ─────────────────────────────────────────────────────────────────────


def mac_app_path() -> Path:
    return Path.home() / "Applications" / "Mnema.app"


def app_path() -> Path:
    """The Mnema.app this engine runs in, wherever it is; outside a bundle (a terminal run of a
    development build) ~/Applications/Mnema.app."""
    from .bundle import app_bundle

    return app_bundle() or mac_app_path()


def _launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{APP_ID}.plist"


def launch_agent_target() -> Path | None:
    """The app the login item opens, None — no login item or not ours."""
    try:
        with open(_launch_agent_path(), "rb") as f:
            args = plistlib.load(f).get("ProgramArguments") or []
    except (OSError, plistlib.InvalidFileException):
        return None
    return Path(args[-1]) if args[:2] == ["/usr/bin/open", "-a"] and len(args) == 3 else None


# Permission prompts, per bundle localization (InfoPlist.strings); English is also in Info.plist.
USAGE = {
    "en": {
        "NSMicrophoneUsageDescription": "Mnema records your voice during a call.",
        "NSAudioCaptureUsageDescription": "Mnema records the other side's voices from a call.",
    },
    "ru": {
        "NSMicrophoneUsageDescription": "Mnema записывает ваш голос во время звонка.",
        "NSAudioCaptureUsageDescription": "Mnema записывает голоса собеседников из звонка.",
    },
}


def _write_strings(path: Path, texts: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = (f'"{key}" = {json.dumps(value, ensure_ascii=False)};' for key, value in texts.items())
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_bundle(app: Path, *, version: str = __version__) -> None:
    """Info.plist and the permission texts of Mnema.app (scripts/build_mac_app.py; the icon is
    the ready Mnema.icns of the Swift package). The app finds its Python inside the bundle."""
    res = app / "Contents" / "Resources"
    res.mkdir(parents=True, exist_ok=True)
    info = {
        "CFBundleIdentifier": APP_ID,
        "CFBundleName": "Mnema",
        "CFBundleDisplayName": "Mnema",
        "CFBundleExecutable": "Mnema",
        "CFBundleIconFile": "Mnema",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
        # AppKit strings (folder picker, field menus) and the app language follow the system
        "CFBundleDevelopmentRegion": "en",
        "CFBundleLocalizations": list(USAGE),
        "LSUIElement": True,  # menu bar only, no Dock icon
        "LSMinimumSystemVersion": "14.2",
        **USAGE["en"],
    }
    with open(app / "Contents" / "Info.plist", "wb") as f:
        plistlib.dump(info, f)
    for lang, texts in USAGE.items():
        _write_strings(res / f"{lang}.lproj" / "InfoPlist.strings", texts)


def _mac_set_autostart(enabled: bool) -> bool:
    agent = _launch_agent_path()
    if not enabled:
        agent.unlink(missing_ok=True)
        return False
    from .bundle import MOVE_TO_APPLICATIONS, app_bundle, translocated

    if (bundle := app_bundle()) is not None and translocated(bundle):
        raise MediaError(_(MOVE_TO_APPLICATIONS))  # the login item would open a temporary copy
    agent.parent.mkdir(parents=True, exist_ok=True)
    with open(agent, "wb") as f:
        plistlib.dump(
            {
                "Label": APP_ID,
                "ProgramArguments": ["/usr/bin/open", "-a", str(app_path())],
                "EnvironmentVariables": {"XDG_CONFIG_HOME": _config_home()},
                "RunAtLoad": True,
                "ProcessType": "Interactive",
            },
            f,
        )
    return True


# ── Linux (GNOME) ─────────────────────────────────────────────────────────────


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or str(Path.home() / default))


def _linux_icon_path() -> Path:
    return (
        _xdg("XDG_DATA_HOME", ".local/share")
        / "icons"
        / "hicolor"
        / "256x256"
        / "apps"
        / "mnema.png"
    )


def _desktop_entry() -> str:
    env = ["env", f"PATH={_path_env()}", f"XDG_CONFIG_HOME={_config_home()}"]
    cmd = " ".join(shlex.quote(c) for c in [*env, *launch_command()])
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Mnema\n"
        "Comment=Call recording and transcription\n"
        "Comment[ru]=Запись звонков и расшифровка\n"
        f"Exec={cmd}\n"
        f"Icon={_linux_icon_path()}\n"
        "Terminal=false\n"
        "Categories=AudioVideo;Audio;Recorder;\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


def _desktop_path() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share") / "applications" / "mnema-bar.desktop"


def _autostart_path() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / "autostart" / "mnema-bar.desktop"


def install_linux_desktop() -> Path:
    from .icons import app_icon

    icon = _linux_icon_path()
    icon.parent.mkdir(parents=True, exist_ok=True)
    try:
        app_icon(256).save(icon)
    except Exception:  # noqa: BLE001 - the shortcut works without an icon
        pass
    desktop = _desktop_path()
    desktop.parent.mkdir(parents=True, exist_ok=True)
    desktop.write_text(_desktop_entry(), encoding="utf-8")
    return desktop


def _linux_set_autostart(enabled: bool) -> bool:
    path = _autostart_path()
    if not enabled:
        path.unlink(missing_ok=True)
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_desktop_entry(), encoding="utf-8")
    return True


# ── common ────────────────────────────────────────────────────────────────────


def set_autostart(enabled: bool) -> bool:
    """Turn start at login on or off; returns the actual state."""
    if platform.system() == "Darwin":
        return _mac_set_autostart(enabled)
    return _linux_set_autostart(enabled)


def autostart_enabled() -> bool:
    path = _launch_agent_path() if platform.system() == "Darwin" else _autostart_path()
    return path.exists()


def install_release(*, terminal: bool = False) -> None:
    """update.apply calls this in the new release's Python: switch the app, keep the old one on
    failure. The reason is one stderr line (SystemExit). A Mac app of 0.4 built from the sources
    gets the DMG: there is no source build any more. terminal — the 0.4 update passes it."""
    if platform.system() == "Darwin":
        raise SystemExit(from_dmg())
    try:
        _switch_app()
    except MediaError as e:
        raise SystemExit(str(e)) from None


def _switch_app() -> None:
    targets = [_desktop_path(), _autostart_path()]
    auto = autostart_enabled()
    with tempfile.TemporaryDirectory() as temp:
        backups = []
        for index, target in enumerate(targets):
            backup = Path(temp) / str(index)
            if target.is_dir():
                shutil.copytree(target, backup, symlinks=True)
            elif target.exists():
                shutil.copy2(target, backup)
            backups.append((target, backup))
        try:
            install_linux_desktop()
            if auto:
                _linux_set_autostart(True)
        except Exception:
            for target, backup in backups:
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink(missing_ok=True)
                if backup.is_dir():
                    shutil.copytree(backup, target, symlinks=True)
                elif backup.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(backup, target)
            raise


def install() -> str:
    """`mnema-bar install` (Linux): the shortcut plus autostart; returns the summary to print."""
    if platform.system() == "Darwin":
        raise MediaError(from_dmg())
    from ..core.base import uv_tool

    if uv_tool():  # the command's environment: no tray, and uv may replace it any time
        raise MediaError(_(_NOT_IN_UV_TOOL, url=INSTALL_PAGE))
    desktop = install_linux_desktop()
    _linux_set_autostart(True)
    subprocess.Popen(
        launch_command(),
        start_new_session=True,
        env={**os.environ, "PATH": _path_env()},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return _(
        "Installed: {desktop}\nMnema is in the app menu and opens at login. Its icon is in the "
        "top bar and needs the AppIndicator extension, which Ubuntu already has.",
        desktop=desktop,
    )


def uninstall() -> str:
    set_autostart(False)
    if platform.system() == "Darwin":
        shutil.rmtree(mac_app_path(), ignore_errors=True)
    else:
        _desktop_path().unlink(missing_ok=True)
        _linux_icon_path().unlink(missing_ok=True)
    return _("Mnema was removed from apps and autostart. Recordings and settings are kept.")
