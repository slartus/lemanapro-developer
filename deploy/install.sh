#!/bin/zsh
# Установка mr-watch на Mac: venv, конфиг, launchd.
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$HOME/projects/mr-watch}"
VENV_DIR="$PROJECT_DIR/.venv"
PLIST_LABEL="ru.slinkin.mr-watch"
PLIST_SRC="$PROJECT_DIR/deploy/ru.slinkin.mr-watch.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$PLIST_LABEL.plist"
CONFIG_DST="$PROJECT_DIR/config.yaml"

cd "$PROJECT_DIR"

[ -x "$HOME/.opencode/bin/opencode" ] || { echo "opencode не найден: ~/.opencode/bin/opencode"; exit 1; }

if [ ! -d "$VENV_DIR" ]; then
  /opt/homebrew/bin/python3.12 -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/pip" install -q -e .
"$VENV_DIR/bin/pip" install -q uvicorn

if [ ! -f "$CONFIG_DST" ]; then
  cp config.example.yaml "$CONFIG_DST"
  chmod 600 "$CONFIG_DST"
  echo "Создан $CONFIG_DST — заполни projects и проверь pat_command."
fi

if ! security find-generic-password -s mr-watch -a gitlab >/dev/null 2>&1; then
  echo "PAT не найден в Keychain. Добавь:"
  echo '  security add-generic-password -s mr-watch -a gitlab -w "<GITLAB_PAT>"'
fi

mkdir -p "$HOME/Library/Logs/mr-watch"

sed -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" -e "s|__HOME__|$HOME|g" "$PLIST_SRC" > "$PLIST_DST"
launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load "$PLIST_DST"
sleep 2
curl -fsS http://127.0.0.1:8123/healthz && echo && echo "mr-watch запущен."
