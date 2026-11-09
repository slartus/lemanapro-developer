from __future__ import annotations

import hmac
import json
import logging
import re
from urllib.parse import unquote

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, Response

from lpdev.gitlab_client import GitlabApiError

logger = logging.getLogger(__name__)

router = APIRouter()

PROXY_PREFIX = "/gl"
MAX_GRAPHQL_BODY_BYTES = 256 * 1024

_SEGMENT = r"[A-Za-z0-9_%-][A-Za-z0-9_.%-]*"
ALLOWED_REST_PATHS = tuple(
    re.compile(rf"^/api/v4{pattern}$")
    for pattern in (
        r"/user",
        r"/merge_requests",
        r"/projects",
        r"/todos",
        r"/personal_access_tokens/self",
        rf"/projects/{_SEGMENT}/merge_requests",
        rf"/projects/{_SEGMENT}/merge_requests/\d+",
        rf"/projects/{_SEGMENT}/merge_requests/\d+/(approvals|discussions)",
        rf"/projects/{_SEGMENT}/repository/(branches|compare)",
        rf"/projects/{_SEGMENT}/pipelines/\d+/(jobs|bridges)",
        rf"/projects/{_SEGMENT}/repository/commits/{_SEGMENT}/statuses",
    )
)
FORBIDDEN_GRAPHQL_OPERATIONS = re.compile(r"\b(mutation|subscription)\b")
ALLOWED_GRAPHQL_KEYS = frozenset({"query", "variables", "operationName"})
MR_LIST_PATHS = (
    re.compile(r"^/api/v4/merge_requests$"),
    re.compile(rf"^/api/v4/projects/{_SEGMENT}/merge_requests$"),
)
PASSTHROUGH_HEADERS = (
    "content-type",
    "x-next-page",
    "x-page",
    "x-per-page",
    "x-total",
    "x-total-pages",
    "retry-after",
)


def _forbidden() -> PlainTextResponse:
    return PlainTextResponse("forbidden", status_code=403)


def _presented_secret(request: Request) -> str:
    private_token = request.headers.get("private-token")
    if private_token:
        return private_token
    authorization = request.headers.get("authorization", "")
    scheme, _, value = authorization.partition(" ")
    return value if scheme.lower() == "bearer" else ""


def _is_authorized(request: Request) -> bool:
    expected = getattr(request.app.state, "proxy_secret", "")
    presented = _presented_secret(request)
    return bool(expected) and bool(presented) and hmac.compare_digest(
        presented.encode("latin-1"), expected.encode("latin-1")
    )


def _raw_upstream_path(request: Request) -> str | None:
    raw_path = request.scope.get("raw_path")
    return raw_path.decode("latin-1")[len(PROXY_PREFIX):] if raw_path else None


def _has_safe_segments(path: str) -> bool:
    segments = path.split("/")
    for index, segment in enumerate(segments):
        decoded = unquote(segment)
        if "\\" in decoded:
            return False
        is_project = index > 0 and segments[index - 1] == "projects"
        if "/" in decoded and not is_project:
            return False
        if any(part in (".", "..") for part in decoded.split("/")):
            return False
    return True


def _is_allowed_rest_path(path: str | None) -> bool:
    if not path or not _has_safe_segments(path):
        return False
    return any(pattern.match(path) for pattern in ALLOWED_REST_PATHS)


def _read_only_graphql_body(body: bytes) -> bytes | None:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, dict) or not set(payload) <= ALLOWED_GRAPHQL_KEYS:
        return None
    query = payload.get("query")
    variables = payload.get("variables")
    operation_name = payload.get("operationName")
    if not isinstance(query, str) or FORBIDDEN_GRAPHQL_OPERATIONS.search(query):
        return None
    if variables is not None and not isinstance(variables, dict):
        return None
    if operation_name is not None and not isinstance(operation_name, str):
        return None
    clean = {"query": query, "variables": variables or {}}
    if operation_name is not None:
        clean["operationName"] = operation_name
    return json.dumps(clean).encode()


def _share_merge_requests(request: Request, path: str, content: bytes) -> None:
    poller = getattr(request.app.state, "poller", None)
    if poller is None or not any(pattern.match(path) for pattern in MR_LIST_PATHS):
        return
    try:
        items = json.loads(content)
        if isinstance(items, list):
            poller.ingest(items)
    except Exception:
        logger.exception("gitlab proxy: sharing merge requests with the poller failed")


async def _forward(
    request: Request, url: str, content: bytes | None = None, graphql: bool = False, path: str | None = None
) -> Response:
    api = request.app.state.ctx.api
    try:
        upstream = await api.forward(request.method, url, content=content, graphql=graphql)
    except GitlabApiError as exc:
        logger.warning("gitlab proxy: %s", exc.message)
        return PlainTextResponse("bad gateway", status_code=502)
    if path is not None and upstream.status_code == 200:
        _share_merge_requests(request, path, upstream.content)
    headers = {name: upstream.headers[name] for name in PASSTHROUGH_HEADERS if name in upstream.headers}
    return Response(content=upstream.content, status_code=upstream.status_code, headers=headers)


@router.get(PROXY_PREFIX + "/api/v4/{rest:path}", include_in_schema=False)
async def proxy_rest(request: Request, rest: str) -> Response:
    if not _is_authorized(request):
        return _forbidden()
    path = _raw_upstream_path(request)
    if not _is_allowed_rest_path(path):
        return _forbidden()
    query = request.scope["query_string"].decode("latin-1")
    return await _forward(request, f"{path}?{query}" if query else path, path=path)


@router.post(PROXY_PREFIX + "/api/graphql", include_in_schema=False)
async def proxy_graphql(request: Request) -> Response:
    if not _is_authorized(request):
        return _forbidden()
    body = await request.body()
    clean_body = _read_only_graphql_body(body) if len(body) <= MAX_GRAPHQL_BODY_BYTES else None
    if clean_body is None:
        return _forbidden()
    return await _forward(request, "/api/graphql", content=clean_body, graphql=True)
