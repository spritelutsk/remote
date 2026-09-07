#!/usr/bin/env bash
# Постоянный агент RustDesk на Linux-машине, к которой подключаются.
#
# Заменяет ручной запуск rustdesk-1.4.9-x86_64.AppImage системной службой:
# после установки машина доступна с загрузки, без входа в сеанс и без окна
# RustDesk на экране.
#
# Запускать на ЦЕЛЕВОЙ машине (той, которой управляют), от root:
#     sudo ./setup-linux.sh --password 'ваш-постоянный-пароль'
#     sudo ./setup-linux.sh --password '...' --deb ./rustdesk-1.4.9-x86_64.deb
set -euo pipefail

SERVER='rd.spritelutsk.duckdns.org'
API='https://rd.spritelutsk.duckdns.org'
KEY='UsY54LZt2ZGCefjA5wwFaThLf9X21uYm477RQqBfu1k='
VERSION='1.4.9'

PASSWORD=''
DEB=''
while [ $# -gt 0 ]; do
    case "$1" in
        --password) PASSWORD="${2:?--password требует значение}"; shift 2 ;;
        --deb)      DEB="${2:?--deb требует путь}";               shift 2 ;;
        -h|--help)  sed -n '2,12p' "$0"; exit 0 ;;
        *) echo "неизвестный аргумент: $1" >&2; exit 2 ;;
    esac
done

[ "$(id -u)" = 0 ] || { echo "нужен root: sudo $0 ..." >&2; exit 1; }
[ -n "$PASSWORD" ] || { echo "не задан --password (постоянный пароль для входа)" >&2; exit 2; }

# --- пакет вместо AppImage --------------------------------------------------
# AppImage живёт в пользовательском сеансе и умирает вместе с ним. .deb ставит
# /usr/bin/rustdesk и юнит rustdesk.service — это и есть «без запущенного».
if ! command -v rustdesk >/dev/null 2>&1; then
    if [ -z "$DEB" ]; then
        arch="$(dpkg --print-architecture)"
        case "$arch" in
            amd64) file="rustdesk-${VERSION}-x86_64.deb" ;;
            arm64) file="rustdesk-${VERSION}-aarch64.deb" ;;
            *) echo "неизвестная архитектура '$arch' — скачайте .deb вручную и передайте --deb" >&2; exit 1 ;;
        esac
        DEB="/tmp/$file"
        echo "[1/5] качаю $file"
        curl -fL --retry 3 -o "$DEB" \
            "https://github.com/rustdesk/rustdesk/releases/download/${VERSION}/${file}"
    fi
    echo "[1/5] ставлю $DEB"
    apt-get install -y "$DEB"
else
    echo "[1/5] rustdesk уже установлен: $(command -v rustdesk)"
fi

# --- конфигурация сервера ---------------------------------------------------
# Тот самый разъезд конфигов, на котором мы уже обожглись: служба работает от
# root и читает /root/.config, а окно настроек правит конфиг пользователя.
# Пишем в оба, иначе служба уйдёт на публичные серверы RustDesk.
write_config() {
    local home="$1" owner="$2" dir="$1/.config/rustdesk"
    mkdir -p "$dir"
    cat > "$dir/RustDesk2.toml" <<TOML
rendezvous_server = '$SERVER'
nat_type = 0
serial = 0

[options]
custom-rendezvous-server = '$SERVER'
relay-server = '$SERVER'
api-server = '$API'
key = '$KEY'
TOML
    chown -R "$owner" "$dir"
    echo "      $dir/RustDesk2.toml"
}

echo "[2/5] останавливаю службу на время правки конфига"
systemctl stop rustdesk 2>/dev/null || true

echo "[3/5] пишу конфигурацию сервера"
write_config /root root
# Пользователь, который вызвал sudo, — чтобы окно RustDesk показывало то же самое.
if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
    user_home="$(getent passwd "$SUDO_USER" | cut -d: -f6)"
    [ -n "$user_home" ] && write_config "$user_home" "$SUDO_USER:$SUDO_USER"
fi

echo "[4/5] ставлю постоянный пароль и включаю службу"
systemctl enable rustdesk >/dev/null
systemctl start rustdesk
sleep 2
# --password пишет в конфиг того пользователя, от которого запущен, поэтому root.
rustdesk --password "$PASSWORD" >/dev/null 2>&1 || \
    echo "      ! не удалось задать пароль автоматически — задайте в окне RustDesk вручную" >&2
systemctl restart rustdesk

echo "[5/5] проверка"
sleep 3
systemctl is-active --quiet rustdesk && echo "      служба активна" || {
    echo "      служба НЕ запустилась:" >&2; systemctl status rustdesk --no-pager -l | tail -20 >&2; exit 1; }
id_now="$(rustdesk --get-id 2>/dev/null || true)"
echo "      ID этой машины: ${id_now:-неизвестен, посмотрите в окне RustDesk}"
echo
echo "Готово. Впишите этот ID в портале (форма «Моё устройство») и проверьте,"
echo "что на сервере появилась строка update_pk $id_now."
