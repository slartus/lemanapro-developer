from __future__ import annotations

import asyncio
import logging
import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

import httpx

from lpdev.config import TrackerConfig
from lpdev.procutil import mask_secrets

logger = logging.getLogger(__name__)

ISSUE_KEY_RE = re.compile(r"[A-Z][A-Z0-9_]+-\d+")
STATUS_KEY_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
WEEK_DAYS = 5

_MAX_RETRIES = 3
_RETRY_BASE_DELAY_SEC = 0.5
_RETRY_AFTER_CAP_SEC = 10.0
_MAX_PAGES = 20
_PER_PAGE = 100
_KEYS_CHUNK = 100
_ERROR_TEXT_LIMIT = 300
_DURATION_RE = re.compile(
    r"P(?:(?P<weeks>\d+)W)?(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?"
)

TokenProvider = Callable[[], Awaitable[str]]


class TrackerApiError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class TrackerUncertain(TrackerApiError):
    pass


@dataclass(frozen=True)
class Worklog:
    id: str
    key: str
    day: date
    minutes: int
    created_at: datetime | None = None


def valid_issue_key(key: object) -> bool:
    return isinstance(key, str) and ISSUE_KEY_RE.fullmatch(key) is not None


def iso_duration(minutes: int) -> str:
    if minutes < 0:
        raise ValueError("duration must not be negative")
    hours, rest = divmod(minutes, 60)
    if not hours:
        return f"PT{rest}M"
    return f"PT{hours}H{rest}M" if rest else f"PT{hours}H"


def parse_duration(value: object, day_hours: int = 8) -> int | None:
    if not isinstance(value, str):
        return None
    match = _DURATION_RE.fullmatch(value.strip())
    if match is None or value.strip() in ("P", "PT"):
        return None
    parts = match.groupdict()
    days = int(parts["weeks"] or 0) * WEEK_DAYS + int(parts["days"] or 0)
    seconds = float(parts["seconds"] or 0)
    total = days * day_hours * 60 + int(parts["hours"] or 0) * 60 + int(parts["minutes"] or 0)
    return total + round(seconds / 60)


def _parse_moment(value: object) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def worklog_author(user: dict[str, Any]) -> str:
    for name in ("trackerUid", "uid"):
        if user.get(name) not in (None, ""):
            return str(user[name])
    raise TrackerApiError("Tracker не вернул uid текущего пользователя")


def my_issues_query(statuses: list[str], queues: list[str]) -> str:
    for value in (*statuses, *queues):
        if not STATUS_KEY_RE.fullmatch(value):
            raise ValueError(f"invalid Tracker key: {value!r}")
    parts = ["Assignee: me()"]
    if queues:
        parts.append("Queue: " + ", ".join(queues))
    parts.append("Status: " + ", ".join(statuses) if statuses else '"Status Type": inProgress')
    parts.append('"Sort by": Updated DESC')
    return " ".join(parts)


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    try:
        port = parts.port
    except ValueError:
        return scheme, "", None
    return scheme, (parts.hostname or "").lower(), port or {"http": 80, "https": 443}.get(scheme)


def user_identities(user: dict[str, Any]) -> set[str]:
    return {str(user[name]) for name in ("login", "uid", "trackerUid", "passportUid") if user.get(name) not in (None, "")}


def format_start(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise ValueError("start must be timezone-aware")
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000%z")


def _error_text(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        messages = payload.get("errorMessages") or []
        errors = payload.get("errors") or {}
        parts = [str(item) for item in messages if item]
        if isinstance(errors, dict):
            parts.extend(f"{name}: {text}" for name, text in errors.items())
        text = "; ".join(parts)
    else:
        text = response.text
    return mask_secrets(" ".join(text.split()))[:_ERROR_TEXT_LIMIT]


def _status_error(response: httpx.Response) -> TrackerApiError:
    status = response.status_code
    if status == 401:
        return TrackerApiError("токен Tracker недействителен или истёк", status)
    if status == 403:
        return TrackerApiError("Tracker: нет доступа (проверьте org_id, org_header и scope токена)", status)
    if status == 404:
        return TrackerApiError("Tracker: не найдено", status)
    if 300 <= status < 400:
        return TrackerApiError(f"Tracker: неожиданный редирект {status}", status)
    detail = _error_text(response)
    return TrackerApiError(f"Tracker: ошибка {status}" + (f": {detail}" if detail else ""), status)


def _retry_after_sec(response: httpx.Response, default: float) -> float:
    try:
        value = float(response.headers["Retry-After"])
    except (KeyError, ValueError):
        return default
    return min(value, _RETRY_AFTER_CAP_SEC) if math.isfinite(value) and value >= 0 else default


def _total_pages(response: httpx.Response) -> int:
    try:
        return max(1, int(response.headers.get("X-Total-Pages", "1")))
    except ValueError:
        return 1


class TrackerClient:
    def __init__(
        self,
        cfg: TrackerConfig,
        token_provider: TokenProvider,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.cfg = cfg
        self._token_provider = token_provider
        self._token: str | None = None
        self._token_lock = asyncio.Lock()
        self._tz = ZoneInfo(cfg.timezone)
        self._client = httpx.AsyncClient(
            base_url=cfg.url,
            timeout=httpx.Timeout(20.0),
            follow_redirects=False,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _headers(self) -> dict[str, str]:
        async with self._token_lock:
            if self._token is None:
                self._token = await self._token_provider()
            token = self._token
        return {"Authorization": f"OAuth {token}", self.cfg.org_header: self.cfg.org_id}

    def _issue_path(self, key: str, suffix: str = "") -> str:
        if not valid_issue_key(key):
            raise TrackerApiError("неверный ключ задачи")
        return f"/issues/{quote(key, safe='')}{suffix}"

    def _check(self, response: httpx.Response) -> httpx.Response:
        if response.status_code == 401:
            self._token = None
        if response.status_code >= 300:
            raise _status_error(response)
        return response

    async def _read(
        self, method: str, path: str, json: Any = None, params: dict[str, Any] | None = None
    ) -> httpx.Response:
        for attempt in range(1, _MAX_RETRIES + 1):
            headers = await self._headers()
            try:
                response = await self._client.request(method, path, json=json, params=params, headers=headers)
            except httpx.HTTPError as exc:
                if attempt == _MAX_RETRIES:
                    raise TrackerApiError(f"Tracker недоступен: {type(exc).__name__}") from exc
                await asyncio.sleep(_RETRY_BASE_DELAY_SEC * attempt)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == _MAX_RETRIES:
                    raise _status_error(response)
                delay = _RETRY_BASE_DELAY_SEC * attempt
                if response.status_code == 429:
                    delay = _retry_after_sec(response, delay)
                await asyncio.sleep(delay)
                continue
            return self._check(response)
        raise TrackerApiError("unreachable retry loop")

    async def _read_pages(self, path: str, body: dict[str, Any], limit: int | None = None) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        per_page = min(_PER_PAGE, limit) if limit else _PER_PAGE
        while True:
            response = await self._read("POST", path, json=body, params={"perPage": per_page, "page": page})
            payload = response.json()
            if not isinstance(payload, list):
                raise TrackerApiError("Tracker: неожиданный ответ")
            items.extend(item for item in payload if isinstance(item, dict))
            if limit is not None and len(items) >= limit:
                return items[:limit]
            if page >= _total_pages(response):
                return items
            if page >= _MAX_PAGES:
                logger.warning("tracker pagination capped at %s pages for %s", _MAX_PAGES, path)
                return items
            page += 1

    async def myself(self) -> dict[str, Any]:
        payload = (await self._read("GET", "/myself")).json()
        if not isinstance(payload, dict):
            raise TrackerApiError("Tracker: неожиданный ответ")
        return payload

    async def search_my_issues(self, statuses: list[str], queues: list[str], limit: int) -> list[dict[str, Any]]:
        return await self._read_pages("/issues/_search", {"query": my_issues_query(statuses, queues)}, limit=limit)

    async def _read_linked(self, path: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        url = path
        params: dict[str, Any] | None = {"perPage": _PER_PAGE}
        for _ in range(_MAX_PAGES):
            response = await self._read("GET", url, params=params)
            payload = response.json()
            if not isinstance(payload, list):
                raise TrackerApiError("Tracker: неожиданный ответ")
            items.extend(item for item in payload if isinstance(item, dict))
            next_url = response.links.get("next", {}).get("url")
            if not next_url:
                return items
            if _origin(next_url) != _origin(self.cfg.url):
                logger.warning("tracker pagination stopped: foreign next link for %s", path)
                return items
            url, params = next_url, None
        logger.warning("tracker pagination capped at %s pages for %s", _MAX_PAGES, path)
        return items

    async def search_issues(self, query: str, limit: int) -> list[dict[str, Any]]:
        return await self._read_pages("/issues/_search", {"query": query}, limit=limit)

    async def issue_comments(self, key: str) -> list[dict[str, Any]]:
        return await self._read_linked(self._issue_path(key, "/comments"))

    async def get_issues(self, keys: list[str]) -> dict[str, dict[str, Any]]:
        valid = sorted({key for key in keys if valid_issue_key(key)})
        found: dict[str, dict[str, Any]] = {}
        for start in range(0, len(valid), _KEYS_CHUNK):
            chunk = valid[start:start + _KEYS_CHUNK]
            for issue in await self._read_pages("/issues/_search", {"keys": chunk}):
                key = issue.get("key")
                if valid_issue_key(key):
                    found[key] = issue
        return found

    def _worklog(self, item: dict[str, Any]) -> Worklog | None:
        issue = item.get("issue") if isinstance(item.get("issue"), dict) else {}
        key = issue.get("key")
        minutes = parse_duration(item.get("duration"), self.cfg.day_hours)
        try:
            start = datetime.fromisoformat(str(item.get("start")))
        except ValueError:
            return None
        if not valid_issue_key(key) or minutes is None or start.tzinfo is None:
            return None
        return Worklog(
            id=str(item.get("id")), key=key, day=start.astimezone(self._tz).date(), minutes=minutes,
            created_at=_parse_moment(item.get("createdAt")),
        )

    async def search_worklogs(self, created_by: str, start_from: date, start_to: date) -> list[Worklog]:
        start = datetime.combine(start_from, datetime.min.time(), self._tz)
        end = datetime.combine(start_to + timedelta(days=1), datetime.min.time(), self._tz)
        body = {"createdBy": created_by, "start": {"from": format_start(start), "to": format_start(end)}}
        result = []
        for item in await self._read_pages("/worklog/_search", body):
            worklog = self._worklog(item)
            if worklog is not None and start_from <= worklog.day <= start_to:
                result.append(worklog)
        return result

    async def issue_worklogs(self, key: str, authors: set[str] | None = None) -> list[Worklog]:
        result = []
        for item in await self._read_linked(self._issue_path(key, "/worklog")):
            author = item.get("createdBy") if isinstance(item.get("createdBy"), dict) else {}
            if authors is not None and not authors & {str(author.get(name)) for name in ("id", "login", "uid") if author.get(name)}:
                continue
            worklog = self._worklog(item)
            if worklog is not None:
                result.append(worklog)
        return result

    async def create_worklog(self, key: str, start: datetime, minutes: int) -> str:
        path = self._issue_path(key, "/worklog")
        body = {"start": format_start(start), "duration": iso_duration(minutes)}
        headers = await self._headers()
        try:
            response = await self._client.post(path, json=body, headers=headers)
        except httpx.TimeoutException as exc:
            raise TrackerUncertain(f"Tracker не ответил вовремя: {type(exc).__name__}") from exc
        except httpx.HTTPError as exc:
            raise TrackerUncertain(f"Tracker недоступен: {type(exc).__name__}") from exc
        if response.status_code >= 500:
            raise TrackerUncertain(f"Tracker: ошибка {response.status_code}, результат неизвестен", response.status_code)
        self._check(response)
        try:
            payload = response.json()
        except ValueError:
            payload = None
        return str(payload.get("id")) if isinstance(payload, dict) and payload.get("id") is not None else ""
