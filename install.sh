#!/usr/bin/env bash
# Two explicit modes: the app (client) or the server in Docker.
set -euo pipefail
cd "$(dirname "$0")"

# say, SERVER_DIR, server_commands
# shellcheck source=scripts/common.sh
source scripts/common.sh

# usage [server|client]: the short overview, or one mode's options
usage() {
  if [ -n "$RU" ]; then
    case "${1:-}" in
      server) cat <<'HELP'
Использование: ./install.sh server [--url http://адрес:порт] [--build] [--swap-size 4G]

Ставит сервер Mnema в Docker (нужен Docker Engine с Compose). Настройки и данные — в
~/mnema-server (или MNEMA_SERVER_DIR), команды сервера — ./mnema.sh в этой папке.

  --url АДРЕС      адрес, по которому сервер видят устройства, например домен; запоминается.
                   Без него — адрес этого компьютера в сети
  --build          собрать образ из этой папки, а не скачивать
  --swap-size 4G   добавить swap, от 1G до 64G
HELP
      ;;
      client) cat <<'HELP'
Использование: ./install.sh client [ПРИГЛАШЕНИЕ]

Ставит приложение Mnema. ПРИГЛАШЕНИЕ — ссылка, которую напечатал сервер, в кавычках:
  ./install.sh client 'http://server:12345#invite=…'
Его можно вставить и позже, в настройках приложения: «Сервер».
HELP
      ;;
      *) cat <<'HELP'
Использование:
  ./install.sh server    сервер Mnema на Linux, в Docker
  ./install.sh client    приложение Mnema на Linux (на Mac проще поставить DMG)

Подробнее: ./install.sh server --help, ./install.sh client --help
и https://github.com/fozt/mnema#install
HELP
      ;;
    esac
  else
    case "${1:-}" in
      server) cat <<'HELP'
Usage: ./install.sh server [--url http://address:port] [--build] [--swap-size 4G]

Installs the Mnema server in Docker (Docker Engine with Compose is needed). Settings and data go
to ~/mnema-server (or MNEMA_SERVER_DIR); the server's commands are ./mnema.sh in this folder.

  --url URL        the address devices use to reach the server, such as a domain; remembered.
                   Without it, this machine's network address
  --build          build the image from this folder instead of pulling it
  --swap-size 4G   add swap, 1G to 64G
HELP
      ;;
      client) cat <<'HELP'
Usage: ./install.sh client [INVITATION]

Installs the Mnema app. INVITATION is the link the server printed, in quotes:
  ./install.sh client 'http://server:12345#invite=…'
It can also be pasted later in the app settings: "Server".
HELP
      ;;
      *) cat <<'HELP'
Usage:
  ./install.sh server    the Mnema server on Linux, in Docker
  ./install.sh client    the Mnema app on Linux (on a Mac the DMG is simpler)

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
SWAP_SIZE=""
URL=""
BUILD=""
CONNECTION=""
while [ $# -gt 0 ]; do
  case "$1" in
    --help|-h) usage "$MODE"; exit 0;;
    --swap-size)
      if [ $# -lt 2 ] || ! [[ "$2" =~ ^([1-9]|[1-5][0-9]|6[0-4])[Gg]$ ]]; then
        say "--swap-size: 1G to 64G, for example --swap-size 4G" \
          "--swap-size: от 1G до 64G, например --swap-size 4G"
        exit 1
      fi
      SWAP_SIZE="${2%[Gg]}"; shift 2;;
    --url)
      if [ "$MODE" != server ] || [ $# -lt 2 ]; then usage "$MODE"; exit 1; fi
      if ! [[ "$2" =~ ^https?://[^[:space:]#\$\"\'\\]+$ ]]; then
        say "--url: an address like http://203.0.113.5:38837" "--url: адрес вроде http://203.0.113.5:38837"
        exit 1
      fi
      URL="$2"; shift 2;;
    --build)
      if [ "$MODE" != server ]; then usage "$MODE"; exit 1; fi
      BUILD=1; shift;;
    -*) usage "$MODE"; exit 1;;
    *) [ -z "$CONNECTION" ] || { usage "$MODE"; exit 1; }; CONNECTION="$1"; shift;;
  esac
done
if [ "$MODE" != server ] && [ -n "$SWAP_SIZE" ]; then
  usage "$MODE"; exit 1
fi
if [ "$MODE" != client ] && [ -n "$CONNECTION" ]; then
  usage "$MODE"; exit 1
fi

as_root() {
  if [ "$(id -u)" = 0 ]; then "$@"; else sudo "$@"; fi
}

OS="$(uname -s)"

# The server: a container from compose.yaml in ~/mnema-server, data in ~/mnema-server/data.
# Nothing else on this machine: its commands are ./mnema.sh in this folder.
if [ "$MODE" = server ]; then
  if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
    say "The server runs in Docker. Install Docker Engine with the Compose plugin: https://docs.docker.com/engine/install/" \
      "Сервер работает в Docker. Установите Docker Engine с плагином Compose: https://docs.docker.com/engine/install/"
    exit 1
  fi
  if ! docker_error="$(docker info 2>&1 >/dev/null)"; then
    case "$docker_error" in
      *"permission denied"*)
        say "This user may not use Docker. Add it to the docker group, open a new shell and install again:" \
          "Этому пользователю нельзя пользоваться Docker. Добавьте его в группу docker, откройте новую оболочку и установите снова:"
        printf '  sudo usermod -aG docker %s\n  newgrp docker\n' "$(id -un)" ;;
      *)
        say "Docker isn't running. Start it and install again:" \
          "Docker не запущен. Запустите его и установите снова:"
        printf '  sudo systemctl enable --now docker\n' ;;
    esac
    exit 1
  fi
  # 0.3.0 ran the server as this systemd service; two servers would share nothing but the name
  if command -v systemctl >/dev/null && systemctl cat mnema.service >/dev/null 2>&1; then
    say "A server of an earlier Mnema runs here as the systemd service mnema. Stop it first: sudo systemctl disable --now mnema" \
      "Здесь работает сервер прежней Mnema, служба systemd mnema. Сначала остановите её: sudo systemctl disable --now mnema"
    exit 1
  fi
  if [ -n "$SWAP_SIZE" ]; then
    if [ "$OS" != Linux ]; then
      say "--swap-size is for Linux." "--swap-size только для Linux."
      exit 1
    fi
    as_root bash "$PWD/scripts/setup-swap.sh" "$SWAP_SIZE"
  fi

  DIR="$SERVER_DIR"
  NAME="${MNEMA_NAME:-mnema}"
  # the published image is pulled on every start (./mnema.sh update); one built here or given in
  # MNEMA_IMAGE is used as it is
  IMAGE="${MNEMA_IMAGE:-ghcr.io/fozt/mnema:latest}" PULL=never
  if [ -n "$BUILD" ]; then
    IMAGE=mnema:local
    docker build -t "$IMAGE" "$PWD"
  elif [ -z "${MNEMA_IMAGE:-}" ]; then
    PULL=always
    docker pull "$IMAGE"
  fi

  # The port is picked once; the image tells a free one and this computer's address, looked up
  # on the host's network by the same rule as ./mnema.sh invite (node.node_address). The detected
  # address goes to MNEMA_URL on every install; --url is remembered by ./mnema.sh invite --url and
  # wins over it (node.node_url).
  PORT="" FREE="" ADDRESS="" DETECTED=""
  if [ -f "$DIR/.env" ]; then PORT="$(sed -n 's/^MNEMA_PORT=//p' "$DIR/.env" | tail -n 1)"; fi
  read -r FREE ADDRESS < <(docker run --rm --network host "$IMAGE" python -m mnema.install_setup host) || true
  PORT="${PORT:-$FREE}"
  if [ -n "$ADDRESS" ]; then DETECTED="http://$ADDRESS:$PORT"; fi
  if [ -z "$PORT" ] || { [ -z "$URL" ] && [ -z "$DETECTED" ] && ! [ -f "$DIR/data/config/mnema/server.json" ]; }; then
    say "The server's network address wasn't found. Give it: ./install.sh server --url http://ADDRESS:${PORT:-PORT}" \
      "Адрес сервера в сети не найден. Укажите его: ./install.sh server --url http://АДРЕС:${PORT:-ПОРТ}"
    exit 1
  fi

  mkdir -p "$DIR/data"
  chmod 700 "$DIR" "$DIR/data"
  # the container runs as the owner of the data, never as root
  uid="$(id -u)"; gid="$(id -g)"
  if [ "$uid" = 0 ]; then
    uid=1000; gid=1000
    chown -R "$uid:$gid" "$DIR/data"
  fi
  cp "$PWD/compose.yaml" "$DIR/compose.yaml"
  # the host's time zone: invitations say until when they are valid in local time
  ZONE="$(readlink /etc/localtime 2>/dev/null | sed -n 's|.*/zoneinfo/||p')"
  printf '%s\n' "MNEMA_NAME=$NAME" "MNEMA_IMAGE=$IMAGE" "MNEMA_PULL=$PULL" "MNEMA_PORT=$PORT" \
    "MNEMA_URL=$DETECTED" "MNEMA_TZ=$ZONE" "MNEMA_UID=$uid" "MNEMA_GID=$gid" > "$DIR/.env"
  docker compose -f "$DIR/compose.yaml" up -d --wait
  # the command of 0.3.1, $DIR/mnema, and its link; never a file or link of someone else
  link="$HOME/.local/bin/mnema"
  if [ -L "$link" ] && [ "$(readlink "$link")" = "$DIR/mnema" ]; then rm -f "$link"; fi
  if [ -f "$DIR/mnema" ] && ! [ -L "$DIR/mnema" ] \
    && grep -q "^# The mnema command on the server's machine" "$DIR/mnema"; then
    rm -f "$DIR/mnema"
  fi

  ./mnema.sh model --show
  answer=n
  if [ -t 0 ] && [ -t 1 ]; then
    if [ -n "$RU" ]; then read -r -p "Выбрать модель для названия и пересказа сейчас? [Y/n] " answer
    else read -r -p "Choose the model for titles and summaries now? [Y/n] " answer; fi
  fi
  case "$answer" in
    [Nn]*) say "The model can be chosen later: ./mnema.sh model" "Модель можно выбрать позже: ./mnema.sh model";;
    *) ./mnema.sh model || true;;
  esac
  echo
  say "The server listens on port $PORT." "Сервер принимает подключения на порту $PORT."
  if [ -n "$URL" ]; then ./mnema.sh invite --url "$URL"; else ./mnema.sh invite; fi
  echo
  say "The server's commands, in $PWD:" "Команды сервера, в папке $PWD:"
  server_commands
  exit 0
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

# One locked runtime set without the dev tools; packages only, model weights download on the
# first job. `uv sync` is exact, so the app and a developer's plain `uv sync` cannot share this
# checkout's .venv: the app gets its own environment (the checkout stays its source, editable).
# Set for this one command, so an exported UV_PROJECT_ENVIRONMENT never redirects it and the
# app's processes never inherit it.
VENV="${XDG_DATA_HOME:-$HOME/.local/share}/mnema/app/.venv"
if [ "$OS" = Darwin ]; then role=mac; else role=linux; fi
# The extras of each role are listed once, in mnema/extras.py (the update reads them too)
role_extras="$(uv run --no-project --python 3.12 -- python -I "$PWD/mnema/extras.py" "$role")"
extras=()
for extra in $role_extras; do extras+=(--extra "$extra"); done
UV_PROJECT_ENVIRONMENT="$VENV" uv sync --locked --python 3.12 --no-dev "${extras[@]}"

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
