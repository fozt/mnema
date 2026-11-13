# Contributing

Issues and pull requests are welcome. Small, focused changes land best.

## Building from source

You need `git`, [uv](https://docs.astral.sh/uv/) (it brings Python 3.12), `ffmpeg` and, on a Mac,
Command Line Tools (`xcode-select --install`).

```bash
git clone https://github.com/fozt/mnema.git && cd mnema
uv sync
task lint && task test  # ruff and pytest
```

`uv sync` installs the core and the tools, without the apps' and the server's extras: the tests
need no recognition model, no Swift helper and no network, and all of them run on a Mac with a
plain `pytest`. The tools include `onnxruntime` and `numpy`, so the bundled silero model and the
Parakeet decoder are tested too. To run the server, the command or the Linux app from this
checkout, add the extras: `uv sync --all-extras`.

The Linux app from this checkout: `./install.sh client` (a tray app in its own environment under
`~/.local/share/mnema`). On a Mac it refuses and points to the DMG: Mnema.app is only built as a
release (below), there is no install from the sources. The `mnema` command from this checkout is
`uv run mnema …`; as a user installs it, from the wheel of a release:
`uv tool install "mnema[cli] @ <wheel>"`.

Recognition from the sources: on the server and Linux it is Parakeet Ultra on ONNX Runtime
(`uv sync --extra parakeet`); the files (about 910 MB) are downloaded into
`$XDG_CACHE_HOME/mnema/<repository>/<revision>` on the first job, or give a folder that already
has them in `MNEMA_PARAKEET_ONNX_DIR`. On a Mac recognition is `mnema-asr`, built below; point
`MNEMA_ASR` at it and Python calls it exactly as the app does.

On Linux the tray app needs `libgirepository1.0-dev libcairo2-dev pkg-config python3-dev` from apt
(`./install.sh client` installs them).

The macOS app: `cd mnema/bar/macos && swift build && swift test`. The Android app: see
[android/README.md](android/README.md).

### mnema-asr, the Mac's recognizer

The Swift package `mnema/bar/macos` has two programs: the panel `Mnema` and `mnema-asr`, which
recognizes speech on the Neural Engine with Parakeet Ultra in CoreML through
[FluidAudio](https://github.com/FluidInference/FluidAudio) (pinned in `Package.swift` to an exact
version, `Package.resolved` is in git). Python never loads the model: `core/mac_helper.py` starts
`mnema-asr`, writes one JSON request on its stdin and reads the replies from its stdout, in the
envelope of `core/worker.py`. The operations are `models`, `download`, `transcribe` and `speech`
(speech spans for cloud recognition); errors are codes, their texts are Python's. The protocol is
the same Swift library `MnemaASRProtocol` and the fixture `tests/fixtures/asr-helper-v1.json`,
checked by the Python tests and by `swift test`.

```bash
cd mnema/bar/macos
swift build -c release --product mnema-asr
.build/release/mnema-asr --version
echo '{"operation":"models","payload":{}}' | .build/release/mnema-asr
```

`download` fetches the model into `~/Library/Application Support/Mnema/models` and needs the
network; every other operation runs offline. To try it from Python, `export
MNEMA_ASR=$PWD/.build/release/mnema-asr` and run `mnema transcribe` on a short file. The WAV is
prepared by Python (ffmpeg, 16 kHz mono), so the times of the words are the same as on the server.

### The release app

The self-contained Mac app of a release (what the DMG installs) builds on an Apple Silicon Mac with
Command Line Tools, `curl` and uv:

```bash
task mac:app   # dist/ffmpeg, then dist/mac/Mnema.app, Mnema-X.Y.Z-mac.zip and Mnema-X.Y.Z.dmg
```

`scripts/build_ffmpeg.sh PREFIX` builds an LGPL ffmpeg and ffprobe from pinned sources.
`scripts/build_mac_app.py --out DIR --ffmpeg PREFIX` puts the Swift panel and `mnema-asr`, a
pinned Python 3.12 with the core and `menubar` packages, and ffmpeg into one bundle, signs it ad
hoc, and writes the zip for updates and the DMG (lzma, about 84 MB). The app has no
heavy Python: a list of packages that must not get in (torch, onnxruntime, yt-dlp, rich and
the like) fails the build if one comes, and the `mnema` command is a separate package. `--smoke` (or `scripts/smoke_mac_app.py APP`) then checks it under a temporary home: the
signature, that nothing loads libraries from outside the bundle and the system, that `mnema-asr`
answers and lists its models, that none of the forbidden packages is in the bundle, ffmpeg, and the
engine started the way the app starts it. Judge it by the exit code, and run it before any release
tag. `--sources` also writes `third-party-sources-X.Y.Z.tar.gz` (below).

The weights of the models are not in the bundle. Their attribution (CC-BY-4.0) is in
`licenses/MODELS.txt` of the app and in the README.

The README screenshots are rendered, not captured: on a Mac with Command Line Tools,
`uv run python -m scripts.screenshots` builds the panel and the settings from invented recordings
through the real panel and settings models, once in English and once in Russian, draws them with
the Mac app's preview and writes `docs/images/{panel,settings}-{light,dark}.png` (English, for
README.md) and `docs/images/{panel,settings}-{light,dark}.ru.png` (Russian, for README.ru.md).
Run it after changing how the panel or the settings look.

The server image (what `./install.sh server` runs) builds from the checkout with Docker:

```bash
docker build -t mnema-server:dev .
```

It installs the server's locked set from `uv.lock` (the extras in `mnema/extras.py`), for
linux/amd64 and linux/arm64. There is no torch and no GPU: recognition is ONNX Runtime on the CPU,
and the build checks the image by opening the bundled silero model and importing the server and
the command. The Parakeet files are downloaded into `/data/cache` on the first job. To try the
installer against that image without touching your own server, give it another server folder,
image and container name, then make an invite for the port it prints with `./mnema.sh` and the
same folder:

```bash
export MNEMA_SERVER_DIR=$(mktemp -d)/mnema-server
MNEMA_IMAGE=mnema-server:dev MNEMA_NAME=mnema-try ./install.sh server
./mnema.sh invite --url http://127.0.0.1:PORT
./mnema.sh users add anna --url http://127.0.0.1:PORT   # a second person on the same server
```

## How the code is organized

One Python package runs everywhere: the Mac app, the Linux client, the command line and the
server, installed with different extras (`mnema/extras.py`). The Mac and the server process
recordings with the same code, the Mac recognizes through `mnema-asr` and the server with ONNX;
Linux and the phone send recordings to a server.

| Path | What is there |
|---|---|
| `mnema/core/` | Media and speech recognition without UI: probing, audio extraction, the recognizer (`asr_engine.py`: `MacHelper` over `mnema-asr` on a Mac, Parakeet Ultra on ONNX Runtime in `parakeet.py` elsewhere), silero in `vad.py`, the engines (Parakeet and the cloud OpenAI one). A recognition runs in a short-lived process (on a Mac, `mnema-asr` itself), so model memory is freed when it ends |
| `mnema/llm/` | The cloud model through LiteLLM: titles, summaries, translation, providers, limits, hiding keys from logs; `config.py` reads the model and keys of the asking user |
| `mnema/ops.py` | The operation registry (`@op`): one definition serves the command line and the server |
| `mnema/server.py`, `mnema/api/` | The server: FastAPI, job queue, uploads, devices and invites, library sync, the model API (`api/model.py`) |
| `mnema/users.py`, `mnema/devices.py` | People of the server and their devices (`devices.db`), the user of the current request or job, where a user's files lie |
| `mnema/job_queue.py` | The server's queue: lanes, fairness between users, the memory check |
| `mnema/live/` | Recording sessions, recording folders and their naming, processing a recording; live text (`asr.py`: Parakeet phrase by phrase on the server) |
| `mnema/sync/`, `mnema/library/` | Syncing recording texts between devices and the server |
| `mnema/bar/` | The desktop app logic: state, panel model, settings, processing queue. The macOS UI is SwiftUI in `mnema/bar/macos` and talks to the Python engine over a JSON line protocol (`protocol.py`); Linux uses AppIndicator and GTK |
| `mnema/cli/` | The `mnema` command, in the separate `mnema[cli]` package |
| `android/` | The Android app (Kotlin, Compose) and its call recording helper |
| `tests/` | Python tests; `tests/fixtures/` holds cases shared with the Kotlin and Swift tests |

Settings live in `~/.config/mnema/.env` (one file for the command line and the app; the server's
copy is `data/config/mnema/.env` in its Docker volume); the recording library is `~/mnema`.

### Several people on one server

A user is a row in `devices.db`; every device and invite belongs to one (the owner, `owner`, is
the one that existed before users). The auth middleware puts the user of the token on the request,
and `users.as_user` carries it into job threads. Everything that belongs to a person is looked up
by that user:

- Jobs, uploads (`.mnema-owner`), results and `/api/live` are visible only to their user.
- The library of the owner stays `<data>/library`; others use `<data>/users/<name>/library`
  (`users.user_library`). The manifest carries a library number, and a client whose last sync was
  with another number asks before sending its recordings (`SyncEngine.send_to_library`).
- The model and keys of a user are one file, `config/mnema/users/<name>.env` (`MNEMA_MODEL`,
  `MNEMA_API_KEY`, `MNEMA_API_BASE`, `MNEMA_ASR_API_KEY` for the OpenAI recognition key,
  `DEEPL_API_KEY`). The server runs in strict mode (`users.set_strict`): `LLMSettings()` there
  reads only the file of the current user, never the process environment or the shared `.env`,
  and without a user it raises. The server has no model of its own and no command that sets one;
  the app writes the file through `/api/model` (`MnemaClient.model_*`). Outside the server (the
  Mac without a server, the command line) the settings come from the machine's `.env`.
  Keys are hidden in logs from both kinds of files (`llm/redact.py`).
- The queue serves users in turn. A local recognition job starts when `MemAvailable` (within the
  cgroup limit) is at least 2 GiB or no local job runs. Slots are `MNEMA_JOB_WORKERS`, by default
  a third of the cores; threads per job are `MNEMA_ASR_THREADS`, by default all cores but one
  split between the running local jobs. Short jobs, text jobs (summaries) and cloud recognition
  have lanes of their own.

Tests that touch any of this run with `users.set_strict(True)` and a user, so a place that forgets
the user fails a test instead of reading someone else's model.

## Recognition

Speech is recognized by one model, Parakeet Ultra (25 languages), and the cloud OpenAI engine. The
registry is `ENGINES` in `mnema/core/backends.py`; the command line (`--backend`), automatic choice,
the Mac app's Recognition list and the server's list follow it. Saved choices of the engines that
are gone (`whisper`, `gigaam`, `mlx`, `whisperx`, `groq/…`) are automatic, `backends.retired`.

A machine's recognizer implements `Recognizer` in `mnema/core/asr_engine.py`: `words(wav)` returns
words with their times, `speech(wav)` the spans of speech, `available()` tells without loading a
model. Replies, paragraphs, the language guess and the cleaning are one code over the words
(`replies`, `parakeet.guess_lang`, `parakeet.tidy`), the same for every recognizer, so the Mac and
the server give the same text up to the models' numerics.

- **Mac**: `MacHelper` over `mnema-asr` (above). The list of models lives in Swift (`mnema-asr
  models`); a new one is an entry there, with its repository, its pinned revision and the files it
  needs.
- **Server and Linux**: `OnnxParakeet` in `mnema/core/parakeet.py`: features on numpy, an encoder
  session and a `decoder_joint` session of ONNX Runtime, the greedy TDT decoding in Python. The
  files are pinned by repository, revision and sha256 (`ONNX_FILES`), downloaded into
  `$XDG_CACHE_HOME/mnema/<repository>/<revision>` and checked before they are used. Pauses are
  silero VAD on ONNX, `mnema/core/vad.py`, whose model is in the package.
  `scripts/export_parakeet_onnx.py` is a one-off export from NeMo (in a throwaway environment, not
  a dependency) that made the reference for the numpy features and the ONNX contract.
- **Cloud**: the choices are `CLOUD_MODELS` (`openai/gpt-4o-transcribe`,
  `openai/gpt-4o-mini-transcribe`), told by `is_cloud`. It takes the pauses from the machine's
  recognizer (`speech`), recognizes every utterance in one request with the previous text as the
  prompt, and needs the user's recognition key, which travels to the worker process with the job.
- A language Parakeet does not know is refused with `OnlyCloud`; audio never goes to the cloud by
  itself.

The Mac app's Recognition setting (`BarPrefs.recognition`: this Mac, the server or the cloud) and
`node.selected_processing` decide where a recording is processed. Live text on the server
(`mnema/live/asr.py`) recognizes a phrase after a pause with the same Parakeet, together with a
tail of up to 10 seconds of the track before it, and shows only the words of the phrase.

A new model is an entry of the Mac helper's list and a pinned ONNX export, or a new `Engine` in
`ENGINES` with `available()` and `run()`, plus an extra in `pyproject.toml` that `full` includes.
Model weights have licenses: add the attribution to the README and `build_mac_app.py` if they ask.

## Conventions

- **Tests check behaviour, not implementation.** Mock only external boundaries: the ONNX Runtime
  session, the `mnema-asr` process, LiteLLM, ffmpeg, yt-dlp, the network. Unit tests run without
  ffmpeg, models or network, none is only for Linux or measures time, and all run on a Mac.
  Behaviour that Python, Kotlin and Swift must share is pinned by the JSON cases in
  `tests/fixtures/`.
- **Models are pydantic v2**, closed sets are `StrEnum`, heavy and platform-specific imports
  (onnxruntime, numpy, litellm, fastapi) are lazy so every module imports on any OS.
- **A new server feature** is a core function plus an `@op` registration; the HTTP routing stays
  as is. Anything it reads for a person (a model, a key, a library) comes from the current user,
  never from a global.
- **Code, comments and source strings are in English.** The interface is English or Russian by the
  system language: texts go through `_()`/`_n()` from `mnema.i18n`, Russian lives in
  `mnema/i18n/ru/*.json`. Commit messages follow
  [Conventional Commits](https://www.conventionalcommits.org/).
- **No terminal commands in app texts**: messages in the apps name settings, not commands.

## Checks

[GitHub Actions](.github/workflows/ci.yml) run on every push and pull request: ruff and pytest on
Linux, `swift build` and `swift test` of the macOS app, and the Android unit tests, lint and helper
check. Run `task lint` and `task test` before sending a pull request; `task typecheck` (`ty`)
stays local; on a Mac it reports a few diagnostics that are the same on every run (Linux-only
modules), and a change must not add more.

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

- the reproducible source archive `mnema-X.Y.Z.tar.gz` (Linux app updates) and from it the wheel
  `mnema-X.Y.Z-py3-none-any.whl` of the command with `mnema-X.Y.Z-constraints.txt`, checked by a
  `uv tool install` into a clean directory (`mnema --version`);
- the LGPL ffmpeg (CI on `main` caches it until `scripts/build_ffmpeg.sh` changes; a tag reads
  that cache and never writes one);
- `Mnema-X.Y.Z-mac.zip` (Mac app updates), `Mnema-X.Y.Z.dmg` (installs) and
  `Mnema-X.Y.Z-mac-code.tar.gz` (the code-only update, below), checked by the smoke,
  and `third-party-sources-X.Y.Z.tar.gz`: the source code of the (L)GPL components inside the
  app (ffmpeg and LAME, the sdists of the (L)GPL Python packages, libsndfile and mpg123 from the
  soundfile wheel), with a README naming the binary each file belongs to and the ones whose
  source is offered on request instead (libquadmath in the scipy wheel)
  (`scripts/third_party_sources.py`);
- the signed `mnema-X.Y.Z.apk`;
- the server image `ghcr.io/fozt/mnema:X.Y.Z` for linux/amd64 and linux/arm64, each built and
  started on its own runner, which checks the healthcheck and an invite.

Then it writes the manifests `mnema-release.json` (with the `mac` block, its `mac.code` for the
code-only update and the `cli` block with the wheel) and `mnema-android.json`
with `scripts/build_release.py`, uploads everything to a draft release, checks the asset list,
tags the image `latest` and publishes the release as the latest. The apps read the manifests from
`https://github.com/fozt/mnema/releases/latest/download/`, the server installer pulls
`ghcr.io/fozt/mnema:latest`. GitHub creates the image package private on its first push: make it
public once in the package settings, otherwise the release stops before it is published.

**A code-only Mac update** carries just the files that change with Mnema's own code (the Swift
panel, `Info.plist`, the resources and the Python package `mnema`), a few megabytes. The build puts
a key of everything else in the bundle, `MnemaRuntime`, into `Info.plist` and the manifest. An app
whose key is the same copies itself (an APFS clone), swaps these files for the new ones, signs the
copy and replaces the old app; any failure after the download falls back to the whole zip. A
release with different libraries, a new Python, ffmpeg or a new `mnema-asr` has a new key and
everyone takes the whole zip. The release checks that the archive applies to the bundle of its own
build and passes the bundle checks. After a code-only and after a full update check by hand once
that macOS still keeps the microphone and system audio permissions; if it does not, release without
`mac.code` and everyone takes the zip.

The APK needs four repository secrets: `ANDROID_KEYSTORE_BASE64` (`base64 -i key.jks`),
`ANDROID_KEYSTORE_PASSWORD`, `ANDROID_KEY_ALIAS` and `ANDROID_KEY_PASSWORD`. Without them the
release stops before anything is uploaded.

## Reporting issues

Include what you did, what you expected and what happened, your platform (macOS version, Linux
distribution, phone model) and the log (`~/.cache/mnema/bar.log` or `docker logs mnema`). For
recognition quality issues attach a short sample if you can.
