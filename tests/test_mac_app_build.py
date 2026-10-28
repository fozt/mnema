"""The self-contained Mnema.app runs its own Python from wherever the bundle is copied."""

import os
import subprocess
from pathlib import Path

import pytest

from scripts import build_mac_app as build

# a fake interpreter: says where it is, what it was asked and which bundle it belongs to
_PYTHON = '#!/bin/sh\necho "python=$0 bundle=$MNEMA_APP_BUNDLE args=$*"\n'


def _bundle(root: Path) -> Path:
    res = root / "Mnema.app" / "Contents" / "Resources"
    python = res / "python" / "bin" / build.PY
    python.parent.mkdir(parents=True)
    python.write_text(_PYTHON)
    python.chmod(0o755)
    (res / "bin").mkdir()
    for name, module in build.WRAPPERS.items():
        wrapper = res / "bin" / name
        wrapper.write_text(build.WRAPPER.format(name=name, module=module, py=build.PY))
        wrapper.chmod(0o755)
    return root / "Mnema.app"


def _run(path: Path, *args: str) -> str:
    return subprocess.run([str(path), *args], capture_output=True, text=True, check=True).stdout


@pytest.mark.parametrize("name", list(build.WRAPPERS))
def test_command_runs_the_python_of_its_own_bundle_through_a_link(tmp_path, name):
    app = _bundle(tmp_path / "first")
    moved = tmp_path / "second" / "Mnema.app"
    moved.parent.mkdir()
    app.rename(moved)  # copied out of the image to another folder
    link = tmp_path / "bin" / name
    link.parent.mkdir()
    link.symlink_to(moved / "Contents/Resources/bin" / name)
    out = _run(link, "--version")
    real = Path(os.path.realpath(moved))
    assert f"python={real}/Contents/Resources/python/bin/{build.PY} " in out
    assert f"bundle={real} " in out
    assert out.rstrip().endswith("--version")


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
    dest = tmp_path / "licenses"

    assert build.package_licenses(site, dest) == ["soxr"]

    assert (dest / "python/soxr-1.1.0/licenses/COPYING.LGPL").read_text() == "lgpl"
    assert (dest / "python/soundfile-0.14.0/LICENSE").read_text() == "bsd"
    assert not (dest / "python/soxr-1.1.0/RECORD").exists()
    listed = (dest / "THIRD-PARTY.txt").read_text().splitlines()
    assert listed == [
        "httpx 0.28.1: BSD-3-Clause",
        "soundfile 0.14.0: BSD License",
        "soxr 1.1.0: LGPL-2.1-or-later",
    ]
    offer = (dest / "SOURCE-OFFER.txt").read_text()
    assert "soxr 1.1.0: LGPL-2.1-or-later" in offer and "soundfile 0.14.0: libsndfile" in offer
    assert "httpx" not in offer


class _Response:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self.data


def test_copyleft_sources_are_the_sdists_uv_lock_pins(tmp_path, monkeypatch):
    import hashlib
    import urllib.request

    url = "https://files.pythonhosted.org/packages/aa/soxr-1.1.0.tar.gz"
    root = tmp_path / "project"
    root.mkdir()
    (root / "uv.lock").write_text(
        '[[package]]\nname = "soxr"\nversion = "1.1.0"\n'
        f'sdist = {{ url = "{url}", hash = "sha256:{hashlib.sha256(b"genuine").hexdigest()}" }}\n'
    )
    monkeypatch.setattr(build, "ROOT", root)
    served = {"data": b"tampered"}
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout: _Response(served["data"]))

    with pytest.raises(SystemExit, match="sha256"):
        build.copyleft_sources(["soxr"], tmp_path / "bad")
    served["data"] = b"genuine"
    saved = build.copyleft_sources(["soxr"], tmp_path / "src")
    assert [(p.name, p.read_bytes()) for p in saved] == [("soxr-1.1.0.tar.gz", b"genuine")]
    with pytest.raises(SystemExit, match="no sdist"):
        build.copyleft_sources(["not-locked"], tmp_path / "none")


def test_trim_drops_build_tools_and_test_suites_but_keeps_what_imports_them(tmp_path):
    home = tmp_path / "python"
    site = home / "lib" / build.PY / "site-packages"
    kept = [
        "torch/testing/__init__.py",  # torch imports it
        "torch/_dynamo/test_case.py",
        "numpy/testing/__init__.py",
        "numpy/__init__.py",
        "pyobjc_core-12.dist-info/METADATA",
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
        "scipy/stats/tests/test_b.py",
        "bin/pip3",
    ]
    rel = {p: (home / p if p.startswith("bin/") else site / p) for p in kept + dropped}
    for path in rel.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
    assert build.trim(home) > 0
    assert [p for p in kept if not rel[p].exists()] == []
    assert [p for p in dropped if rel[p].exists()] == []


def test_trim_refuses_a_bundle_with_a_package_that_must_not_ship(tmp_path):
    home = tmp_path / "python"
    (home / "bin").mkdir(parents=True)
    (home / "lib" / build.PY / "site-packages" / "mutagen-1.47.0.dist-info").mkdir(parents=True)
    with pytest.raises(SystemExit, match="mutagen"):
        build.trim(home)
