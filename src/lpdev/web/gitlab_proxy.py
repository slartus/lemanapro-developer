from __future__ import annotations

import hmac
import json
import logging
import re

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
        rf"/projects/{_SEGMENT}/merge_requests/\d+/(approvals|discussions)",
        rf"/projects/{_SEGMENT}/repository/(branches|compare)",
        rf"/projects/{_SEGMENT}/pipelines/\d+/(jobs|bridges)",
        rf"/projects/{_SEGMENT}/repository/commits/{_SEGMENT}/statuses",
    )
)
FORBIDDEN_GRAPHQL_OPERATIONS = re.compile(r"\b(mutation|subscription)\b")
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
    return bool(expected) and bool(presented) and hmac.compare_digest(presented, expected)


def _raw_upstream_path(request: Request) -> str:
    return request.scope["raw_path"].decode("latin-1")[len(PROXY_PREFIX):]


def _is_allowed_rest_path(path: str) -> bool:
    if any(segment in (".", "..") for segment in path.split("/")) or "%2e" in path.lower():
        return False
    return any(pattern.match(path) for pattern in ALLOWED_REST_PATHS)


def _is_read_only_graphql(body: bytes) -> bool:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, ValueError):
        return False
    query = payload.get("query") if isinstance(payload, dict) else None
    return isinstance(query, str) and not FORBIDDEN_GRAPHQL_OPERATIONS.search(query)


async def _forward(request: Request, url: str, content: bytes | None = None, graphql: bool = False) -> Response:
    api = request.app.state.ctx.api
    try:
        upstream = await api.forward(request.method, url, content=content, graphql=graphql)
    except GitlabApiError as exc:
        logger.warning("gitlab proxy: %s", exc.message)
        return PlainTextResponse("bad gateway", status_code=502)
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
    return await _forward(request, f"{path}?{query}" if query else path)


@router.post(PROXY_PREFIX + "/api/graphql", include_in_schema=False)
async def proxy_graphql(request: Request) -> Response:
    if not _is_authorized(request):
        return _forbidden()
    body = await request.body()
    if len(body) > MAX_GRAPHQL_BODY_BYTES or not _is_read_only_graphql(body):
        return _forbidden()
    return await _forward(request, "/api/graphql", content=body, graphql=True)
