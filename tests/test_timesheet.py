from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo

import pytest

from lpdev.timesheet import (
    Activity,
    WeekInputs,
    allocate,
    bucket_events,
    build_week,
    event_keys,
    event_weight,
    extract_issue_keys,
    format_week,
    strip_issue_key_prefix,
    parse_week,
    submit_window,
    week_start_of,
)

MSK = ZoneInfo("Europe/Moscow")
MR_LOOKUP = {(10, 5): {"source_branch": "feature/SUPERAPP-77-login", "title": "Login", "web_url": "https://gl/mr/5"}}


@pytest.mark.parametrize(
    ("text", "queues", "keys"),
    [
        ("feature/SUPERAPP-10350-fix", ["SUPERAPP"], ["SUPERAPP-10350"]),
        ("feature/superapp-10350_fix", ["SUPERAPP"], ["SUPERAPP-10350"]),
        ("fix_SUPERAPP-1 and SUPERAPP-2, SUPERAPP-1", ["SUPERAPP"], ["SUPERAPP-1", "SUPERAPP-2"]),
        ("SUPERAPP-007", ["SUPERAPP"], ["SUPERAPP-7"]),
        ("OTHER-1 SUPERAPP-2", ["SUPERAPP"], ["SUPERAPP-2"]),
        ("XSUPERAPP-1", ["SUPERAPP"], []),
        ("SUPERAPP-1 OTHER-2 utf-8", [], ["SUPERAPP-1", "OTHER-2"]),
        ("lower-1", [], []),
        ("", ["SUPERAPP"], []),
        (None, [], []),
    ],
)
def test_extract_issue_keys(text, queues, keys):
    assert extract_issue_keys(text, queues) == keys


def _push(ref, title="wip", count=1, at="2026-11-09T10:00:00Z", action="pushed"):
    return {
        "action_name": "pushed to", "created_at": at, "project_id": 10,
        "push_data": {"ref": ref, "commit_title": title, "commit_count": count, "action": action},
    }


def _mr_event(action, title="Some MR", iid=5, at="2026-11-09T10:00:00Z"):
    return {"action_name": action, "target_type": "MergeRequest", "target_iid": iid, "target_title": title,
            "project_id": 10, "created_at": at}


def _note(title="Some MR", iid=5, at="2026-11-09T10:00:00Z", noteable="MergeRequest"):
    return {"action_name": "commented on", "target_type": "DiffNote", "target_title": title, "project_id": 10,
            "created_at": at, "note": {"noteable_type": noteable, "noteable_iid": iid}}


@pytest.mark.parametrize(
    ("event", "keys"),
    [
        (_push("feature/SUPERAPP-1-x", "SUPERAPP-2 fix"), ["SUPERAPP-1"]),
        (_push("main", "SUPERAPP-2 fix"), ["SUPERAPP-2"]),
        (_push("main", "fix"), []),
        (_mr_event("opened", "SUPERAPP-3: title"), ["SUPERAPP-3"]),
        (_mr_event("accepted", "no key"), ["SUPERAPP-77"]),
        (_mr_event("approved", "no key", iid=6), []),
        (_note("no key"), ["SUPERAPP-77"]),
        (_note("no key", noteable="Issue"), []),
        ({**_mr_event("opened", "no key"), "project_id": "x"}, []),
    ],
)
def test_event_keys_fallback_order(event, keys):
    assert event_keys(event, MR_LOOKUP, ["SUPERAPP"]) == keys


@pytest.mark.parametrize(
    ("event", "weight"),
    [
        (_push("x", count=1), 3),
        (_push("x", count=3), 5),
        (_push("x", count=40), 6),
        (_push("x", count=None), 3),
        (_push("x", action="removed"), 0),
        (_mr_event("opened"), 2),
        (_mr_event("accepted"), 2),
        (_mr_event("merged"), 2),
        (_mr_event("approved"), 1),
        (_mr_event("closed"), 0),
        (_note(), 1),
        ({"action_name": "created", "target_type": "Project"}, 0),
    ],
)
def test_event_weights(event, weight):
    assert event_weight(event) == weight


def test_bucket_events_by_local_day_with_unkeyed_and_sources():
    events = [
        _push("feature/SUPERAPP-1", at="2026-11-09T21:30:00Z", count=2),
        _mr_event("opened", "SUPERAPP-1 and SUPERAPP-2"),
        _mr_event("approved", "SUPERAPP-2 review"),
        _push("main", "Bump deps"),
        _push("main", "Bump deps"),
        _mr_event("approved", "Unrelated", iid=5 + 100),
        _note("OTHER-5 out of queue", iid=99),
        {"action_name": "pushed to", "created_at": "garbage", "push_data": {"ref": "SUPERAPP-9"}},
        {"action_name": "pushed to", "created_at": "2026-11-09T10:00:00", "push_data": {"ref": "SUPERAPP-9"}},
    ]
    activity = bucket_events(events, MSK, MR_LOOKUP, ["SUPERAPP"])
    assert activity.weights == {
        ("SUPERAPP-1", date(2026, 11, 10)): 4,
        ("SUPERAPP-1", date(2026, 11, 9)): 1,
        ("SUPERAPP-2", date(2026, 11, 9)): 2,
        (None, date(2026, 11, 9)): 3 + 3 + 1 + 1,
    }
    assert activity.sources == {"SUPERAPP-1": {"push", "mr"}, "SUPERAPP-2": {"mr", "review"}}
    assert activity.titles["SUPERAPP-1"] == "SUPERAPP-1 and SUPERAPP-2"
    assert activity.unkeyed == [
        {"title": "main: Bump deps", "url": None},
        {"title": "Unrelated", "url": None},
        {"title": "OTHER-5 out of queue", "url": None},
    ]


def test_unkeyed_items_are_capped():
    events = [_push("main", f"commit {n}") for n in range(80)]
    assert len(bucket_events(events, MSK, {}, ["SUPERAPP"]).unkeyed) == 50


ALLOCATE_CASES = [
    (480, {"A-1": 3, "A-2": 1}, [], {}, 30),
    (480, {"A-1": 1, "A-2": 1, "A-3": 1}, [], {}, 30),
    (480, {"A-1": 5, "B-1": 1}, ["C-1", "C-2", "C-3"], {}, 30),
    (480, {}, ["C-1", "C-2", "C-3"], {}, 30),
    (480, {"A-1": 10, "A-2": 0.1}, [], {}, 30),
    (420, {"A-1": 1, None: 1}, ["C-1"], {"A-1": 60}, 30),
    (480, {"A-1": 1}, [], {"B-1": 600}, 30),
    (465, {"A-1": 1, "A-2": 2}, [], {}, 30),
    (480, {"A-1": 1, "A-2": 1, "A-3": 1}, [], {}, 15),
    (30, {"A-1": 1, "A-2": 1, "A-3": 1}, [], {}, 30),
    (0, {"A-1": 1}, ["C-1"], {}, 30),
    (480, {}, [], {}, 30),
]


@pytest.mark.parametrize(("remaining", "weights", "in_progress", "fixed", "step"), ALLOCATE_CASES)
def test_allocate_sums_to_budget_in_whole_steps(remaining, weights, in_progress, fixed, step):
    result = allocate(remaining, weights, in_progress, fixed, step)
    budget = max(0, remaining - sum(fixed.values())) // step * step
    free = {key: value for key, value in result.items() if key not in fixed}
    has_candidates = any(w > 0 for w in weights.values()) or in_progress
    assert sum(free.values()) == (budget if has_candidates else 0)
    assert all(value % step == 0 and value > 0 for value in free.values())
    assert all(result[key] == value for key, value in fixed.items())


def test_allocate_proportional_with_largest_remainder_and_key_tie_break():
    assert allocate(480, {"A-1": 3, "A-2": 1}, [], {}, 30) == {"A-1": 360, "A-2": 120}
    assert allocate(480, {"A-2": 1, "A-1": 1, "A-3": 1}, [], {}, 30) == {"A-1": 180, "A-2": 150, "A-3": 150}


def test_allocate_caps_idle_in_progress_share():
    result = allocate(480, {"A-1": 1}, ["C-1", "C-2", "C-3"], {}, 30)
    assert sum(result[key] for key in ("C-1", "C-2", "C-3") if key in result) <= 120
    assert result["A-1"] >= 360


def test_allocate_idle_share_below_cap_is_proportional():
    assert allocate(480, {"A-1": 7.5}, ["C-1"], {}, 30) == {"A-1": 450, "C-1": 30}


def test_allocate_only_in_progress_splits_everything():
    assert allocate(480, {}, ["C-2", "C-1"], {}, 30) == {"C-1": 240, "C-2": 240}


def test_allocate_merges_tiny_shares_into_the_heaviest():
    assert allocate(480, {"A-1": 100, "A-2": 1}, [], {}, 30) == {"A-1": 480}
    assert allocate(30, {"A-1": 1, "A-2": 1, "A-3": 1}, [], {}, 30) == {"A-1": 30}


def test_allocate_respects_fixed_edits_and_rounds_down_budget():
    assert allocate(480, {"A-1": 1, "A-2": 1}, [], {"A-1": 0}, 30) == {"A-1": 0, "A-2": 480}
    assert allocate(470, {"A-1": 1}, [], {"B-1": 100}, 30) == {"B-1": 100, "A-1": 360}
    assert allocate(480, {"A-1": 1}, [], {"B-1": 600}, 30) == {"B-1": 600}


def test_allocate_unkeyed_activity_takes_its_share():
    assert allocate(480, {"A-1": 1, None: 1}, [], {}, 30) == {"A-1": 240, None: 240}


def test_week_helpers():
    assert parse_week("2026-W46") == date(2026, 11, 9)
    assert format_week(date(2026, 11, 9)) == "2026-W46"
    assert format_week(date(2026, 12, 28)) == "2026-W53"
    assert format_week(date(2027, 1, 4)) == "2027-W01"
    assert week_start_of(date(2026, 11, 15)) == date(2026, 11, 9)
    assert submit_window(date(2026, 11, 10)) == (date(2026, 11, 2), date(2026, 11, 10))
    for bad in ("2026-46", "2026-W54", "2026-W00", "26-W01", "2026-W1"):
        with pytest.raises(ValueError):
            parse_week(bad)


def _inputs(**overrides) -> WeekInputs:
    values = dict(
        week_start=date(2026, 11, 2), today=date(2026, 11, 10), day_hours=8, step=30,
        activity=Activity(), in_progress={}, manual=[], logged={}, edits={}, submissions={}, titles={},
        tracker={"enabled": True, "submit_enabled": True, "error": None}, calendar_ok=True,
    )
    values.update(overrides)
    return WeekInputs(**values)


def test_build_week_contract_shape_and_calendar():
    activity = bucket_events(
        [
            _push("feature/SUPERAPP-1", at="2026-11-02T08:00:00Z"),
            _push("main", "chore", at="2026-11-02T09:00:00Z"),
            _push("feature/SUPERAPP-1", at="2026-11-07T09:00:00Z"),
        ],
        MSK, {}, ["SUPERAPP"],
    )
    week = build_week(_inputs(
        activity=activity,
        in_progress={"SUPERAPP-5": "In progress task"},
        manual=["SUPERAPP-9"],
        logged={("SUPERAPP-1", date(2026, 11, 2)): 60, ("OTHER-1", date(2026, 11, 2)): 60},
        edits={("SUPERAPP-9", date(2026, 11, 3)): 90, ("SUPERAPP-9", date(2026, 11, 7)): 60},
        submissions={("SUPERAPP-1", date(2026, 11, 2)): "unknown"},
        titles={"SUPERAPP-1": "Real title"},
    ))
    assert (week["week"], week["prev_week"], week["next_week"], week["today"]) == ("2026-W45", "2026-W44", "2026-W46", "2026-11-10")
    assert [(d["date"], d["kind"], d["norm"], d["logged"], d["submittable"]) for d in week["days"]] == [
        ("2026-11-02", "work", 480, 120, True),
        ("2026-11-03", "short", 420, 0, True),
        ("2026-11-04", "holiday", 0, 0, False),
        ("2026-11-05", "work", 480, 0, True),
        ("2026-11-06", "work", 480, 0, True),
        ("2026-11-07", "weekend", 0, 0, False),
        ("2026-11-08", "weekend", 0, 0, False),
    ]
    rows = {row["key"]: row for row in week["rows"]}
    assert [row["key"] for row in week["rows"]] == ["SUPERAPP-1", "SUPERAPP-5", "OTHER-1", "SUPERAPP-9"]
    assert rows["SUPERAPP-1"]["title"] == "Real title"
    assert rows["SUPERAPP-1"]["url"] == "https://tracker.yandex.ru/SUPERAPP-1"
    assert rows["SUPERAPP-1"]["sources"] == ["push"]
    assert rows["SUPERAPP-5"]["sources"] == ["in_progress"]
    assert rows["SUPERAPP-9"]["sources"] == ["manual"]
    assert rows["OTHER-1"]["sources"] == []
    assert len(rows["SUPERAPP-1"]["cells"]) == 7
    monday = "2026-11-02"
    assert rows["SUPERAPP-1"]["cells"][monday] == {"suggested": 180, "edited": None, "logged": 60, "submission": "unknown"}
    assert rows["SUPERAPP-5"]["cells"][monday]["suggested"] == 0
    assert week["unkeyed"]["cells"][monday] == {"suggested": 180}
    assert week["unkeyed"]["items"] == [{"title": "main: chore", "url": None}]
    tuesday = "2026-11-03"
    assert rows["SUPERAPP-9"]["cells"][tuesday] == {"suggested": 90, "edited": 90, "logged": 0, "submission": None}
    assert rows["SUPERAPP-5"]["cells"][tuesday]["suggested"] == 330
    assert rows["SUPERAPP-5"]["cells"]["2026-11-04"]["suggested"] == 0
    saturday = "2026-11-07"
    assert rows["SUPERAPP-1"]["cells"][saturday]["suggested"] == 0
    assert rows["SUPERAPP-9"]["cells"][saturday]["edited"] == 60
    for day in week["days"]:
        total = day["logged"] + sum(row["cells"][day["date"]]["suggested"] for row in week["rows"])
        total += week["unkeyed"]["cells"][day["date"]]["suggested"]
        assert total == max(day["norm"], day["logged"]) or day["norm"] == 0


def test_build_week_without_any_data():
    week = build_week(_inputs(calendar_ok=False, tracker={"enabled": False, "submit_enabled": False, "error": None}))
    assert week["rows"] == []
    assert week["unkeyed"] is None
    assert week["calendar_ok"] is False
    assert week["tracker"]["enabled"] is False


def test_build_week_overlogged_day_suggests_nothing():
    week = build_week(_inputs(
        in_progress={"SUPERAPP-5": ""},
        logged={("SUPERAPP-1", date(2026, 11, 2)): 600},
    ))
    rows = {row["key"]: row for row in week["rows"]}
    assert rows["SUPERAPP-5"]["cells"]["2026-11-02"]["suggested"] == 0


def test_build_week_old_weeks_are_not_submittable():
    week = build_week(_inputs(week_start=date(2026, 10, 26)))
    assert not any(day["submittable"] for day in week["days"])
    current = build_week(_inputs(week_start=date(2026, 11, 9)))
    assert [day["submittable"] for day in current["days"]] == [True, True, False, False, False, False, False]


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("SUPERAPP-10264: [an][ios] Диз-демо", "[an][ios] Диз-демо"),
        ("superapp-10264 — Диз-демо", "Диз-демо"),
        ("SUPERAPP-10264 - fix", "fix"),
        ("  SUPERAPP-10264:fix", "fix"),
        ("SUPERAPP-102645: other", "SUPERAPP-102645: other"),
        ("Fix SUPERAPP-10264", "Fix SUPERAPP-10264"),
        ("SUPERAPP-10264", "SUPERAPP-10264"),
        ("SUPERAPP-10264: ", "SUPERAPP-10264: "),
    ],
)
def test_strip_issue_key_prefix(title, expected):
    assert strip_issue_key_prefix(title, "SUPERAPP-10264") == expected


def test_build_week_strips_key_from_titles_and_exposes_step():
    activity = bucket_events([_mr_event("opened", "SUPERAPP-77: Login", at="2026-11-02T08:00:00Z")], MSK, {}, ["SUPERAPP"])
    week = build_week(_inputs(activity=activity))
    assert week["rows"][0]["title"] == "Login"
    assert week["step"] == 30


def test_build_week_does_not_suggest_into_the_future():
    week = build_week(_inputs(week_start=date(2026, 11, 9), in_progress={"SUPERAPP-5": ""}))
    cells = week["rows"][0]["cells"]
    assert cells["2026-11-10"]["suggested"] == 480
    assert all(cells[day]["suggested"] == 0 for day in ("2026-11-11", "2026-11-12", "2026-11-13"))
