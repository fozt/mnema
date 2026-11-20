#!/usr/bin/env bash
# The server's commands on its machine, in the server's folder (~/mnema-server):
#   ./mnema.sh users [add NAME | remove NAME] | invite [--user NAME] | devices [revoke ID] | update | logs
# The installer puts this file next to compose.yaml, its .env and data/; run from a clone of the
# repository it finds the server in ~/mnema-server (or MNEMA_SERVER_DIR). update pulls the newest
# image and always recreates the container (compose.yaml: pull_policy), so new lines of the
# server's .env apply; a new image that does not become healthy is replaced by the previous one.
# After it this file and compose.yaml are taken from the running image: the commands stay at the
# server's version. logs follows the container's log. Every other command runs as mnema in the
# container.
set -euo pipefail

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

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$here/compose.yaml" ] && [ -f "$here/.env" ]; then
  DIR="$here"
else
  DIR="${MNEMA_SERVER_DIR:-$HOME/mnema-server}"
fi
NAME="$(sed -n 's/^MNEMA_NAME=//p' "$DIR/.env" 2>/dev/null | tail -n 1 || true)"
NAME="${NAME:-mnema}"

# The commands: those of the container from its own parser, so the list cannot fall behind them;
# update and logs live here. A stopped container shows only these two.
commands() {
  docker exec ${RU:+-e MNEMA_UI_LANG=ru} "$NAME" mnema --server-help 2>/dev/null || true
  say "  ./mnema.sh update   the newest version of the server" \
    "  ./mnema.sh update   новая версия сервера"
  say "  ./mnema.sh logs     the server's log" \
    "  ./mnema.sh logs     журнал сервера"
}

usage() {
  say "Usage: ./mnema.sh COMMAND, in the server's folder $DIR" \
    "Использование: ./mnema.sh КОМАНДА, в папке сервера $DIR"
  echo
  commands
}

case "${1:-}" in
  "") usage; exit 1;;
  --help|-h) usage; exit 0;;
  --commands) commands; exit 0;;  # for the installer's last lines
esac
if ! [ -f "$DIR/compose.yaml" ] || ! [ -f "$DIR/.env" ]; then
  say "The Mnema server isn't installed in $DIR. Install it:" \
    "Сервер Mnema не установлен в $DIR. Установите его:"
  printf '  curl -fsSL https://github.com/fozt/mnema/releases/latest/download/install-server.sh | bash\n'
  exit 1
fi

compose() { docker compose -f "$DIR/compose.yaml" "$@"; }

# This file and compose.yaml from the image the server now runs; a changed compose.yaml applies
# at once. An image without them (built before they were in it) leaves the files as they are.
refresh() {
  local file changed=""
  for file in compose.yaml mnema.sh; do
    if docker run --rm --entrypoint cat "$1" "/app/host/$file" > "$DIR/.$file.new" 2>/dev/null \
      && [ -s "$DIR/.$file.new" ]; then
      if [ "$file" = compose.yaml ] && [ "$(cat "$DIR/.$file.new")" != "$(cat "$DIR/$file")" ]; then
        changed=1
      fi
      mv "$DIR/.$file.new" "$DIR/$file"  # a new file: this running script keeps reading the old one
    else
      rm -f "$DIR/.$file.new"
    fi
  done
  chmod 755 "$DIR/mnema.sh"
  if [ -n "$changed" ]; then compose up -d --wait; fi
}

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
  if ! compose up -d --wait --force-recreate; then
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
  refresh "$image"
  docker exec "$NAME" mnema --version
elif [ -t 0 ] && [ -t 1 ]; then
  exec docker exec -it "$NAME" mnema "$@"
else  # stdin still reaches the command: piped input reaches the command
  exec docker exec -i "$NAME" mnema "$@"
fi
