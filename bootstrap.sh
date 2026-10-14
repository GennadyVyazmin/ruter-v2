#!/usr/bin/env bash
set -Eeuo pipefail

# Stage 1: prepare Debian or Ubuntu. Stage 2: run the Ruter v2 installer.
# A downloader is needed only to fetch this script initially.

REPO=https://github.com/GennadyVyazmin/ruter-v2.git
TARGET_USER=
LAN_IF=

usage() {
  echo 'Usage: bash bootstrap.sh [--user LOGIN] [--interface LAN_IF]' >&2
  exit 2
}

while (($#)); do
  case "$1" in
    --user) [[ $# -ge 2 ]] || usage; TARGET_USER=$2; shift 2 ;;
    --interface) [[ $# -ge 2 ]] || usage; LAN_IF=$2; shift 2 ;;
    *) usage ;;
  esac
done

[[ $EUID -eq 0 ]] || { echo 'Запустите загрузчик от root (su или sudo).' >&2; exit 1; }
if [[ -z $TARGET_USER ]]; then
  TARGET_USER=${SUDO_USER:-}
  if [[ -z $TARGET_USER || $TARGET_USER == root ]]; then
    TARGET_USER=$(logname 2>/dev/null || true)
  fi
  if [[ -z $TARGET_USER || $TARGET_USER == root ]]; then
    read -r -p 'Имя пользователя для Ruter v2: ' TARGET_USER
  fi
fi
[[ -n $TARGET_USER && $TARGET_USER != root ]] || usage
[[ $TARGET_USER =~ ^[a-z_][a-z0-9_-]*[$]?$ ]] || { echo 'Некорректное имя пользователя.' >&2; exit 1; }
getent passwd "$TARGET_USER" >/dev/null || { echo 'Пользователь не найден.' >&2; exit 1; }
USER_HOME=$(getent passwd "$TARGET_USER" | cut -d: -f6)
[[ -d $USER_HOME && $USER_HOME != / ]] || { echo 'Некорректная домашняя папка.' >&2; exit 1; }
CHECKOUT="$USER_HOME/ruter-v2"
command -v apt-get >/dev/null || { echo 'Нужна Debian/Ubuntu с apt-get.' >&2; exit 1; }
[[ -r /etc/os-release ]] || { echo 'Не удалось определить Linux-дистрибутив.' >&2; exit 1; }
# shellcheck disable=SC1091
DISTRO_ID=$( . /etc/os-release; printf '%s' "$ID" )
[[ $DISTRO_ID == debian || $DISTRO_ID == ubuntu ]] || {
  echo 'Поддерживаются только Debian и Ubuntu.' >&2; exit 1;
}
[[ -d /run/systemd/system ]] || { echo 'Нужна система с работающей systemd.' >&2; exit 1; }
if command -v systemctl >/dev/null && systemctl is-active --quiet sing-box.service; then
  echo 'На этой машине уже работает sing-box.service. Production/старый шлюз не тронут.' >&2
  exit 1
fi
if command -v systemctl >/dev/null && systemctl is-active --quiet ruter-xhttp-lab.service; then
  echo 'Временный XHTTP-шлюз ещё работает. Остановите его штатным скриптом перед установкой.' >&2
  exit 1
fi

echo '=== ЭТАП 1/2: подготовка Debian/Ubuntu ==='
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y sudo git curl wget ca-certificates python3 iproute2 procps passwd
for cmd in sudo git curl wget python3 ip ss sysctl systemctl usermod; do
  command -v "$cmd" >/dev/null || { echo "Не установлена команда $cmd" >&2; exit 1; }
done
[[ -s /etc/ssl/certs/ca-certificates.crt ]] || { echo 'Сертификаты CA не установлены.' >&2; exit 1; }

if [[ -z $LAN_IF ]]; then
  mapfile -t DEFAULT_IFACES < <(ip -4 route show default | awk '{for (i=1; i<NF; i++) if ($i=="dev") print $(i+1)}' | sort -u)
  if ((${#DEFAULT_IFACES[@]} == 1)); then
    LAN_IF=${DEFAULT_IFACES[0]}
  else
    printf 'Не удалось однозначно выбрать LAN-интерфейс. Кандидаты: %s\n' "${DEFAULT_IFACES[*]:-нет}"
    read -r -p 'Имя LAN-интерфейса: ' LAN_IF
  fi
fi
[[ $LAN_IF =~ ^[a-zA-Z0-9_.:-]+$ ]] || { echo 'Некорректный интерфейс.' >&2; exit 1; }
ip link show dev "$LAN_IF" >/dev/null || { echo "Интерфейс $LAN_IF не найден." >&2; exit 1; }
echo "Установка для пользователя $TARGET_USER, интерфейс $LAN_IF."

# The named user is explicitly chosen by the operator for future sudo use.
if ! id -nG "$TARGET_USER" | tr ' ' '\n' | grep -qx sudo; then
  usermod -aG sudo "$TARGET_USER"
  echo "Пользователь $TARGET_USER добавлен в группу sudo; права появятся после нового входа."
fi

echo '=== ЭТАП 2/2: исходники и установка Ruter v2 ==='
if [[ -e $CHECKOUT || -L $CHECKOUT ]]; then
  [[ -d $CHECKOUT && ! -L $CHECKOUT ]] || { echo "Путь занят: $CHECKOUT" >&2; exit 1; }
  [[ $(git -C "$CHECKOUT" remote get-url origin 2>/dev/null || true) == "$REPO" ]] || {
    echo "Существующая папка не является репозиторием Ruter v2: $CHECKOUT" >&2; exit 1;
  }
  [[ -z $(git -C "$CHECKOUT" status --porcelain) ]] || {
    echo "В $CHECKOUT есть локальные изменения. Обновление остановлено без перезаписи." >&2; exit 1;
  }
  git -C "$CHECKOUT" pull --ff-only
else
  git clone --depth 1 "$REPO" "$CHECKOUT"
fi
chown -R "$TARGET_USER:$(id -gn "$TARGET_USER")" "$CHECKOUT"
cd "$CHECKOUT"
./ruter-v2 --interface "$LAN_IF" install

echo 'Ruter v2 установлен, но ещё не запущен.'
echo 'Главное меню: ruter. После проверки выберите пункт 3 для запуска шлюза.'
