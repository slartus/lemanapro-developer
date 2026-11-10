from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from jinja2 import pass_context
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from lpdev.agents import list_readonly_agents
from lpdev.config import Config, default_config_path, load_config
from lpdev.db import (
    create_question,
    create_review,
    get_review,
    has_active_question,
    list_mrs,
    list_questions,
    question_rejection_reason,
    retry_review,
    sync_projects,
)
from lpdev.gitlab_client import GitlabApiError
from lpdev.review_findings import layout_review, load_snippets
from lpdev.web.radar_routes import radar_payload, ui_state_payload

logger = logging.getLogger(__name__)

router = APIRouter()

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

FAVICON_PATH = Path(__file__).parent / "static" / "favicon.svg"

MAX_QUESTION_CHARS = 4000
MAX_INSTRUCTIONS_CHARS = 2000
CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


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
    gitlab_url = ctx.config.gitlab.url
    poller = getattr(request.app.state, "poller", None)
    if poller is not None:
        poller.request_refresh()
    radar = getattr(request.app.state, "radar", None)
    if radar is not None:
        radar.request_refresh()
    initial = {
        "gitlabUrl": gitlab_url,
        "mrs": [_mr_payload(row, gitlab_url) for row in list_mrs(ctx.conn)],
        "radar": radar_payload(ctx),
        "userState": ui_state_payload(ctx, "radar"),
        "reviewProjects": sorted(ctx.project_ids),
    }
    return templates.TemplateResponse(request, "board.html", {"initial": initial})


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
    rows, orphans = await asyncio.to_thread(
        layout_review, review["review_md"], load_snippets(review["snippets_json"])
    )
    questions = await _question_items(ctx.conn, review)
    return templates.TemplateResponse(
        request, "review.html",
        {"review": review, "mr": mr, "siblings": siblings,
         "rows": rows, "snippets": orphans, "questions": questions,
         "max_question_chars": MAX_QUESTION_CHARS,
         "max_instructions_chars": MAX_INSTRUCTIONS_CHARS},
    )


def _layout_answers(questions: list, session_id: str | None) -> list[dict]:
    return [
        {
            "q": q,
            "rows": layout_review(q["answer_md"], [])[0],
            "previous": q["root_session_id"] != session_id,
        }
        for q in questions
    ]


async def _question_items(conn, review) -> list[dict]:
    return await asyncio.to_thread(
        _layout_answers, list_questions(conn, review["id"]), review["session_id"]
    )


def normalize_user_text(value: str) -> str:
    text = unicodedata.normalize("NFC", value)
    return CONTROL_CHARS_RE.sub("", text).strip()


async def _read_json_body(request: Request) -> tuple[dict | None, bool]:
    raw = await request.body()
    if not raw.strip():
        return None, True
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None, False
    if not isinstance(body, dict):
        return None, False
    return body, True


@router.get("/reviews/{review_id}/questions", response_class=HTMLResponse)
async def review_questions(request: Request, review_id: int):
    ctx = _ctx(request)
    review = get_review(ctx.conn, review_id)
    if review is None:
        return PlainTextResponse("not found", status_code=404)
    questions = await _question_items(ctx.conn, review)
    return templates.TemplateResponse(
        request, "_qa.html", {"review": review, "questions": questions},
    )


@router.post("/reviews/{review_id}/questions")
async def review_ask(request: Request, review_id: int):
    if request.headers.get("x-requested-with") != "lpdev":
        return PlainTextResponse("forbidden", status_code=403)
    ctx = _ctx(request)
    if get_review(ctx.conn, review_id) is None:
        return PlainTextResponse("not found", status_code=404)
    body, ok = await _read_json_body(request)
    if not ok or body is None or not isinstance(body.get("question"), str):
        return PlainTextResponse("неверное тело запроса", status_code=400)
    question = normalize_user_text(body["question"])
    if not question:
        return PlainTextResponse("вопрос пустой", status_code=400)
    if len(question) > MAX_QUESTION_CHARS:
        return PlainTextResponse(
            f"вопрос длиннее {MAX_QUESTION_CHARS} символов", status_code=413
        )
    question_id = create_question(ctx.conn, review_id, question)
    if question_id is None:
        return PlainTextResponse(
            question_rejection_reason(ctx.conn, review_id), status_code=409
        )
    _wake_question_worker(request)
    return JSONResponse({"id": question_id}, status_code=201)


@router.post("/force-check")
async def force_check(request: Request):
    if request.headers.get("x-requested-with") != "lpdev":
        return PlainTextResponse("forbidden", status_code=403)
    try:
        config = load_config(default_config_path())
        _warn_if_workers_changed(request, config)
        _apply_config(request, config, force=True)
    except Exception:
        logger.exception("force-check: config reload failed")
        return PlainTextResponse(
            "config error (подробности в logs/app.log)", status_code=400
        )
    return RedirectResponse("/", status_code=303)


@router.post("/reviews/{review_id}/retry")
async def review_retry(request: Request, review_id: int):
    if request.headers.get("x-requested-with") != "lpdev":
        return PlainTextResponse("forbidden", status_code=403)
    ctx = _ctx(request)
    review = get_review(ctx.conn, review_id)
    if review is None:
        return PlainTextResponse("not found", status_code=404)
    if review["status"] in ACTIVE_STATUSES:
        return PlainTextResponse("ревью уже выполняется", status_code=409)
    body, ok = await _read_json_body(request)
    if not ok:
        return PlainTextResponse("неверное тело запроса", status_code=400)
    raw_instructions = (body or {}).get("instructions")
    if raw_instructions is not None and not isinstance(raw_instructions, str):
        return PlainTextResponse("неверное тело запроса", status_code=400)
    instructions = normalize_user_text(raw_instructions or "")
    if len(instructions) > MAX_INSTRUCTIONS_CHARS:
        return PlainTextResponse(
            f"инструкции длиннее {MAX_INSTRUCTIONS_CHARS} символов", status_code=413
        )
    if retry_review(ctx.conn, review_id, instructions or None):
        _wake_workers(request)
    elif has_active_question(ctx.conn, review_id):
        return PlainTextResponse(
            "по ревью обрабатывается вопрос, дождитесь ответа", status_code=409
        )
    return RedirectResponse(f"/reviews/{review_id}", status_code=303)


ACTIVE_STATUSES = ("queued", "cloning", "reviewing")


def _find_mr(conn, project_db_id: int, iid: int):
    return conn.execute(
        "SELECT mr.id, mr.head_sha, mr.state, project.agent FROM mr JOIN project ON project.id = mr.project_id "
        "WHERE mr.project_id = ? AND mr.iid = ?",
        (project_db_id, iid),
    ).fetchone()


@router.post("/api/mr/{gitlab_project_id}/{iid}/review")
async def start_mr_review(request: Request, gitlab_project_id: int, iid: int):
    if request.headers.get("x-requested-with") != "lpdev":
        return PlainTextResponse("forbidden", status_code=403)
    ctx = _ctx(request)
    project_db_id = ctx.project_ids.get(gitlab_project_id)
    if project_db_id is None:
        return PlainTextResponse("проект не настроен для авторевью", status_code=404)
    poller = getattr(request.app.state, "poller", None)
    queued_by_ingest = 0
    detail = None
    if poller is not None:
        try:
            detail = await ctx.api.get_project_mr(gitlab_project_id, iid)
        except GitlabApiError as exc:
            if _find_mr(ctx.conn, project_db_id, iid) is None:
                return PlainTextResponse(f"GitLab: {exc.message}", status_code=502)
        else:
            queued_by_ingest = poller.ingest([{**detail, "project_id": gitlab_project_id}])
    mr = _find_mr(ctx.conn, project_db_id, iid)
    if mr is None and detail is not None and detail.get("state") not in (None, "opened"):
        return PlainTextResponse("MR не открыт", status_code=409)
    if mr is None:
        if poller is not None:
            poller.request_refresh(force=True)
        return PlainTextResponse(
            "в таблице нет этого MR — сначала нажми «Проверить сейчас» и повтори", status_code=409
        )
    active = ctx.conn.execute(
        f"SELECT id FROM review WHERE mr_id = ? AND status IN ({','.join('?' * len(ACTIVE_STATUSES))})"
        " ORDER BY id DESC LIMIT 1",
        (mr["id"], *ACTIVE_STATUSES),
    ).fetchone()
    if active and queued_by_ingest:
        _wake_workers(request)
        return JSONResponse({"review_id": active["id"]}, status_code=201)
    if active:
        return PlainTextResponse("ревью уже выполняется", status_code=409)
    if mr["state"] not in (None, "opened"):
        return PlainTextResponse("MR не открыт", status_code=409)
    review_id = create_review(ctx.conn, mr["id"], "manual", mr["head_sha"], mr["agent"])
    if review_id is None:
        existing = ctx.conn.execute(
            "SELECT id FROM review WHERE mr_id = ? AND head_sha = ? AND \"trigger\" = 'manual'",
            (mr["id"], mr["head_sha"]),
        ).fetchone()
        if existing is None or not retry_review(ctx.conn, existing["id"]):
            return PlainTextResponse("не удалось поставить ревью", status_code=409)
        review_id = existing["id"]
    _wake_workers(request)
    return JSONResponse({"review_id": review_id}, status_code=201)


MR_ROW_FIELDS = (
    "id", "iid", "gitlab_project_id", "path_with_namespace", "title", "web_url", "author_name",
    "author_username", "source_branch", "state", "updated_at",
    "latest_review_id", "latest_review_status", "latest_done_review_id",
    "crit_count", "major_count", "minor_count", "nit_count", "verdict",
)


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


def _wake_question_worker(request: Request) -> None:
    question_event = getattr(request.app.state, "question_event", None)
    if question_event is not None:
        question_event.set()


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
        "questions": [
            {"id": q["id"], "status": q["status"]}
            for q in list_questions(_ctx(request).conn, review_id)
        ],
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
        if any(existing["gitlab_project_id"] == item["gitlab_project_id"] for existing in resolved):
            logger.info("settings: project %s listed twice, keeping the first entry", item["gitlab_project_id"])
            continue
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


def _apply_config(request: Request, config: Config, force: bool = False):
    ctx = _ctx(request)
    radar_changed = (ctx.config.radar, ctx.config.gitlab.url) != (config.radar, config.gitlab.url)
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
        poller.request_refresh(force=force)
    radar = getattr(request.app.state, "radar", None)
    if radar is not None and (force or radar_changed):
        radar.request_refresh(force=force)


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
            "settings_data": {
                "gitlabUrl": config.gitlab.url,
                "agents": _list_agents(),
                "reviewProjects": [project.model_dump() for project in config.projects],
                "radar": config.radar.model_dump(),
            },
            "saved": request.query_params.get("saved") == "1",
            "restart_needed": request.query_params.get("restart") == "1",
        },
    )


@router.get("/project-agents")
async def project_agents(request: Request):
    return JSONResponse({"agents": _list_agents()})


REPORTER_ACCESS = 20
MAINTAINER_ACCESS = 40
RADAR_SETTINGS_FIELDS = (
    "enabled",
    "interval_minutes",
    "fast_refresh_while_ci",
    "project_filter_mode",
    "selected_project_ids",
    "ignored_check_names",
    "approval_threshold",
    "hidden_sections",
)


@router.get("/api/settings/projects")
async def settings_projects(request: Request):
    ctx = _ctx(request)
    failure: GitlabApiError | None = None
    try:
        async with asyncio.TaskGroup() as group:
            reporter_task = group.create_task(ctx.api.get_member_projects(REPORTER_ACCESS))
            maintainer_task = group.create_task(ctx.api.get_member_projects(MAINTAINER_ACCESS))
    except* GitlabApiError as group_error:
        failure = group_error.exceptions[0]
    if failure is not None:
        logger.warning("settings: project list failed: %s", failure)
        return PlainTextResponse("GitLab не отдал список проектов", status_code=502)
    projects = reporter_task.result()
    maintained = {project.get("id") for project in maintainer_task.result() if isinstance(project, dict)}
    return JSONResponse(
        [
            {
                "id": project["id"],
                "path": project.get("path_with_namespace") or str(project["id"]),
                "name": project.get("name") or project.get("path_with_namespace") or str(project["id"]),
                "maintainer": project["id"] in maintained,
            }
            for project in projects
            if isinstance(project, dict) and isinstance(project.get("id"), int)
        ],
        headers={"Cache-Control": "no-store"},
    )


@router.post("/settings")
async def settings_save(request: Request):
    if request.headers.get("x-requested-with") != "lpdev":
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
    review = body.get("review") if isinstance(body.get("review"), dict) else {}
    if review:
        review_section = raw.setdefault("review", {})
        if isinstance(review_section, dict):
            review_section.update(review)
    radar = body.get("radar")
    if isinstance(radar, dict):
        radar_section = raw.setdefault("radar", {})
        if not isinstance(radar_section, dict):
            radar_section = raw["radar"] = {}
        radar_section.update({key: radar[key] for key in RADAR_SETTINGS_FIELDS if key in radar})
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
    if config.radar.project_filter_mode == "selected" and not config.radar.selected_project_ids:
        return PlainTextResponse("радар: отметь хотя бы один проект или выбери «все проекты»", status_code=400)
    if isinstance(radar, dict):
        validated = config.radar.model_dump()
        raw["radar"].update({key: validated[key] for key in RADAR_SETTINGS_FIELDS if key in radar})
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
