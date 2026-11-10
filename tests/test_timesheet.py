from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from lpdev.timesheet import (
    PASS_THROUGH,
    interval_days,
    owned_history,
    suggest_day,
    Activity,
    EventContext,
    WeekInputs,
    allocate,
    bucket_events,
    build_week,
    event_keys,
    event_weight,
    extract_issue_keys,
    PrLink,
    extract_closing_keys,
    format_week,
    mr_keys,
    parse_mr_links,
    resolve_pr_links,
    status_intervals,
    is_ignored_push,
    strip_issue_key_prefix,
    parse_week,
    submit_window,
    week_start_of,
)

MSK = ZoneInfo("Europe/Moscow")


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


ME = "slinkin"
MR_LOOKUP = {
    (10, 5): {"source_branch": "feature/SUPERAPP-77-login", "title": "Login", "web_url": "https://gl/mr/5", "author_username": ME},
    (10, 6): {"source_branch": "feature/SUPERAPP-88", "title": "SUPERAPP-88: Their work", "web_url": "https://gl/mr/6",
              "author_username": "colleague"},
    (10, 7): {"source_branch": "task/no_key", "title": "Their unkeyed work", "web_url": "https://gl/mr/7",
              "author_username": "colleague"},
}


def _context(**overrides) -> EventContext:
    by_branch = {(10, info["source_branch"]): info for (_, _), info in MR_LOOKUP.items()}
    values = dict(
        mr_by_iid=MR_LOOKUP, mr_by_branch=by_branch, me=ME,
        ignored_push_branches=("develop", "main", "master", "release/*", "rc/*"), default_branches={10: "develop"},
        queues=("SUPERAPP",),
    )
    values.update(overrides)
    return EventContext(**values)


def _push(ref, title="wip", count=1, at="2026-11-09T10:00:00Z", action="pushed", action_name="pushed to", project=10):
    return {
        "project_id": project, "action_name": action_name, "target_id": None, "target_iid": None, "target_type": None,
        "target_title": "lemanapro-mobile", "created_at": at, "author": {"username": ME},
        "push_data": {"commit_count": count, "action": action, "ref_type": "branch", "commit_from": "a" * 40,
                      "commit_to": "b" * 40, "ref": ref, "commit_title": title, "ref_count": None},
    }


def _mr_event(action, title="Some MR", iid=5, at="2026-11-09T10:00:00Z"):
    return {"project_id": 10, "action_name": action, "target_id": 9000 + iid, "target_iid": iid,
            "target_type": "MergeRequest", "target_title": title, "created_at": at, "author": {"username": ME}}


def _note(title="Some MR", iid=5, at="2026-11-09T10:00:00Z", noteable="MergeRequest", target_type="DiffNote"):
    return {"project_id": 10, "action_name": "commented on", "target_id": 777, "target_iid": None,
            "target_type": target_type, "target_title": title, "created_at": at, "author": {"username": ME},
            "note": {"id": 777, "type": target_type, "body": "x", "noteable_type": noteable, "noteable_id": 9000 + iid,
                     "noteable_iid": iid}}


@pytest.mark.parametrize(
    ("event", "keys"),
    [
        (_push("feature/SUPERAPP-1-x", "SUPERAPP-2 fix"), ["SUPERAPP-1"]),
        (_push("task/x", "SUPERAPP-2 fix"), ["SUPERAPP-2"]),
        (_push("task/x", "fix"), []),
        (_push("feature/SUPERAPP-77-login", "fix"), ["SUPERAPP-77"]),
        (_push("task/no_key", "fix"), []),
        (_mr_event("opened", "SUPERAPP-3: title"), ["SUPERAPP-3"]),
        (_mr_event("accepted", "no key"), ["SUPERAPP-77"]),
        (_mr_event("approved", "no key", iid=60), []),
        (_note("no key"), ["SUPERAPP-77"]),
        (_note("no key", noteable="Issue"), []),
        ({**_mr_event("opened", "no key"), "project_id": "x"}, []),
    ],
)
def test_event_keys_fallback_order(event, keys):
    assert event_keys(event, _context()) == keys


def test_push_key_falls_back_to_the_branch_merge_request():
    context = _context(mr_by_branch={(10, "task/remove_old_snackbar"): {
        "title": "SUPERAPP-10395: Remove old snackbar", "source_branch": "task/remove_old_snackbar", "web_url": "u",
    }})
    assert event_keys(_push("task/remove_old_snackbar", "same behaviour"), context) == ["SUPERAPP-10395"]
    assert event_keys(_push("task/unknown_branch", "fix"), context) == []
    context = _context(mr_by_branch={(10, "task/a"): {"title": "No key", "source_branch": "feature/SUPERAPP-6-a"}})
    assert event_keys(_push("task/a", "fix"), context) == ["SUPERAPP-6"]


@pytest.mark.parametrize(
    ("event", "weight"),
    [
        (_push("x", count=1), 3),
        (_push("x", count=3), 5),
        (_push("x", count=40), 6),
        (_push("x", count=None), 3),
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


@pytest.mark.parametrize(
    ("event", "ignored"),
    [
        (_push("develop", "Merge branch 'task/remove_old_snackbar' into 'develop'"), True),
        (_push("task/x", "Merge branch 'develop' into task/x"), True),
        (_push("task/x", "Merge remote-tracking branch 'origin/develop' into task/x"), True),
        (_push("main"), True),
        (_push("master"), True),
        (_push("release/8.1"), True),
        (_push("rc/8.1.0"), True),
        (_push("trunk", project=10), True),
        (_push("trunk", project=11), False),
        (_push("hotfix/8.1.1"), False),
        (_push("task/x", count=0, action="removed", action_name="deleted"), True),
        (_push("task/x", count=0), True),
        (_push("task/x", action_name="pushed new", action="created"), False),
        ({**_push("v1.0"), "push_data": {**_push("v1.0")["push_data"], "ref_type": "tag"}}, True),
        (_mr_event("opened"), False),
    ],
)
def test_ignored_pushes(event, ignored):
    assert is_ignored_push(event, _context(default_branches={10: "trunk"})) is ignored


def test_default_branch_is_ignored_only_for_its_project():
    context = _context(default_branches={10: "trunk"}, ignored_push_branches=())
    assert is_ignored_push(_push("trunk", project=10), context)
    assert not is_ignored_push(_push("trunk", project=11), context)
    assert not is_ignored_push(_push("develop", project=10), context)


def test_review_activity_counts_only_on_my_merge_requests():
    events = [
        _mr_event("approved", "SUPERAPP-88: Their work", iid=6),
        _note("SUPERAPP-88: Their work", iid=6),
        _note("Their unkeyed work", iid=7),
        _mr_event("approved", "Their unkeyed work", iid=7),
        _mr_event("accepted", "SUPERAPP-88: Their work", iid=6),
        _note("Unknown MR", iid=404),
        _note("SUPERAPP-77 Login", iid=5),
        _note("On an issue", iid=1, noteable="Issue", target_type="Note"),
    ]
    activity = bucket_events(events, MSK, _context())
    assert activity.weights == {("SUPERAPP-77", date(2026, 11, 9)): 1}
    assert activity.sources == {"SUPERAPP-77": {"comment"}}
    assert activity.unkeyed == []


def test_review_activity_is_dropped_when_my_username_is_unknown():
    activity = bucket_events([_note("SUPERAPP-77 Login", iid=5)], MSK, _context(me=None))
    assert activity.weights == {}


def test_my_comment_without_key_is_not_unkeyed_work():
    lookup = {(10, 8): {"source_branch": "task/mine", "title": "Mine", "web_url": "u", "author_username": ME}}
    activity = bucket_events([_note("Mine", iid=8)], MSK, _context(mr_by_iid=lookup))
    assert activity.weights == {}
    assert activity.unkeyed == []


def test_bucket_events_real_day_shape():
    snackbar_mr = {"title": "SUPERAPP-10395: Remove old snackbar", "source_branch": "task/remove_old_snackbar",
                   "web_url": "https://gl/mr/5172", "author_username": ME}
    lookup = {**MR_LOOKUP, (3024, 5172): snackbar_mr}
    context = _context(
        mr_by_iid=lookup,
        mr_by_branch={(3024, "task/remove_old_snackbar"): snackbar_mr},
        default_branches={3024: "develop"},
    )
    events = [
        _push("task/remove_old_snackbar", "same behaviour", project=3024, action_name="pushed new", action="created"),
        _push("task/remove_old_snackbar", "ktlint", project=3024),
        _push("develop", "Merge branch 'task/remove_old_snackbar' into 'develop'", project=3024),
        _push("develop", "Merge branch 'task/ios_back_gesture' into 'develop'", project=3024),
        _push("task/ios_back_gesture", "Fix gesture", project=3024, count=2),
        _push("task/ios_back_gesture", "Polish", project=3024),
        _push("task/remove_old_snackbar", count=0, action="removed", action_name="deleted", project=3024),
        {**_mr_event("accepted", "SUPERAPP-10395: Remove old snackbar", iid=5172), "project_id": 3024},
        _mr_event("approved", "Расширить скоупы источников для пропуска стадии скриншот тестов", iid=7),
        _note("Расширить скоупы источников для пропуска стадии скриншот тестов", iid=7),
        _push("feature/SUPERAPP-1", at="2026-11-09T21:30:00Z", count=2),
        {"action_name": "pushed to", "created_at": "garbage", "project_id": 10, "push_data": {"ref": "SUPERAPP-9"}},
    ]
    activity = bucket_events(events, MSK, context)
    monday = date(2026, 11, 9)
    assert activity.weights == {
        ("SUPERAPP-10395", monday): 3 + 3 + 2,
        (None, monday): 4 + 3,
        ("SUPERAPP-1", date(2026, 11, 10)): 4,
    }
    assert activity.sources == {"SUPERAPP-10395": {"push", "mr"}, "SUPERAPP-1": {"push"}}
    assert activity.titles["SUPERAPP-10395"] == "SUPERAPP-10395: Remove old snackbar"
    assert activity.unkeyed == [{"title": "task/ios_back_gesture", "url": None}]


def test_unkeyed_groups_by_branch_and_upgrades_to_the_merge_request():
    mine = {"title": "Gesture polish", "source_branch": "task/ios_back_gesture", "web_url": "https://gl/mr/9",
            "author_username": ME}
    events = [
        _push("task/ios_back_gesture", "Fix gesture"),
        _push("task/ios_back_gesture", "Polish"),
        _mr_event("opened", "Gesture polish", iid=9),
    ]
    activity = bucket_events(events, MSK, _context(mr_by_iid={(10, 9): mine}))
    assert activity.unkeyed == [{"title": "Gesture polish", "url": "https://gl/mr/9"}]
    activity = bucket_events(events[:2], MSK, _context(mr_by_branch={(10, "task/ios_back_gesture"): mine}))
    assert activity.unkeyed == [{"title": "Gesture polish", "url": "https://gl/mr/9"}]


def test_unkeyed_opened_merge_request_without_known_branch():
    activity = bucket_events([_mr_event("opened", "Draft idea", iid=50)], MSK, _context())
    assert activity.unkeyed == [{"title": "Draft idea", "url": None}]


def test_unkeyed_items_are_capped():
    events = [_push(f"task/branch_{n}", "commit") for n in range(80)]
    assert len(bucket_events(events, MSK, _context()).unkeyed) == 50


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
            _push("task/chore", "chore", at="2026-11-02T09:00:00Z"),
            _push("feature/SUPERAPP-1", at="2026-11-07T09:00:00Z"),
        ],
        MSK, _context(),
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
    assert rows["SUPERAPP-1"]["cells"][monday] == {"suggested": 150, "edited": None, "logged": 60, "submission": "unknown"}
    assert rows["SUPERAPP-5"]["cells"][monday]["suggested"] == 30
    assert week["unkeyed"]["cells"][monday] == {"suggested": 180}
    assert week["unkeyed"]["items"] == [{"title": "task/chore", "url": None}]
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
    activity = bucket_events([_mr_event("opened", "SUPERAPP-77: Login", at="2026-11-02T08:00:00Z")], MSK, _context())
    week = build_week(_inputs(activity=activity))
    assert week["rows"][0]["title"] == "Login"
    assert week["step"] == 30


def test_build_week_does_not_suggest_into_the_future():
    week = build_week(_inputs(week_start=date(2026, 11, 9), in_progress={"SUPERAPP-5": ""}))
    cells = week["rows"][0]["cells"]
    assert cells["2026-11-10"]["suggested"] == 480
    assert all(cells[day]["suggested"] == 0 for day in ("2026-11-11", "2026-11-12", "2026-11-13"))


@pytest.mark.parametrize(
    ("text", "keys"),
    [
        ("Closes SUPERAPP-10397", ["SUPERAPP-10397"]),
        ("fixes: superapp-1\nResolved SUPERAPP-2", ["SUPERAPP-1", "SUPERAPP-2"]),
        ("Задача (SUPERAPP-10397)", ["SUPERAPP-10397"]),
        ("CLOSED:SUPERAPP-3, Fixed SUPERAPP-4, Resolves SUPERAPP-5", ["SUPERAPP-3", "SUPERAPP-4", "SUPERAPP-5"]),
        ("See SUPERAPP-10397 and SUPERAPP-1 nearby", []),
        ("Prefixes SUPERAPP-1", []),
        ("Closes OTHER-1 (OTHER-2)", []),
        ("Closes SUPERAPP-12345678 (SUPERAPP-9x)", ["SUPERAPP-12345678"]),
        (None, []),
    ],
)
def test_extract_closing_keys(text, keys):
    assert extract_closing_keys(text, ["SUPERAPP"]) == keys


def test_extract_closing_keys_without_queue_filter():
    assert extract_closing_keys("Closes ANY-1 (lower-2)", []) == ["ANY-1"]


GITLAB = "https://gitlab.lmru.tech"


@pytest.mark.parametrize(
    ("text", "links"),
    [
        ("PR:   https://gitlab.lmru.tech/products/lmwork/lmru-mobile_lmwork/-/merge_requests/5175",
         [("products/lmwork/lmru-mobile_lmwork", 5175)]),
        ("https://gitlab.lmru.tech/a/B/-/merge_requests/7/diffs#note_1 и https://gitlab.lmru.tech/a/b/-/merge_requests/8?tab=x",
         [("a/b", 7), ("a/b", 8)]),
        ("[MR](https://gitlab.lmru.tech/a/b/-/merge_requests/9#note_5)", [("a/b", 9)]),
        ("https://gitlab.lmru.tech:443/a/b/-/merge_requests/1 https://gitlab.lmru.tech/a/b/-/merge_requests/1", [("a/b", 1)]),
        ("https://evil.example.com/a/b/-/merge_requests/1", []),
        ("http://gitlab.lmru.tech/a/b/-/merge_requests/1", []),
        ("https://gitlab.lmru.tech.evil.com/a/b/-/merge_requests/1", []),
        ("https://gitlab.lmru.tech:bad/a/b/-/merge_requests/1", []),
        ("https://gitlab.lmru.tech/a/b/-/issues/1", []),
        ("https://gitlab.lmru.tech/a/b/-/merge_requests/new", []),
        (None, []),
    ],
)
def test_parse_mr_links(text, links):
    assert parse_mr_links(text, GITLAB) == links


def test_parse_mr_links_respects_the_gitlab_base_path():
    assert parse_mr_links("https://h.example/gitlab/a/b/-/merge_requests/3 https://h.example/a/b/-/merge_requests/4",
                          "https://h.example/gitlab") == [("a/b", 3)]


def _link(key, bot=False, mine=True, iid=1):
    return PrLink("a/b", iid, key, bot, mine)


@pytest.mark.parametrize(
    ("links", "expected"),
    [
        ([_link("SUPERAPP-1")], "SUPERAPP-1"),
        ([_link("SUPERAPP-1"), _link("SUPERAPP-1", bot=True)], "SUPERAPP-1"),
        ([_link("SUPERAPP-1"), _link("SUPERAPP-2", bot=True)], "SUPERAPP-2"),
        ([_link("SUPERAPP-1", bot=True), _link("SUPERAPP-2", bot=True)], None),
        ([_link("SUPERAPP-1"), _link("SUPERAPP-2")], None),
        ([_link("SUPERAPP-1", mine=False), _link("SUPERAPP-2")], "SUPERAPP-2"),
        ([_link("SUPERAPP-1", mine=False), _link("SUPERAPP-2", mine=False)], None),
    ],
)
def test_resolve_pr_links(links, expected):
    assert resolve_pr_links(links).get(("a/b", 1)) == expected


def test_resolve_pr_links_keeps_other_merge_requests():
    links = [_link("SUPERAPP-1"), _link("SUPERAPP-2"), _link("SUPERAPP-3", iid=2)]
    assert resolve_pr_links(links) == {("a/b", 2): "SUPERAPP-3"}


@pytest.mark.parametrize(
    ("info", "keys"),
    [
        ({"iid": 5, "title": "SUPERAPP-1: x", "source_branch": "task/SUPERAPP-2", "description": "Closes SUPERAPP-4"}, ["SUPERAPP-1"]),
        ({"iid": 5, "title": "x", "source_branch": "task/SUPERAPP-2", "description": "Closes SUPERAPP-4"}, ["SUPERAPP-2"]),
        ({"iid": 5, "title": "x", "source_branch": "task/x", "description": "Closes SUPERAPP-4"}, ["SUPERAPP-3"]),
        ({"iid": 6, "title": "x", "source_branch": "task/x", "description": "Closes SUPERAPP-4"}, ["SUPERAPP-4"]),
        ({"iid": 6, "title": "x", "source_branch": "task/x", "description": "about SUPERAPP-4"}, []),
        ({"title": "x", "source_branch": "task/x"}, []),
    ],
)
def test_mr_key_sources_order(info, keys):
    context = _context(tracker_links={(10, 5): "SUPERAPP-3"})
    assert mr_keys(info, 10, context) == keys


def test_title_wins_over_conflicting_tracker_link(caplog):
    import logging

    caplog.set_level(logging.DEBUG, logger="lpdev.timesheet")
    context = _context(tracker_links={(10, 5): "SUPERAPP-3"})
    assert mr_keys({"iid": 5, "title": "SUPERAPP-1 x"}, 10, context) == ["SUPERAPP-1"]
    assert "linked to SUPERAPP-3" in caplog.text


def test_tracker_link_does_not_make_foreign_reviews_count():
    lookup = {(10, 7): {"iid": 7, "source_branch": "task/no_key", "title": "Their", "author_username": "colleague"}}
    context = _context(mr_by_iid=lookup, tracker_links={(10, 7): "SUPERAPP-9"})
    activity = bucket_events([_note("Their", iid=7), _mr_event("approved", "Their", iid=7)], MSK, context)
    assert activity.weights == {}


@pytest.mark.parametrize(
    ("count", "weight", "ignored"),
    [(1, 0, True), (2, 3, False), (3, 4, False), (9, 6, False), (None, 0, True)],
)
def test_merge_commit_pushes_count_only_the_extra_commits(count, weight, ignored):
    event = _push("task/x", "Merge branch 'develop' into task/x", count=count)
    assert is_ignored_push(event, _context()) is ignored
    if not ignored:
        assert event_weight(event) == weight


def test_merge_commit_title_is_not_a_key_source():
    event = _push("task/x", "Merge branch 'feature/SUPERAPP-1' into task/x", count=3)
    assert event_keys(event, _context()) == []
    assert event_keys(_push("feature/SUPERAPP-2", "Merge branch 'develop'", count=3), _context()) == ["SUPERAPP-2"]


def _at(text):
    from datetime import datetime

    return datetime.fromisoformat(text)


STATUSES = {"development": "d", "codereview": "r"}


@pytest.mark.parametrize(
    ("history", "today", "expected"),
    [
        (
            [(_at("2026-11-09T06:48:21+00:00"), None, "backlog"), (_at("2026-11-09T06:48:51+00:00"), "todo", "development"),
             (_at("2026-11-09T06:57:06+00:00"), "development", "codereview")],
            date(2026, 11, 12),
            [("development", "2026-11-09", "2026-11-09"), ("codereview", "2026-11-09", "2026-11-12")],
        ),
        (
            [(_at("2026-11-19T09:00:00+03:00"), "todo", "development"), (_at("2026-11-19T18:00:00+03:00"), "development", "onHold"),
             (_at("2026-11-21T09:00:00+03:00"), "onHold", "development"), (_at("2026-11-22T10:00:00+03:00"), "development", "codereview"),
             (_at("2026-11-22T15:00:00+03:00"), "codereview", "done")],
            date(2026, 11, 25),
            [("development", "2026-11-19", "2026-11-19"), ("development", "2026-11-21", "2026-11-22"),
             ("codereview", "2026-11-22", "2026-11-22")],
        ),
        (
            [(_at("2026-11-02T10:00:00+03:00"), "todo", "development"), (_at("2026-11-03T10:00:00+03:00"), "development", "codereview"),
             (_at("2026-11-04T10:00:00+03:00"), "codereview", "development"), (_at("2026-11-04T12:00:00+03:00"), "development", "done")],
            date(2026, 11, 10),
            [("development", "2026-11-02", "2026-11-04"), ("codereview", "2026-11-03", "2026-11-04")],
        ),
        ([(_at("2026-11-08T22:30:00+00:00"), "todo", "development")], date(2026, 11, 10), [("development", "2026-11-09", "2026-11-10")]),
        ([(_at("2026-11-09T10:00:00+03:00"), "todo", "open")], date(2026, 11, 10), []),
        ([(_at("2026-11-09T10:00:00+03:00"), "development", "done")], date(2026, 11, 10), []),
        ([(_at("2026-11-12T10:00:00+03:00"), "todo", "development")], date(2026, 11, 10), [("development", "2026-11-12", "2026-11-12")]),
        ([], date(2026, 11, 10), []),
        (
            [(_at("2026-11-09T09:00:00+03:00"), "todo", "development"), (_at("2026-11-09T11:00:00+03:00"), "development", "codereview"),
             (_at("2026-11-09T15:00:00+03:00"), "codereview", "rc")],
            date(2026, 11, 10),
            [("development", "2026-11-09", "2026-11-09"), ("codereview", "2026-11-09", "2026-11-09")],
        ),
        (
            [(_at("2026-11-09T09:00:00+03:00"), "todo", "codereview"), (_at("2026-11-09T11:00:00+03:00"), "codereview", "development"),
             (_at("2026-11-09T15:00:00+03:00"), "development", "rc")],
            date(2026, 11, 10),
            [("codereview", "2026-11-09", "2026-11-09"), ("development", "2026-11-09", "2026-11-09")],
        ),
    ],
)
def test_status_intervals(history, today, expected):
    result = status_intervals(history, STATUSES, MSK, today)
    assert [(item["status"], item["from"], item["to"]) for item in result] == expected


def test_status_intervals_mark_only_the_current_status_open():
    history = [
        (_at("2026-11-09T09:00:00+03:00"), "todo", "development"),
        (_at("2026-11-09T11:00:00+03:00"), "development", "codereview"),
    ]
    result = status_intervals(history, STATUSES, MSK, date(2026, 11, 10))
    assert [(item["status"], item["open"]) for item in result] == [("development", False), ("codereview", True)]


def test_logged_task_does_not_get_a_second_share():
    weights = {"A-1": 2, "A-2": 4, "A-3": 1, "A-4": 1}
    before = suggest_day(480, weights, [], {}, {}, 30)
    assert before == {"A-1": 120, "A-2": 240, "A-3": 60, "A-4": 60}
    after = suggest_day(480, weights, [], {"A-1": 150}, {}, 30)
    assert after.get("A-1", 0) == 0
    assert sum(after.values()) == 330
    assert all(after[key] <= before[key] for key in after)


def test_partly_logged_task_gets_only_the_rest_of_its_share():
    assert suggest_day(480, {"A-1": 1, "A-2": 1}, [], {"A-1": 60}, {}, 30) == {"A-1": 180, "A-2": 240}


def test_time_logged_outside_the_activity_shrinks_every_share():
    result = suggest_day(480, {"A-1": 1, "A-2": 1}, [], {"B-9": 240}, {}, 30)
    assert result == {"A-1": 120, "A-2": 120}


def test_full_day_logged_suggests_nothing_and_edits_stay_fixed():
    assert suggest_day(480, {"A-1": 1}, [], {"A-1": 480}, {}, 30) == {}
    assert suggest_day(480, {"A-1": 1, "A-2": 1}, [], {}, {"A-1": 60}, 30) == {"A-1": 60, "A-2": 240}
    assert suggest_day(0, {"A-1": 1}, [], {}, {"A-1": 60}, 30) == {"A-1": 60}


@pytest.mark.parametrize("logged", [{}, {"A-1": 90}, {"A-2": 45}, {"A-1": 30, "A-3": 200}, {"Z-1": 470}])
def test_suggestions_fill_exactly_the_open_part_of_the_day(logged):
    result = suggest_day(480, {"A-1": 3, "A-2": 2, None: 1}, ["A-3"], logged, {}, 30)
    open_part = max(0, 480 - sum(logged.values())) // 30 * 30
    assert sum(result.values()) == open_part
    assert all(minutes % 30 == 0 for minutes in result.values())


def _at(value):
    return datetime.fromisoformat(value)


def test_owned_history_follows_status_only_while_assigned_to_me():
    me, qa = frozenset({"5"}), frozenset({"77"})
    statuses = [
        (_at("2026-11-02T10:00:00+03:00"), "todo", "development"),
        (_at("2026-11-04T10:00:00+03:00"), "development", "testing"),
        (_at("2026-11-06T10:00:00+03:00"), "testing", "rework"),
    ]
    assignees = [
        (_at("2026-11-04T10:00:05+03:00"), me, qa),
        (_at("2026-11-06T10:00:05+03:00"), qa, me),
    ]
    mine = owned_history(statuses, assignees, {"5"})
    assert [(moment.isoformat(), dst) for moment, _, dst in mine] == [
        ("2026-11-02T10:00:00+03:00", "development"),
        ("2026-11-04T10:00:00+03:00", "testing"),
        ("2026-11-04T10:00:05+03:00", None),
        ("2026-11-06T10:00:05+03:00", "rework"),
    ]
    theirs = owned_history(statuses, assignees, {"77"})
    assert [dst for _, _, dst in theirs] == ["testing", "rework", None]
    assert [dst for _, _, dst in owned_history(statuses, None, {"5"})] == ["development", "testing", "rework"]
    assert [dst for _, _, dst in owned_history(statuses, [], {"5"})] == ["development", "testing", "rework"]


def test_pass_through_statuses_are_not_intervals_and_owned_days_follow_intervals():
    me, qa = frozenset({"5"}), frozenset({"77"})
    statuses = [
        (_at("2026-11-02T10:00:00+03:00"), "todo", "development"),
        (_at("2026-11-04T10:00:00+03:00"), "development", "testing"),
        (_at("2026-11-06T10:00:00+03:00"), "testing", "rework"),
    ]
    assignees = [(_at("2026-11-04T10:00:05+03:00"), me, qa), (_at("2026-11-06T10:00:05+03:00"), qa, me)]
    days = [date(2026, 11, 2) + timedelta(days=offset) for offset in range(7)]
    today = date(2026, 11, 10)
    qa_intervals = status_intervals(owned_history(statuses, assignees, {"77"}), ["testing", "rework"], MSK, today, PASS_THROUGH)
    assert [(item["status"], item["from"], item["to"]) for item in qa_intervals] == [("testing", "2026-11-04", "2026-11-06")]
    assert interval_days(qa_intervals, ["testing"], days) == {date(2026, 11, 4), date(2026, 11, 5), date(2026, 11, 6)}
    dev_intervals = status_intervals(owned_history(statuses, assignees, {"5"}), ["development", "rework"], MSK, today, PASS_THROUGH)
    assert interval_days(dev_intervals, ["development"], days) == {date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4)}
    assert date(2026, 11, 5) not in interval_days(dev_intervals, ["development", "rework"], days)
    assert date(2026, 11, 6) in interval_days(dev_intervals, ["development", "rework"], days)


def test_build_week_suggests_in_progress_only_on_owned_days():
    week = build_week(_inputs(
        in_progress={"SUPERAPP-5": "Mine early", "SUPERAPP-6": "Unknown history"},
        owned_days={"SUPERAPP-5": frozenset({date(2026, 11, 2)}), "SUPERAPP-6": None},
    ))
    cells = {row["key"]: {day: cell["suggested"] for day, cell in row["cells"].items() if cell["suggested"]} for row in week["rows"]}
    assert cells["SUPERAPP-5"] == {"2026-11-02": 240}
    assert cells["SUPERAPP-6"]["2026-11-02"] == 240 and cells["SUPERAPP-6"]["2026-11-03"] == 420


def test_build_week_owned_weight_makes_owned_days_the_main_signal():
    activity = bucket_events([_push("feature/SUPERAPP-1", at="2026-11-02T08:00:00Z")], MSK, _context())
    inputs = dict(activity=activity, in_progress={"SUPERAPP-5": "Testing"}, owned_days={"SUPERAPP-5": frozenset({date(2026, 11, 2)})})
    idle = build_week(_inputs(**inputs))
    weighed = build_week(_inputs(**inputs, owned_weight=3))

    def monday(week):
        return {row["key"]: row["cells"]["2026-11-02"]["suggested"] for row in week["rows"]}

    assert monday(idle)["SUPERAPP-1"] > monday(idle)["SUPERAPP-5"]
    assert monday(weighed) == {"SUPERAPP-1": 240, "SUPERAPP-5": 240}
