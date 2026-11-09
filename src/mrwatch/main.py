from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse

from mrwatch.config import Config, ResolvedPaths, default_config_path, get_pat, load_config, resolve_paths
from mrwatch.db import backup, connect, init_schema, poll_state_get, requeue_incomplete, sync_projects
from mrwatch.git_ops import cleanup_old_review_logs, cleanup_old_workspaces, cleanup_stale_askpass
from mrwatch.gitlab_client import GitLabClient
from mrwatch.poller import Poller
from mrwatch.web.routes import router as web_router
from mrwatch.worker import ReviewRegistry, start_workers

logger = logging.getLogger("mrwatch")

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
    mtimes = [p.stat().st_mtime for p in backup_dir.glob("mrwatch-*.db") if p.is_file()]
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
    backups = sorted(backup_dir.glob("mrwatch-*.db"))
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


def create_app(ctx: AppContext, port: int = DEFAULT_PORT) -> FastAPI:
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
        poller = Poller(
            ctx.conn, ctx.api, ctx.config, ctx.project_ids, work_event, registry=registry
        )
        app.state.poller = poller
        app.state.work_event = work_event
        worker_count = ctx.config.review.max_parallel
        app.state.worker_count = worker_count
        tasks = [asyncio.create_task(poller.run(), name="mrwatch-poller")]
        tasks.extend(start_workers(
            worker_count, ctx.conn, ctx.api, ctx.config,
            ctx.paths, work_event, config_provider=lambda: ctx.config,
            registry=registry,
        ))
        tasks.append(asyncio.create_task(_backup_loop(ctx), name="mrwatch-backup"))
        for task in tasks:
            _supervise(task)
        app.state.tasks = tasks
        logger.info("started: %s workers, poll interval %ss",
                    worker_count, ctx.config.gitlab.poll_interval_sec)
        yield
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await ctx.api.aclose()
        ctx.conn.close()
        logger.info("stopped")

    app = FastAPI(title="mr-watch", lifespan=lifespan)
    app.state.ctx = ctx
    app.include_router(web_router)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        last_ok = poll_state_get(ctx.conn, "poll_last_ok")
        last_error = poll_state_get(ctx.conn, "poll_last_error")
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

    app.add_middleware(NoStoreHtmlMiddleware)
    app.add_middleware(HostCheckMiddleware, allowed_hosts={
        f"127.0.0.1:{port}", f"localhost:{port}",
    })
    return app


def bootstrap() -> tuple[AppContext, int]:
    config = load_config(default_config_path())
    paths = resolve_paths(config)
    setup_logging(paths.logs)
    conn = connect(paths.db)
    init_schema(conn)
    pat = get_pat(config.gitlab)
    api = GitLabClient(config.gitlab.url, pat)
    project_ids = sync_projects(conn, [
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
