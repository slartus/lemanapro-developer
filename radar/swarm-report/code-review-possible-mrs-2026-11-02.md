# Code review: «Возможные MR» — атрибуция по первому уникальному коммиту

Дата: 2026-11-02. Дифф: working tree (README.md, src/dashboard.js, src/gitlab.js, tests/dashboard.test.js, tests/gitlab.test.js).
`npm test`: 154/154 проходят. Сверка с документацией GitLab: compare `straight=false` — сравнение через merge-base (`from…to`), `commits` «always complete» (не обрезается даже при `compare_timeout`) — выбор `commits[commits.length-1]` как старейшего уникального коммита корректен, пагинация не проблема.

## Вердикт: needs-fixes

## BLOCKER

Нет.

## MAJOR

1. **Ошибки compare кэшируются как «факт»** — src/dashboard.js:293-305 (`firstUniqueCommitOf`).
   `optional()` возвращает `null` и для легитимного пустого ответа (0 уникальных коммитов), и для проглоченной `GitLabError` (403/404/5xx) — оба кладутся в `branchOriginCache` под `projectId:branch:tipSha`. Единичный транзиентный 5xx прячет ветку из секции до смены tip. Для целевого сценария фичи (бот забампил versionCode последним, MR ещё не создан, в ветку больше не пушат) ветка исчезает на неопределённый срок при одном сбое GitLab. README заявляет «ошибка compare исключает ветку» — на одно обновление, а код запоминает её до нового коммита. Тест `task/error` (tests/dashboard.test.js:254) фиксирует только разовое исключение и не ловит кэширование.
   Фикс: не писать в кэш результат, полученный через ошибку (различать «пустой ответ» и «сбой»; на `GitLabError` возвращать null без `branchOriginCache.set`).

## MINOR

1. **Не-GitLabError ошибки сети роняют всё обновление** — src/dashboard.js:299.
   Таймаут `AbortSignal.timeout(20s)`, DNS-сбой или обрыв соединения дают не-`GitLabError`, `optional()` их пробрасывает → reject уходит через `collectPossibleMrs` → `combinePossibleMrs` → `fetchRaw` → бейдж «!» и старый дашборд с ошибкой (src/background.js:274-281). Раньше секция не делала сетевых запросов; паттерн консистентен с остальным кодом (`fetchStates`), но противоречит цели «ошибка compare не ломает секцию». Стоит в `firstUniqueCommitOf` глотать всё, кроме `GitLabError` 401/429.
2. **Последовательный обход проектов** — src/dashboard.js:337-347.
   `await collectPossibleMrs(...)` внутри `for...of`: параллелизм 6 действует только внутри проекта, между проектами — последовательно. Худший случай: N проектов с бот-типами = N последовательных раундтрипов на обновление. Кэш и fast path смягчают; можно `Promise.all` по проектам либо один общий `mapLimit` по парам (project, branch).
3. **README неточен про фатальные ошибки** — README.md:74.
   «ошибка compare исключает ветку, не ломая секцию»: 401/429 (и сетевые ошибки, см. MINOR-1) роняют всё обновление, а не только исключают ветку. Уточнить формулировку.

## NIT

1. `updatedAt` из tip-коммита даже при атрибуции по origin — src/dashboard.js:333. Каждый бот-бамп поднимает ветку наверх секции (сортировка `byUpdatedDesc`). Если задумано как «активность ветки» — сознательный выбор, иначе брать дату origin-коммита.
2. Вытеснение кэша полным `clear()` на 1000 записей — src/dashboard.js:300-302. После очистки лавина повторных compare (умеренная из-за mapLimit 6). LRU был бы плавнее; для объёмов расширения достаточно.
3. Module-level кэш без хука сброса для тестов — src/dashboard.js:290. Изоляция сейчас держится на уникальных именах веток (проверено: task/bot-tip, task/foreign-origin, task/stale-sync, task/error, task/cached:sha1/sha2 не пересекаются; my-tip ветки кэш не пишут). Новый тест с переиспользованным именем молча получит закэшированный результат — стоит экспортировать reset или чистить в beforeEach.
4. Ключ кэша не включает default branch — src/dashboard.js:294. Атрибуция зависит от `from=<default_branch>`; смена default при неизменном tip даст устаревший результат. Теоретический кейс.
5. Имя переменной `renamedTip` (tests/dashboard.test.js:275) — речь о сменившемся tip, а не о переименовании.
6. Интеграционные тесты `collectDashboard` (tests/dashboard.test.js:982-1038) покрывают только fast path (в фикстурах проектов нет `default_branch`, tips мои) — сквозная передача клиента fetchRaw→combinePossibleMrs→compare не покрыта end-to-end.

## Проверено, в порядке

- Логика атрибуции: bot-tip атрибутируется по первому уникальному; 0 уникальных → null → исключение; пустой/некорректный ответ compare (`commits` отсутствует) → null; стековые ветки (first unique чужой) — скрытие, известный компромисс. Fast path tip-мой корректен и не дергает compare (проверено тестом с calls).
- Регрессии: item shape не изменился (key `branch:<projectId>:<name>`, kind, updatedAt и пр.); единственный прод-вызов `combinePossibleMrs` обновлён (await, src/dashboard.js:419); других вызовов `collectPossibleMrs`/`combinePossibleMrs` в src нет — только тесты, все обновлены под async+client.
- Асинхронность: все await на месте, `mapLimit` корректен, unhandled rejections нет; 401/429 пробрасываются (консистентно с остальными секциями), 403/404/5xx глотаются.
- Стиль: соответствует соседнему коду, мёртвого кода и лишних комментариев нет, README-таблица и bullets точны (кроме MINOR-3).
- Тесты: новые ветки логики покрыты (bot-tip mine/foreign, 0 unique, ошибка compare, кэш + смена tip, нет default branch, контракт запроса from/to/straight), unit-тесты `fetchFirstUniqueCommit` (выбор старейшего, пустой ответ, нет commits, проброс ошибки).

## Вывод

Логика атрибуции и рефактор корректны и покрыты тестами, но кэширование проглоченных ошибок compare навсегда прячет ветку из секции (MAJOR-1) — починить перед коммитом.
