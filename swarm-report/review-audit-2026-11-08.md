# mr-watch — аудит (refactoring/audit) 2026-11-08

Консилиум: architect, security, diagnostics(+api/frontend), test. Тесты: 112 passed (`/tmp/opencode/mrwatch-venv`, `.venv` в проекте нет).
Проверено вручную: symlink-обход sanitize (воспроизведён), бэкап через to_thread, курсор sweep, ревью #125.

## Ревью #125 «reason=length»
Лог: input 11 796 токенов, output 0, reasoning 8192 = `limit.output` модели в `~/.config/opencode/opencode.json`.
Дифф ни при чём: модель тратит весь бюджет вывода на reasoning. Повторный прогон детерминированно падает так же.
Сообщение об ошибке вводит в заблуждение.

## P0 — безопасность
1. [BLOCKER] git_ops.py:165 sanitize_clone — `.opencode` как symlink: `is_dir()`→True, `rmtree(ignore_errors=True)` молча не удаляет ссылку → проектный агент/плагины opencode из MR → RCE, кража PAT из Keychain. Фикс: `-c core.symlinks=false` в HARDEN_ARGS, `is_symlink()`→unlink, fail-closed.
2. [MAJOR] sanitize не удаляет AGENTS.md / CLAUDE.md / .claude / .agents — автоинструкции opencode из ветки автора MR («скажи APPROVE»).
3. [MAJOR] worker.py:155 — агент не валидируется (allowlist + проверка read-only frontmatter); убрать подтягивание агентов из репо в settings.
4. [MAJOR] config.yaml/.bak 0644, `_atomic_write_yaml` сбрасывает права; `pat_command` исполняется с shell=True → целостность конфига = RCE. Фикс: 0600, проверка на старте.
5. [MINOR] git: `--end-of-options`/валидация sha, diff с `--no-ext-diff --no-textconv`; PAT по абсолютному Link `next` без проверки хоста; `href` web_url без проверки схемы; settings.html innerHTML без экранирования; .gitignore без `config.yaml.bak`, `tmp/`.

## P1 — надёжность
6. [MAJOR] main.py:97 — бэкап БД не работает никогда (`check_same_thread`), `data/backups/` нет.
7. [MAJOR] Отмена задач не убивает opencode/git (start_new_session) — сироты жгут токены, workspace удаляется из-под процесса.
8. [MAJOR] Воркер/поллер умирают молча при исключении вне try; /healthz не видит; healthz=200 при degraded.
9. [MAJOR] Дифф по `mr.head_sha` из БД, checkout по текущему head MR — ревью несогласованного кода после push.
10. [MAJOR] Поллер: курсор sweep сдвигается только по своим MR (poller.py:223) + `scope=all` по всему инстансу + кап 50 страниц → пропуск MR; `_full_scan` откатывает курсор назад.
11. [MAJOR] `head_sha=None` → IntegrityError роняет весь тик («ядовитый» MR).
12. [MAJOR] todo: mark_as_done 403 (нужен scope api, противоречит README) → бесконечные повторы каждый тик; todo не проверяет state MR.
13. [MAJOR] Hot-reload настроек не доходит до воркеров; settings_save без expanduser opencode.bin → FileNotFoundError из app.log.
14. [MINOR] retry_review не сбрасывает attempt; CLONE_RETRY_BACKOFF_SEC не используется; QueueFull после записи; author:null → AttributeError; claim не фильтрует enabled; communicate() без потокового лимита (OOM); логи ревью не ротируются; пустые каталоги workspaces; блокирующие rmtree в loop; нет индекса review(mr_id).
15. [MINOR] API: retry всегда 303; 400 вместо 502 на ошибки апстрима; body не dict → 500.

## P2 — чистота
16. Дубли: 3× upsert_mr в poller, `_kill_group`/маскирование в двух модулях. Мёртвый код: list_reviews, request_review, collect_project_instructions (+ бессмысленна после sanitize), state_changed, неиспользуемые импорты.
17. Расхождения с PLAN: путь конфига, логи в ./logs vs ~/Library/Logs, `routes.py` перегружен (YAML I/O + резолв проектов) → сервисный слой.

## Тесты (добавить)
- argv opencode: нет `--auto`; sanitize на symlink/AGENTS.md; бэкап через to_thread; отмена → процесс убит; head_sha=None; курсор при чужих MR; supervisor воркера; токен не в .git/config и тексте исключения; флейк-риск sleep(0.15) в test_poller; pytest-cov + `asyncio_default_fixture_loop_scope`.
