# Драфты без ревью, удаление фильтра «скрыть draft», бейдж на вкладке — 2026-11-09

Ветка: `feat/drafts-and-tab-badge`

## Задача
- Драфты не ревьюятся автоматически.
- Убрать фильтр «скрыть draft» на главной.
- Бейдж на вкладке браузера с числом новых ревью, сброс при активации вкладки.

## Решения
- Проверка draft — в одном месте, `Poller._enqueue`: закрывает todo, full_scan, sweep (new/push) и новое ревью после supersede. `draft = None` не считается драфтом.
- `upsert_mr` возвращает `became_ready` / `became_draft` (по старому `is_draft` до UPDATE).
- draft → ready: ревью (`trigger = new`, CHECK на trigger не позволяет `ready` без миграции) для открытого MR, если MR релевантен (`mr_wants_review`: роль reviewer/author/mentioned или уже было ревью) и нет ревью на текущий head.
- ready → draft: `cancel_draft_reviews` переводит queued/cloning/reviewing в `cancelled` («отменено: MR переведён в draft»), активные отменяются через `ReviewRegistry`.
- Новый коммит в draft: активное ревью отменяется, новое не ставится.
- Ручной Re-review для draft разрешён.
- Фильтр «скрыть draft» удалён вместе с `data-draft`; пометка draft в строке осталась.
- Бейдж: скрытая вкладка опрашивает `/api/mrs` раз в 60 с; новыми считаются ревью, перешедшие в done/error после ухода вкладки в фон (каждое один раз). Заголовок `(N) …` и красная точка на favicon (canvas, без canvas — только заголовок). Сброс на `visibilitychange`.

## Изменённые файлы
`src/mrwatch/db.py`, `src/mrwatch/poller.py`, `src/mrwatch/web/templates/index.html`, `README.md`, `docs/CHANGELOG.md`, `tests/test_db.py`, `tests/test_poller.py`, `tests/test_web_main.py`.

## Validation
- `ruff check src tests` — чисто.
- `pytest tests -q` — 541 passed (было 527).
- Мутационная проверка: отключение проверки draft в `_enqueue` роняет 6 тестов, отключение отмены при `became_draft` — 1.

## Риски
- `_backfill_roles` → `update_mr_roles` обновляет `is_draft` без признака перехода: если draft → ready совпадёт с разовым бэкфиллом, ревью не поставится.
- В draft-MR queued-ревью (например, ручной Re-review) при новом коммите перебазируется, а не отменяется.

## Статус
Done.
