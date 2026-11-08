from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from jinja2 import pass_context
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from mrwatch.agents import list_readonly_agents
from mrwatch.config import Config, default_config_path, load_config
from mrwatch.db import get_review, list_mrs, retry_review, sync_projects
from mrwatch.gitlab_client import GitlabApiError
from mrwatch.review_findings import load_snippets

logger = logging.getLogger(__name__)

router = APIRouter()

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

FAVICON_PATH = Path(__file__).parent / "static" / "favicon.svg"


@lru_cache(maxsize=1)
def _favicon_bytes() -> bytes:
    return FAVICON_PATH.read_bytes()


@router.get("/favicon.svg", include_in_schema=False)
@router.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(
        _favicon_bytes(),
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


def safe_url(value: str | None, gitlab_url: str) -> str:
    if not value:
        return ""
    parsed = urlsplit(value)
    allowed = urlsplit(gitlab_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return ""
    if parsed.hostname.lower() != (allowed.hostname or "").lower():
        return ""
    return value


@pass_context
def _safe_url_filter(context, value: str | None) -> str:
    return safe_url(value, context["request"].app.state.ctx.config.gitlab.url)


templates.env.filters["safe_url"] = _safe_url_filter


def _ctx(request: Request):
    return request.app.state.ctx


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    ctx = _ctx(request)
    mrs = list_mrs(ctx.conn)
    return templates.TemplateResponse(
        request,
        "index.html",
        {"mrs": mrs, "project_ids": ctx.project_ids, "role_labels": ROLE_LABELS},
    )


@router.get("/reviews/{review_id}", response_class=HTMLResponse)
async def review_detail(request: Request, review_id: int):
    ctx = _ctx(request)
    review = get_review(ctx.conn, review_id)
    if review is None:
        return PlainTextResponse("not found", status_code=404)
    mr = ctx.conn.execute(
        """
        SELECT mr.*, project.path_with_namespace, project.gitlab_project_id
        FROM mr JOIN project ON project.id = mr.project_id WHERE mr.id = ?
        """,
        (review["mr_id"],),
    ).fetchone()
    siblings = ctx.conn.execute(
        "SELECT id, status, created_at FROM review WHERE mr_id = ? ORDER BY id DESC",
        (review["mr_id"],),
    ).fetchall()
    return templates.TemplateResponse(
        request, "review.html",
        {"review": review, "mr": mr, "siblings": siblings,
         "snippets": load_snippets(review["snippets_json"])},
    )


@router.post("/force-check")
async def force_check(request: Request):
    if request.headers.get("x-requested-with") != "mrwatch":
        return PlainTextResponse("forbidden", status_code=403)
    try:
        config = load_config(default_config_path())
        _warn_if_workers_changed(request, config)
        _apply_config(request, config)
    except Exception:
        logger.exception("force-check: config reload failed")
        return PlainTextResponse(
            "config error (подробности в logs/app.log)", status_code=400
        )
    return RedirectResponse("/", status_code=303)


@router.post("/reviews/{review_id}/retry")
async def review_retry(request: Request, review_id: int):
    if request.headers.get("x-requested-with") != "mrwatch":
        return PlainTextResponse("forbidden", status_code=403)
    ctx = _ctx(request)
    review = get_review(ctx.conn, review_id)
    if review is None:
        return PlainTextResponse("not found", status_code=404)
    if review["status"] in ACTIVE_STATUSES:
        return PlainTextResponse("ревью уже выполняется", status_code=409)
    if retry_review(ctx.conn, review_id):
        _wake_workers(request)
    return RedirectResponse(f"/reviews/{review_id}", status_code=303)


ACTIVE_STATUSES = ("queued", "cloning", "reviewing")
MR_ROW_FIELDS = (
    "id", "iid", "path_with_namespace", "title", "web_url", "author_name",
    "author_username", "source_branch", "state", "updated_at",
    "latest_review_id", "latest_review_status", "latest_done_review_id",
    "crit_count", "major_count", "minor_count", "nit_count", "verdict",
)


ROLE_LABELS = {
    "reviewer": "ревьюер",
    "mentioned": "упомянули",
    "assignee": "назначен",
    "author": "мой MR",
}


def _mr_payload(row, gitlab_url: str) -> dict:
    payload = {field: row[field] for field in MR_ROW_FIELDS}
    payload["is_draft"] = bool(row["is_draft"])
    payload["mention_url"] = safe_url(row["mention_url"], gitlab_url)
    payload["my_roles"] = [role for role in (row["my_roles"] or "").split(",") if role]
    return payload


def _wake_workers(request: Request) -> None:
    work_event = getattr(request.app.state, "work_event", None)
    if work_event is not None:
        work_event.set()


@router.get("/api/reviews/{review_id}")
async def api_review(request: Request, review_id: int):
    review = get_review(_ctx(request).conn, review_id)
    if review is None:
        return PlainTextResponse("not found", status_code=404)
    return JSONResponse({
        "id": review["id"],
        "status": review["status"],
        "error": review["error"],
        "finished_at": review["finished_at"],
        "verdict": review["verdict"],
    })


@router.get("/api/mrs")
async def api_mrs(request: Request):
    ctx = _ctx(request)
    gitlab_url = ctx.config.gitlab.url
    return JSONResponse([_mr_payload(row, gitlab_url) for row in list_mrs(ctx.conn)])


def _config_path() -> Path:
    return default_config_path()


def _write_private(path: Path, text: str, exclusive: bool = True) -> None:
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())


def _atomic_write_yaml(path: Path, data: dict) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.unlink(missing_ok=True)
    try:
        _write_private(tmp, yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _write_backup(path: Path, backup: Path) -> None:
    backup.unlink(missing_ok=True)
    _write_private(backup, path.read_text(encoding="utf-8"))


def _normalize_slug(slug: str, gitlab_url: str) -> str:
    slug = slug.strip().rstrip("/")
    if "://" in slug:
        slug = slug.split("://", 1)[-1]
    host_part = gitlab_url.split("://", 1)[-1]
    if slug.startswith(host_part + "/"):
        slug = slug[len(host_part):]
    elif "://" not in slug and "." in slug.split("/", 1)[0] and "/" in slug:
        slug = slug.split("/", 1)[-1]
    return slug.strip("/").removesuffix(".git")


async def _resolve_projects(request: Request, projects: list[dict], gitlab_url: str) -> list[dict]:
    ctx = _ctx(request)
    api = ctx.api
    resolved = []
    for item in projects:
        item = dict(item)
        slug = _normalize_slug(str(item.get("slug") or ""), gitlab_url)
        item["slug"] = slug
        pid = item.get("gitlab_project_id")
        if not pid:
            detail = await api.get_project_by_path(slug)
            item["gitlab_project_id"] = detail["id"]
            item["slug"] = str(detail.get("path_with_namespace") or slug)
        elif not slug:
            detail = await api.get_project_by_path(str(pid))
            item["slug"] = str(detail.get("path_with_namespace") or "")
        resolved.append(item)
    return resolved


def _warn_if_workers_changed(request: Request, config: Config) -> bool:
    running = getattr(request.app.state, "worker_count", None)
    if running is None or running == config.review.max_parallel:
        return False
    logger.warning(
        "max_parallel changed %s -> %s: restart required to apply",
        running, config.review.max_parallel,
    )
    return True


def _apply_config(request: Request, config: Config):
    ctx = _ctx(request)
    mapping = sync_projects(
        ctx.conn,
        [
            (p.gitlab_project_id, p.slug, p.agent, p.re_review_on_push, True)
            for p in config.projects
        ],
    )
    ctx.config = config
    ctx.project_ids = mapping
    poller = getattr(request.app.state, "poller", None)
    if poller is not None:
        poller.reload(config, mapping)
        poller.wake()


def _list_agents() -> list[str]:
    return list_readonly_agents() or ["mr-watch-reviewer"]


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    ctx = _ctx(request)
    try:
        config = load_config(_config_path())
        error = None
    except Exception as exc:
        config = ctx.config
        error = "не удалось прочитать config.yaml, правки не сохранятся"
        logger.exception("settings: config read failed: %s", exc)
    return templates.TemplateResponse(
        request, "settings.html",
        {
            "config": config,
            "config_error": error,
            "agents": _list_agents(),
            "saved": request.query_params.get("saved") == "1",
            "restart_needed": request.query_params.get("restart") == "1",
        },
    )


@router.get("/project-agents")
async def project_agents(request: Request):
    return JSONResponse({"agents": _list_agents()})


@router.post("/settings")
async def settings_save(request: Request):
    if request.headers.get("x-requested-with") != "mrwatch":
        return PlainTextResponse("forbidden", status_code=403)
    try:
        body = await request.json()
    except Exception:
        return PlainTextResponse("неверное тело запроса", status_code=400)
    if not isinstance(body, dict):
        return PlainTextResponse("неверное тело запроса", status_code=400)
    path = _config_path()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("settings: config read failed")
        return PlainTextResponse("config.yaml не читается, правки не сохранены", status_code=400)
    if not isinstance(raw, dict) or not isinstance(raw.get("gitlab"), dict):
        return PlainTextResponse("config.yaml повреждён, правки не сохранены", status_code=400)
    previous_projects = raw.get("projects")
    gitlab = body.get("gitlab") if isinstance(body.get("gitlab"), dict) else {}
    review = body.get("review") if isinstance(body.get("review"), dict) else {}
    if "poll_interval_sec" in gitlab:
        raw["gitlab"]["poll_interval_sec"] = gitlab["poll_interval_sec"]
    if review:
        review_section = raw.setdefault("review", {})
        if isinstance(review_section, dict):
            review_section.update(review)
    logger.info(
        "settings: saving projects: %s",
        [
            (p.get("gitlab_project_id"), p.get("agent"))
            for p in (body.get("projects") or [])
        ],
    )
    try:
        raw["projects"] = await _resolve_projects(
            request, body.get("projects") or [], raw["gitlab"]["url"]
        )
    except GitlabApiError as exc:
        logger.warning("settings: project resolve failed: %s", exc)
        if exc.status is None or exc.status >= 500:
            return PlainTextResponse(
                "GitLab недоступен или вернул ошибку, повторите позже", status_code=502
            )
        return PlainTextResponse(
            "не удалось определить проект в GitLab: проверьте ссылку/slug и права токена",
            status_code=400,
        )
    except Exception as exc:
        logger.warning("settings: project resolve failed: %s", exc)
        return PlainTextResponse(
            "не удалось определить проект в GitLab: проверьте ссылку/slug и права токена",
            status_code=400,
        )
    previous = {
        p.get("gitlab_project_id"): p
        for p in (previous_projects or [])
        if isinstance(p, dict)
    }
    for item in raw["projects"]:
        old = previous.get(item.get("gitlab_project_id"), {})
        for key in ("model", "variant"):
            if key not in item and key in old:
                item[key] = old[key]
    try:
        config = Config.model_validate(raw)
    except Exception as exc:
        logger.warning("settings: invalid config: %s", exc)
        return PlainTextResponse("невалидные значения: проверьте числа и заполненность проектов", status_code=400)
    try:
        backup = path.with_suffix(".yaml.bak")
        if path.is_file():
            _write_backup(path, backup)
        _atomic_write_yaml(path, raw)
    except Exception:
        logger.exception("settings: config write failed")
        return PlainTextResponse("не удалось записать config.yaml", status_code=400)
    restart_needed = _warn_if_workers_changed(request, config)
    try:
        _apply_config(request, config)
    except Exception:
        logger.exception("settings: apply failed")
        return PlainTextResponse(
            "конфиг сохранён, но не применён — нажмите «Проверить сейчас»",
            status_code=500,
        )
    suffix = "&restart=1" if restart_needed else ""
    return RedirectResponse(f"/settings?saved=1{suffix}", status_code=303)
