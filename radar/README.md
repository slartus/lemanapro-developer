# radar

Логика радара MR (бывший gitlab-mr-radar): сбор и раскладка открытых MR по секциям «на ревью», «назначены», «упомянули», «ждут апрува», «треды», «мои MR», «возможные MR», «сломан default branch». Запускается бэкендом lemanapro-developer, Chrome-расширения больше нет.

- `src/gitlab.js` — клиент GitLab (REST v4 и GraphQL, ретраи на 429).
- `src/dashboard.js` — конвейер `fetchRaw` → `enrich` → `classify`.
- `src/view.js` — секции и счётчики из дашборда (`deriveView`), стабилизация статусов, проблемы токена.
- `src/snooze.js` — отложенные MR.
- `src/links.js` — проверка ссылок.
- `src/runner.mjs` — точка входа для бэкенда: JSON на stdin, дашборд на stdout; в GitLab ходит только через прокси бэкенда.

`view.js`, `snooze.js` и `links.js` также грузятся в браузер общей таблицей MR (`src/lpdev/web/static/board.js`).

Тесты: `node --test` (Node 22+).

Скриншоты в `docs/` и отчёты в `swarm-report/` — история расширения.
