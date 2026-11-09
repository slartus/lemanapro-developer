from __future__ import annotations


import httpx
import pytest

from lpdev.gitlab_client import _MAX_PAGES, GitLabClient, GitlabApiError

BASE_URL = "https://gitlab.example.com"


def _client(handler) -> GitLabClient:
    return GitLabClient(BASE_URL, "secret-token", transport=httpx.MockTransport(handler))


async def test_get_user():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v4/user"
        assert request.headers["PRIVATE-TOKEN"] == "secret-token"
        return httpx.Response(200, json={"id": 1, "username": "me"})

    client = _client(handler)
    try:
        assert await client.get_user() == {"id": 1, "username": "me"}
    finally:
        await client.aclose()


async def test_get_todos():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["state"] == "pending"
        return httpx.Response(200, json=[{"id": 1}, {"id": 2}])

    client = _client(handler)
    try:
        todos = await client.get_todos()
        assert len(todos) == 2
    finally:
        await client.aclose()


async def test_retry_on_429():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json=[])

    client = _client(handler)
    try:
        assert await client.get_todos() == []
        assert calls["n"] == 2
    finally:
        await client.aclose()


async def test_401_raises_without_retry():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, json={"message": "401 Unauthorized"})

    client = _client(handler)
    try:
        with pytest.raises(GitlabApiError) as exc_info:
            await client.get_user()
        assert exc_info.value.status == 401
        assert "secret-token" not in str(exc_info.value)
        assert calls["n"] == 1
    finally:
        await client.aclose()


async def test_500_retries_then_raises():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, json={"message": "boom"})

    client = _client(handler)
    try:
        with pytest.raises(GitlabApiError) as exc_info:
            await client.get_todos()
        assert exc_info.value.status == 500
        assert calls["n"] == 3
    finally:
        await client.aclose()


async def test_get_todos_paginates():
    def handler(request: httpx.Request) -> httpx.Response:
        if "page=2" not in str(request.url):
            return httpx.Response(
                200,
                json=[{"id": 1}],
                headers={
                    "Link": (
                        f'<{BASE_URL}/api/v4/todos?state=pending&per_page=100&page=2>'
                        '; rel="next"'
                    )
                },
            )
        return httpx.Response(200, json=[{"id": 2}])

    client = _client(handler)
    try:
        todos = await client.get_todos()
        assert [t["id"] for t in todos] == [1, 2]
    finally:
        await client.aclose()


async def test_project_merge_requests_follow_next_link():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        assert request.url.path == "/api/v4/projects/1/merge_requests"
        if len(seen) == 1:
            assert request.url.params["updated_after"] == "2026-01-01T00:00:00Z"
            return httpx.Response(
                200,
                json=[{"id": 1}],
                headers={
                    "Link": (
                        f"<{BASE_URL}/api/v4/projects/1/merge_requests"
                        '?id_after=5&per_page=100>; rel="next"'
                    )
                },
            )
        assert request.url.params["id_after"] == "5"
        assert len(request.url.params.get_list("per_page")) == 1
        assert "updated_after" not in request.url.params
        return httpx.Response(200, json=[{"id": 2}])

    client = _client(handler)
    try:
        mrs = await client.get_project_merge_requests(1, "2026-01-01T00:00:00Z")
    finally:
        await client.aclose()
    assert [m["id"] for m in mrs] == [1, 2]
    assert len(seen) == 2


async def test_project_merge_requests_stop_without_next_link():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json=[{"id": 1}])

    client = _client(handler)
    try:
        mrs = await client.get_project_merge_requests(1, "2026-01-01T00:00:00Z")
    finally:
        await client.aclose()
    assert mrs == [{"id": 1}]
    assert len(calls) == 1


async def test_project_merge_requests_capped_at_max_pages():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(
            200,
            json=[{"id": len(calls)}],
            headers={
                "Link": f'<{BASE_URL}/api/v4/projects/1/merge_requests?page=9>; rel="next"'
            },
        )

    client = _client(handler)
    try:
        mrs = await client.get_project_merge_requests(1, "2026-01-01T00:00:00Z")
    finally:
        await client.aclose()
    assert len(mrs) == len(calls) == _MAX_PAGES


async def test_rate_limit_seen_by_the_proxy_delays_poller_requests(monkeypatch):
    responses = iter([
        httpx.Response(429, headers={"Retry-After": "7"}),
        httpx.Response(200, json={"username": "me"}),
    ])
    client = GitLabClient(
        "https://gitlab.example.com", "tok", transport=httpx.MockTransport(lambda request: next(responses))
    )
    sleeps: list[float] = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    forwarded = await client.forward("GET", "/api/v4/user")
    monkeypatch.setattr("lpdev.gitlab_client.asyncio.sleep", fake_sleep)
    user = await client.get_user()
    await client.aclose()
    assert forwarded.status_code == 429
    assert user == {"username": "me"}
    assert len(sleeps) == 1 and 6 < sleeps[0] <= 7
