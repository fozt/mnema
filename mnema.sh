#!/usr/bin/env bash
# The server's commands on its machine, run in this folder (the one with install.sh):
#   ./mnema.sh invite | devices [revoke ID] | update | logs
# The server is the container that ./install.sh server set up in ~/mnema-server (or
# MNEMA_SERVER_DIR). update pulls the newest image and recreates the container (compose.yaml:
# pull_policy); a new image that does not become healthy is replaced by the previous one. logs
# follows the container's log. Every other command runs as mnema in the container.
set -euo pipefail
# shellcheck source=scripts/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/scripts/common.sh"
DIR="$SERVER_DIR"

usage() {
  say "Usage: ./mnema.sh COMMAND, in the folder of install.sh" \
    "Использование: ./mnema.sh КОМАНДА, в папке install.sh"
  echo
  server_commands
}

case "${1:-}" in
  "") usage; exit 1;;
  --help|-h) usage; exit 0;;
esac
if ! [ -f "$DIR/compose.yaml" ] || ! [ -f "$DIR/.env" ]; then
  say "The Mnema server isn't installed in $DIR. Install it: ./install.sh server" \
    "Сервер Mnema не установлен в $DIR. Установите его: ./install.sh server"
  exit 1
fi
NAME="$(sed -n 's/^MNEMA_NAME=//p' "$DIR/.env" | tail -n 1)"
NAME="${NAME:-mnema}"

compose() { docker compose -f "$DIR/compose.yaml" "$@"; }

if [ "$1" = update ] || [ "$1" = logs ]; then
  if [ $# -ne 1 ]; then usage; exit 1; fi
fi

if [ "$1" = logs ]; then
  exec docker logs -f "$NAME"
elif [ "$1" = update ]; then
  image="$(compose config --images)"
  if grep -qx 'MNEMA_PULL=never' "$DIR/.env"; then
    say "The image $image is built here or given at install and is not pulled: the server restarts with it as it is." \
      "Образ $image собран здесь или указан при установке и не скачивается: сервер перезапускается с ним как есть."
  fi
  old="$(docker image inspect -f '{{.Id}}' "$image" 2>/dev/null || true)"
  if ! compose up -d --wait; then
    new="$(docker image inspect -f '{{.Id}}' "$image" 2>/dev/null || true)"
    if [ -n "$old" ] && [ "$old" != "$new" ]; then
      say "The new version didn't start. Going back to the previous one." \
        "Новая версия не запустилась. Возвращаю прежнюю."
      docker tag "$old" "$image"
      compose up -d --wait --pull never
    fi
    exit 1
  fi
  if [ -n "$old" ] && [ "$old" != "$(docker image inspect -f '{{.Id}}' "$image")" ]; then
    docker image rm "$old" >/dev/null 2>&1 || true  # the previous image, unless still in use
  fi
  docker exec "$NAME" mnema --version
elif [ -t 0 ] && [ -t 1 ]; then
  exec docker exec -it "$NAME" mnema "$@"
else  # stdin still reaches the command: piped input reaches the command
  exec docker exec -i "$NAME" mnema "$@"
fi
