# lemanapro-developer

Локальная страница разработчика для self-hosted GitLab. Объединяет два инструмента:

- **авторевью MR** (бывший mr-watch) — описан ниже;
- **радар MR** (бывший gitlab-mr-radar, каталог `radar/`) — секции «на ревью», «назначены», «упомянули», «ждут апрува», «треды», «мои MR», «возможные MR», «сломан default branch». Логика на JS, запускается бэкендом; тесты — `cd radar && node --test`.

План объединения в одну таблицу MR — в разработке.

### Переход с mr-watch

1. Остановить mr-watch и выгрузить его LaunchAgent: `launchctl unload -w ~/Library/LaunchAgents/ru.slinkin.mr-watch.plist`, plist убрать из `~/Library/LaunchAgents`.
2. `deploy/migrate-from-mr-watch.sh` — копирует БД (`data/lpdev.db`) и конфиг с новыми путями. Бэкапы mr-watch не переносятся.
3. `deploy/install.sh`.

## Авторевью

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
  - разово добирает роли (`ревьюер/упомянули/назначен/мой`) для уже
    известных MR.
  Триггеры ревью: упоминание в todo, назначение ревьювером, новый свой MR,
  push в MR (только при `re_review_on_push`).
  MR в статусе draft автоматически не ревьюится ни одним триггером. При
  переводе MR в Ready на текущий head ставится ревью `new`, если MR открыт,
  связан со мной (ревьюер/упомянули/мой) или по нему уже были ревью, и ревью
  на этот head ещё нет. При переводе в draft активные ревью
  (`queued`/`cloning`/`reviewing`) отменяются (`cancelled`); новый коммит в
  draft отменяет устаревшее ревью, но нового не ставит. Ручной Re-review для
  draft работает.
  Если в MR пришёл новый коммит, пока ревью по старому head ещё идёт
  (`cloning`/`reviewing`), оно отменяется (статус `cancelled`, процесс
  останавливается, клон удаляется) и на новый head ставится ревью `push` —
  всегда, независимо от `re_review_on_push`, но только для открытого MR.
  Ревью в очереди (`queued`) просто переносится на новый head; завершённые
  (`done`/`error`) не трогаются. Отменённое ревью можно перезапустить (Re-review).
  Debounce: ревью `push` и перенос ревью из очереди на новый head
  откладывают старт на `review.push_debounce_sec` (по умолчанию 120 с, 0 —
  без отсрочки), каждый следующий push отодвигает его снова — серия push'ей
  даёт одно ревью по устоявшемуся head. Ревью `new` (todo, ревьюер, Ready) и
  ручной Re-review не откладываются.
- **Worker'ы** (`review.max_parallel`, по умолчанию 2) берут ревью из очереди в
  SQLite и до клона сверяют `head_sha` ревью с текущим head MR в GitLab
  (закрывает рестарт с ревью на старом коммите и пропущенную poller'ом смену
  head). Если head ушёл: MR закрыт/смержен или в draft — ревью `cancelled`;
  иначе ревью переносится на новый head и возвращается в очередь с той же
  отсрочкой `push_debounce_sec` (если на новый head уже есть такое ревью —
  `cancelled`). Дальше — shallow-клон ровно на `head_sha` ревью, base-ветка в
  `refs/lpdev/base`, diff `git diff <base_sha> <head_sha>` (base —
  `diff_refs.base_sha` MR, а если head MR ушёл вперёд — merge-base с
  `refs/lpdev/base`), затем `opencode run` с агентом проекта. После ответа,
  до удаления клона, сервис сам вырезает код к находкам по `путь:строка`:
  hunk диффа ±6 строк, а если строки нет в диффе — ±4 строки файла из клона.
  Ошибки клона — до 3 попыток с бэкоффом; если head MR уехал —
  ревью переносится на новый head. При свободном месте < 5 ГБ ревью не
  запускается. К Re-review можно приложить доп. инструкции (до 2000
  символов): они идут в промпт отдельной секцией после правил и до данных MR
  и сохраняются при переносе ревью на новый head.
- **Вопросы ревьюеру**: на странице завершённого ревью (`done` с сессией
  opencode) можно задать вопрос (до 4000 символов, до 50 на ревью, по одному
  за раз). Отдельный worker клонирует `head_sha` ревью в тот же каталог, что
  и ревью, и запускает `opencode run --session`: первый вопрос — форк сессии
  ревью (`--fork`), следующие продолжают сессию предыдущего ответа. Таймаут —
  `review.question_timeout_sec` (600 с), лог — `logs/question-<id>.log`.
  Пока вопрос в работе, Re-review запрещён. После Re-review старые вопросы
  показываются свёрнутыми «к предыдущей версии ревью». Если коммита ревью
  больше нет в репозитории, вопрос завершается ошибкой.
- **Веб-UI** на `http://127.0.0.1:8123`: таблица открытых MR (закрытые и
  смерженные не показываются, сортировка по последней активности в GitLab) с
  ролями (`ревьюер/упомянули/назначен/мой`), draft, вердиктом, счётчиками находок и
  свежестью активности (подсветка > 3 и > 7 дней); фильтры по статусу, роли,
  «требуют правок»; бейдж «упомянули» ведёт на комментарий с
  упоминанием. Автообновление через `/api/mrs` и `/api/reviews/{id}`: 5 с при
  активных ревью, иначе 30 с, в скрытой вкладке — раз в 60 с; ревью, завершившиеся
  (`done`/`error`) пока вкладка скрыта, показываются счётчиком `(N)` в заголовке
  вкладки и красной точкой на favicon, сброс — при возврате на вкладку; после «Проверить
  сейчас» — быстрые обновления через ~2 и ~6 с. Страница ревью — текст ревью
  (пропорциональный шрифт, markdown-заголовки, inline code, fenced-блоки
  модели — как код с подписью языка), код к находке сразу после её строки
  (несопоставленные — внизу, «Код к находкам»), кнопка «Развернуть весь код» и
  Re-review (берёт текущего агента проекта). `/settings` — правка интервала, параметров
  ревью и проектов (проект задаётся ссылкой или slug), «Проверить сейчас» —
  перечитать конфиг и опросить GitLab сразу.

## Безопасность

- opencode: `--pure`, **без** `--auto`; окружение — только `PATH`, `HOME`,
  `TMPDIR` и `OPENCODE_DISABLE_PROJECT_CONFIG=1`,
  `OPENCODE_DISABLE_CLAUDE_CODE=1`; PAT в окружение не попадает.
- Агенты — только глобальные (`~/.config/opencode/agent/` или `agents/`) и
  только read-only: перед каждым ревью frontmatter проверяется, `edit`, `bash`,
  `webfetch` должны быть запрещены (`tools: false` или `permission: deny`),
  иначе ревью падает с ошибкой. В `/settings` предлагаются только такие агенты;
  если их нет — предлагается `mr-watch-reviewer` по умолчанию (при запуске
  ревью он всё равно проходит проверку read-only).
- Клон: без hooks и LFS, `protocol.file.allow=never`,
  `transfer.fsckObjects=true`, `core.symlinks=false`; PAT отдаётся через
  временный `GIT_ASKPASS`, в URL и `.git/config` не попадает, remote после
  клона удаляется. Санитизация (без учёта регистра): в корне — `.opencode`,
  `opencode.json(c)`, `AGENTS.md`, `CLAUDE.md`, `CONTEXT.md`, `.claude`,
  `.agents`; рекурсивно — `AGENTS.md`, `CLAUDE.md`, `CONTEXT.md`. Если что-то
  не удалилось — ревью падает.
- Код к находкам читается только внутри клона: путь без `..`, абсолютных
  путей и `.git` (в любом регистре, а также по inode каталога `.git`), открытие
  через `O_NOFOLLOW`, только обычные файлы ≤ 2 МБ, не бинарные; лимиты —
  30 находок, 40 строк / 4000 символов на фрагмент, 60 000 символов на ревью.
- Subprocess git/opencode: своя process group, таймаут (SIGTERM → SIGKILL),
  убийство группы при отмене, лимит вывода; `glpat-`/`Bearer` маскируются в
  логах.
- `gitlab.url` — только `https://`, без логина/пароля, query и fragment.
  Ссылки в UI проходят `safe_url` (только хост GitLab).
- Веб-UI: bind `127.0.0.1`, Host-check (`127.0.0.1:8123`/`localhost:8123`),
  POST-ручки требуют `X-Requested-With: lpdev` (CSRF), HTML с `no-store`.
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

Путь — `$LPDEV_CONFIG`; launchd-plist задаёт `<проект>/config.yaml`, без
переменной — `~/.config/lemanapro-developer/config.yaml`. Поля — в `config.example.yaml`:
GitLab (URL, интервал, `pat_command`), проекты (`gitlab_project_id`, `slug`,
`agent`, `re_review_on_push`, опционально `model`/`variant`), ревью
(параллельность, таймаут, глубина клона, TTL workspaces и логов, отсрочка
ревью после push `push_debounce_sec`), opencode
(`bin`, `model`, `variant`, `length_retry_variant`), пути.

Если модель упёрлась в лимит вывода (`reason=length`) и не выдала текста, ревью
автоматически повторяется с `opencode.length_retry_variant` (по умолчанию
`low`, `null` — без повтора) в пределах того же таймаута.

Изменение `max_parallel` через UI применяется только после рестарта.

## Агент ревью

Агенты живут вне репо, в `~/.config/opencode/agent/<имя>.md`:
`mr-watch-reviewer` (общий, по умолчанию), `lmwork-reviewer` (LMWork),
`platformeco-reviewer` (BFF-каналы Platformeco). Требования:

- frontmatter запрещает `edit`, `bash`, `webfetch` (иначе агент отклоняется);
- находки — строками списка `- [BLOCKER|MAJOR|MINOR|NIT] путь:строка — ...`:
  ссылка `путь:строка` (или `путь:от-до`) от корня репо сразу после маркера
  уровня, код не цитировать — его подставит сервис;
- блок исправления — только для BLOCKER/MAJOR, ≤ 5 строк, не больше 3 на ревью;
- критерии уровней явные: BLOCKER — auth, утечка токена/секретов, инъекции,
  потеря данных, падение, поломка контракта;
- в конце секция `## Verdict` с одним из `APPROVE`, `APPROVE_WITH_NITS`,
  `REQUEST_CHANGES` (или строка `Verdict: ...`); есть BLOCKER или MAJOR →
  `REQUEST_CHANGES`.

Счётчики уровней, вердикт и привязка кода разбираются одним парсером с общими
правилами: строки по `\n` (с отбросом `\r`), содержимое fenced-блоков и цитат
(`>`) игнорируется.

## Эксплуатация

```bash
launchctl load|unload ~/Library/LaunchAgents/ru.slinkin.lemanapro-developer.plist
tail -f logs/app.log                      # основной лог
tail -f ~/Library/Logs/lemanapro-developer/err.log   # stdout/stderr launchd (out.log/err.log)
curl -s http://127.0.0.1:8123/healthz     # status, last_poll, last_error, dead_tasks
```

- `/healthz`: 200 `ok`; 503 `degraded`, если последний опрос упал или умерла
  фоновая задача (поллер/воркер/бэкап — в `dead_tasks`).
- БД: `data/lpdev.db` (SQLite WAL, миграции через `PRAGMA user_version`).
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
