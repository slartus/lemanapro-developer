from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
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
    worklog_submission_attempted,
    worklog_submission_finish,
    worklog_submissions,
)
from lpdev.gitlab_client import GitlabApiError
from lpdev.timesheet import (
    WeekInputs,
    EventContext,
    PrLink,
    bucket_events,
    build_week,
    event_day,
    event_keys,
    is_ignored_push,
    mr_ref,
    parse_mr_links,
    push_ref,
    parse_week,
    queue_of,
    resolve_pr_links,
    status_intervals,
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
    worklog_author,
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
RECONCILE_MIN_AGE = timedelta(minutes=2)
MAX_RECONCILE_ON_READ = 20
RECONCILE_ON_READ_TIMEOUT_SEC = 10.0
PERMANENT_RECONCILE_STATUSES = frozenset({403, 404})
MAX_IN_PROGRESS = 5
WORKLOG_START = dt_time(9, 0)
OPEN_SUBMISSION_STATUSES = ("pending", "unknown")
MAX_GITLAB_LOOKUPS = 50
MAX_GITLAB_PARALLEL = 6
LOOKUP_CACHE_SIZE = 5000
BRANCH_LOOKUP_TTL_SEC = 600.0
PR_LINKS_TTL_SEC = 600.0
LOOKUP_FAILURE_TTL_SEC = 60.0
HISTORY_TTL_SEC = 600.0
MAX_HISTORY_PARALLEL = 6
MAX_HISTORY_KEYS = 30
USER_LOOKUP_WARNING = "GitLab не отдал текущего пользователя: комментарии, апрувы и мержи в своих MR не учтены"
PR_LINK_LOOKBACK = timedelta(days=7)
MAX_PR_LINK_ISSUES = 49
MAX_TRACKER_PARALLEL = 6

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


def _mr_info(mr: dict[str, Any]) -> dict[str, Any]:
    author = mr.get("author") if isinstance(mr.get("author"), dict) else {}
    return {
        "title": mr.get("title"), "source_branch": mr.get("source_branch"),
        "web_url": mr.get("web_url"), "author_username": author.get("username"),
        "iid": mr.get("iid"), "description": mr.get("description"),
    }


async def _gather_limited(coros: Iterable[Awaitable[Any]], limit: int = MAX_GITLAB_PARALLEL) -> list[Any]:
    semaphore = asyncio.Semaphore(limit)

    async def run(coro: Awaitable[Any]) -> Any:
        async with semaphore:
            return await coro

    return list(await asyncio.gather(*(run(coro) for coro in coros)))


@dataclass
class GitlabLookups:
    username: str | None = None
    mrs: dict[tuple[int, int], dict[str, Any]] = field(default_factory=dict)
    branches: dict[tuple[int, str], tuple[float, dict[str, Any] | None]] = field(default_factory=dict)
    projects: dict[int, dict[str, Any]] = field(default_factory=dict)
    failures: dict[tuple[Any, ...], float] = field(default_factory=dict)

    def _trim(self) -> None:
        for cache in (self.mrs, self.branches, self.projects, self.failures):
            if len(cache) > LOOKUP_CACHE_SIZE:
                cache.clear()

    def _failed_recently(self, marker: tuple[Any, ...]) -> bool:
        until = self.failures.get(marker)
        return until is not None and time.monotonic() < until

    def _fail(self, marker: tuple[Any, ...]) -> None:
        self.failures[marker] = time.monotonic() + LOOKUP_FAILURE_TTL_SEC

    def would_call(self, kind: str, ref: Any) -> bool:
        if self._failed_recently((kind, ref)):
            return False
        if kind == "mr":
            return ref not in self.mrs
        if kind == "project":
            return ref not in self.projects
        cached = self.branches.get(ref)
        return cached is None or time.monotonic() - cached[0] >= BRANCH_LOOKUP_TTL_SEC

    async def me(self, gitlab) -> str | None:
        if self.username is None and not self._failed_recently(("user",)):
            try:
                self.username = str((await gitlab.get_user()).get("username") or "") or None
            except GitlabApiError as exc:
                logger.warning("timesheet: gitlab user lookup failed: %s", exc.message)
                self._fail(("user",))
        return self.username

    async def mr(self, gitlab, ref: tuple[int, int]) -> dict[str, Any] | None:
        if ref not in self.mrs:
            if self._failed_recently(("mr", ref)):
                return None
            try:
                detail = await gitlab.get_project_mr(*ref)
            except GitlabApiError as exc:
                logger.warning("timesheet: merge request %s!%s lookup failed: %s", ref[0], ref[1], exc.message)
                self._fail(("mr", ref))
                return None
            self.mrs[ref] = _mr_info(detail if isinstance(detail, dict) else {})
        return self.mrs[ref]

    async def branch_mr(self, gitlab, ref: tuple[int, str]) -> dict[str, Any] | None:
        cached = self.branches.get(ref)
        if cached is not None and time.monotonic() - cached[0] < BRANCH_LOOKUP_TTL_SEC:
            return cached[1]
        if self._failed_recently(("branch", ref)):
            return None
        try:
            found = await gitlab.get_merge_requests_by_branch(*ref)
        except GitlabApiError as exc:
            logger.warning("timesheet: merge requests of branch %s:%s lookup failed: %s", ref[0], ref[1], exc.message)
            self._fail(("branch", ref))
            return None
        info = next((_mr_info(mr) for mr in found if isinstance(mr, dict)), None)
        self.branches[ref] = (time.monotonic(), info)
        return info

    async def project(self, gitlab, project_id: int) -> dict[str, Any]:
        if project_id not in self.projects:
            if self._failed_recently(("project", project_id)):
                return {}
            try:
                detail = await gitlab.get_project_by_path(str(project_id))
            except GitlabApiError as exc:
                logger.warning("timesheet: project %s lookup failed: %s", project_id, exc.message)
                self._fail(("project", project_id))
                return {}
            detail = detail if isinstance(detail, dict) else {}
            self.projects[project_id] = {
                "default_branch": detail.get("default_branch") if isinstance(detail.get("default_branch"), str) else None,
                "path": detail.get("path_with_namespace") if isinstance(detail.get("path_with_namespace"), str) else None,
            }
        return self.projects[project_id]

    async def context(
        self, gitlab, conn, cfg: TrackerConfig, events: list[dict[str, Any]],
        links: dict[tuple[str, int], str] | None = None, warnings: list[str] | None = None,
    ) -> EventContext:
        self._trim()
        budget = MAX_GITLAB_LOOKUPS
        me = await self.me(gitlab)
        if me is None and warnings is not None:
            warnings.append(USER_LOOKUP_WARNING)
        by_iid = _mr_lookup(conn)
        defaults: dict[int, str] = {}
        paths = _project_paths(conn)
        project_ids = {ref[0] for ref in map(push_ref, events) if ref is not None}
        if links:
            project_ids |= {
                event["project_id"] for event in events
                if isinstance(event.get("project_id"), int) and event["project_id"] not in paths
            }
        wanted_projects: list[int] = []
        for project_id in sorted(project_ids):
            if self.would_call("project", project_id) and budget <= 0:
                continue
            budget -= self.would_call("project", project_id)
            wanted_projects.append(project_id)
        details = await _gather_limited(self.project(gitlab, project_id) for project_id in wanted_projects)
        for project_id, detail in zip(wanted_projects, details):
            if detail.get("default_branch"):
                defaults[project_id] = detail["default_branch"]
            if detail.get("path"):
                paths.setdefault(project_id, detail["path"].lower())
        tracker_links: dict[tuple[int, int], str] = {}
        if links:
            by_path: dict[str, list[int]] = {}
            for project_id, path in paths.items():
                by_path.setdefault(path, []).append(project_id)
            for (path, iid), key in links.items():
                for project_id in by_path.get(path, []):
                    tracker_links[(project_id, iid)] = key
        wanted_mrs: list[tuple[int, int]] = []
        for ref in dict.fromkeys(ref for ref in map(mr_ref, events) if ref is not None):
            if ref in by_iid:
                continue
            if self.would_call("mr", ref) and budget <= 0:
                logger.warning("timesheet: merge request lookups capped, %s!%s skipped", ref[0], ref[1])
                continue
            budget -= self.would_call("mr", ref)
            wanted_mrs.append(ref)
        for ref, info in zip(wanted_mrs, await _gather_limited(self.mr(gitlab, ref) for ref in wanted_mrs)):
            if info is not None:
                by_iid[ref] = info
        by_branch: dict[tuple[int, str], dict[str, Any]] = {}
        for (project_id, _), info in by_iid.items():
            if info.get("source_branch"):
                by_branch.setdefault((project_id, info["source_branch"]), info)
        context = EventContext(
            mr_by_iid=by_iid, mr_by_branch=by_branch, me=me,
            ignored_push_branches=tuple(cfg.ignored_push_branches), default_branches=defaults, queues=tuple(cfg.queues),
            tracker_links=tracker_links,
        )
        wanted_branches: list[tuple[int, str]] = []
        for event in events:
            ref = push_ref(event)
            if ref is None or ref in by_branch or ref in wanted_branches or is_ignored_push(event, context) or event_keys(event, context):
                continue
            if self.would_call("branch", ref) and budget <= 0:
                logger.warning("timesheet: branch lookups capped, %s:%s skipped", ref[0], ref[1])
                continue
            budget -= self.would_call("branch", ref)
            wanted_branches.append(ref)
        for ref, info in zip(wanted_branches, await _gather_limited(self.branch_mr(gitlab, ref) for ref in wanted_branches)):
            if info is not None:
                by_branch[ref] = info
        return context


@dataclass
class TimesheetState:
    tracker_factory: TrackerFactory = _default_tracker_factory
    tracker: TrackerClient | None = None
    tracker_cfg: TrackerConfig | None = None
    me: dict[str, Any] | None = None
    events: dict[tuple[date, date], tuple[float, list[dict[str, Any]]]] = field(default_factory=dict)
    submit_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tracker_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    gitlab: GitlabLookups = field(default_factory=lambda: GitlabLookups())
    links: dict[date, tuple[float, dict[tuple[str, int], str]]] = field(default_factory=dict)
    histories: dict[str, tuple[float, list[tuple[datetime, str | None, str | None]] | None]] = field(default_factory=dict)
    issue_links: dict[str, tuple[str, list[tuple[str, int, bool]]]] = field(default_factory=dict)

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
                self.links.clear()
                self.histories.clear()
                self.issue_links.clear()
            return self.tracker

    async def pr_links(
        self, tracker: TrackerClient, cfg: TrackerConfig, gitlab_url: str, since: date
    ) -> dict[tuple[str, int], str]:
        cached = self.links.get(since)
        if cached is not None and time.monotonic() - cached[0] < PR_LINKS_TTL_SEC:
            return cached[1]
        try:
            identities = user_identities(await self.myself(tracker))
            query = f'Assignee: me() Updated: >= "{since.isoformat()}" "Sort by": Updated DESC'
            issues = await tracker.search_issues(query, MAX_PR_LINK_ISSUES)
        except TrackerApiError as exc:
            logger.warning("timesheet: tracker merge request links skipped: %s", exc.message)
            return {}
        semaphore = asyncio.Semaphore(MAX_TRACKER_PARALLEL)

        async def issue_links(issue: dict[str, Any]) -> list[PrLink]:
            key = issue.get("key")
            if not valid_issue_key(key) or (cfg.queues and queue_of(key) not in cfg.queues):
                return []
            assignee = issue.get("assignee") if isinstance(issue.get("assignee"), dict) else {}
            assigned = bool(identities & {str(assignee.get(name)) for name in ("id", "login", "uid") if assignee.get(name)})
            updated = str(issue.get("updatedAt") or "")
            cached = self.issue_links.get(key)
            if cached is not None and updated and cached[0] == updated:
                return [PrLink(path, iid, key, from_bot, assigned) for path, iid, from_bot in cached[1]]
            try:
                async with semaphore:
                    comments = await tracker.issue_comments(key)
            except TrackerApiError as exc:
                logger.warning("timesheet: comments of %s skipped: %s", key, exc.message)
                return []
            parsed: list[tuple[str, int, bool]] = []
            for comment in comments:
                author = comment.get("createdBy") if isinstance(comment.get("createdBy"), dict) else {}
                name = author.get("login") or author.get("display")
                parsed.extend((path, iid, name in cfg.pr_link_bots) for path, iid in parse_mr_links(comment.get("text"), gitlab_url))
            if updated:
                if len(self.issue_links) > LOOKUP_CACHE_SIZE:
                    self.issue_links.clear()
                self.issue_links[key] = (updated, parsed)
            return [PrLink(path, iid, key, from_bot, assigned) for path, iid, from_bot in parsed]

        found = [link for links_of in await asyncio.gather(*(issue_links(issue) for issue in issues)) for link in links_of]
        links = resolve_pr_links(found)
        self.links[since] = (time.monotonic(), links)
        while len(self.links) > EVENTS_CACHE_SIZE:
            self.links.pop(min(self.links, key=lambda item: self.links[item][0]))
        return links

    async def status_history(self, tracker: TrackerClient, key: str) -> list[tuple[datetime, str | None, str | None]] | None:
        cached = self.histories.get(key)
        if cached is not None:
            ttl = HISTORY_TTL_SEC if cached[1] is not None else LOOKUP_FAILURE_TTL_SEC
            if time.monotonic() - cached[0] < ttl:
                return cached[1]
        try:
            history = await tracker.status_history(key)
        except TrackerApiError as exc:
            logger.warning("timesheet: status history of %s skipped: %s", key, exc.message)
            history = None
        if len(self.histories) > LOOKUP_CACHE_SIZE:
            self.histories.clear()
        self.histories[key] = (time.monotonic(), history)
        return history

    async def row_activity(
        self, tracker: TrackerClient | None, cfg: TrackerConfig, keys: list[str], days: list[date], today: date
    ) -> dict[str, list[dict[str, str]] | None]:
        if tracker is None or not cfg.activity_statuses:
            return {}
        semaphore = asyncio.Semaphore(MAX_HISTORY_PARALLEL)

        async def one(key: str) -> tuple[str, list[dict[str, str]] | None]:
            async with semaphore:
                history = await self.status_history(tracker, key)
            if history is None:
                return key, None
            intervals = status_intervals(history, cfg.activity_statuses, _tz(cfg), today)
            return key, [
                {**interval, "letter": cfg.activity_statuses[interval["status"]]}
                for interval in intervals
                if interval["from"] <= days[-1].isoformat() and interval["to"] >= days[0].isoformat()
            ]

        return dict(await asyncio.gather(*(one(key) for key in keys[:MAX_HISTORY_KEYS])))

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
        "SELECT project.gitlab_project_id, mr.iid, mr.source_branch, mr.title, mr.web_url, mr.author_username "
        "FROM mr JOIN project ON project.id = mr.project_id"
    ).fetchall()
    return {
        (row["gitlab_project_id"], row["iid"]): {
            "source_branch": row["source_branch"], "title": row["title"], "web_url": row["web_url"],
            "author_username": row["author_username"], "iid": row["iid"],
        }
        for row in rows
    }


def _project_paths(conn) -> dict[int, str]:
    rows = conn.execute("SELECT gitlab_project_id, path_with_namespace FROM project").fetchall()
    return {row["gitlab_project_id"]: row["path_with_namespace"].lower() for row in rows if row["path_with_namespace"]}


def _in_progress(issues: list[dict[str, Any]], cfg: TrackerConfig) -> dict[str, str]:
    result: dict[str, str] = {}
    for issue in issues:
        key = issue.get("key")
        if not valid_issue_key(key) or (cfg.queues and queue_of(key) not in cfg.queues):
            continue
        result[key] = str(issue.get("summary") or "")
        if len(result) >= MAX_IN_PROGRESS:
            break
    return result


def _issue_status(issue: dict[str, Any]) -> dict[str, str] | None:
    status = issue.get("status")
    if not isinstance(status, dict) or not status.get("key"):
        return None
    return {"key": str(status["key"]), "display": str(status.get("display") or status["key"])}


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
    statuses: dict[str, dict[str, str]] = field(default_factory=dict)
    error: str | None = None


async def _tracker_data(state: TimesheetState, tracker: TrackerClient, cfg: TrackerConfig, days: list[date], keys: set[str]) -> TrackerData:
    data = TrackerData()
    try:
        me = await state.myself(tracker)
        issues, data.worklogs = await asyncio.gather(
            tracker.search_my_issues(cfg.in_progress_statuses, cfg.queues, MAX_IN_PROGRESS),
            tracker.search_worklogs(worklog_author(me), days[0], days[-1]),
        )
        data.in_progress = _in_progress(issues, cfg)
        data.statuses = {key: status for issue in issues if (key := issue.get("key")) and (status := _issue_status(issue))}
        wanted = (keys | {worklog.key for worklog in data.worklogs}) - set(data.in_progress)
        issues = await tracker.get_issues(sorted(wanted)) if wanted else {}
        data.titles = {key: str(issue.get("summary") or "") for key, issue in issues.items()}
        data.statuses |= {key: status for key, issue in issues.items() if (status := _issue_status(issue))}
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
    tracker = await state.tracker_for(cfg)

    async def tracker_links() -> dict[tuple[str, int], str] | None:
        return await state.pr_links(tracker, cfg, ctx.config.gitlab.url, week_start - PR_LINK_LOOKBACK) if tracker else None

    events_result, links = await asyncio.gather(
        state.events_between(ctx.api, days[0] - timedelta(days=EVENTS_PADDING_DAYS), days[-1] + timedelta(days=EVENTS_PADDING_DAYS)),
        tracker_links(),
        return_exceptions=True,
    )
    if isinstance(links, BaseException):
        if not isinstance(links, Exception):
            raise links
        logger.warning("timesheet: tracker merge request links failed: %s", links)
        links = None
    if isinstance(events_result, GitlabApiError):
        logger.warning("timesheet: gitlab events failed: %s", events_result.message)
        return _error("GitLab не отдал события, повторите позже", 502)
    if isinstance(events_result, BaseException):
        raise events_result
    week_events = [event for event in events_result if isinstance(event, dict) and event_day(event, tz) in days]
    warnings: list[str] = []
    context = await state.gitlab.context(ctx.api, ctx.conn, cfg, week_events, links, warnings)
    activity = bucket_events(week_events, tz, context)
    iso_days = [day.isoformat() for day in days]
    if tracker is not None:
        await _reconcile_week(ctx.conn, state, tracker, iso_days)
    edits = {(key, date.fromisoformat(day)): minutes for (key, day), minutes in timesheet_cells(ctx.conn, iso_days).items()}
    manual = timesheet_tasks(ctx.conn, week_start.isoformat())
    journal = worklog_submissions(ctx.conn, iso_days)
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
    payload = build_week(inputs)
    activity_by_key = await state.row_activity(tracker, cfg, [row["key"] for row in payload["rows"]], days, today)
    for row in payload["rows"]:
        row["activity"] = activity_by_key.get(row["key"])
        row["status"] = data.statuses.get(row["key"])
    return JSONResponse({**payload, "warnings": warnings}, headers={"Cache-Control": "no-store"})


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
    return worklog.created_at is not None and worklog.created_at >= since - RECONCILE_SLACK


@dataclass
class Reconciled:
    confirmed: set[tuple[str, date]] = field(default_factory=set)
    unresolved: set[tuple[str, date]] = field(default_factory=set)


async def _reconcile(conn, tracker: TrackerClient, identities: set[str], rows, claimed: set[str]) -> Reconciled:
    outcome = Reconciled()
    now = datetime.now(UTC)
    for row in rows:
        key, day = row["issue_key"], date.fromisoformat(row["day"])
        since = _parse_created(row["created_at"])
        worklog_submission_attempted(conn, row["id"])
        try:
            worklogs = await tracker.issue_worklogs(key, identities)
        except TrackerApiError as exc:
            if _is_permanent(exc):
                worklog_submission_finish(conn, row["id"], "failed", error=f"сверка невозможна: {exc.message}")
                logger.info("timesheet: reconcile %s %s gave up (%s)", key, day, exc.status)
                continue
            logger.warning("timesheet: reconcile %s %s failed: %s", key, day, exc.message)
            outcome.unresolved.add((key, day))
            continue
        match = next((w for w in worklogs if _matches(w, key, day, row["minutes"], since, claimed)), None)
        if match is not None:
            claimed.add(match.id)
            worklog_submission_finish(conn, row["id"], "done", tracker_id=match.id)
            timesheet_cell_set(conn, key, day.isoformat(), None)
            outcome.confirmed.add((key, day))
            logger.info("timesheet: reconciled %s %s %s as done", key, day, row["minutes"])
        elif now - since < RECONCILE_MIN_AGE:
            outcome.unresolved.add((key, day))
        else:
            worklog_submission_finish(conn, row["id"], "failed", error="не найдено в Tracker после сбоя, можно повторить")
            logger.info("timesheet: reconciled %s %s %s as failed", key, day, row["minutes"])
    outcome.unresolved -= outcome.confirmed
    return outcome


def _open_rows(journal, cells: set[tuple[str, date]] | None = None) -> list:
    return [
        row for row in journal
        if row["status"] in OPEN_SUBMISSION_STATUSES
        and (cells is None or (row["issue_key"], date.fromisoformat(row["day"])) in cells)
    ]


def _is_permanent(exc: TrackerApiError) -> bool:
    return exc.status is not None and (exc.status in PERMANENT_RECONCILE_STATUSES or 300 <= exc.status < 400)


def _rows_for_read(journal, now: datetime) -> list:
    rows = [row for row in _open_rows(journal) if now - _parse_created(row["created_at"]) >= RECONCILE_MIN_AGE]
    rows.sort(key=lambda row: (row["reconcile_attempts"], row["updated_at"] or row["created_at"], row["id"]))
    return rows[:MAX_RECONCILE_ON_READ]


def _claimed(journal) -> set[str]:
    return {row["tracker_id"] for row in journal if row["status"] == "done" and row["tracker_id"]}


async def _reconcile_week(conn, state: TimesheetState, tracker: TrackerClient, iso_days: list[str]) -> None:
    if state.submit_lock.locked():
        return
    async with state.submit_lock:
        journal = worklog_submissions(conn, iso_days)
        rows = _rows_for_read(journal, datetime.now(UTC))
        if not rows:
            return
        try:
            async with asyncio.timeout(RECONCILE_ON_READ_TIMEOUT_SEC):
                identities = user_identities(await state.myself(tracker))
                await _reconcile(conn, tracker, identities, rows, _claimed(journal))
        except TrackerApiError as exc:
            logger.warning("timesheet: reconcile skipped: %s", exc.message)
        except TimeoutError:
            logger.warning("timesheet: reconcile on read timed out after %ss", RECONCILE_ON_READ_TIMEOUT_SEC)


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
        claimed = _claimed(journal)
        reconciled = await _reconcile(conn, tracker, identities, _open_rows(journal, cells), claimed)
        worklogs = await tracker.search_worklogs(worklog_author(me), days[0], days[-1])
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
    still_open = {(row["issue_key"], date.fromisoformat(row["day"])) for row in _open_rows(journal, cells)}
    for entry in entries:
        if (entry.key, entry.day) in reconciled.confirmed:
            results.append(_result(entry, "skipped", "уже списано, подтверждено сверкой"))
            continue
        if (entry.key, entry.day) in reconciled.unresolved | still_open:
            results.append(_result(entry, "unknown", "не удалось сверить с Tracker, повторите позже"))
            continue
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
    if not tracker_id:
        message = "Tracker не вернул id worklog, результат проверяется"
        worklog_submission_finish(conn, submission_id, "unknown", error=message)
        logger.warning("timesheet: submit %s %s %s returned no id", entry.key, entry.day, entry.minutes)
        return await _confirm(conn, tracker, entry, submission_id, identities, known_ids, started_at, message)
    known_ids.add(tracker_id)
    worklog_submission_finish(conn, submission_id, "done", tracker_id=tracker_id)
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
