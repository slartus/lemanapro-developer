# Сверка коммита ревью с head MR и debounce при push — 2026-11-09

Ветка: `feat/head-check-and-push-debounce`

## Задача
- Перед запуском ревью сверять его коммит с текущим head MR. Закрывает дыры: ревью после рестарта (`requeue_incomplete`) шло по устаревшему коммиту; падение между `upsert_mr` и `supersede_reviews` теряло смену head.
- Debounce при частых push: серия push'ей не должна порождать шторм клонов и отмен.

## Решения
- `Worker._reconcile_head` — после `get_project_mr`, до клона, только при несовпадении sha:
  - MR закрыт → `cancelled` «отменено: MR закрыт»;
  - MR в draft → `cancelled` «отменено: MR переведён в draft»;
  - иначе → `rebase_and_defer_review`: ревью переносится на новый sha с отсрочкой `push_debounce_sec`, клон в этом проходе не делается; конфликт UNIQUE → `cancelled` «отменено: в MR новый коммит <sha12>».
- `_handle_head_mismatch` (смена head при клоне): при конфликте `cancelled` вместо `error`.
- `review.push_debounce_sec` (по умолчанию 120, 0 — выключено; есть в `/settings`):
  - ревью с trigger `push` (после supersede и по `re_review_on_push`) получают `not_before = now + debounce`;
  - перенос queued-ревью на новый head в `supersede_reviews` сдвигает `not_before` вперёд;
  - активное ревью по старому head отменяется сразу;
  - `new` (todo/упоминание/ревьювер/draft→ready) и `manual` — без отсрочки; `retry_review` сбрасывает `not_before`.
- `mr.head_sha` worker не обновляет — иначе poller не увидит смену head и не обработает остальные ревью MR.
- `parse_draft` перенесён в `gitlab_client.py` — иначе цикл импорта poller → worker.

## Validation
- `ruff check src tests` — чисто; `pytest tests -q` — 621 passed (было 599).
- Переписан `test_worker_uses_review_head_sha_when_head_moved` (поведение намеренно изменено); три теста пути «смена head при клоне» теперь подставляют совпадающий `detail.sha`.

## Риски
- Перенос в worker обнуляет `attempt` (число переносов ограничено числом новых коммитов).
- Любой state кроме `opened` при смене head даёт `cancelled`, включая `locked`.
- В UI отложенное ревью отображается как `queued` без времени старта (`/api/mrs` не отдаёт `not_before`).

## Статус
Done.
