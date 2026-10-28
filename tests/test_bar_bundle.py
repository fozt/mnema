"""First start of the self-contained Mnema.app: the mnema command, the login item, its place."""

import os
import plistlib
from pathlib import Path

import pytest

from mnema.bar import bundle, install


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


def _agent_app() -> str:
    with open(install._launch_agent_path(), "rb") as f:
        return plistlib.load(f)["ProgramArguments"][-1]


def test_first_start_links_the_command_and_opens_at_login(app):
    assert bundle.prepare() == ""
    assert Path(os.path.realpath(_link())) == (app / "Contents/Resources/bin/mnema").resolve()
    assert _agent_app() == str(app)


def test_translocated_app_asks_to_move_and_sets_nothing_up(tmp_path, monkeypatch):
    path = _app(tmp_path / "private/var/folders/x/T/AppTranslocation/ABC/d/Mnema.app")
    monkeypatch.setenv(bundle.ENV, str(path))
    assert bundle.prepare() == "Перенесите Mnema в папку «Программы» и откройте её оттуда"
    assert not _link().exists() and not _link().is_symlink()
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
    assert not _link().exists() and not _link().is_symlink()


def test_someone_elses_command_stays(app, tmp_path):
    _link().parent.mkdir(parents=True)
    _link().write_text("mine")
    foreign = tmp_path / "other" / "mnema"
    foreign.parent.mkdir()
    foreign.write_text("other")
    bundle.prepare()
    assert _link().read_text() == "mine"
    _link().unlink()
    _link().symlink_to(foreign)
    bundle.prepare()
    assert Path(os.path.realpath(_link())) == foreign.resolve()


def test_older_mnema_installs_hand_the_command_over(app, tmp_path):
    venv = Path(os.environ["XDG_DATA_HOME"]) / "mnema/app/.venv/bin/mnema"
    venv.parent.mkdir(parents=True)
    venv.write_text("#!/bin/sh\n")
    _link().parent.mkdir(parents=True)
    _link().symlink_to(venv)
    bundle.prepare()
    assert Path(os.path.realpath(_link())) == (app / "Contents/Resources/bin/mnema").resolve()
    older = _app(tmp_path / "Old" / "Mnema.app")
    _link().unlink()
    _link().symlink_to(older / "Contents/Resources/bin/mnema")
    bundle.prepare()
    assert Path(os.path.realpath(_link())) == (app / "Contents/Resources/bin/mnema").resolve()
    _link().unlink()
    _link().symlink_to(tmp_path / "gone" / "mnema")  # a broken link
    bundle.prepare()
    assert Path(os.path.realpath(_link())) == (app / "Contents/Resources/bin/mnema").resolve()


def test_another_apps_bundle_is_not_ours(app, tmp_path):
    other = _app(tmp_path / "Other.app", bundle_id="com.example.other")
    _link().parent.mkdir(parents=True)
    _link().symlink_to(other / "Contents/Resources/bin/mnema")
    bundle.prepare()
    assert Path(os.path.realpath(_link())) == (other / "Contents/Resources/bin/mnema").resolve()


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
    assert Path(os.path.realpath(_link())) == (app / "Contents/Resources/bin/mnema").resolve()


def test_moved_bundle_takes_the_command_and_login_item_along(app, tmp_path, monkeypatch):
    bundle.prepare()
    moved = tmp_path / "Elsewhere" / "Mnema.app"
    moved.parent.mkdir()
    app.rename(moved)
    monkeypatch.setenv(bundle.ENV, str(moved))
    bundle.prepare()
    assert Path(os.path.realpath(_link())) == (moved / "Contents/Resources/bin/mnema").resolve()
    assert _agent_app() == str(moved)


def test_open_at_login_from_a_translocated_app_is_refused(tmp_path, monkeypatch):
    from mnema.bar.runtime import BarRuntime

    path = _app(tmp_path / "private/var/folders/x/T/AppTranslocation/ABC/d/Mnema.app")
    monkeypatch.setenv(bundle.ENV, str(path))
    rt = BarRuntime(prefs_file=tmp_path / "bar.json")
    rt.handle("autostart")
    assert rt.state.error == "Перенесите Mnema в папку «Программы» и откройте её оттуда"
    assert not install.autostart_enabled() and rt.prefs.autostart is False
