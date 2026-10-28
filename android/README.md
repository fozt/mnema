# Mnema for Android

A thin Mnema client for Pixel phones: records a call as two tracks (the other side and me);
recognition, the title and the summary are made on the chosen server, so the phone has no model and
no key.

## Environment

- JDK 21 with `javac` (not a JRE).
- Android SDK: platform `android-37.0`, build-tools `37.0.0`; the path in `ANDROID_HOME` or in
  `android/local.properties` as `sdk.dir=/path/to/Sdk` (the file is not committed).
- Gradle comes with the `./gradlew` wrapper (9.8.0), no separate install needed.

## Commands (from `android/`)

```bash
./gradlew --console=plain testDebugUnitTest lintDebug assembleRelease   # checks before a commit
python3 scripts/check-helper-deps.py   # helper classes before R8: only the platform and its own package
```

The release APK is `app/build/outputs/apk/release/app-release.apk`. It is built with R8, not
debuggable, and without signing variables it is signed with the debug key `~/.android/debug.keystore`
that Gradle creates on the first build. From the repository root: `task android:build`,
`task android:test`, `task android:install` (installs the APK on a connected phone with `adb`).

## Release

The phone version is released together with the desktop one: a GitHub release `vX.Y.Z` carries the
signed APK and `mnema-android.json`. The app checks
`https://github.com/fozt/mnema/releases/latest/download/mnema-android.json`, which always serves the
manifest of the latest release: `version`, `version_code`, `apk`, `sha256`, `size`, `min_sdk`.
`python -m scripts.build_release vX.Y.Z --apk dist/mnema-X.Y.Z.apk` writes it.

The permanent signing key lives outside git: `~/.config/mnema/android/mnema-release.jks`, its
parameters in `~/.config/mnema/android/signing.env` (`MNEMA_KEYSTORE`, `MNEMA_KEYSTORE_PASSWORD`,
`MNEMA_KEY_ALIAS`, `MNEMA_KEY_PASSWORD`).

A signed APK locally, from the repository root:

```bash
android/scripts/release-apk.sh 0.3.0   # dist/mnema-0.3.0.apk and its SHA-256
```

The script takes the signing variables from the environment, otherwise from `signing.env`. The
version goes to Gradle as `-PmnemaVersion=X.Y.Z`: versionName `X.Y.Z`, versionCode
`X*1000000 + Y*10000 + Z`, Y below 100, Z below 10000 (0.3.0 is 30000). Without the property it is
`0.0.0` and 1. Android does not install an APK signed with the debug key over one signed with the
permanent key: remove the app first.

In GitHub Actions the key comes from the repository secrets `ANDROID_KEYSTORE_BASE64` (the output of
`base64 -w0 mnema-release.jks`), `ANDROID_KEYSTORE_PASSWORD`, `ANDROID_KEY_ALIAS` and
`ANDROID_KEY_PASSWORD`. Without them no APK is published.

## Versions

Dependency versions are in `gradle/libs.versions.toml`. SDK: compileSdk 37, targetSdk 36, minSdk 34.
targetSdk is 36 because 37 closes the local network to apps without the `ACCESS_LOCAL_NETWORK`
permission.
