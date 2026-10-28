"""Install mnema-bar as a regular app that runs without a terminal.

macOS: ~/Applications/Mnema.app (no py2app): Info.plist plus the Swift panel (mnema/bar/macos),
built at install time by `swift build` from the Command Line Tools; without them there is no
Mnema.app. The engine is this venv's Python inside the panel process (interpreter, libpython,
PATH and log are in Info.plist; without a shared libpython the panel cannot load it). The process
is Mnema.app itself: notifications (UNUserNotificationCenter needs a real app with an identifier)
and the Microphone and System Audio Recording permissions belong to Mnema. Autostart is a
LaunchAgent. Linux (GNOME): .desktop files for the app menu (~/.local/share/applications) and
autostart (~/.config/autostart), the icon in hicolor.

The engine's PATH is fixed at install time: launched from Finder or the Dock it would not see
Homebrew's ffmpeg. So is the config dir (XDG_CONFIG_HOME): launchd and the session do not know the
shell's value, and the app must read .env and bar.json where the installer and the CLI wrote them.
"""

from __future__ import annotations

import contextlib
import json
import os
import platform
import plistlib
import shlex
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import time
from pathlib import Path

from .. import __version__
from ..core.base import MediaError
from ..i18n import _

BUNDLE_ID = "com.mnema.bar"
LSREGISTER = (
    "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework"
    "/Support/lsregister"
)
# English sources of NO_BUILD_TOOLS (terminal and log) and UPDATE_NO_* (the app's update notice,
# no terminal commands: the command goes to the log). The public names resolve in __getattr__.
_NO_BUILD_TOOLS = "Install the Command Line Tools: xcode-select --install"
_UPDATE_NO_BUILD_TOOLS = "Updating needs the Command Line Tools. Details are in the Mnema log"
_UPDATE_NO_LIBPYTHON = "Couldn't update. Install Mnema again"
_EXTRA_PATH = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin")
_SWIFT_PACKAGE = Path(__file__).parent / "macos"


def __getattr__(name: str) -> str:
    """NO_BUILD_TOOLS, UPDATE_NO_BUILD_TOOLS and UPDATE_NO_LIBPYTHON in the current language:
    importers (update.py) read them when they raise, so the text is never frozen at import."""
    if name == "NO_BUILD_TOOLS":
        return _(_NO_BUILD_TOOLS)
    if name == "UPDATE_NO_BUILD_TOOLS":
        return _(_UPDATE_NO_BUILD_TOOLS)
    if name == "UPDATE_NO_LIBPYTHON":
        return _(_UPDATE_NO_LIBPYTHON)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
    """The Mnema.app this engine runs in: the self-contained bundle wherever it is, otherwise the
    source build in ~/Applications."""
    from .bundle import app_bundle

    return app_bundle() or mac_app_path()


def _launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{BUNDLE_ID}.plist"


def launch_agent_target() -> Path | None:
    """The app the login item opens, None — no login item or not ours."""
    try:
        with open(_launch_agent_path(), "rb") as f:
            args = plistlib.load(f).get("ProgramArguments") or []
    except (OSError, plistlib.InvalidFileException):
        return None
    return Path(args[-1]) if args[:2] == ["/usr/bin/open", "-a"] and len(args) == 3 else None


# A fixed engine PATH in Info.plist rather than the installing shell's: Homebrew and ~/.local/bin
# are visible when launched from Finder or the Dock, and a reinstall does not change it
_ENGINE_PATH = (
    "~/.local/bin", "/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin",
    "/usr/bin", "/bin", "/usr/sbin", "/sbin",
)  # fmt: skip


def _engine_path() -> str:
    return os.pathsep.join(str(Path(p).expanduser()) for p in _ENGINE_PATH)


def _stable_libdir() -> Path | None:
    """A libpython folder that survives a Python update: the venv points at the `cpython-3.12-…`
    link (home in pyvenv.cfg) that uv moves to each new patch release, while the interpreter's
    LIBDIR is a concrete `cpython-3.12.13-…` that gets removed. None — not a venv or not found."""
    try:
        cfg = (Path(sys.prefix) / "pyvenv.cfg").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in cfg.splitlines():
        key, _sep, value = line.partition("=")
        if key.strip() == "home":
            lib = Path(value.strip()).parent / "lib"
            return lib if lib.is_dir() else None
    return None


def _libpython() -> Path | None:
    """This interpreter's shared libpython, from the stable folder (_stable_libdir), else from
    LIBDIR. None — a Python without a shared libpython cannot be embedded in the app."""
    libdir = sysconfig.get_config_var("LIBDIR")
    version = sysconfig.get_config_var("LDVERSION") or sysconfig.get_python_version()
    name = f"libpython{version}.dylib"
    if not (libdir and (Path(libdir) / name).exists()):
        return None
    stable = _stable_libdir()
    return (stable if stable is not None and (stable / name).exists() else Path(libdir)) / name


def has_build_tools() -> bool:
    """Without the Command Line Tools /usr/bin/swift is a stub that opens an "install the tools"
    window in the middle of the install."""
    try:
        ok = subprocess.run(["xcode-select", "-p"], capture_output=True, timeout=10).returncode
    except (OSError, subprocess.TimeoutExpired):
        return False
    return ok == 0


def _log_build(text: str, what: str = "swift build") -> None:
    """Why the panel did not build goes to the log the install error points to."""
    log = log_path()
    with contextlib.suppress(OSError):  # the log must not break the install
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {what}: {text.strip()}\n")


class Unbuildable(MediaError):
    """Mnema.app cannot be built here: the text is for the terminal (with a command), update is
    for the app's update notice."""

    def __init__(self, text: str, update: str) -> None:
        super().__init__(text)
        self.update = update


def _build_swift_app() -> Path:
    """Build the panel (package mnema/bar/macos) with swift from the Command Line Tools, no
    Xcode. The build cache persists, so a reinstall rebuilds only what changed. A failed build
    raises MediaError with a hint; the reason is in the log."""
    swift = shutil.which("swift") if has_build_tools() else None
    if swift is None:
        _log_build("no swift or Command Line Tools")
        raise Unbuildable(_(_NO_BUILD_TOOLS), _(_UPDATE_NO_BUILD_TOOLS))
    failed = _("Mnema.app didn't build, details are in {log}", log=log_path())
    scratch = log_path().parent / "swift-build"
    cmd = [
        swift, "build", "-c", "release",
        "--package-path", str(_SWIFT_PACKAGE), "--scratch-path", str(scratch),
    ]  # fmt: skip
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=900)
    except (OSError, subprocess.SubprocessError) as e:
        _log_build(str(e))
        raise MediaError(failed) from e
    built = scratch / "release" / "Mnema"
    if out.returncode == 0 and built.exists():
        return built
    text = out.stdout + out.stderr
    # the reason is in "file:line: error:" lines; the tail of the output is the compiler command
    errors = [line for line in text.splitlines() if ": error:" in line]
    _log_build(
        f"exit code {out.returncode}\n" + ("\n".join(errors[:40]) if errors else text[-4000:])
    )
    raise MediaError(failed)


def _write_icns(dest: Path) -> None:
    """The app icon: a PNG set through iconutil (present on every macOS)."""
    from .icons import app_icon

    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "Mnema.iconset"
        iconset.mkdir()
        base = app_icon(1024)
        for size in (16, 32, 64, 128, 256, 512):
            base.resize((size, size)).save(iconset / f"icon_{size}x{size}.png")
            base.resize((size * 2, size * 2)).save(iconset / f"icon_{size}x{size}@2x.png")
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(dest)], check=True)


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


def write_bundle(app: Path, *, version: str = __version__, engine: dict | None = None) -> None:
    """Info.plist, permission texts and the icon of Mnema.app. ``engine`` — the source build's
    keys saying how to run its venv; the self-contained app (scripts/build_mac_app.py) has none
    and finds its Python inside the bundle."""
    res = app / "Contents" / "Resources"
    res.mkdir(parents=True, exist_ok=True)
    info = {
        "CFBundleIdentifier": BUNDLE_ID,
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
        **(engine or {}),
    }
    with open(app / "Contents" / "Info.plist", "wb") as f:
        plistlib.dump(info, f)
    for lang, texts in USAGE.items():
        _write_strings(res / f"{lang}.lproj" / "InfoPlist.strings", texts)
    try:
        _write_icns(res / "Mnema.icns")
    except Exception:  # noqa: BLE001 - the app works without an icon
        pass


def install_mac_app(*, target: Path | None = None) -> Path:
    """Build Mnema.app into target (default ~/Applications/Mnema.app). The build runs before the
    old app is removed: a failed or interrupted build leaves it intact."""
    libpython = _libpython()  # the panel loads the venv's Python in its own process
    if libpython is None:
        raise Unbuildable(
            _(
                "{python} has no shared libpython, so Mnema.app can't run it. Install Python "
                "from uv: uv python install 3.12, then ./install.sh client",
                python=sys.executable,
            ),
            _(_UPDATE_NO_LIBPYTHON),
        )
    panel = _build_swift_app()
    app = target if target is not None else mac_app_path()
    if app.exists():
        shutil.rmtree(app)
    macos = app / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    binary = macos / "Mnema"
    shutil.copy(panel, binary)
    binary.chmod(0o755)
    # how to run the engine: interpreter, PATH, log, config dir
    engine = {
        "MnemaPython": sys.executable,
        "MnemaLibPython": str(libpython),
        "MnemaPath": _engine_path(),
        "MnemaLog": str(log_path()),
        "MnemaConfigHome": _config_home(),
    }
    write_bundle(app, engine=engine)
    if not _sign_mac_app(app):
        (app / "Contents" / ".unsigned").touch()  # install() warns notifications may not show
    return app


def _sign_mac_app(app: Path) -> bool:
    """Ad-hoc sign the whole bundle (Info.plist bound, identifier com.mnema.bar): without it the
    notification center refuses (UNErrorDomain 1, "not allowed for this app"); the built panel has
    only a linker signature. The designated requirement is the identifier, not the binary hash,
    so permissions (microphone, system audio, notifications) survive a reinstall.
    False — not signed (Mnema works, notifications probably do not)."""
    try:
        out = subprocess.run(
            [
                "codesign", "--force", "--deep", "--sign", "-",
                f"-r=designated => identifier \"{BUNDLE_ID}\"", str(app),
            ],
            capture_output=True, check=False, timeout=60,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.returncode == 0


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
                "Label": BUNDLE_ID,
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
    failure. The reason is one stderr line (SystemExit): for the app's notice it has no terminal
    commands and the helpful command goes to the log; terminal — run from a command, so the
    reason keeps its command."""
    try:
        _switch_app()
    except Unbuildable as e:
        if terminal:
            raise SystemExit(str(e)) from None
        _log_build(str(e), "update")
        raise SystemExit(e.update) from None
    except MediaError as e:
        raise SystemExit(str(e)) from None


def _place_mac_app() -> Path:
    """Build the whole bundle next to the app (same volume), then switch it in with one rename:
    macOS never sees a half-built Mnema.app (Launchpad kept the icon-less one), and an interrupted
    install keeps the previous app."""
    app = mac_app_path()
    app.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".mnema-update-", dir=app.parent) as temp:
        staging, previous = Path(temp) / "Mnema.app", Path(temp) / "previous.app"
        install_mac_app(target=staging)
        if app.exists():
            app.rename(previous)
        try:
            staging.rename(app)
        except BaseException:
            if previous.exists():
                previous.rename(app)
            raise
    # the same path held the old bundle: refresh its cached icon and name
    try:
        subprocess.run([LSREGISTER, "-f", str(app)], capture_output=True, check=False, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        pass  # only the cached icon is stale
    return app


def _switch_app() -> None:
    if platform.system() == "Darwin":
        _place_mac_app()
        return
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


def _bar_running() -> bool:
    """Mnema is running (holds bar.lock): `open` would then only activate the old process."""
    import fcntl

    path = Path.home() / ".cache" / "mnema" / "bar.lock"
    if not path.exists():
        return False
    with path.open("a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fh, fcntl.LOCK_UN)
    return False


def install() -> str:
    """`mnema-bar install`: the app or shortcut plus autostart; returns the summary to print."""
    if platform.system() == "Darwin":
        running = _bar_running()  # before open, or we would see the new one
        app = _place_mac_app()
        _mac_set_autostart(True)
        subprocess.run(["/usr/bin/open", str(app)], check=False)
        if (app / "Contents" / ".unsigned").exists():
            tail = _("Notifications may not show: couldn't sign the app (codesign).")
        else:
            tail = _(
                "macOS will ask to allow notifications for Mnema. "
                "Allow them to know when a recording is ready."
            )
        if running:
            tail += "\n" + _("An old version of Mnema is running. Quit it and open it again.")
        return (
            _(
                "Installed: {app}\nMnema is running, its icon is in the menu bar. "
                "It opens at login.\n"
                "On the first recording macOS will ask Mnema for microphone and system audio "
                "access.\n",
                app=app,
            )
            + tail
        )
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
