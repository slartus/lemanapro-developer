# mr-watch — аудит, hardening и доработки таблицы (2026-11-08)

Профиль: refactoring (audit → plan → executing → validation → report).
Процесс: каждое изменение — отдельная ветка и агент → self-review отдельным агентом → итеративные фиксы до READY-TO-MERGE → merge --no-ff в main → push (github.com/slartus/mr-watch, private). CI (GitHub Actions, ubuntu): ruff + pytest --cov, зелёный.

## Ветки (в порядке мержа)
| Ветка | Суть | Итераций ревью |
|---|---|---|
| hardening/security-reliability | P0+P1 аудита, фикс reason=length, редизайн UI | 3 |
| feat/mr-role | роли ревьюер/упомянули/назначен/мой MR, бэкфилл | 3 |
| feat/activity-staleness | подсветка давности активности | 1 |
| feat/mr-state-tags | тег draft (закрытые/смерженные не показываются — решение пользователя) | 2 |
| feat/favicon | иконка сервиса | 1 |
| feat/review-verdict | вердикт ревью, линейный парсер, защита от подделки | 3 |
| feat/mention-link | ссылка на комментарий с упоминанием | 2 |
| fix/review-leftovers | отложенные MINOR, корректная отмена (зависание shutdown) | 3 |
| refactor/dead-code-dedup | мёртвый код, дубли, тестовый хелпер вынесен | 2 |
| test/coverage-tooling | pytest-cov 88%, ruff, CI, hardening-тесты | 2 |
| test/repo-url-token | gitlab.url только https без userinfo, тесты токена | 2 |
| docs/sync-with-code | README/PLAN/example/CHANGELOG по коду | 1 |

## Ключевые найденные и закрытые дефекты
- BLOCKER: обход санитизации клона через symlink `.opencode` → проектный агент opencode → RCE/кража PAT.
- MAJOR: AGENTS.md/CLAUDE.md/CONTEXT.md (в т.ч. вложенные, любой регистр) подгружались opencode → подделка ревью.
- MAJOR: бэкап БД не работал никогда; сироты opencode/git при остановке; зависание shutdown при отмене во время очистки клона.
- MAJOR: курсор поллера, «ядовитый» MR без sha, бесконечные 403 mark_as_done, дифф не того коммита.
- BLOCKER: O(n²) парсер вердикта (DoS); подделка вердикта через code fence.
- Ревью #125 (reason=length): модель тратила весь output (8192) на reasoning; добавлены model/variant и автоповтор.

## Итог
- Тесты: 78 → 434, покрытие 91% (порог 88), ruff чистый, CI зелёный.
- Схема БД: user_version 8 (миграции атомарные, идемпотентные).
- Вне репо: ~/.config/opencode/agent/lmwork-reviewer.md (read-only, правила LMWork).

## Не сделано / риски
- Запущенный сервис работает на старом коде до рестарта; рестарт применит миграции v4–v8 к data/mrwatch.db.
- На живом GitLab новые функции (роли, draft, ссылки упоминаний, бэкфилл) не проверены — только тестами.
- Сервисный слой из routes.py не выносился (архитектурное изменение, вне scope).
- Кэш клонов (вместо клона на ревью) — осознанно не делался.
- API-ключ gpustack лежит открытым в ~/.config/opencode/opencode.json.
