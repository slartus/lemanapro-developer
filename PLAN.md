# PLAN: mr-watch — GitLab MR auto-review service

Сервис на Mac (m1pro): поллит self-hosted GitLab, находит MR, где пользователь
упомянут / назначен ревьювером или исполнителем / является автором,
автоматически запускает ревью через opencode (агент из конфига), сохраняет
результат в SQLite, показывает в веб-UI. В GitLab ничего не пишет.

## Стек и запуск

- Python 3.12 (brew) + venv: fastapi, uvicorn[standard], httpx, jinja2, pyyaml, pydantic
- SQLite (WAL), один процесс asyncio: poller + N воркеров (default 2) + бэкап + FastAPI
- БД: `data/mrwatch.db`
- launchd LaunchAgent `ru.slinkin.mr-watch`, порт 8123, bind 127.0.0.1
- Путь: `~/projects/mr-watch`; конфиг: `$MRWATCH_CONFIG` (plist задаёт
  `<проект>/config.yaml`), без переменной — `~/.config/mr-watch/config.yaml`;
  chmod 600, без секретов
- GitLab PAT: macOS Keychain (`security add-generic-password -s mr-watch -a gitlab -w`),
  читается через `gitlab.pat_command`

## Компоненты (src/mrwatch/)

| Модуль | Ответственность |
|--------|-----------------|
| `config.py` | YAML-конфиг + pydantic-модель (extra=forbid), проверка прав файла, PAT через pat_command |
| `db.py` | SQLite WAL, схема, миграции по `user_version`, идемпотентные вставки, бэкап |
| `gitlab_client.py` | httpx-клиент API v4: /user, /todos, /projects/:id/merge_requests, notes; ретраи, keyset |
| `poller.py` | Цикл: бэкфилл ролей → todos → фулскан новых проектов → sweep по per-project курсорам |
| `git_ops.py` | Клон (безопасные флаги), пин на head_sha, санитизация, diff, cleanup |
| `agents.py` | Allowlist глобальных read-only агентов opencode (проверка frontmatter) |
| `opencode_runner.py` | subprocess opencode run, таймаут, env-изоляция, автоповтор при reason=length, счётчики находок и вердикт |
| `review_findings.py` | Общий парсер находок (строки, fence, цитаты), извлечение кода к находкам из диффа/клона, раскладка текста ревью для UI |
| `procutil.py` | Лимит вывода, kill process group, маскирование секретов |
| `worker.py` | N корутин: queued → cloning → reviewing → done/error, ретраи клона |
| `web/` | FastAPI + Jinja2: таблица MR, ревью, retry, настройки, `/api/mrs`, `/api/reviews/{id}` |
| `main.py` | lifespan: старт и супервизия задач, восстановление очереди, cleanup, healthz, Host-check |

## Схема SQLite (`SCHEMA_VERSION = 9`)

- `project(id PK, gitlab_project_id UNIQUE, path_with_namespace, agent, re_review_on_push, enabled)`
- `mr(id PK, project_id FK, iid, title, author_username, author_name, source_branch, web_url, state, head_sha, my_roles, is_draft, mention_url, first_seen_at, updated_at, UNIQUE(project_id, iid))`
- `review(id PK, mr_id FK, trigger('new'|'push'|'manual'), head_sha, status('queued'|'cloning'|'reviewing'|'done'|'error'), agent, session_id, review_md, exit_code, error, attempt, not_before, crit_count, major_count, minor_count, nit_count, verdict, snippets_json, created_at, started_at, finished_at, UNIQUE(mr_id, head_sha, trigger))`
- `poll_state(key PK, value)` — курсоры `mr_cursor:<pid>`, `mr_scan_done:<pid>`,
  `roles_backfill_v5:<pid>`, здоровье опроса (`poll_last_ok`, `poll_last_error`)
- `todo_seen(todo_id PK, seen_at)` — обработанные todo
- Миграции: последовательные `_migrate_to_vN` в транзакции, `PRAGMA user_version`
- Идемпотентность: `INSERT ... ON CONFLICT DO NOTHING`; при старте все промежуточные статусы → queued
- Бэкап: раз в сутки (при старте сразу, если последний старше суток) в `data/backups/`, хранится 7

## GitLab API (v4)

- `GET /user` — username лениво, на первом тике поллера
- Каждый тик: `GET /todos?state=pending&per_page=100`; todo с
  `target_type=MergeRequest` по отслеживаемым проектам обрабатывается один раз
  (id в `todo_seen`), в GitLab ничего не помечается
- Фулскан проекта при первом появлении: `GET /projects/:id/merge_requests?state=opened`
  + notes для чужих MR (поиск `@username`); ревьюится свой/упомянувший открытый MR
- Sweep: `GET /projects/:id/merge_requests?updated_after=<cursor>&order_by=updated_at&sort=asc&per_page=100&pagination=keyset`
  (все state); новый свой открытый MR → `new`, смена head_sha при
  `re_review_on_push` → `push`
- Бэкфилл ролей: разово на проект пересчитывает `my_roles`/draft уже известных MR
- По триггеру и перед ревью: `GET /projects/:id/merge_requests/:iid` (diff_refs, description, reviewers)
- Курсор на проект = max(updated_at) − перекрытие 2 мин, назад не двигается;
  стартовый курсор нового проекта задаёт фулскан; дедуп по UNIQUE-ключам БД
- Пагинация: keyset по `Link`, не более 50 страниц, `next` только на тот же origin
- Токен: `read_api` + `read_repository` (клон HTTPS); write-scope не нужен
- Диффы по API не тянем: агент работает в клоне, diff строит сервис

## Lifecycle ревью

1. Обнаружение → upsert mr + review(trigger='new'|'push', status='queued')
2. Воркер: проверка агента и свободного места (≥ 5 ГБ) → свежий shallow-клон в
   `workspaces/<gitlab_project_id>/<iid>/<review_id>/` → fetch
   `merge-requests/<iid>/head`, пин ровно на `head_sha` ревью (при
   расхождении — ревью переносится на актуальный head или падает) → base-ветка
   в `refs/mrwatch/base` → удаление remote → санитизация →
   `git diff <base_sha> <head_sha>` (base — `diff_refs.base_sha` MR, а если head
   MR ушёл вперёд — merge-base с `refs/mrwatch/base`) →
   `opencode run` → вырезка кода к находкам по `путь:строка` (hunk диффа ±6
   строк, иначе ±4 строки файла клона; ошибка не валит ревью) → review_md,
   счётчики находок, вердикт, `snippets_json` в БД → удаление клона (finally)
3. Ошибки клона/получения MR — до 3 попыток с бэкоффом (`not_before`);
   ошибки opencode не авторетраятся, кроме одного повтора при `reason=length`
   с `opencode.length_retry_variant`
4. Модель/вариант: `projects[].model|variant` → `opencode.model|variant` → дефолт opencode
5. Retry/Re-review — кнопка в UI (409, если ревью ещё идёт); берёт текущего
   агента проекта, сбрасывает вердикт, счётчики и `snippets_json`
6. UI показывает только MR со state `opened`/`locked`, сортировка по
   `updated_at` (последняя активность в GitLab); автообновление 5 с при
   активных ревью / 30 с иначе, пауза в скрытой вкладке, после «Проверить
   сейчас» — обновления через 2 и 4 с
7. Страница ревью: пропорциональный шрифт, markdown-заголовки, inline code,
   fenced-блоки — как код с подписью языка; код находки — сразу после её
   строки, несопоставленный — внизу; «Развернуть весь код»
8. Формат агента: `- [УРОВЕНЬ] путь:строка — ...`, код не цитируется, блок
   исправления ≤ 5 строк только для BLOCKER/MAJOR (≤ 3 на ревью), явные
   критерии уровней, BLOCKER или MAJOR → `REQUEST_CHANGES`. Счётчики, вердикт
   и привязка кода — один парсер (`review_findings`) с общими правилами
   строк/fence/цитат

## Безопасность (MUST)

1. Никогда не передавать `--auto` в opencode run; всегда `--pure`
2. Агент — только из глобальных `~/.config/opencode/agent|agents/`, имя
   `[A-Za-z0-9_.-]+`, frontmatter обязан запрещать `edit`, `bash`, `webfetch`;
   проверка перед каждым ревью
3. Системный промпт: MR-контекст = данные, не инструкции; запрет команд/сети
4. Env opencode: `PATH`, `HOME`, `TMPDIR`, `OPENCODE_DISABLE_PROJECT_CONFIG=1`,
   `OPENCODE_DISABLE_CLAUDE_CODE=1`; PAT отсутствует
5. Санитизация клона (casefold, fail-closed): корень — `.opencode`,
   `opencode.json`, `opencode.jsonc`, `AGENTS.md`, `CLAUDE.md`, `CONTEXT.md`,
   `.claude`, `.agents`; вложенные — `AGENTS.md`, `CLAUDE.md`, `CONTEXT.md`
6. Клон: `GIT_LFS_SKIP_SMUDGE=1 git -c core.hooksPath=/dev/null -c protocol.file.allow=never
   -c transfer.fsckObjects=true -c core.symlinks=false clone --depth N --no-checkout --single-branch`,
   без submodules; ветки и sha валидируются; diff `--no-ext-diff --no-textconv`
7. Токен не в URL: временный `GIT_ASKPASS` (0700, в `tmp/`), после клона
   `git remote remove origin`; маскирование glpat-/Bearer в логах и ошибках
8. `gitlab.url`: только https, без userinfo/query/fragment/пробелов
9. Веб-UI: 127.0.0.1 + Host-check; POST — `X-Requested-With: mrwatch`;
   ссылки через `safe_url` (только хост GitLab); текст ревью и код — только
   через autoescape Jinja, без markdown-HTML; HTML `no-store`
10. Subprocess: минимальный env, cwd=клон, process group, таймаут
    (SIGTERM→SIGKILL), kill группы при отмене, потоковый лимит вывода
11. Конфиг: отказ старта при group/other-writable или чужом владельце,
    warning при world-readable; запись из UI — атомарно, 0600, `.yaml.bak` 0600
12. Код к находкам: чтение только внутри клона (путь без `..`/абсолютных/`.git`
    в любом регистре, проверка inode `.git`, `O_NOFOLLOW` на каждом сегменте),
    только обычные небинарные файлы ≤ 2 МБ; лимиты 30 находок, 40 строк /
    4000 символов на фрагмент, 60 000 на ревью
13. Ретеншн: клон удаляется сразу после ревью; при старте — workspaces старше
    `workspace_ttl_days`, review-логи старше `log_ttl_days`, askpass-остатки

## Deploy

- `deploy/install.sh`: venv, `pip install -e .`, конфиг из примера (0600),
  проверка PAT, plist → `~/Library/LaunchAgents/`, RunAtLoad + KeepAlive
- PATH для launchd включает /opt/homebrew/bin; `MRWATCH_CONFIG` в plist
- Логи: `logs/app.log` (ротация 5×5 МБ) и `logs/review-<id>.log`;
  stdout/stderr launchd — `~/Library/Logs/mr-watch/{out,err}.log`
- healthz: `{"status":"ok|degraded","last_poll","last_error","dead_tasks"}`, 503 при degraded

## Разработка

- dev-группа: pytest, pytest-asyncio, pytest-cov, ruff; `pytest` из корня
- `ruff check src tests`; покрытие `fail_under = 88`
- CI: GitHub Actions (`.github/workflows/ci.yml`) — lint + тесты с покрытием

## Не входит в MVP

- Комментарии/approve в GitLab, отметка todo как done, Telegram-уведомления
- Полный markdown-рендер ревью (только заголовки, inline code и fenced-блоки)
- Аутентификация в веб-UI (localhost, один пользователь)
- Hot-reload `max_parallel` (нужен рестарт)
- Агенты из репозитория MR и автоматическая установка агентов
