#!/bin/zsh
# Двойной клик — запуск таймшита для QA. Окно Terminal держится открытым, Ctrl+C останавливает сервис.
cd "$(dirname "$0")"
exec ./lpdev-qa/lpdev-qa "$@"
