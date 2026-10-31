"""The self-contained Mnema.app runs its own Python from wherever the bundle is copied."""

import os
import plistlib
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from mnema.bar import codesign
from scripts import build_mac_app as build

# a fake interpreter: says where it is, what it was asked and which bundle it belongs to
_PYTHON = '#!/bin/sh\necho "python=$0 bundle=$MNEMA_APP_BUNDLE args=$*"\n'


def _bundle(root: Path) -> Path:
    python = root / "Mnema.app" / "Contents" / "Resources" / "python" / "bin" / build.PY
    python.parent.mkdir(parents=True)
    python.write_text(_PYTHON)
    python.chmod(0o755)
    return root / "Mnema.app"


def _run(path: Path, *args: str) -> str:
    return subprocess.run([str(path), *args], capture_output=True, text=True, check=True).stdout


def test_build_and_smoke_scripts_run_on_a_bare_python():
    """The release runs them with uv run --no-project: a Python without the app's packages."""
    root = Path(build.__file__).resolve().parent.parent
    code = f"import sys; sys.path.insert(0, {str(root)!r}); "
    code += "import scripts.build_mac_app, scripts.smoke_mac_app"
    done = subprocess.run(
        [sys.executable, "-I", "-S", "-c", code], cwd=root, capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr


def test_console_scripts_run_the_python_next_to_them(tmp_path):
    bin_dir = _bundle(tmp_path / "a") / "Contents/Resources/python/bin"
    script = bin_dir / "tool"
    script.write_text("#!/build/machine/python/bin/python3.12\nimport sys\nprint(sys.argv)\n")
    script.chmod(0o755)
    binary = bin_dir / "native"
    binary.write_bytes(b"\xcf\xfa\xed\xfe" + bytes(16))
    build.relocate_scripts(bin_dir)
    assert binary.read_bytes() == b"\xcf\xfa\xed\xfe" + bytes(16)  # binaries stay as they are
    moved = tmp_path / "b"
    (tmp_path / "a").rename(moved)
    out = _run(moved / "Mnema.app/Contents/Resources/python/bin/tool", "x")
    assert f"python={os.path.realpath(moved)}/Mnema.app/Contents/Resources/python/bin/" in out


def test_mcp_client_of_the_old_app_is_told_how_to_get_the_command(tmp_path):
    """MCP configs still start Resources/bin/mnema-mcp of the app: it fails and names the wheel of
    this very release with the versions it was tested with."""
    stub = build.write_mcp_stub(tmp_path / "bin", "0.5.0")
    done = subprocess.run([str(stub)], capture_output=True, text=True, input="{}\n")
    assert done.returncode != 0 and done.stdout == ""
    release = "https://github.com/fozt/mnema/releases/download/v0.5.0"
    wheel = f"{release}/mnema-0.5.0-py3-none-any.whl"
    constraints = f"{release}/mnema-0.5.0-constraints.txt"
    assert f'uv tool install "mnema[cli] @ {wheel}" --constraints {constraints}' in done.stderr
    assert len(stub.read_text().splitlines()) == 3


def test_builder_paths_are_outside_and_system_paths_are_not():
    for path in ("/Users/runner/miniconda3/lib", "/opt/homebrew/opt/libomp/lib/libomp.dylib"):
        assert build.outside(path)
    for path in ("/usr/lib/libSystem.B.dylib", "/usr/lib", "/System/Library/Frameworks/AppKit"):
        assert not build.outside(path)
    assert not build.outside("@rpath/libomp.dylib")


def _dist(site: Path, name: str, version: str, headers: str, files: dict[str, str]) -> None:
    info = site / f"{name}-{version}.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n{headers}"
    )
    for rel, text in files.items():
        (info / rel).parent.mkdir(parents=True, exist_ok=True)
        (info / rel).write_text(text)


def test_bundle_carries_every_package_license_and_names_the_copyleft_ones(tmp_path):
    site = tmp_path / "site-packages"
    _dist(site, "soxr", "1.1.0", "License-Expression: LGPL-2.1-or-later\n",
          {"licenses/COPYING.LGPL": "lgpl", "RECORD": ""})  # fmt: skip
    _dist(site, "soundfile", "0.14.0", "Classifier: License :: OSI Approved :: BSD License\n",
          {"LICENSE": "bsd"})  # fmt: skip
    _dist(site, "httpx", "0.28.1", "License: BSD-3-Clause\n", {"licenses/LICENSE.md": "bsd"})
    _dist(site, "mnema", "0.2.21", "", {})
    _dist(site, "chardet", "5.2.0", "License: LGPLv2.1+\n", {})
    _dist(site, "gplthing", "1.0", "License: GPLv3\n", {})
    (site / "scipy/.dylibs").mkdir(parents=True)
    (site / "scipy/.dylibs/libquadmath.0.dylib").write_bytes(b"")
    dest = tmp_path / "licenses"

    assert build.package_licenses(site, dest, "0.4.0") == ["chardet", "gplthing", "soxr"]

    assert (dest / "python/soxr-1.1.0/licenses/COPYING.LGPL").read_text() == "lgpl"
    assert (dest / "python/soundfile-0.14.0/LICENSE").read_text() == "bsd"
    assert not (dest / "python/soxr-1.1.0/RECORD").exists()
    listed = (dest / "THIRD-PARTY.txt").read_text().splitlines()
    assert listed == [
        "chardet 5.2.0: LGPLv2.1+",
        "gplthing 1.0: GPLv3",
        "httpx 0.28.1: BSD-3-Clause",
        "soundfile 0.14.0: BSD License",
        "soxr 1.1.0: LGPL-2.1-or-later",
    ]
    offer = (dest / "SOURCE-OFFER.txt").read_text()
    assert "third-party-sources-0.4.0.tar.gz" in offer
    assert "soxr 1.1.0: LGPL-2.1-or-later" in offer
    assert "libsndfile 1.2.2" in offer and "mpg123 1.32.3" in offer
    on_request = offer.split("On request:")[1]
    assert "libquadmath" in on_request and "scipy/.dylibs/libquadmath.0.dylib" in on_request
    assert "httpx" not in offer


def test_trim_drops_build_tools_and_test_suites_but_keeps_what_imports_them(tmp_path):
    home = tmp_path / "python"
    site = home / "lib" / build.PY / "site-packages"
    kept = [
        "numpy/testing/__init__.py",  # numpy imports it
        "numpy/__init__.py",
        "pyobjc_core-12.dist-info/METADATA",
        "litellm/proxy/__init__.py",
        "objc/_objc.cpython-312-darwin.so",
        "bin/python3.12",
    ]
    dropped = [
        "pip/__init__.py",
        "pip-26.2.2.dist-info/METADATA",
        "setuptools/__init__.py",
        "setuptools-82.0.1.dist-info/METADATA",
        "_distutils_hack/__init__.py",
        "distutils-precedence.pth",
        "PyObjCTest/test_x.py",
        "numpy/_core/tests/test_a.py",
        "litellm/proxy/_experimental/out/index.html",
        "objc/_objc.cpython-312-darwin.so.dSYM/Contents/Info.plist",
        "bin/pip3",
        "bin/mnema",  # the command is a package of its own now
        "bin/mnema-bar",
        "bin/mnema-mcp",
    ]
    rel = {p: (home / p if p.startswith("bin/") else site / p) for p in kept + dropped}
    for path in rel.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
    assert build.trim(home) > 0
    assert [p for p in kept if not rel[p].exists()] == []
    assert [p for p in dropped if rel[p].exists()] == []


@pytest.mark.parametrize(
    "dist", ["mutagen-1.47.0", "torch-2.8.0", "yt_dlp-2026.10.21", "scikit_learn-1.7.2",
             "silero_vad-6.2.1", "rich-15.0.0", "mcp-1.20.0", "Deno-2.9.8"],
)  # fmt: skip
def test_trim_refuses_a_bundle_with_a_package_that_must_not_ship(tmp_path, dist):
    home = tmp_path / "python"
    (home / "bin").mkdir(parents=True)
    (home / "lib" / build.PY / "site-packages" / f"{dist}.dist-info").mkdir(parents=True)
    with pytest.raises(SystemExit, match=dist.split("-")[0]):
        build.trim(home)


def test_bundle_carries_the_swift_licenses_and_the_model_attribution(tmp_path):
    """FluidAudio's license and the licenses it lists go into the bundle; the weights the app
    downloads (CC-BY-4.0) are attributed."""
    checkouts = tmp_path / "checkouts"
    fluid = checkouts / "FluidAudio"
    (fluid / "ThirdPartyLicenses" / "KokoroANE-v3").mkdir(parents=True)
    (fluid / "LICENSE").write_text("apache")
    (fluid / "ThirdPartyLicenses" / "vbx-LICENSE.md").write_text("vbx")
    (fluid / "ThirdPartyLicenses" / "KokoroANE-v3" / "LICENSE").write_text("kokoro")
    (fluid / "README.md").write_text("readme")
    dest = tmp_path / "licenses"

    build.swift_licenses(checkouts, dest)

    assert (dest / "swift/FluidAudio/LICENSE").read_text() == "apache"
    assert (dest / "swift/FluidAudio/ThirdPartyLicenses/vbx-LICENSE.md").read_text() == "vbx"
    assert (dest / "swift/FluidAudio/ThirdPartyLicenses/KokoroANE-v3/LICENSE").exists()
    assert not (dest / "swift/FluidAudio/README.md").exists()
    models = (dest / "MODELS.txt").read_text()
    for credit in ("CC-BY-4.0", "NVIDIA", "moondream", "FluidInference/parakeet-ultra-coreml"):
        assert credit in models


SITE = "Contents/Resources/python/lib/python3.12/site-packages"
CODE = [
    "Contents/Info.plist",
    "Contents/MacOS/Mnema",
    "Contents/Resources/Mnema.icns",
    "Contents/Resources/bin/mnema-mcp",
    "Contents/Resources/en.lproj/InfoPlist.strings",
    "Contents/Resources/licenses/THIRD-PARTY.txt",
    f"{SITE}/mnema-0.5.0.dist-info/RECORD",
    f"{SITE}/mnema/__init__.py",
    f"{SITE}/mnema/__pycache__/__init__.cpython-312.pyc",
]
RUNTIME = [
    "Contents/MacOS/mnema-asr",
    "Contents/Resources/bin/ffmpeg",
    "Contents/Resources/python/bin/python3.12",
    f"{SITE}/httpx/__init__.py",
    f"{SITE}/mnemonic/__init__.py",  # not mnema
    f"{SITE}/mnema.pth",
]


def _app(root: Path) -> Path:
    app = root / "Mnema.app"
    for rel in CODE + RUNTIME:
        (app / rel).parent.mkdir(parents=True, exist_ok=True)
        (app / rel).write_text(rel)
    (app / "Contents/MacOS/Mnema").chmod(0o755)
    (app / "Contents/Resources/python/bin/python3").symlink_to("python3.12")
    return app


def test_runtime_key_follows_the_runtime_not_the_code_or_the_signatures(tmp_path):
    app = _app(tmp_path)
    key = codesign.runtime_key(app)
    for rel in CODE:
        (app / rel).write_text("new code")
    (app / "Contents/_CodeSignature").mkdir()
    (app / "Contents/_CodeSignature/CodeResources").write_text("seal")
    (app / f"{SITE}/mnema-0.6.0.dist-info").mkdir()
    (app / f"{SITE}/mnema-0.6.0.dist-info/METADATA").write_text("x")
    assert codesign.runtime_key(app) == key

    changes = [
        lambda: (app / f"{SITE}/httpx/__init__.py").write_text("other"),
        lambda: (app / "Contents/MacOS/mnema-asr").chmod(0o755),
        lambda: (
            (app / "Contents/Resources/python/bin/python3").unlink()
            or (app / "Contents/Resources/python/bin/python3").symlink_to("python3.13")
        ),
        lambda: (app / f"{SITE}/httpx/__init__.py").rename(app / f"{SITE}/httpx/core.py"),
        lambda: (app / "Contents/Resources/bin/ffprobe").write_text(""),
    ]
    seen = {key}
    for change in changes:
        change()
        seen.add(codesign.runtime_key(app))
    assert len(seen) == len(changes) + 1


def test_code_archive_is_the_code_of_the_bundle_and_rebuilds_byte_for_byte(tmp_path):
    app = _app(tmp_path)

    archive = build.code_archive(app, "0.5.0")

    assert archive.name == "Mnema-0.5.0-mac-code.tar.gz" and archive.parent == tmp_path
    with tarfile.open(archive) as package:
        members = package.getmembers()
    assert [m.name for m in members] == sorted(CODE) and all(m.isfile() for m in members)
    assert {m.name for m in members if m.mode & 0o111} == {"Contents/MacOS/Mnema"}
    first = archive.read_bytes()
    os.utime(app / "Contents/Info.plist", (0, 0))
    assert build.code_archive(app, "0.5.0").read_bytes() == first


def _plist(app: Path, **keys: str) -> Path:
    (app / "Contents").mkdir(parents=True)
    info = {"CFBundleShortVersionString": "0.5.0", **keys}
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
    return app


def test_release_check_fails_when_the_runtime_changed_from_the_same_inputs(tmp_path, capsys):
    new = _plist(tmp_path / "new/Mnema.app", MnemaRuntime="b" * 64, MnemaRuntimeInputs="i")
    archive = tmp_path / "code.tar.gz"
    same_inputs = _plist(tmp_path / "a/Mnema.app", MnemaRuntime="a" * 64, MnemaRuntimeInputs="i")
    with pytest.raises(SystemExit, match="runtime"):
        build.check_code_update(same_inputs, new, archive, "0.5.0")

    other_inputs = _plist(tmp_path / "b/Mnema.app", MnemaRuntime="a" * 64, MnemaRuntimeInputs="j")
    build.check_code_update(other_inputs, new, archive, "0.5.0")
    without_key = _plist(tmp_path / "c/Mnema.app")  # 0.4: always the whole app
    build.check_code_update(without_key, new, archive, "0.5.0")
    assert "whole" in capsys.readouterr().out
