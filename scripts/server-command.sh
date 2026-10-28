#!/usr/bin/env bash
# The mnema command on the server's machine. ./install.sh server copies it to ~/mnema-server/mnema,
# sets DIR on its second line to that folder and links ~/.local/bin/mnema to it.
# `mnema update` pulls the newest image and recreates the container (compose.yaml: pull_policy);
# every other command runs as mnema in the container: mnema model, mnema invite, mnema devices.
set -euo pipefail
: "${DIR:?}"

compose() { docker compose -f "$DIR/compose.yaml" "$@"; }

if [ $# -eq 1 ] && [ "$1" = update ]; then
  image="$(compose config --images)"
  old="$(docker image inspect -f '{{.Id}}' "$image" 2>/dev/null || true)"
  compose up -d --wait
  if [ -n "$old" ] && [ "$old" != "$(docker image inspect -f '{{.Id}}' "$image")" ]; then
    docker image rm "$old" >/dev/null 2>&1 || true  # the previous image, unless still in use
  fi
  compose exec -T mnema mnema --version
elif [ -t 0 ] && [ -t 1 ]; then
  compose exec mnema mnema "$@"
else
  compose exec -T mnema mnema "$@"
fi
