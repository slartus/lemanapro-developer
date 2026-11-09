# Фича: статус существующего MR у «возможных MR»

Дата: 2026-11-02. Коммиты: 1ca3139 (+ 7b282ac — фикс порядка compare). Запушено в main.

## Сценарий
Ветка SUPERAPP-10010-migrate-artifactory-to-jf-cli имеет закрытый MR !4945 («ПОКА НЕ СМОТРЕТЬ»), но секция показывала тег «создать MR» и вела на форму создания.

## Решение
- `src/dashboard.js` — для каждой показанной ветки: `GET /projects/:id/merge_requests?source_branch=<ветка>&state=all`, из closed/merged берётся MR с max iid → поля `mrState/mrIid/mrTitle/mrUrl` (только при находке). Ошибки (включая сетевые, кроме 401/429) → без полей, тег «создать MR».
- `src/popup.js` — строка ведёт на `mrUrl` (fallback item.webUrl → baseUrl через isUnderBaseUrl); теги: merged → «смержена» (title «!iid название»), closed → «MR закрыт» (title), иначе «создать MR»; branch.merged показывается только без mrState. `tag()` получила attrs.
- README, тесты (+5: closed/merged/newest+opened-ignored/failure/401, params-ассерт), `scripts/debug-possible-mrs.mjs` закоммичен (oldest = commits[0] по факту хронологического порядка compare API).

## Ревью (агент): OK
Закрыты миноры: tooltip у merged-тега, глотание сетевых ошибок, тест 401-проброса, params-ассерт. NIT: webUrl fallback-цепочка — сделана.

## Валидация
`npm test` — зелёные. Живая проверка: SUPERAPP-10010-… → mrState closed, !4945; запросов +1 на показанную ветку.
