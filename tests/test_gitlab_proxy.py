from __future__ import annotations

import dataclasses
import json

import httpx
import pytest

from lpdev.gitlab_client import GitLabClient
from lpdev.main import create_app

REAL_PAT = "real-pat"


class Upstream:
    def __init__(self, responder=None) -> None:
        self.requests: list[httpx.Request] = []
        self.responder = responder or (lambda request: httpx.Response(200, json={"ok": True}))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responder(request)


def _app(ctx, upstream: Upstream):
    ctx_obj, _ = ctx
    api = GitLabClient("https://gitlab.example.com", REAL_PAT, transport=httpx.MockTransport(upstream))
    app = create_app(dataclasses.replace(ctx_obj, api=api))
    return app


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8123")


def _auth(app) -> dict[str, str]:
    return {"PRIVATE-TOKEN": app.state.proxy_secret}


@pytest.mark.parametrize("headers", [{}, {"PRIVATE-TOKEN": "wrong"}, {"Authorization": "Bearer wrong"}, {"PRIVATE-TOKEN": ""}])
async def test_rejects_requests_without_the_proxy_secret(ctx, headers):
    upstream = Upstream()
    app = _app(ctx, upstream)
    async with _client(app) as client:
        rest = await client.get("/gl/api/v4/user", headers=headers)
        graphql = await client.post("/gl/api/graphql", headers=headers, json={"query": "{ currentUser { id } }"})
    assert rest.status_code == 403
    assert graphql.status_code == 403
    assert upstream.requests == []


async def test_forwards_allowed_rest_path_with_raw_path_query_and_real_token(ctx):
    upstream = Upstream(
        lambda request: httpx.Response(
            200, json=[{"id": 1}], headers={"x-next-page": "2", "set-cookie": "s=1", "x-total": "3"}
        )
    )
    app = _app(ctx, upstream)
    async with _client(app) as client:
        response = await client.get(
            "/gl/api/v4/projects/group%2Fapp/pipelines/7/jobs?scope%5B%5D=failed&per_page=100&page=1",
            headers=_auth(app),
        )
    assert response.status_code == 200
    assert response.json() == [{"id": 1}]
    assert response.headers["x-next-page"] == "2"
    assert response.headers["x-total"] == "3"
    assert "set-cookie" not in response.headers
    sent = upstream.requests[0]
    assert sent.method == "GET"
    assert sent.url.raw_path == b"/api/v4/projects/group%2Fapp/pipelines/7/jobs?scope%5B%5D=failed&per_page=100&page=1"
    assert sent.headers["PRIVATE-TOKEN"] == REAL_PAT
    assert app.state.proxy_secret not in str(sent.headers)


@pytest.mark.parametrize(
    "path",
    [
        "/gl/api/v4/user",
        "/gl/api/v4/merge_requests?scope=created_by_me",
        "/gl/api/v4/projects?membership=true",
        "/gl/api/v4/todos?state=pending",
        "/gl/api/v4/personal_access_tokens/self",
        "/gl/api/v4/projects/42/merge_requests?state=opened",
        "/gl/api/v4/projects/42/merge_requests/5/approvals",
        "/gl/api/v4/projects/42/merge_requests/5/discussions",
        "/gl/api/v4/projects/42/repository/branches",
        "/gl/api/v4/projects/42/repository/compare?from=main&to=feature",
        "/gl/api/v4/projects/g%2Fp/pipelines/9/bridges",
        "/gl/api/v4/projects/g%2Fp/repository/commits/abc123/statuses?pipeline_id=9",
    ],
)
async def test_allows_paths_radar_uses(ctx, path):
    upstream = Upstream()
    app = _app(ctx, upstream)
    async with _client(app) as client:
        response = await client.get(path, headers=_auth(app))
    assert response.status_code == 200
    assert len(upstream.requests) == 1


@pytest.mark.parametrize(
    "path",
    [
        "/gl/api/v4/projects/42/variables",
        "/gl/api/v4/projects/42/merge_requests/5/notes",
        "/gl/api/v4/users",
        "/gl/api/v4/projects/42/../../user/keys",
        "/gl/api/v4/projects/%2e%2e/merge_requests",
        "/gl/api/v4/projects/42/repository/files/secret",
        "/gl/api/v4/projects/42/merge_requests/abc/approvals",
    ],
)
async def test_rejects_paths_outside_the_allowlist(ctx, path):
    upstream = Upstream()
    app = _app(ctx, upstream)
    async with _client(app) as client:
        response = await client.get(path, headers=_auth(app))
    assert response.status_code in (403, 404)
    assert upstream.requests == []


async def test_rest_proxy_is_get_only(ctx):
    upstream = Upstream()
    app = _app(ctx, upstream)
    async with _client(app) as client:
        response = await client.post("/gl/api/v4/user", headers=_auth(app))
    assert response.status_code == 405
    assert upstream.requests == []


async def test_forwards_read_only_graphql_with_bearer_token(ctx):
    upstream = Upstream(lambda request: httpx.Response(200, json={"data": {"currentUser": {"id": "1"}}}))
    app = _app(ctx, upstream)
    payload = {"query": "query Me { currentUser { id } }", "variables": {}}
    async with _client(app) as client:
        response = await client.post("/gl/api/graphql", headers={"Authorization": f"Bearer {app.state.proxy_secret}"}, json=payload)
    assert response.status_code == 200
    assert response.json() == {"data": {"currentUser": {"id": "1"}}}
    sent = upstream.requests[0]
    assert sent.method == "POST"
    assert sent.url.path == "/api/graphql"
    assert sent.headers["Authorization"] == f"Bearer {REAL_PAT}"
    assert json.loads(sent.content) == payload


@pytest.mark.parametrize(
    "body",
    [
        json.dumps({"query": "mutation { mergeRequestAccept(input: {}) { errors } }"}).encode(),
        json.dumps({"query": "subscription { x }"}).encode(),
        json.dumps({"query": "query A { a } mutation B { b }"}).encode(),
        json.dumps({"variables": {}}).encode(),
        json.dumps(["query { a }"]).encode(),
        b"not json",
        json.dumps({"query": "{ a }", "pad": "x" * (256 * 1024)}).encode(),
    ],
)
async def test_rejects_non_read_only_or_malformed_graphql(ctx, body):
    upstream = Upstream()
    app = _app(ctx, upstream)
    async with _client(app) as client:
        response = await client.post(
            "/gl/api/graphql", headers={**_auth(app), "Content-Type": "application/json"}, content=body
        )
    assert response.status_code == 403
    assert upstream.requests == []


async def test_rate_limit_is_passed_through_and_pauses_later_requests(ctx):
    upstream = Upstream(lambda request: httpx.Response(429, headers={"Retry-After": "60"}))
    app = _app(ctx, upstream)
    async with _client(app) as client:
        first = await client.get("/gl/api/v4/user", headers=_auth(app))
        second = await client.get("/gl/api/v4/user", headers=_auth(app))
    assert first.status_code == 429
    assert first.headers["retry-after"] == "60"
    assert second.status_code == 429
    assert int(second.headers["retry-after"]) > 25
    assert len(upstream.requests) == 1


async def test_upstream_network_error_is_a_bad_gateway(ctx):
    def fail(request):
        raise httpx.ConnectError("boom")

    app = _app(ctx, Upstream(fail))
    async with _client(app) as client:
        response = await client.get("/gl/api/v4/user", headers=_auth(app))
    assert response.status_code == 502
    assert REAL_PAT not in response.text


@pytest.mark.parametrize("headers", [{"Sec-Fetch-Site": "cross-site"}, {"Origin": "https://evil.example.com"}])
async def test_cross_origin_requests_are_rejected_even_with_the_secret(ctx, headers):
    upstream = Upstream()
    app = _app(ctx, upstream)
    async with _client(app) as client:
        response = await client.get("/gl/api/v4/user", headers={**_auth(app), **headers})
    assert response.status_code == 403
    assert upstream.requests == []


@pytest.mark.parametrize("path", ["//gl/api/v4/user", "/gl", "/gl/", "/GL/api/v4/user"])
async def test_prefix_variants_do_not_reach_upstream(ctx, path):
    upstream = Upstream()
    app = _app(ctx, upstream)
    async with _client(app) as client:
        response = await client.get(path, headers={**_auth(app), "Sec-Fetch-Site": "cross-site"})
    assert response.status_code in (403, 404)
    assert upstream.requests == []


async def test_proxy_secret_is_unique_per_app(ctx):
    upstream = Upstream()
    assert _app(ctx, upstream).state.proxy_secret != _app(ctx, upstream).state.proxy_secret


async def test_rate_limit_from_poller_requests_pauses_proxy(ctx):
    calls = {"n": 0}

    def responder(request):
        calls["n"] += 1
        return httpx.Response(429, headers={"Retry-After": "120"})

    upstream = Upstream(responder)
    app = _app(ctx, upstream)
    api = app.state.ctx.api
    api._pause_for(120)
    async with _client(app) as client:
        response = await client.get("/gl/api/v4/user", headers=_auth(app))
    assert response.status_code == 429
    assert calls["n"] == 0


@pytest.mark.parametrize(
    "payload",
    [
        {"query": "{ currentUser { id } }", "_json": [{"query": "mutation { x }"}]},
        {"query": "{ currentUser { id } }", "extensions": {"persistedQuery": {"sha256Hash": "abc"}}},
        {"query": "{ a }", "variables": ["x"]},
        {"query": "{ a }", "operationName": 1},
        {"query": "{ a }", "extra": 1},
    ],
)
async def test_rejects_graphql_bodies_with_unexpected_shape(ctx, payload):
    upstream = Upstream()
    app = _app(ctx, upstream)
    async with _client(app) as client:
        response = await client.post("/gl/api/graphql", headers=_auth(app), json=payload)
    assert response.status_code == 403
    assert upstream.requests == []


async def test_graphql_body_is_rebuilt_from_validated_fields(ctx):
    upstream = Upstream(lambda request: httpx.Response(200, json={"data": {}}))
    app = _app(ctx, upstream)
    raw = b'{"query": "query A($x: ID!) { a(id: $x) }", "variables": {"x": "1"}, "operationName": "A"}'
    async with _client(app) as client:
        response = await client.post(
            "/gl/api/graphql", headers={**_auth(app), "Content-Type": "application/json"}, content=raw
        )
    assert response.status_code == 200
    assert json.loads(upstream.requests[0].content) == {
        "query": "query A($x: ID!) { a(id: $x) }",
        "variables": {"x": "1"},
        "operationName": "A",
    }


@pytest.mark.parametrize(
    "path",
    [
        "/gl/api/v4/projects/a%2F..%2F..%2Fusers/merge_requests",
        "/gl/api/v4/projects/a%2F.%2Fb/merge_requests",
        "/gl/api/v4/projects/a%5Cb/merge_requests",
        "/gl/api/v4/projects/42/repository/commits/a%2Fb/statuses",
        "/gl/api/v4/projects/42/merge_requests%2F5/approvals",
    ],
)
async def test_rejects_encoded_traversal_and_slashes_outside_the_project_segment(ctx, path):
    upstream = Upstream()
    app = _app(ctx, upstream)
    async with _client(app) as client:
        response = await client.get(path, headers=_auth(app))
    assert response.status_code == 403
    assert upstream.requests == []


async def test_non_ascii_secret_header_is_forbidden_not_an_error(ctx):
    upstream = Upstream()
    app = _app(ctx, upstream)
    async with _client(app) as client:
        response = await client.get("/gl/api/v4/user", headers={"PRIVATE-TOKEN": b"\xe9"})
    assert response.status_code == 403
