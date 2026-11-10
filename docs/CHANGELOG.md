# CHANGELOG — mr-watch

Все изменения проекта фиксируются здесь. Каждая запись проходит self-review
отдельным агентом; находки и исправления отражаются в записи.

Формат: `## <дата> — <изменение>` + секции `Что`, `Self-review`, `Правки`.

## 2026-11-07 — Инициализация проекта

**Что:** создан скелет `~/projects/mr-watch/` (src/mrwatch, docs, deploy, data,
workspaces, logs, tests), PLAN.md — архитектура из консилиума (architect/devops/
api/security): Python 3.12 + FastAPI + SQLite, один процесс, poller + 2 воркера,
opencode-ревью в read-only песочнице, launchd-деплой.

**Self-review:** ревью-агент, вердикт NEEDS_FIXES — 2 MAJOR, 4 MINOR.

**Правки по находкам:**
- [MAJOR] PLAN.md: shallow-клон не содержит base — добавлен добор base-ветки
  (`git fetch --depth N origin <target_branch>`) перед `git diff base...head`
- [MAJOR] PLAN.md: не был определён стартовый курсор — задан: `now` − `backfill_hours`
  (default 0), бэклог осознанно не ревьюится
- [MINOR] PLAN.md: добавлено `POST /todos/:id/mark_as_read` после обработки todo
- [MINOR] PLAN.md: указан путь БД `data/mrwatch.db`
- [MINOR] PLAN.md: оговорено, что `mr.state` актуален только для open-MR
- [MINOR] CHANGELOG: добавлена секция «Правки» в запись (этот блок)

## 2026-11-07 — Чанк 2: config + db

**Что:** `src/mrwatch/config.py` (pydantic v2 + YAML, ConfigError, PAT из
keychain через pat_command, resolve_paths, MRWATCH_CONFIG), `src/mrwatch/db.py`
(SQLite WAL: project/mr/review/poll_state, идемпотентные upsert, claim через
BEGIN IMMEDIATE, requeue при старте, backup, user_version-миграции),
`pyproject.toml` (src-layout, dev-группа pytest), `config.example.yaml`,
тесты `tests/test_config.py` + `tests/test_db.py` (29 шт).

**Self-review:** ревью-агент, вердикт NEEDS_FIXES — 1 MAJOR, 6 MINOR.
Проверено запуском сьюта: изначально 25 passed / 1 failed (баг теста).

**Правки по находкам:**
- [MAJOR] tests: test_requeue_incomplete проверял не тот review — claim
  возвращает первый queued, ассерты переведены на claimed["id"]
- [MINOR] db: retry_review теперь очищает review_md/session_id/exit_code
- [MINOR] db: механизм миграций — PRAGMA user_version + SCHEMA_VERSION
- [MINOR] config: Field(ge=1) на числовых полях (poll_interval, max_parallel,
  timeout, clone_depth, workspace_ttl_days; backfill_hours ge=0)
- [MINOR] pyproject: [dependency-groups] dev = ["pytest"]
- [MINOR] db: request_review() — re-review done/error с тем же (mr, sha,
  trigger) без упора в UNIQUE; retry_review расширен на status 'done'
- [MINOR] db: upsert_mr при update — COALESCE для state/head_sha (None не
  затирает старые значения, флаги changed согласованы); +тесты (FK-нарушение,
  update c None, request_review, retry из done)
- Итог сьюта: 29 passed

## 2026-11-07 — Чанк 3: gitlab_client + poller

**Что:** `src/mrwatch/gitlab_client.py` (httpx.AsyncClient, PRIVATE-TOKEN,
ретраи 429/5xx c Retry-After cap 10s, 401/403 сразу GitlabApiError, keyset-
пагинация по Link cap 50 страниц, todos/merge_requests/project_mr/notes/
mark_todo_done), `src/mrwatch/poller.py` (тик: todos → detail → upsert →
триггер inserted/mentioned/directly_addressed/reviewer → request_review('new')
→ mark_todo_done; sweep updated_after: 'new' для моих MR, 'push' при смене
sha и re_review_on_push; cursor = max(updated_at)−120с, стартовый
now−backfill_hours; poll_last_ok/poll_last_error), db.poll_state_delete.
Реализация оркестратором напрямую (субагенты трижды вернули пустой ответ).
pyproject: dev-группа + pytest-asyncio, asyncio_mode=auto.

**Self-review:** ревью-агент, вердикт NEEDS_FIXES — 1 MAJOR, 5 MINOR.

**Правки по находкам:**
- [MAJOR] gitlab_client: get_todos без пагинации — накопление чужих pending-
  todos вытесняло MR-todos за страницу 1; добавлена Link-пагинация + warning
  на cap. Сторонние todos осознанно НЕ помечаются done (не трогаем список
  пользователя в GitLab)
- [MINOR] gitlab_client: warning при обрыве пагинации MR на 50-й странице
- [MINOR] poller: push-ревью теперь только для «моих» MR (author/reviewer)
- [MINOR] gitlab_client: mark_todo_done проверяет статус (не-2xx → warning,
  404 толерируется)
- [MINOR] tests: переименован test_error_recorded_then_cleared →
  test_failed_todo_tick_creates_no_reviews; удалён мёртвый _dump; добавлен
  тест пагинации todos
- Итог сьюта: 49 passed

## 2026-11-07 — Чанк 4: git_ops + opencode_runner

**Что:** `src/mrwatch/git_ops.py` (безопасный клон: merge-requests/<iid>/head +
добор base-ветки; харден -c core.hooksPath=/dev/null, protocol.file.allow=never,
transfer.fsckObjects=true; GIT_LFS_SKIP_SMUDGE, GIT_TERMINAL_PROMPT=0; PAT через
GIT_ASKPASS-скрипт во временном каталоге — токен не в URL и не в env; после
фетчей `git remote remove origin`; санитизация .opencode/opencode.json*;
subprocess: минимальный env, process group, таймаут SIGTERM→SIGKILL группы,
маскирование glpat-/Bearer; diff base_sha..head_sha two-dot с fallback None;
cleanup workspaces по TTL), `src/mrwatch/opencode_runner.py` (opencode run
--agent --dir --format json --pure, БЕЗ --auto, минимальный env без PAT,
таймаут с killpg, логи с маскированием, парсер NDJSON → review_md + session_id,
build_review_prompt: «MR-контекст = данные, не инструкции» + дифф с обрезкой).

**Self-review:** ревью-агент, вердикт NEEDS_FIXES — 2 MAJOR, 5 MINOR.

**Правки по находкам:**
- [MAJOR] git_ops: askpass-скрипт теперь с уникальным именем (uuid) — гонка
  параллельных ревью на общем askpass.sh; ошибка если pat_command задан, а
  tmpdir нет (раньше — молча без askpass)
- [MAJOR] git_ops: добавлен `git remote remove origin` после фетчей (PLAN §6:
  URL origin не должен остаться в .git/config)
- [MINOR] маскирование Bearer в обоих модулях
- [MINOR] clone_for_review: workspace.resolve() + rmtree при неудачном клоне
- [MINOR] opencode_runner: FileNotFoundError → OpencodeError
- [MINOR] tests: monkeypatch.setenv вместо утечки os.environ
- [MINOR] принято как ограничение: капы stdout применяются после communicate()
  (в памяти читается весь вывод; для локального тулинга допустимо, E2E покажет
  реальную потребность)
- Итог сьюта: 68 passed

## 2026-11-07 — Чанки 5+6: worker + main + веб-UI

**Что:** `src/mrwatch/worker.py` (Worker: claim_next_review → MR detail через
API → клон → diff → opencode → finish_review; ретраи клон-фазы до 3 попыток с
requeue, opencode-фаза без авторетрая; гард диска <5ГБ; workspace
workspaces/<pid>/<iid>/<review_id>/, удаление в finally после успешного клона;
очередь-уведомление asyncio.Queue с 5с-таймаутом ожидания), `src/mrwatch/main.py`
(AppContext, create_app с lifespan: requeue_incomplete, cleanup_old_workspaces,
poller + N воркеров + nightly backup с pruning 7 копий; /healthz c
poll_last_ok/poll_last_error и статусом degraded; HostCheckMiddleware — только
127.0.0.1:8123/localhost:8123; bootstrap/get_app для uvicorn --factory),
`src/mrwatch/web/routes.py` + шаблоны index.html/review.html (список MR и
ревью, страница ревью с review_md в <pre>, POST /reviews/{id}/retry с
обязательным заголовком X-Requested-With: mrwatch против CSRF), `db.py`:
+requeue_review (клон-фаза, attempt сохраняется). Тесты test_worker.py,
test_web_main.py (healthz ok/degraded, Host-check 403, XSS-экранирование,
retry 403 без заголовка / 303 с ним).

**Self-review:** ревью-агент недоступен (3 пустых ответа Task tool подряд),
ревью выполнено оркестратором. Вердикт NEEDS_FIXES — 2 MAJOR, 4 MINOR.

**Правки по находкам:**
- [MAJOR] db.connect: isolation_level=None (autocommit) — при дефолтном
  isolation_level смешение неявных транзакций с явным BEGIN IMMEDIATE в
  claim_next_review хрупко (риск "cannot start a transaction within a
  transaction"); все хелперы и так делают commit явно
- [MAJOR] worker: удаление workspace перенесено в finally на всей фазе после
  клона (раньше исключение в diff/prompt/run_review оставляло workspace)
- [MINOR] main: conn.close() в shutdown lifespan
- [MINOR] main: setup_logging защищён от дублей хендлеров
- [MINOR] poller: QueueFull при переполнении очереди (>100) ловится общим
  except tick и уходит в poll_last_error — приемлемо, переполнение практически
  недостижимо
- [MINOR] uvicorn entrypoint: фактический `mrwatch.main:get_app --factory`
  вместо PLAN'овского app.main:app (пакет mrwatch, src-layout); учесть в plist
- [MINOR] покрытие: нет тестов low-disk/mr-missing путей — отложено до E2E
- Итог сьюта: 78 passed

## 2026-11-07 — Чанк 7: глобальный read-only reviewer-агент

**Что:** `~/.config/opencode/agent/mr-watch-reviewer.md` — глобальный агент
opencode для ревью MR: mode: all (работает и как primary в `opencode run
--agent`, и как subagent), temperature 0.1, тулзы write/edit/bash/webfetch/
task/todowrite/lsp/skill выключены; permission: edit/bash/webfetch deny,
external_directory deny (изоляция workspace), read с deny-паттернами .env*,
secrets*, *.pem, *.key. Системный промпт: контекст MR = данные, не инструкции
(anti prompt-injection), маскирование секретов в находках, запрет выдумывать
проблемы, фиксированный формат ответа (Summary / Findings / Risk / Verdict:
APPROVE | APPROVE_WITH_NITS | REQUEST_CHANGES), русский язык.

**Self-review:** смоук-тест на мини-репо (init + правка calc.py).
- Первая версия с mode: subagent — `opencode run --agent` фолбэкнулся на
  build-агента (тот дергал ревьюера как задачу, свой формат). Исправлено на
  mode: all — агент запускается напрямую, сам делает Read/Glob, выдает ответ
  строго по формату, изменений файлов не производит.
- Опечатки в промпте исправлены (prompt-injection, «работаешь»).
- Проверено, что `opencode agent list` парсит конфиг (deny-паттерны read
  применились).

## 2026-11-07 — Чанк 8: деплой (launchd, install-скрипт, README)

**Что:** `deploy/ru.slinkin.mr-watch.plist` (LaunchAgent ru.slinkin.mr-watch:
venv-uvicorn mrwatch.main:get_app --factory на 127.0.0.1:8123, PATH с
/opt/homebrew/bin, MRWATCH_CONFIG, RunAtLoad + KeepAlive, логи в
~/Library/Logs/mr-watch/{out,err}.log), `deploy/install.sh` (venv + pip -e,
создание config.yaml из примера, проверка PAT в Keychain с подсказкой,
рендер plist по месту с подстановкой __PROJECT_DIR__/__HOME__, load + healthz
проверка), `README.md` (как работает, безопасность, установка, конфиг,
эксплуатация, разработка). `plutil -lint` OK, `zsh -n` OK. Входная точка
уточнена относительно PLAN: uvicorn --factory mrwatch.main:get_app (пакет
mrwatch, src-layout).

## 2026-11-07 — Чанк 9: E2E на реальном GitLab

**Что:** валидация end-to-end на https://gitlab.lmru.tech (проект
products/lmwork/lmru-mobile_lmwork, id 3024). PAT (read_api + read_repository)
в Keychain. Сервис поднят вручную (uvicorn на 127.0.0.1:8123), два ревью
существующих MR (!5170, !5168) прогнаны через request_review; MR не
создавались — только чтение. Оба ревью завершены статусом done (4120 и 2707
символов, вердикт APPROVE_WITH_NITS по SUPERAPP-задаче), workspaces
очищены, healthz ok, UI отдаёт страницы, чужой Host → 403.

**Self-review:** E2E выявил 5 дефектов, все исправлены, 78 тестов зелёные:
- [MAJOR] load_config не разворачивал `~` в opencode.bin → FileNotFoundError
  subprocess. Фикс: expanduser в load_config.
- [MAJOR] бесконечный цикл re-review: pending todo + авто-ретрай done-ревью
  с тем же (mr_id, head_sha, trigger) → каждые 60с done сбрасывался в queued.
  Фикс: поллер вызывает create_review (без ретрая); request_review остался
  только для веб-эндпоинта retry.
- [MINOR] неверный эндпоинт: POST /todos/:id/mark_as_done, а не mark_as_read
  (404); PLAN поправлен. Todo остаются pending без write-scope (безвредно —
  дедап по (mr_id, head_sha, trigger) защищает от дублей).
- [MINOR] статус не переключался в 'reviewing' перед opencode → db.mark_reviewing
  + вызов в worker после клона.
- [MINOR] OpencodeError падал в 'internal worker error' → worker ловит
  OpencodeError → структурный error 'opencode failed: ...', remove в finally.

**Правки по находкам:** перечислены выше; повторный прогон E2E подтверждил
стабильность done-статусов на тиках поллера (цикл сломан).

## 2026-11-07 — Триггеры по требованию пользователя: ревьювер, упоминания, push, счётчики

**Что:**
- Первичный full scan: при отсутствии курсора поллер сканирует все открытые
  MR watched-проектов (get_project_merge_requests с keyset-пагинацией) и
  создаёт ревью для MR, где пользователь автор/ревьювер, или упомянут
  (`@username`) в описании/комментариях (notes). После скана пишется курсор —
  дальше работает инкрементальный sweep. backfill_hours удалён из конфига
  (заменён full scan).
- re_review_on_push включён в config.yaml (true): новый коммит после ревью
  → повторное ревью (механизм существовал, был выключен).
- Счётчики находок: парсер `parse_severity_counts` ([BLOCKER/MAJOR/MINOR/NIT]
  маркеры), колонки review.crit_count/major_count/minor_count/nit_count
  (миграция SCHEMA_VERSION 2), worker пишет counts при done, retry сбрасывает.
- UI: колонка «Находки» в списке MR (counts последнего done-ревью) + бейджи
  на странице ревью.
- Формат агента mr-watch-reviewer: добавлен уровень NIT с описанием уровней
  (BLOCKER — критичный, MAJOR — вероятный баг, MINOR — качество, NIT — мелочь).

**Self-review:** само (ревью-агент недоступен — пустые ответы Task).
- Regex парсера строгий: только заглавные маркеры в начале пункта списка —
  текст «email me@example.com» и `[major]` не считаются (тесты).
- Миграция v1→v2: ALTER TABLE, старые ревью получают 0 (тест на реальном
  апгрейде user_version 1→2).
- list_mrs: LEFT JOIN по последнему done-ревью на MR — MR без ревью показывают «—».
- Найден и исправлен баг теста full_scan_cursor_used_by_sweep (нужен второй
  tick — курсор пишется в конце первого).

**Правки по находкам:** перечислены выше; 88 тестов зелёные.

## 2026-11-07 — Ясная ошибка при обрыве вывода модели

**Что:** если opencode вернул `step-finish reason=length` без текста ревью
(модель выжгла reasoning-бюджет на большом диффе), ошибка теперь объясняет
это явно («model stopped at token limit (reason=length)…») вместо общего
«no review text». Функция `output_truncated_by_length` в opencode_runner.

**Self-review:** само (тест output_truncated_by_length: length → True,
stop/пусто → False). 89 тестов зелёные.

## 2026-11-07 — Бейджи находок без подписей

**Что:** в панели бейджи находок показывают только число (цвет = уровень):
нули скрыты, при полном отсутствии находок — зелёная галочка. Легенда
уровней — одна строка над таблицей. Обновлены index.html и review.html.

**Self-review:** само (тесты обновлены на формат бейджей, 89 зелёных).

## 2026-11-07 — Панель: находки в таблице ревью + мелочи

**Что:** колонка «Находки» (бейджи crit/major/minor/nit, 0 скрыты, чисто → ✓)
перенесена из «MR на контроле» в «Последние ревью» (считается по каждой
строке ревью из review.*; легенда над первой таблицей). Там же: проект —
только последний сегмент пути; автор — author_name из GitLab (миграция
SCHEMA_VERSION=3, mr.author_name, upsert_mr с COALESCE; поллер передаёт
author.name в todos/full_scan/sweep).

**Self-review:** ревью-агент недоступен (пустые Task) — проверка оркестратором:
90 тестов зелёные; GET / содержит бейджи в таблице ревью и легенду;
healthz ok.

## 2026-11-07 — Панель: одна таблица вместо двух

**Что:** «Последние ревью» и «MR на контроле» объединены в одну таблицу —
строка на MR: проект (последний сегмент пути), MR (ссылка в GitLab), автор
(author_name), ветка, статус последнего ревью (ссылка на страницу ревью,
любой статус, не только done), находки последнего done-ревью (бейджи, ✓
если чисто), обновлён. Колонки ID и «Создано» убраны. list_mrs: latest —
последнее ревью любого статуса + latest_review_id. На странице ревью —
список всех ревью этого MR (текущее выделено), чтобы история была достижима.

**Self-review:** ревью-агент недоступен (пустые Task) — проверка оркестратором:
90 тестов зелёные; GET / отдаёт одну таблицу с ожидаемыми колонками;
healthz ok.

## 2026-11-07 — Панель: тёмная тема

**Что:** тёмная тема обеих страниц через @media (prefers-color-scheme: dark)
— следует настройке системы/браузера автоматически, переключения JS не нужны.
Палитры: фон/текст, шапки таблиц, статусы, бейджи находок, pre, кнопки;
color-scheme: dark для нативных контролов.

**Self-review:** ревью-агент недоступен — проверка оркестратором: 90 тестов
зелёные; обе страницы содержат dark-блок; сервис перезапущен, healthz ok.

## 2026-11-07 — Поллер: добавление проекта в конфиг подхватывает его фулсканом

**Что:** ранее фулскан выполнялся один раз при отсутствии mr_cursor, поэтому
новый проект в config.yaml не сканировался (курсор уже стоял). Теперь: (a)
фулскан вызывается каждый тик, но пропускает уже просканированные проекты
(per-project маркер poll_state mr_scan_done:<gitlab_pid>, ставится только при
успешном скане — упавший проект ретраится следующим тиком); (b) если общий
курсор после сканов всё ещё не выставлен (пустые проекты) — инициализируется
now−overlap, sweep стартует со следующего тика; (c) в config.yaml добавлен
проект products/lmwork/peach-platformeco-channel (id 5268, разработан через
GitLab API). Первый тик после рестарта нашёл в peach 1 MR пользователя →
ревью в очереди.

**Self-review:** ревью-агент недоступен — проверка оркестратором: 91 тест
(+тест: проект, добавленный после первого скана, подхватывается следующим
тиком без пересканирования старых); E2E: peach MR появился, ревью queued.

## 2026-11-07 — Панель: мержнутые и закрытые MR остаются видимыми

**Что:** ранее смотрелись только state=opened (фулскан и sweep) — MR при
мерже выпадал из sweep и оставался в таблице с устаревшим «opened», а
смерженные до установки сервиса никогда не попадали. Теперь sweep ходит
без фильтра state (scope=all + updated_after — объём не растёт): state
merged/closed подтягивается в БД (upsert COALESCE уже умел). Фулскан
сознательно оставлен opened-only (иначе первый скан тянет всю историю +
notes по каждому). В панели у названия MR — бейдж «смержен»/«закрыт»
(для opened не показывается), свои цвета для light/dark темы.

**Self-review:** ревью-агент недоступен — проверка оркестратором: 92 теста
(+test_sweep_picks_up_merged_state); сервис перезапущен, healthz ok.

## 2026-11-07 — Панель: только открытые MR (уточнение)

**Что:** по решению пользователя закрытые/смерженные MR в панели не нужны.
Sweep оставлен без фильтра state (дёшево видит мерж), но list_mrs отдаёт
только state IS NULL/’opened’ — строка смерженного MR корректно исчезает из
панели вместо того, чтобы висеть с устаревшим «opened». Бейджи
смержен/закрыт убраны (неактуальны). Фулскан — по-прежнему opened-only.

**Self-review:** ревью-агент недоступен — проверка оркестратором: 93 теста
(+test_list_mrs_hides_merged); сервис перезапущен, healthz ok.

## 2026-11-07 — Панель одной таблицей, непрерывный фулскан, форс-проверка

**Что:** по запросам пользователя:
- Бейджи находок перенесены из таблицы MR в сводную: одна таблица
  «Проект | MR | Автор | Ветка | Ревью | Находки | Обновлён» — строка на MR,
  статус последнего ревью ведёт на его страницу; история ревью — на странице
  MR. Колонки ID и «Создано» убраны.
- Тёмная тема (prefers-color-scheme) в обеих страницах.
- В конфиг добавлен проект peach-platformeco-channel (id 5268).
- Фулскан открытых MR теперь выполняется каждый тик, просканированные
  проекты пропускаются по маркеру mr_scan_done:<pid> (ставится только при
  успехе); новый/пере-добавленный проект подхватывается автоматически.
- Sweep ходит без state-фильтра (видит мерж), но ревью и панель — только
  открытые; смерженные не висят с ложным статусом.
- Кнопка «Проверить сейчас» (POST /force-check): перечитывает конфиг,
  sync_projects, будит поллер; защита X-Requested-With; 400 без утечки путей.

**Self-review:** отдельный агент, вердикт REQUEST_CHANGES (3 MAJOR, 4 MINOR,
5 NIT). Исправлены все MAJOR/MINOR/NIT, кроме двух осознанных исключений
(см. ниже):
- [MAJOR] впервые увиденный merged/closed MR создавал бы ревью — гвард
  state in (None, 'opened') в sweep И full_scan (второй вскрыт новым тестом).
- [MAJOR] стейл-маркер блокировал повторно добавленный проект —
  sync_projects инвалидирует маркер при insert/enable; SCAN_DONE_KEY вынесен
  в db.py.
- [MAJOR] list_mrs материализовала review_md всех последних ревью — явные
  колонки; находки берутся из последнего done-ревью (fallback при error).
- [MINOR] потеря wake между timeout и clear — clear до wait + is_set→continue.
- [MINOR] force-check обновляет ctx.config; рестарт всё ещё нужен для
  gitlab.url/max_parallel/opencode.bin/PAT (зафиксировано).
- [MINOR] кнопка: disable во время запроса, try/catch, разблокировка.

**Правки по находкам:** перечислены выше. Осознанно не исправлено:
объём sweep (scope=all) на большом инстансе — предсуществующий дизайн,
сузить до created_by_me + reviewer_username при необходимости; force-check
возвращается до фактического завершения тика (wake — только сигнал).
Новые тесты: полный скан нового проекта, merged-MR пути, сокрытие смерженных,
инвалидация маркера, force-check (403/400/reload+wake). Итог: 98 passed.

## 2026-11-07 — UI настроек (редактирование конфига из панели)

**Что:** страница /settings (ссылка в шапке панели): редактирование
интервала опроса, параллельных ревью, таймаута, глубины клона, TTL
workspace; таблица проектов (id, slug, агент, re_review_on_push) с
добавлением/удалением строк; сохранение — валидация pydantic-моделью,
атомарная запись (tmp+rename) config.yaml, бэкап .yaml.bak, применение на
лету (sync_projects + poller.reload + wake) без рестарта. gitlab.url,
pat_command, opencode.bin и max_parallel — только в файле (рестарт).
Защита как у force-check: X-Requested-With, чужой Host → 403, ошибка
валидации → 400 без утечки путей, файл не меняется.

**Self-review:** оркестратор (агенты недоступны в этой сессии). Проверено
тестами: рендер страницы, 403 без заголовка, сохранение с двумя проектами
(файл + бэкап + применение в ctx), отклонение невалидного значения без
порчи файла. Итог: 102 passed. Сервис перезапущен.

## 2026-11-07 — Настройки: резолвинг проекта по ссылке, агенты списком

**Что:** доработки страницы /settings:
- Поле ID проекта опционально: если пусто, сервис сам определяет id через
  GitLab API (GET /projects/<urlencoded-path>); в поле slug можно вставлять
  полную ссылку на проект — префикс gitlab-хоста и .git отрезаются, slug
  канонизируется по path_with_namespace из ответа API. Ошибка резолвинга →
  400 с понятным текстом, файл не портится.
- Агент — выпадающий список из ~/.config/opencode/agent/*.md (fallback
  mr-watch-reviewer); текущий агент проекта показывается, даже если его нет
  в списке.

**Self-review:** оркестратор (агенты недоступны в этой сессии). Новые тесты:
резолвинг по URL (id 99 записан в конфиг, slug канонизирован), ошибка
резолвинга → 400 без записи. Итог: 104 passed. Сервис перезапущен.

## 2026-11-07 — Настройки: поле ID убрано, агенты проекта из репо

**Что:**
- Колонка ID удалена полностью: проект задаётся ссылкой или slug, id
  определяется автоматически через GitLab API (GET /projects/<path>) при
  каждом сохранении; slug канонизируется по path_with_namespace.
- Агент — текстовое поле с datalist (глобальные ~/.config/opencode/agent/*.md
  + подгруженные) и кнопкой «⤵ подтянуть агенты из репо проекта»:
  GET /project-agents?slug=... читает .opencode/agents и .opencode/agent
  (оба варианта имени каталога, увидено на lmwork: agents/) на default
  branch через repository/tree, мерджит в datalist. Ошибка/пусто — понятный
  текст, страница не падает.

**Self-review:** оркестратор (агенты недоступны в этой сессии). Живой прогон:
lmru-mobile_lmwork вернул 16 агентов (lmwork-*). Новые тесты: резолвинг
slug→id при сохранении (URL → id 99), ошибка резолвинга → 400 без записи.
Итог: 104 passed. Сервис перезапущен.

## 2026-11-07 — Настройки: правки по ревью отдельного агента (итерация 1)

**Self-review:** вердикт REQUEST_CHANGES (2 MAJOR, 7 MINOR, 4 NIT).
Исправлено:
- [MAJOR] битый JSON/повреждённый config.yaml → 500. Фикс: guard'ы на тело
  запроса и структуру raw-конфига, везде 400, файл не трогается.
- [MAJOR] каждое сохранение резолвило ВСЕ проекты через GitLab API (при
  недоступности GitLab нельзя было сохранить даже локальные изменения).
  Фикс: существующий pid хранится в data-pid строки и отправляется как есть,
  API-резолвинг только для новых/беспидовых строк.
- [MINOR] мёртвые проверки статуса в get_project_by_path/get_project_tree
  (_get сам бросает GitlabApiError) — убраны.
- [MINOR] get_project_tree без пагинации (>100 файлов терялись) — цикл по
  response.links.next.
- [MINOR] пустые строки (без ссылки) молча пропадали из payload — alert.
- [MINOR] мёртвый flash ?invalid=1 — убран.
- [MINOR] гонка двух вкладок на фиксированном .tmp + отсутствие fsync —
  уникальное имя tmp (pid), fsync перед replace.
- [MINOR] сбой _apply_config после записи файла расходил состояние — теперь
  500 с подсказкой «нажмите Проверить сейчас».
- [MINOR]/[NIT] _normalize_slug: границы пути по /, чужие хосты, bare-host
  форма, схема http/https взаимозаменяемы.
- [NIT] pullAgents перезаписывал выбранный агент первым из репо (видел
  пользователь: глобальный агент «исчез», поле сменилось на
  lmwork-analytics-writer) — заполняется только пустое поле.
- [NIT] неиспользуемая AGENTS-константа удалена; color-scheme: dark в
  settings.html.
Новые тесты: _normalize_slug (6 вариантов), /project-agents (мердж двух
каталогов, 400), сохранение секций вне UI (pat_command/opencode/paths),
битое тело → 400 без записи. Итог: 109 passed. Сервис перезапущен.

## 2026-11-07 — Настройки: datalist → select (Chrome фильтрует подсказки)

**Self-review:** пользователь: после «⤵ подтянуть» в выпадающем списке для
lmwork виден только mr-watch-reviewer. Причина: datalist в Chrome фильтрует
опции по текущему значению поля — 16 подгруженных lmwork-агентов не
начинаются с «mr-watch-reviewer» и скрываются. Фикс: datalist заменён на
обычный select (все агенты видны всегда, выбранное значение сохраняется),
кнопка ⤵ добавляет агентов репо во все select-ы и показывает их количество
(«⤵ 16»); без ошибок поле больше не перезаписывается. Итог: 109 passed.

## 2026-11-07 — Правила проекта в промпте ревью

**Что:** workspace и так содержит весь рабочий дерево MR (AGENTS.md,
.opencode/rules, skills, commands), но opencode без opencode.json (его мы
вырезаем санитизацией) подхватывает только AGENTS.md автоматически — ревью
шло без правил проекта. Теперь worker собирает AGENTS.md +
.opencode/rules/*.md (по алфавиту) и инлайнит их в промпт секцией «Правила
проекта» (тоже помечены как данные, не инструкции). Лимиты: 16КБ на файл,
40КБ суммарно, обрезка с пометкой. Skills/commands из MR по-прежнему не
исполняются (у ревьюера deny bash/write/edit, skill off).

**Self-review:** оркестратор. Новые тесты: сбор правил (порядок, только
.md, AGENTS.md), кап размера с пометкой обрезки, наличие секции в промпте.
Итог: 112 passed. Сервис перезапущен.

## 2026-11-07 — Настройки: no-store для HTML, лог входящих сохранений

**Self-review:** пользователь: выбрал агента, сохранил, после перезагрузки —
снова mr-watch-reviewer. Диагностика: бэкенд сохраняет корректно (curl-POST
меняет agent в config.yaml, 303) — в файл приходило старое значение, т.е.
браузер отправил данные со старой закэшированной страницы (datalist-версия
JS). Фиксы: Cache-Control: no-store для всех text/html ответов (middleware),
INFO-лог списка (pid, agent) при каждом сохранении для диагностики. Тесты
112 passed, сервис перезапущен.

## 2026-11-07 — Настройки: починен JS сохранения (итерация 2)

**Self-review:** пользователь: «UI всё равно показывает дефолт». Лог сервера
пуст — POST не отправлялся. Причина: накопленные правки сломали JS —
`if (bad)` стоял до `let bad` (TDZ ReferenceError, save() падал сразу), в
addRow была задвоенная кнопка. Блок <script> переписан целиком начисто
(rowHtml() для консистентных строк), синтаксис проверен node --check.
Тесты 112 passed, сервис перезапущен.

## 2026-11-08 — Аудит, hardening, фичи таблицы, тесты/CI, синхронизация документации

**Что:**
- Аудит (`swarm-report/review-audit-2026-11-08.md`): безопасность, надёжность,
  чистота, расхождения с PLAN.
- Hardening: `core.symlinks=false`, рекурсивная casefold-санитизация
  (AGENTS.md/CLAUDE.md/CONTEXT.md, `.opencode`, `opencode.json(c)`, `.claude`,
  `.agents`), `OPENCODE_DISABLE_PROJECT_CONFIG`/`OPENCODE_DISABLE_CLAUDE_CODE`,
  allowlist глобальных read-only агентов, ревью ровно `head_sha` +
  `refs/mrwatch/base`, `gitlab.url` только https без userinfo, `safe_url`,
  проверка прав конфига и запись 0600, kill process group при отмене, лимиты
  вывода, `next`-ссылки пагинации только на свой origin.
- Надёжность: per-project курсоры и `/projects/:id/merge_requests`, todo
  больше не помечаются в GitLab (локальный `todo_seen`, PAT read-only),
  рабочий ежесуточный бэкап БД, супервизия задач и `/healthz` 503 +
  `dead_tasks`, ротация review-логов (`log_ttl_days`), автоповтор при
  `reason=length`, per-project `model`/`variant`.
- Таблица: роли (ревьюер/упомянули/назначен/мой MR), draft, вердикт, свежесть
  активности, ссылка на комментарий упоминания, фильтры, автообновление через
  `/api/mrs`, favicon.
- Чистка: мёртвый код и дубли (маскирование, kill group, пагинация), сбор
  правил проекта в промпт удалён (файлы правил вырезаются санитизацией).
- Тесты/CI: dev-группа (pytest, pytest-asyncio, pytest-cov, ruff), порог
  покрытия 88%, GitHub Actions (`ruff check src tests`, `pytest --cov`).
- Документация: README, PLAN, `config.example.yaml` приведены к коду;
  `install.sh` выставляет 0600 на созданный конфиг.

## 2026-11-08 — Код к находкам, рендер ревью, формат агентов, таблица

**Что:**
- Код к находкам: после ответа модели, до удаления клона, сервис вырезает код
  по `путь:строка` (hunk диффа ±6 строк, иначе ±4 строки файла); чтение только
  внутри клона, `O_NOFOLLOW`, без `.git` в любом регистре и по inode, лимиты
  размера. Хранится в `review.snippets_json` (схема v9).
- Страница ревью: код находки сразу после её строки, несопоставленные — внизу,
  кнопка «Развернуть весь код»; fenced-блоки модели — как код с подписью
  языка, inline code, пропорциональный шрифт прозы, markdown-заголовки.
- Агенты (`mr-watch-reviewer`, `lmwork-reviewer`, `platformeco-reviewer`, вне
  репо): ссылка `путь:строка` после маркера уровня, код не цитировать, блок
  исправления ≤ 5 строк только для BLOCKER/MAJOR (≤ 3 на ревью), явные
  критерии уровней, BLOCKER или MAJOR → `REQUEST_CHANGES`.
- Счётчики уровней и вердикт разбираются одним парсером с общими правилами
  fence/строк; `is_fence_close` сделана публичной.
- Re-review берёт текущего агента проекта.
- Таблица: сортировка по последней активности в GitLab, автообновление 5 с /
  30 с, пауза в скрытой вкладке, быстрые обновления после «Проверить сейчас»;
  роль автора — «мой».

## 2026-11-09 — Отмена устаревшего ревью при новом коммите в MR

**Что:**
- Новый терминальный статус `cancelled` (схема v10: таблица `review`
  пересоздана с новым CHECK, данные, UNIQUE и индексы сохранены).
- Poller при смене head MR (todo, фулскан, sweep): активное ревью по старому
  head (`cloning`/`reviewing`) переводится в `cancelled`, его задача в worker'е
  отменяется (процесс opencode/git убивается, клон удаляется), на новый head
  ставится ревью `push` — всегда, независимо от `re_review_on_push`, только для
  открытого MR. `queued` переносится на новый head (при конфликте — `cancelled`);
  `done`/`error` не трогаются. Дубля `new` + `push` на один head не создаётся.
- Запись результата (`finish_review`, `mark_reviewing`, `requeue_review`,
  `rebase_review_head`) условная — запоздавшая запись не затирает `cancelled`.
- Worker'ы: общий реестр задач ревью; отмена одного ревью не останавливает
  worker, остановка сервиса — останавливает.
- UI: бейдж `cancelled`, фильтр по статусу, на странице ревью — блок «Ревью
  отменено» в нейтральном стиле и Re-review для отменённых.

## 2026-11-09 — Драфты не ревьюятся, бейдж на вкладке

**Что:**
- Poller не ставит ревью для MR в draft ни на одном пути (todo, фулскан,
  sweep, `re_review_on_push`, новое ревью после отмены устаревшего). Draft
  определяется по `draft`/`work_in_progress`; отсутствие поля — не draft.
- Переход draft → Ready (`upsert_mr` возвращает `became_ready`): для открытого
  MR, связанного со мной (ревьюер/упомянули/мой) или с историей ревью, на текущий
  head ставится ревью `new`, если ревью на этот head ещё нет. Отдельный trigger
  не заведён: `trigger` ограничен CHECK (`new`/`push`/`manual`).
- Переход Ready → draft (`became_draft`): активные ревью
  (`queued`/`cloning`/`reviewing`) → `cancelled` с ошибкой «отменено: MR
  переведён в draft» (`cancel_draft_reviews`), задачи worker'ов отменяются.
- Новый коммит в draft отменяет устаревшее ревью, но нового не ставит. Ручной
  Re-review для draft не ограничен.
- UI: убран фильтр «скрыть draft» (пометка draft в таблице осталась). В скрытой
  вкладке `/api/mrs` опрашивается раз в 60 с; ревью, завершившиеся
  (`done`/`error`, не `cancelled`) за это время, считаются по одному разу и
  показываются как `(N)` в заголовке вкладки и красная точка на favicon (canvas;
  без canvas — только заголовок). Сброс — при возврате на вкладку.

## 2026-11-09 — Вопросы ревьюеру и доп. инструкции к Re-review

**Что:**
- Схема v11: таблица `review_question` (вопрос, ответ, статус
  `queued`/`running`/`done`/`error`/`cancelled`, `root_session_id` — снимок
  сессии ревью, `parent_session_id`, `session_id` ответа) без FK на `review`
  (пересоздание `review` в будущих миграциях не упирается в `foreign_keys=ON`);
  колонка `review.extra_instructions`.
- Вопрос создаётся одним `INSERT … SELECT` только для `done`-ревью с сессией,
  без активного вопроса и при лимите < 50; иначе 409 с причиной.
- `QuestionWorker` (одна корутина, свой реестр задач и event пробуждения):
  клон `head_sha` ревью в каталог ревью, проверка агента, `opencode run
  --session <id>` (первый вопрос — `--fork` от сессии ревью, далее — цепочка
  от последнего ответа), id сессии проверяется по `^ses_[A-Za-z0-9]{1,64}$`,
  текст вопроса — только внутри серверного шаблона промпта. Клон удаляется
  всегда, в т.ч. при отмене. При рестарте `running` → `queued`.
- Re-review принимает `{"instructions": ...}` (≤ 2000 символов, NFC, без
  управляющих символов); инструкции — отдельная секция промпта до данных MR,
  переживают перенос на новый head. Re-review при активном вопросе — 409.
- API: `POST/GET /reviews/{id}/questions` (JSON / HTML-фрагмент ленты),
  `/api/reviews/{id}` отдаёт `questions: [{id, status}]`.
- UI: секция «Вопросы ревьюеру» (лента, форма с счётчиком, Ctrl/Cmd+Enter,
  поллинг 4 с пока есть активные), ответ рендерится тем же макросом, что и
  ревью (`_macros.html`); «Доп. инструкции» у Re-review и в мета ревью.
- `review.question_timeout_sec` (по умолчанию 600); логи `question-*.log`
  чистятся по `log_ttl_days`.

## 2026-11-09 — Сверка head перед запуском ревью и debounce push

**Что:**
- Worker после `get_project_mr` и до клона сверяет `head_sha` ревью с `sha`
  MR. При расхождении: MR не открыт → `cancelled` «отменено: MR закрыт»; MR в
  draft → `cancelled` «отменено: MR переведён в draft»; иначе ревью
  перебазируется на новый head и возвращается в `queued` с `attempt = 0` и
  отсрочкой `push_debounce_sec` (`rebase_and_defer_review`, `extra_instructions`
  сохраняются), клон в этом проходе не делается. Конфликт уникальности на новом
  head → `cancelled` «отменено: в MR новый коммит <sha12>». Закрывает ревью по
  устаревшему коммиту после рестарта (`requeue_incomplete`) и потерю смены head
  при падении между `upsert_mr` и `supersede_reviews`. При совпадении sha
  поведение прежнее (ручной Re-review драфта работает).
- Смена head, обнаруженная при клоне (`_handle_head_mismatch`), при конфликте
  теперь даёт `cancelled` вместо `error`.
- `review.push_debounce_sec` (по умолчанию 120, `ge=0`, 0 — выключено; поле в
  /settings): ревью `push` (после отмены активного и по `re_review_on_push`)
  создаётся с `not_before = now + debounce`; `supersede_reviews` при переносе
  `queued`-ревью на новый head ставит ту же отсрочку, так что каждый push
  отодвигает старт. Активные ревью по старому head отменяются сразу; `new` и
  ручной Re-review (`retry_review` обнуляет `not_before`) не откладываются.
- `mr.head_sha` worker не обновляет: иначе poller не увидит смену head и не
  перенесёт/отменит другие ревью этого MR.
- `parse_draft` перенесён в `gitlab_client` (импорт в worker без цикла
  poller ↔ worker), из `poller` по-прежнему импортируется.

## 2026-11-10 — Таймшит

**Что:**
- Вкладка «Таймшит»: `GET /timesheet`, `GET /api/timesheet?week=YYYY-Www`,
  `PUT /api/timesheet/cell`, `POST /api/timesheet/tasks`,
  `DELETE /api/timesheet/cells`, `POST /api/timesheet/submit`. Предложения по
  списанию времени в задачи Yandex Tracker на неделю из GitLab-событий и моих
  задач «в работе», запись worklog в Tracker без дублей (журнал
  `worklog_submission`, блокировка, сверка `unknown` после таймаута).
- `tracker:` в конфиге (по умолчанию выключен, через `/settings` не меняется),
  токен — `token_command` (Keychain), `run_secret_command` для PAT и токена.
- Производственный календарь РФ на 2026 год (`calendar/ru/2026.json`),
  `workcalendar.py`; для лет без файла — Пн–Пт и `calendar_ok=false`.
- `tracker_client.py` (OAuth + `X-Org-Id`/`X-Cloud-Org-Id`, без редиректов,
  ключ задачи в пути только после проверки, ретраи только на чтение),
  `gitlab_client.get_my_events`.
- Миграция БД v13: `timesheet_cell`, `timesheet_task`, `worklog_submission`.
- Hardening: все не-GET/HEAD/OPTIONS запросы без `X-Requested-With: lpdev` —
  403 (кроме прокси радара `/gl/`); маскирование `OAuth <token>` и `y0_…`.
- Источники ключа задачи для MR: заголовок → ветка → ссылка на MR в
  комментариях задачи Tracker (бот `ya360-pr-check` или человек) → `Closes KEY`
  / `(KEY)` в описании. Push без ключа связывается с MR своей ветки.
- Учитывается только своя работа: события на чужих MR, push в интеграционные
  ветки (`tracker.ignored_push_branches`), merge-коммиты и удаления веток не
  считаются. «Без задачи» — одна строка на ветку.
- Предложение по задаче = её доля нормы дня (по весам активности) минус уже
  списанное на неё; перебор на одной задаче вычитается из долей остальных.
- Строка задачи: текущий статус из Tracker, интервалы статусов
  (`tracker.activity_statuses`, «09.11d, 09.11–r»), задачи «в работе» по
  `tracker.in_progress_statuses`.
- Списание одной ячейки из диалога (✓ — предложение, + — вручную) с
  предупреждением, если в этот день задача не была в разработке/ревью;
  индикатор загрузки недели.
- Миграция БД v14: `worklog_submission.reconcile_attempts`; сверка при чтении
  ограничена 10 с, задачи с 403/404 переводятся в `failed`.
- Загрузка недели параллельно (не больше 6 запросов к GitLab/Tracker), ссылки
  из комментариев кэшируются по `updatedAt` задачи: холодная загрузка 27–37 с →
  5–9 с.

**Self-review:** консилиум (architect/security/ui) до реализации, три круга
ревью отдельным агентом (NEEDS-FIX → NEEDS-FIX → READY), проверка Tracker API
на чтение до включения записи.

**Правки по находкам:**
- [BLOCKER] Повторная отправка после сверки, нашедшей worklog, давала дубль —
  подтверждённые сверкой ячейки получают `skipped` без окна по времени.
- [BLOCKER] `createdBy` в `/worklog/_search` принимает только `trackerUid`, не
  логин (422 на живом API) — исправлено.
- [MAJOR] Сбой чтения при сверке не блокировал POST; `pending`/`unknown`
  блокировали ячейку навсегда; диалог списания брал несохранённые значения;
  поиск «моих задач» тянул 531 задачу; пагинация worklog/комментариев — всё
  исправлено с тестами.

## 2026-11-10 — Таймшит: «моя задача по дням», роль QA, режим только таймшита

**Что:**
- Задачи «в работе» считаются по дням, когда задача была **моей** (статус из
  `in_progress_statuses` и исполнитель «я», по changelog статуса и
  исполнителя), а не по текущему состоянию на всю неделю. Кандидаты ищутся с
  `changed(from: ...)`, поэтому задачи, ушедшие в тестирование или релиз за
  неделю, остаются в днях разработки; вернувшиеся в доработку — снова мои
  только с дня возврата. Проход через статус меньше минуты не считается.
- `rework` («Доработка») добавлен в статусы разработчика по умолчанию (`w`).
- `tracker.role: qa`: статус `testing` (`t`), день тестирования — вес 3 без
  лимита 25%, мои переходы статуса 2 и мои комментарии 1.
- `lpdev timesheet`: только страница таймшита на 127.0.0.1, без поллера,
  радара, авторевью и LaunchAgent; `projects:` в конфиге не обязателен.
- Предупреждение в диалоге ячейки — «задача не была у вас в работе».

**Проверка:** pytest, `node --test`, ruff; на копии БД и с записью,
выключенной в конфиге: две недели в роли разработчика, неделя в роли QA,
история двух реальных задач тестировщика (интервалы совпали с changelog).

## 2026-11-10 — Готовая сборка таймшита для QA

**Что:**
- `deploy/qa/build.sh`: PyInstaller-бинарь (one-dir, arm64, ad-hoc codesign,
  `pyinstaller==6.22.3`), zip `dist/lpdev-qa-arm64-<commit>.zip` с бинарём,
  `Lemanapro Timesheet.command` (двойной клик) и `INSTALL.md`; сборка только
  из чистого дерева, печатает commit + sha256 для публикации.
- org_id в мастере по умолчанию `7867633` (наша организация); в INSTALL.md
  описано получение OAuth-токена: ClientId на `oauth.yandex.ru/client/new/api`
  → `oauth.yandex.ru/authorize?response_type=token&client_id=<ClientId>`
  (прежняя страница документации не работает).
- `src/lpdev/qa_setup.py` — мастер первого запуска: GitLab URL (дефолт
  `gitlab.lmru.tech`), PAT `read_api` (проверка `GET /user`), org_id, очереди,
  «Списывать?», OAuth-токен Трекера (проверка `/myself`), порт. Токены — в
  Keychain (`service lpdev-qa`), в конфиг пишутся только `*_command`; конфиг
  `~/.config/lpdev-qa/config.yaml` (0600, role qa), данные `~/lemanapro-timesheet/`.
- `src/lpdev/qa_main.py` (entry `lpdev-qa`) — повторный запуск гасит прошлый
  инстанс по pidfile (pid + время старта процесса, SIGTERM → SIGKILL через 10 с),
  открывает вкладку браузера; Ctrl+C — остановка сервиса и закрытие вкладки
  (osascript, только для уже запущенных Chrome/Safari, best-effort);
  `--setup` — мастер заново, `--port` — порт.
- Тесты `tests/test_qa_setup.py`, `tests/test_qa_main.py` (36), README и
  INSTALL.md.

**Self-review:** ревью-агент, вердикт APPROVE_WITH_NITS — 0 BLOCKER/MAJOR,
8 MINOR, 5 NIT. Смоук бинаря: старт, `/healthz`, страница таймшита, повторный
запуск гасит старый инстанс, SIGTERM — чистое завершение.

**Правки по находкам:**
- [MINOR] AppleScript закрывал вкладку, поднимая не запущенный браузер —
  обёрнуто в `if application ... is running then`
- [MINOR] pidfile переживал перезагрузку, substring-эвристика по comm могла
  убить чужой python — в pidfile пишется `ps -o lstart=`, kill только при
  точном совпадении; пустой/битый pidfile убивать не может
- [MINOR] ошибка верификации токена прерывала мастер — PAT/токен переспрашиваются
  в цикле, Keychain пишется только после успешной проверки
- [MINOR] битый конфиг после мастера давал traceback — ConfigError перехвачен
  с подсказкой `--setup`; Ctrl+C до старта uvicorn больше не traceback
  (exit 130)
- [BLOCKER] на русской локали macOS `ps -o lstart=` возвращал кириллицу и
  запись pidfile падала с UnicodeEncodeError (бинарь не стартовал) — ps
  вызывается с `LC_ALL=C`, не-ASCII символы заменяются (безопасный исход:
  инстанс не убивается); в INSTALL.md добавлен отзыв доступа
  (`id.yandex.ru/personal/data-access`)
- [MINOR] версия pyinstaller зафиксирована (воспроизводимость sha256);
  INSTALL.md — обновление заменой папки, не «поверх»
- [NIT] `--port` валидируется (`validate_port`), atexit-закрытие вкладок
  регистрируется только при открытом браузере, pidfile пишется сразу с 0600
- Не исправлено (осознанно): секрет в argv `security -w` (нет скриптуемого
  способа у CLI, компромисс задокументирован в INSTALL.md); TOCTOU между
  проверкой порта и bind — uvicorn корректно выходит с кодом 1.

## 2026-11-10 — On-demand: нулевой фоновый трафик GitLab

**Что:**
- Убран весь периодический фон: `Poller.run()/wake()` и `RadarScheduler.run()/wake()/notify_mr_updated()` удалены; lifespan не создаёт фоновых задач опроса. `gitlab.poll_interval_sec` и `radar.interval_minutes`/`fast_refresh_while_ci` остаются в конфиге для совместимости, но игнорируются.
- Таблица MR и радар обновляются on-demand: при открытии главной страницы (GET /) и по кнопке «Проверить сейчас» (POST /force-check, без кулдауна, с перечитыванием конфига). Дедупликация между вкладками: poller — 60 с, радар — 120 с (single-flight, force обходит гэп).
- Авто-ревью убрано: новые PR, пуши и упоминания больше не создают ревью; очередь — только кнопка на карточке MR. Supersede сохранён: при новом head активное/queued-ревью отменяется и (если не draft) переоткрывается как push-ревью с debounce. `re_review_on_push` — историческое поле, игнорируется.
- Групповой sweep: если все отслеживаемые проекты имеют общую родительскую группу — один запрос `GET /groups/:id/merge_requests` вместо N пер-проектных; при ошибке группы — fallback на пер-проектный опрос (для одного проекта — как раньше).
- Радар: снапшот в БД + on-demand обновление; окно fast-refresh при CI убрано.
- Настройки: поля интервалов и «ревью на push» удалены из UI; README и config.example.yaml переписаны под on-demand.
- Сетевой слой не тронут: 429/Retry-After, ретраи, семафор, кап пагинации.

**Объём GitLab-запросов** (по правилу экономии): триггеры — только действия пользователя: открытие страницы (дедуп 60/120 с), кнопка, ревью (1 запрос get_project_mr). Фоновый трафик 0 req/день (было: ~8.6k/день poller при 5 проектах + радар до десятков тысяч при CI).

**Проверка:** pytest 1330 passed, ruff, `node --test`.
