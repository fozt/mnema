"""The self-contained Mnema.app replaces itself from the release's Mac zip; the network and the
system tools (ditto, codesign, xattr) are the boundary."""

from __future__ import annotations

import hashlib
import io
import os
import plistlib
import shutil
import subprocess
import sys
import threading
import zipfile
from pathlib import Path

import httpx
import pytest

from mnema import __version__, update
from mnema.bar import runtime
from mnema.bar.runtime import BarRuntime
from mnema.bar.state import Phase
from mnema.core.base import MediaError

NEW = "99.0.0"
RELEASE_PAGE = f"https://github.com/fozt/mnema/releases/tag/v{NEW}"


def _info(version: str, bundle_id: str = "com.mnema.bar") -> bytes:
    return plistlib.dumps(
        {
            "CFBundleIdentifier": bundle_id,
            "CFBundleExecutable": "Mnema",
            "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": version,
            "CFBundleVersion": version,
        }
    )


def _make_app(path: Path, version: str, bundle_id: str = "com.mnema.bar") -> Path:
    (path / "Contents/MacOS").mkdir(parents=True)
    (path / "Contents/Info.plist").write_bytes(_info(version, bundle_id))
    (path / "Contents/MacOS/Mnema").write_text(f"#!/bin/sh\n# {version}\n")
    return path


def _zip(version: str, bundle_id: str = "com.mnema.bar") -> bytes:
    """What ditto -c -k --keepParent Mnema.app makes: the bundle folder at the top."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as package:
        package.writestr("Mnema.app/Contents/Info.plist", _info(version, bundle_id))
        package.writestr("Mnema.app/Contents/MacOS/Mnema", f"#!/bin/sh\n# {version}\n")
    return out.getvalue()


def _version_of(app: Path) -> str:
    return plistlib.loads((app / "Contents/Info.plist").read_bytes())["CFBundleShortVersionString"]


@pytest.fixture
def mac(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("MNEMA_UPDATE_URL", "https://releases.test/latest")
    monkeypatch.setattr(update.platform, "mac_ver", lambda: ("15.1", ("", "", ""), "arm64"))
    applications = tmp_path / "Applications"
    bundle = _make_app(applications / "Mnema.app", __version__)
    monkeypatch.setenv("MNEMA_APP_BUNDLE", str(bundle))
    state = {
        "bundle": bundle,
        "version": NEW,
        "zip": _zip(NEW),
        "calls": [],
        "commands": [],
        "codesign": 0,
    }

    def handler(request):
        state["calls"].append(request.url.path)
        if request.url.path == "/latest":
            mac_block = {
                "archive": f"https://github.com/fozt/mnema/releases/download/v{NEW}/"
                f"Mnema-{NEW}-mac.zip",
                "sha256": state.get("sha256") or hashlib.sha256(state["zip"]).hexdigest(),
                "size": state.get("size") or len(state["zip"]),
                "min_macos": state.get("min_macos", "14.0"),
            }
            manifest = {
                "version": state["version"],
                "archive": f"https://github.com/fozt/mnema/releases/download/v{NEW}/mnema.tgz",
                "sha256": "0" * 64,
            }
            if not state.get("no_mac"):
                manifest["mac"] = mac_block
            return httpx.Response(200, json=manifest)
        return httpx.Response(200, content=state["zip"])

    def run(cmd, **kwargs):
        state["commands"].append(Path(cmd[0]).name)
        tool = Path(cmd[0]).name
        if tool == "ditto":
            try:
                with zipfile.ZipFile(cmd[-2]) as package:
                    package.extractall(cmd[-1])
            except zipfile.BadZipFile:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="not a zip")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if tool == "codesign":
            return subprocess.CompletedProcess(cmd, state["codesign"], stdout="", stderr="bad")
        assert tool == "xattr", cmd
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="No such xattr")

    monkeypatch.setattr(update, "TRANSPORT", httpx.MockTransport(handler))
    monkeypatch.setattr(subprocess, "run", run)
    return state


def _left_over(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir() if p.name != "Mnema.app")


def test_update_swaps_the_bundle_and_keeps_the_previous_until_next_start(mac):
    bundle = mac["bundle"]
    steps = []

    result = update.apply(on=lambda share, message: steps.append((share, message)))

    assert result.changed and result.version == NEW and result.bundle == str(bundle)
    assert _version_of(bundle) == NEW
    assert _version_of(bundle.parent / ".Mnema-previous.app") == __version__
    assert _left_over(bundle.parent) == [".Mnema-previous.app"]  # no download folders left
    assert mac["calls"][-1] == f"/fozt/mnema/releases/download/v{NEW}/Mnema-{NEW}-mac.zip"
    assert "codesign" in mac["commands"] and steps[-1] == (None, f"Устанавливаю Mnema {NEW}…")

    (bundle.parent / ".mnema-update-abc").mkdir()  # an interrupted attempt
    update.drop_previous_bundle()

    assert _left_over(bundle.parent) == [] and _version_of(bundle) == NEW


def test_next_update_replaces_an_earlier_previous_bundle(mac):
    _make_app(mac["bundle"].parent / ".Mnema-previous.app", "0.0.1")

    update.apply()

    assert _version_of(mac["bundle"].parent / ".Mnema-previous.app") == __version__


@pytest.mark.parametrize(
    ("damage", "error"),
    [
        ({"sha256": "0" * 64}, "скачалось с ошибкой"),
        ({"size": 10}, "превышает допустимый размер"),
        ({"zip": _zip(NEW, bundle_id="com.other.app")}, "не приложение Mnema"),
        ({"zip": _zip("98.0.0")}, "другая версия Mnema"),
        ({"codesign": 3}, "Подпись новой версии"),
        ({"zip": b"not a zip"}, "Неверное содержимое"),
    ],
)
def test_a_bad_package_leaves_the_running_bundle_alone(mac, damage, error):
    mac.update(damage)

    with pytest.raises(MediaError, match=error):
        update.apply()

    assert _version_of(mac["bundle"]) == __version__
    assert _left_over(mac["bundle"].parent) == []


@pytest.mark.parametrize("version", [__version__, "0.0.1"])
def test_same_or_older_release_downloads_nothing(mac, version):
    mac["version"] = version

    assert not update.apply().changed
    assert mac["calls"] == ["/latest"] and mac["commands"] == []


def test_release_without_the_mac_app_is_not_offered_to_the_bundle(mac):
    mac["no_mac"] = True

    assert not update.check().any
    assert not update.apply().changed and mac["calls"] == ["/latest", "/latest"]


def test_command_does_not_replace_the_bundle_of_a_running_app(mac):
    import fcntl

    from mnema.bar import app

    lock = Path.home() / ".cache" / "mnema" / "bar.lock"
    lock.parent.mkdir(parents=True)
    with lock.open("w") as running:  # the app holds it while it runs
        fcntl.flock(running, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert app.running_elsewhere()
        with pytest.raises(MediaError, match="Mnema запущена"):
            update.apply(terminal=True)
    assert _version_of(mac["bundle"]) == __version__ and mac["commands"] == []
    assert not app.running_elsewhere() and update.apply(terminal=True).changed


def test_bundle_in_a_folder_it_cannot_write_points_to_the_release_page(mac):
    if os.geteuid() == 0:
        pytest.skip("root writes anywhere")
    folder = mac["bundle"].parent
    folder.chmod(0o555)
    try:
        with pytest.raises(MediaError, match="странице выпуска") as refused:
            update.apply()
    finally:
        folder.chmod(0o755)
    assert RELEASE_PAGE in str(refused.value)
    assert mac["calls"] == ["/latest"] and _version_of(mac["bundle"]) == __version__


def test_translocated_bundle_asks_to_move_to_applications(mac, monkeypatch, tmp_path):
    bundle = _make_app(tmp_path / "private/var/folders/x/AppTranslocation/ABC/d/Mnema.app", "0.1")
    monkeypatch.setenv("MNEMA_APP_BUNDLE", str(bundle))

    with pytest.raises(MediaError, match="«Программы»") as refused:
        update.apply()

    assert RELEASE_PAGE in str(refused.value)
    assert mac["calls"] == ["/latest"] and _left_over(bundle.parent) == []


def test_older_macos_than_the_release_needs_is_refused(mac):
    mac["min_macos"] = "26.0"

    with pytest.raises(MediaError, match="macOS 26.0"):
        update.apply()

    assert mac["calls"] == ["/latest"]


def test_bundled_cli_finds_its_bundle_without_the_app_environment(mac, monkeypatch):
    monkeypatch.delenv("MNEMA_APP_BUNDLE")
    monkeypatch.setattr(sys, "prefix", str(mac["bundle"] / "Contents/Resources/python"))

    assert update.apply().bundle == str(mac["bundle"])
    assert _version_of(mac["bundle"]) == NEW


def test_app_shows_the_download_share_then_relaunches(mac, monkeypatch, tmp_path):
    """The Mac zip arrives in pieces: the app's environment carries the share of each, the
    steps after the download carry none. Only the update's own notices count: the server
    check and the recognition list notify from their threads at any moment."""
    data = mac["zip"]
    pieces = [data[i : i + len(data) // 3 + 1] for i in range(0, len(data), len(data) // 3 + 1)]
    handler = update.TRANSPORT.handler

    def chunked(request):
        if request.url.path.endswith("-mac.zip"):
            return httpx.Response(200, content=iter(pieces))
        return handler(request)

    monkeypatch.setattr(update, "TRANSPORT", httpx.MockTransport(chunked))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: None)
    seen = []

    def changed() -> None:
        if threading.current_thread().name == "mnema-bar-update":
            seen.append(bar.menu_env())

    bar = BarRuntime(on_change=changed, prefs_file=tmp_path / "bar.json")

    bar.handle("update")
    for thread in threading.enumerate():
        if thread.name == "mnema-bar-update":
            thread.join(timeout=10)

    downloading = [e.update_progress for e in seen if e.update_step == f"Скачиваю Mnema {NEW}…"]
    assert downloading[0] is None and downloading[-1] == 1.0 and len(downloading) == 4
    assert [e.update_progress for e in seen if e.update_step == f"Устанавливаю Mnema {NEW}…"] == [
        None
    ]
    assert bar.quit_requested and _version_of(mac["bundle"]) == NEW
    assert bar.menu_env().update_step == "" and bar.menu_env().update_progress is None


def test_relaunch_opens_the_bundle_that_holds_the_new_version(monkeypatch, tmp_path):
    bundle = tmp_path / "Applications/Mnema.app"
    monkeypatch.setattr(runtime._platform, "system", lambda: "Darwin")
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: calls.append((a, kw)))
    bar = BarRuntime(prefs_file=tmp_path / "bar.json")

    bar._relaunch(bundle=str(bundle))

    assert calls[0][0][0][-1] == f"sleep 3; /usr/bin/open {bundle}"
    assert bar.quit_requested


def test_relaunch_of_a_new_bundle_waits_for_recording(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: calls.append((a, kw)))
    bar = BarRuntime(prefs_file=tmp_path / "bar.json")
    bar.state.phase = Phase.RECORDING

    bar._relaunch(bundle=str(tmp_path / "Mnema.app"))

    assert not calls and not bar.quit_requested
    assert "Перезапустите Mnema после" in bar.state.notice


@pytest.mark.skipif(sys.platform != "darwin", reason="ditto and codesign are macOS tools")
def test_real_signed_bundle_goes_through_ditto_and_codesign(monkeypatch, tmp_path):
    """The real tools on a tiny ad-hoc signed bundle: the zip of build_mac_app.py's shape."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("MNEMA_UPDATE_URL", "https://releases.test/latest")
    build = tmp_path / "build"
    for name, identifier in ((NEW, "com.mnema.bar"), ("wrong-id", "com.other.app")):
        app = _make_app(build / name / "Mnema.app", NEW)  # Info.plist says Mnema in both
        shutil.copy("/usr/bin/true", app / "Contents/MacOS/Mnema")
        subprocess.run(
            ["codesign", "-f", "-s", "-", "-i", identifier,
             f"-r=designated => identifier \"{identifier}\"", str(app)],
            check=True, capture_output=True,
        )  # fmt: skip
        subprocess.run(
            ["ditto", "-c", "-k", "--keepParent", str(app), str(build / f"{name}.zip")],
            check=True,
        )
    bundle = _make_app(tmp_path / "Applications/Mnema.app", __version__)
    monkeypatch.setenv("MNEMA_APP_BUNDLE", str(bundle))
    served = {"zip": (build / f"{NEW}.zip").read_bytes()}

    def handler(request):
        data = served["zip"]
        if request.url.path == "/latest":
            return httpx.Response(
                200,
                json={
                    "version": NEW,
                    "archive": "https://releases.test/source.tgz",
                    "sha256": "0" * 64,
                    "mac": {
                        "archive": "https://releases.test/Mnema-mac.zip",
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "size": len(data),
                    },
                },
            )
        return httpx.Response(200, content=data)

    monkeypatch.setattr(update, "TRANSPORT", httpx.MockTransport(handler))
    served["zip"] = (build / "wrong-id.zip").read_bytes()  # signed, but not as Mnema
    with pytest.raises(MediaError, match="Подпись новой версии"):
        update.apply()
    assert _version_of(bundle) == __version__

    served["zip"] = (build / f"{NEW}.zip").read_bytes()
    assert update.apply().bundle == str(bundle)
    assert _version_of(bundle) == NEW
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(bundle)], check=True)
