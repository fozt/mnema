"""Build a self-contained Mnema.app for macOS (arm64), its update zip, its code archive and the DMG.

    uv run python -m scripts.build_mac_app --out DIR [--ffmpeg PREFIX] [--version X.Y.Z] [--smoke]
        [--sources] [--previous APP]

Layout: Contents/MacOS/Mnema (the Swift panel) and mnema-asr (recognition on CoreML),
Contents/Resources/python (uv-managed python-build-standalone, pinned, with the app's packages
installed into it), Contents/Resources/bin (ffmpeg and ffprobe from PREFIX/bin; mnema-mcp only
tells old MCP configs how to get the mnema command, a package of its own),
Contents/Resources/licenses. Every path is relative to the bundle, so the app runs from wherever
it is copied. Packages the app must not carry fail the build (FORBIDDEN). Without --ffmpeg the
app uses ffmpeg from PATH (a warning says so). --sources also writes
third-party-sources-X.Y.Z.tar.gz (scripts/third_party_sources.py) next to the zip and the DMG.

Mnema-X.Y.Z-mac-code.tar.gz is the code of the bundle (mnema.bar.codesign.CODE_PATHS): a bundle
with the same runtime key (MnemaRuntime) updates with it alone. --previous (the app of the last
release) checks that its own update code makes this version from it; --smoke checks the same on
this build.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mnema.bar import codesign  # noqa: E402 - code, runtime and the signature of the bundle
from mnema.bar.codesign import macho_files  # noqa: E402
from mnema.core.base import APP_ID  # noqa: E402
from mnema.extras import MAC as EXTRAS  # noqa: E402 - the one list of what the Mac app ships
from scripts import third_party_sources  # noqa: E402

SWIFT_PACKAGE = ROOT / "mnema" / "bar" / "macos"
SWIFT_PRODUCTS = ("Mnema", "mnema-asr")  # both into Contents/MacOS
PY = "python3.12"
PYTHON_VERSION = "3.12.16"  # python-build-standalone: the same interpreter in every build
# The heavy Python the app does without: recognition is mnema-asr, the command (rich, yt-dlp,
# deno, mcp) is a package of its own. mutagen (GPL) only came with yt-dlp. Distribution names,
# canonical (PEP 503).
FORBIDDEN = (
    "torch", "torchaudio", "mlx", "mlx-whisper", "parakeet-mlx", "whisperx", "gigaam", "numba",
    "scipy", "scikit-learn", "librosa", "transformers", "silero-vad", "onnxruntime",
    "whisperlivekit", "yt-dlp", "deno", "rich", "mcp", "mutagen",
)  # fmt: skip
# stdlib parts the app never imports
_STDLIB_DROP = ("idlelib", "tkinter", "test", "turtledemo", "turtle.py")
# Installed but never imported at run time: pip and setuptools are build tools (torch, cffi and
# numba want setuptools only to compile extensions), PyObjCTest is pyobjc's own test suite.
_SITE_DROP = (
    "pip", "setuptools", "pkg_resources", "_distutils_hack", "distutils-precedence.pth", "wheel",
    "PyObjCTest",
)  # fmt: skip
_DIST_DROP = ("pip", "setuptools", "wheel")
# Packages whose "tests" folders are only their test suites
_TESTS_DROP = ("numpy",)
# Parts never imported: litellm's proxy admin UI, the debug symbols of pyobjc
_SLICES = ("litellm/proxy/_experimental", "objc/*.dSYM")
_LIB_DROP = re.compile(r"^(libtcl|libtk|tcl|tk|itcl|thread)")
_BIN_DROP = re.compile(r"^(idle|pydoc|2to3)")
# uv's relocatable shebang: the script runs the python next to it, wherever the bundle is
_SHEBANG = (
    '#!/bin/sh\n\'\'\'exec\' "$(dirname -- "$(realpath -- "$0")")"/{py} "$0" "$@"\n\' \'\'\'\n'
)

# Old MCP configs start the app's mnema-mcp: it says where the command is now
MCP_STUB = (
    "#!/bin/sh\n"
    "echo 'mnema-mcp is part of the mnema command now. Install it: "
    'uv tool install "mnema[cli] @ {wheel}"\' >&2\n'
    "exit 1\n"
)
WHEEL = (
    "https://github.com/fozt/mnema/releases/download/v{version}/mnema-{version}-py3-none-any.whl"
)
# The weights mnema-asr downloads on first use: CC-BY-4.0 asks for attribution
MODELS = """\
Models Mnema downloads on first use (not part of this app)

Parakeet Ultra on CoreML: https://huggingface.co/FluidInference/parakeet-ultra-coreml
  CoreML conversion by FluidInference of moondream/parakeet-ultra
  (https://huggingface.co/moondream/parakeet-ultra), a fine-tune of NVIDIA
  parakeet-tdt-0.6b-v3 (https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3).
  License: CC-BY-4.0, https://creativecommons.org/licenses/by/4.0/

silero VAD on CoreML: https://huggingface.co/FluidInference/silero-vad-coreml
  CoreML conversion by FluidInference of silero-vad (https://github.com/snakers4/silero-vad).
  License: MIT
"""


def say(text: str) -> None:
    print(f"build_mac_app: {text}", flush=True)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run([str(c) for c in cmd], check=True, **kw)


def pyproject_version() -> str:
    with open(ROOT / "pyproject.toml", "rb") as f:
        return tomllib.load(f)["project"]["version"]


def managed_python() -> Path:
    """The prefix of the uv-managed CPython PYTHON_VERSION (python-build-standalone):
    relocatable, with a shared libpython for the embedded engine."""
    find = ["uv", "python", "find", "--managed-python", "--no-project", PYTHON_VERSION]
    out = subprocess.run(find, capture_output=True, text=True, cwd=tempfile.gettempdir())
    if out.returncode != 0:
        run(["uv", "python", "install", PYTHON_VERSION])
        out = subprocess.run(find, capture_output=True, text=True, check=True)
    python = Path(out.stdout.strip())
    said = run([python, "-I", "-c", "import platform; print(platform.python_version())"],
               capture_output=True, text=True).stdout.strip()  # fmt: skip
    if said != PYTHON_VERSION:
        raise SystemExit(f"{python} is Python {said}, the app needs {PYTHON_VERSION}")
    prefix = python.resolve().parent.parent
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


def install_packages(python: Path, work: Path) -> Path:
    """Returns the requirements installed: an input of the runtime."""
    requirements = work / "requirements.txt"
    export = ["uv", "export", "--frozen", "--no-dev", "--no-emit-project", "--no-hashes"]
    export.append("--no-header")  # the header names the output path
    for extra in EXTRAS:
        export += ["--extra", extra]
    run([*export, "-o", requirements], cwd=ROOT, capture_output=True)
    # copies, not links into the uv cache: signing rewrites files in place
    install = ["uv", "pip", "install", "--python", python, "--link-mode", "copy", "--no-config"]
    env = {**os.environ, "UV_PYTHON_DOWNLOADS": "never"}
    env.pop("VIRTUAL_ENV", None)
    run([*install, "--no-build", "-r", requirements], env=env)  # wheels only, as locked
    run([*install, "--no-deps", "--reinstall-package", "mnema", source_tree(work)], env=env)
    return requirements


def _dist_name(dist_info: Path) -> str:
    """The canonical distribution name of a NAME-VERSION.dist-info folder (PEP 503)."""
    return re.sub(r"[-_.]+", "-", dist_info.name.rsplit("-", 1)[0]).lower()


def trim(python_home: Path) -> int:
    """Remove what the app never imports (_SITE_DROP, _TESTS_DROP, _SLICES, the console scripts
    of mnema); returns bytes freed. Fails if a package that must not ship came in."""
    site = python_home / "lib" / PY / "site-packages"
    found = [p.name.removesuffix(".dist-info") for p in sorted(site.glob("*.dist-info"))]
    if found := [name for name in found if _dist_name(Path(name)) in FORBIDDEN]:
        raise SystemExit(f"must not ship: {', '.join(found)}")
    doomed = [site / name for name in _SITE_DROP]
    doomed += [p for name in _DIST_DROP for p in site.glob(f"{name}-*.dist-info")]
    doomed += [
        p for p in (python_home / "bin").iterdir() if re.match(r"^(pip|wheel|mnema)", p.name)
    ]
    for package in _TESTS_DROP:
        doomed += [p for p in (site / package).rglob("tests") if p.is_dir()]
    doomed += [p for pattern in _SLICES for p in site.glob(pattern)]
    freed = 0
    for path in doomed:
        if path.is_symlink() or path.is_file():
            freed += path.lstat().st_size
            path.unlink()
        elif path.is_dir():
            freed += sum(f.lstat().st_size for f in path.rglob("*") if not f.is_dir())
            shutil.rmtree(path)
    return freed


def write_mcp_stub(bin_dir: Path, version: str) -> Path:
    stub = bin_dir / "mnema-mcp"
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub.write_text(MCP_STUB.format(wheel=WHEEL.format(version=version)), encoding="utf-8")
    stub.chmod(0o755)
    return stub


def swift_licenses(checkouts: Path, dest: Path) -> None:
    """The licenses of the Swift packages built into the app (FluidAudio and those it lists in
    ThirdPartyLicenses/) into dest/swift/<package>/, and the attribution of the downloaded models
    into dest/MODELS.txt."""
    for package in sorted(p for p in checkouts.iterdir() if p.is_dir()):
        target = dest / "swift" / package.name
        for file in package.iterdir():
            if file.is_file() and _LICENSE_FILE.match(file.name):
                target.mkdir(parents=True, exist_ok=True)
                shutil.copy2(file, target / file.name)
        if (package / "ThirdPartyLicenses").is_dir():
            shutil.copytree(package / "ThirdPartyLicenses", target / "ThirdPartyLicenses")
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "MODELS.txt").write_text(MODELS, encoding="utf-8")


_COPYLEFT = re.compile(r"\bL?GPL(?:v\d|\b)|GNU (Lesser |Library )?General Public", re.I)
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


def build_swift(cache: Path) -> dict[str, Path]:
    """The panel and mnema-asr, release builds; FluidAudio's resource bundles (text to speech)
    stay out of the app."""
    cmd = ["swift", "build", "-c", "release", "--package-path", SWIFT_PACKAGE, "--scratch-path"]
    for product in SWIFT_PRODUCTS:
        run([*cmd, cache, "--product", product])
    return {product: cache / "release" / product for product in SWIFT_PRODUCTS}


def compile_bytecode(python: Path, app: Path) -> None:
    """Bytecode once at build time: the signed bundle is never written to at run time. The same
    bytes wherever the bundle is built: paths from the bundle's folder, a fixed hash seed (sets of
    strings)."""
    lib = python.parent.parent / "lib" / PY
    cmd = [python, "-I", "-m", "compileall", "-q", "-j0", "--invalidation-mode", "unchecked-hash"]
    cmd += ["-s", app.parent, "-p", "/"]
    env = {**os.environ, "PYTHONHASHSEED": "0"}
    # a few packages ship files that are not Python 3 (templates, test data): not an error
    subprocess.run([*map(str, cmd), str(lib)], capture_output=True, env=env)


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


def runtime_inputs(requirements: Path, ffmpeg: Path | None) -> str:
    """What the runtime is built from. The same inputs must give the same runtime key: the release
    check fails otherwise."""
    digest = hashlib.sha256()

    def add(name: str, data: bytes) -> None:
        digest.update(name.encode() + b"\0" + hashlib.sha256(data).digest())

    add("python", PYTHON_VERSION.encode())
    add("requirements", requirements.read_bytes())
    for tool in ("uv", "swift"):
        add(tool, run([tool, "--version"], capture_output=True).stdout)
    for tool in ("ffmpeg", "ffprobe"):
        add(tool, (ffmpeg / "bin" / tool).read_bytes() if ffmpeg else b"")
    sources = [SWIFT_PACKAGE / "Package.swift", SWIFT_PACKAGE / "Package.resolved"]
    for folder in ("MnemaASR", "MnemaASRProtocol"):
        sources += sorted(p for p in (SWIFT_PACKAGE / "Sources" / folder).rglob("*") if p.is_file())
    sources += [Path(__file__), Path(codesign.__file__)]  # trimming, signing and the key itself
    for path in sources:
        add(path.relative_to(ROOT).as_posix(), path.read_bytes() if path.is_file() else b"")
    return digest.hexdigest()


def seal(app: Path, inputs: str) -> str:
    """Sign every Mach-O inside, write the runtime key and its inputs into Info.plist, sign the
    bundle; returns the key. The key is taken after the inner signatures, which belong to the
    runtime, and the bundle signature changes no runtime file."""
    main_executable = app / codesign.MAIN
    codesign.sign_files([p for p in macho_files(app) if p != main_executable])
    key = codesign.runtime_key(app)
    plist = app / "Contents" / "Info.plist"
    info = plistlib.loads(plist.read_bytes())
    info[codesign.RUNTIME] = key
    info[codesign.RUNTIME_INPUTS] = inputs
    plist.write_bytes(plistlib.dumps(info))
    codesign.sign_bundle(app, APP_ID)
    return key


def _tar_entry(info: tarfile.TarInfo) -> tarfile.TarInfo:
    """The same archive from the same files."""
    info.mtime = info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mode = 0o755 if info.mode & 0o111 else 0o644
    info.pax_headers = {}
    return info


def code_archive(app: Path, version: str) -> Path:
    """Mnema-X.Y.Z-mac-code.tar.gz next to the bundle: its code, files only, paths from it."""
    archive = app.parent / f"Mnema-{version}-mac-code.tar.gz"
    with (
        archive.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as package,
    ):
        for rel in codesign.code_files(app):
            if (app / rel).is_symlink():
                raise SystemExit(f"a link in the code of the bundle: {rel}")
            package.add(app / rel, arcname=rel, recursive=False, filter=_tar_entry)
    return archive


def _info(app: Path) -> dict:
    return plistlib.loads((app / "Contents" / "Info.plist").read_bytes())


def check_code_update(previous: Path, app: Path, archive: Path, version: str) -> None:
    """The code archive on the bundle of an earlier release (or of this build): when the runtime
    keys match, that bundle's own update code, with nothing but the system tools, must make this
    version from it. A runtime key that changed though its inputs did not fails the release."""
    old, new = _info(previous), _info(app)
    if codesign.RUNTIME not in old:
        say(f"{previous} has no runtime key: it updates as a whole")
        return
    if old[codesign.RUNTIME] != new[codesign.RUNTIME]:
        if old.get(codesign.RUNTIME_INPUTS) == new[codesign.RUNTIME_INPUTS]:
            raise SystemExit(
                f"the runtime key differs from {previous}, built from the same inputs: the build "
                "is not reproducible, a code-only update would never apply"
            )
        say(f"the runtime differs from {previous}: it updates as a whole")
        return
    python = previous / "Contents" / "Resources" / "python" / "bin" / PY
    code = (
        "import sys; from pathlib import Path; from mnema import update; "
        "update.prepare_code_bundle(*map(Path, sys.argv[1:4]), sys.argv[4])"
    )
    with tempfile.TemporaryDirectory(dir=app.parent, prefix=".code-check-") as work:
        # as on a Mac without Command Line Tools
        env = {"PATH": "/usr/bin:/bin", "HOME": work, "TMPDIR": work, "LANG": "en_US.UTF-8"}
        run([python, "-I", "-B", "-c", code, previous, archive, work, version], env=env)
        made = Path(work) / "Mnema.app"
        if _info(made)["CFBundleShortVersionString"] != version:
            raise SystemExit(f"the code archive made another version from {previous}")
        if codesign.runtime_key(made) != new[codesign.RUNTIME]:
            raise SystemExit(f"the code archive changed the runtime of {previous}")
    say(f"the code archive updates {previous} to {version}")


def write_metadata(app: Path, python: Path, version: str) -> None:
    """Info.plist and the permission texts (mnema.bar.install.write_bundle)."""
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
    say("building the Swift panel and mnema-asr")
    for product, built in build_swift(work / "swift").items():
        shutil.copy2(built, app / "Contents" / "MacOS" / product)
    say("copying Python")
    python = copy_python(managed_python(), res / "python")
    say("installing packages")
    requirements = install_packages(python, work)
    say(f"trimmed {trim(res / 'python') / 2**20:.0f} MB the app never imports")
    relocate_scripts(res / "python" / "bin")
    bin_dir = res / "bin"
    write_mcp_stub(bin_dir, version)
    licenses = res / "licenses"
    licenses.mkdir()
    shutil.copy2(ROOT / "LICENSE", licenses / "Mnema-LICENSE")
    shutil.copy2(res / "python" / "lib" / PY / "LICENSE.txt", licenses / "Python-LICENSE.txt")
    swift_licenses(work / "swift" / "checkouts", licenses)
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
    shutil.copy2(SWIFT_PACKAGE / "Mnema.icns", res / "Mnema.icns")
    compile_bytecode(python, app)
    if links := absolute_links(app):
        raise SystemExit(f"links leave the bundle: {', '.join(map(str, links[:5]))}")
    scrub_all(app)
    say("signing")
    say(f"runtime key {seal(app, runtime_inputs(requirements, ffmpeg))}")
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
        create += ["-format", "ULMO", "-quiet", image]  # lzma: half the size of lzfse
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
        "--previous", type=Path,
        help="Mnema.app of the last release: check the code archive updates it",
    )  # fmt: skip
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
    code = code_archive(app, version)
    for path in (archive, image, code):
        say(f"{path} {path.stat().st_size / 2**20:.1f} MB")
    if args.previous:
        check_code_update(args.previous.resolve(), app, code, version)
    if args.smoke:
        from scripts.smoke_mac_app import smoke

        check_code_update(app, app, code, version)
        smoke(app, version=version, ffmpeg=args.ffmpeg is not None)


if __name__ == "__main__":
    main()
