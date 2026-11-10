from __future__ import annotations

import json
from datetime import date
from functools import lru_cache
from importlib import resources
from typing import Literal

from lpdev.config import SHORT_DAY_REDUCTION_MINUTES

DayKind = Literal["work", "short", "holiday", "weekend"]


@lru_cache(maxsize=16)
def _year(year: int) -> dict[str, frozenset[date]] | None:
    resource = resources.files("lpdev").joinpath("calendar", "ru", f"{year}.json")
    if not resource.is_file():
        return None
    raw = json.loads(resource.read_text(encoding="utf-8"))
    return {
        name: frozenset(date.fromisoformat(value) for value in raw.get(name, []))
        for name in ("holidays", "short", "workdays_on_weekend")
    }


def has_year(year: int) -> bool:
    return _year(year) is not None


def day_kind(day: date) -> DayKind:
    data = _year(day.year)
    if data is None:
        return "weekend" if day.weekday() >= 5 else "work"
    if day in data["holidays"]:
        return "holiday"
    if day in data["short"]:
        return "short"
    if day in data["workdays_on_weekend"]:
        return "work"
    return "weekend" if day.weekday() >= 5 else "work"


def day_norm_minutes(day: date, day_hours: int) -> int:
    kind = day_kind(day)
    if kind == "work":
        return day_hours * 60
    if kind == "short":
        return max(0, day_hours * 60 - SHORT_DAY_REDUCTION_MINUTES)
    return 0
