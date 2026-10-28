"""Release scripts: the version bump and the GitHub release manifests."""

import hashlib
import json
import plistlib
import subprocess
import tomllib
import zipfile
from pathlib import Path

import pytest

from scripts import build_release, bump_version


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    (root / "mnema").mkdir(parents=True)
    (root / "mnema/__init__.py").write_text('"""Mnema."""\n\n__version__ = "0.2.21"\n')
    (root / "pyproject.toml").write_text(
        '[project]\nname = "mnema"\nversion = "0.2.21"\n\n[tool.ruff]\ntarget-version = "py312"\n'
    )
    (root / "uv.lock").write_text(
        'version = 1\n\n[[package]]\nname = "litellm"\nversion = "1.80.0"\n\n'
        '[[package]]\nname = "mnema"\nversion = "0.2.21"\nsource = { editable = "." }\n'
    )
    (root / "README.md").write_text("Mnema\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "mnema"], check=True)
    return root


def versions(root):
    lock = tomllib.loads((root / "uv.lock").read_text())
    return (
        tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"],
        (root / "mnema/__init__.py").read_text().split('"')[-2],
        {package["name"]: package["version"] for package in lock["package"]},
    )


def test_bump_sets_the_version_in_all_three_files(source):
    bump_version.bump(source, "0.3.0")

    assert versions(source) == ("0.3.0", "0.3.0", {"litellm": "1.80.0", "mnema": "0.3.0"})
    assert 'target-version = "py312"' in (source / "pyproject.toml").read_text()
    assert bump_version.current(source) == "0.3.0"


@pytest.mark.parametrize("version", ["0.2.21", "0.2.9", "0.1.99"])
def test_bump_never_goes_back(source, version):
    with pytest.raises(ValueError, match="above the current 0.2.21"):
        bump_version.bump(source, version)
    assert versions(source)[0] == "0.2.21"


@pytest.mark.parametrize(
    "version", ["0.3", "0.3.0.1", "v0.3.0", "0.100.0", "0.3.10000", "0.03.0", "01.3.0", ""]
)
def test_bump_rejects_versions_the_phone_cannot_encode(source, version):
    with pytest.raises(ValueError, match="X.Y.Z"):
        bump_version.bump(source, version)
    assert versions(source)[0] == "0.2.21"


def test_bump_changes_nothing_when_a_file_has_no_version(source):
    (source / "uv.lock").write_text('[[package]]\nname = "litellm"\nversion = "1.80.0"\n')

    with pytest.raises(ValueError, match="uv.lock"):
        bump_version.bump(source, "0.3.0")
    assert versions(source)[:2] == ("0.2.21", "0.2.21")


def test_bump_command_reports_a_bad_version(capsys):
    assert bump_version.main(["0.3"]) == 1
    assert bump_version.main([]) == 2
    assert "X.Y.Z" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("version", "code"),
    [("0.2.8", 20008), ("0.3.0", 30000), ("0.2.9999", 29999), ("12.34.56", 12_340_056)],
)
def test_version_code_follows_the_gradle_formula(version, code):
    assert bump_version.version_code(version) == code


def test_release_manifest_points_at_the_github_release_of_the_tag(source, tmp_path):
    bump_version.bump(source, "0.3.0")

    archive, manifest = build_release.build(source, tmp_path / "dist", "v0.3.0")

    assert archive.name == "mnema-0.3.0.tar.gz"
    assert json.loads(manifest.read_text()) == {
        "version": "0.3.0",
        "archive": "https://github.com/fozt/mnema/releases/download/v0.3.0/mnema-0.3.0.tar.gz",
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
    }


def test_release_manifest_describes_the_mac_app_the_update_installs(source, tmp_path):
    from mnema import update

    bump_version.bump(source, "0.3.0")
    mac_zip = _mac_zip(tmp_path / "Mnema-0.3.0-mac.zip", "0.3.0")

    _archive, manifest = build_release.build(source, tmp_path / "dist", "v0.3.0", mac_zip=mac_zip)

    data = json.loads(manifest.read_text())
    assert data["mac"] == {
        "archive": "https://github.com/fozt/mnema/releases/download/v0.3.0/Mnema-0.3.0-mac.zip",
        "sha256": hashlib.sha256(mac_zip.read_bytes()).hexdigest(),
        "size": mac_zip.stat().st_size,
        "min_macos": "14.2",
    }
    release = update.Release.model_validate(data)
    assert release.mac is not None and release.mac.size == mac_zip.stat().st_size


def _mac_zip(path: Path, version: str) -> Path:
    info = {"CFBundleShortVersionString": version, "LSMinimumSystemVersion": "14.2"}
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("Mnema.app/Contents/Info.plist", plistlib.dumps(info))
        package.writestr("Mnema.app/Contents/MacOS/Mnema", "#!/bin/sh\n")
    return path


@pytest.mark.parametrize(
    ("name", "inside"), [("Mnema-0.2.9-mac.zip", "0.2.9"), ("Mnema-0.3.0-mac.zip", "0.2.9")]
)
def test_release_refuses_the_mac_app_of_another_version(source, tmp_path, name, inside):
    bump_version.bump(source, "0.3.0")
    mac_zip = _mac_zip(tmp_path / name, inside)

    with pytest.raises(ValueError, match="0.2.9"):
        build_release.build(source, tmp_path / "dist", "v0.3.0", mac_zip=mac_zip)
    assert not (tmp_path / "dist").exists()


@pytest.mark.parametrize("tag", ["0.2.21", "v0.2", "v0.3.0"])
def test_release_needs_the_tag_of_the_checked_out_version(source, tmp_path, tag):
    with pytest.raises(ValueError):
        build_release.build(source, tmp_path / "dist", tag)
    assert not (tmp_path / "dist").exists()


def test_android_manifest_describes_the_apk_of_the_release(tmp_path):
    apk = tmp_path / "mnema-0.3.0.apk"
    apk.write_bytes(b"signed apk")

    manifest = build_release.android_manifest(tmp_path / "dist", "v0.3.0", apk)

    assert manifest.name == "mnema-android.json"
    assert json.loads(manifest.read_text()) == {
        "version": "0.3.0",
        "version_code": 30000,
        "apk": "https://github.com/fozt/mnema/releases/download/v0.3.0/mnema-0.3.0.apk",
        "sha256": hashlib.sha256(b"signed apk").hexdigest(),
        "size": len(b"signed apk"),
        "min_sdk": 34,
    }


def test_build_command_writes_all_manifests(source, tmp_path, monkeypatch):
    bump_version.bump(source, "0.3.0")
    apk = tmp_path / "mnema-0.3.0.apk"
    apk.write_bytes(b"signed apk")
    monkeypatch.setattr(build_release, "__file__", str(source / "scripts/build_release.py"))

    mac_zip = _mac_zip(tmp_path / "Mnema-0.3.0-mac.zip", "0.3.0")
    dist = tmp_path / "dist"

    build_release.main(
        ["v0.3.0", "--dist", str(dist), "--apk", str(apk), "--mac-zip", str(mac_zip)]
    )

    assert sorted(p.name for p in dist.iterdir()) == [
        "mnema-0.3.0.tar.gz",
        "mnema-android.json",
        "mnema-release.json",
    ]
    mac = json.loads((dist / "mnema-release.json").read_text())["mac"]
    assert mac["size"] == mac_zip.stat().st_size


def test_build_command_writes_into_dist_of_the_checkout_by_default(source, monkeypatch):
    bump_version.bump(source, "0.3.0")
    monkeypatch.setattr(build_release, "__file__", str(source / "scripts/build_release.py"))

    build_release.main(["v0.3.0"])

    assert sorted(p.name for p in (source / "dist").iterdir()) == [
        "mnema-0.3.0.tar.gz",
        "mnema-release.json",
    ]


def test_android_manifest_min_sdk_is_the_one_the_app_is_built_with():
    from pathlib import Path

    gradle = (Path(__file__).parents[1] / "android/app/build.gradle.kts").read_text()
    assert f"minSdk = {build_release.ANDROID_MIN_SDK}\n" in gradle
