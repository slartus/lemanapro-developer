#!/bin/zsh
# Выкатка в рабочую копию сервиса: fast-forward на ветку/коммит, зависимости, перезапуск LaunchAgent.
# Разработка — в отдельном worktree; в рабочей копии правки не делаются.
set -euo pipefail

TARGET="${1:?usage: deploy/update.sh <branch-or-commit>}"
PROJECT_DIR="${PROJECT_DIR:-$HOME/projects/lemanapro-developer}"
PLIST_LABEL="ru.slinkin.lemanapro-developer"
SERVICE="gui/$(id -u)/$PLIST_LABEL"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8123/healthz}"

service_pid() {
  launchctl print "$SERVICE" 2>/dev/null | awk '/^\tpid = / { print $3; exit }'
}

cd "$PROJECT_DIR"
launchctl print "$SERVICE" >/dev/null 2>&1 || {
  echo "LaunchAgent не загружен: launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/$PLIST_LABEL.plist"
  exit 1
}
[ "$(git symbolic-ref --quiet --short HEAD || true)" = "main" ] || { echo "Рабочая копия сервиса должна быть на ветке main"; exit 1; }
git rev-parse --verify --quiet "$TARGET^{commit}" >/dev/null || { echo "Нет такого коммита или ветки: $TARGET"; exit 1; }
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "В рабочей копии сервиса есть изменения — выкатка остановлена:"
  git status --short --untracked-files=no
  exit 1
fi

OLD_HEAD="$(git rev-parse HEAD)"
git merge --ff-only "$TARGET"
if ! "$PROJECT_DIR/.venv/bin/pip" install -q -e .; then
  git reset --keep "$OLD_HEAD"
  echo "pip install упал — рабочая копия возвращена на $OLD_HEAD, сервис не перезапускался"
  exit 1
fi

OLD_PID="$(service_pid)"
launchctl kickstart -k "$SERVICE"
for _ in {1..30}; do
  NEW_PID="$(service_pid)"
  if [ -n "$NEW_PID" ] && [ "$NEW_PID" != "$OLD_PID" ] && curl -fsS --max-time 2 "$HEALTH_URL" >/dev/null 2>&1; then
    echo "Выкачено: $(git log --oneline -1) (pid $NEW_PID)"
    exit 0
  fi
  sleep 1
done
echo "Сервис не перезапустился или не ответил на $HEALTH_URL за 30 с. Откат: git -C $PROJECT_DIR reset --keep $OLD_HEAD && launchctl kickstart -k $SERVICE"
exit 1
