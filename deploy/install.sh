#!/usr/bin/env bash
# Ставит приложение на чистый Ubuntu 22.04/24.04 или Debian 12. Запускать от
# root на свежем VPS:
#
#   bash install.sh
#
# Скрипт идемпотентный: повторный запуск ничего не ломает, а обновляет код и
# перезапускает сервис. Секреты он не трогает - их вписываете вы в
# /etc/pari-mixer/env после первого запуска.
set -euo pipefail

APP_USER=pari
APP_DIR=/opt/pari-mixer-scraper
DATA_DIR=/var/lib/pari-mixer
ENV_FILE=/etc/pari-mixer/env
REPO=${REPO:-https://github.com/nolovedeepweb111/pari-mixer-scraper.git}
HERE=$(cd "$(dirname "$0")" && pwd)

echo "==> Системные пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git nginx certbot python3-certbot-nginx

echo "==> Пользователь $APP_USER"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"

echo "==> Код в $APP_DIR"
# Каталог с кодом принадлежит root, а сервис его только читает: так пользователь
# pari не может подменить собственный код. Заодно git при повторном запуске не
# спотыкается о "dubious ownership" - первая версия скрипта отдавала каталог
# пользователю pari, и эта строка чинит владельца обратно.
[ -d "$APP_DIR" ] && chown -R root:root "$APP_DIR"
if [ -d "$APP_DIR/.git" ]; then
    git -C "$APP_DIR" fetch --quiet origin
    git -C "$APP_DIR" reset --hard --quiet origin/master
else
    git clone --quiet "$REPO" "$APP_DIR"
fi

echo "==> Виртуальное окружение и зависимости"
[ -d "$APP_DIR/.venv" ] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

echo "==> Каталог данных $DATA_DIR"
# База лежит ВНЕ каталога с кодом: git pull её не тронет, а сборщик пишет
# рядом с ней временные файлы сборки, поэтому нужна запись в сам каталог.
mkdir -p "$DATA_DIR"
chown -R "$APP_USER:$APP_USER" "$DATA_DIR"

echo "==> Переменные окружения $ENV_FILE"
mkdir -p "$(dirname "$ENV_FILE")"
if [ ! -f "$ENV_FILE" ]; then
    cp "$HERE/env.example" "$ENV_FILE"
    echo "    создан из шаблона - впишите в него ключи (см. deploy/README.md)"
fi
# Там лежат ACCESS_KEYS, AUTH_SECRET и ключ Steam.
chown root:"$APP_USER" "$ENV_FILE"
chmod 640 "$ENV_FILE"

echo "==> systemd"
cp "$HERE/pari-mixer.service" /etc/systemd/system/pari-mixer.service
systemctl daemon-reload
systemctl enable --now pari-mixer

echo "==> nginx"
DOMAIN=$(grep -E '^\s*APP_DOMAIN=' "$ENV_FILE" | cut -d= -f2- | tr -d '"' | xargs || true)
if [ -z "${DOMAIN:-}" ]; then
    echo "    APP_DOMAIN в $ENV_FILE не задан - конфиг nginx пока пропущен."
else
    sed "s/__DOMAIN__/$DOMAIN/g" "$HERE/nginx.conf" > /etc/nginx/sites-available/pari-mixer
    ln -sf /etc/nginx/sites-available/pari-mixer /etc/nginx/sites-enabled/pari-mixer
    rm -f /etc/nginx/sites-enabled/default
    nginx -t && systemctl reload nginx
    echo "    настроен на $DOMAIN"
fi

echo
echo "Готово. Дальше:"
echo "  1) впишите секреты:      nano $ENV_FILE"
echo "  2) перезапустите:        systemctl restart pari-mixer"
echo "  3) выпустите сертификат: certbot --nginx -d ${DOMAIN:-ваш.домен}"
echo "  4) посмотрите логи:      journalctl -u pari-mixer -f"
