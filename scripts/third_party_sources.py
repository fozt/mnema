"""third-party-sources-X.Y.Z.tar.gz: the one release asset with the source code of the GNU LGPL
and GPL components inside Mnema.app.

scripts/build_mac_app.py --sources writes it next to the zip and the DMG. Inside, under
third-party-sources-X.Y.Z/: the ffmpeg and LAME tarballs and licenses that
scripts/build_ffmpeg.sh left in its PREFIX and that script itself, the sdists of the (L)GPL
Python packages as uv.lock pins them, the LGPL sources of the libsndfile inside the soundfile
wheel, and README.txt: what each file is and which binary of the app it belongs to. Every
download is checked against its sha256.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import tarfile
import tomllib
import urllib.request
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parent.parent
SITE = "python/lib/python3.12/site-packages"  # inside Mnema.app/Contents/Resources
ISSUES = "https://github.com/fozt/mnema/issues"


class Source(NamedTuple):
    name: str  # the file in the archive
    url: str
    sha256: str
    about: str  # what it is and where it is in the app
    license: str = ""  # empty for a build script


_SNDFILE = f"{SITE}/_soundfile_data/libsndfile_arm64.dylib"
_SNDFILE_BUILD = "bastibe/libsndfile-binaries/a9a4f7fc136fe1da32ca729e9c3afdd5ce70a6a2"
# The libsndfile_arm64.dylib of the soundfile 0.14.0 wheel is the one libsndfile-binaries added
# at that commit: the LGPL parts its mac_build.sh downloads (ogg, vorbis, FLAC and opus are BSD)
SOUNDFILE = "0.14.0"
LIBSNDFILE = (
    Source(
        "libsndfile-1.2.2.tar.xz",
        "https://github.com/libsndfile/libsndfile/releases/download/1.2.2/libsndfile-1.2.2.tar.xz",
        "3799ca9924d3125038880367bf1468e53a1b7e3686a934f098b7e1d286cdb80e",
        f"libsndfile 1.2.2: {_SNDFILE}",
        "LGPL-2.1-or-later",
    ),
    Source(
        "mpg123-1.32.3.tar.bz2",
        "https://downloads.sourceforge.net/project/mpg123/mpg123/1.32.3/mpg123-1.32.3.tar.bz2",
        "2d9913a57d4ee8f497a182c6e82582602409782a4fb481e989feebf4435867b4",
        f"mpg123 1.32.3, linked into {_SNDFILE}",
        "LGPL-2.1-or-later",
    ),
    Source(
        "lame-3.100.tar.gz",
        "https://downloads.sourceforge.net/project/lame/lame/3.100/lame-3.100.tar.gz",
        "ddfe36cab873794038ae2c1210557ad34857a4b6bdc515785d1da9e175b1da1e",
        f"LAME 3.100, linked into {_SNDFILE}",
        "LGPL-2.0-or-later",
    ),
    Source(
        "libsndfile-binaries-mac_build.sh",
        f"https://raw.githubusercontent.com/{_SNDFILE_BUILD}/mac_build.sh",
        "8074171e8dc00433e69d78f2191f28ae4f1815a2e7f1a5cff4d71ce4f09b4172",
        f"the script that built {_SNDFILE}",
    ),
)


def _version(site: Path, dist: str) -> str | None:
    found = sorted(site.glob(f"{dist}-*.dist-info"))
    return found[0].name.removesuffix(".dist-info").split("-", 1)[1] if found else None


def native_sources(site: Path) -> list[Source]:
    """The pinned sources of the LGPL libraries inside the wheels in site."""
    version = _version(site, "soundfile")
    if version is None:
        return []
    if version != SOUNDFILE:
        raise SystemExit(
            f"soundfile {version} may carry another libsndfile: "
            "update LIBSNDFILE in scripts/third_party_sources.py"
        )
    return list(LIBSNDFILE)


def on_request(site: Path) -> list[str]:
    """LGPL libraries inside wheels whose exact source is not pinned: the wheels do not say
    which GCC their libquadmath comes from. Their source is offered on request."""
    return [
        f"libquadmath from GCC (LGPL-2.1-or-later): {SITE}/{path.relative_to(site)}"
        for path in sorted(site.glob("*/.dylibs/libquadmath*"))
    ]


def sdists(names: list[str]) -> list[Source]:
    """The sdists of the named distributions, as uv.lock pins them."""
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    wanted = {n.lower().replace("_", "-") for n in names}
    found = []
    for pkg in lock.get("package", []):
        sdist = pkg.get("sdist")
        if pkg["name"] not in wanted or not sdist:
            continue
        url = sdist["url"]
        about = f"the {pkg['name']} {pkg['version']} package in {SITE}"
        found.append(
            Source(url.rsplit("/", 1)[-1], url, sdist["hash"].removeprefix("sha256:"), about)
        )
        wanted.discard(pkg["name"])
    if wanted:
        raise SystemExit(f"no sdist in uv.lock for: {', '.join(sorted(wanted))}")
    return found


def fetch(source: Source) -> bytes:
    with urllib.request.urlopen(source.url, timeout=300) as response:  # noqa: S310 - pinned https
        data = response.read()
    if hashlib.sha256(data).hexdigest() != source.sha256:
        raise SystemExit(f"{source.url}: sha256 differs from the pinned one")
    return data


def _readme(version: str, files: dict[str, tuple[bytes, list[str]]], requested: list[str]) -> str:
    text = (
        f"Source code of the GNU LGPL and GPL components of Mnema.app {version}, the app in\n"
        f"Mnema-{version}.dmg and Mnema-{version}-mac.zip of the same release. Paths are inside\n"
        "Mnema.app/Contents/Resources.\n"
    )
    for name, (_, about) in files.items():
        text += f"\n{name}\n" + "".join(f"    {line}\n" for line in about)
    if requested:
        text += f"\nNot in this archive, the source is provided on request at {ISSUES}:\n"
        text += "".join(f"    {line}\n" for line in requested)
    return text


def _member(name: str, size: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size, info.mode = size, 0o644  # mtime 0, no owner: the same inputs give the same archive
    return info


def write(out: Path, version: str, ffmpeg: Path, site: Path, copyleft: list[str]) -> Path:
    """out/third-party-sources-<version>.tar.gz. ffmpeg is the PREFIX of build_ffmpeg.sh, site the
    app's site-packages, copyleft the (L)GPL distributions in it."""
    files: dict[str, tuple[bytes, list[str]]] = {}  # name in the archive → data, what it is

    def add(name: str, data: bytes, about: str) -> None:
        if name in files and files[name][0] != data:
            raise SystemExit(f"two different files named {name}")
        files.setdefault(name, (data, []))[1].append(about)

    tools = "bin/ffmpeg and bin/ffprobe"
    for path in sorted((ffmpeg / "src").iterdir()):
        add(path.name, path.read_bytes(), f"{tools}, built by build_ffmpeg.sh")
    for path in sorted((ffmpeg / "licenses").iterdir()):
        add(f"licenses/{path.name}", path.read_bytes(), f"license of {tools}")
    add("build_ffmpeg.sh", (ROOT / "scripts" / "build_ffmpeg.sh").read_bytes(), f"builds {tools}")
    for source in [*sdists(copyleft), *native_sources(site)]:
        about = f"{source.about} ({source.license})" if source.license else source.about
        known = files.get(source.name)  # LAME is in both ffmpeg and libsndfile
        if known and hashlib.sha256(known[0]).hexdigest() == source.sha256:
            add(source.name, known[0], about)
        else:
            add(source.name, fetch(source), about)

    top = f"third-party-sources-{version}"
    readme = _readme(version, files, on_request(site)).encode()
    archive = out / f"{top}.tar.gz"
    part = out / f".{archive.name}.part"
    out.mkdir(parents=True, exist_ok=True)
    with (
        part.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as package,
    ):
        for name, data in [
            ("README.txt", readme),
            *((n, d) for n, (d, _) in sorted(files.items())),
        ]:
            package.addfile(_member(f"{top}/{name}", len(data)), io.BytesIO(data))
    part.replace(archive)
    return archive
