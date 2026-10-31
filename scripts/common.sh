# Sourced by install.sh and mnema.sh: the language of their messages, the server's folder and
# its commands.

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

# The server in Docker: compose.yaml, its .env and the data folder data/
SERVER_DIR="${MNEMA_SERVER_DIR:-$HOME/mnema-server}"

# The commands of ./mnema.sh, one per line
server_commands() {
  if [ -n "$RU" ]; then cat <<'HELP'
  ./mnema.sh invite                 приглашение для одного устройства (--hours до 72)
  ./mnema.sh devices                подключённые устройства
  ./mnema.sh devices revoke ID      отключить устройство
  ./mnema.sh update                 новая версия сервера
  ./mnema.sh logs                   журнал сервера
HELP
  else cat <<'HELP'
  ./mnema.sh invite                 an invitation for one device (--hours up to 72)
  ./mnema.sh devices                connected devices
  ./mnema.sh devices revoke ID      disconnect a device
  ./mnema.sh update                 the newest version of the server
  ./mnema.sh logs                   the server's log
HELP
  fi
}
