# Вопросы ревьюеру и доп. инструкции к Re-review — 2026-11-09

Ветка: `feat/reviewer-questions`

## Задача
Окно ввода для дополнительных вопросов ревьюеру, оба режима:
- A) диалог по готовому ревью в контексте его сессии opencode;
- B) доп. инструкции к Re-review, добавляемые в промпт.

## Research (консилиум)
- architect: отдельная таблица `review_question` и отдельный `QuestionWorker` (вопросы не ждут длинные ревью); цепочка вопросов — первый `--session <ревью> --fork`, следующие продолжают сессию предыдущего ответа; клон в тот же путь и на тот же `head_sha`, что у ревью; взаимоисключение вопроса и Re-review через БД.
- security: пользовательский текст только внутри серверного шаблона промпта; `session_id` только из БД + regex; `validate_agent` перед каждым вопросом; рендер ответа только экранирующим `layout_review`; лимиты (1 активный вопрос, 4000/2000 символов); сохранить env-изоляцию, `--pure`, harden-клон, удаление клона под shield.
- frontend: серверный HTML-фрагмент ленты + поллинг только ленты (набранный текст не теряется); инструкции — свёрнутое поле в существующем Re-review.
- Спайк (opencode 1.19.4): `--session <id> --fork` работает после пересоздания каталога по тому же пути с другим root-коммитом; в JSON sessionID — id форка.

## Реализация
- `db.py`: v11 — `review.extra_instructions`, таблица `review_question` (без FK, чтобы будущие пересоздания `review` не падали при `foreign_keys=ON`); `create_question`, `claim_next_question`, `finish_question`, `list_questions`, `has_active_question`, `question_rejection_reason`; `retry_review` с инструкциями и запретом при активном вопросе; `requeue_incomplete` для вопросов.
- `opencode_runner.py`: `--session`/`--fork`, `is_valid_session_id`, `build_question_prompt`, `run_question`, секция инструкций в `build_review_prompt`.
- `worker.py`: `QuestionWorker`; ревью-воркер передаёт инструкции в промпт.
- `main.py`: задача `mrwatch-question-worker` со своим event и реестром.
- `config.py`, `config.example.yaml`: `review.question_timeout_sec` (600).
- `git_ops.py`: очистка `question-*.log`.
- `web/routes.py`: `POST/GET /reviews/{id}/questions`, retry с `instructions`, `questions` в `/api/reviews/{id}`.
- Шаблоны: `_macros.html` (`render_rows`, `snippet_block`), `_qa.html`, секция вопросов и инструкций в `review.html`, стили textarea в `base.html`.
- README, CHANGELOG.

## Validation
- `ruff check src tests` — чисто; `pytest tests -q` — 599 passed (было 541).

## Риски
- Лимит 50 вопросов на строку ревью включает вопросы к прошлым версиям.
- Параллельные ревью и вопрос — два клона на диске (проверка свободного места есть).
- Раскрытый блок «к предыдущей версии ревью» сворачивается при обновлении ленты.
- Bidi/Cf-символы в вопросе не вырезаются (только Cc).
- Агент read-only может читать файлы вне клона — отдельная задача (`external_directory`).

## Статус
Done.
