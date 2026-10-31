"""First start of the self-contained Mnema.app: app.json, the login item, its place."""

import json
import os
import plistlib
from pathlib import Path

import pytest

from mnema import __version__
from mnema.bar import bundle, install
from mnema.core import base


def _app(path: Path, bundle_id: str = "com.mnema.bar") -> Path:
    bin_dir = path / "Contents" / "Resources" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "mnema").write_text("#!/bin/sh\n")
    with open(path / "Contents" / "Info.plist", "wb") as f:
        plistlib.dump({"CFBundleIdentifier": bundle_id}, f)
    return path


@pytest.fixture(autouse=True)
def _mac(monkeypatch):
    """The login item is a LaunchAgent, as on a Mac, also when the suite runs on Linux."""
    monkeypatch.setattr(install.platform, "system", lambda: "Darwin")


@pytest.fixture
def app(tmp_path, monkeypatch):
    path = _app(tmp_path / "Volumes" / "Apps" / "Mnema.app")
    monkeypatch.setenv(bundle.ENV, str(path))
    return path


def _link() -> Path:
    return Path.home() / ".local" / "bin" / "mnema"


def _app_json() -> dict | None:
    path = base.app_file()
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _agent_app() -> str:
    with open(install._launch_agent_path(), "rb") as f:
        return plistlib.load(f)["ProgramArguments"][-1]


def test_first_start_tells_where_the_app_is_and_opens_at_login(app):
    """app.json: the bundle and its mnema-asr for the mnema command (its ffmpeg and recognition)."""
    assert bundle.prepare() == ""
    assert _app_json() == {
        "bundle": str(app),
        "asr": str(app / "Contents/MacOS/mnema-asr"),
        "version": __version__,
    }
    assert _agent_app() == str(app)


def test_translocated_app_asks_to_move_and_sets_nothing_up(tmp_path, monkeypatch):
    path = _app(tmp_path / "private/var/folders/x/T/AppTranslocation/ABC/d/Mnema.app")
    monkeypatch.setenv(bundle.ENV, str(path))
    assert bundle.prepare() == "Перенесите Mnema в папку «Программы» и откройте её оттуда"
    assert _app_json() is None
    assert not install.autostart_enabled()


def test_app_opened_from_the_disk_image_asks_to_move(app, monkeypatch):
    real = os.statvfs

    def statvfs(path):
        stat = real(path)
        if Path(path) == app:  # the mounted image is read-only
            return os.statvfs_result((*stat[:8], stat.f_flag | os.ST_RDONLY, stat.f_namemax))
        return stat

    monkeypatch.setattr(os, "statvfs", statvfs)
    assert bundle.prepare() == "Перенесите Mnema в папку «Программы» и откройте её оттуда"
    assert _app_json() is None


def test_the_command_link_into_a_mnema_bundle_is_removed(app, tmp_path):
    """The app no longer has a mnema command: a link of an earlier version into a Mnema bundle,
    or a broken one, goes; the command of uv tool, a venv or another app stays."""
    _link().parent.mkdir(parents=True)
    _link().symlink_to(app / "Contents/Resources/bin/mnema")
    bundle.prepare()
    assert not _link().is_symlink()
    _link().symlink_to(tmp_path / "gone" / "mnema")
    bundle.prepare()
    assert not _link().is_symlink()
    other = _app(tmp_path / "Other.app", bundle_id="com.example.other")
    tool = tmp_path / "uv" / "tools" / "mnema" / "bin" / "mnema"
    tool.parent.mkdir(parents=True)
    tool.write_text("#!/bin/sh\n")
    for target in (other / "Contents/Resources/bin/mnema", tool):
        _link().symlink_to(target)
        bundle.prepare()
        assert Path(os.path.realpath(_link())) == target.resolve()
        _link().unlink()
    _link().write_text("mine")
    bundle.prepare()
    assert _link().read_text() == "mine"


def test_login_item_of_the_source_build_moves_to_the_bundle_and_its_app_stays(app, monkeypatch):
    source = install.mac_app_path()
    (source / "Contents").mkdir(parents=True)
    (source / "Contents" / "Info.plist").write_text("source build")
    monkeypatch.delenv(bundle.ENV)
    install.set_autostart(True)
    assert _agent_app() == str(source)
    monkeypatch.setenv(bundle.ENV, str(app))
    bundle.prepare()
    assert _agent_app() == str(app)
    assert (source / "Contents" / "Info.plist").read_text() == "source build"


def test_login_item_turned_off_stays_off(app):
    bundle.prepare()
    install.set_autostart(False)
    bundle.prepare()
    assert not install.autostart_enabled()


def test_moved_bundle_takes_app_json_and_login_item_along(app, tmp_path, monkeypatch):
    bundle.prepare()
    moved = tmp_path / "Elsewhere" / "Mnema.app"
    moved.parent.mkdir()
    app.rename(moved)
    monkeypatch.setenv(bundle.ENV, str(moved))
    bundle.prepare()
    found = _app_json()
    assert found is not None
    assert (found["bundle"], found["asr"]) == (str(moved), str(moved / "Contents/MacOS/mnema-asr"))
    assert _agent_app() == str(moved)


def test_open_at_login_from_a_translocated_app_is_refused(tmp_path, monkeypatch):
    from mnema.bar.runtime import BarRuntime

    path = _app(tmp_path / "private/var/folders/x/T/AppTranslocation/ABC/d/Mnema.app")
    monkeypatch.setenv(bundle.ENV, str(path))
    rt = BarRuntime(prefs_file=tmp_path / "bar.json")
    rt.handle("autostart")
    assert rt.state.error == "Перенесите Mnema в папку «Программы» и откройте её оттуда"
    assert not install.autostart_enabled() and rt.prefs.autostart is False
