#!/bin/zsh
# Перенос данных mr-watch в lemanapro-developer: БД (консистентная копия через sqlite backup) и конфиг.
# Запускать при остановленном mr-watch, до deploy/install.sh. Исходные данные не изменяются.
set -euo pipefail

SRC_DIR="${SRC_DIR:-$HOME/projects/mr-watch}"
PROJECT_DIR="${PROJECT_DIR:-$HOME/projects/lemanapro-developer}"
SRC_DB="$SRC_DIR/data/mrwatch.db"
DST_DB="$PROJECT_DIR/data/lpdev.db"
SRC_CONFIG="$SRC_DIR/config.yaml"
DST_CONFIG="$PROJECT_DIR/config.yaml"

[ -f "$SRC_DB" ] || { echo "Нет БД: $SRC_DB"; exit 1; }
[ -f "$SRC_CONFIG" ] || { echo "Нет конфига: $SRC_CONFIG"; exit 1; }
[ ! -e "$DST_DB" ] || { echo "Уже существует: $DST_DB"; exit 1; }
[ ! -e "$DST_CONFIG" ] || { echo "Уже существует: $DST_CONFIG"; exit 1; }

if pgrep -f "mrwatch.main:get_app" >/dev/null; then
  echo "mr-watch запущен (pgrep -fl mrwatch.main). Останови его перед переносом."
  exit 1
fi

mkdir -p "$PROJECT_DIR/data"
sqlite3 "$SRC_DB" ".backup '$DST_DB'"
[ "$(sqlite3 "$DST_DB" 'PRAGMA integrity_check;')" = "ok" ] || { echo "integrity_check не прошёл: $DST_DB"; exit 1; }

sed -e "s#$SRC_DIR/data/mrwatch.db#$PROJECT_DIR/data/lpdev.db#" \
    -e "s#~/projects/mr-watch/data/mrwatch.db#~/projects/lemanapro-developer/data/lpdev.db#" \
    -e "s#~/projects/mr-watch/#~/projects/lemanapro-developer/#g" \
    -e "s#$SRC_DIR/#$PROJECT_DIR/#g" \
    "$SRC_CONFIG" > "$DST_CONFIG"
chmod 600 "$DST_CONFIG"

echo "БД: $DST_DB"
echo "Конфиг: $DST_CONFIG"
grep -n "mr-watch" "$DST_CONFIG" | grep -v -e "-s mr-watch" -e "mr-watch-reviewer" && echo "Проверь оставшиеся пути выше." || true
