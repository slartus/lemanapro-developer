from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from urllib.parse import quote, urlsplit

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_RETRY_BASE_DELAY_SEC = 0.5
_RETRY_AFTER_CAP_SEC = 10.0
_MAX_PAGES = 50


class GitlabApiError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    port = parts.port or {"http": 80, "https": 443}.get(scheme)
    return (scheme, (parts.hostname or "").lower(), port)


class GitLabClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._origin = _origin(base_url)
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"PRIVATE-TOKEN": token},
            timeout=httpx.Timeout(30.0),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        url: str = path
        query = params
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                response = await self._client.get(url, params=query)
            except httpx.HTTPError as exc:
                if attempt == _MAX_RETRIES:
                    raise GitlabApiError(f"request failed: {type(exc).__name__}") from exc
                await asyncio.sleep(_RETRY_BASE_DELAY_SEC * attempt)
                continue
            if response.status_code < 400:
                if url != path:
                    query = None
                url = path
                return response
            if response.status_code in (401, 403):
                raise GitlabApiError(
                    f"auth failed with status {response.status_code}",
                    status=response.status_code,
                )
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == _MAX_RETRIES:
                    raise GitlabApiError(
                        f"gitlab error status {response.status_code}",
                        status=response.status_code,
                    )
                delay = _RETRY_BASE_DELAY_SEC * attempt
                retry_after = response.headers.get("Retry-After")
                if response.status_code == 429 and retry_after is not None:
                    try:
                        delay = min(float(retry_after), _RETRY_AFTER_CAP_SEC)
                    except ValueError:
                        pass
                await asyncio.sleep(delay)
                continue
            raise GitlabApiError(
                f"gitlab error status {response.status_code}",
                status=response.status_code,
            )
        raise GitlabApiError("unreachable retry loop")

    def _next_url(self, response: httpx.Response) -> str | None:
        next_url = response.links.get("next", {}).get("url")
        if not next_url:
            return None
        parts = urlsplit(next_url)
        if parts.scheme or parts.netloc:
            candidate = _origin(next_url)
            if candidate != self._origin:
                logger.warning("pagination stopped: foreign next link host %s", candidate[1])
                return None
        return next_url

    async def get_user(self) -> dict[str, Any]:
        response = await self._get("/api/v4/user")
        return response.json()

    async def get_todos(self) -> list[dict[str, Any]]:
        return await self._paginated("/api/v4/todos", {"state": "pending", "per_page": 100})

    async def _paginated(self, url: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        query: dict[str, Any] | None = params
        for _ in range(_MAX_PAGES):
            response = await self._get(url, query)
            items.extend(response.json())
            next_url = self._next_url(response)
            if not next_url:
                return items
            url = next_url
            query = None
        logger.warning("pagination capped at %s pages for %s", _MAX_PAGES, url)
        return items

    async def get_project_merge_requests(
        self,
        project_id: int,
        updated_after: str | None = None,
        state: str | None = "opened",
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "order_by": "updated_at",
            "sort": "asc" if updated_after is not None else "desc",
            "per_page": 100,
            "pagination": "keyset",
        }
        if state is not None:
            params["state"] = state
        if updated_after is not None:
            params["updated_after"] = updated_after
        return await self._paginated(
            f"/api/v4/projects/{project_id}/merge_requests", params
        )

    async def get_project_by_path(self, path: str) -> dict[str, Any]:
        encoded = quote(path, safe="")
        response = await self._get(f"/api/v4/projects/{encoded}")
        return response.json()

    async def get_project_mr(self, project_id: int, iid: int) -> dict[str, Any]:
        response = await self._get(f"/api/v4/projects/{project_id}/merge_requests/{iid}")
        return response.json()

    async def get_mr_notes(self, project_id: int, iid: int) -> list[dict[str, Any]]:
        response = await self._get(
            f"/api/v4/projects/{project_id}/merge_requests/{iid}/notes",
            {"per_page": 100, "sort": "desc", "order_by": "created_at"},
        )
        return response.json()
