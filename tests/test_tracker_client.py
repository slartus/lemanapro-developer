from __future__ import annotations

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from lpdev.config import TrackerConfig
from lpdev.tracker_client import (
    MY_ISSUES_QUERY,
    TrackerApiError,
    TrackerClient,
    TrackerUncertain,
    format_start,
    iso_duration,
    parse_duration,
    user_identities,
    valid_issue_key,
)

MSK = ZoneInfo("Europe/Moscow")
CFG = TrackerConfig(enabled=True, org_id="777", token_command="printf tok", queues=["SUPERAPP"])


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def instant(delay):
        instant.delays.append(delay)

    instant.delays = []
    monkeypatch.setattr("lpdev.tracker_client.asyncio.sleep", instant)
    return instant


def _client(handler, cfg: TrackerConfig = CFG, token: str = "y0_secret") -> TrackerClient:
    calls = {"tokens": 0}

    async def provider() -> str:
        calls["tokens"] += 1
        return token

    client = TrackerClient(cfg, provider, transport=httpx.MockTransport(handler))
    client.token_calls = calls
    return client


@pytest.mark.parametrize(
    ("minutes", "text"), [(0, "PT0M"), (30, "PT30M"), (60, "PT1H"), (90, "PT1H30M"), (600, "PT10H")]
)
def test_iso_duration(minutes, text):
    assert iso_duration(minutes) == text
    assert parse_duration(text) == minutes


def test_iso_duration_rejects_negative():
    with pytest.raises(ValueError):
        iso_duration(-1)


@pytest.mark.parametrize(
    ("text", "day_hours", "minutes"),
    [
        ("P1D", 8, 480),
        ("P1W", 8, 2400),
        ("P1DT2H", 8, 600),
        ("P1D", 6, 360),
        ("PT45M", 8, 45),
        ("PT1H30M29S", 8, 90),
        ("PT0.5S", 8, 0),
        ("PT90S", 8, 2),
    ],
)
def test_parse_duration_with_tracker_day_and_week(text, day_hours, minutes):
    assert parse_duration(text, day_hours) == minutes


@pytest.mark.parametrize("text", [None, 5, "", "P", "PT", "1H", "PT1X", "-PT1H"])
def test_parse_duration_rejects_garbage(text):
    assert parse_duration(text) is None


@pytest.mark.parametrize(
    ("key", "ok"),
    [("SUPERAPP-1", True), ("MOB_2-15", True), ("superapp-1", False), ("S-1", False), ("SUPERAPP-1/x", False),
     ("SUPERAPP-", False), ("../SUPERAPP-1", False), (1, False)],
)
def test_valid_issue_key(key, ok):
    assert valid_issue_key(key) is ok


def test_format_start_requires_timezone():
    assert format_start(datetime(2026, 11, 9, 10, 0, tzinfo=MSK)) == "2026-11-09T10:00:00.000+0300"
    with pytest.raises(ValueError):
        format_start(datetime(2026, 11, 9, 10, 0))


def test_user_identities():
    assert user_identities({"login": "me", "uid": 1, "trackerUid": 2, "passportUid": None}) == {"me", "1", "2"}


async def test_headers_org_and_myself():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"login": "me", "uid": 5})

    cfg = CFG.model_copy(update={"org_header": "X-Cloud-Org-Id"})
    client = _client(handler, cfg)
    try:
        assert (await client.myself())["login"] == "me"
        await client.myself()
    finally:
        await client.aclose()
    request = seen[0]
    assert str(request.url) == "https://api.tracker.yandex.net/v3/myself"
    assert request.headers["Authorization"] == "OAuth y0_secret"
    assert request.headers["X-Cloud-Org-Id"] == "777"
    assert "X-Org-Id" not in request.headers
    assert client.token_calls["tokens"] == 1


async def test_search_my_issues_paginates_by_total_pages():
    pages = []

    def handler(request: httpx.Request) -> httpx.Response:
        pages.append(request.url.params["page"])
        assert request.url.path == "/v3/issues/_search"
        assert json.loads(request.content) == {"query": MY_ISSUES_QUERY}
        page = int(request.url.params["page"])
        return httpx.Response(200, json=[{"key": f"SUPERAPP-{page}"}], headers={"X-Total-Pages": "2"})

    client = _client(handler)
    try:
        issues = await client.search_my_issues()
    finally:
        await client.aclose()
    assert [issue["key"] for issue in issues] == ["SUPERAPP-1", "SUPERAPP-2"]
    assert pages == ["1", "2"]


async def test_get_issues_by_keys_skips_invalid_keys():
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=[{"key": "SUPERAPP-1", "summary": "Title"}, {"key": "bad"}, "x"])

    client = _client(handler)
    try:
        issues = await client.get_issues(["SUPERAPP-1", "SUPERAPP-1", "bad key"])
        assert await client.get_issues([]) == {}
    finally:
        await client.aclose()
    assert bodies == [{"keys": ["SUPERAPP-1"]}]
    assert issues == {"SUPERAPP-1": {"key": "SUPERAPP-1", "summary": "Title"}}


async def test_search_worklogs_filters_by_local_start_day():
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=[
            {"id": 1, "issue": {"key": "SUPERAPP-1"}, "start": "2026-11-08T21:30:00.000+0000", "duration": "PT1H"},
            {"id": 2, "issue": {"key": "SUPERAPP-2"}, "start": "2026-11-08T20:00:00.000+0000", "duration": "PT1H"},
            {"id": 3, "issue": {"key": "SUPERAPP-3"}, "start": "2026-11-10T09:00:00.000+0300", "duration": "P1D"},
            {"id": 4, "issue": {"key": "SUPERAPP-4"}, "start": "garbage", "duration": "PT1H"},
            {"id": 5, "issue": {"key": "SUPERAPP-5"}, "start": "2026-11-10T09:00:00", "duration": "PT1H"},
            {"id": 6, "issue": {}, "start": "2026-11-10T09:00:00.000+0300", "duration": "PT1H"},
            "junk",
        ])

    client = _client(handler)
    try:
        worklogs = await client.search_worklogs("me", date(2026, 11, 9), date(2026, 11, 15))
    finally:
        await client.aclose()
    assert [(w.id, w.key, w.day.isoformat(), w.minutes) for w in worklogs] == [
        ("1", "SUPERAPP-1", "2026-11-09", 60),
        ("3", "SUPERAPP-3", "2026-11-10", 480),
    ]
    body = bodies[0]
    assert body["createdBy"] == "me"
    assert body["createdAt"]["from"] == "2026-10-26T00:00:00.000+0300"


async def test_issue_worklogs_filters_by_author_and_quotes_key():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.raw_path == b"/v3/issues/SUPERAPP-1/worklog"
        return httpx.Response(200, json=[
            {"id": 1, "issue": {"key": "SUPERAPP-1"}, "createdBy": {"id": "5"}, "start": "2026-11-09T10:00:00.000+0300", "duration": "PT30M"},
            {"id": 2, "issue": {"key": "SUPERAPP-1"}, "createdBy": {"id": "9"}, "start": "2026-11-09T10:00:00.000+0300", "duration": "PT30M"},
            "junk",
        ])

    client = _client(handler)
    try:
        mine = await client.issue_worklogs("SUPERAPP-1", {"me", "5"})
        everyone = await client.issue_worklogs("SUPERAPP-1")
    finally:
        await client.aclose()
    assert [w.id for w in mine] == ["1"]
    assert [w.id for w in everyone] == ["1", "2"]


async def test_invalid_key_never_reaches_the_network():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not be called")

    client = _client(handler)
    try:
        for key in ("SUPERAPP-1/../../x", "superapp-1", "SUPERAPP-1?x=1"):
            with pytest.raises(TrackerApiError):
                await client.create_worklog(key, datetime(2026, 11, 9, 10, tzinfo=MSK), 30)
            with pytest.raises(TrackerApiError):
                await client.issue_worklogs(key)
    finally:
        await client.aclose()


async def test_read_retries_429_with_retry_after_and_5xx(_no_sleep):
    statuses = iter([429, 503, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        status = next(statuses)
        return httpx.Response(status, json={"login": "me"} if status == 200 else {}, headers={"Retry-After": "7"})

    client = _client(handler)
    try:
        assert (await client.myself())["login"] == "me"
    finally:
        await client.aclose()
    assert _no_sleep.delays == [7.0, 1.0]


async def test_read_retries_network_errors_then_gives_up():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ConnectError("boom")

    client = _client(handler)
    try:
        with pytest.raises(TrackerApiError, match="недоступен"):
            await client.myself()
    finally:
        await client.aclose()
    assert len(calls) == 3


async def test_read_gives_up_after_repeated_5xx():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Authorization: OAuth y0_leaked")

    client = _client(handler)
    try:
        with pytest.raises(TrackerApiError) as excinfo:
            await client.myself()
    finally:
        await client.aclose()
    assert excinfo.value.status == 500
    assert "y0_leaked" not in excinfo.value.message


async def test_401_is_a_clear_message_and_drops_the_cached_token():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"errorMessages": ["bad token"]})

    client = _client(handler)
    try:
        with pytest.raises(TrackerApiError, match="токен Tracker недействителен"):
            await client.myself()
        with pytest.raises(TrackerApiError):
            await client.myself()
    finally:
        await client.aclose()
    assert client.token_calls["tokens"] == 2


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (403, {}, "нет доступа"),
        (404, {}, "не найдено"),
        (302, {}, "редирект"),
        (422, {"errorMessages": ["Bad duration"], "errors": {"start": "OAuth y0_abc wrong"}}, "Bad duration; start: OAuth ***"),
        (400, None, "plain text"),
    ],
)
async def test_errors_are_masked_and_readable(status, body, expected):
    def handler(request: httpx.Request) -> httpx.Response:
        if body is None:
            return httpx.Response(status, text="plain   text")
        return httpx.Response(status, json=body, headers={"Location": "https://evil.example.com"})

    client = _client(handler)
    try:
        with pytest.raises(TrackerApiError) as excinfo:
            await client.myself()
    finally:
        await client.aclose()
    assert expected in excinfo.value.message
    assert excinfo.value.status == status
    assert "y0_abc" not in excinfo.value.message


async def test_long_error_text_is_truncated():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="x" * 5000)

    client = _client(handler)
    try:
        with pytest.raises(TrackerApiError) as excinfo:
            await client.myself()
    finally:
        await client.aclose()
    assert len(excinfo.value.message) < 400


async def test_non_list_payloads_are_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"oops": 1} if request.method == "POST" or "worklog" in request.url.path else [1])

    client = _client(handler)
    try:
        with pytest.raises(TrackerApiError):
            await client.search_my_issues()
        with pytest.raises(TrackerApiError):
            await client.search_worklogs("me", date(2026, 11, 9), date(2026, 11, 15))
        with pytest.raises(TrackerApiError):
            await client.issue_worklogs("SUPERAPP-1")
        with pytest.raises(TrackerApiError):
            await client.myself()
    finally:
        await client.aclose()


async def test_create_worklog_sends_start_and_duration_once():
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(201, json={"id": 42})

    client = _client(handler)
    try:
        worklog_id = await client.create_worklog("SUPERAPP-1", datetime(2026, 11, 9, 10, tzinfo=MSK), 90)
    finally:
        await client.aclose()
    assert worklog_id == "42"
    assert len(sent) == 1
    assert sent[0].method == "POST"
    assert sent[0].url.path == "/v3/issues/SUPERAPP-1/worklog"
    assert json.loads(sent[0].content) == {"start": "2026-11-09T10:00:00.000+0300", "duration": "PT1H30M"}


@pytest.mark.parametrize(
    "failure",
    [httpx.ReadTimeout("slow"), httpx.ConnectError("down"), 500, 503],
)
async def test_create_worklog_is_never_retried_and_reports_uncertainty(failure):
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if isinstance(failure, Exception):
            raise failure
        return httpx.Response(failure)

    client = _client(handler)
    try:
        with pytest.raises(TrackerUncertain):
            await client.create_worklog("SUPERAPP-1", datetime(2026, 11, 9, 10, tzinfo=MSK), 30)
    finally:
        await client.aclose()
    assert len(sent) == 1


async def test_create_worklog_client_errors_are_definite():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "3"})

    client = _client(handler)
    try:
        with pytest.raises(TrackerApiError) as excinfo:
            await client.create_worklog("SUPERAPP-1", datetime(2026, 11, 9, 10, tzinfo=MSK), 30)
    finally:
        await client.aclose()
    assert not isinstance(excinfo.value, TrackerUncertain)


async def test_create_worklog_without_id_in_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, text="ok")

    client = _client(handler)
    try:
        assert await client.create_worklog("SUPERAPP-1", datetime(2026, 11, 9, 10, tzinfo=MSK), 30) == ""
    finally:
        await client.aclose()
