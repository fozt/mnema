# Mnema

**Record a call, get a transcript and a short summary.** Speech is recognized on your Mac or on
your own server; only the transcript text goes to the language model you choose.

![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Android-lightgrey)
![License](https://img.shields.io/badge/license-MIT-green)

[Документация на русском →](README.ru.md)

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/images/panel-dark.png">
    <img src="docs/images/panel-light.png" width="360" alt="Mnema panel in the macOS menu bar">
  </picture>
</p>

## Features

- **One-click call recording** from the macOS menu bar, the Linux top panel or an Android phone.
  Your microphone and the other side of the call are kept as two separate tracks.
- **Local speech recognition**: Parakeet (25 European languages) and Whisper (everything else),
  plus GigaAM (Russian) on your server. No audio is sent to the cloud.
- **A title and a short summary** of every call from a cloud model of your choice: Gemini,
  DeepSeek, OpenAI, Claude, Qwen, OpenRouter or Groq. Free tiers are enough for daily use.
- **Plain files you own**: every recording is a folder with `transcript.txt` and `summary.txt`,
  ready for Obsidian, an LLM or an archive.
- **Your own server** for Linux and the phone, with invites per device and sync of texts
  between all devices.
- **Command line** for files and links: transcribe a video or a YouTube link, translate,
  compress, merge tracks.
- **MCP server** so Claude and other assistants can transcribe and read your recordings.

The interface is in Russian for now.

## How it works

```
Mac ─────────────── records, recognizes and summarizes on its own
                    (a server is optional: sync, or processing there)

Linux desktop ──┐
Android phone ──┼── record ──▶ your Mnema server ── recognizes and summarizes
Command line  ──┘
```

A Mac needs nothing else. Linux and the phone don't recognize speech themselves: they send the
recording to a Mnema server you run on a Linux machine, at home or on a VPS.

## Install

### Mac

Requirements: Apple Silicon, macOS 14.2 or later.

1. Download `Mnema-X.Y.Z.dmg` from [Releases](https://github.com/fozt/mnema/releases/latest), open
   it and drag Mnema to *Applications*. The app carries everything it needs: Python, the speech
   engines (Parakeet and Whisper) and ffmpeg.
2. Open Mnema from *Applications*. The app isn't notarized by Apple, so the first time macOS says
   it couldn't verify Mnema and offers only *Done* and *Move to Trash*: click *Done*, open *System
   Settings → Privacy & Security*, scroll down to *Security*, click *Open Anyway* next to "Mnema
   was blocked", confirm with your password or Touch ID, and click *Open Anyway* once more. macOS
   remembers the choice.
3. Click the Mnema icon in the menu bar, then *⋯ → Настройки…* (Settings). On the *Запись*
   (Recording) tab click *Выбрать модель…* (Choose model) and pick a provider, paste its key and
   choose a model. Mnema checks the key and the model before saving.
4. Click *Начать запись* (Start recording). macOS asks for microphone and system audio access the
   first time.

Speech models (about 1.3 GB) are downloaded on the first recording.

Opened straight from the disk image, Mnema asks to be moved to *Applications* first. On its first
start it turns on *Open at login* and links the `mnema` command to `~/.local/bin/mnema` unless
something else is already there. Updates come from GitHub Releases: *⋯ → Настройки… → Общие →
Проверить обновления* (Settings → General → Check for updates) downloads the new app and replaces
this one in place, so Mnema has to stay in a folder you can write to, such as *Applications*. To
remove Mnema, turn off *Open at login* (Settings → General), quit it, move it to the Trash and
delete the `~/.local/bin/mnema` link if Mnema made it; settings stay in `~/.config/mnema`,
recordings in `~/mnema`. The licenses of everything inside the app are in
`Mnema.app/Contents/Resources/licenses`.

### Server (Linux)

Needed for Linux desktops and the phone. Works on Debian and Ubuntu, with or without an NVIDIA
GPU; give it at least 6 GB of memory (or add swap with `--swap-size`).

```bash
git clone https://github.com/fozt/mnema.git && cd mnema
./install.sh server
mnema model              # choose the model for titles and summaries
```

The installer sets up the `mnema` systemd service and prints an invite: a link like
`http://host:port#invite=…` that connects one device and expires in 6 hours. Open the port from
the invite in your firewall.

The invite carries the server's address, which the installer finds on its own (Tailscale, a public
IP or the home network). If devices reach the server another way, for example by a domain name or
through port forwarding on a router, give that address with `--url http://my.host:port`.

| Command | What it does |
|---|---|
| `mnema invite` | A new invite (`--hours` up to 72) |
| `mnema devices` | Connected devices; `mnema devices revoke <id>` disconnects one |
| `mnema update --server` | Install the latest release |
| `journalctl -u mnema -f` | Server log |

The server speaks plain HTTP and requires a token from every device. If the network between your
devices and the server is not trusted, put a TLS proxy in front of it.

### Linux desktop

Debian or Ubuntu with GNOME, KDE, Xfce, Cinnamon or MATE. On plain GNOME (Fedora, Arch) enable the
*AppIndicator and KStatusNotifierItem Support* extension.

```bash
git clone https://github.com/fozt/mnema.git && cd mnema
./install.sh client 'http://host:port#invite=…'
```

Mnema appears in the applications menu and in the top panel. Without a server it only records;
recordings are processed once you connect one in *Настройки → Сервер*.

### Android

Android 14 or later. Recording the other side of a call is tested only on Pixel phones: on
other phones Mnema may record only your microphone, and it tells you when call audio is missing.

1. Download `mnema.apk` from [Releases](https://github.com/fozt/mnema/releases/latest) and install
   it (allow installs from your browser).
2. Follow the *Начало работы* (Getting started) checklist: file access, notifications, no battery
   limits.
3. Start the recording helper. It needs Wi-Fi debugging, which is in *Developer options* (tap
   *Build number* seven times to unlock them). The app walks you through pairing; no computer is
   needed. After a reboot, start the helper again from the record screen.
4. In *Настройки → Обработка → Сервер* tap *Добавить сервер* and paste an invite from your server.

The phone records both sides of a call as two tracks and keeps recordings in
`Documents/Mnema`. It updates itself from *Настройки → Обновление*.

## Using Mnema

**Recordings** live in `~/mnema` on a computer and in `Documents/Mnema` on the phone. Each one is
a folder named after the call, for example `2026-10-21_Release planning`:

| File | What it is |
|---|---|
| `transcript.txt` | The transcript, in paragraphs, each marked `mic` (you) or `system` (the other side) |
| `summary.txt` | The gist in one or two sentences, then the main points |
| `recording.mp3` | Both tracks mixed together |
| `mic.wav`, `system.wav` | The original tracks |

Rename a recording in the app or in Finder: the folder name is its title. Folders inside `~/mnema`
are sections; move recordings between them from the card menu. Texts sync between your devices
through the server; audio stays where it was recorded.

**Language** is detected from the speech. If it comes out wrong, pick it in the menu of the
recording that is being processed, or in *⋯ → Обработать запись…* (Process recording) for an
existing one.

**Without a model** you still get the transcript; the card offers *Составить пересказ* (Make
summary) once a model is set.

### Choosing a model

Any provider works; a key takes a minute to get:

| Provider | Where to get a key | Note |
|---|---|---|
| Gemini | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | Free tier, about 20 recordings a day per model |
| OpenRouter | [openrouter.ai/settings/keys](https://openrouter.ai/settings/keys) | Free models; allow them in the privacy settings |
| DeepSeek | [platform.deepseek.com/api_keys](https://platform.deepseek.com/api_keys) | Paid, very cheap |
| OpenAI, Claude, Qwen, Groq | the provider's console | Paid |

One recording costs one request. On a server the same steps run with `mnema model`.

## Command line

The Mac app and the installer link `mnema` into `~/.local/bin`; add that folder to `PATH` if your
shell doesn't have it.

```bash
mnema transcribe meeting.mov                    # → meeting.txt
mnema transcribe "https://youtu.be/VIDEO_ID"    # anything yt-dlp can download
mnema transcribe call.m4a --translate           # also a translation, call.ru.md
mnema rec --system                              # record mic and system audio, Ctrl-C to stop
mnema finish ~/mnema/2026-10-21_08-35 --from 0:10 --to 1:35   # process a part of a recording
mnema compress screen.mov                       # HEVC, roughly 9 GB → 1 GB
```

| Command | Purpose |
|---|---|
| `transcribe` | Speech to text; files, folders and links, several at once |
| `translate` | Translate a transcript or a media file with the model |
| `rec`, `finish` | Record a call; process a saved recording or a part of it |
| `live` | Live captions and live translation through a server |
| `compress`, `concat`, `merge`, `mp3` | Compress video, join files, mix tracks, extract audio |
| `model`, `update` | Choose the model, update Mnema |

On a Mac commands run locally; on Linux they run on the connected server. `--local` and
`--runner URL` choose explicitly. For YouTube videos that require sign-in add
`--cookies-from-browser chrome`.

### Claude and other assistants

`mnema-mcp` is an MCP server with four tools: `transcribe`, `translate`, `list_recordings` and
`read_transcript`. Add it to Claude Code:

```bash
claude mcp add mnema -- "$(dirname "$(readlink ~/.local/bin/mnema)")/mnema-mcp"
```

## Privacy

- **Audio** stays on the device that recorded it, or goes to your own server when that server
  processes it.
- **The model provider** receives only the transcript text, to write the title and the summary.
- **Sync** carries texts only: `transcript.txt`, `summary.txt` and processing history.
- **Cloud recognition** (Groq, OpenAI) is off unless you choose it on your server.
- **Updates** are checked against GitHub Releases.

## Troubleshooting

- **Log:** `~/.cache/mnema/bar.log` on a computer, `journalctl -u mnema` on the server.
- **No notifications on a Mac:** allow Mnema in *System Settings → Notifications*.
- **No microphone or call audio:** allow Mnema in *System Settings → Privacy & Security →
  Microphone* and *Screen & System Audio Recording*.
- **The phone can't start the helper:** turn on Wi-Fi debugging and stay on Wi-Fi.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md): building from source, the architecture, tests and
releases.

## License

[MIT](LICENSE)
