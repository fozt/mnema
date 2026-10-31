"""Installing Mnema (mnema/bar/install.py): Mnema.app comes only from the DMG, so every source
install on a Mac is refused with the DMG's address; Linux gets an app menu shortcut. The
bundle's Info.plist and the login item are written here.

The external boundary is the OS: subprocess (open, notify) and the platform."""

from __future__ import annotations

import plistlib
import sys
from pathlib import Path

import pytest

from mnema.bar import install
from mnema.bar.app import main

DMG = "Mnema для Mac ставится из DMG: https://github.com/fozt/mnema/releases/latest"


@pytest.fixture
def home(monkeypatch, tmp_path):
    """Leave the real Mnema settings, shortcuts and login item alone."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(install, "_launch_agent_path", lambda: tmp_path / "agent.plist")
    started = []
    monkeypatch.setattr(install.subprocess, "Popen", lambda cmd, **kw: started.append(cmd))
    return started


def _info(app: Path) -> dict:
    return plistlib.loads((app / "Contents" / "Info.plist").read_bytes())


def test_bundle_is_english_with_a_russian_localization(tmp_path) -> None:
    """Info.plist of the built app: a menu bar app with permission prompts that follow the system
    language, English in Info.plist and en.lproj, Russian in ru.lproj."""
    app = tmp_path / "Mnema.app"
    install.write_bundle(app, version="9.1.2")
    info = _info(app)
    assert info["CFBundleIdentifier"] == "com.mnema.bar" and info["CFBundleExecutable"] == "Mnema"
    assert info["CFBundleShortVersionString"] == "9.1.2" and info["LSUIElement"] is True
    assert info["CFBundleDevelopmentRegion"] == "en"
    assert info["CFBundleLocalizations"] == ["en", "ru"]
    assert info["NSMicrophoneUsageDescription"] == "Mnema records your voice during a call."
    res = app / "Contents" / "Resources"
    english = (res / "en.lproj" / "InfoPlist.strings").read_text(encoding="utf-8")
    russian = (res / "ru.lproj" / "InfoPlist.strings").read_text(encoding="utf-8")
    assert '"NSMicrophoneUsageDescription" = "Mnema records your voice during a call.";' in english
    assert (
        '"NSAudioCaptureUsageDescription" = "Mnema записывает голоса собеседников из звонка.";'
        in russian
    )


def test_launch_agent_opens_the_running_bundle(home, monkeypatch, tmp_path) -> None:
    """The login item opens the app the engine runs in, with the config dir the engine reads."""
    from mnema.config import config_dir

    app = tmp_path / "Applications" / "Mnema.app"
    monkeypatch.setenv("MNEMA_APP_BUNDLE", str(app))
    monkeypatch.setattr(install.platform, "system", lambda: "Darwin")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    installed = config_dir()
    assert install.set_autostart(True)
    agent = plistlib.loads(install._launch_agent_path().read_bytes())
    assert agent["RunAtLoad"] is True
    assert agent["ProgramArguments"] == ["/usr/bin/open", "-a", str(app)]
    monkeypatch.setenv("XDG_CONFIG_HOME", agent["EnvironmentVariables"]["XDG_CONFIG_HOME"])
    assert config_dir() == installed


def test_mac_install_from_the_sources_is_sent_to_the_dmg(home, monkeypatch, capsys) -> None:
    """`mnema-bar install` on a Mac builds nothing and starts nothing: the app is the DMG."""
    monkeypatch.setattr(install.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(install.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(sys, "argv", ["mnema-bar", "install"])
    with pytest.raises(SystemExit) as refused:
        main()
    assert refused.value.code == DMG
    assert home == [] and not install._launch_agent_path().exists()


@pytest.mark.parametrize("terminal", [False, True])
def test_update_of_a_mac_app_built_from_the_sources_is_sent_to_the_dmg(
    monkeypatch, terminal
) -> None:
    """The 0.4 update of a source-built Mac app runs install_release of the new release: one
    line naming the DMG, the old app stays."""
    monkeypatch.setattr(install.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(install.platform, "machine", lambda: "arm64")
    with pytest.raises(SystemExit) as refused:
        install.install_release(terminal=terminal)
    assert refused.value.code == DMG


def test_the_command_of_uv_tool_installs_no_app(home, monkeypatch, tmp_path) -> None:
    """The environment of `uv tool install mnema[cli]` has no tray and uv replaces it at will:
    `mnema-bar install` there makes no shortcut."""
    monkeypatch.setattr(install.platform, "system", lambda: "Linux")
    tool = tmp_path / "uv-tools" / "mnema"
    tool.mkdir(parents=True)
    (tool / "uv-receipt.toml").write_text("[tool]\n")
    monkeypatch.setattr(sys, "prefix", str(tool))
    monkeypatch.setattr(sys, "argv", ["mnema-bar", "install"])
    with pytest.raises(SystemExit) as refused:
        main()
    assert refused.value.code == (
        "В команде mnema нет приложения. Установите Mnema: https://github.com/fozt/mnema#install"
    )
    assert home == [] and not install._desktop_path().exists()


def test_linux_install_makes_the_shortcut_and_starts_the_tray(home, monkeypatch) -> None:
    monkeypatch.setattr(install.platform, "system", lambda: "Linux")
    summary = install.install()
    assert summary.startswith("Установлено: ")
    entry = install._desktop_path().read_text(encoding="utf-8")
    assert install._autostart_path().read_text(encoding="utf-8") == entry
    assert f"{sys.executable} -m mnema.bar" in entry
    assert home == [[sys.executable, "-m", "mnema.bar"]]


def _cyrillic(text: str) -> bool:
    return any("а" <= ch.lower() <= "я" or ch in "ёЁ" for ch in text)


def test_install_texts_follow_an_english_interface(home, monkeypatch) -> None:
    """With an English interface the summary and the refusals have no Russian."""
    from mnema import i18n

    monkeypatch.setattr(install.platform, "system", lambda: "Linux")
    with i18n.using("en"):
        summary = install.install()
        uninstalled = install.uninstall()
        monkeypatch.setattr(install.platform, "machine", lambda: "arm64")
        refusal = install.from_dmg()
    assert summary.startswith("Installed: ") and not _cyrillic(summary)
    assert not _cyrillic(uninstalled)
    assert (
        refusal
        == "Mnema for Mac installs from the DMG: https://github.com/fozt/mnema/releases/latest"
    )
    entry = install._desktop_entry()
    assert "Comment=Call recording and transcription\n" in entry
    assert "Comment[ru]=Запись звонков и расшифровка\n" in entry


def test_mnema_bar_on_a_mac_points_at_the_dmg_in_english(monkeypatch) -> None:
    """`mnema-bar` with no arguments on a Mac: the app is Mnema.app from the DMG."""
    from mnema import i18n

    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr("platform.machine", lambda: "arm64")
    monkeypatch.setattr(sys, "argv", ["mnema-bar"])
    monkeypatch.setenv("MNEMA_UI_LANG", "en")
    with i18n.using("en"), pytest.raises(SystemExit) as refused:
        main()
    assert refused.value.code == (
        "Mnema for Mac installs from the DMG: https://github.com/fozt/mnema/releases/latest"
    )


def test_old_source_app_on_an_intel_mac_is_not_sent_to_the_dmg(monkeypatch) -> None:
    """The 0.4 update of a source-built app calls install_release: the DMG is Apple silicon only."""
    from mnema import i18n

    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr("platform.machine", lambda: "x86_64")
    with i18n.using("en"), pytest.raises(SystemExit) as refused:
        install.install_release(terminal=True)
    assert refused.value.code == (
        "The Mnema app needs a Mac with Apple silicon. On this Mac, the mnema command works with "
        "a server: https://github.com/fozt/mnema#install"
    )
