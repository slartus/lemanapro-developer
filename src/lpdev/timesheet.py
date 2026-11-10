from __future__ import annotations

import fnmatch
import logging
import math
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
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
MERGE_COMMIT_PREFIXES = ("Merge branch ", "Merge remote-tracking branch ")

_KEY_TOKEN = r"[A-Za-z][A-Za-z0-9_]+-\d+"
_CLOSING_KEY_RE = re.compile(
    rf"(?<![A-Za-z])(?:closes|fixes|resolves|closed|fixed|resolved)\s*:?\s*(?P<closing>{_KEY_TOKEN})(?!\d)"
    rf"|\((?P<paren>{_KEY_TOKEN})\)",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://[^\s<>\"'()\[\]]+")
_MR_PATH_RE = re.compile(r"/(?P<project>[^?#]+?)/-/merge_requests/(?P<iid>\d+)(?:/[^?#]*)?")
logger = logging.getLogger(__name__)

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


def extract_closing_keys(text: object, queues: Iterable[str]) -> list[str]:
    if not isinstance(text, str) or not text:
        return []
    allowed = list(queues)
    found: list[str] = []
    for match in _CLOSING_KEY_RE.finditer(text):
        token = match["closing"] or match["paren"]
        found.extend(extract_issue_keys(token, allowed))
    return list(dict.fromkeys(found))


def parse_mr_links(text: object, gitlab_url: str) -> list[tuple[str, int]]:
    if not isinstance(text, str) or not text:
        return []
    base = urlsplit(gitlab_url)
    base_path = base.path.rstrip("/")
    links = []
    for raw in _URL_RE.findall(text):
        parts = urlsplit(raw)
        try:
            same_origin = _origin(parts) == _origin(base)
        except ValueError:
            continue
        if not same_origin or not parts.path.startswith(base_path + "/"):
            continue
        match = _MR_PATH_RE.fullmatch(parts.path[len(base_path):])
        if match is not None:
            links.append((match["project"].lower(), int(match["iid"])))
    return list(dict.fromkeys(links))


@dataclass(frozen=True)
class PrLink:
    project_path: str
    iid: int
    key: str
    from_bot: bool
    assigned_to_me: bool


def resolve_pr_links(links: Iterable[PrLink]) -> dict[tuple[str, int], str]:
    grouped: dict[tuple[str, int], list[PrLink]] = {}
    for link in links:
        grouped.setdefault((link.project_path, link.iid), []).append(link)
    resolved: dict[tuple[str, int], str] = {}
    for mr, candidates in grouped.items():
        keys = {link.key for link in candidates}
        if len(keys) == 1:
            resolved[mr] = keys.pop()
            continue
        for preferred in ({link.key for link in candidates if link.from_bot},
                          {link.key for link in candidates if link.assigned_to_me}):
            if len(preferred) == 1:
                resolved[mr] = preferred.pop()
                break
            if preferred:
                break
        if mr not in resolved:
            logger.debug("timesheet: %s!%s is linked to several issues %s, link ignored", mr[0], mr[1], sorted(keys))
    return resolved


def _origin(parts) -> tuple[str, str, int | None]:
    scheme = parts.scheme.lower()
    return scheme, (parts.hostname or "").lower(), parts.port or {"http": 80, "https": 443}.get(scheme)


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
    return iid if isinstance(iid, int) and not isinstance(iid, bool) else None


def mr_ref(event: Mapping[str, Any]) -> tuple[int, int] | None:
    iid = _mr_iid(event)
    project_id = event.get("project_id")
    if iid is None or not isinstance(project_id, int):
        return None
    return project_id, iid


def push_ref(event: Mapping[str, Any]) -> tuple[int, str] | None:
    push = event.get("push_data")
    project_id = event.get("project_id")
    if not isinstance(push, Mapping) or not isinstance(project_id, int) or not isinstance(push.get("ref"), str):
        return None
    return project_id, push["ref"]


@dataclass(frozen=True)
class EventContext:
    mr_by_iid: MrLookup = field(default_factory=dict)
    mr_by_branch: Mapping[tuple[int, str], Mapping[str, Any]] = field(default_factory=dict)
    me: str | None = None
    ignored_push_branches: tuple[str, ...] = ()
    default_branches: Mapping[int, str] = field(default_factory=dict)
    queues: tuple[str, ...] = ()
    tracker_links: Mapping[tuple[int, int], str] = field(default_factory=dict)


def event_source(event: Mapping[str, Any]) -> str | None:
    action = str(event.get("action_name") or "")
    push = event.get("push_data")
    if isinstance(push, Mapping):
        return "push"
    if event.get("target_type") in NOTE_TARGETS:
        return "comment"
    if event.get("target_type") != "MergeRequest":
        return None
    return "mr" if action in MR_ACTIONS or action == "approved" else None


def event_weight(event: Mapping[str, Any]) -> float:
    source = event_source(event)
    if source == "push":
        count = event["push_data"].get("commit_count")
        extra = max(0, count - 1) if isinstance(count, int) else 0
        return min(PUSH_WEIGHT + extra, PUSH_MAX_WEIGHT)
    if source == "mr" and event.get("action_name") != "approved":
        return MR_WEIGHT
    if source in ("comment", "mr"):
        return REVIEW_WEIGHT
    return 0


def is_ignored_push(event: Mapping[str, Any], context: EventContext) -> bool:
    push = event.get("push_data")
    if not isinstance(push, Mapping):
        return False
    ref = push.get("ref")
    title = str(push.get("commit_title") or "")
    if push.get("action") in ("removed", "deleted") or event.get("action_name") == "deleted":
        return True
    if push.get("commit_count") == 0 or push.get("ref_type") == "tag" or not isinstance(ref, str) or not ref:
        return True
    if title.startswith(MERGE_COMMIT_PREFIXES):
        return True
    if ref == context.default_branches.get(event.get("project_id")):
        return True
    return any(fnmatch.fnmatchcase(ref, pattern) for pattern in context.ignored_push_branches)


def event_mr(event: Mapping[str, Any], context: EventContext) -> Mapping[str, Any]:
    ref = mr_ref(event)
    if ref is not None:
        return context.mr_by_iid.get(ref, {})
    branch = push_ref(event)
    return context.mr_by_branch.get(branch, {}) if branch is not None else {}


def is_counted(event: Mapping[str, Any], context: EventContext) -> bool:
    source = event_source(event)
    if source is None:
        return False
    if source == "push":
        return not is_ignored_push(event, context)
    if mr_ref(event) is None:
        return False
    if source == "mr" and event.get("action_name") == "opened":
        return True
    author = event_mr(event, context).get("author_username")
    return context.me is not None and author == context.me


def mr_keys(info: Mapping[str, Any], project_id: object, context: EventContext) -> list[str]:
    iid = info.get("iid")
    linked = context.tracker_links.get((project_id, iid)) if isinstance(project_id, int) and isinstance(iid, int) else None
    for text in (info.get("title"), info.get("source_branch")):
        keys = extract_issue_keys(text, context.queues)
        if keys:
            if linked is not None and linked not in keys:
                logger.debug("timesheet: %s!%s is linked to %s in Tracker but named %s", project_id, iid, linked, keys)
            return keys
    if linked is not None:
        return [linked]
    return extract_closing_keys(info.get("description"), context.queues)


def event_keys(event: Mapping[str, Any], context: EventContext) -> list[str]:
    info = event_mr(event, context)
    push = event.get("push_data")
    own = [push.get("ref"), push.get("commit_title")] if isinstance(push, Mapping) else [event.get("target_title")]
    for text in own:
        keys = extract_issue_keys(text, context.queues)
        if keys:
            return keys
    return mr_keys(info, event.get("project_id"), context) if info else []


def event_title(event: Mapping[str, Any], context: EventContext) -> str:
    info = event_mr(event, context)
    if isinstance(event.get("push_data"), Mapping):
        return str(info.get("title") or "")
    return str(event.get("target_title") or info.get("title") or "")


def unkeyed_item(event: Mapping[str, Any], context: EventContext) -> tuple[tuple[Any, ...], dict[str, str | None]]:
    info = event_mr(event, context)
    project_id = event.get("project_id")
    url = str(info["web_url"]) if info.get("web_url") else None
    branch = push_ref(event)
    if branch is not None:
        return (project_id, "branch", branch[1]), {"title": str(info.get("title") or branch[1]), "url": url}
    title = str(event.get("target_title") or info.get("title") or info.get("source_branch") or "")
    if info.get("source_branch"):
        return (project_id, "branch", info["source_branch"]), {"title": title, "url": url}
    return (project_id, "mr", _mr_iid(event)), {"title": title, "url": url}


def event_day(event: Mapping[str, Any], tz: ZoneInfo) -> date | None:
    try:
        moment = datetime.fromisoformat(str(event.get("created_at")).replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return moment.astimezone(tz).date()


def bucket_events(events: Iterable[Mapping[str, Any]], tz: ZoneInfo, context: EventContext) -> Activity:
    activity = Activity()
    unkeyed: dict[tuple[Any, ...], dict[str, str | None]] = {}
    for event in events:
        weight = event_weight(event)
        day = event_day(event, tz)
        if not weight or day is None or not is_counted(event, context):
            continue
        source = event_source(event)
        keys = event_keys(event, context)
        if not keys:
            if source == "comment" or event.get("action_name") == "approved":
                continue
            bucket = (None, day)
            activity.weights[bucket] = activity.weights.get(bucket, 0) + weight
            group, item = unkeyed_item(event, context)
            known = unkeyed.get(group)
            if known is None and len(unkeyed) < MAX_UNKEYED_ITEMS:
                unkeyed[group] = item
            elif known is not None and known["url"] is None and item["url"] is not None:
                unkeyed[group] = item
            continue
        title = event_title(event, context)
        for key in keys:
            bucket = (key, day)
            activity.weights[bucket] = activity.weights.get(bucket, 0) + weight / len(keys)
            activity.sources.setdefault(key, set()).add(source)
            if title and key not in activity.titles:
                activity.titles[key] = title
    activity.unkeyed = list(unkeyed.values())
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
