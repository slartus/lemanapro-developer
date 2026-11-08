from __future__ import annotations


import httpx
import pytest

from mrwatch.gitlab_client import GitLabClient, GitlabApiError

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
