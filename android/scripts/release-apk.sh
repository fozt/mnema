#!/usr/bin/env bash
# Builds the APK of a version signed with the permanent key into dist/mnema-VERSION.apk at the repository root; CI builds it the same way.
set -euo pipefail

version=${1:-}
if [[ ! $version =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]?)\.(0|[1-9][0-9]{0,3})$ ]]; then
  echo "Usage: $0 X.Y.Z, where Y is below 100 and Z below 10000" >&2
  exit 2
fi

signing=$HOME/.config/mnema/android/signing.env
if [[ -z ${MNEMA_KEYSTORE:-} && -f $signing ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$signing"
  set +a
fi
for name in MNEMA_KEYSTORE MNEMA_KEYSTORE_PASSWORD MNEMA_KEY_ALIAS MNEMA_KEY_PASSWORD; do
  if [[ -z ${!name:-} ]]; then
    echo "No $name: set the signing variables or the file $signing" >&2
    exit 1
  fi
done
if [[ ! -f $MNEMA_KEYSTORE ]]; then
  echo "No signing key file: $MNEMA_KEYSTORE" >&2
  exit 1
fi
# Gradle resolves a relative path from app/
MNEMA_KEYSTORE=$(realpath "$MNEMA_KEYSTORE")
export MNEMA_KEYSTORE

android=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
dist=$(dirname "$android")/dist
(cd "$android" && ./gradlew --console=plain -PmnemaVersion="$version" assembleRelease)
mkdir -p "$dist"
apk=$dist/mnema-$version.apk
cp "$android/app/build/outputs/apk/release/app-release.apk" "$apk"
echo "$apk"
echo "SHA-256: $(sha256sum "$apk" | cut -d ' ' -f 1)"
