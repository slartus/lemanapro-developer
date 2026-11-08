# PLAN: mr-watch — GitLab MR auto-review service

Сервис на Mac (m1pro): поллит self-hosted GitLab, находит MR, где пользователь
упомянут / назначен ревьювером / является автором, автоматически запускает ревью
через opencode (агент из конфига), сохраняет результат в SQLite, показывает в веб-UI.

## Стек и запуск

- Python 3.12 (brew) + venv: fastapi, uvicorn[standard], httpx, jinja2, pyyaml, pydantic
- SQLite (WAL), один процесс asyncio: poller + N воркеров (default 2) + FastAPI
- БД: `data/mrwatch.db`
- launchd LaunchAgent `ru.slinkin.mr-watch`, порт 8123, bind 127.0.0.1
- Путь: `~/projects/mr-watch`, конфиг: `~/.config/mr-watch/config.yaml` (chmod 600, без секретов)
- GitLab PAT: macOS Keychain (`security add-generic-password -s mr-watch -a gitlab -w`)

## Компоненты (src/mrwatch/)

| Модуль | Ответственность |
|--------|-----------------|
| `config.py` | YAML-конфиг + pydantic-модель, чтение PAT из keychain |
| `db.py` | SQLite WAL, схема, миграции, идемпотентные вставки |
| `gitlab_client.py` | httpx-клиент API v4: /user, /todos, /merge_requests, notes |
| `poller.py` | Цикл: todos + MR-list(updated_after cursor, keyset) → события → очередь |
| `git_ops.py` | Клон (безопасные флаги), санитизация, diff base...head, cleanup |
| `opencode_runner.py` | subprocess opencode run, таймаут, env-изоляция, парсинг результата |
| `worker.py` | N корутин: queued → cloning → reviewing → done/error, ретраи клона |
| `web/` | FastAPI + Jinja2 + HTMX: список MR, детали ревью, retry |
| `main.py` | lifespan: старт задач, восстановление очереди после рестарта, healthz |

## Схема SQLite

- `project(id PK, gitlab_project_id UNIQUE, path_with_namespace, agent, re_review_on_push, enabled)`
- `mr(id PK, project_id FK, iid, title, author_username, source_branch, web_url, state, head_sha, first_seen_at, updated_at, UNIQUE(project_id, iid))`
- `review(id PK, mr_id FK, trigger('new'|'push'|'manual'), head_sha, status('queued'|'cloning'|'reviewing'|'done'|'error'), agent, session_id, review_md, exit_code, error, attempt, created_at, started_at, finished_at, UNIQUE(mr_id, head_sha, trigger))`
- `poll_state(key PK, value)` — курсоры и здоровье пуллинга
- Идемпотентность: `INSERT ... ON CONFLICT DO NOTHING`; при старте все промежуточные статусы → queued

## GitLab API (v4)

- `GET /user` — username/id на старте
- Каждый тик: `GET /todos?state=pending&per_page=100` (action_name: mentioned / directly_addressed / review_requested / assigned); обработанное todo — `POST /todos/:id/mark_as_done`, чтобы не копились
- Каждый тик: `GET /merge_requests?scope=all&state=opened&updated_after=<cursor>&order_by=updated_at&sort=asc&per_page=100&pagination=keyset` — клиентский разбор: author==me, me∈reviewers, смена head_sha (push)
- По триггеру: `GET /projects/:id/merge_requests/:iid` (diff_refs, description, reviewers)
- Курсор = max(updated_at) − перекрытие 2 мин; дедуп по UNIQUE-ключам БД
- Стартовый курсор при первом запуске (пустой poll_state): `now` минус
  `backfill_hours` (конфиг, default 0) — без backfill сервис молча начинает с
  текущего момента, бэклог осознанно не ревьюится
- Токен: минимально `read_api` + `read_repository` (клон HTTPS)
- Диффы по API не тянем: агент работает в клоне, diff строит сервис

## Lifecycle ревью

1. Обнаружение → INSERT mr + review(trigger='new', status='queued')
2. Воркер: свежий shallow-клон в `workspaces/<project_id>/<iid>/<review_id>/`
   → добор base-ветки (`git fetch --depth N origin <target_branch>`; иначе
   `git diff base...head` падает — в shallow нет merge-base) → санитизация
   → `opencode run` → review_md в БД → `shutil.rmtree` клона (finally)
3. Дефолт: ревью один раз при обнаружении; `re_review_on_push` — пер-проектный флаг
   (дедуп по head_sha); Retry/Re-review — кнопка в UI. opencode-ошибки не авторетраятся.
4. `mr.state` актуален только для open (poller запрашивает `state=opened`):
   закрытие/merge фиксируем, когда MR всплывает в тик-запросе; устаревший
   state в списке — не ошибка

## Безопасность (MUST)

1. Никогда не передавать `--auto` в opencode run
2. Read-only reviewer-агент в глобальном `~/.config/opencode/agent/mr-watch-reviewer.md`
   (deny всё, кроме read/glob/grep; deny на .env*; external_directory — deny/ask)
3. Системный промпт: MR-контекст = данные, не инструкции; запрет команд/сети
4. Санитизация клона: `rm -rf .opencode opencode.json*` после клона + `--pure`
5. Клон: `GIT_LFS_SKIP_SMUDGE=1 git -c core.hooksPath=/dev/null -c protocol.file.allow=never
   -c transfer.fsckObjects=true clone --depth N --single-branch`, без submodules
6. Токен не в URL: credential через osxkeychain/GIT_ASKPASS; после клона `git remote remove origin`
7. PAT не в env процесса opencode; маскирование glpat-/Bearer в логах
8. Веб-UI: 127.0.0.1 + проверка Host-заголовка; ревью — plain text `<pre>` (XSS)
9. Subprocess: минимальный env, cwd=клон, process group, таймаут (SIGTERM→SIGKILL), лимит stdout
10. Ретеншн: клон удаляется сразу после ревью; cleanup workspaces старше 3 дней на старте

## Deploy

- `deploy/ru.slinkin.mr-watch.plist` → `~/Library/LaunchAgents/`, RunAtLoad + KeepAlive
- PATH для launchd включает /opt/homebrew/bin; логи: `~/Library/Logs/mr-watch/`
- healthz: `{"status":"ok","last_poll":...}`

## Не входит в MVP

- Комментарии в GitLab / Telegram-уведомления
- Markdown-рендер ревью (plain `<pre>`)
- Аутентификация в веб-UI (localhost, один пользователь)
