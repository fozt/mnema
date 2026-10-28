"""third-party-sources-X.Y.Z.tar.gz: the one release asset with the (L)GPL sources of Mnema.app."""

import hashlib
import tarfile
import urllib.request
from pathlib import Path

import pytest

from scripts import third_party_sources as sources

_SDIST_URL = "https://files.pythonhosted.org/packages/aa/soxr-1.1.0.tar.gz"
_SNDFILE_URL = "https://example.org/libsndfile-1.2.2.tar.xz"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _Response:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self.data


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    """A checkout with uv.lock and build_ffmpeg.sh, the PREFIX of build_ffmpeg.sh, the app's
    site-packages and the internet they come from."""
    root = tmp_path / "project"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts/build_ffmpeg.sh").write_text("#!/bin/sh\n")
    (root / "uv.lock").write_text(
        '[[package]]\nname = "soxr"\nversion = "1.1.0"\n'
        f'sdist = {{ url = "{_SDIST_URL}", hash = "sha256:{_sha(b"soxr")}" }}\n'
    )
    monkeypatch.setattr(sources, "ROOT", root)
    ffmpeg = tmp_path / "ffmpeg"
    for name, data in {
        "src/ffmpeg-9.0.2.tar.xz": b"ffmpeg",
        "src/lame-3.100.tar.gz": b"lame",
        "licenses/lame-COPYING": b"lgpl",
    }.items():
        (ffmpeg / name).parent.mkdir(parents=True, exist_ok=True)
        (ffmpeg / name).write_bytes(data)
    site = tmp_path / "site-packages"
    (site / f"soundfile-{sources.SOUNDFILE}.dist-info").mkdir(parents=True)
    (site / "scipy/.dylibs").mkdir(parents=True)
    (site / "scipy/.dylibs/libquadmath.0.dylib").write_bytes(b"")
    pins = (
        sources.Source(
            "libsndfile-1.2.2.tar.xz", _SNDFILE_URL, _sha(b"sndfile"), "libsndfile", "LGPL"
        ),
        sources.Source(
            "lame-3.100.tar.gz", "https://example.org/lame", _sha(b"lame"), "LAME", "LGPL"
        ),
    )
    monkeypatch.setattr(sources, "LIBSNDFILE", pins)
    served = {_SDIST_URL: b"soxr", _SNDFILE_URL: b"sndfile", "https://example.org/lame": b"lame"}
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout: _Response(served[url]))
    return {"ffmpeg": ffmpeg, "site": site, "served": served, "out": tmp_path / "out"}


def _write(inputs) -> Path:
    return sources.write(inputs["out"], "0.4.0", inputs["ffmpeg"], inputs["site"], ["soxr"])


def test_archive_holds_each_source_once_and_names_the_binary_it_builds(inputs):
    archive = _write(inputs)

    assert archive.name == "third-party-sources-0.4.0.tar.gz"
    with tarfile.open(archive) as package:
        files = {
            m.name.removeprefix("third-party-sources-0.4.0/"): package.extractfile(m).read()
            for m in package.getmembers()
        }
    readme = files.pop("README.txt").decode()
    assert files == {
        "ffmpeg-9.0.2.tar.xz": b"ffmpeg",
        "lame-3.100.tar.gz": b"lame",
        "licenses/lame-COPYING": b"lgpl",
        "build_ffmpeg.sh": b"#!/bin/sh\n",
        "soxr-1.1.0.tar.gz": b"soxr",
        "libsndfile-1.2.2.tar.xz": b"sndfile",
    }
    lame = readme.split("lame-3.100.tar.gz\n")[1].split("\n\n")[0]
    assert "bin/ffmpeg" in lame and "LAME (LGPL)" in lame  # one tarball, both binaries
    assert "soxr 1.1.0 package" in readme
    on_request = readme.split("on request")[1]
    assert "scipy/.dylibs/libquadmath.0.dylib" in on_request


def test_the_same_inputs_give_the_same_archive(inputs):
    first = _write(inputs).read_bytes()
    assert _write(inputs).read_bytes() == first


def test_a_download_that_differs_from_its_pin_is_refused(inputs):
    inputs["served"][_SNDFILE_URL] = b"tampered"
    with pytest.raises(SystemExit, match="sha256"):
        _write(inputs)
    assert not inputs["out"].exists() or not list(inputs["out"].iterdir())


def test_another_soundfile_needs_its_libsndfile_sources_pinned_again(inputs):
    site = inputs["site"]
    (site / f"soundfile-{sources.SOUNDFILE}.dist-info").rename(site / "soundfile-9.9.dist-info")
    with pytest.raises(SystemExit, match="soundfile 9.9"):
        _write(inputs)


def test_a_copyleft_package_without_an_sdist_in_uv_lock_stops_the_build(inputs):
    with pytest.raises(SystemExit, match="no sdist"):
        sources.write(inputs["out"], "0.4.0", inputs["ffmpeg"], inputs["site"], ["not-locked"])
