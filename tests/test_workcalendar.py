from __future__ import annotations

from datetime import date, timedelta

import pytest

from lpdev.workcalendar import day_kind, day_norm_minutes, has_year


@pytest.mark.parametrize(
    ("day", "kind", "norm"),
    [
        (date(2026, 1, 1), "holiday", 0),
        (date(2026, 1, 9), "holiday", 0),
        (date(2026, 1, 10), "weekend", 0),
        (date(2026, 1, 12), "work", 480),
        (date(2026, 2, 23), "holiday", 0),
        (date(2026, 3, 9), "holiday", 0),
        (date(2026, 4, 30), "short", 420),
        (date(2026, 5, 11), "holiday", 0),
        (date(2026, 11, 3), "short", 420),
        (date(2026, 11, 4), "holiday", 0),
        (date(2026, 11, 7), "weekend", 0),
        (date(2026, 11, 10), "work", 480),
        (date(2026, 12, 31), "holiday", 0),
    ],
)
def test_ru_2026_calendar(day, kind, norm):
    assert day_kind(day) == kind
    assert day_norm_minutes(day, 8) == norm


def test_ru_2026_totals_match_the_official_norm():
    days = [date(2026, 1, 1) + timedelta(days=offset) for offset in range(365)]
    assert sum(day_kind(day) in ("work", "short") for day in days) == 247
    assert sum(day_norm_minutes(day, 8) for day in days) == 1972 * 60


def test_unknown_year_falls_back_to_five_day_week():
    assert not has_year(2099)
    assert has_year(2026)
    assert day_kind(date(2099, 1, 1)) == "work"
    assert day_kind(date(2099, 1, 3)) == "weekend"
    assert day_norm_minutes(date(2099, 1, 1), 6) == 360
    assert day_norm_minutes(date(2099, 1, 3), 8) == 0


def test_short_day_norm_never_negative():
    assert day_norm_minutes(date(2026, 4, 30), 1) == 0


def test_workday_on_weekend(monkeypatch):
    from lpdev import workcalendar

    saturday = date(2026, 6, 6)
    data = {"holidays": frozenset(), "short": frozenset(), "workdays_on_weekend": frozenset({saturday})}
    monkeypatch.setattr(workcalendar, "_year", lambda year: data)
    assert day_kind(saturday) == "work"
    assert day_kind(date(2026, 6, 7)) == "weekend"
