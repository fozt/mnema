#!/usr/bin/env bash
# A swap file for a low-memory server: sudo scripts/setup-swap.sh 4G (no size: by RAM).
# Only its own swap file: an existing active or configured swap is never changed.
# setup_swap takes its paths as arguments, so tests run it on temporary files.
set -euo pipefail

# Russian for a Russian interface language (looked up as mnema.i18n.detect does).
_swap_ru=""
for _value in "${MNEMA_UI_LANG:-}" "${LC_ALL:-}" "${LC_MESSAGES:-}" "${LANG:-}"; do
  _value="${_value//[[:space:]]/}"
  if [ -n "$_value" ]; then
    case "$_value" in [Rr][Uu]*) _swap_ru=1;; esac
    break
  fi
done

# say ENGLISH RUSSIAN
say() { if [ -n "$_swap_ru" ]; then printf '%s\n' "$2"; else printf '%s\n' "$1"; fi; }

setup_swap() (
  size="${1:-auto}"
  swap_file="${2:-/swapfile}"
  fstab="${3:-/etc/fstab}"
  lock="${4:-/run/lock/mnema-swap.lock}"
  meminfo="${5:-/proc/meminfo}"
  if [ "$size" != auto ]; then
    size="${size%G}"
    [[ "$size" =~ ^([1-9]|[1-5][0-9]|6[0-4])$ ]] || {
      say "Swap size: 1G to 64G (G = GiB), for example 4G." \
        "Размер swap: от 1G до 64G (G = GiB), например 4G."
      exit 1
    }
  fi
  for cmd in swapon swapoff mkswap findmnt flock; do
    command -v "$cmd" >/dev/null || {
      say "Swap needs $cmd (the util-linux package)." "Для swap нужен $cmd (пакет util-linux)."
      exit 1
    }
  done
  exec 9>"$lock"
  flock -x 9
  active="$(swapon --show=NAME --noheadings)"
  if [ -n "$active" ]; then
    say "Swap is already active; leaving it as it is." "Swap уже активен; оставляю без изменений."
    exit 0
  fi
  if [ ! -f "$fstab" ] || [ -L "$fstab" ]; then
    say "Unusual $fstab; set up swap by hand or don't run this script." \
      "Необычный $fstab; настройте swap вручную или не запускайте этот скрипт."
    exit 1
  fi
  if awk '$1 !~ /^#/ && $3 == "swap" { found=1 } END { exit !found }' "$fstab"; then
    say "Swap is already configured in $fstab; not changing it. Check it with swapon --show." \
      "Swap уже настроен в $fstab; не меняю. Проверьте его активацию через swapon --show."
    exit 0
  fi
  if [ -e "$swap_file" ] || [ -L "$swap_file" ]; then
    say "$swap_file already exists; not overwriting it. Set up swap by hand or don't run this script." \
      "$swap_file уже существует; не перезаписываю. Настройте swap вручную или не запускайте этот скрипт."
    exit 1
  fi
  parent="$(dirname "$swap_file")"
  filesystem="$(findmnt -n -o FSTYPE -T "$parent")"
  case "$filesystem" in
    ext4|xfs) ;;
    *)
      say "A swap file is created automatically only on ext4/xfs, here: $filesystem. Set up swap by hand." \
        "Swap-файл автоматически создаётся только на ext4/xfs, здесь: $filesystem. Настройте swap вручную."
      exit 1;;
  esac
  if [ "$size" = auto ]; then
    ram_kib="$(awk '$1 == "MemTotal:" {print $2}' "$meminfo")"
    [[ "$ram_kib" =~ ^[0-9]+$ ]] || { say "Couldn't detect the RAM." "Не удалось определить RAM."; exit 1; }
    size=$(( (ram_kib + 1048575) / 1048576 ))
    if [ "$size" -lt 2 ]; then size=2; fi
    if [ "$size" -gt 4 ]; then size=4; fi
  fi
  available="$(df -Pk "$parent" | awk 'NR == 2 {print $4}')"
  [[ "$available" =~ ^[0-9]+$ ]] || {
    say "Couldn't detect the free space." "Не удалось определить свободное место."
    exit 1
  }
  # At least 2 GiB stay free after the file; too little space is an error, not a silent skip.
  if [ "$available" -lt "$(( (size + 2) * 1048576 ))" ]; then
    say "Swap of ${size} GiB needs 2 GiB more free space in reserve. Free up space or choose a smaller size." \
      "Для swap ${size} GiB нужно ещё 2 GiB свободного места в запасе. Освободите место или задайте меньший размер."
    exit 1
  fi

  created=0
  activated=0
  committed=0
  fstab_tmp=""
  cleanup() {
    status=$?
    trap - EXIT
    if [ -n "$fstab_tmp" ]; then rm -f -- "$fstab_tmp"; fi
    if [ "$created" = 1 ] && [ "$committed" = 0 ]; then
      # Once fstab is written atomically the install is done, even on a signal before committed=1.
      if ! awk -v path="$swap_file" '$1 == path && $3 == "swap" { found=1 } END { exit !found }' "$fstab"; then
        if [ "$activated" = 1 ]; then
          if ! current="$(swapon --show=NAME --noheadings)"; then
            say "Couldn't check $swap_file during the rollback; the file is kept." \
              "Не удалось проверить состояние $swap_file при откате; файл сохранён." >&2
            exit "$status"
          fi
          if printf '%s\n' "$current" | awk -v path="$swap_file" '$0 == path { found=1 } END { exit !found }'; then
            if ! swapoff "$swap_file"; then
              say "Couldn't turn off our own $swap_file during the rollback; the file is kept." \
                "Не удалось отключить собственный $swap_file при откате; файл сохранён." >&2
              exit "$status"
            fi
          fi
        fi
        rm -f -- "$swap_file"
      fi
    fi
    exit "$status"
  }
  trap cleanup EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  umask 077
  # noclobber keeps a file that appeared after the check.
  (set -C; : > "$swap_file")
  created=1
  chmod 600 "$swap_file"
  say "Creating ${size} GiB of swap: ${swap_file}…" "Создаю swap ${size} GiB: ${swap_file}…"
  dd if=/dev/zero of="$swap_file" bs=1M count="$((size * 1024))" status=none conv=fsync
  mkswap "$swap_file"
  fstab_tmp="$(mktemp "${fstab}.mnema.XXXXXX")"
  cp -p -- "$fstab" "$fstab_tmp"
  printf '\n%s none swap sw 0 0\n' "$swap_file" >> "$fstab_tmp"
  # Set first: a signal may arrive right after a successful swapon.
  activated=1
  swapon "$swap_file"
  mv -- "$fstab_tmp" "$fstab"
  committed=1
  say "Swap is on and saved in $fstab." "Swap включён и сохранён в $fstab."
)

if [[ "${BASH_SOURCE[0]}" = "$0" ]]; then
  if [ "${1:-}" = --help ] || [ "${1:-}" = -h ]; then
    say "Usage: sudo scripts/setup-swap.sh [4G]" "Использование: sudo scripts/setup-swap.sh [4G]"
    say "Size: 1G to 64G (G = GiB); without it 2G to 4G by RAM." \
      "Размер: от 1G до 64G (G = GiB); без аргумента — от 2G до 4G по RAM."
    exit 0
  fi
  if [ "$(id -u)" != 0 ]; then
    say "Run it with sudo: sudo scripts/setup-swap.sh 4G (G = GiB)." \
      "Запустите через sudo: sudo scripts/setup-swap.sh 4G (G = GiB)."
    exit 1
  fi
  setup_swap "${1:-auto}"
fi
