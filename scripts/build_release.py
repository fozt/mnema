"""Build the release files of a version from this checkout: the source archive and the manifests.

Usage: python -m scripts.build_release vX.Y.Z [--dist DIR] [--apk PATH] [--mac-zip PATH]
    [--mac-code PATH] [--wheel]

Writes DIR/mnema-X.Y.Z.tar.gz and DIR/mnema-release.json (the channel of `mnema update`); with
--mac-zip the manifest also describes the Mac app (Mnema-X.Y.Z-mac.zip from
scripts/build_mac_app.py), with --mac-code its code-only update (Mnema-X.Y.Z-mac-code.tar.gz);
with --wheel uv builds the wheel of the mnema command from the source archive
(mnema-X.Y.Z-py3-none-any.whl, the manifest's cli block) and DIR/mnema-X.Y.Z-constraints.txt,
the locked versions of its dependencies (the cli block names it too: uv tool install
--constraints <url>); with --apk also DIR/mnema-android.json (the channel of
the phone).
Nothing is uploaded: the manifests point at the assets of the GitHub release vX.Y.Z, and
`releases/latest/download/` serves the manifests of the latest release.
"""

from __future__ import annotations

import argparse
import ast
import gzip
import hashlib
import json
import os
import plistlib
import re
import subprocess
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path

from mnema.bar.codesign import MAIN, RUNTIME, is_code

from .bump_version import parse, version_code

RELEASES = "https://github.com/fozt/mnema/releases"
ANDROID_MIN_SDK = 34  # minSdk in android/app/build.gradle.kts
MAX_CODE_ARCHIVE = 64 * 1024 * 1024  # mnema.update takes no larger code archive
SOURCE_DATE_EPOCH = "1577836800"  # 2020-01-01: the wheel's file times, the same in every build
_KEY = re.compile(r"^[a-f0-9]{64}$")


def _metadata(info: tarfile.TarInfo) -> tarfile.TarInfo:
    """Rebuilding the same tag gives the same archive and SHA-256."""
    info.mtime = info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mode = 0o755 if info.mode & 0o111 else 0o644
    info.pax_headers = {}
    return info


def _tracked(root: Path, folder: str) -> list[Path]:
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
    return {"min_macos": str(minimum), RUNTIME: info.get(RUNTIME)}


def _mac_code(mac_code: Path, version: str, runtime: object) -> str:
    """The code archive is the code of the app in the zip, and nothing else: its Info.plist names
    the same runtime key. Returns the key."""
    if not (isinstance(runtime, str) and _KEY.fullmatch(runtime)):
        raise ValueError(f"The Mac app has no {RUNTIME} key in its Info.plist: no code archive")
    if mac_code.name != f"Mnema-{version}-mac-code.tar.gz":
        raise ValueError(f"The code of Mnema {version} is Mnema-{version}-mac-code.tar.gz")
    if mac_code.stat().st_size > MAX_CODE_ARCHIVE:
        raise ValueError(f"{mac_code.name} is larger than the update takes")
    with tarfile.open(mac_code, "r:gz") as package:
        names = []
        for member in package:
            if not member.isfile() or not is_code(member.name):
                raise ValueError(f"{mac_code.name}: {member.name} is not code of the app")
            names.append(member.name)
        if MAIN not in names or "Contents/Info.plist" not in names:
            raise ValueError(f"{mac_code.name} holds no whole code of the app")
        plist = package.extractfile("Contents/Info.plist")
        info = plistlib.loads(plist.read()) if plist else {}
    if info.get("CFBundleShortVersionString") != version:
        raise ValueError(f"{mac_code.name} holds Mnema {info.get('CFBundleShortVersionString')}")
    if info.get(RUNTIME) != runtime:
        raise ValueError(f"{mac_code.name} is built for another runtime than the app in the zip")
    return runtime


def _wheel(archive: Path, output: Path, version: str) -> tuple[Path, Path]:
    """The wheel of the mnema command, built by uv from the source archive (what is released, not
    the checkout), and the locked versions of its dependencies for uv tool --constraints."""
    wheel = output / f"mnema-{version}-py3-none-any.whl"
    constraints = output / f"mnema-{version}-constraints.txt"
    env = {**os.environ, "SOURCE_DATE_EPOCH": SOURCE_DATE_EPOCH}
    with tempfile.TemporaryDirectory() as temp:
        project = Path(temp) / "mnema"
        built = Path(temp) / "built"
        with tarfile.open(archive) as package:
            package.extractall(project, filter="data")
        subprocess.run(
            ["uv", "build", "--wheel", "--no-config", "--out-dir", str(built), str(project)],
            check=True, env=env,
        )  # fmt: skip
        if not (built / wheel.name).is_file():
            raise ValueError(f"uv built no {wheel.name}")
        wheel.unlink(missing_ok=True)
        (built / wheel.name).rename(wheel)
        locked = subprocess.run(
            ["uv", "export", "--frozen", "--no-dev", "--no-emit-project", "--no-header",
             "--no-hashes", "--extra", "cli", "--project", str(project)],
            check=True, env=env, capture_output=True, text=True,
        ).stdout  # fmt: skip
    constraints.write_text(locked)
    return wheel, constraints


def build(
    root: Path,
    output: Path,
    tag: str,
    package_url: str | None = None,
    mac_zip: Path | None = None,
    mac_code: Path | None = None,
    wheel: bool = False,
) -> list[Path]:
    """package_url — where the release assets are served; by default the GitHub release of tag.
    mac_zip — Mnema-X.Y.Z-mac.zip of the same version, described by the manifest's mac block;
    mac_code — the code archive of that app (mac.code). wheel — build the command's wheel and its
    constraints (cli)."""
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
    runtime = mac_app.pop(RUNTIME, None)
    if mac_code is not None:
        if mac_zip is None:
            raise ValueError("The code archive goes with the Mac app: --mac-zip")
        _mac_code(mac_code, version, runtime)
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
        if mac_code is not None:
            code = _asset(mac_code, f"{package_url}/{mac_code.name}")
            release["mac"]["code"] = {**code, "runtime": runtime}
    files = [archive]
    if wheel:
        command, constraints = _wheel(archive, output, version)
        release["cli"] = {
            "wheel": f"{package_url}/{command.name}",
            "sha256": hashlib.sha256(command.read_bytes()).hexdigest(),
            # uv tool install --constraints <url>: the locked versions, not the newest on PyPI
            "constraints": f"{package_url}/{constraints.name}",
        }
        files += [command, constraints]
    manifest = _write_json(output / "mnema-release.json", release)
    return [*files, manifest]


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
    parser.add_argument(
        "--mac-code", type=Path, help="Mnema-X.Y.Z-mac-code.tar.gz of that app (with --mac-zip)"
    )
    parser.add_argument(
        "--wheel",
        action="store_true",
        help="build the wheel of the mnema command and its constraints",
    )
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    dist = args.dist or root / "dist"
    files = build(
        root, dist, args.tag, mac_zip=args.mac_zip, mac_code=args.mac_code, wheel=args.wheel
    )
    for path in files:
        print(path)
    if args.apk:
        print(android_manifest(dist, args.tag, args.apk))


if __name__ == "__main__":
    main()
