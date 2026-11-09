# radar

Логика радара MR (бывший gitlab-mr-radar): сбор и раскладка открытых MR по секциям «на ревью», «назначены», «упомянули», «ждут апрува», «треды», «мои MR», «возможные MR», «сломан default branch». Запускается бэкендом lemanapro-developer, Chrome-расширения больше нет.

- `src/gitlab.js` — клиент GitLab (REST v4 и GraphQL, ретраи на 429).
- `src/dashboard.js` — конвейер `fetchRaw` → `enrich` → `classify`.
- `src/view.js` — секции и счётчики из дашборда (`deriveView`), стабилизация статусов, проблемы токена.
- `src/snooze.js` — отложенные MR.
- `src/links.js`, `src/config.js` — ссылки и константы.
- `src/popup.js`, `src/popup.css` — UI бывшего расширения, исходник для переноса в общую таблицу MR; нигде не загружается.

Тесты: `node --test` (Node 22+).

Скриншоты в `docs/` и отчёты в `swarm-report/` — история расширения.
