#!/bin/sh
# LGPL ffmpeg and ffprobe for the Mac app (arm64, macOS 14+): ./scripts/build_ffmpeg.sh PREFIX
#
# PREFIX/bin       ffmpeg, ffprobe (only system libraries and frameworks)
# PREFIX/src       the exact source tarballs (shipped with the release for the LGPL)
# PREFIX/licenses  license texts
#
# Needs only clang, make, curl and tar: no pkg-config, so no libraries found through it.
# Codecs: native decoders and the native aac encoder, libmp3lame (static, from source),
# VideoToolbox (hevc_videotoolbox) and AudioToolbox.
set -eu

PREFIX=${1:?usage: build_ffmpeg.sh PREFIX}

FFMPEG_VERSION=9.0.2
FFMPEG_URL="https://ffmpeg.org/releases/ffmpeg-$FFMPEG_VERSION.tar.xz"
FFMPEG_SHA256=8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e
LAME_VERSION=3.100
LAME_URL="https://downloads.sourceforge.net/project/lame/lame/$LAME_VERSION/lame-$LAME_VERSION.tar.gz"
LAME_SHA256=ddfe36cab873794038ae2c1210557ad34857a4b6bdc515785d1da9e175b1da1e

if [ "$(uname -s)" != Darwin ] || [ "$(uname -m)" != arm64 ]; then
    echo "build_ffmpeg.sh builds for macOS arm64 only" >&2
    exit 1
fi

mkdir -p "$PREFIX/bin" "$PREFIX/src" "$PREFIX/licenses"
PREFIX=$(cd "$PREFIX" && pwd)
WORK=$(mktemp -d "${TMPDIR:-/tmp}/mnema-ffmpeg.XXXXXX")
trap 'rm -rf "$WORK"' EXIT
DEPS="$WORK/deps"
JOBS=$(sysctl -n hw.ncpu)

export MACOSX_DEPLOYMENT_TARGET=14.0
TARGET_FLAGS="-arch arm64 -mmacosx-version-min=$MACOSX_DEPLOYMENT_TARGET"
# Homebrew and other local prefixes stay out of the build
export PATH=/usr/bin:/bin:/usr/sbin:/sbin
unset CPATH C_INCLUDE_PATH LIBRARY_PATH PKG_CONFIG_PATH 2>/dev/null || true

fetch() {  # url sha256 → PREFIX/src/<name>, reused when the checksum matches
    file="$PREFIX/src/${1##*/}"
    if [ -f "$file" ] && [ "$(shasum -a 256 "$file" | cut -d' ' -f1)" = "$2" ]; then
        return
    fi
    curl -fsSL --retry 3 -o "$file.part" "$1"
    got=$(shasum -a 256 "$file.part" | cut -d' ' -f1)
    if [ "$got" != "$2" ]; then
        rm -f "$file.part"
        echo "checksum mismatch for $1: $got" >&2
        exit 1
    fi
    mv "$file.part" "$file"
}

fetch "$LAME_URL" "$LAME_SHA256"
fetch "$FFMPEG_URL" "$FFMPEG_SHA256"

echo "building lame $LAME_VERSION"
tar -xf "$PREFIX/src/lame-$LAME_VERSION.tar.gz" -C "$WORK"
(
    cd "$WORK/lame-$LAME_VERSION"
    ./configure --prefix="$DEPS" --build=aarch64-apple-darwin --host=aarch64-apple-darwin \
        --enable-static --disable-shared --disable-frontend --disable-decoder \
        --disable-dependency-tracking CC=clang CFLAGS="$TARGET_FLAGS -O2" >/dev/null
    make -j"$JOBS" >/dev/null
    make install >/dev/null
)

echo "building ffmpeg $FFMPEG_VERSION"
tar -xf "$PREFIX/src/ffmpeg-$FFMPEG_VERSION.tar.xz" -C "$WORK"
(
    cd "$WORK/ffmpeg-$FFMPEG_VERSION"
    # --disable-autodetect: only what is listed, never a library the build machine happens to have
    ./configure --prefix="$WORK/ffmpeg" --cc=clang --arch=arm64 --target-os=darwin \
        --enable-static --disable-shared --disable-ffplay --disable-doc --disable-debug \
        --disable-autodetect --enable-pthreads --enable-zlib --enable-bzlib \
        --enable-securetransport --enable-videotoolbox --enable-audiotoolbox \
        --enable-libmp3lame \
        --extra-cflags="$TARGET_FLAGS -I$DEPS/include" \
        --extra-ldflags="$TARGET_FLAGS -L$DEPS/lib" >/dev/null
    if grep -qE '#define CONFIG_(GPL|NONFREE) 1' config.h; then
        echo "the build is not LGPL" >&2
        exit 1
    fi
    make -j"$JOBS" >/dev/null
    make install >/dev/null
    cp COPYING.LGPLv2.1 "$PREFIX/licenses/ffmpeg-COPYING.LGPLv2.1"
    cp LICENSE.md "$PREFIX/licenses/ffmpeg-LICENSE.md"
)
cp "$WORK/lame-$LAME_VERSION/COPYING" "$PREFIX/licenses/lame-COPYING"

for name in ffmpeg ffprobe; do
    install -m 755 "$WORK/ffmpeg/bin/$name" "$PREFIX/bin/$name"
    strip -x "$PREFIX/bin/$name"
    foreign=$(otool -L "$PREFIX/bin/$name" | tail -n +2 | awk '{print $1}' \
        | grep -vE '^(/usr/lib/|/System/Library/)' || true)
    if [ -n "$foreign" ]; then
        echo "$name links outside the system: $foreign" >&2
        exit 1
    fi
done
"$PREFIX/bin/ffmpeg" -hide_banner -version | head -1
