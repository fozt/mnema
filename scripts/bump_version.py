"""Set the release version in pyproject.toml, mnema/__init__.py and uv.lock.

Usage: python -m scripts.bump_version X.Y.Z

Then commit the three files and tag the commit vX.Y.Z; pushing the tag publishes the release.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

# The Android versionCode is X*1000000 + Y*10000 + Z (android/app/build.gradle.kts), so the same
# version must fit it: Y below 100, Z below 10000, no leading zeros.
VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]?)\.(0|[1-9][0-9]{0,3})")

_FIELDS = {
    "pyproject.toml": (r'(?m)^version = "[^"]+"$', 'version = "{version}"'),
    "mnema/__init__.py": (r'(?m)^__version__ = "[^"]+"$', '__version__ = "{version}"'),
    "uv.lock": (
        r'(?m)(\[\[package\]\]\nname = "mnema"\n)version = "[^"]+"$',
        r'\g<1>version = "{version}"',
    ),
}


def parse(version: str) -> tuple[int, int, int]:
    match = VERSION.fullmatch(version)
    if not match:
        raise ValueError(
            f"The version must look like X.Y.Z with Y below 100 and Z below 10000: {version}"
        )
    major, minor, patch = map(int, match.groups())
    return major, minor, patch


def version_code(version: str) -> int:
    """The Android versionCode of a release version."""
    major, minor, patch = parse(version)
    return major * 1_000_000 + minor * 10_000 + patch


def current(root: Path) -> str:
    return tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]


def set_version(root: Path, version: str) -> None:
    """Write the version into all three files, or change none of them."""
    parse(version)
    texts = {}
    for name, (pattern, replacement) in _FIELDS.items():
        text, count = re.subn(
            pattern, replacement.format(version=version), (root / name).read_text()
        )
        if count != 1:
            raise ValueError(f"No single version field in {name}")
        texts[name] = text
    for name, text in texts.items():
        (root / name).write_text(text)


def bump(root: Path, version: str) -> None:
    """A release only moves forward: the phone installs an APK only over a lower versionCode."""
    if parse(version) <= parse(current(root)):
        raise ValueError(f"The version must be above the current {current(root)}: {version}")
    set_version(root, version)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("Usage: python -m scripts.bump_version X.Y.Z", file=sys.stderr)
        return 2
    root = Path(__file__).resolve().parents[1]
    try:
        bump(root, args[0])
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(
        f"Version {args[0]}. Commit pyproject.toml, mnema/__init__.py and uv.lock, tag v{args[0]}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
