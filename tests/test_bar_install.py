"""Building Mnema at install (mnema/bar/install.py): the Swift panel goes into the bundle; without
the Command Line Tools or on a failed build — a clear refusal, and the previous app stays intact.

The external boundary is the OS tools (swift, xcode-select, codesign through subprocess.run) and
their lookup (shutil.which): no real swift build runs in unit tests."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

from mnema.bar import install
from mnema.bar.app import main
from mnema.core.base import MediaError

SWIFT_BINARY = b"\xcf\xfa\xed\xfe swift"
BUILD_ERROR = "PanelView.swift:12:5: error: cannot find 'Foo' in scope"
NO_TOOLS = "Установите Command Line Tools: xcode-select --install"


@pytest.fixture
def toolchain(monkeypatch, tmp_path):
    """Install tools: swift is found, the Command Line Tools are installed, swift build succeeds.
    A test changes found / clt / swift ("ok" | "error" | "timeout"); calls — what was run."""
    tools = types.SimpleNamespace(found={"swift"}, clt=True, swift="ok", calls=[], swift_kwargs={})
    tools.cache = tmp_path / "cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(tools.cache))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # leave the real Mnema bar.lock alone
    monkeypatch.setattr(install, "mac_app_path", lambda: tmp_path / "Applications" / "Mnema.app")
    monkeypatch.setattr(install, "_launch_agent_path", lambda: tmp_path / "agent.plist")
    monkeypatch.setattr(
        shutil, "which", lambda name, *a, **k: f"/usr/bin/{name}" if name in tools.found else None
    )
    # the panel loads this interpreter's libpython: substitute our own
    monkeypatch.setattr(install.sysconfig, "get_config_var", lambda name: {
        "LIBDIR": str(tmp_path), "LDVERSION": "3.12",
    }.get(name))  # fmt: skip
    (tmp_path / "libpython3.12.dylib").write_bytes(b"")
    # venv: home in pyvenv.cfg is uv's stable link to Python; its libpython survives an update
    python = tmp_path / "cpython-3.12"
    (python / "bin").mkdir(parents=True)
    (python / "lib").mkdir()
    tools.libpython = python / "lib" / "libpython3.12.dylib"
    tools.libpython.write_bytes(b"")
    venv = tmp_path / "venv"
    venv.mkdir()
    (venv / "pyvenv.cfg").write_text(f"home = {python / 'bin'}\n", encoding="utf-8")
    monkeypatch.setattr(sys, "prefix", str(venv))

    def run(cmd, **kw):
        tools.calls.append(cmd)
        tool = Path(cmd[0]).name
        if tool == "xcode-select":
            return types.SimpleNamespace(returncode=0 if tools.clt else 2, stdout="", stderr="")
        if tool == "swift":
            tools.swift_kwargs = kw
            if tools.swift == "timeout":
                raise subprocess.TimeoutExpired(cmd, kw["timeout"])
            if tools.swift == "ok":
                release = Path(cmd[cmd.index("--scratch-path") + 1]) / "release"
                release.mkdir(parents=True, exist_ok=True)
                (release / "Mnema").write_bytes(SWIFT_BINARY)
            return types.SimpleNamespace(
                returncode=0 if tools.swift == "ok" else 1,
                stdout="",
                stderr="" if tools.swift == "ok" else BUILD_ERROR,
            )
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(install.subprocess, "run", run)
    return tools


def _swift_calls(tools) -> list[list[str]]:
    return [c for c in tools.calls if Path(c[0]).name == "swift"]


def _info(app: Path) -> dict:
    return plistlib.loads((app / "Contents" / "Info.plist").read_bytes())


def test_swift_panel_goes_into_the_bundle(toolchain) -> None:
    """Mnema.app: the panel, Info.plist with permission descriptions and LSUIElement, the engine is
    this interpreter with Homebrew on PATH and a log; the bundle is signed by identifier."""
    app = install.install_mac_app()
    assert (app / "Contents" / "MacOS" / "Mnema").read_bytes() == SWIFT_BINARY
    info = _info(app)
    assert info["CFBundleExecutable"] == "Mnema" and info["CFBundleIdentifier"] == "com.mnema.bar"
    assert info["LSUIElement"] is True
    assert info["NSMicrophoneUsageDescription"] and info["NSAudioCaptureUsageDescription"]
    # the app must know how to run the engine: this interpreter, Homebrew on PATH, the log
    assert info["MnemaPython"] == sys.executable
    assert "/opt/homebrew/bin" in info["MnemaPath"].split(os.pathsep)
    assert info["MnemaLog"] == str(install.log_path())
    [sign] = [c for c in toolchain.calls if c[0] == "codesign"]
    assert sign[:5] == ["codesign", "--force", "--deep", "--sign", "-"] and sign[-1] == str(app)
    # permissions follow the identifier, not the hash
    assert 'identifier "com.mnema.bar"' in sign[5]


def test_bundle_is_english_with_a_russian_localization(toolchain) -> None:
    """Permission prompts follow the system language: English in Info.plist and en.lproj,
    today's Russian texts in ru.lproj."""
    app = install.install_mac_app()
    info = _info(app)
    assert info["CFBundleDevelopmentRegion"] == "en"
    assert info["CFBundleLocalizations"] == ["en", "ru"]
    assert info["NSMicrophoneUsageDescription"] == "Mnema records your voice during a call."
    res = app / "Contents" / "Resources"
    english = (res / "en.lproj" / "InfoPlist.strings").read_text(encoding="utf-8")
    russian = (res / "ru.lproj" / "InfoPlist.strings").read_text(encoding="utf-8")
    assert '"NSMicrophoneUsageDescription" = "Mnema records your voice during a call.";' in english
    assert (
        '"NSMicrophoneUsageDescription" = "Mnema записывает ваш голос во время звонка.";' in russian
    )
    assert (
        '"NSAudioCaptureUsageDescription" = "Mnema записывает голоса собеседников из звонка.";'
        in russian
    )


def test_launch_agent_opens_the_app(toolchain, monkeypatch) -> None:
    monkeypatch.setattr(install.platform, "system", lambda: "Darwin")
    app = install.install_mac_app()
    assert install.set_autostart(True)
    agent = plistlib.loads(install._launch_agent_path().read_bytes())
    assert agent["RunAtLoad"] is True
    assert agent["ProgramArguments"] == ["/usr/bin/open", "-a", str(app)]


def test_panel_reads_the_settings_dir_the_installer_wrote(toolchain, monkeypatch, tmp_path):
    """XDG_CONFIG_HOME is set only in the installer's shell and launchd does not know it: the panel
    takes the config dir from Info.plist (Bridge.swift passes XDG_CONFIG_HOME to the engine),
    autostart from the LaunchAgent; both read .env and bar.json where the installer and CLI wrote
    them."""
    from mnema.config import config_dir

    monkeypatch.setattr(install.platform, "system", lambda: "Darwin")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    installed = config_dir()
    home = _info(install.install_mac_app())["MnemaConfigHome"]
    install.set_autostart(True)
    agent = plistlib.loads(install._launch_agent_path().read_bytes())["EnvironmentVariables"]
    for configured in (home, agent["XDG_CONFIG_HOME"]):
        monkeypatch.setenv("XDG_CONFIG_HOME", configured)
        assert config_dir() == installed


@pytest.mark.parametrize("failure", ["error", "timeout"])
def test_release_install_keeps_existing_app_when_swift_build_fails(toolchain, monkeypatch, failure):
    """Update: the new release's panel did not build — the previous app stays intact, the reason
    is one line for the user (SystemExit), details are in the log."""
    monkeypatch.setattr(install.platform, "system", lambda: "Darwin")
    app = install.install_mac_app()
    before = (app / "Contents/Info.plist").read_bytes()
    toolchain.swift = failure
    with pytest.raises(SystemExit) as failed:
        install.install_release()
    assert failed.value.code == f"Mnema.app не собралась, подробности в {install.log_path()}"
    assert (app / "Contents/MacOS/Mnema").read_bytes() == SWIFT_BINARY
    assert (app / "Contents/Info.plist").read_bytes() == before
    log = install.log_path().read_text(encoding="utf-8")
    assert "swift build" in log and (BUILD_ERROR if failure == "error" else "900") in log


@pytest.mark.parametrize(
    ("missing", "said", "logged"),
    [
        ("tools", "Для обновления нужны Command Line Tools. Подробности в журнале Mnema", NO_TOOLS),
        ("libpython", "Не удалось обновить. Установите Mnema заново", "uv python install 3.12"),
    ],
)
@pytest.mark.parametrize("terminal", [False, True], ids=["app", "terminal"])
def test_release_install_that_cannot_build_says_it_without_terminal_commands(
    toolchain, monkeypatch, tmp_path, missing, said, logged, terminal
):
    """Update from the app: Mnema.app cannot be built — the notice gets the reason without terminal
    commands, the helpful command goes to the log. Updated from a command — the reason keeps its
    command. The previous app stays intact."""
    monkeypatch.setattr(install.platform, "system", lambda: "Darwin")
    app = install.install_mac_app()
    if missing == "tools":
        toolchain.clt = False
    else:
        (tmp_path / "libpython3.12.dylib").unlink()
        toolchain.libpython.unlink()
    with pytest.raises(SystemExit) as failed:
        install.install_release(terminal=terminal)
    if terminal:
        assert isinstance(failed.value.code, str) and logged in failed.value.code
    else:
        assert failed.value.code == said
        assert logged in install.log_path().read_text(encoding="utf-8")
    assert (app / "Contents/MacOS/Mnema").read_bytes() == SWIFT_BINARY


def test_release_install_restores_app_after_failure_replacing_the_panel(toolchain, monkeypatch):
    monkeypatch.setattr(install.platform, "system", lambda: "Darwin")
    app = install.install_mac_app()
    before = (app / "Contents/Info.plist").read_bytes()

    def full_disk(*args, **kwargs):
        raise OSError("нет места")

    monkeypatch.setattr(shutil, "copy", full_disk)
    with pytest.raises(OSError, match="нет места"):
        install.install_release()
    assert (app / "Contents/MacOS/Mnema").read_bytes() == SWIFT_BINARY
    assert (app / "Contents/Info.plist").read_bytes() == before


def test_release_swap_restores_previous_if_activation_rename_fails(toolchain, monkeypatch):
    monkeypatch.setattr(install.platform, "system", lambda: "Darwin")
    app = install.install_mac_app()
    (app / "old-version").write_text("previous")
    rename = Path.rename

    def fail_activation(path, target):
        if path.name == "Mnema.app" and path.parent.name.startswith(".mnema-update-"):
            raise OSError("переключение не удалось")
        return rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_activation)
    with pytest.raises(OSError, match="переключение"):
        install.install_release()
    assert (app / "old-version").read_text() == "previous"
    assert (app / "Contents/MacOS/Mnema").read_bytes() == SWIFT_BINARY


def test_panel_gets_the_libpython_that_survives_python_updates(toolchain) -> None:
    """The panel loads the venv's Python in its own process: the libpython path in Info.plist comes
    from the stable python folder (uv moves its link to each new patch release), not from the
    LIBDIR of one version."""
    assert _info(install.install_mac_app())["MnemaLibPython"] == str(toolchain.libpython)


def test_python_without_shared_libpython_is_refused(toolchain, tmp_path) -> None:
    """A Python without a shared libpython: the panel could not run the engine, so nothing is built
    or installed, and the reason is clear."""
    (tmp_path / "libpython3.12.dylib").unlink()  # LIBDIR has only a static build
    with pytest.raises(MediaError, match="разделяемой libpython"):
        install.install_mac_app()
    assert not _swift_calls(toolchain) and not install.mac_app_path().exists()


def test_swift_build_command(toolchain) -> None:
    install.install_mac_app()
    [cmd] = _swift_calls(toolchain)
    assert cmd[:2] == ["/usr/bin/swift", "build"] and cmd[cmd.index("-c") + 1] == "release"
    package = Path(cmd[cmd.index("--package-path") + 1])
    assert package.parts[-3:] == ("mnema", "bar", "macos") and (package / "Package.swift").exists()
    # a reinstall rebuilds only what changed: the cache persists next to the log
    assert Path(cmd[cmd.index("--scratch-path") + 1]) == toolchain.cache / "mnema" / "swift-build"
    assert toolchain.swift_kwargs["timeout"] == 900


@pytest.mark.parametrize("missing", ["swift", "command-line-tools"])
def test_install_without_command_line_tools_keeps_the_previous_app(
    toolchain, monkeypatch, missing
) -> None:
    """`mnema-bar install` without the Command Line Tools: /usr/bin/swift is then a stub that opens
    an install window, so it is not run; a refusal with a hint, the previous app stays intact."""
    monkeypatch.setattr(install.platform, "system", lambda: "Darwin")
    app = install.install_mac_app()
    before = (app / "Contents/Info.plist").read_bytes()
    toolchain.calls.clear()
    if missing == "swift":
        toolchain.found = set()
    else:
        toolchain.clt = False
    monkeypatch.setattr(sys, "argv", ["mnema-bar", "install"])
    with pytest.raises(SystemExit) as failed:
        main()
    assert failed.value.code == NO_TOOLS
    assert not _swift_calls(toolchain) and ["/usr/bin/open", str(app)] not in toolchain.calls
    assert (app / "Contents/Info.plist").read_bytes() == before
    assert (app / "Contents/MacOS/Mnema").read_bytes() == SWIFT_BINARY


def _cyrillic(text: str) -> bool:
    return any("а" <= ch.lower() <= "я" or ch in "ёЁ" for ch in text)


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
def test_install_summary_and_refusals_follow_an_english_interface(
    toolchain, monkeypatch, system
) -> None:
    """With an English interface the install summary, the update notices and the refusals have no
    Russian; the shared refusal constants are read in the language current at that moment."""
    from mnema import i18n

    monkeypatch.setattr(install.platform, "system", lambda: system)
    monkeypatch.setattr(install.subprocess, "Popen", lambda *a, **k: None)
    with i18n.using("en"):
        summary = install.install()
        uninstalled = install.uninstall()
        notices = [
            install.NO_BUILD_TOOLS,
            install.UPDATE_NO_BUILD_TOOLS,
            install.UPDATE_NO_LIBPYTHON,
        ]
    assert summary.startswith("Installed: ") and not _cyrillic(summary)
    assert not _cyrillic(uninstalled) and not any(map(_cyrillic, notices))
    assert install.NO_BUILD_TOOLS == NO_TOOLS
    if system == "Linux":
        entry = install._desktop_entry()
        assert "Comment=Call recording and transcription\n" in entry
        assert "Comment[ru]=Запись звонков и расшифровка\n" in entry


def test_mac_refusal_without_the_app_reads_in_english(monkeypatch) -> None:
    """`mnema-bar` with no arguments on a Mac points at the install command, in English too."""
    from mnema import i18n

    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr(sys, "argv", ["mnema-bar"])
    monkeypatch.setenv("MNEMA_UI_LANG", "en")
    with i18n.using("en"), pytest.raises(SystemExit) as refused:
        main()
    said = str(refused.value.code)
    assert said.startswith("Install the Command Line Tools") and said.endswith("mnema-bar install")
    assert not _cyrillic(said)


def test_install_interrupted_while_building_keeps_the_previous_app(toolchain, monkeypatch) -> None:
    """Ctrl-C while the new bundle is being signed: the previous Mnema.app stays as it was and no
    half-built bundle is left in Applications."""
    monkeypatch.setattr(install.platform, "system", lambda: "Darwin")
    install.install()
    app = install.mac_app_path()
    (app / "Contents/previous").touch()
    stub = install.subprocess.run

    def interrupted(cmd, **kw):
        if Path(cmd[0]).name == "codesign":
            raise KeyboardInterrupt
        return stub(cmd, **kw)

    monkeypatch.setattr(install.subprocess, "run", interrupted)
    with pytest.raises(KeyboardInterrupt):
        install.install()
    assert (app / "Contents/previous").exists() and (app / "Contents/MacOS/Mnema").exists()
    assert sorted(p.name for p in app.parent.iterdir()) == ["Mnema.app"]
