from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time as dt_time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from lpdev.config import TrackerConfig, run_secret_command
from lpdev.db import (
    timesheet_cell_set,
    timesheet_cells,
    timesheet_cells_clear,
    timesheet_task_add,
    timesheet_tasks,
    worklog_submission_create,
    worklog_submission_finish,
    worklog_submissions,
)
from lpdev.gitlab_client import GitlabApiError
from lpdev.timesheet import (
    WeekInputs,
    bucket_events,
    build_week,
    event_day,
    parse_week,
    queue_of,
    submit_window,
    week_days,
    week_start_of,
)
from lpdev.tracker_client import (
    TrackerApiError,
    TrackerClient,
    TrackerUncertain,
    Worklog,
    user_identities,
    valid_issue_key,
)
from lpdev.web.routes import templates
from lpdev.workcalendar import day_norm_minutes, has_year

logger = logging.getLogger(__name__)

router = APIRouter()

EVENTS_TTL_SEC = 300.0
EVENTS_CACHE_SIZE = 8
EVENTS_PADDING_DAYS = 2
MAX_SUBMIT_ENTRIES = 50
DUPLICATE_WINDOW = timedelta(minutes=10)
RECONCILE_SLACK = timedelta(minutes=1)
MAX_IN_PROGRESS = 5
IN_PROGRESS_STATUSES = frozenset({"inProgress", "inReview", "testing"})
WORKLOG_START = dt_time(9, 0)
OPEN_SUBMISSION_STATUSES = ("pending", "unknown")

TrackerFactory = Callable[[TrackerConfig], TrackerClient]


class InvalidRequest(Exception):
    pass


def _default_tracker_factory(cfg: TrackerConfig) -> TrackerClient:
    async def token() -> str:
        try:
            return await asyncio.to_thread(run_secret_command, cfg.token_command, "Tracker token")
        except RuntimeError as exc:
            raise TrackerApiError(f"не удалось получить токен Tracker: {exc}") from exc

    return TrackerClient(cfg, token)


@dataclass
class TimesheetState:
    tracker_factory: TrackerFactory = _default_tracker_factory
    tracker: TrackerClient | None = None
    tracker_cfg: TrackerConfig | None = None
    me: dict[str, Any] | None = None
    events: dict[tuple[date, date], tuple[float, list[dict[str, Any]]]] = field(default_factory=dict)
    submit_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tracker_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def aclose(self) -> None:
        if self.tracker is not None:
            await self.tracker.aclose()
            self.tracker = None

    async def tracker_for(self, cfg: TrackerConfig) -> TrackerClient | None:
        async with self.tracker_lock:
            if not cfg.enabled:
                await self.aclose()
                self.tracker_cfg = None
                return None
            if self.tracker is None or self.tracker_cfg != cfg:
                await self.aclose()
                self.tracker = self.tracker_factory(cfg)
                self.tracker_cfg = cfg
                self.me = None
            return self.tracker

    async def myself(self, tracker: TrackerClient) -> dict[str, Any]:
        if self.me is None:
            self.me = await tracker.myself()
        return self.me

    async def events_between(self, gitlab, after: date, before: date) -> list[dict[str, Any]]:
        now = time.monotonic()
        cached = self.events.get((after, before))
        if cached is not None and now - cached[0] < EVENTS_TTL_SEC:
            return cached[1]
        events = await gitlab.get_my_events(after, before)
        self.events[(after, before)] = (now, events)
        while len(self.events) > EVENTS_CACHE_SIZE:
            self.events.pop(min(self.events, key=lambda item: self.events[item][0]))
        return events


def _ctx(request: Request):
    return request.app.state.ctx


def _state(request: Request) -> TimesheetState:
    return request.app.state.timesheet


def _cfg(request: Request) -> TrackerConfig:
    return _ctx(request).config.tracker


def _tz(cfg: TrackerConfig) -> ZoneInfo:
    return ZoneInfo(cfg.timezone)


def _today(cfg: TrackerConfig) -> date:
    return datetime.now(_tz(cfg)).date()


def _error(message: str, status: int = 422) -> JSONResponse:
    return JSONResponse({"detail": message}, status_code=status)


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = json.loads(await request.body())
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise InvalidRequest("неверное тело запроса") from None
    if not isinstance(body, dict):
        raise InvalidRequest("неверное тело запроса")
    return body


def _key(value: object, cfg: TrackerConfig) -> str:
    if not valid_issue_key(value):
        raise InvalidRequest("неверный ключ задачи")
    if cfg.queues and queue_of(value) not in cfg.queues:
        raise InvalidRequest(f"очередь {queue_of(value)} не разрешена в настройках")
    return value


def _day(value: object) -> date:
    if not isinstance(value, str):
        raise InvalidRequest("неверная дата")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise InvalidRequest("неверная дата") from None


def _week(value: object) -> date:
    if not isinstance(value, str):
        raise InvalidRequest("неверная неделя")
    try:
        return parse_week(value)
    except ValueError:
        raise InvalidRequest("неверная неделя") from None


def _in_window(day: date, cfg: TrackerConfig) -> date:
    window_from, window_to = submit_window(_today(cfg))
    if not window_from <= day <= window_to:
        raise InvalidRequest("дата вне окна списания (с понедельника прошлой недели по сегодня)")
    return day


def _minutes(value: object, day: date, cfg: TrackerConfig, allow_zero: bool) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidRequest("минуты должны быть целым числом")
    norm = day_norm_minutes(day, cfg.day_hours)
    if value % cfg.step_minutes:
        raise InvalidRequest(f"минуты должны быть кратны {cfg.step_minutes}")
    if value < (0 if allow_zero else cfg.step_minutes) or value > norm:
        raise InvalidRequest(f"минуты вне диапазона 0..{norm}")
    return value


def _mr_lookup(conn) -> dict[tuple[int, int], dict[str, Any]]:
    rows = conn.execute(
        "SELECT project.gitlab_project_id, mr.iid, mr.source_branch, mr.title, mr.web_url "
        "FROM mr JOIN project ON project.id = mr.project_id"
    ).fetchall()
    return {
        (row["gitlab_project_id"], row["iid"]): {
            "source_branch": row["source_branch"], "title": row["title"], "web_url": row["web_url"],
        }
        for row in rows
    }


def _in_progress(issues: list[dict[str, Any]], cfg: TrackerConfig) -> dict[str, str]:
    result: dict[str, str] = {}
    for issue in issues:
        key = issue.get("key")
        status = issue.get("status") if isinstance(issue.get("status"), dict) else {}
        status_type = issue.get("statusType") if isinstance(issue.get("statusType"), dict) else {}
        if not valid_issue_key(key) or (cfg.queues and queue_of(key) not in cfg.queues):
            continue
        if status.get("key") in IN_PROGRESS_STATUSES or status_type.get("key") == "inProgress":
            result[key] = str(issue.get("summary") or "")
        if len(result) >= MAX_IN_PROGRESS:
            break
    return result


def _parse_created(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _submission_states(rows) -> dict[tuple[str, date], str]:
    latest: dict[tuple[str, date], str] = {}
    for row in rows:
        latest[(row["issue_key"], date.fromisoformat(row["day"]))] = row["status"]
    return {cell: status for cell, status in latest.items() if status != "done"}


def _unseen_done(rows, worklogs: list[Worklog], now: datetime) -> list[tuple[str, date, int]]:
    seen = {worklog.id for worklog in worklogs}
    return [
        (row["issue_key"], date.fromisoformat(row["day"]), row["minutes"])
        for row in rows
        if row["status"] == "done" and row["tracker_id"] not in seen
        and now - _parse_created(row["created_at"]) < DUPLICATE_WINDOW
    ]


@dataclass
class TrackerData:
    in_progress: dict[str, str] = field(default_factory=dict)
    worklogs: list[Worklog] = field(default_factory=list)
    titles: dict[str, str] = field(default_factory=dict)
    error: str | None = None


async def _tracker_data(state: TimesheetState, tracker: TrackerClient, cfg: TrackerConfig, days: list[date], keys: set[str]) -> TrackerData:
    data = TrackerData()
    try:
        me = await state.myself(tracker)
        data.in_progress = _in_progress(await tracker.search_my_issues(), cfg)
        data.worklogs = await tracker.search_worklogs(str(me.get("login") or ""), days[0], days[-1])
        wanted = (keys | {worklog.key for worklog in data.worklogs}) - set(data.in_progress)
        issues = await tracker.get_issues(sorted(wanted)) if wanted else {}
        data.titles = {key: str(issue.get("summary") or "") for key, issue in issues.items()}
    except TrackerApiError as exc:
        logger.warning("timesheet: tracker read failed: %s", exc.message)
        return TrackerData(error=exc.message)
    return data


@router.get("/timesheet", response_class=HTMLResponse)
async def timesheet_page(request: Request):
    return templates.TemplateResponse(request, "timesheet.html", {"active": "timesheet"})


@router.get("/api/timesheet")
async def api_timesheet(request: Request):
    ctx = _ctx(request)
    cfg = _cfg(request)
    state = _state(request)
    tz = _tz(cfg)
    today = _today(cfg)
    raw_week = request.query_params.get("week")
    try:
        week_start = _week(raw_week) if raw_week is not None else week_start_of(today)
    except InvalidRequest as exc:
        return _error(str(exc))
    days = week_days(week_start)
    try:
        events = await state.events_between(
            ctx.api, days[0] - timedelta(days=EVENTS_PADDING_DAYS), days[-1] + timedelta(days=EVENTS_PADDING_DAYS)
        )
    except GitlabApiError as exc:
        logger.warning("timesheet: gitlab events failed: %s", exc.message)
        return _error("GitLab не отдал события, повторите позже", 502)
    activity = bucket_events(
        (event for event in events if isinstance(event, dict) and event_day(event, tz) in days),
        tz, _mr_lookup(ctx.conn), cfg.queues,
    )
    iso_days = [day.isoformat() for day in days]
    edits = {(key, date.fromisoformat(day)): minutes for (key, day), minutes in timesheet_cells(ctx.conn, iso_days).items()}
    manual = timesheet_tasks(ctx.conn, week_start.isoformat())
    journal = worklog_submissions(ctx.conn, iso_days)
    tracker = await state.tracker_for(cfg)
    data = TrackerData()
    if tracker is not None:
        keys = {key for key, _ in activity.weights if key is not None} | set(manual) | {key for key, _ in edits}
        data = await _tracker_data(state, tracker, cfg, days, keys)
    logged: dict[tuple[str, date], int] = {}
    extra = _unseen_done(journal, data.worklogs, datetime.now(UTC)) if tracker is not None and data.error is None else []
    for key, day, minutes in [(w.key, w.day, w.minutes) for w in data.worklogs] + extra:
        logged[(key, day)] = logged.get((key, day), 0) + minutes
    inputs = WeekInputs(
        week_start=week_start, today=today, day_hours=cfg.day_hours, step=cfg.step_minutes,
        activity=activity, in_progress=data.in_progress, manual=manual, logged=logged, edits=edits,
        submissions=_submission_states(journal), titles=data.titles,
        tracker={
            "enabled": cfg.enabled,
            "submit_enabled": cfg.enabled and cfg.submit_enabled and data.error is None,
            "error": data.error,
        },
        calendar_ok=all(has_year(day.year) for day in days),
    )
    return JSONResponse(build_week(inputs), headers={"Cache-Control": "no-store"})


@router.put("/api/timesheet/cell")
async def api_timesheet_cell(request: Request):
    cfg = _cfg(request)
    try:
        body = await _json_body(request)
        key = _key(body.get("key"), cfg)
        day = _in_window(_day(body.get("date")), cfg)
        minutes = body.get("minutes")
        if minutes is not None:
            minutes = _minutes(minutes, day, cfg, allow_zero=True)
    except InvalidRequest as exc:
        return _error(str(exc))
    timesheet_cell_set(_ctx(request).conn, key, day.isoformat(), minutes)
    return JSONResponse({"ok": True})


@router.post("/api/timesheet/tasks")
async def api_timesheet_task(request: Request):
    cfg = _cfg(request)
    try:
        body = await _json_body(request)
        key = _key(body.get("key"), cfg)
        week_start = _week(body.get("week"))
        window_from, window_to = submit_window(_today(cfg))
        if week_start + timedelta(days=6) < window_from or week_start > window_to:
            raise InvalidRequest("неделя только для просмотра")
    except InvalidRequest as exc:
        return _error(str(exc))
    timesheet_task_add(_ctx(request).conn, key, week_start.isoformat())
    return JSONResponse({"ok": True})


@router.delete("/api/timesheet/cells")
async def api_timesheet_cells_clear(request: Request):
    cfg = _cfg(request)
    try:
        days = week_days(_week(request.query_params.get("week")))
        raw_day = request.query_params.get("date")
        if raw_day is not None:
            day = _day(raw_day)
            if day not in days:
                raise InvalidRequest("дата не из этой недели")
            days = [day]
    except InvalidRequest as exc:
        return _error(str(exc))
    window_from, window_to = submit_window(_today(cfg))
    editable = [day.isoformat() for day in days if window_from <= day <= window_to]
    timesheet_cells_clear(_ctx(request).conn, editable)
    return JSONResponse({"ok": True})


@dataclass(frozen=True)
class Entry:
    key: str
    day: date
    minutes: int


def _entries(body: dict[str, Any], cfg: TrackerConfig) -> list[Entry]:
    raw = body.get("entries")
    if not isinstance(raw, list) or not raw:
        raise InvalidRequest("нет записей для списания")
    if len(raw) > MAX_SUBMIT_ENTRIES:
        raise InvalidRequest(f"не больше {MAX_SUBMIT_ENTRIES} записей за раз")
    entries = []
    for item in raw:
        if not isinstance(item, dict):
            raise InvalidRequest("неверная запись")
        key = _key(item.get("key"), cfg)
        day = _in_window(_day(item.get("date")), cfg)
        entries.append(Entry(key, day, _minutes(item.get("minutes"), day, cfg, allow_zero=False)))
    if len({(entry.key, entry.day) for entry in entries}) != len(entries):
        raise InvalidRequest("повторяющиеся ячейки в одном запросе")
    return entries


def _matches(worklog: Worklog, key: str, day: date, minutes: int, since: datetime, claimed: set[str]) -> bool:
    if worklog.key != key or worklog.day != day or worklog.minutes != minutes or worklog.id in claimed:
        return False
    return worklog.created_at is None or worklog.created_at >= since - RECONCILE_SLACK


async def _reconcile(conn, tracker: TrackerClient, identities: set[str], rows, claimed: set[str]) -> None:
    for row in rows:
        key, day = row["issue_key"], date.fromisoformat(row["day"])
        since = _parse_created(row["created_at"])
        try:
            worklogs = await tracker.issue_worklogs(key, identities)
        except TrackerApiError as exc:
            logger.warning("timesheet: reconcile %s %s failed: %s", key, day, exc.message)
            continue
        match = next((w for w in worklogs if _matches(w, key, day, row["minutes"], since, claimed)), None)
        if match is not None:
            claimed.add(match.id)
            worklog_submission_finish(conn, row["id"], "done", tracker_id=match.id)
            timesheet_cell_set(conn, key, day.isoformat(), None)
            logger.info("timesheet: reconciled %s %s %s as done", key, day, row["minutes"])
        else:
            worklog_submission_finish(conn, row["id"], "failed", error="не найдено в Tracker после сбоя, можно повторить")
            logger.info("timesheet: reconciled %s %s %s as failed", key, day, row["minutes"])


def _result(entry: Entry, status: str, error: str | None = None) -> dict[str, Any]:
    return {"key": entry.key, "date": entry.day.isoformat(), "minutes": entry.minutes, "status": status, "error": error}


@router.post("/api/timesheet/submit")
async def api_timesheet_submit(request: Request):
    cfg = _cfg(request)
    if not (cfg.enabled and cfg.submit_enabled):
        return _error("списание в Tracker выключено в настройках", 403)
    try:
        entries = _entries(await _json_body(request), cfg)
    except InvalidRequest as exc:
        return _error(str(exc))
    state = _state(request)
    conn = _ctx(request).conn
    async with state.submit_lock:
        tracker = await state.tracker_for(cfg)
        if tracker is None:
            return _error("Tracker не настроен", 403)
        return JSONResponse({"results": await _submit(conn, state, tracker, cfg, entries)})


async def _submit(conn, state: TimesheetState, tracker: TrackerClient, cfg: TrackerConfig, entries: list[Entry]) -> list[dict[str, Any]]:
    days = sorted({entry.day for entry in entries})
    iso_days = [day.isoformat() for day in days]
    cells = {(entry.key, entry.day) for entry in entries}
    try:
        me = await state.myself(tracker)
        identities = user_identities(me)
        journal = worklog_submissions(conn, iso_days)
        claimed = {row["tracker_id"] for row in journal if row["status"] == "done" and row["tracker_id"]}
        open_rows = [
            row for row in journal
            if row["status"] in OPEN_SUBMISSION_STATUSES and (row["issue_key"], date.fromisoformat(row["day"])) in cells
        ]
        await _reconcile(conn, tracker, identities, open_rows, claimed)
        worklogs = await tracker.search_worklogs(str(me.get("login") or ""), days[0], days[-1])
    except TrackerApiError as exc:
        logger.warning("timesheet: submit pre-read failed: %s", exc.message)
        return [_result(entry, "failed", exc.message) for entry in entries]
    now = datetime.now(UTC)
    journal = worklog_submissions(conn, iso_days)
    known_ids = {worklog.id for worklog in worklogs} | claimed
    day_totals: dict[date, int] = {}
    for key, day, minutes in [(w.key, w.day, w.minutes) for w in worklogs] + _unseen_done(journal, worklogs, now):
        day_totals[day] = day_totals.get(day, 0) + minutes
    recent_done = {
        (row["issue_key"], date.fromisoformat(row["day"]), row["minutes"])
        for row in journal
        if row["status"] == "done" and now - _parse_created(row["created_at"]) < DUPLICATE_WINDOW
    }
    results = []
    for entry in entries:
        if (entry.key, entry.day, entry.minutes) in recent_done:
            results.append(_result(entry, "skipped", "уже списано за последние 10 минут"))
            continue
        norm = day_norm_minutes(entry.day, cfg.day_hours)
        if day_totals.get(entry.day, 0) + entry.minutes > norm:
            results.append(_result(entry, "failed", "превышена норма дня"))
            continue
        results.append(await _create(conn, tracker, cfg, entry, identities, known_ids))
        if results[-1]["status"] == "done":
            day_totals[entry.day] = day_totals.get(entry.day, 0) + entry.minutes
    return results


async def _create(conn, tracker: TrackerClient, cfg: TrackerConfig, entry: Entry, identities: set[str], known_ids: set[str]) -> dict[str, Any]:
    submission_id = worklog_submission_create(conn, entry.key, entry.day.isoformat(), entry.minutes)
    start = datetime.combine(entry.day, WORKLOG_START, _tz(cfg))
    started_at = datetime.now(UTC)
    try:
        tracker_id = await tracker.create_worklog(entry.key, start, entry.minutes)
    except TrackerUncertain as exc:
        worklog_submission_finish(conn, submission_id, "unknown", error=exc.message)
        logger.warning("timesheet: submit %s %s %s uncertain", entry.key, entry.day, entry.minutes)
        return await _confirm(conn, tracker, entry, submission_id, identities, known_ids, started_at, exc.message)
    except TrackerApiError as exc:
        worklog_submission_finish(conn, submission_id, "failed", error=exc.message)
        logger.info("timesheet: submit %s %s %s failed (%s)", entry.key, entry.day, entry.minutes, exc.status)
        return _result(entry, "failed", exc.message)
    known_ids.add(tracker_id)
    worklog_submission_finish(conn, submission_id, "done", tracker_id=tracker_id or None)
    timesheet_cell_set(conn, entry.key, entry.day.isoformat(), None)
    logger.info("timesheet: submit %s %s %s done", entry.key, entry.day, entry.minutes)
    return _result(entry, "done")


async def _confirm(
    conn, tracker: TrackerClient, entry: Entry, submission_id: int, identities: set[str], known_ids: set[str],
    since: datetime, message: str,
) -> dict[str, Any]:
    try:
        worklogs = await tracker.issue_worklogs(entry.key, identities)
    except TrackerApiError:
        return _result(entry, "unknown", message)
    match = next((w for w in worklogs if _matches(w, entry.key, entry.day, entry.minutes, since, known_ids)), None)
    if match is None:
        return _result(entry, "unknown", message)
    known_ids.add(match.id)
    worklog_submission_finish(conn, submission_id, "done", tracker_id=match.id)
    timesheet_cell_set(conn, entry.key, entry.day.isoformat(), None)
    logger.info("timesheet: submit %s %s %s confirmed after uncertainty", entry.key, entry.day, entry.minutes)
    return _result(entry, "done")
