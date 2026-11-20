#!/usr/bin/env bash
# The Mnema server in Docker, with nothing of the source on the machine:
#   curl -fsSL https://github.com/fozt/mnema/releases/latest/download/install-server.sh | bash
# Everything goes to ~/mnema-server (or MNEMA_SERVER_DIR): compose.yaml, its .env, the data and
# ./mnema.sh, the server's commands. Its own files come from the image, so ./mnema.sh update keeps
# them at the server's version. In a clone (./install.sh server) they come from the clone, which
# can also build the image (--build).
set -euo pipefail

# All of it in main, called on the last line: piped from curl, bash reads the whole script before
# running anything, so no command can swallow the rest of it from the same stdin.
main() {
INSTALLER_URL="https://github.com/fozt/mnema/releases/latest/download/install-server.sh"

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

# A clone next to this script gives the server's files and can build the image; piped from curl
# there is no script file and no clone.
CHECKOUT=""
source_file="${BASH_SOURCE[0]:-}"
if [ -n "$source_file" ] && [ -f "$source_file" ]; then
  here="$(cd "$(dirname "$source_file")" && pwd)"
  if [ -f "$here/Dockerfile" ] && [ -f "$here/compose.yaml" ] && [ -f "$here/mnema.sh" ]; then
    CHECKOUT="$here"
  fi
fi
# how to run this again, in the hints: ./install.sh sets its own
AGAIN="${MNEMA_INSTALL_AGAIN:-curl -fsSL $INSTALLER_URL | bash -s --}"

usage() {
  if [ -n "$RU" ]; then
    cat <<HELP
Использование: $AGAIN [--url http://адрес:порт] [--swap-size 4G]

Ставит сервер Mnema в Docker (нужен Docker Engine с Compose). Всё — в ~/mnema-server (или
MNEMA_SERVER_DIR): настройки, данные и команды сервера ./mnema.sh.

  --url АДРЕС      адрес, по которому сервер видят устройства, например домен; запоминается.
                   Без него — адрес этого компьютера в сети
  --swap-size 4G   добавить swap, от 1G до 64G; 0 — не предлагать
  --build          собрать образ из клона репозитория (./install.sh server --build)
HELP
  else
    cat <<HELP
Usage: $AGAIN [--url http://address:port] [--swap-size 4G]

Installs the Mnema server in Docker (Docker Engine with Compose is needed). Everything goes to
~/mnema-server (or MNEMA_SERVER_DIR): settings, data and the server's commands, ./mnema.sh.

  --url URL        the address devices use to reach the server, such as a domain; remembered.
                   Without it, this machine's network address
  --swap-size 4G   add swap, 1G to 64G; 0 to not offer it
  --build          build the image from a clone of the repository (./install.sh server --build)
HELP
  fi
}

SWAP_SIZE="" URL="" BUILD=""
while [ $# -gt 0 ]; do
  case "$1" in
    --help|-h) usage; exit 0;;
    --swap-size)
      if [ $# -lt 2 ] || ! [[ "$2" =~ ^(0|([1-9]|[1-5][0-9]|6[0-4])[Gg])$ ]]; then
        say "--swap-size: 1G to 64G, for example --swap-size 4G, or 0 for no swap" \
          "--swap-size: от 1G до 64G, например --swap-size 4G, или 0, если swap не нужен"
        exit 1
      fi
      SWAP_SIZE="${2%[Gg]}"; shift 2;;
    --url)
      if [ $# -lt 2 ]; then usage; exit 1; fi
      if ! [[ "$2" =~ ^https?://[^[:space:]#\$\"\'\\]+$ ]]; then
        say "--url: an address like http://203.0.113.5:38837" "--url: адрес вроде http://203.0.113.5:38837"
        exit 1
      fi
      URL="$2"; shift 2;;
    --build) BUILD=1; shift;;
    *) usage; exit 1;;
  esac
done
if [ -n "$BUILD" ] && [ -z "$CHECKOUT" ]; then
  say "--build builds the image from a clone of the repository: ./install.sh server --build there." \
    "--build собирает образ из клона репозитория: запустите там ./install.sh server --build."
  exit 1
fi

as_root() {
  if [ "$(id -u)" = 0 ]; then "$@"; else sudo "$@"; fi
}

OS="$(uname -s)"
if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  say "The server runs in Docker. Install Docker Engine with the Compose plugin: https://docs.docker.com/engine/install/" \
    "Сервер работает в Docker. Установите Docker Engine с плагином Compose: https://docs.docker.com/engine/install/"
  exit 1
fi
if ! docker_error="$(docker info 2>&1 >/dev/null)"; then
  case "$docker_error" in
    *"permission denied"*)
      say "This user has no access to Docker. Add it to the docker group, open a new shell and install again:" \
        "У этого пользователя нет доступа к Docker. Добавьте его в группу docker, откройте новую оболочку и установите снова:"
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
if [ -n "$SWAP_SIZE" ] && [ "$OS" != Linux ]; then
  say "--swap-size is for Linux." "--swap-size только для Linux."
  exit 1
fi

DIR="${MNEMA_SERVER_DIR:-$HOME/mnema-server}"
NAME="${MNEMA_NAME:-mnema}"
# the published image is pulled on every start (./mnema.sh update); one built here or given in
# MNEMA_IMAGE is used as it is
IMAGE="${MNEMA_IMAGE:-ghcr.io/fozt/mnema:latest}" PULL=never
if [ -n "$BUILD" ]; then
  IMAGE=mnema:local
  docker build -t "$IMAGE" "$CHECKOUT"
elif [ -z "${MNEMA_IMAGE:-}" ]; then
  PULL=always
  docker pull "$IMAGE"
fi

# The server's own files: from the clone, otherwise from the image (/app/host)
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
host_file() {  # NAME PATH_IN_THE_CLONE
  if [ -n "$CHECKOUT" ]; then
    cp "$CHECKOUT/$2" "$WORK/$1"
  else
    docker run --rm --entrypoint cat "$IMAGE" "/app/host/$1" > "$WORK/$1"
  fi
}
host_file compose.yaml compose.yaml
host_file mnema.sh mnema.sh

# No swap (/proc/swaps lists none): ask at a terminal, otherwise tell how to add it.
# --swap-size 0 leaves it alone. Swap only offered never stops the install; asked for, it does.
SWAPS="${MNEMA_PROC_SWAPS:-/proc/swaps}" OFFERED=""
if [ -z "$SWAP_SIZE" ] && [ "$OS" = Linux ] && [ -r "$SWAPS" ] \
  && [ -z "$(awk 'NR > 1' "$SWAPS")" ]; then
  if [ -t 0 ]; then
    answer=y
    if [ -n "$RU" ]; then read -r -p "Подкачки нет. Добавить 4 ГБ? [Y/n] " answer || answer=n
    else read -r -p "No swap. Add 4 GB? [Y/n] " answer || answer=n; fi
    case "$answer" in [Nn]*) ;; *) SWAP_SIZE=4 OFFERED=1;; esac
  else
    say "No swap. Add it with: $AGAIN --swap-size 4G" \
      "Подкачки нет. Добавьте её: $AGAIN --swap-size 4G"
  fi
fi
if [ -n "$SWAP_SIZE" ] && [ "$SWAP_SIZE" != 0 ]; then
  host_file setup-swap.sh scripts/setup-swap.sh
  if [ -n "$OFFERED" ]; then
    as_root bash "$WORK/setup-swap.sh" "$SWAP_SIZE" \
      || say "Swap wasn't added, installing without it. Try later: $AGAIN --swap-size 4G" \
        "Подкачка не добавлена, установка идёт без неё. Попробуйте позже: $AGAIN --swap-size 4G"
  else
    as_root bash "$WORK/setup-swap.sh" "$SWAP_SIZE"
  fi
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
  say "The server's network address wasn't found. Give it: $AGAIN --url http://ADDRESS:${PORT:-PORT}" \
    "Адрес сервера в сети не найден. Укажите его: $AGAIN --url http://АДРЕС:${PORT:-ПОРТ}"
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
cp "$WORK/compose.yaml" "$DIR/compose.yaml"
cp "$WORK/mnema.sh" "$DIR/mnema.sh"
chmod 755 "$DIR/mnema.sh"
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

echo
say "The server listens on port $PORT." "Сервер принимает подключения на порту $PORT."
if [ -n "$URL" ]; then "$DIR/mnema.sh" invite --url "$URL" < /dev/null
else "$DIR/mnema.sh" invite < /dev/null; fi
echo
say "The model is set in the Mnema app on a Mac: Settings → Recording → Choose model…" \
  "Модель задаётся в приложении Mnema на Mac: Настройки → Запись → Выбрать модель…"
echo
say "The server's commands, in $DIR:" "Команды сервера, в папке $DIR:"
"$DIR/mnema.sh" --commands
}

main "$@"
