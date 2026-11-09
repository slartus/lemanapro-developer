# GitLab MR Radar — что добавить (консилиум, 2026-10-26)

Профиль: Research. Роли: architect, security, frontend, ui, devops, api (general-purpose вместо отсутствующего api-designer). Код не менялся.

## Подтверждено по коду
- `src/popup.js:108` — внешние CI-ссылки пропускают `http:` на любой хост (target_url задаёт любой Developer) → оставить только https.
- `src/background.js:180` — любое изменение `settings` откладывает refresh → пользовательское состояние (snooze/seen) хранить в отдельном ключе.
- Нет `scope=assigned_to_me`, нет тестов, нет CI.

## Консенсус
- Уведомления (сгруппированные, без первого прогона, содержимое скрыто по умолчанию) + snooze («до изменения MR» и по таймеру).
- Убрать N+1 approvals/discussions: GraphQL-счётчики + кеш по updatedAt + полный refresh раз в N циклов. ETag не рассчитывать.
- Предупреждение об истечении токена (`/personal_access_tokens/self`, 16.x+, 404 → пропуск).
- Красный default branch, issue todos, причина блокировки merge, возраст MR цветом.
- Аналитика — отдельная страница, только локально.

## Разногласия
- Порядок: architect — сначала разбить `collectDashboard` на конвейер + `deriveView` + тесты, иначе уведомления/snooze дублируются в popup и background. Принято.
- Code Owners: api — дорого (3 пути, glob, /diffs), отложить; security — свой матчер без RegExp. Отложено.
- MR dependencies: API-поле Premium; парсинг описания возможен, низкий приоритет.
- Фреймворк: не вводить; модули + store, Preact+htm (вендорно) только при росте.

## Приоритет
0. Конвейер ядра, изоляция ошибок, тесты, CI (GitHub Actions) + фикс http-ссылок.
1. userState + diff → уведомления, snooze.
2. GraphQL + кеш деталей, assigned_to_me.
3. Срок токена, issue todos, красный default branch, причина блокировки, возраст.
4. Chrome Web Store (unlisted) вместо Load unpacked.
5. Security-радар: аудит своих токенов, deploy keys, незащищённая default-ветка, новые Maintainer.
6. Мульти-инстанс (глобальный ключ instanceId:project!iid, токен привязан к origin).
7. Серверный дайджест в homelab (общее ядро, Group Access Token Reporter/read_api, поллинг).
Отложено: Code Owners, content script (требует TRUSTED_CONTEXTS для storage), Firefox, retry джоб (нужен scope api — не делать).

## Непроверенное
- Поля GraphQL и версии GitLab — по памяти агента, сверить introspection на целевом инстансе.
- Меняет ли approve `updated_at`.
- Доступен ли `/projects/:id/variables` с read_api.
