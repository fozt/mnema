"""Build a self-contained Mnema.app for macOS (arm64), its update zip and the DMG.

    uv run python -m scripts.build_mac_app --out DIR [--ffmpeg PREFIX] [--version X.Y.Z] [--smoke]
        [--sources]

Layout: Contents/MacOS/Mnema (the Swift panel), Contents/Resources/python (uv-managed
python-build-standalone 3.12 with the app's packages installed into it), Contents/Resources/bin
(mnema and mnema-mcp wrappers, ffmpeg and ffprobe from PREFIX/bin), Contents/Resources/licenses.
Every path is relative to the bundle, so the app runs from wherever it is copied. Without
--ffmpeg the app uses ffmpeg from PATH (a warning says so). --sources also writes
third-party-sources-X.Y.Z.tar.gz (scripts/third_party_sources.py) next to the zip and the DMG.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mnema.extras import MAC as EXTRAS  # noqa: E402 - the one list of what the Mac app ships
from scripts import third_party_sources  # noqa: E402

SWIFT_PACKAGE = ROOT / "mnema" / "bar" / "macos"
BUNDLE_ID = "com.mnema.bar"
PY = "python3.12"
# mutagen (GPL) is in yt-dlp[default] only to embed thumbnails and tags, which mnema never asks for
NOT_SHIPPED = ("mutagen",)
# stdlib parts the app never imports
_STDLIB_DROP = ("idlelib", "tkinter", "test", "turtledemo", "turtle.py")
# Installed but never imported at run time: pip and setuptools are build tools (torch, cffi and
# numba want setuptools only to compile extensions), PyObjCTest is pyobjc's own test suite.
_SITE_DROP = (
    "pip", "setuptools", "pkg_resources", "_distutils_hack", "distutils-precedence.pth", "wheel",
    "PyObjCTest",
)  # fmt: skip
_DIST_DROP = ("pip", "setuptools", "wheel")
# Packages whose "tests" folders are only their test suites (not torch: it imports its own)
_TESTS_DROP = ("numpy", "scipy", "sympy", "sklearn", "networkx", "mpmath")
_LIB_DROP = re.compile(r"^(libtcl|libtk|tcl|tk|itcl|thread)")
_BIN_DROP = re.compile(r"^(idle|pydoc|2to3)")
MACHO_MAGIC = {
    b"\xcf\xfa\xed\xfe",  # 64-bit little endian
    b"\xce\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",  # universal
    b"\xca\xfe\xba\xbf",
}
# uv's relocatable shebang: the script runs the python next to it, wherever the bundle is
_SHEBANG = (
    '#!/bin/sh\n\'\'\'exec\' "$(dirname -- "$(realpath -- "$0")")"/{py} "$0" "$@"\n\' \'\'\'\n'
)

WRAPPER = """#!/bin/sh
# {name} from Mnema.app: the bundled Python, wherever the app is (links are followed)
self=$(realpath -- "$0") || exit 1
res=$(dirname -- "$(dirname -- "$self")")
export MNEMA_APP_BUNDLE="$(dirname -- "$(dirname -- "$res")")"
export PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1
export SSL_CERT_FILE="$res/python/lib/{py}/site-packages/certifi/cacert.pem"
export PATH="$res/bin:$res/python/bin:$PATH"
exec "$res/python/bin/{py}" -I -B -c \
  'import sys; sys.argv[0] = "{name}"; from {module} import main; sys.exit(main())' "$@"
"""
WRAPPERS = {"mnema": "mnema.cli", "mnema-mcp": "mnema.mcp.server"}


def say(text: str) -> None:
    print(f"build_mac_app: {text}", flush=True)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run([str(c) for c in cmd], check=True, **kw)


def pyproject_version() -> str:
    with open(ROOT / "pyproject.toml", "rb") as f:
        return tomllib.load(f)["project"]["version"]


def managed_python() -> Path:
    """The prefix of the uv-managed CPython 3.12 (python-build-standalone): relocatable, with a
    shared libpython for the embedded engine."""
    find = ["uv", "python", "find", "--managed-python", "--no-project", "3.12"]
    out = subprocess.run(find, capture_output=True, text=True, cwd=tempfile.gettempdir())
    if out.returncode != 0:
        run(["uv", "python", "install", "3.12"])
        out = subprocess.run(find, capture_output=True, text=True, check=True)
    prefix = Path(out.stdout.strip()).resolve().parent.parent
    if not (prefix / "lib" / f"lib{PY}.dylib").exists():
        raise SystemExit(f"{prefix}: no lib{PY}.dylib, not a python-build-standalone install")
    return prefix


def copy_python(src: Path, dest: Path) -> Path:
    def ignore(folder: str, names: list[str]) -> set[str]:
        here = Path(folder)
        skip = {n for n in names if n == "__pycache__"}
        if here == src / "lib":
            skip |= {n for n in names if _LIB_DROP.match(n)}
        elif here == src / "lib" / PY:
            skip |= {n for n in names if n in _STDLIB_DROP or n == "EXTERNALLY-MANAGED"}
        elif here == src / "lib" / PY / "lib-dynload":
            skip |= {n for n in names if n.startswith("_tkinter")}
        elif here == src / "bin":
            skip |= {n for n in names if _BIN_DROP.match(n)}
        return skip

    shutil.copytree(src, dest, symlinks=True, ignore=ignore)
    libpython = dest / "lib" / f"lib{PY}.dylib"
    # the install name is uv's absolute path; the app dlopens the file by its own path
    run(["install_name_tool", "-id", f"@rpath/lib{PY}.dylib", libpython], capture_output=True)
    return dest / "bin" / PY


def install_packages(python: Path, work: Path) -> None:
    requirements = work / "requirements.txt"
    export = ["uv", "export", "--frozen", "--no-dev", "--no-emit-project", "--no-hashes"]
    for extra in EXTRAS:
        export += ["--extra", extra]
    for name in NOT_SHIPPED:
        export += ["--no-emit-package", name]
    run([*export, "-o", requirements], cwd=ROOT, capture_output=True)
    # copies, not links into the uv cache: signing rewrites files in place
    install = ["uv", "pip", "install", "--python", python, "--link-mode", "copy", "--no-config"]
    env = {**os.environ, "UV_PYTHON_DOWNLOADS": "never"}
    env.pop("VIRTUAL_ENV", None)
    run([*install, "-r", requirements], env=env)
    run([*install, "--no-deps", "--reinstall-package", "mnema", source_tree(work)], env=env)


def trim(python_home: Path) -> int:
    """Remove what the app never imports (see _SITE_DROP, _TESTS_DROP); returns bytes freed.
    Fails if a package that must not ship came in."""
    site = python_home / "lib" / PY / "site-packages"
    if found := [n for n in NOT_SHIPPED if any(site.glob(f"{n}-*.dist-info"))]:
        raise SystemExit(f"must not ship: {', '.join(found)}")
    doomed = [site / name for name in _SITE_DROP]
    doomed += [p for name in _DIST_DROP for p in site.glob(f"{name}-*.dist-info")]
    doomed += [p for p in (python_home / "bin").iterdir() if re.match(r"^(pip|wheel)", p.name)]
    for package in _TESTS_DROP:
        doomed += [p for p in (site / package).rglob("tests") if p.is_dir()]
    freed = 0
    for path in doomed:
        if path.is_symlink() or path.is_file():
            freed += path.lstat().st_size
            path.unlink()
        elif path.is_dir():
            freed += sum(f.lstat().st_size for f in path.rglob("*") if not f.is_dir())
            shutil.rmtree(path)
    return freed


_COPYLEFT = re.compile(r"\bL?GPL\b|GNU (Lesser |Library )?General Public", re.I)
_LICENSE_FILE = re.compile(r"^(LICEN[CS]E|COPYING|NOTICE|AUTHORS)", re.I)
SOURCE_OFFER = """\
Mnema.app includes components under the GNU LGPL or GPL, listed below. Their source code is in
third-party-sources-{version}.tar.gz attached to the Mnema release this app comes from
(https://github.com/fozt/mnema/releases/tag/v{version}), except those under "On request". For
those, or if the archive is not there, open an issue at
{issues} and the source will be provided, for three years after the release. You may
replace the LGPL libraries in the bundle with modified versions.

ffmpeg and LAME (LGPL): bin/ffmpeg, bin/ffprobe
"""


def _metadata(dist_info: Path) -> dict[str, list[str]]:
    from email.parser import HeaderParser

    text = (dist_info / "METADATA").read_text(encoding="utf-8", errors="replace")
    msg = HeaderParser().parsestr(text)
    return {key.lower(): msg.get_all(key) or [] for key in set(msg.keys())}


def package_licenses(site_packages: Path, dest: Path, app_version: str) -> list[str]:
    """Copy the license files of every installed distribution into dest/python/<name>-<ver>/,
    write dest/THIRD-PARTY.txt and dest/SOURCE-OFFER.txt; returns the copyleft distributions."""
    lines, copyleft, offer = [], [], []
    for dist_info in sorted(site_packages.glob("*.dist-info")):
        meta = _metadata(dist_info)
        name, version = meta.get("name", [""])[0], meta.get("version", [""])[0]
        if not name or name.lower() == "mnema":
            continue
        kind = (meta.get("license-expression") or [""])[0] or (meta.get("license") or [""])[0]
        if "\n" in kind or len(kind) > 100:  # the whole license text in the field
            kind = ""
        classifiers = [
            c.removeprefix("License :: ").removeprefix("OSI Approved :: ")
            for c in meta.get("classifier", [])
            if c.startswith("License ::")
        ]
        kind = kind or "; ".join(classifiers) or "see the license files"
        target = dest / "python" / f"{name}-{version}"
        files = [p for p in dist_info.rglob("*") if p.is_file() and _LICENSE_FILE.match(p.name)]
        for file in files:
            out = target / file.relative_to(dist_info)
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file, out)
        lines.append(f"{name} {version}: {kind}")
        if _COPYLEFT.search(kind):
            copyleft.append(name)
            offer.append(f"{name} {version}: {kind}")
    for source in third_party_sources.native_sources(site_packages):
        if source.license:
            offer.append(f"{source.about} ({source.license})")
    if requested := third_party_sources.on_request(site_packages):
        offer += ["", "On request:", *requested]
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "THIRD-PARTY.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    header = SOURCE_OFFER.format(version=app_version, issues=third_party_sources.ISSUES)
    (dest / "SOURCE-OFFER.txt").write_text(header + "\n".join(offer) + "\n", encoding="utf-8")
    return copyleft


def source_tree(work: Path) -> Path:
    """The project as git sees it (tracked and new files, nothing ignored such as Swift build
    folders), without the Swift package: the bundle carries the built panel, not its sources."""
    listed = run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT, capture_output=True,
    ).stdout.decode()  # fmt: skip
    src = work / "src"
    shutil.rmtree(src, ignore_errors=True)
    for name in filter(None, listed.split("\0")):
        if name.startswith(("mnema/bar/macos/", "android/")) or not (ROOT / name).is_file():
            continue
        dest = src / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, dest)
    return src


def relocate_scripts(bin_dir: Path) -> None:
    """Console scripts got the build path in their shebang: run the python next to them."""
    for path in bin_dir.iterdir():
        if path.is_symlink() or not path.is_file():
            continue
        head = path.read_bytes()[:256]
        if not head.startswith(b"#!/") or b"python" not in head.split(b"\n", 1)[0]:
            continue
        text = path.read_text(encoding="utf-8", errors="surrogateescape")
        body = text.split("\n", 1)[1] if "\n" in text else ""
        path.write_text(_SHEBANG.format(py=PY) + body, encoding="utf-8", errors="surrogateescape")


def absolute_links(tree: Path) -> list[Path]:
    return [
        p
        for p in tree.rglob("*")
        if p.is_symlink()
        and (os.readlink(p).startswith("/") or not p.resolve().is_relative_to(tree))
    ]


def build_panel(cache: Path) -> Path:
    cmd = ["swift", "build", "-c", "release", "--package-path", SWIFT_PACKAGE]
    run([*cmd, "--scratch-path", cache])
    return cache / "release" / "Mnema"


def compile_bytecode(python: Path) -> None:
    """Bytecode once at build time: the signed bundle is never written to at run time."""
    lib = python.parent.parent / "lib" / PY
    cmd = [python, "-I", "-m", "compileall", "-q", "-j0", "--invalidation-mode", "unchecked-hash"]
    # a few packages ship files that are not Python 3 (templates, test data): not an error
    subprocess.run([*map(str, cmd), str(lib)], capture_output=True)


def macho_files(tree: Path) -> list[Path]:
    found = []
    for path in tree.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open("rb") as f:
            if f.read(4) in MACHO_MAGIC:
                found.append(path)
    return found


_LOAD_CMD = re.compile(r"^\s+cmd (LC_\w+)$")
_LOAD_PATH = re.compile(r"^\s+(?:name|path) (\S.*?) \(offset \d+\)$")


def load_commands(binary: Path) -> list[tuple[str, str]]:
    """(command, path) of the install name, dependencies and rpaths of a Mach-O."""
    out = run(["otool", "-l", binary], capture_output=True, text=True).stdout
    found, cmd = [], ""
    for line in out.splitlines():
        if m := _LOAD_CMD.match(line):
            cmd = m.group(1)
        elif m := _LOAD_PATH.match(line):
            found.append((cmd, m.group(1)))
    return found


def outside(path: str) -> bool:
    """An absolute path outside the system: not there on the user's Mac, or worse, someone
    else's library."""
    return path.startswith("/") and not (
        path in ("/usr/lib", "/System") or path.startswith(("/usr/lib/", "/System/"))
    )


def scrub(binary: Path) -> list[str]:
    """Drop the builders' rpaths (/Users/runner/..., Command Line Tools) and absolute install
    names; returns dependencies on absolute paths outside the system, which cannot be fixed."""
    args: list[str] = []
    broken = []
    for cmd, path in load_commands(binary):
        if not outside(path):
            continue
        if cmd == "LC_RPATH":
            args += ["-delete_rpath", path]
        elif cmd == "LC_ID_DYLIB":
            args += ["-id", f"@rpath/{Path(path).name}"]
        else:
            broken.append(f"{binary}: {path}")
    if args:
        run(["install_name_tool", *args, binary], capture_output=True)
    return broken


def scrub_all(app: Path) -> None:
    with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as pool:
        broken = [b for found in pool.map(scrub, macho_files(app)) for b in found]
    if broken:
        raise SystemExit("libraries outside the bundle:\n" + "\n".join(broken[:20]))


def sign(app: Path) -> None:
    """Ad-hoc, inside out: every Mach-O first, then the bundle with a designated requirement by
    identifier, so macOS permissions survive updates."""
    inner = [p for p in macho_files(app) if p != app / "Contents" / "MacOS" / "Mnema"]
    inner.sort(key=lambda p: len(p.parts), reverse=True)
    for i in range(0, len(inner), 200):
        batch = inner[i : i + 200]
        run(["codesign", "--force", "--sign", "-", "--timestamp=none", *batch], capture_output=True)
    requirement = f'-r=designated => identifier "{BUNDLE_ID}"'
    run(["codesign", "--force", "--sign", "-", "--timestamp=none", requirement, app])


def write_metadata(app: Path, python: Path, version: str) -> None:
    """Info.plist, permission texts and the icon: the same as the source build makes, without
    the engine keys (the app finds its Python in the bundle)."""
    code = (
        "import sys; from pathlib import Path; from mnema.bar import install; "
        "install.write_bundle(Path(sys.argv[1]), version=sys.argv[2])"
    )
    run([python, "-I", "-B", "-c", code, app, version])


def assemble(out: Path, ffmpeg: Path | None, version: str, sources: bool = False) -> Path:
    app = out / "Mnema.app"
    work = out / ".build"
    shutil.rmtree(app, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    res = app / "Contents" / "Resources"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    say("building the Swift panel")
    panel = build_panel(work / "swift")
    shutil.copy2(panel, app / "Contents" / "MacOS" / "Mnema")
    say("copying Python")
    python = copy_python(managed_python(), res / "python")
    say("installing packages")
    install_packages(python, work)
    say(f"trimmed {trim(res / 'python') / 2**20:.0f} MB the app never imports")
    relocate_scripts(res / "python" / "bin")
    bin_dir = res / "bin"
    bin_dir.mkdir()
    for name, module in WRAPPERS.items():
        wrapper = bin_dir / name
        wrapper.write_text(WRAPPER.format(name=name, module=module, py=PY), encoding="utf-8")
        wrapper.chmod(0o755)
    licenses = res / "licenses"
    licenses.mkdir()
    shutil.copy2(ROOT / "LICENSE", licenses / "Mnema-LICENSE")
    shutil.copy2(res / "python" / "lib" / PY / "LICENSE.txt", licenses / "Python-LICENSE.txt")
    site = res / "python" / "lib" / PY / "site-packages"
    copyleft = package_licenses(site, licenses, version)
    if sources and ffmpeg is not None:
        archive = third_party_sources.write(out, version, ffmpeg, site, copyleft)
        say(f"{archive} {archive.stat().st_size / 2**20:.0f} MB")
    if ffmpeg is not None:
        for tool in ("ffmpeg", "ffprobe"):
            shutil.copy2(ffmpeg / "bin" / tool, bin_dir / tool)
        if (ffmpeg / "licenses").is_dir():
            shutil.copytree(ffmpeg / "licenses", licenses / "ffmpeg")
    else:
        say("WARNING: no --ffmpeg, the app will use ffmpeg and ffprobe from PATH")
    write_metadata(app, python, version)
    compile_bytecode(python)
    if links := absolute_links(app):
        raise SystemExit(f"links leave the bundle: {', '.join(map(str, links[:5]))}")
    scrub_all(app)
    say("signing")
    sign(app)
    return app


def package(app: Path, version: str) -> tuple[Path, Path]:
    out = app.parent
    archive = out / f"Mnema-{version}-mac.zip"
    archive.unlink(missing_ok=True)
    run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", app, archive])
    image = out / f"Mnema-{version}.dmg"
    image.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(dir=out, prefix=".dmg-") as staging:
        run(["ditto", app, Path(staging) / "Mnema.app"])
        (Path(staging) / "Applications").symlink_to("/Applications")
        create = ["hdiutil", "create", "-volname", "Mnema", "-srcfolder", staging, "-ov"]
        create += ["-format", "ULMO", "-quiet", image]  # lzma: a fifth smaller than UDZO
        for attempt in range(3):  # CI runners sometimes answer "Resource busy"
            if subprocess.run(create, check=False).returncode == 0:
                break
            if attempt == 2:
                raise SystemExit("hdiutil create failed")
            time.sleep(10)
    return archive, image


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Build Mnema.app, its zip and DMG")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--ffmpeg", type=Path, help="prefix with bin/ffmpeg, bin/ffprobe, licenses/")
    p.add_argument("--version", help="must match pyproject.toml (default: that version)")
    p.add_argument("--smoke", action="store_true", help="check the built app")
    p.add_argument(
        "--sources", action="store_true",
        help="also write third-party-sources-X.Y.Z.tar.gz (needs --ffmpeg)",
    )  # fmt: skip
    args = p.parse_args(argv)
    if sys.platform != "darwin":
        raise SystemExit("Mnema.app builds on macOS only")
    version = pyproject_version()
    if args.version and args.version != version:
        raise SystemExit(f"--version {args.version} differs from pyproject.toml {version}")
    if args.ffmpeg is not None and not (args.ffmpeg / "bin" / "ffmpeg").exists():
        raise SystemExit(f"{args.ffmpeg}/bin/ffmpeg not found")
    if args.sources and args.ffmpeg is None:
        raise SystemExit("--sources needs --ffmpeg: the archive carries its sources")
    out = args.out.resolve()
    app = assemble(out, args.ffmpeg.resolve() if args.ffmpeg else None, version, args.sources)
    archive, image = package(app, version)
    for path in (archive, image):
        say(f"{path} {path.stat().st_size / 2**20:.0f} MB")
    if args.smoke:
        from scripts.smoke_mac_app import smoke

        smoke(app, version=version, ffmpeg=args.ffmpeg is not None)


if __name__ == "__main__":
    main()
