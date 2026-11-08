from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from mrwatch.config import Config, default_config_path, load_config
from mrwatch.db import get_review, list_mrs, retry_review, sync_projects

logger = logging.getLogger(__name__)

router = APIRouter()

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _ctx(request: Request):
    return request.app.state.ctx


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    ctx = _ctx(request)
    mrs = list_mrs(ctx.conn)
    return templates.TemplateResponse(
        request, "index.html", {"mrs": mrs, "project_ids": ctx.project_ids}
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
        {"review": review, "mr": mr, "siblings": siblings},
    )


@router.post("/force-check")
async def force_check(request: Request):
    if request.headers.get("x-requested-with") != "mrwatch":
        return PlainTextResponse("forbidden", status_code=403)
    ctx = _ctx(request)
    try:
        config = load_config(default_config_path())
        mapping = sync_projects(
            ctx.conn,
            [
                (p.gitlab_project_id, p.slug, p.agent, p.re_review_on_push, True)
                for p in config.projects
            ],
        )
    except Exception:
        logger.exception("force-check: config reload failed")
        return PlainTextResponse(
            "config error (подробности в logs/app.log)", status_code=400
        )
    ctx.project_ids = mapping
    ctx.config = config
    poller = getattr(request.app.state, "poller", None)
    if poller is not None:
        poller.reload(config, mapping)
        poller.wake()
    return RedirectResponse("/", status_code=303)


@router.post("/reviews/{review_id}/retry")
async def review_retry(request: Request, review_id: int):
    if request.headers.get("x-requested-with") != "mrwatch":
        return PlainTextResponse("forbidden", status_code=403)
    ctx = _ctx(request)
    retry_review(ctx.conn, review_id)
    return RedirectResponse(f"/reviews/{review_id}", status_code=303)


def _config_path() -> Path:
    return default_config_path()


def _atomic_write_yaml(path: Path, data: dict) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


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
    agents_dir = Path("~/.config/opencode/agent").expanduser()
    names = sorted(p.stem for p in agents_dir.glob("*.md"))
    return names or ["mr-watch-reviewer"]


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
        },
    )


@router.get("/project-agents")
async def project_agents(request: Request, slug: str):
    ctx = _ctx(request)
    try:
        detail = await ctx.api.get_project_by_path(
            _normalize_slug(slug, ctx.config.gitlab.url)
        )
        branch = detail.get("default_branch") or "main"
        agents: list[str] = []
        for dir_name in (".opencode/agents", ".opencode/agent"):
            try:
                tree = await ctx.api.get_project_tree(detail["id"], dir_name, branch)
            except Exception:
                continue
            agents.extend(
                entry["name"][: -len(".md")]
                for entry in tree
                if entry.get("type") == "blob" and entry["name"].endswith(".md")
            )
        if agents:
            return JSONResponse({"agents": sorted(set(agents))})
        return JSONResponse(
            {"agents": [], "error": "в репо проекта нет агентов (.opencode/agents|agent/*.md)"}
        )
    except Exception as exc:
        logger.warning("project-agents failed: %s", exc)
        return JSONResponse(
            {"agents": [], "error": "не удалось получить агенты проекта из репо"},
            status_code=400,
        )


@router.post("/settings")
async def settings_save(request: Request):
    if request.headers.get("x-requested-with") != "mrwatch":
        return PlainTextResponse("forbidden", status_code=403)
    try:
        body = await request.json()
    except Exception:
        return PlainTextResponse("неверное тело запроса", status_code=400)
    path = _config_path()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("settings: config read failed")
        return PlainTextResponse("config.yaml не читается, правки не сохранены", status_code=400)
    if not isinstance(raw, dict) or not isinstance(raw.get("gitlab"), dict):
        return PlainTextResponse("config.yaml повреждён, правки не сохранены", status_code=400)
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
    except Exception as exc:
        logger.warning("settings: project resolve failed: %s", exc)
        return PlainTextResponse(
            "не удалось определить проект в GitLab: проверьте ссылку/slug и права токена",
            status_code=400,
        )
    try:
        config = Config.model_validate(raw)
    except Exception as exc:
        logger.warning("settings: invalid config: %s", exc)
        return PlainTextResponse("невалидные значения: проверьте числа и заполненность проектов", status_code=400)
    try:
        backup = path.with_suffix(".yaml.bak")
        if path.is_file():
            backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        _atomic_write_yaml(path, raw)
    except Exception:
        logger.exception("settings: config write failed")
        return PlainTextResponse("не удалось записать config.yaml", status_code=400)
    try:
        _apply_config(request, config)
    except Exception:
        logger.exception("settings: apply failed")
        return PlainTextResponse(
            "конфиг сохранён, но не применён — нажмите «Проверить сейчас»",
            status_code=500,
        )
    return RedirectResponse("/settings?saved=1", status_code=303)
