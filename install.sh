#!/usr/bin/env bash
# Two explicit modes: the app (client) or the server in Docker (install-server.sh, with this clone).
set -euo pipefail
cd "$(dirname "$0")"

# Messages in Russian for a Russian interface language (looked up as mnema.i18n.detect does).
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

# usage [client]: the short overview, or the app's options (the server's: install-server.sh)
usage() {
  if [ -n "$RU" ]; then
    case "${1:-}" in
      client) cat <<'HELP'
Использование: ./install.sh client [ПРИГЛАШЕНИЕ]

Ставит приложение Mnema на Linux. ПРИГЛАШЕНИЕ — ссылка, которую напечатал сервер, в кавычках:
  ./install.sh client 'http://server:12345#invite=…'
Его можно вставить и позже, в настройках приложения: «Сервер».
HELP
      ;;
      *) cat <<'HELP'
Использование:
  ./install.sh server    сервер Mnema на Linux, в Docker
  ./install.sh client    приложение Mnema на Linux (на Mac — DMG)

Подробнее: ./install.sh server --help, ./install.sh client --help
и https://github.com/fozt/mnema#install
HELP
      ;;
    esac
  else
    case "${1:-}" in
      client) cat <<'HELP'
Usage: ./install.sh client [INVITATION]

Installs the Mnema app on Linux. INVITATION is the link the server printed, in quotes:
  ./install.sh client 'http://server:12345#invite=…'
It can also be pasted later in the app settings: "Server".
HELP
      ;;
      *) cat <<'HELP'
Usage:
  ./install.sh server    the Mnema server on Linux, in Docker
  ./install.sh client    the Mnema app on Linux (on a Mac, the DMG)

More: ./install.sh server --help, ./install.sh client --help
and https://github.com/fozt/mnema#install
HELP
      ;;
    esac
  fi
}

if [ $# -eq 0 ]; then usage; exit 1; fi
if [ "$1" = --help ] || [ "$1" = -h ]; then usage; exit 0; fi
MODE="$1"; shift
case "$MODE" in client|server) ;; *) usage; exit 1;; esac
# The server: install-server.sh, with this clone's files (it can build the image here)
if [ "$MODE" = server ]; then
  MNEMA_INSTALL_AGAIN="./install.sh server" exec bash ./install-server.sh "$@"
fi
CONNECTION=""
while [ $# -gt 0 ]; do
  case "$1" in
    --help|-h) usage "$MODE"; exit 0;;
    -*) usage "$MODE"; exit 1;;
    *) [ -z "$CONNECTION" ] || { usage "$MODE"; exit 1; }; CONNECTION="$1"; shift;;
  esac
done

as_root() {
  if [ "$(id -u)" = 0 ]; then "$@"; else sudo "$@"; fi
}

OS="$(uname -s)"

# Mnema.app is built only as the DMG of a release (scripts/build_mac_app.py), for arm64 only
if [ "$OS" = Darwin ]; then
  if [ "$(uname -m)" != arm64 ]; then
    say "The Mnema app needs a Mac with Apple silicon. On this Mac, use the mnema command with a server: https://github.com/fozt/mnema#command-line" \
      "Приложению Mnema нужен Mac на Apple silicon. На этом Mac пользуйтесь командой mnema с сервером: https://github.com/fozt/mnema/blob/main/README.ru.md#командная-строка"
    exit 1
  fi
  say "Mnema for Mac installs from the DMG: https://github.com/fozt/mnema/releases/latest" \
    "Mnema для Mac ставится из DMG: https://github.com/fozt/mnema/releases/latest"
  exit 1
fi
export PATH="$HOME/.local/bin:$PATH:/usr/local/sbin:/usr/sbin:/sbin"

# System packages: ffmpeg for everyone; the laptop also needs the microphone, system audio, tray.
if [ "$OS" = "Linux" ] && command -v apt-get >/dev/null; then
  missing=()
  command -v ffmpeg >/dev/null || missing+=(ffmpeg)
  command -v curl >/dev/null || missing+=(curl)
  ldconfig -p 2>/dev/null | grep libportaudio >/dev/null || missing+=(libportaudio2)
  command -v parec >/dev/null || missing+=(pulseaudio-utils)
  for pkg in gir1.2-ayatanaappindicator3-0.1 libgirepository1.0-dev libcairo2-dev pkg-config python3-dev libnotify-bin zenity; do
    dpkg -s "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
  done
  if [ ${#missing[@]} -gt 0 ]; then
    say "Installing system packages: ${missing[*]}" "Ставлю системные пакеты: ${missing[*]}"
    as_root apt-get update -qq
    as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y "${missing[@]}"
  fi
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

# One locked runtime set without the dev tools; packages only, model weights download on the
# first job. `uv sync` is exact, so the app and a developer's plain `uv sync` cannot share this
# checkout's .venv: the app gets its own environment (the checkout stays its source, editable).
# Set for this one command, so an exported UV_PROJECT_ENVIRONMENT never redirects it and the
# app's processes never inherit it.
VENV="${XDG_DATA_HOME:-$HOME/.local/share}/mnema/app/.venv"
# The extras of each role are listed once, in mnema/extras.py (the update reads them too)
role_extras="$(uv run --no-project --python 3.12 -- python -I "$PWD/mnema/extras.py" linux)"
extras=()
for extra in $role_extras; do extras+=(--extra "$extra"); done
UV_PROJECT_ENVIRONMENT="$VENV" uv sync --locked --python 3.12 --no-dev "${extras[@]}"

"$VENV/bin/python" -m mnema.install_setup client --venv "$VENV" --connection "$CONNECTION"

# The tray app, when there is a desktop.
if [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
  "$VENV/bin/mnema-bar" install || {
    say "The app didn't install. Try again: $VENV/bin/mnema-bar install" \
      "Приложение не установилось. Повторите: $VENV/bin/mnema-bar install"
    exit 1
  }
fi
say "Done. Restart Mnema if it was running." "Готово. Перезапустите Mnema, если она была запущена."
