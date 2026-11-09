#!/bin/zsh
# Перенос данных mr-watch в lemanapro-developer: БД (консистентная копия через sqlite backup) и конфиг.
# Запускать при остановленном и выгруженном mr-watch, до deploy/install.sh. Источник открывается только на чтение.
set -euo pipefail
umask 077

SRC_DIR="${SRC_DIR:-$HOME/projects/mr-watch}"
PROJECT_DIR="${PROJECT_DIR:-$HOME/projects/lemanapro-developer}"
OLD_PLIST_LABEL="ru.slinkin.mr-watch"
OLD_PLIST="$HOME/Library/LaunchAgents/$OLD_PLIST_LABEL.plist"
SRC_DB="$SRC_DIR/data/mrwatch.db"
DST_DB="$PROJECT_DIR/data/lpdev.db"
SRC_CONFIG="$SRC_DIR/config.yaml"
DST_CONFIG="$PROJECT_DIR/config.yaml"

for dir in "$SRC_DIR" "$PROJECT_DIR"; do
  [[ "$dir" =~ '^[A-Za-z0-9/._-]+$' ]] || { echo "Недопустимые символы в пути: $dir"; exit 1; }
done

[ -f "$SRC_DB" ] || { echo "Нет БД: $SRC_DB"; exit 1; }
[ -f "$SRC_CONFIG" ] || { echo "Нет конфига: $SRC_CONFIG"; exit 1; }
[ ! -e "$DST_DB" ] || { echo "Уже существует: $DST_DB"; exit 1; }
[ ! -e "$DST_CONFIG" ] || { echo "Уже существует: $DST_CONFIG"; exit 1; }

if launchctl list "$OLD_PLIST_LABEL" >/dev/null 2>&1 || [ -e "$OLD_PLIST" ]; then
  echo "LaunchAgent mr-watch загружен или его plist на месте. Выгрузи и убери plist:"
  echo "  launchctl unload -w $OLD_PLIST && mv $OLD_PLIST ~/mr-watch.plist.disabled"
  exit 1
fi
if pgrep -f "mrwatch.main:get_app" >/dev/null; then
  echo "mr-watch запущен (pgrep -fl mrwatch.main). Останови его перед переносом."
  exit 1
fi

TMP_DB="$DST_DB.tmp"
TMP_CONFIG="$DST_CONFIG.tmp"
trap 'rm -f "$TMP_DB" "$TMP_CONFIG"' EXIT

mkdir -p "$PROJECT_DIR/data"
rm -f "$TMP_DB" "$TMP_CONFIG"
sqlite3 -readonly "$SRC_DB" ".backup '$TMP_DB'"
[ "$(sqlite3 "$TMP_DB" 'PRAGMA integrity_check;')" = "ok" ] || { echo "integrity_check не прошёл"; exit 1; }

sed -e "s#$SRC_DIR/data/mrwatch.db#$PROJECT_DIR/data/lpdev.db#" \
    -e "s#~/projects/mr-watch/data/mrwatch.db#~/projects/lemanapro-developer/data/lpdev.db#" \
    -e "s#~/projects/mr-watch/#~/projects/lemanapro-developer/#g" \
    -e "s#$SRC_DIR/#$PROJECT_DIR/#g" \
    "$SRC_CONFIG" > "$TMP_CONFIG"

mv "$TMP_DB" "$DST_DB"
mv "$TMP_CONFIG" "$DST_CONFIG"

echo "БД: $DST_DB"
echo "Конфиг: $DST_CONFIG"
echo "Бэкапы mr-watch (data/backups) не переносятся: первый бэкап снимется при старте."
grep -nE "mr-watch|mrwatch" "$DST_CONFIG" | grep -v -e "-s mr-watch" -e "mr-watch-reviewer" && echo "Проверь оставшиеся пути выше." || true
