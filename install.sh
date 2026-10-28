#!/usr/bin/env bash
# Two explicit modes: the app (client) or a headless Linux server.
set -euo pipefail
cd "$(dirname "$0")"

# Messages in Russian for a Russian interface language (looked up as mnema.i18n.detect does),
# English otherwise.
RU=""
for value in "${MNEMA_UI_LANG:-}" "${LC_ALL:-}" "${LC_MESSAGES:-}" "${LANG:-}"; do
  value="${value//[[:space:]]/}"
  if [ -n "$value" ]; then
    case "$value" in [Rr][Uu]*) RU=1;; esac
    break
  fi
done

# say ENGLISH RUSSIAN
say() { if [ -n "$RU" ]; then printf '%s\n' "$2"; else printf '%s\n' "$1"; fi; }

usage() {
  if [ -n "$RU" ]; then
    cat <<'HELP'
Использование:
  ./install.sh client [ПРИГЛАШЕНИЕ]
  ./install.sh server [--swap-size 4G] [--threads 4] [--url http://адрес:порт]

Клиент на Mac обрабатывает записи сам. Модель для названия и пересказа выбирают в настройках
приложения: «Запись», «Выбрать модель…». С сервером записи видны на телефоне и других
устройствах, а при «Где обрабатывать» → «На сервере» Mac отправляет записи на подключённый
сервер, как Linux.
Клиент на Linux отправляет записи на сервер: распознаёт и пересказывает сервер. Движков у клиента
нет.
Приложение ставится в своё окружение ~/.local/share/mnema/app/.venv, а не в .venv этой папки.
Приглашение, которое напечатал сервер, вставьте в кавычках:
  ./install.sh client 'http://server:12345#invite=…'
Приглашение подключает одно устройство и действует 6 часов. Новое выдаёт сам сервер:
  mnema invite
Приглашение можно вставить и позднее, в настройках приложения: «Сервер».
Сервер сам выбирает свободный порт и сохраняет его для последующих установок.
--url: адрес, по которому сервер видят клиенты, например домен или проброшенный порт. Только для
server. Без него приглашение ведёт на адрес этого компьютера в сети: Tailscale, публичный, домашний.
Swap создаётся только с --swap-size: от 1G до 64G (G = GiB).
--threads: потоки процессора для распознавания, число или auto, то есть все доступные. Только для
server. Сохраняется в ~/.config/mnema/.env как MNEMA_ASR_THREADS; без флага остаётся прежнее
значение.
Модель сервера для названия и пересказа задаётся один раз командой mnema model от пользователя
службы. Она пишет строки MNEMA_MODEL и MNEMA_API_KEY в ~/.config/mnema/.env, их можно вписать
туда и руками.
Команда mnema — ссылка ~/.local/bin/mnema на эту установку.
HELP
  else
    cat <<'HELP'
Usage:
  ./install.sh client [INVITATION]
  ./install.sh server [--swap-size 4G] [--threads 4] [--url http://address:port]

The Mac client processes recordings itself. The model for titles and summaries is chosen in the
app settings: "Recording", "Choose model…". With a server the recordings show up on the phone and
other devices, and with "Where to process" set to "On the server" the Mac sends recordings to the
connected server, as Linux does.
The Linux client sends recordings to the server, which transcribes and summarizes them. The
client has no engines.
The app goes into its own environment ~/.local/share/mnema/app/.venv, not this folder's .venv.
Paste the invitation the server printed, in quotes:
  ./install.sh client 'http://server:12345#invite=…'
An invitation connects one device and is valid for 6 hours. The server issues new ones:
  mnema invite
The invitation can also be pasted later in the app settings: "Server".
The server picks a free port itself and keeps it for later installs.
--url: the address clients reach the server at, such as a domain or a forwarded port. Server
only. Without it the invitation leads to this computer's network address: Tailscale, public, home.
Swap is created only with --swap-size: 1G to 64G (G = GiB).
--threads: CPU threads for transcription, a number or auto for all available. Server only. Saved
in ~/.config/mnema/.env as MNEMA_ASR_THREADS; without the flag the previous value stays.
The server's model for titles and summaries is set once with mnema model as the service user. It
writes MNEMA_MODEL and MNEMA_API_KEY to ~/.config/mnema/.env, which can also be edited by hand.
The mnema command is the link ~/.local/bin/mnema to this installation.
HELP
  fi
}

if [ $# -eq 0 ]; then usage; exit 1; fi
if [ "$1" = --help ] || [ "$1" = -h ]; then usage; exit 0; fi
MODE="$1"; shift
case "$MODE" in client|server) ;; *) usage; exit 1;; esac
SWAP_SIZE=""
THREADS=""
URL=""
CONNECTION=""
while [ $# -gt 0 ]; do
  case "$1" in
    --help|-h) usage; exit 0;;
    --swap-size)
      if [ $# -lt 2 ] || ! [[ "$2" =~ ^([1-9]|[1-5][0-9]|6[0-4])[Gg]$ ]]; then
        say "--swap-size: 1G to 64G, for example --swap-size 4G" \
          "--swap-size: от 1G до 64G, например --swap-size 4G"
        exit 1
      fi
      SWAP_SIZE="${2%[Gg]}"; shift 2;;
    --threads)
      if [ "$MODE" != server ]; then
        say "--threads is only for the server install: ./install.sh server --threads N" \
          "--threads задаётся только при установке сервера: ./install.sh server --threads N"
        exit 1
      fi
      if [ $# -lt 2 ] || ! [[ "$2" =~ ^([1-9][0-9]{0,3}|auto)$ ]]; then
        say "--threads: a whole number from 1, or auto, for example --threads 8" \
          "--threads: целое число от 1 или auto, например --threads 8"
        exit 1
      fi
      THREADS="$2"; shift 2;;
    --url)
      if [ "$MODE" != server ] || [ $# -lt 2 ] || [ -z "$2" ]; then usage; exit 1; fi
      URL="$2"; shift 2;;
    -*) usage; exit 1;;
    *) [ -z "$CONNECTION" ] || { usage; exit 1; }; CONNECTION="$1"; shift;;
  esac
done
if [ "$MODE" != server ] && [ -n "$SWAP_SIZE" ]; then
  usage; exit 1
fi
if [ "$MODE" != client ] && [ -n "$CONNECTION" ]; then
  usage; exit 1
fi

as_root() {
  if [ "$(id -u)" = 0 ]; then "$@"; else sudo "$@"; fi
}

OS="$(uname -s)"
if [ "$MODE" = server ] && [ "$OS" != Linux ]; then
  say "The server installs on Linux." "Сервер ставится на Linux."
  exit 1
fi
# swift from the Command Line Tools builds Mnema.app: without them nothing starts
if [ "$OS" = Darwin ] && ! xcode-select -p >/dev/null 2>&1; then
  say "Install the Command Line Tools: xcode-select --install" \
    "Установите Command Line Tools: xcode-select --install"
  exit 1
fi
export PATH="$HOME/.local/bin:$PATH:/usr/local/sbin:/usr/sbin:/sbin"

# System packages: ffmpeg for everyone; the laptop also needs the microphone, system audio, tray.
if [ "$OS" = "Linux" ] && command -v apt-get >/dev/null; then
  missing=()
  command -v ffmpeg >/dev/null || missing+=(ffmpeg)
  command -v git >/dev/null || missing+=(git)
  command -v curl >/dev/null || missing+=(curl)
  if [ "$MODE" = client ]; then
    ldconfig -p 2>/dev/null | grep libportaudio >/dev/null || missing+=(libportaudio2)
    command -v parec >/dev/null || missing+=(pulseaudio-utils)
    for pkg in gir1.2-ayatanaappindicator3-0.1 libgirepository1.0-dev libcairo2-dev pkg-config python3-dev libnotify-bin zenity; do
      dpkg -s "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
    done
  fi
  if [ ${#missing[@]} -gt 0 ]; then
    say "Installing system packages: ${missing[*]}" "Ставлю системные пакеты: ${missing[*]}"
    as_root apt-get update -qq
    as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y "${missing[@]}"
  fi
elif [ "$OS" = "Darwin" ] && ! command -v ffmpeg >/dev/null && command -v brew >/dev/null; then
  brew install ffmpeg
fi
if ! command -v ffmpeg >/dev/null; then
  say "ffmpeg is required. Install it and run this again." \
    "Нужен ffmpeg. Установите его и повторите."
  exit 1
fi

if ! command -v uv >/dev/null; then
  say "Installing uv into ~/.local/bin…" "Ставлю uv в ~/.local/bin…"
  uv_installer="$(mktemp)"
  trap 'rm -f "$uv_installer"' EXIT
  curl --proto '=https' --tlsv1.2 -LsSf https://astral.sh/uv/install.sh -o "$uv_installer"
  env UV_INSTALL_DIR="$HOME/.local/bin" UV_NO_MODIFY_PATH=1 sh "$uv_installer"
  rm -f "$uv_installer"
  trap - EXIT
fi

if [ -n "$SWAP_SIZE" ]; then
  as_root bash "$PWD/scripts/setup-swap.sh" "$SWAP_SIZE"
fi

# One locked runtime set without the dev tools; packages only, model weights download on the
# first job. `uv sync` is exact, so the app and a developer's plain `uv sync` cannot share this
# checkout's .venv: the app gets its own environment (the checkout stays its source, editable).
# The server keeps .venv here: its systemd unit runs from this folder.
# Set for this one command, so an exported UV_PROJECT_ENVIRONMENT never redirects the server
# and the app's processes never inherit it.
if [ "$MODE" = server ]; then
  VENV="$PWD/.venv"
  role=server
else
  VENV="${XDG_DATA_HOME:-$HOME/.local/share}/mnema/app/.venv"
  if [ "$OS" = Darwin ]; then role=mac; else role=linux; fi
fi
# The extras of each role are listed once, in mnema/extras.py (the update reads them too)
role_extras="$(uv run --no-project --python 3.12 -- python -I "$PWD/mnema/extras.py" "$role")"
extras=()
for extra in $role_extras; do extras+=(--extra "$extra"); done
UV_PROJECT_ENVIRONMENT="$VENV" uv sync --locked --python 3.12 --no-dev "${extras[@]}"

if [ "$MODE" = server ]; then
  options=(server --repo "$PWD" --user "$(id -un)")
  SYSTEMD_DIR="${MNEMA_SYSTEMD_DIR:-/etc/systemd/system}"
  if command -v systemctl >/dev/null && [ -d "$SYSTEMD_DIR" ]; then
    options+=(--systemd "$SYSTEMD_DIR")
  fi
  if [ -n "$THREADS" ]; then options+=(--threads "$THREADS"); fi
  if [ -n "$URL" ]; then options+=(--url "$URL"); fi
  "$VENV/bin/python" -m mnema.install_setup "${options[@]}"
  exit 0
fi

"$VENV/bin/python" -m mnema.install_setup client --venv "$VENV" --connection "$CONNECTION"

# The menu bar / tray app, when there is a desktop.
if [ "$OS" = Darwin ] || [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
  "$VENV/bin/mnema-bar" install || {
    say "The app didn't install. Try again: $VENV/bin/mnema-bar install" \
      "Приложение не установилось. Повторите: $VENV/bin/mnema-bar install"
    exit 1
  }
fi
say "Done. Restart Mnema if it was running." "Готово. Перезапустите Mnema, если она была запущена."
