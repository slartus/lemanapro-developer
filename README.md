# mr-watch

Локальный сервис автоматического ревью merge request для self-hosted GitLab.
Следит за MR, где вы упомянуты, назначены ревьювером или являетесь автором,
и прогоняет их через [opencode](https://opencode.ai) с read-only агентом
ревью. Результаты — в локальной базе и веб-UI, ничего не пишется в GitLab.

## Как работает

- **Poller** опрашивает GitLab (todos + список MR по проектам), находит
  триггеры: упоминание в todo, назначение ревьювером, новый MR, push в MR
  (опционально, `re_review_on_push`).
- **Worker'ы** (по умолчанию 2) берут ревью из очереди в SQLite: клонируют
  репо на нужный MR (shallow), строят diff, запускают `opencode run` с
  агентом, указанным для проекта в конфиге.
- **Веб-UI** на `http://127.0.0.1:8123`: список MR, история ревью, страница
  ревью с полным текстом и кнопкой Re-review.

## Безопасность

- opencode запускается с флагом `--pure` и **без** `--auto`, read-only агент
  `mr-watch-reviewer` (глобальный, `~/.config/opencode/agent/`): edit/bash/
  webfetch запрещены, внешние каталоги запрещены, чтение `.env*`/секретов
  запрещено.
- Клон изолирован: без hooks, без LFS, `protocol.file.allow=never`, токен не
  попадает в `.git/config` (remote удаляется) и в окружение opencode.
- Веб-UI отвечает только на `127.0.0.1`/`localhost` (Host-check), retry
  требует заголовка `X-Requested-With` (защита от CSRF).
- GitLab PAT хранится в macOS Keychain, не в конфиге.

## Установка

```bash
# 1. PAT в Keychain
security add-generic-password -s mr-watch -a gitlab -w "<GITLAB_PAT>"
# PAT нужен со scope read_api + read_repository

# 2. Конфиг
cp config.example.yaml config.yaml
$EDITOR config.yaml

# 3. Установка и запуск (venv + launchd)
./deploy/install.sh
```

Проверка: `curl http://127.0.0.1:8123/healthz`, UI — http://127.0.0.1:8123

## Конфиг

См. `config.example.yaml`: URL GitLab, интервал опроса, список проектов с
маппингом `gitlab_project_id → agent`, параметры ревью (параллельность,
таймаут, глубина клона, TTL workspaces).

## Эксплуатация

```bash
launchctl load|unload ~/Library/LaunchAgents/ru.slinkin.mr-watch.plist
tail -f ~/Library/Logs/mr-watch/out.log   # err.log для stderr
curl -s http://127.0.0.1:8123/healthz     # статус и время последнего опроса
```

- БД: `data/mrwatch.db` (SQLite WAL), бэкап раз в сутки в `data/backups/`
  (хранится 7).
- Логи ревью: `logs/review-<id>.log`, общий лог: `logs/app.log`.
- Workspaces клонов: `workspaces/` — удаляются после ревью; протухшие (>TTL)
  чистятся при старте.

## Разработка

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'  # или dependency-groups
PYTHONPATH=src .venv/bin/pytest tests/ -q
```

История изменений и self-review: `docs/CHANGELOG.md`, архитектура: `PLAN.md`.
