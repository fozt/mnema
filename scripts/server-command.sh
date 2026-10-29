#!/usr/bin/env bash
# The mnema command on the server's machine. ./install.sh server copies it to ~/mnema-server/mnema,
# sets DIR on its second line to that folder and links ~/.local/bin/mnema to it.
# `mnema update` pulls the newest image and recreates the container (compose.yaml: pull_policy);
# a new image that does not become healthy is replaced by the previous one. Every other command
# runs as mnema in the container: mnema model, mnema invite, mnema devices.
set -euo pipefail
: "${DIR:?}"

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

compose() { docker compose -f "$DIR/compose.yaml" "$@"; }

if [ $# -eq 1 ] && [ "$1" = update ]; then
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
  compose exec -T mnema mnema --version
elif [ -t 0 ] && [ -t 1 ]; then
  compose exec mnema mnema "$@"
else
  compose exec -T mnema mnema "$@"
fi
