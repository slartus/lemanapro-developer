# mr-watch

Локальный сервис автоматического ревью merge request для self-hosted GitLab.
Следит за MR, где вы упомянуты, назначены ревьювером/исполнителем или являетесь
автором, и прогоняет их через [opencode](https://opencode.ai) с read-only
агентом ревью. Результаты — в локальной базе и веб-UI, в GitLab ничего не
пишется (ни комментарии, ни отметки todo).

## Как работает

- **Poller** на каждый тик:
  - читает pending-todo (`/todos`); обработанные todo запоминаются локально
    (таблица `todo_seen`), в GitLab не помечаются;
  - при первом появлении проекта делает фулскан открытых MR
    (`/projects/:id/merge_requests`), ищет упоминания в описании и заметках;
  - дальше — sweep по `/projects/:id/merge_requests?updated_after=<курсор>`
    с отдельным курсором на каждый проект (перекрытие 2 мин);
  - разово добирает роли (`ревьюер/упомянули/назначен/мой MR`) для уже
    известных MR.
  Триггеры ревью: упоминание в todo, назначение ревьювером, новый свой MR,
  push в MR (только при `re_review_on_push`).
- **Worker'ы** (`review.max_parallel`, по умолчанию 2) берут ревью из очереди в
  SQLite: shallow-клон ровно на `head_sha` ревью, base-ветка в
  `refs/mrwatch/base`, diff `base...head`, затем `opencode run` с агентом
  проекта. Ошибки клона — до 3 попыток с бэкоффом; если head MR уехал —
  ревью переносится на новый head. При свободном месте < 5 ГБ ревью не
  запускается.
- **Веб-UI** на `http://127.0.0.1:8123`: таблица открытых MR (закрытые и
  смерженные не показываются) с ролями, draft, вердиктом, счётчиками находок и
  свежестью активности (подсветка > 3 и > 7 дней); фильтры по статусу, роли,
  «скрыть draft», «требуют правок»; бейдж «упомянули» ведёт на комментарий с
  упоминанием. Автообновление через `/api/mrs` и `/api/reviews/{id}`. Страница
  ревью — полный текст и Re-review. `/settings` — правка интервала, параметров
  ревью и проектов (проект задаётся ссылкой или slug), «Проверить сейчас» —
  перечитать конфиг и опросить GitLab сразу.

## Безопасность

- opencode: `--pure`, **без** `--auto`; окружение — только `PATH`, `HOME`,
  `TMPDIR` и `OPENCODE_DISABLE_PROJECT_CONFIG=1`,
  `OPENCODE_DISABLE_CLAUDE_CODE=1`; PAT в окружение не попадает.
- Агенты — только глобальные (`~/.config/opencode/agent/` или `agents/`) и
  только read-only: перед каждым ревью frontmatter проверяется, `edit`, `bash`,
  `webfetch` должны быть запрещены (`tools: false` или `permission: deny`),
  иначе ревью падает с ошибкой. В `/settings` предлагаются только такие агенты.
- Клон: без hooks и LFS, `protocol.file.allow=never`,
  `transfer.fsckObjects=true`, `core.symlinks=false`; PAT отдаётся через
  временный `GIT_ASKPASS`, в URL и `.git/config` не попадает, remote после
  клона удаляется. Санитизация (без учёта регистра): в корне — `.opencode`,
  `opencode.json(c)`, `AGENTS.md`, `CLAUDE.md`, `CONTEXT.md`, `.claude`,
  `.agents`; рекурсивно — `AGENTS.md`, `CLAUDE.md`, `CONTEXT.md`. Если что-то
  не удалилось — ревью падает.
- Subprocess git/opencode: своя process group, таймаут (SIGTERM → SIGKILL),
  убийство группы при отмене, лимит вывода; `glpat-`/`Bearer` маскируются в
  логах.
- `gitlab.url` — только `https://`, без логина/пароля, query и fragment.
  Ссылки в UI проходят `safe_url` (только хост GitLab).
- Веб-UI: bind `127.0.0.1`, Host-check (`127.0.0.1:8123`/`localhost:8123`),
  POST-ручки требуют `X-Requested-With: mrwatch` (CSRF), HTML с `no-store`.
- Конфиг: при group/other-writable или чужом владельце сервис не стартует,
  world-readable — warning; UI пишет конфиг и `.yaml.bak` с правами 0600.
  PAT — в macOS Keychain, в конфиге только `pat_command`.

## Установка

```bash
# 1. PAT в Keychain (scope: read_api + read_repository)
security add-generic-password -s mr-watch -a gitlab -w "<GITLAB_PAT>"

# 2. Агент ревью — в ~/.config/opencode/agent/ (см. «Агент ревью»)

# 3. Конфиг (install.sh создаст его из примера, если нет)
cp config.example.yaml config.yaml && chmod 600 config.yaml
$EDITOR config.yaml

# 4. Установка и запуск (venv + launchd)
./deploy/install.sh
```

Проверка: `curl http://127.0.0.1:8123/healthz`, UI — http://127.0.0.1:8123

## Конфиг

Путь — `$MRWATCH_CONFIG`; launchd-plist задаёт `<проект>/config.yaml`, без
переменной — `~/.config/mr-watch/config.yaml`. Поля — в `config.example.yaml`:
GitLab (URL, интервал, `pat_command`), проекты (`gitlab_project_id`, `slug`,
`agent`, `re_review_on_push`, опционально `model`/`variant`), ревью
(параллельность, таймаут, глубина клона, TTL workspaces и логов), opencode
(`bin`, `model`, `variant`, `length_retry_variant`), пути.

Если модель упёрлась в лимит вывода (`reason=length`) и не выдала текста, ревью
автоматически повторяется с `opencode.length_retry_variant` (по умолчанию
`low`, `null` — без повтора) в пределах того же таймаута.

Изменение `max_parallel` через UI применяется только после рестарта.

## Агент ревью

Агенты (`mr-watch-reviewer`, `lmwork-reviewer` и др.) живут вне репо, в
`~/.config/opencode/agent/<имя>.md`. Требования:

- frontmatter запрещает `edit`, `bash`, `webfetch` (иначе агент отклоняется);
- находки — строками списка с маркером `- [BLOCKER|MAJOR|MINOR|NIT] ...`
  (по ним считаются счётчики);
- в конце секция `## Verdict` с одним из `APPROVE`, `APPROVE_WITH_NITS`,
  `REQUEST_CHANGES` (или строка `Verdict: ...`).

## Эксплуатация

```bash
launchctl load|unload ~/Library/LaunchAgents/ru.slinkin.mr-watch.plist
tail -f logs/app.log                      # основной лог
tail -f ~/Library/Logs/mr-watch/err.log   # stdout/stderr launchd (out.log/err.log)
curl -s http://127.0.0.1:8123/healthz     # status, last_poll, last_error, dead_tasks
```

- `/healthz`: 200 `ok`; 503 `degraded`, если последний опрос упал или умерла
  фоновая задача (поллер/воркер/бэкап — в `dead_tasks`).
- БД: `data/mrwatch.db` (SQLite WAL, миграции через `PRAGMA user_version`).
  Бэкап раз в сутки в `data/backups/` (при старте — сразу, если последний
  старше суток), хранится 7.
- Логи: `logs/app.log` (ротация 5 × 5 МБ), `logs/review-<id>.log` — удаляются
  при старте старше `review.log_ttl_days`.
- Workspaces клонов: `workspaces/` — удаляются после ревью; старше
  `review.workspace_ttl_days` чистятся при старте. Временные askpass-скрипты —
  в `tmp/`, чистятся при старте.

## Разработка

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e . --group dev    # pip >= 25.1; иначе: pip install -e . pytest pytest-asyncio pytest-cov ruff
.venv/bin/pytest                          # из корня, настройки в pyproject.toml
.venv/bin/pytest --cov                    # порог покрытия 88%
.venv/bin/ruff check src tests
```

CI (GitHub Actions, `.github/workflows/ci.yml`): `ruff check src tests` и
`pytest tests -q --cov` на Python 3.12.

История изменений: `docs/CHANGELOG.md`, архитектура: `PLAN.md`.
