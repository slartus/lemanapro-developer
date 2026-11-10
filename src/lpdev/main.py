from __future__ import annotations

import asyncio
import logging
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse

from lpdev.config import Config, ResolvedPaths, default_config_path, get_pat, load_config, resolve_paths
from lpdev.db import backup, connect, init_schema, poll_state_get, requeue_incomplete, sync_projects
from lpdev.git_ops import cleanup_old_review_logs, cleanup_old_workspaces, cleanup_stale_askpass
from lpdev.gitlab_client import GitLabClient
from lpdev.poller import Poller
from lpdev.radar_scheduler import RUNNER_PATH, RadarScheduler
from lpdev.web.gitlab_proxy import PROXY_PREFIX
from lpdev.web.gitlab_proxy import router as gitlab_proxy_router
from lpdev.web.radar_routes import router as radar_router
from lpdev.web.routes import favicon
from lpdev.web.routes import router as web_router
from lpdev.web.timesheet_routes import TimesheetState
from lpdev.web.timesheet_routes import router as timesheet_router
from lpdev.worker import QuestionWorker, ReviewRegistry, start_workers

logger = logging.getLogger("lpdev")

DEFAULT_PORT = 8123
BACKUP_INTERVAL_SEC = 86400
BACKUP_KEEP = 7


@dataclass
class AppContext:
    conn: sqlite3.Connection
    config: Config
    paths: ResolvedPaths
    api: GitLabClient
    project_ids: dict[int, int]


class NoStoreHtmlMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            async def send_wrapper(message) -> None:
                if message["type"] == "http.response.start":
                    headers = message.setdefault("headers", [])
                    content_type = next(
                        (v for k, v in headers if k == b"content-type"), b""
                    )
                    if content_type.startswith(b"text/html"):
                        headers.append((b"cache-control", b"no-store"))
                await send(message)
            await self.app(scope, receive, send_wrapper)
            return
        await self.app(scope, receive, send)


class HostCheckMiddleware:
    def __init__(self, app, allowed_hosts: set[str]) -> None:
        self.app = app
        self.allowed_hosts = allowed_hosts

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            host = next(
                (value.decode("latin-1") for key, value in scope["headers"] if key == b"host"),
                "",
            )
            if host not in self.allowed_hosts:
                response = PlainTextResponse("forbidden", status_code=403)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
ALLOWED_FETCH_SITES = frozenset({"same-origin", "none"})


class SameOriginMiddleware:
    def __init__(self, app, allowed_hosts: set[str], protected_prefixes: tuple[str, ...] = ()) -> None:
        self.app = app
        self.allowed_origins = {f"http://{host}" for host in allowed_hosts}
        self.protected_prefixes = protected_prefixes

    def _is_protected(self, scope) -> bool:
        return scope["method"] not in SAFE_METHODS or scope["path"].startswith(self.protected_prefixes)

    def _is_same_origin(self, scope) -> bool:
        headers = {key: value.decode("latin-1") for key, value in scope["headers"]}
        fetch_site = headers.get(b"sec-fetch-site")
        if fetch_site is not None and fetch_site not in ALLOWED_FETCH_SITES:
            return False
        origin = headers.get(b"origin")
        return origin is None or origin in self.allowed_origins

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and self._is_protected(scope) and not self._is_same_origin(scope):
            response = PlainTextResponse("forbidden", status_code=403)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class RequestedWithMiddleware:
    def __init__(self, app, exempt_prefixes: tuple[str, ...] = ()) -> None:
        self.app = app
        self.exempt_prefixes = exempt_prefixes

    def _is_allowed(self, scope) -> bool:
        if scope["method"] in SAFE_METHODS or scope["path"].startswith(self.exempt_prefixes):
            return True
        return any(key == b"x-requested-with" and value == b"lpdev" for key, value in scope["headers"])

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and not self._is_allowed(scope):
            response = PlainTextResponse("forbidden", status_code=403)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def setup_logging(logs_dir: Path) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    root = logging.getLogger()
    if any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        return
    handler = RotatingFileHandler(
        logs_dir / "app.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(console)


def _backup_due(backup_dir: Path, interval_sec: float, now: float | None = None) -> bool:
    mtimes = [p.stat().st_mtime for p in backup_dir.glob("lpdev-*.db") if p.is_file()]
    if not mtimes:
        return True
    return (time.time() if now is None else now) - max(mtimes) >= interval_sec


async def _backup_loop(ctx: AppContext) -> None:
    backup_dir = ctx.paths.db.parent / "backups"
    first = True
    while True:
        if not (first and await asyncio.to_thread(_backup_due, backup_dir, BACKUP_INTERVAL_SEC)):
            await asyncio.sleep(BACKUP_INTERVAL_SEC)
        first = False
        try:
            dest = await asyncio.to_thread(backup, ctx.paths.db, ctx.paths.db.parent / "backups")
            _prune_backups(dest.parent, BACKUP_KEEP)
            logger.info("db backup written: %s", dest.name)
        except Exception:
            logger.exception("db backup failed")


def _prune_backups(backup_dir: Path, keep: int) -> None:
    backups = sorted(backup_dir.glob("lpdev-*.db"))
    for old in backups[:-keep]:
        old.unlink(missing_ok=True)


def _log_task_exit(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("background task %s died", task.get_name(), exc_info=exc)
    else:
        logger.error("background task %s exited unexpectedly", task.get_name())


def _supervise(task: asyncio.Task) -> asyncio.Task:
    task.add_done_callback(_log_task_exit)
    return task


def create_app(ctx: AppContext, port: int = DEFAULT_PORT, timesheet_only: bool = False) -> FastAPI:
    @asynccontextmanager
    async def timesheet_lifespan(app: FastAPI):
        tasks = [_supervise(asyncio.create_task(_backup_loop(ctx), name="lpdev-backup"))]
        app.state.tasks = tasks
        logger.info("started: timesheet only")
        yield
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await app.state.timesheet.aclose()
        await ctx.api.aclose()
        ctx.conn.close()
        logger.info("stopped")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        requeue_incomplete(ctx.conn)
        cleanup_old_workspaces(ctx.paths.workspaces, ctx.config.review.workspace_ttl_days)
        cleanup_old_review_logs(ctx.paths.logs, ctx.config.review.log_ttl_days)
        tmp_dir = ctx.paths.workspaces.parent / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        cleanup_stale_askpass(tmp_dir)
        work_event = asyncio.Event()
        registry = ReviewRegistry()
        radar = RadarScheduler(
            ctx.conn,
            lambda: ctx.config,
            api_base=f"http://127.0.0.1:{port}{PROXY_PREFIX}",
            proxy_secret=app.state.proxy_secret,
        )
        app.state.radar = radar
        poller = Poller(
            ctx.conn, ctx.api, ctx.config, ctx.project_ids, work_event, registry=registry,
            on_mr_updated=radar.notify_mr_updated,
        )
        app.state.poller = poller
        app.state.work_event = work_event
        worker_count = ctx.config.review.max_parallel
        app.state.worker_count = worker_count
        tasks = [asyncio.create_task(poller.run(), name="lpdev-poller")]
        tasks.extend(start_workers(
            worker_count, ctx.conn, ctx.api, ctx.config,
            ctx.paths, work_event, config_provider=lambda: ctx.config,
            registry=registry,
        ))
        question_event = asyncio.Event()
        app.state.question_event = question_event
        question_worker = QuestionWorker(
            0, ctx.conn, ctx.api, ctx.config, ctx.paths, question_event,
            config_provider=lambda: ctx.config, registry=ReviewRegistry(),
        )
        tasks.append(
            asyncio.create_task(question_worker.run(), name="lpdev-question-worker")
        )
        tasks.append(asyncio.create_task(radar.run(), name="lpdev-radar"))
        tasks.append(asyncio.create_task(_backup_loop(ctx), name="lpdev-backup"))
        for task in tasks:
            _supervise(task)
        app.state.tasks = tasks
        logger.info("started: %s workers, poll interval %ss",
                    worker_count, ctx.config.gitlab.poll_interval_sec)
        yield
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await app.state.timesheet.aclose()
        await ctx.api.aclose()
        ctx.conn.close()
        logger.info("stopped")

    app = FastAPI(title="lemanapro-developer", lifespan=timesheet_lifespan if timesheet_only else lifespan)
    app.state.ctx = ctx
    app.state.proxy_secret = secrets.token_urlsafe(32)
    app.state.timesheet = TimesheetState()
    app.state.timesheet_only = timesheet_only
    if timesheet_only:
        app.add_api_route("/favicon.svg", favicon, include_in_schema=False)
        app.add_api_route("/favicon.ico", favicon, include_in_schema=False)
        app.add_api_route("/", lambda: RedirectResponse("/timesheet"), include_in_schema=False)
    else:
        app.include_router(web_router)
        app.include_router(gitlab_proxy_router)
        app.include_router(radar_router)
        app.mount("/static/radar", StaticFiles(directory=RUNNER_PATH.parent), name="radar-static")
    app.include_router(timesheet_router)
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "web" / "static"), name="static")

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        last_ok = None if timesheet_only else poll_state_get(ctx.conn, "poll_last_ok")
        last_error = None if timesheet_only else poll_state_get(ctx.conn, "poll_last_error")
        dead_tasks = [
            task.get_name() for task in getattr(app.state, "tasks", []) if task.done()
        ]
        degraded = bool(last_error) or bool(dead_tasks)
        return JSONResponse(
            {
                "status": "degraded" if degraded else "ok",
                "last_poll": last_ok,
                "last_error": last_error,
                "dead_tasks": dead_tasks,
            },
            status_code=503 if degraded else 200,
        )

    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    app.add_middleware(NoStoreHtmlMiddleware)
    app.add_middleware(RequestedWithMiddleware, exempt_prefixes=(PROXY_PREFIX + "/",))
    app.add_middleware(SameOriginMiddleware, allowed_hosts=allowed_hosts, protected_prefixes=(PROXY_PREFIX + "/",))
    app.add_middleware(HostCheckMiddleware, allowed_hosts=allowed_hosts)
    return app


def bootstrap(timesheet_only: bool = False) -> tuple[AppContext, int]:
    config = load_config(default_config_path(), timesheet_only=timesheet_only)
    paths = resolve_paths(config)
    setup_logging(paths.logs)
    conn = connect(paths.db)
    init_schema(conn)
    pat = get_pat(config.gitlab)
    api = GitLabClient(config.gitlab.url, pat)
    project_ids = {} if timesheet_only else sync_projects(conn, [
        (p.gitlab_project_id, p.slug, p.agent, p.re_review_on_push, True)
        for p in config.projects
    ])
    return AppContext(conn=conn, config=config, paths=paths, api=api, project_ids=project_ids), DEFAULT_PORT


app = None


def get_app() -> FastAPI:
    global app
    if app is None:
        ctx, port = bootstrap()
        app = create_app(ctx, port)
    return app
