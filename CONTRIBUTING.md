# Contributing

Issues and pull requests are welcome. Small, focused changes land best.

## Building from source

You need `git`, `ffmpeg` and, on a Mac, Command Line Tools (`xcode-select --install`). The installer
brings [uv](https://docs.astral.sh/uv/) and Python 3.12 itself.

```bash
git clone https://github.com/fozt/mnema.git && cd mnema
./install.sh client      # the app from this checkout: Mnema.app on a Mac, the tray app on Linux
```

For development, install the core and the tools and run the checks. Tests don't need the
recognition engines:

```bash
uv sync
task lint && task test  # ruff and pytest
```

To run recognition from this checkout too, add the engines and the apps' extras:
`uv sync --all-extras`.

On Linux the tray app needs `libgirepository1.0-dev libcairo2-dev pkg-config python3-dev` from apt
(`./install.sh client` installs them).

The macOS app: `cd mnema/bar/macos && swift build && swift test`. The Android app: see
[android/README.md](android/README.md).

The self-contained Mac app of a release (what the DMG installs) builds on an Apple Silicon Mac with
Command Line Tools, `curl` and uv:

```bash
task mac:app   # dist/ffmpeg, then dist/mac/Mnema.app, Mnema-X.Y.Z-mac.zip and Mnema-X.Y.Z.dmg
```

`scripts/build_ffmpeg.sh PREFIX` builds an LGPL ffmpeg and ffprobe from pinned sources (about two
minutes). `scripts/build_mac_app.py --out DIR --ffmpeg PREFIX` puts the Swift panel, a copy of
uv's Python 3.12 with every package, the `mnema` and `mnema-mcp` commands and ffmpeg into one
bundle, signs it ad hoc and writes the zip and the DMG (about ten minutes, 1.4 GB unpacked).
`--smoke` (or `scripts/smoke_mac_app.py APP`) then checks it under a temporary home: the
signature, that nothing loads libraries from outside the bundle and the system, the bundled
commands, ffmpeg through mnema, and the engine started the way the app starts it. `./install.sh
client` keeps building Mnema.app from the checkout into `~/Applications` for development.

## How the code is organized

One Python package runs everywhere: the Mac app, the Linux client, the command line and the
server. The Mac and the server process recordings with the same code; Linux and the phone send
recordings to a server.

| Path | What is there |
|---|---|
| `mnema/core/` | Media and speech recognition without UI: probing, audio extraction, recognition engines (Parakeet, GigaAM, Whisper). Each recognition runs in a short-lived worker process, so model memory is freed when it ends |
| `mnema/llm/` | The cloud model through LiteLLM: titles, summaries, translation, providers, limits, hiding keys from logs |
| `mnema/ops.py` | The operation registry (`@op`): one definition serves the command line and the server |
| `mnema/server.py`, `mnema/api/` | The server: FastAPI, job queue, uploads, devices and invites, library sync |
| `mnema/live/` | Recording sessions, recording folders and their naming, processing a recording |
| `mnema/sync/`, `mnema/library/` | Syncing recording texts between devices and the server |
| `mnema/bar/` | The desktop app logic: state, panel model, settings, processing queue. The macOS UI is SwiftUI in `mnema/bar/macos` and talks to the Python engine over a JSON line protocol (`protocol.py`); Linux uses AppIndicator and GTK |
| `mnema/cli/` | The `mnema` command |
| `mnema/mcp/` | The `mnema-mcp` server |
| `android/` | The Android app (Kotlin, Compose) and its call recording helper |
| `tests/` | Python tests; `tests/fixtures/` holds cases shared with the Kotlin and Swift tests |

Settings live in `~/.config/mnema/.env` (one file for the command line, the app and the server);
the recording library is `~/mnema`.

## Adding a recognition engine

Local engines live in one registry, `ENGINES` in `mnema/core/backends.py`. The command line
(`--backend`), automatic choice, the Mac app's Recognition list and the server's
list all follow it.

1. Write a subclass of `Engine` in `mnema/core/backends.py` with `id` (stored in settings and
   history), `title`, `extra`, `langs` (`None` for any language) and `names_lang` (it works
   without a given language), plus `available()` (installed packages only, no imports of heavy
   modules) and `run()`. Add it to `ENGINES`: their order is the order automatic choice tries.
2. Add an extra with its packages to `pyproject.toml`, put it into `full` (the server installs
   every engine) and run `uv lock`.
3. To ship it in the Mac app, add the extra to `MAC` in `mnema/extras.py`: the DMG build,
   `./install.sh client` on a Mac and updates read that list, and `scripts/smoke_mac_app.py`
   checks that exactly these engines are installed in the bundle and that every library their
   extras pull in imports. Nothing else needs editing.

## Conventions

- **Tests check behaviour, not implementation.** Mock only external boundaries: speech engines,
  LiteLLM, ffmpeg, yt-dlp, the network. Unit tests run without ffmpeg, models or network.
  Behaviour that Python, Kotlin and Swift must share is pinned by the JSON cases in
  `tests/fixtures/`.
- **Models are pydantic v2**, closed sets are `StrEnum`, heavy and platform-specific imports
  (torch, mlx, whisperx, litellm, fastapi) are lazy so every module imports on any OS.
- **A new server feature** is a core function plus an `@op` registration; the HTTP routing stays
  as is.
- **Code, comments and source strings are in English.** The interface is English or Russian by the
  system language: texts go through `_()`/`_n()` from `mnema.i18n`, Russian lives in
  `mnema/i18n/ru/*.json`. Commit messages follow
  [Conventional Commits](https://www.conventionalcommits.org/).
- **No terminal commands in app texts**: messages in the apps name settings, not commands.

## Checks

[GitHub Actions](.github/workflows/ci.yml) run on every push and pull request: ruff and pytest on
Linux, `swift build` and `swift test` of the macOS app, and the Android unit tests, lint and helper
check. Run `task lint` and `task test` before sending a pull request; `task typecheck` (`ty`)
stays local, and without the engines (`uv sync --all-extras`) it reports their imports as
unresolved.

## Releases

A release is a tag `vX.Y.Z` on `main`. The version must fit the Android versionCode: Y below 100,
Z below 10000.

```bash
uv run python -m scripts.bump_version 0.3.1   # pyproject.toml, mnema/__init__.py, uv.lock
git commit -am "chore(release): v0.3.1" && git tag v0.3.1
git push origin main v0.3.1
```

Pushing the tag runs [the release workflow](.github/workflows/release.yml). It runs the same
checks as CI (nothing is published if one fails), checks that the tag matches the version and
builds, each in its own job:

- the reproducible source archive `mnema-X.Y.Z.tar.gz` (Linux app and server updates);
- the LGPL ffmpeg (CI on `main` caches it until `scripts/build_ffmpeg.sh` changes; a tag reads
  that cache and never writes one);
- `Mnema-X.Y.Z-mac.zip` (Mac app updates) and `Mnema-X.Y.Z.dmg` (installs), checked by the smoke,
  and the sdists of the (L)GPL Python packages inside the app;
- the signed `mnema-X.Y.Z.apk`.

Then it writes the manifests `mnema-release.json` (with the `mac` block) and `mnema-android.json`
with `scripts/build_release.py`, uploads everything with the ffmpeg and lame source tarballs,
the package sdists and the licenses to a draft release, checks the asset list and publishes the release as the latest. The
apps read the manifests from `https://github.com/fozt/mnema/releases/latest/download/`.

The APK needs four repository secrets: `ANDROID_KEYSTORE_BASE64` (`base64 -i key.jks`),
`ANDROID_KEYSTORE_PASSWORD`, `ANDROID_KEY_ALIAS` and `ANDROID_KEY_PASSWORD`. Without them the
release stops before anything is uploaded.

## Reporting issues

Include what you did, what you expected and what happened, your platform (macOS version, Linux
distribution, phone model) and the log (`~/.cache/mnema/bar.log` or `journalctl -u mnema`). For
recognition quality issues attach a short sample if you can.
