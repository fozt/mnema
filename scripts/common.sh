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

# The commands of ./mnema.sh: those of the container $1 from its own parser, so the list cannot
# fall behind them; update and logs live here. A stopped container shows only these two.
server_commands() {
  docker exec ${RU:+-e MNEMA_UI_LANG=ru} "$1" mnema --server-help 2>/dev/null || true
  say "  ./mnema.sh update   the newest version of the server" \
    "  ./mnema.sh update   новая версия сервера"
  say "  ./mnema.sh logs     the server's log" \
    "  ./mnema.sh logs     журнал сервера"
}
