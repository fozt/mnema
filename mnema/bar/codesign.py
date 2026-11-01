"""Mnema.app as code and runtime, and its ad-hoc signature: scripts/build_mac_app.py builds with
it, the code-only update (mnema/update.py) applies with it.

The code (CODE_PATHS) is what a release changes every time: the Swift panel, Info.plist, the
resources and the mnema package. The runtime is the rest of the bundle: Python with its packages,
ffmpeg and mnema-asr. Its key (RUNTIME in Info.plist) names the runtime the code was built for: a
code-only update goes only to a bundle with the same key. Signatures (_CodeSignature) are neither.

Standard library only: the build and the release scripts run without the app's packages. Signing
calls /usr/bin/codesign alone: a Mac without Command Line Tools has nothing else.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
from collections.abc import Iterator
from fnmatch import fnmatchcase
from pathlib import Path

# Paths from the bundle; a trailing "/" is a folder with everything in it
CODE_PATHS = (
    "Contents/MacOS/Mnema",
    "Contents/Info.plist",
    "Contents/Resources/*.lproj/",
    "Contents/Resources/Mnema.icns",
    "Contents/Resources/licenses/",
    # names the mnema command wheel of this very release
    "Contents/Resources/bin/mnema-mcp",
    "Contents/Resources/python/lib/python3.12/site-packages/mnema/",
    "Contents/Resources/python/lib/python3.12/site-packages/mnema-*.dist-info/",
)
SIGNATURE = "_CodeSignature"
RUNTIME = "MnemaRuntime"  # Info.plist: the runtime key
RUNTIME_INPUTS = "MnemaRuntimeInputs"  # Info.plist: what the runtime was built from
MAIN = "Contents/MacOS/Mnema"
CODESIGN = "/usr/bin/codesign"
MACHO_MAGIC = {
    b"\xcf\xfa\xed\xfe",  # 64-bit little endian
    b"\xce\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",  # universal
    b"\xca\xfe\xba\xbf",
}
_BATCH = 200


def is_code(path: str) -> bool:
    """path: POSIX, relative to the bundle."""
    parts = path.split("/")
    for pattern in CODE_PATHS:
        folder = pattern.endswith("/")
        expected = pattern.rstrip("/").split("/")
        if len(parts) > len(expected) if folder else len(parts) == len(expected):
            if all(fnmatchcase(part, want) for part, want in zip(parts, expected, strict=False)):
                return True
    return False


def _entries(app: Path) -> Iterator[tuple[str, Path]]:
    """Files and links of the bundle, sorted by path; folders do not count, signatures neither."""
    found = []
    for folder, dirs, files in os.walk(app):
        here = Path(folder)
        dirs[:] = [name for name in dirs if name != SIGNATURE]
        links = [name for name in dirs if (here / name).is_symlink()]  # not walked into
        for name in [*files, *links]:
            path = here / name
            found.append((path.relative_to(app).as_posix(), path))
    yield from sorted(found)


def code_files(app: Path) -> list[str]:
    """The code of the bundle, POSIX paths from it, sorted."""
    return [rel for rel, _path in _entries(app) if is_code(rel)]


def runtime_files(app: Path) -> dict[str, str]:
    """The runtime by file: each file or link by its path → its type, the executable bits of a
    file and the sha256 of its content, or the target of a link."""
    files = {}
    for rel, path in _entries(app):
        if is_code(rel):
            continue
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            files[rel] = f"link\0\0{os.readlink(path)}"
        elif stat.S_ISREG(mode):
            files[rel] = f"file\0{mode & 0o111:o}\0{_sha256(path)}"
        else:
            raise ValueError(f"{rel}: neither a file nor a link")
    return files


def runtime_key(app: Path) -> str:
    """sha256 over runtime_files, in path order."""
    digest = hashlib.sha256()
    for rel, entry in runtime_files(app).items():
        digest.update(os.fsencode(f"{rel}\0{entry}\n"))
    return digest.hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as data:
        while chunk := data.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def remove_code(app: Path) -> None:
    """Remove the code of the bundle; the runtime stays."""
    for pattern in CODE_PATHS:
        for path in app.glob(pattern.rstrip("/")):
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()


def is_macho(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    with path.open("rb") as data:
        return data.read(4) in MACHO_MAGIC


def macho_files(tree: Path) -> list[Path]:
    return [path for path in tree.rglob("*") if is_macho(path)]


def _codesign(*args: str | Path) -> None:
    cmd = [CODESIGN, "--force", "--sign", "-", "--timestamp=none", *map(str, args)]
    done = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if done.returncode:
        raise subprocess.CalledProcessError(done.returncode, cmd, done.stdout, done.stderr)


def sign_files(files: list[Path]) -> None:
    """Ad-hoc, the deepest first: what the bundle signature seals must be signed already."""
    ordered = sorted(files, key=lambda path: len(path.parts), reverse=True)
    for start in range(0, len(ordered), _BATCH):
        _codesign(*ordered[start : start + _BATCH])


def sign_bundle(app: Path, identifier: str) -> None:
    """The bundle and its main executable, after every Mach-O inside: the designated requirement
    by identifier keeps macOS permissions across updates."""
    _codesign(f'-r=designated => identifier "{identifier}"', app)
