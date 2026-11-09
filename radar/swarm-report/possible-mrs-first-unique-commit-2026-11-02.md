# Фикс: ветки с бот-коммитом на tip пропадали из «Возможных MR»

Дата: 2026-11-02. Ветка: main. Коммит: 818c3d7 (запушен).

## Симптом
Ветка `task/multi-scanner-mode` (lmru-mobile_lmwork) не показывалась в секции «Возможные MR»: tip — коммит JenkinsBot «Bump common versionCode» (CI пушит бампы после сборок), а старая эвристика требовала, чтобы последний коммит был authored/committed пользователем.

## Решение
Семантика «первый уникальный коммит — мой»: ветка моя, если автор старейшего коммита, отсутствующего в default-ветке (`GET /projects/:id/repository/compare?from=<default>&to=<branch>&straight=false`), — пользователь. Совпадение tip остаётся fast path без compare. Компромисс (принят): стековые ветки, начатые коллегой, скрываются.

## Изменения
- `src/gitlab.js` — `fetchFirstUniqueCommit` (compare, oldest commit или null).
- `src/dashboard.js` — `collectPossibleMrs` async (+client), `myIdentitiesOf`/`isMineCommit`, `firstUniqueCommitOf` с кэшем `${projectId}:${branch}:${tipSha}` (лимит 1000, null-негативы кэшируются, ошибки — нет; 401/429 пробрасываются, остальное — глотается).
- `combinePossibleMrs`/`fetchRaw` — await по цепочке; README (2 места) обновлены.

## Ревью (агент, swarm-report/code-review-possible-mrs-2026-11-02.md) → фиксы
- MAJOR: проглоченная ошибка compare кэшировалась и прятала ветку навсегда → теперь при ошибке null без записи в кэш.
- MINOR: сетевые не-GitLabError роняли обновление → тоже глотаются; README уточнён.
- Не делали (осознанно): параллелизация по проектам (ограничение нагрузки 6 одновременных compare), NIT'ы (updatedAt от tip, LRU, ключ без default branch).

## Валидация
`npm test` — 156 pass / 0 fail. Новые тесты: атрибуция по bot-tip (+контракт params), чужой origin, 0 уникальных, ошибка compare без кэша (повторный вызов → success), сетевая ошибка, 401 пробрасывается, кэш по tip sha.
