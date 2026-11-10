from __future__ import annotations

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from lpdev.config import TrackerConfig
from lpdev.tracker_client import (
    TrackerApiError,
    TrackerClient,
    TrackerUncertain,
    format_start,
    iso_duration,
    my_issues_query,
    parse_duration,
    user_identities,
    valid_issue_key,
    worklog_author,
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


async def test_search_my_issues_queries_in_progress_statuses_with_a_limit():
    pages = []

    def handler(request: httpx.Request) -> httpx.Response:
        pages.append((request.url.params["page"], request.url.params["perPage"]))
        assert request.url.path == "/v3/issues/_search"
        assert json.loads(request.content) == {
            "query": 'Assignee: me() Queue: SUPERAPP Status: development, codereview "Sort by": Updated DESC'
        }
        page = int(request.url.params["page"])
        return httpx.Response(200, json=[
            {"key": f"SUPERAPP-{page}{n}", "status": {"key": "development"}, "statusType": {"key": "inProgress"}}
            for n in range(5)
        ], headers={"X-Total-Pages": "100"})

    client = _client(handler)
    try:
        issues = await client.search_my_issues(["development", "codereview"], ["SUPERAPP"], 5)
    finally:
        await client.aclose()
    assert len(issues) == 5
    assert pages == [("1", "5")]


@pytest.mark.parametrize(
    ("statuses", "queues", "query"),
    [
        (["development"], [], 'Assignee: me() Status: development "Sort by": Updated DESC'),
        ([], [], 'Assignee: me() "Status Type": inProgress "Sort by": Updated DESC'),
        ([], ["SUPERAPP", "MOB"], 'Assignee: me() Queue: SUPERAPP, MOB "Status Type": inProgress "Sort by": Updated DESC'),
    ],
)
def test_my_issues_query(statuses, queues, query):
    assert my_issues_query(statuses, queues) == query


@pytest.mark.parametrize("bad", ["dev ops", 'x" OR "y', "1dev", ""])
def test_my_issues_query_rejects_raw_input(bad):
    with pytest.raises(ValueError):
        my_issues_query([bad], [])


def test_worklog_author_prefers_tracker_uid():
    me = {"uid": 9000000000151720, "trackerUid": 9000000000151720, "login": "60121337", "passportUid": 1130000066073403}
    assert worklog_author(me) == "9000000000151720"
    assert worklog_author({"uid": 7, "login": "x"}) == "7"
    with pytest.raises(TrackerApiError):
        worklog_author({"login": "60121337"})


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
    assert body["start"] == {"from": "2026-11-09T00:00:00.000+0300", "to": "2026-11-16T00:00:00.000+0300"}
    assert "createdAt" not in body


async def test_search_worklogs_paginates():
    pages = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        pages.append((page, request.url.params["perPage"]))
        assert json.loads(request.content)["createdBy"] == "me"
        return httpx.Response(200, headers={"X-Total-Pages": "2"}, json=[
            {"id": page, "issue": {"key": f"SUPERAPP-{page}"}, "start": "2026-11-09T10:00:00.000+0300", "duration": "PT1H"},
        ])

    client = _client(handler)
    try:
        worklogs = await client.search_worklogs("me", date(2026, 11, 9), date(2026, 11, 15))
    finally:
        await client.aclose()
    assert [w.key for w in worklogs] == ["SUPERAPP-1", "SUPERAPP-2"]
    assert pages == [(1, "100"), (2, "100")]


async def test_issue_worklogs_filters_by_author_and_quotes_key():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.raw_path == b"/v3/issues/SUPERAPP-1/worklog?perPage=100"
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
            await client.search_my_issues([], [], 5)
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


async def test_search_issues_stops_at_the_limit():
    pages = []

    def handler(request: httpx.Request) -> httpx.Response:
        pages.append((request.url.params["page"], request.url.params["perPage"]))
        assert json.loads(request.content) == {"query": "Q"}
        page = int(request.url.params["page"])
        return httpx.Response(200, json=[{"key": f"SUPERAPP-{page}{n}"} for n in range(3)], headers={"X-Total-Pages": "5"})

    client = _client(handler)
    try:
        issues = await client.search_issues("Q", 3)
    finally:
        await client.aclose()
    assert len(issues) == 3
    assert pages == [("1", "3")]


async def test_issue_comments():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.raw_path == b"/v3/issues/SUPERAPP-1/comments?perPage=100"
        return httpx.Response(200, json=[{"id": 1, "text": "PR: x"}, "junk"], headers={"X-Total-Pages": "1"})

    client = _client(handler)
    try:
        assert await client.issue_comments("SUPERAPP-1") == [{"id": 1, "text": "PR: x"}]
        with pytest.raises(TrackerApiError):
            await client.issue_comments("bad key")
    finally:
        await client.aclose()


async def test_issue_comments_rejects_non_list():
    client = _client(lambda request: httpx.Response(200, json={"x": 1}))
    try:
        with pytest.raises(TrackerApiError):
            await client.issue_comments("SUPERAPP-1")
    finally:
        await client.aclose()


REAL_WORKLOG = {
    "self": "https://api.tracker.yandex.net/v3/issues/SUPERAPP-9886/worklog/1",
    "id": 1,
    "issue": {"self": "https://api.tracker.yandex.net/v3/issues/SUPERAPP-9886", "id": "abc", "key": "SUPERAPP-9886",
              "display": "Task"},
    "start": "2026-11-02T04:00:00.000+0000",
    "createdAt": "2026-11-06T14:11:37.252+0000",
    "duration": "P1D",
    "createdBy": {"self": "https://api.tracker.yandex.net/v3/users/9000000000151720", "id": "9000000000151720",
                  "display": "Артём Слинкин", "cloudUid": "ajeabcdef", "passportUid": 1130000066073403},
}


async def test_search_worklogs_with_real_response_shape():
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=[
            REAL_WORKLOG,
            {**REAL_WORKLOG, "id": 2, "duration": "PT7H", "start": "2026-11-03T06:00:00.000+0000"},
            {**REAL_WORKLOG, "id": 3, "duration": "PT4H", "start": "2026-11-08T21:30:00.000+0000"},
        ], headers={"X-Total-Pages": "1"})

    client = _client(handler)
    try:
        worklogs = await client.search_worklogs("9000000000151720", date(2026, 11, 2), date(2026, 11, 8))
    finally:
        await client.aclose()
    assert bodies[0] == {
        "createdBy": "9000000000151720",
        "start": {"from": "2026-11-02T00:00:00.000+0300", "to": "2026-11-09T00:00:00.000+0300"},
    }
    assert [(w.key, w.day.isoformat(), w.minutes) for w in worklogs] == [
        ("SUPERAPP-9886", "2026-11-02", 480), ("SUPERAPP-9886", "2026-11-03", 420),
    ]
    assert worklogs[0].created_at is not None


async def test_issue_worklogs_matches_my_uid_against_created_by_id():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[
            REAL_WORKLOG,
            {**REAL_WORKLOG, "id": 2, "createdBy": {"id": "8000000000061439", "display": "ya360-pr-check"}},
        ])

    me = {"uid": 9000000000151720, "trackerUid": 9000000000151720, "login": "60121337", "passportUid": 1130000066073403}
    client = _client(handler)
    try:
        worklogs = await client.issue_worklogs("SUPERAPP-9886", user_identities(me))
    finally:
        await client.aclose()
    assert [w.id for w in worklogs] == ["1"]


async def test_comments_follow_link_pagination_on_the_same_host_only(monkeypatch):
    monkeypatch.setattr("lpdev.tracker_client._PER_PAGE", 2)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "id=2" in str(request.url):
            return httpx.Response(200, json=[{"id": 3, "text": "c"}, {"id": 4, "text": "d"}], headers={
                "Link": '<https://evil.example.com/v3/issues/SUPERAPP-1/comments?id=3>; rel="next"', "X-Total-Pages": "1",
            })
        return httpx.Response(200, json=[
            {"id": 1, "text": "PR: https://gitlab.lmru.tech/products/lmwork/lmru-mobile_lmwork/-/merge_requests/5172 Визуальных изменений не",
             "createdBy": {"id": "8000000000061439", "display": "ya360-pr-check"}},
            {"id": 2, "text": "b"},
        ], headers={"Link": '<https://api.tracker.yandex.net/v3/issues/SUPERAPP-1/comments?perPage=100&id=2>; rel="next"',
                    "X-Total-Pages": "1"})

    client = _client(handler)
    try:
        comments = await client.issue_comments("SUPERAPP-1")
    finally:
        await client.aclose()
    assert [c["id"] for c in comments] == [1, 2, 3, 4]
    assert len(calls) == 2


async def test_a_short_page_is_the_last_one():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=[{"id": 1}], headers={
            "Link": '<https://api.tracker.yandex.net/v3/issues/SUPERAPP-1/comments?id=1>; rel="next"'})

    client = _client(handler)
    try:
        assert len(await client.issue_comments("SUPERAPP-1")) == 1
    finally:
        await client.aclose()
    assert len(calls) == 1


async def test_comments_pagination_is_capped(monkeypatch):
    monkeypatch.setattr("lpdev.tracker_client._PER_PAGE", 1)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": 1}], headers={
            "Link": '<https://api.tracker.yandex.net/v3/issues/SUPERAPP-1/comments?id=1>; rel="next"'})

    client = _client(handler)
    try:
        comments = await client.issue_comments("SUPERAPP-1")
    finally:
        await client.aclose()
    assert len(comments) == 20


def _status_change(at, src, dst, kind="IssueWorkflow"):
    def status(key, ident):
        return None if key is None else {"self": f"https://api.tracker.yandex.net/v3/statuses/{ident}", "id": ident,
                                         "key": key, "display": key}
    return {
        "id": at, "self": "x", "updatedAt": at, "type": kind, "transport": "front",
        "fields": [
            {"field": {"self": "https://api.tracker.yandex.net/v3/fields/status", "id": "status", "display": "Статус"},
             "from": status(src, "10"), "to": status(dst, "37")},
            {"field": {"self": "https://api.tracker.yandex.net/v3/fields/statusStartTime", "id": "statusStartTime",
                       "display": "Статус изменен"}, "from": "2026-11-09T06:48:48.525+0000", "to": at},
            {"field": {"id": "statusType", "display": "Тип статуса"}, "from": None, "to": None},
        ],
        "updatedBy": {"id": "9000000000151720", "display": "Артём Слинкин"},
    }


async def test_status_history_reads_the_real_changelog_shape_with_link_pages(monkeypatch):
    monkeypatch.setattr("lpdev.tracker_client._PER_PAGE", 2)
    calls = []
    pages = {
        None: [_status_change("2026-11-09T06:48:21.060+0000", None, "backlog", "IssueCreated"),
               _status_change("2026-11-09T06:48:51.745+0000", "todo", "development")],
        "a": [_status_change("2026-11-09T06:48:48.525+0000", "backlog", "todo"),
              {"id": "x", "updatedAt": "2026-11-09T07:00:00.000+0000", "fields": [{"field": {"id": "assignee"}}]},
              {"id": "y", "updatedAt": "garbage", "fields": [{"field": {"id": "status"}, "from": None, "to": {"key": "x"}}]},
              {"id": "z", "updatedAt": "2026-11-09T07:00:00.000+0000", "fields": "junk"},
              _status_change("2026-11-09T06:57:06.162+0000", "development", "codereview")],
        "b": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        cursor = request.url.params.get("id")
        following = {None: "a", "a": "b", "b": "c"}[cursor]
        link = (f'<https://api.tracker.yandex.net/v3/issues/SUPERAPP-10397/changelog?field=status&perPage=100>; rel="first", '
                f'<https://api.tracker.yandex.net/v3/issues/SUPERAPP-10397/changelog?field=status&id={following}&perPage=100>; rel="next"')
        return httpx.Response(200, json=pages[cursor], headers={"Link": link})

    client = _client(handler)
    try:
        history = await client.status_history("SUPERAPP-10397")
    finally:
        await client.aclose()
    assert [(moment.isoformat(), src, dst) for moment, src, dst in history] == [
        ("2026-11-09T06:48:21.060000+00:00", None, "backlog"),
        ("2026-11-09T06:48:48.525000+00:00", "backlog", "todo"),
        ("2026-11-09T06:48:51.745000+00:00", "todo", "development"),
        ("2026-11-09T06:57:06.162000+00:00", "development", "codereview"),
    ]
    assert len(calls) == 3
    assert "field=status" in calls[0] and "perPage=2" in calls[0]


async def test_status_history_is_capped_at_ten_pages(monkeypatch):
    monkeypatch.setattr("lpdev.tracker_client._PER_PAGE", 1)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[_status_change("2026-11-09T06:48:51.745+0000", "todo", "development")], headers={
            "Link": '<https://api.tracker.yandex.net/v3/issues/SUPERAPP-1/changelog?id=n>; rel="next"'})

    client = _client(handler)
    try:
        assert len(await client.status_history("SUPERAPP-1")) == 10
    finally:
        await client.aclose()
