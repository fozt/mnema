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
import tarfile
import threading
import zipfile
from pathlib import Path

import httpx
import pytest

from mnema import __version__, update
from mnema.bar import codesign, runtime
from mnema.bar.runtime import BarRuntime
from mnema.bar.state import Phase
from mnema.core.base import MediaError
from scripts import build_mac_app

NEW = "99.0.0"
RELEASE_PAGE = f"https://github.com/fozt/mnema/releases/tag/v{NEW}"
FULL_ZIP = f"/fozt/mnema/releases/download/v{NEW}/Mnema-{NEW}-mac.zip"
CODE_TAR = f"/fozt/mnema/releases/download/v{NEW}/Mnema-{NEW}-mac-code.tar.gz"
SITE = "Contents/Resources/python/lib/python3.12/site-packages"


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
            if "code" in state:
                mac_block["code"] = {
                    "archive": f"https://github.com/fozt/mnema/releases/download/v{NEW}/"
                    f"Mnema-{NEW}-mac-code.tar.gz",
                    "sha256": state.get("code_sha256") or hashlib.sha256(state["code"]).hexdigest(),
                    "size": len(state["code"]),
                    "runtime": state["runtime"],
                }
            if not state.get("no_mac"):
                manifest["mac"] = mac_block
            return httpx.Response(200, json=manifest)
        if request.url.path.endswith("-mac-code.tar.gz"):
            return httpx.Response(200, content=state["code"])
        return httpx.Response(200, content=state["zip"])

    def run(cmd, **kwargs):
        state["commands"].append(Path(cmd[0]).name)
        tool = Path(cmd[0]).name
        if tool == "cp":  # cp -c [-R] SOURCE TARGET: an APFS clone
            if state.get("no_clone"):
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="not supported")
            source, target = Path(cmd[-2]), Path(cmd[-1])
            if source.is_dir():
                shutil.copytree(source, target, symlinks=True)
            else:
                shutil.copy2(source, target)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if tool == "codesign" and "--force" in cmd:
            return subprocess.CompletedProcess(cmd, state.get("sign", 0), stdout="", stderr="")
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


def _mnema_app(path: Path, version: str, *, asr: str = "asr 1", python: str = "") -> Path:
    """A bundle of build_mac_app.py's layout, sealed by it: the code of the version over a
    runtime (mnema-asr, Python and its packages)."""
    shutil.rmtree(path, ignore_errors=True)
    files = {
        "Contents/MacOS/Mnema": f"#!/bin/sh\n# {version}\n",
        "Contents/MacOS/mnema-asr": f"#!/bin/sh\n# {asr}\n",
        "Contents/Resources/en.lproj/InfoPlist.strings": f"/* {version} */\n",
        "Contents/Resources/python/bin/python3.12": python or "#!/bin/sh\n",
        f"{SITE}/httpx/__init__.py": "# httpx\n",
        f"{SITE}/mnema/__init__.py": f'__version__ = "{version}"\n',
        f"{SITE}/mnema-{version}.dist-info/METADATA": f"Name: mnema\nVersion: {version}\n",
    }
    for rel, text in files.items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(text)
    for rel in ("MacOS/Mnema", "MacOS/mnema-asr", "Resources/python/bin/python3.12"):
        (path / "Contents" / rel).chmod(0o755)
    (path / "Contents/Resources/python/bin/python3").symlink_to("python3.12")
    (path / "Contents/Info.plist").write_bytes(_info(version))
    if sys.platform == "darwin":  # real Mach-O: codesign seals them
        shutil.copy("/usr/bin/true", path / "Contents/MacOS/Mnema")
        shutil.copy(
            "/usr/bin/false" if asr == "asr 1" else "/bin/ls", path / "Contents/MacOS/mnema-asr"
        )
    build_mac_app.seal(path, inputs="test inputs")
    return path


def _runtime_of(app: Path) -> str:
    return plistlib.loads((app / "Contents/Info.plist").read_bytes())[codesign.RUNTIME]


def _code_release(mac, tmp_path: Path, *, asr: str = "asr 1") -> Path:
    """The installed bundle, and the release's code archive built over the given runtime."""
    _mnema_app(mac["bundle"], __version__)
    new = _mnema_app(tmp_path / "build" / "Mnema.app", NEW, asr=asr)
    mac["code"] = build_mac_app.code_archive(new, NEW).read_bytes()
    mac["runtime"] = _runtime_of(new)
    return new


def _with_member(archive: bytes, name: str, data: bytes) -> bytes:
    out = io.BytesIO()
    with (
        tarfile.open(fileobj=io.BytesIO(archive)) as source,
        tarfile.open(fileobj=out, mode="w:gz") as target,
    ):
        for member in source:
            target.addfile(member, source.extractfile(member))
        info = tarfile.TarInfo(name)
        info.size = len(data)
        target.addfile(info, io.BytesIO(data))
    return out.getvalue()


def test_same_runtime_takes_only_the_code(mac, tmp_path):
    bundle = mac["bundle"]
    _code_release(mac, tmp_path)
    asr = (bundle / "Contents/MacOS/mnema-asr").read_bytes()

    result = update.apply()

    assert result.bundle == str(bundle) and _version_of(bundle) == NEW
    assert CODE_TAR in mac["calls"] and FULL_ZIP not in mac["calls"]
    site = bundle / SITE
    assert (site / "mnema/__init__.py").read_text() == f'__version__ = "{NEW}"\n'
    assert [p.name for p in site.glob("mnema-*.dist-info")] == [f"mnema-{NEW}.dist-info"]
    assert (bundle / "Contents/MacOS/mnema-asr").read_bytes() == asr
    assert os.readlink(bundle / "Contents/Resources/python/bin/python3") == "python3.12"
    assert os.access(bundle / "Contents/MacOS/Mnema", os.X_OK)
    assert _runtime_of(bundle) == codesign.runtime_key(bundle)
    assert _version_of(bundle.parent / ".Mnema-previous.app") == __version__
    assert _left_over(bundle.parent) == [".Mnema-previous.app"]


@pytest.mark.parametrize("case", ["another runtime", "no runtime key", "no clones"])
def test_another_runtime_or_volume_takes_the_whole_app(mac, tmp_path, case):
    _code_release(mac, tmp_path, asr="asr 2" if case == "another runtime" else "asr 1")
    if case == "no runtime key":  # a bundle of 0.4
        (mac["bundle"] / "Contents/Info.plist").write_bytes(_info(__version__))
    mac["no_clone"] = case == "no clones"

    assert update.apply().changed and _version_of(mac["bundle"]) == NEW

    assert FULL_ZIP in mac["calls"] and CODE_TAR not in mac["calls"]


@pytest.mark.parametrize(
    "damage",
    ["checksum", "outside the code", "not a tar", "runtime changed here", "built for another",
     "signing"],
)  # fmt: skip
def test_a_code_package_that_fails_falls_back_to_the_whole_app(mac, tmp_path, damage):
    bundle = mac["bundle"]
    _code_release(mac, tmp_path, asr="asr 2" if damage == "built for another" else "asr 1")
    if damage == "checksum":
        mac["code_sha256"] = "0" * 64
    elif damage == "outside the code":
        mac["code"] = _with_member(mac["code"], "Contents/MacOS/mnema-asr", b"#!/bin/sh\n")
    elif damage == "not a tar":
        mac["code"] = b"not a tar"
    elif damage == "runtime changed here":
        (bundle / SITE / "httpx/__init__.py").write_text("# changed\n")
    elif damage == "built for another":  # the manifest names the installed runtime
        mac["runtime"] = _runtime_of(bundle)
    else:
        mac["sign"] = 1

    assert update.apply().bundle == str(bundle) and _version_of(bundle) == NEW

    assert CODE_TAR in mac["calls"] and mac["calls"][-1] == FULL_ZIP
    assert not (bundle / SITE / "mnema").exists()  # the whole app of the zip
    assert _left_over(bundle.parent) == [".Mnema-previous.app"]


def test_when_both_packages_fail_the_running_bundle_stays(mac, tmp_path):
    _code_release(mac, tmp_path)
    mac["sign"] = 1
    mac["codesign"] = 3

    with pytest.raises(MediaError, match="Подпись новой версии"):
        update.apply()

    assert _version_of(mac["bundle"]) == __version__ and _left_over(mac["bundle"].parent) == []
    assert _runtime_of(mac["bundle"]) == codesign.runtime_key(mac["bundle"])


@pytest.mark.skipif(sys.platform != "darwin", reason="cp -c, codesign and xattr are macOS tools")
def test_real_code_update_clones_the_runtime_and_signs_the_new_code(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("MNEMA_UPDATE_URL", "https://releases.test/latest")
    bundle = _mnema_app(tmp_path / "Applications/Mnema.app", __version__)
    monkeypatch.setenv("MNEMA_APP_BUNDLE", str(bundle))
    subprocess.run(
        [
            "xattr",
            "-w",
            "com.apple.quarantine",
            "0081;0;Safari;",
            str(bundle / "Contents/MacOS/mnema-asr"),
        ],
        check=True,
    )
    new = _mnema_app(tmp_path / "build/Mnema.app", NEW)
    code = build_mac_app.code_archive(new, NEW).read_bytes()
    asr = (bundle / "Contents/MacOS/mnema-asr").read_bytes()

    def handler(request):
        if request.url.path == "/latest":
            return httpx.Response(
                200,
                json={
                    "version": NEW,
                    "archive": "https://releases.test/source.tgz",
                    "sha256": "0" * 64,
                    "mac": {
                        "archive": "https://releases.test/Mnema-mac.zip",
                        "sha256": "0" * 64,  # never downloaded
                        "size": 1,
                        "code": {
                            "archive": "https://releases.test/Mnema-mac-code.tar.gz",
                            "sha256": hashlib.sha256(code).hexdigest(),
                            "size": len(code),
                            "runtime": _runtime_of(new),
                        },
                    },
                },
            )
        assert request.url.path == "/Mnema-mac-code.tar.gz"
        return httpx.Response(200, content=code)

    monkeypatch.setattr(update, "TRANSPORT", httpx.MockTransport(handler))

    assert update.apply().bundle == str(bundle)

    assert _version_of(bundle) == NEW
    assert (bundle / "Contents/MacOS/mnema-asr").read_bytes() == asr
    subprocess.run(
        [
            "codesign",
            "--verify",
            "--deep",
            "--strict",
            '-R=identifier "com.mnema.bar"',
            str(bundle),
        ],
        check=True,
    )
    quarantine = ["xattr", "-p", "com.apple.quarantine", str(bundle / "Contents/MacOS/mnema-asr")]
    assert subprocess.run(quarantine, capture_output=True).returncode != 0


def _wrapper_python() -> str:
    """The bundle's python3.12: this test environment, which has the mnema package."""
    return f'#!/bin/sh\nexec "{sys.executable}" "$@"\n'


@pytest.mark.skipif(sys.platform != "darwin", reason="cp -c, codesign and xattr are macOS tools")
def test_release_check_applies_the_code_with_the_previous_release_and_system_tools(tmp_path):
    """The previous release's own update code makes this version from its bundle and the code
    archive; the previous bundle is left as it was."""
    previous = _mnema_app(tmp_path / "previous/Mnema.app", __version__, python=_wrapper_python())
    new = _mnema_app(tmp_path / "build/Mnema.app", NEW, python=_wrapper_python())
    archive = build_mac_app.code_archive(new, NEW)
    key = _runtime_of(previous)

    build_mac_app.check_code_update(previous, new, archive, NEW)

    assert _version_of(previous) == __version__ and codesign.runtime_key(previous) == key
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(previous)], check=True)
