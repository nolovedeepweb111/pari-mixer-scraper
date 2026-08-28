#!/usr/bin/env bash
# Обновляет прод до свежего master. После установки лежит на сервере как
# /usr/local/bin/pms-update, так что выкат - одно слово:
#
#   pms-update
#
# Почему через временный каталог, а не запуском /opt/pari-mixer-scraper/deploy/
# install.sh: тот скрипт делает своему каталогу git reset --hard, то есть
# переписал бы файл, из которого сам в этот момент читается. bash читает скрипт
# по мере выполнения, и подменять его под собой нельзя.
set -euo pipefail

REPO=${REPO:-https://github.com/nolovedeepweb111/pari-mixer-scraper.git}
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

echo "==> Забираю свежий код"
git clone --quiet --depth 1 "$REPO" "$TMP"
bash "$TMP/deploy/install.sh"
