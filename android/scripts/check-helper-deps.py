#!/usr/bin/env python3
"""Helper javac classes before R8 reference only the platform and their own package; otherwise exit 1."""
import re
import struct
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

ANDROID = Path(__file__).resolve().parent.parent
APP = ANDROID / "app"
HELPER_PACKAGE = "dev/mnema/app/helper/"
HELPER_SOURCES = APP / "src/main/java/dev/mnema/app/helper"
COMPILE_TASK = ":app:compileReleaseJavaWithJavac"
JAVAC_CLASSES = APP / "build/intermediates/javac/release/compileReleaseJavaWithJavac/classes"
PLATFORM = ("java/", "android/", "org/json/", "dalvik/")

TYPE = re.compile(r"L([A-Za-z0-9_$/]+);")


class ClassRefs(NamedTuple):
    """names — all classes of the constant pool, used — member owners and descriptor types."""

    name: str
    names: set[str]
    used: set[str]


def class_refs(data: bytes) -> ClassRefs:
    if data[:4] != b"\xca\xfe\xba\xbe":
        raise ValueError("not a class file")
    count = struct.unpack_from(">H", data, 8)[0]
    pos = 10
    utf8: dict[int, str] = {}
    classes: dict[int, int] = {}
    literals: set[int] = set()
    member_owners: set[int] = set()
    index = 1
    while index < count:
        tag = data[pos]
        if tag == 1:
            length = struct.unpack_from(">H", data, pos + 1)[0]
            utf8[index] = data[pos + 3 : pos + 3 + length].decode("utf-8", errors="replace")
            pos += 3 + length
        elif tag == 7:
            classes[index] = struct.unpack_from(">H", data, pos + 1)[0]
            pos += 3
        elif tag == 8:
            literals.add(struct.unpack_from(">H", data, pos + 1)[0])
            pos += 3
        elif tag in (9, 10, 11):
            member_owners.add(struct.unpack_from(">H", data, pos + 1)[0])
            pos += 5
        elif tag in (3, 4, 12, 17, 18):
            pos += 5
        elif tag in (5, 6):
            pos += 9
            index += 1
        elif tag == 15:
            pos += 4
        elif tag in (16, 19, 20):
            pos += 3
        else:
            raise ValueError(f"unknown constant pool tag: {tag}")
        index += 1
    this_class = struct.unpack_from(">H", data, pos + 2)[0]

    def class_names(entry: int) -> set[str]:
        name = utf8[classes[entry]]
        return set(TYPE.findall(name)) if name.startswith("[") else {name}

    # descriptors and signatures are Utf8, except string literals
    described = {t for i, text in utf8.items() if i not in literals for t in TYPE.findall(text)}
    names = described.union(*(class_names(entry) for entry in classes))
    used = described.union(*(class_names(entry) for entry in member_owners))
    return ClassRefs(utf8[classes[this_class]], names, used)


def top_level_name(name: str) -> str:
    """dev/mnema/app/helper/Capture$Stats → Capture (the source file name); another package → ''."""
    if not name.startswith(HELPER_PACKAGE) or "/" in name[len(HELPER_PACKAGE) :]:
        return ""
    return name[len(HELPER_PACKAGE) :].split("$", 1)[0]


def violations(classes_dir: Path, owners: set[str]) -> tuple[int, list[str]]:
    """How many helper classes were checked and the list of "class -> foreign type"."""
    package_dir = classes_dir / HELPER_PACKAGE
    missing = {owner for owner in owners if not (package_dir / f"{owner}.class").is_file()}
    if missing:
        sys.exit(f"No javac classes in {package_dir} for sources: {', '.join(sorted(missing))}")
    checked = 0
    found: list[str] = []
    for path in sorted(package_dir.glob("*.class")):
        refs = class_refs(path.read_bytes())
        if top_level_name(refs.name) not in owners:
            continue
        checked += 1
        for name in sorted(refs.names):
            if name.startswith(PLATFORM) or top_level_name(name) in owners:
                continue
            # Kotlin classes of the package contribute only constants that javac inlined
            if name.startswith(HELPER_PACKAGE) and name not in refs.used:
                continue
            found.append(f"{refs.name} -> {name}")
    return checked, found


def main() -> int:
    owners = {path.stem for path in HELPER_SOURCES.glob("*.java")}
    if not owners:
        sys.exit(f"No helper Java sources in {HELPER_SOURCES}")
    gradlew = str(ANDROID / "gradlew")
    subprocess.run([gradlew, "--console=plain", COMPILE_TASK], cwd=ANDROID, check=True)
    checked, found = violations(JAVAC_CLASSES, owners)
    if found:
        print("Helper classes reference disallowed types:", file=sys.stderr)
        for line in found:
            print(f"  {line}", file=sys.stderr)
        return 1
    print(f"Helper classes checked: {checked}; no foreign references")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
