from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from lpdev.workcalendar import day_kind, day_norm_minutes

TRACKER_WEB_URL = "https://tracker.yandex.ru"
WEEK_RE = re.compile(r"(\d{4})-W(\d{2})")
IN_PROGRESS_WEIGHT = 0.5
IN_PROGRESS_MAX_SHARE = 0.25
PUSH_WEIGHT = 3
PUSH_MAX_WEIGHT = 6
MR_WEIGHT = 2
REVIEW_WEIGHT = 1
MAX_UNKEYED_ITEMS = 50
_UPPER_KEY_RE = re.compile(r"(?<![A-Za-z0-9_])[A-Z][A-Z0-9_]+-\d+(?!\d)")
MR_ACTIONS = frozenset({"opened", "accepted", "merged"})
NOTE_TARGETS = frozenset({"Note", "DiffNote", "DiscussionNote"})

MrLookup = Mapping[tuple[int, int], Mapping[str, Any]]


@dataclass
class Activity:
    weights: dict[tuple[str | None, date], float] = field(default_factory=dict)
    sources: dict[str, set[str]] = field(default_factory=dict)
    titles: dict[str, str] = field(default_factory=dict)
    unkeyed: list[dict[str, str | None]] = field(default_factory=list)


def parse_week(value: str) -> date:
    match = WEEK_RE.fullmatch(value)
    if match is None:
        raise ValueError("week must look like 2026-W46")
    try:
        return date.fromisocalendar(int(match[1]), int(match[2]), 1)
    except ValueError:
        raise ValueError("no such ISO week") from None


def format_week(week_start: date) -> str:
    year, week, _ = week_start.isocalendar()
    return f"{year}-W{week:02d}"


def week_start_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


def week_days(week_start: date) -> list[date]:
    return [week_start + timedelta(days=offset) for offset in range(7)]


def submit_window(today: date) -> tuple[date, date]:
    return week_start_of(today) - timedelta(days=7), today


def issue_url(key: str) -> str:
    return f"{TRACKER_WEB_URL}/{key}"


def queue_of(key: str) -> str:
    return key.rsplit("-", 1)[0]


def extract_issue_keys(text: object, queues: Iterable[str]) -> list[str]:
    if not isinstance(text, str) or not text:
        return []
    allowed = list(queues)
    if allowed:
        alternatives = "|".join(re.escape(queue) for queue in sorted(allowed, key=len, reverse=True))
        pattern = re.compile(rf"(?<![A-Za-z0-9])({alternatives})-(\d+)(?!\d)", re.IGNORECASE)
        found = [f"{match[1].upper()}-{int(match[2])}" for match in pattern.finditer(text)]
    else:
        found = [match[0] for match in _UPPER_KEY_RE.finditer(text)]
    return list(dict.fromkeys(found))


def strip_issue_key_prefix(title: str, key: str) -> str:
    stripped = re.sub(rf"^\s*{re.escape(key)}[:\s\-–—]+", "", title, flags=re.IGNORECASE)
    return stripped or title


def _mr_iid(event: Mapping[str, Any]) -> int | None:
    if event.get("target_type") in NOTE_TARGETS:
        note = event.get("note") if isinstance(event.get("note"), Mapping) else {}
        if note.get("noteable_type") != "MergeRequest":
            return None
        iid = note.get("noteable_iid")
    elif event.get("target_type") == "MergeRequest":
        iid = event.get("target_iid")
    else:
        return None
    return iid if isinstance(iid, int) else None


def _mr_info(event: Mapping[str, Any], mr_lookup: MrLookup) -> Mapping[str, Any]:
    iid = _mr_iid(event)
    project_id = event.get("project_id")
    if iid is None or not isinstance(project_id, int):
        return {}
    return mr_lookup.get((project_id, iid), {})


def event_source(event: Mapping[str, Any]) -> str | None:
    action = str(event.get("action_name") or "")
    push = event.get("push_data")
    if isinstance(push, Mapping):
        return None if push.get("action") == "removed" else "push"
    if event.get("target_type") in NOTE_TARGETS or action == "commented on":
        return "comment"
    if event.get("target_type") != "MergeRequest":
        return None
    if action == "approved":
        return "review"
    return "mr" if action in MR_ACTIONS else None


def event_weight(event: Mapping[str, Any]) -> float:
    source = event_source(event)
    if source == "push":
        count = event["push_data"].get("commit_count")
        extra = max(0, count - 1) if isinstance(count, int) else 0
        return min(PUSH_WEIGHT + extra, PUSH_MAX_WEIGHT)
    if source == "mr":
        return MR_WEIGHT
    if source in ("comment", "review"):
        return REVIEW_WEIGHT
    return 0


def event_keys(event: Mapping[str, Any], mr_lookup: MrLookup, queues: Iterable[str] = ()) -> list[str]:
    queues = list(queues)
    push = event.get("push_data")
    if isinstance(push, Mapping):
        candidates = [push.get("ref"), push.get("commit_title")]
    else:
        candidates = [event.get("target_title"), _mr_info(event, mr_lookup).get("source_branch")]
    for text in candidates:
        keys = extract_issue_keys(text, queues)
        if keys:
            return keys
    return []


def event_description(event: Mapping[str, Any], mr_lookup: MrLookup) -> dict[str, str | None]:
    push = event.get("push_data")
    if isinstance(push, Mapping):
        ref = str(push.get("ref") or "")
        title = str(push.get("commit_title") or "")
        return {"title": f"{ref}: {title}" if ref and title else ref or title or "push", "url": None}
    info = _mr_info(event, mr_lookup)
    title = event.get("target_title") or info.get("title") or info.get("source_branch") or ""
    url = info.get("web_url")
    return {"title": str(title), "url": str(url) if url else None}


def event_day(event: Mapping[str, Any], tz: ZoneInfo) -> date | None:
    try:
        moment = datetime.fromisoformat(str(event.get("created_at")).replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return moment.astimezone(tz).date()


def bucket_events(
    events: Iterable[Mapping[str, Any]], tz: ZoneInfo, mr_lookup: MrLookup, queues: Iterable[str] = ()
) -> Activity:
    queues = list(queues)
    activity = Activity()
    seen_unkeyed: set[tuple[str, str | None]] = set()
    for event in events:
        weight = event_weight(event)
        day = event_day(event, tz)
        if not weight or day is None:
            continue
        source = event_source(event)
        keys = event_keys(event, mr_lookup, queues)
        if not keys:
            bucket = (None, day)
            activity.weights[bucket] = activity.weights.get(bucket, 0) + weight
            item = event_description(event, mr_lookup)
            marker = (str(item["title"]), item["url"])
            if marker not in seen_unkeyed and len(activity.unkeyed) < MAX_UNKEYED_ITEMS:
                seen_unkeyed.add(marker)
                activity.unkeyed.append(item)
            continue
        title = event_description(event, mr_lookup)["title"]
        for key in keys:
            bucket = (key, day)
            activity.weights[bucket] = activity.weights.get(bucket, 0) + weight / len(keys)
            activity.sources.setdefault(key, set()).add(source)
            if title and key not in activity.titles and source != "push":
                activity.titles[key] = str(title)
    return activity


def _sort_key(key: str | None) -> tuple[int, str]:
    return (1, "") if key is None else (0, key)


def allocate(
    remaining_minutes: int,
    weights: Mapping[str | None, float],
    in_progress: Iterable[str],
    fixed: Mapping[str, int],
    step: int,
) -> dict[str | None, int]:
    result: dict[str | None, int] = {key: minutes for key, minutes in fixed.items()}
    budget = max(0, remaining_minutes - sum(fixed.values())) // step * step
    active = {key: weight for key, weight in weights.items() if weight > 0 and key not in fixed}
    idle = sorted({key for key in in_progress if key not in fixed and key not in active})
    if budget == 0 or not (active or idle):
        return result
    shares: dict[str | None, float] = {}
    if active:
        total = sum(active.values())
        idle_share = min(IN_PROGRESS_WEIGHT * len(idle) / (total + IN_PROGRESS_WEIGHT * len(idle)), IN_PROGRESS_MAX_SHARE) * budget if idle else 0.0
        for key, weight in active.items():
            shares[key] = (budget - idle_share) * weight / total
        for key in idle:
            shares[key] = idle_share / len(idle)
    else:
        for key in idle:
            shares[key] = budget / len(idle)

    def heaviest() -> str | None:
        return min(shares, key=lambda key: (-shares[key], _sort_key(key)))

    for key in sorted(shares, key=lambda key: (shares[key], _sort_key(key))):
        target = heaviest()
        if key != target and shares[key] < step:
            shares[target] += shares.pop(key)

    units = {key: share / step for key, share in shares.items()}
    floors = {key: math.floor(value + 1e-9) for key, value in units.items()}
    left = budget // step - sum(floors.values())
    order = sorted(units, key=lambda key: (-(units[key] - floors[key]), _sort_key(key)))
    for key in order[:left]:
        floors[key] += 1
    for key, count in floors.items():
        if count:
            result[key] = count * step
    return result


@dataclass(frozen=True)
class WeekInputs:
    week_start: date
    today: date
    day_hours: int
    step: int
    activity: Activity
    in_progress: Mapping[str, str]
    manual: list[str]
    logged: Mapping[tuple[str, date], int]
    edits: Mapping[tuple[str, date], int]
    submissions: Mapping[tuple[str, date], str]
    titles: Mapping[str, str]
    tracker: Mapping[str, Any]
    calendar_ok: bool


def _row_order(inputs: WeekInputs, keys: set[str]) -> list[str]:
    totals: dict[str, float] = {}
    for (key, _), weight in inputs.activity.weights.items():
        if key is not None:
            totals[key] = totals.get(key, 0) + weight
    return sorted(keys, key=lambda key: (-totals.get(key, 0), key not in inputs.in_progress, key))


def build_week(inputs: WeekInputs, norm_for: Callable[[date, int], int] = day_norm_minutes) -> dict[str, Any]:
    days = week_days(inputs.week_start)
    window_from, window_to = submit_window(inputs.today)
    keys: set[str] = {key for key, _ in inputs.activity.weights if key is not None}
    keys |= set(inputs.in_progress) | set(inputs.manual)
    keys |= {key for key, day in inputs.logged if day in days}
    keys |= {key for key, day in inputs.edits if day in days}
    keys |= {key for key, day in inputs.submissions if day in days}
    rows = _row_order(inputs, keys)
    day_payload = []
    suggestions: dict[date, dict[str | None, int]] = {}
    for day in days:
        norm = norm_for(day, inputs.day_hours)
        logged_total = sum(minutes for (_, logged_day), minutes in inputs.logged.items() if logged_day == day)
        fixed = {key: minutes for (key, edit_day), minutes in inputs.edits.items() if edit_day == day}
        weights = {key: weight for (key, weight_day), weight in inputs.activity.weights.items() if weight_day == day}
        idle = [key for key in inputs.in_progress if not weights.get(key)]
        remaining = max(0, norm - logged_total) if norm and day <= inputs.today else 0
        suggestions[day] = allocate(remaining, weights, idle, fixed, inputs.step)
        day_payload.append({
            "date": day.isoformat(),
            "kind": day_kind(day),
            "norm": norm,
            "logged": logged_total,
            "submittable": window_from <= day <= window_to and norm > 0,
        })
    row_payload = []
    for key in rows:
        sources = sorted(inputs.activity.sources.get(key, set()))
        if key in inputs.in_progress:
            sources.append("in_progress")
        if key in inputs.manual:
            sources.append("manual")
        cells = {}
        for day in days:
            cells[day.isoformat()] = {
                "suggested": suggestions[day].get(key, 0),
                "edited": inputs.edits.get((key, day)),
                "logged": inputs.logged.get((key, day), 0),
                "submission": inputs.submissions.get((key, day)),
            }
        title = inputs.titles.get(key) or inputs.in_progress.get(key) or inputs.activity.titles.get(key) or ""
        row_payload.append({"key": key, "title": strip_issue_key_prefix(title, key), "url": issue_url(key), "sources": sources, "cells": cells})
    unkeyed = None
    if inputs.activity.unkeyed:
        unkeyed = {
            "items": list(inputs.activity.unkeyed),
            "cells": {day.isoformat(): {"suggested": suggestions[day].get(None, 0)} for day in days},
        }
    return {
        "week": format_week(inputs.week_start),
        "prev_week": format_week(inputs.week_start - timedelta(days=7)),
        "next_week": format_week(inputs.week_start + timedelta(days=7)),
        "today": inputs.today.isoformat(),
        "calendar_ok": inputs.calendar_ok,
        "step": inputs.step,
        "tracker": dict(inputs.tracker),
        "days": day_payload,
        "rows": row_payload,
        "unkeyed": unkeyed,
    }
