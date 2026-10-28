"""Build the release files of a version from this checkout: the source archive and the manifests.

Usage: python -m scripts.build_release vX.Y.Z [--dist DIR] [--apk PATH] [--mac-zip PATH]

Writes DIR/mnema-X.Y.Z.tar.gz and DIR/mnema-release.json (the channel of `mnema update`); with
--mac-zip the manifest also describes the Mac app (Mnema-X.Y.Z-mac.zip from
scripts/build_mac_app.py); with --apk also DIR/mnema-android.json (the channel of the phone).
Nothing is uploaded: the manifests point at the assets of the GitHub release vX.Y.Z, and
`releases/latest/download/` serves the manifests of the latest release.
"""

from __future__ import annotations

import argparse
import ast
import gzip
import hashlib
import json
import plistlib
import tarfile
import tomllib
import zipfile
from pathlib import Path

from .bump_version import parse, version_code

RELEASES = "https://github.com/fozt/mnema/releases"
ANDROID_MIN_SDK = 34  # minSdk in android/app/build.gradle.kts


def _metadata(info: tarfile.TarInfo) -> tarfile.TarInfo:
    """Rebuilding the same tag gives the same archive and SHA-256."""
    info.mtime = info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mode = 0o755 if info.mode & 0o111 else 0o644
    info.pax_headers = {}
    return info


def _tracked(root: Path, folder: str) -> list[Path]:
    import subprocess

    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--", folder],
        check=True, capture_output=True,
    ).stdout  # fmt: skip
    return sorted(root / name for name in out.decode().split("\0") if name)


def _version_of(tag: str) -> str:
    if not tag.startswith("v"):
        raise ValueError(f"A release tag looks like v1.2.3: {tag}")
    version = tag.removeprefix("v")
    parse(version)
    return version


def _write_json(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data, indent=2) + "\n")
    return path


def _asset(path: Path, url: str) -> dict:
    digest = hashlib.sha256()
    with path.open("rb") as data:
        while chunk := data.read(1 << 20):
            digest.update(chunk)
    return {"archive": url, "sha256": digest.hexdigest(), "size": path.stat().st_size}


def _mac_app(mac_zip: Path, version: str) -> dict:
    """The Info.plist inside Mnema-X.Y.Z-mac.zip: the app of this version, and the macOS it
    needs (LSMinimumSystemVersion) — the update must not offer it to an older system."""
    try:
        with zipfile.ZipFile(mac_zip) as package:
            info = plistlib.loads(package.read("Mnema.app/Contents/Info.plist"))
    except (OSError, KeyError, zipfile.BadZipFile, plistlib.InvalidFileException) as exc:
        raise ValueError(f"{mac_zip.name} holds no Mnema.app/Contents/Info.plist") from exc
    if info.get("CFBundleShortVersionString") != version:
        raise ValueError(f"{mac_zip.name} holds Mnema {info.get('CFBundleShortVersionString')}")
    if not (minimum := info.get("LSMinimumSystemVersion")):
        raise ValueError(f"{mac_zip.name}: Info.plist has no LSMinimumSystemVersion")
    return {"min_macos": str(minimum)}


def build(
    root: Path,
    output: Path,
    tag: str,
    package_url: str | None = None,
    mac_zip: Path | None = None,
) -> list[Path]:
    """package_url — where the release assets are served; by default the GitHub release of tag.
    mac_zip — Mnema-X.Y.Z-mac.zip of the same version, described by the manifest's mac block."""
    version = _version_of(tag)
    package_url = package_url or f"{RELEASES}/download/{tag}"
    project = tomllib.loads((root / "pyproject.toml").read_text())
    module = ast.parse((root / "mnema/__init__.py").read_text())
    declared = next(
        ast.literal_eval(node.value)
        for node in module.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets
        )
    )
    if mac_zip is not None and mac_zip.name != f"Mnema-{version}-mac.zip":
        raise ValueError(f"The Mac app of {tag} is Mnema-{version}-mac.zip: {mac_zip.name}")
    mac_app = _mac_app(mac_zip, version) if mac_zip is not None else {}
    lock = tomllib.loads((root / "uv.lock").read_text())
    locked = next(package["version"] for package in lock["package"] if package["name"] == "mnema")
    if {version, project["project"]["version"], declared, locked} != {version}:
        # The Russian text is matched by tests/test_update.py (owned by the installer work).
        raise ValueError("Тег, пакет, исходники и uv.lock должны иметь одну версию")
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"mnema-{version}.tar.gz"
    files = [root / name for name in ("pyproject.toml", "uv.lock", "README.md")]
    files += _tracked(root, "mnema")  # git files only: a stray mnema/.env with keys stays out
    with (
        archive.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w") as package,
    ):
        for path in files:
            relative = path.relative_to(root)
            if (
                path.is_file()
                and not path.is_symlink()
                and not any(
                    part in {"__pycache__", ".build", ".swiftpm"} for part in relative.parts
                )
                and path.suffix != ".pyc"
            ):
                package.add(path, arcname=str(relative), recursive=False, filter=_metadata)
    release: dict = {
        "version": version,
        "archive": f"{package_url}/{archive.name}",
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
    }
    if mac_zip is not None:
        release["mac"] = {
            **_asset(mac_zip, f"{package_url}/{mac_zip.name}"),
            **mac_app,
        }
    manifest = _write_json(output / "mnema-release.json", release)
    return [archive, manifest]


def android_manifest(output: Path, tag: str, apk: Path, package_url: str | None = None) -> Path:
    """mnema-android.json for the signed APK of the release (android/scripts/release-apk.sh)."""
    version = _version_of(tag)
    package_url = package_url or f"{RELEASES}/download/{tag}"
    data = apk.read_bytes()
    output.mkdir(parents=True, exist_ok=True)
    return _write_json(
        output / "mnema-android.json",
        {
            "version": version,
            "version_code": version_code(version),
            "apk": f"{package_url}/{apk.name}",
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "min_sdk": ANDROID_MIN_SDK,
        },
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m scripts.build_release")
    parser.add_argument("tag", help="release tag, vX.Y.Z")
    parser.add_argument(
        "--dist", type=Path, help="output folder, by default dist/ of this checkout"
    )
    parser.add_argument("--apk", type=Path, help="signed APK of this version")
    parser.add_argument("--mac-zip", type=Path, help="Mnema-X.Y.Z-mac.zip of this version")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    dist = args.dist or root / "dist"
    for path in build(root, dist, args.tag, mac_zip=args.mac_zip):
        print(path)
    if args.apk:
        print(android_manifest(dist, args.tag, args.apk))


if __name__ == "__main__":
    main()
