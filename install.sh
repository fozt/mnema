#!/usr/bin/env bash
# Two explicit modes: the app (client) or the server in Docker.
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
  ./install.sh server [--url http://адрес:порт] [--build] [--swap-size 4G]

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

Сервер работает в Docker, нужен Docker Engine с Compose. Установщик кладёт compose.yaml и
настройки в ~/mnema-server, данные в ~/mnema-server/data, запускает контейнер из образа
ghcr.io/fozt/mnema:latest и ставит команду mnema: она выполняет mnema model, mnema invite и
mnema devices в контейнере, а mnema update скачивает новый образ и пересоздаёт контейнер.
Порт выбирается один раз и сохраняется для последующих установок.
--url: адрес, по которому сервер видят устройства, например домен или проброшенный порт. Без него
приглашение ведёт на адрес этого компьютера в сети: Tailscale, публичный, домашний.
--build: собрать образ из этой папки, а не скачивать.
--swap-size: создать swap от 1G до 64G (G = GiB), только на Linux.
Команда mnema — ссылка ~/.local/bin/mnema на эту установку.
HELP
  else
    cat <<'HELP'
Usage:
  ./install.sh client [INVITATION]
  ./install.sh server [--url http://address:port] [--build] [--swap-size 4G]

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

The server runs in Docker and needs Docker Engine with Compose. The installer puts compose.yaml
and its settings into ~/mnema-server, the data into ~/mnema-server/data, starts the container
from the image ghcr.io/fozt/mnema:latest and adds the mnema command: it runs mnema model,
mnema invite and mnema devices in the container, and mnema update pulls the new image and
recreates the container.
The port is picked once and kept for later installs.
--url: the address devices reach the server at, such as a domain or a forwarded port. Without it
the invitation leads to this computer's network address: Tailscale, public, home.
--build: build the image from this folder instead of pulling it.
--swap-size: create swap of 1G to 64G (G = GiB), Linux only.
The mnema command is the link ~/.local/bin/mnema to this installation.
HELP
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
    --help|-h) usage; exit 0;;
    --swap-size)
      if [ $# -lt 2 ] || ! [[ "$2" =~ ^([1-9]|[1-5][0-9]|6[0-4])[Gg]$ ]]; then
        say "--swap-size: 1G to 64G, for example --swap-size 4G" \
          "--swap-size: от 1G до 64G, например --swap-size 4G"
        exit 1
      fi
      SWAP_SIZE="${2%[Gg]}"; shift 2;;
    --url)
      if [ "$MODE" != server ] || [ $# -lt 2 ]; then usage; exit 1; fi
      if ! [[ "$2" =~ ^https?://[^[:space:]#\$\"\'\\]+$ ]]; then
        say "--url: an address like http://203.0.113.5:38837" "--url: адрес вроде http://203.0.113.5:38837"
        exit 1
      fi
      URL="$2"; shift 2;;
    --build)
      if [ "$MODE" != server ]; then usage; exit 1; fi
      BUILD=1; shift;;
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

# The server: a container from compose.yaml in ~/mnema-server, data in ~/mnema-server/data, and
# the host command ~/.local/bin/mnema (scripts/server-command.sh). Nothing else on this machine.
if [ "$MODE" = server ]; then
  if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
    say "The server runs in Docker. Install Docker Engine with the Compose plugin: https://docs.docker.com/engine/install/" \
      "Сервер работает в Docker. Установите Docker Engine с плагином Compose: https://docs.docker.com/engine/install/"
    exit 1
  fi
  if ! docker info >/dev/null 2>&1; then
    say "Docker isn't running, or this user may not use it: https://docs.docker.com/engine/install/linux-postinstall/" \
      "Docker не запущен, или этому пользователю он недоступен: https://docs.docker.com/engine/install/linux-postinstall/"
    exit 1
  fi
  if [ -n "$SWAP_SIZE" ]; then
    if [ "$OS" != Linux ]; then
      say "--swap-size is for Linux." "--swap-size только для Linux."
      exit 1
    fi
    as_root bash "$PWD/scripts/setup-swap.sh" "$SWAP_SIZE"
  fi

  DIR="$HOME/mnema-server"
  NAME="${MNEMA_NAME:-mnema}"
  # the published image is pulled on every start (mnema update); one built here or given in
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
  # on the host's network by the same rule as mnema invite (node.node_address).
  PORT="" FREE="" ADDRESS=""
  if [ -f "$DIR/.env" ]; then PORT="$(sed -n 's/^MNEMA_PORT=//p' "$DIR/.env" | tail -n 1)"; fi
  read -r FREE ADDRESS < <(docker run --rm --network host "$IMAGE" python -m mnema.install_setup host) || true
  PORT="${PORT:-$FREE}"
  if [ -z "$URL" ] && [ -n "$ADDRESS" ]; then URL="http://$ADDRESS:$PORT"; fi
  if [ -z "$URL" ] || [ -z "$PORT" ]; then
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
  printf '%s\n' "MNEMA_NAME=$NAME" "MNEMA_IMAGE=$IMAGE" "MNEMA_PULL=$PULL" "MNEMA_PORT=$PORT" \
    "MNEMA_URL=$URL" "MNEMA_UID=$uid" "MNEMA_GID=$gid" > "$DIR/.env"
  { sed -n 1p "$PWD/scripts/server-command.sh"; printf 'DIR=%q\n' "$DIR"; sed 1d "$PWD/scripts/server-command.sh"; } > "$DIR/mnema"
  chmod 755 "$DIR/mnema"
  docker compose -f "$DIR/compose.yaml" up -d --wait

  link="$HOME/.local/bin/mnema"
  mnema="$link"
  if [ -e "$link" ] && ! [ -L "$link" ]; then
    mnema="$DIR/mnema"  # someone else's file: left alone
    say "$link is taken by another file. Run Mnema as $mnema" "$link занят другим файлом. Запускайте Mnema так: $mnema"
  else
    mkdir -p "$HOME/.local/bin"
    ln -sfn "$DIR/mnema" "$link"
  fi

  "$mnema" model --show
  answer=n
  if [ -t 0 ] && [ -t 1 ]; then
    if [ -n "$RU" ]; then read -r -p "Выбрать модель для названия и пересказа сейчас? [Y/n] " answer
    else read -r -p "Choose the model for titles and summaries now? [Y/n] " answer; fi
  fi
  case "$answer" in
    [Nn]*) say "The model can be chosen later: mnema model" "Модель можно выбрать позже: mnema model";;
    *) "$mnema" model || true;;
  esac
  echo
  "$mnema" invite --url "$URL"
  say "Commands: mnema invite, mnema devices, mnema model, mnema update. Log: docker logs -f $NAME" \
    "Команды: mnema invite, mnema devices, mnema model, mnema update. Журнал: docker logs -f $NAME"
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
