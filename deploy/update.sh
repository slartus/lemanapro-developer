#!/bin/zsh
# Выкатка в рабочую копию сервиса: fast-forward на ветку/коммит, зависимости, перезапуск LaunchAgent.
# Разработка — в отдельном worktree; в рабочей копии правки не делаются.
set -euo pipefail

TARGET="${1:?usage: deploy/update.sh <branch-or-commit>}"
PROJECT_DIR="${PROJECT_DIR:-$HOME/projects/lemanapro-developer}"
PLIST_LABEL="ru.slinkin.lemanapro-developer"

cd "$PROJECT_DIR"
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "В рабочей копии сервиса есть изменения — выкатка остановлена:"
  git status --short --untracked-files=no
  exit 1
fi
git merge --ff-only "$TARGET"
"$PROJECT_DIR/.venv/bin/pip" install -q -e .
launchctl kickstart -k "gui/$(id -u)/$PLIST_LABEL"
for _ in {1..30}; do
  if curl -fsS http://127.0.0.1:8123/healthz >/dev/null 2>&1; then
    echo "Выкачено: $(git log --oneline -1)"
    exit 0
  fi
  sleep 1
done
echo "Сервис не ответил на /healthz за 30 с"
exit 1
