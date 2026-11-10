from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, UTC

import httpx
import pytest

from lpdev.config import TrackerConfig
from lpdev.db import timesheet_cell_set, timesheet_cells, timesheet_tasks, worklog_submissions
from lpdev.gitlab_client import GitlabApiError
from lpdev.main import create_app
from lpdev.tracker_client import TrackerApiError, TrackerUncertain, Worklog
from lpdev.web.timesheet_routes import TimesheetState

H = {"X-Requested-With": "lpdev"}
TODAY = date(2026, 11, 10)
TRACKER = TrackerConfig(
    enabled=True, submit_enabled=True, org_id="1", token_command="printf tok", queues=["SUPERAPP"]
)


class FakeTracker:
    def __init__(self) -> None:
        self.worklogs: list[Worklog] = []
        self.issues = [
            {"key": "SUPERAPP-5", "summary": "Doing it", "status": {"key": "development"}, "statusType": {"key": "inProgress"}},
            {"key": "SUPERAPP-6", "summary": "Backlog", "status": {"key": "open"}, "statusType": {"key": "new"}},
            {"key": "OTHER-1", "summary": "Other queue", "status": {"key": "codereview"}, "statusType": {"key": "paused"}},
        ]
        self.titles = {"SUPERAPP-1": "Login screen"}
        self.created: list[tuple[str, datetime, int]] = []
        self.mode = "ok"
        self.fail_reads: TrackerApiError | None = None
        self.fail_issue_worklogs = False
        self.issue_worklogs_error: TrackerApiError | None = None
        self.issue_worklogs_delay = 0.0
        self.issue_worklogs_calls: list[str] = []
        self.linked_issues: list[dict] = []
        self.my_issue_queries: list[tuple] = []
        self.histories: dict[str, list] = {}
        self.history_calls: list[str] = []
        self.failing_histories: set[str] = set()
        self.history_active = 0
        self.history_peak = 0
        self.comments: dict[str, list[dict]] = {}
        self.failing_comments: set[str] = set()
        self.fail_link_search = False
        self.link_queries: list[tuple[str, int]] = []
        self.closed = False
        self.next_id = 100

    def _read(self) -> None:
        if self.fail_reads is not None:
            raise self.fail_reads

    async def myself(self):
        self._read()
        return {"login": "60121337", "uid": 5, "trackerUid": 5, "passportUid": 1130000066073403}

    async def search_my_issues(self, statuses, queues, limit):
        self._read()
        self.my_issue_queries.append((tuple(statuses), tuple(queues), limit))
        return [
            issue for issue in self.issues
            if issue["status"]["key"] in statuses and (not queues or issue["key"].rsplit("-", 1)[0] in queues)
        ][:limit]

    async def status_history(self, key):
        self.history_calls.append(key)
        self.history_active += 1
        self.history_peak = max(self.history_peak, self.history_active)
        try:
            await asyncio.sleep(0)
            if key in self.failing_histories:
                raise TrackerApiError("Tracker: ошибка 500", 500)
            return self.histories.get(key, [])
        finally:
            self.history_active -= 1

    async def search_issues(self, query, limit):
        self._read()
        self.link_queries.append((query, limit))
        if self.fail_link_search:
            raise TrackerApiError("Tracker: ошибка 500", 500)
        return self.linked_issues[:limit]

    async def issue_comments(self, key):
        self._read()
        if key in self.failing_comments:
            raise TrackerApiError("Tracker: не найдено", 404)
        return self.comments.get(key, [])

    async def get_issues(self, keys):
        self._read()
        return {key: {"key": key, "summary": self.titles[key]} for key in keys if key in self.titles}

    async def search_worklogs(self, created_by, start_from, start_to):
        self._read()
        assert created_by == "5"
        return [w for w in self.worklogs if start_from <= w.day <= start_to]

    async def issue_worklogs(self, key, authors=None):
        self._read()
        self.issue_worklogs_calls.append(key)
        if self.issue_worklogs_delay:
            await asyncio.sleep(self.issue_worklogs_delay)
        if self.fail_issue_worklogs:
            raise TrackerApiError("Tracker недоступен: ConnectError")
        if self.issue_worklogs_error is not None:
            raise self.issue_worklogs_error
        assert authors is not None and "5" in authors
        return [w for w in self.worklogs if w.key == key]

    def add(self, key: str, day: date, minutes: int) -> str:
        self.next_id += 1
        self.worklogs.append(Worklog(str(self.next_id), key, day, minutes, datetime.now(UTC)))
        return str(self.next_id)

    async def create_worklog(self, key, start, minutes):
        self.created.append((key, start, minutes))
        if self.mode == "error":
            raise TrackerApiError("Tracker: ошибка 422: bad", 422)
        if self.mode == "timeout_created":
            self.add(key, start.date(), minutes)
            raise TrackerUncertain("Tracker не ответил вовремя: ReadTimeout")
        if self.mode == "timeout_lost":
            raise TrackerUncertain("Tracker не ответил вовремя: ReadTimeout")
        if self.mode == "no_id":
            self.add(key, start.date(), minutes)
            return ""
        return self.add(key, start.date(), minutes)

    async def aclose(self):
        self.closed = True


class FakeGitlab:
    def __init__(self) -> None:
        self.mrs: dict[tuple[int, int], dict] = {}
        self.branch_mrs: dict[tuple[int, str], list[dict]] = {}
        self.default_branches: dict[int, str] = {10: "develop"}
        self.calls: list[tuple] = []
        self.fail: set[str] = set()

    def install(self, api) -> None:
        api.get_user = self.get_user
        api.get_project_mr = self.get_project_mr
        api.get_merge_requests_by_branch = self.get_merge_requests_by_branch
        api.get_project_by_path = self.get_project_by_path

    def _maybe_fail(self, name: str) -> None:
        if name in self.fail:
            raise GitlabApiError("gitlab error status 500", status=500)

    async def get_user(self):
        self.calls.append(("user",))
        self._maybe_fail("user")
        return {"id": 1, "username": "me"}

    async def get_project_mr(self, project_id, iid):
        self.calls.append(("mr", project_id, iid))
        self._maybe_fail("mr")
        if (project_id, iid) not in self.mrs:
            raise GitlabApiError("gitlab error status 404", status=404)
        return self.mrs[(project_id, iid)]

    async def get_merge_requests_by_branch(self, project_id, branch):
        self.calls.append(("branch", project_id, branch))
        self._maybe_fail("branch")
        return self.branch_mrs.get((project_id, branch), [])

    async def get_project_by_path(self, path):
        self.calls.append(("project", path))
        self._maybe_fail("project")
        return {"id": int(path), "default_branch": self.default_branches.get(int(path), "main"),
                "path_with_namespace": getattr(self, "paths", {}).get(int(path), "g/p")}


EVENTS = [
    {"action_name": "pushed to", "created_at": "2026-11-09T09:00:00Z", "project_id": 10,
     "push_data": {"ref": "feature/SUPERAPP-1-login", "commit_title": "wip", "commit_count": 1}},
    {"action_name": "pushed to", "created_at": "2026-11-09T10:00:00Z", "project_id": 10,
     "push_data": {"ref": "task/bump", "commit_title": "Bump", "commit_count": 1}},
    {"action_name": "opened", "created_at": "2026-11-10T10:00:00Z", "project_id": 10, "target_type": "MergeRequest",
     "target_iid": 1, "target_title": "Without key"},
    {"action_name": "pushed to", "created_at": "2026-11-20T10:00:00Z", "project_id": 10,
     "push_data": {"ref": "feature/SUPERAPP-99", "commit_title": "later", "commit_count": 1}},
    "junk",
]


@pytest.fixture
def env(ctx, monkeypatch):
    ctx_obj, _ = ctx
    ctx_obj.config = ctx_obj.config.model_copy(update={"tracker": TRACKER})
    monkeypatch.setattr("lpdev.web.timesheet_routes._today", lambda cfg: TODAY)
    gitlab_calls = []

    async def get_my_events(after, before):
        gitlab_calls.append((after, before))
        if getattr(get_my_events, "fail", False):
            raise GitlabApiError("gitlab error status 502", status=502)
        return getattr(get_my_events, "events", EVENTS)

    ctx_obj.api.get_my_events = get_my_events
    gitlab = FakeGitlab()
    gitlab.install(ctx_obj.api)
    tracker = FakeTracker()
    factory_calls = []
    app = create_app(ctx_obj)

    def factory(cfg):
        factory_calls.append(cfg)
        return tracker

    app.state.timesheet = TimesheetState(tracker_factory=factory)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8123")
    yield {
        "ctx": ctx_obj, "app": app, "client": client, "tracker": tracker,
        "gitlab": get_my_events, "gitlab_calls": gitlab_calls, "factory_calls": factory_calls, "fake_gitlab": gitlab,
    }


async def _get(env, week=None):
    params = {"week": week} if week else None
    response = await env["client"].get("/api/timesheet", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _rows(payload):
    return {row["key"]: row for row in payload["rows"]}


async def test_timesheet_page_renders_with_active_sidebar(env):
    response = await env["client"].get("/timesheet")
    assert response.status_code == 200
    assert '<a href="/timesheet" class="active" aria-current="page"' in response.text


async def test_api_timesheet_current_week(env):
    env["tracker"].add("SUPERAPP-1", date(2026, 11, 9), 60)
    env["tracker"].add("SUPERAPP-1", date(2026, 10, 1), 60)
    env["ctx"].conn.execute(
        "UPDATE mr SET source_branch = 'feature/SUPERAPP-7-x' WHERE iid = 1"
    )
    payload = await _get(env)
    assert (payload["week"], payload["prev_week"], payload["next_week"], payload["today"]) == (
        "2026-W46", "2026-W45", "2026-W47", "2026-11-10"
    )
    assert payload["calendar_ok"] is True
    assert payload["step"] == 30
    assert payload["tracker"] == {"enabled": True, "submit_enabled": True, "error": None}
    assert [day["date"] for day in payload["days"]][0] == "2026-11-09"
    assert payload["days"][0]["logged"] == 60
    rows = _rows(payload)
    assert set(rows) == {"SUPERAPP-1", "SUPERAPP-5", "SUPERAPP-7"}
    assert rows["SUPERAPP-1"]["title"] == "Login screen"
    assert rows["SUPERAPP-5"]["title"] == "Doing it"
    assert rows["SUPERAPP-7"]["sources"] == ["mr"]
    monday = rows["SUPERAPP-1"]["cells"]["2026-11-09"]
    assert monday["logged"] == 60
    assert monday["suggested"] + payload["unkeyed"]["cells"]["2026-11-09"]["suggested"] + rows["SUPERAPP-5"]["cells"]["2026-11-09"]["suggested"] == 420
    assert payload["unkeyed"]["items"] == [{"title": "task/bump", "url": None}]
    assert "SUPERAPP-99" not in rows
    assert env["gitlab_calls"] == [(date(2026, 11, 7), date(2026, 11, 17))]


async def test_events_are_cached(env):
    await _get(env)
    await _get(env)
    await _get(env, "2026-W45")
    assert len(env["gitlab_calls"]) == 2


async def test_tracker_disabled_uses_gitlab_only(env):
    env["ctx"].config = env["ctx"].config.model_copy(update={"tracker": TrackerConfig(queues=["SUPERAPP"])})
    payload = await _get(env)
    assert payload["tracker"] == {"enabled": False, "submit_enabled": False, "error": None}
    assert set(_rows(payload)) == {"SUPERAPP-1"}
    assert env["factory_calls"] == []


async def test_tracker_error_is_reported_and_blocks_submit(env):
    env["tracker"].fail_reads = TrackerApiError("токен Tracker недействителен или истёк", 401)
    payload = await _get(env)
    assert payload["tracker"] == {"enabled": True, "submit_enabled": False, "error": "токен Tracker недействителен или истёк"}
    assert set(_rows(payload)) == {"SUPERAPP-1"}


async def test_gitlab_error_is_bad_gateway(env):
    env["gitlab"].fail = True
    response = await env["client"].get("/api/timesheet")
    assert response.status_code == 502
    assert "GitLab" in response.json()["detail"]


@pytest.mark.parametrize("week", ["2026-46", "2026-W60", "x"])
async def test_invalid_week(env, week):
    response = await env["client"].get("/api/timesheet", params={"week": week})
    assert response.status_code == 422


async def test_unknown_year_reports_calendar_not_ok(env):
    payload = await _get(env, "2099-W10")
    assert payload["calendar_ok"] is False
    assert not any(day["submittable"] for day in payload["days"])


async def test_tracker_client_is_rebuilt_on_config_change_and_closed(env):
    await _get(env)
    env["ctx"].config = env["ctx"].config.model_copy(update={"tracker": TRACKER.model_copy(update={"day_hours": 6})})
    await _get(env)
    assert len(env["factory_calls"]) == 2
    assert env["tracker"].closed
    env["tracker"].closed = False
    await env["app"].state.timesheet.aclose()
    assert env["tracker"].closed


async def test_put_cell_sets_and_resets(env):
    client, conn = env["client"], env["ctx"].conn
    response = await client.put("/api/timesheet/cell", json={"key": "SUPERAPP-1", "date": "2026-11-09", "minutes": 90}, headers=H)
    assert response.json() == {"ok": True}
    assert timesheet_cells(conn, ["2026-11-09"]) == {("SUPERAPP-1", "2026-11-09"): 90}
    payload = await _get(env)
    assert _rows(payload)["SUPERAPP-1"]["cells"]["2026-11-09"]["edited"] == 90
    await client.put("/api/timesheet/cell", json={"key": "SUPERAPP-1", "date": "2026-11-09", "minutes": None}, headers=H)
    assert timesheet_cells(conn, ["2026-11-09"]) == {}


@pytest.mark.parametrize(
    "body",
    [
        {"key": "superapp-1", "date": "2026-11-09", "minutes": 30},
        {"key": "OTHER-1", "date": "2026-11-09", "minutes": 30},
        {"key": "SUPERAPP-1", "date": "2026-11-01", "minutes": 30},
        {"key": "SUPERAPP-1", "date": "2026-11-11", "minutes": 30},
        {"key": "SUPERAPP-1", "date": "11/09/2026", "minutes": 30},
        {"key": "SUPERAPP-1", "date": 20261109, "minutes": 30},
        {"key": "SUPERAPP-1", "date": "2026-11-09", "minutes": 45},
        {"key": "SUPERAPP-1", "date": "2026-11-09", "minutes": 510},
        {"key": "SUPERAPP-1", "date": "2026-11-09", "minutes": -30},
        {"key": "SUPERAPP-1", "date": "2026-11-09", "minutes": True},
        {"key": "SUPERAPP-1", "date": "2026-11-09", "minutes": "60"},
        {"key": "SUPERAPP-1", "date": "2026-11-04", "minutes": 30},
    ],
)
async def test_put_cell_validation(env, body):
    response = await env["client"].put("/api/timesheet/cell", json=body, headers=H)
    assert response.status_code == 422
    assert response.json()["detail"]


@pytest.mark.parametrize("content", [b"not json", b"[1]", b""])
async def test_bad_bodies(env, content):
    response = await env["client"].put("/api/timesheet/cell", content=content, headers=H)
    assert response.status_code == 422


async def test_delete_cells_for_week_and_day_within_window(env):
    conn = env["ctx"].conn
    for day in ("2026-11-02", "2026-11-09", "2026-11-10"):
        timesheet_cell_set(conn, "SUPERAPP-1", day, 30)
    timesheet_cell_set(conn, "SUPERAPP-1", "2026-10-26", 30)
    response = await env["client"].delete("/api/timesheet/cells", params={"week": "2026-W46", "date": "2026-11-10"}, headers=H)
    assert response.json() == {"ok": True}
    assert set(timesheet_cells(conn, ["2026-11-09", "2026-11-10"])) == {("SUPERAPP-1", "2026-11-09")}
    await env["client"].delete("/api/timesheet/cells", params={"week": "2026-W46"}, headers=H)
    await env["client"].delete("/api/timesheet/cells", params={"week": "2026-W44"}, headers=H)
    assert set(timesheet_cells(conn, ["2026-11-02", "2026-11-09", "2026-10-26"])) == {
        ("SUPERAPP-1", "2026-11-02"), ("SUPERAPP-1", "2026-10-26")
    }


@pytest.mark.parametrize("params", [{}, {"week": "bad"}, {"week": "2026-W46", "date": "2026-11-02"}, {"week": "2026-W46", "date": "x"}])
async def test_delete_cells_validation(env, params):
    response = await env["client"].delete("/api/timesheet/cells", params=params, headers=H)
    assert response.status_code == 422


async def test_add_task(env):
    response = await env["client"].post("/api/timesheet/tasks", json={"key": "SUPERAPP-42", "week": "2026-W45"}, headers=H)
    assert response.json() == {"ok": True}
    assert timesheet_tasks(env["ctx"].conn, "2026-11-02") == ["SUPERAPP-42"]
    payload = await _get(env, "2026-W45")
    assert _rows(payload)["SUPERAPP-42"]["sources"] == ["manual"]


@pytest.mark.parametrize(
    "body",
    [
        {"key": "OTHER-1", "week": "2026-W46"},
        {"key": "SUPERAPP-1 ", "week": "2026-W46"},
        {"key": "SUPERAPP-1", "week": "2026-W44"},
        {"key": "SUPERAPP-1", "week": "2026-W47"},
        {"key": "SUPERAPP-1", "week": None},
    ],
)
async def test_add_task_validation(env, body):
    response = await env["client"].post("/api/timesheet/tasks", json=body, headers=H)
    assert response.status_code == 422


def _age_journal(env, minutes: int) -> None:
    moment = (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    env["ctx"].conn.execute("UPDATE worklog_submission SET created_at = ?", (moment,))


async def _submit(env, entries):
    response = await env["client"].post("/api/timesheet/submit", json={"entries": entries}, headers=H)
    assert response.status_code == 200, response.text
    return response.json()["results"]


ENTRY = {"key": "SUPERAPP-1", "date": "2026-11-09", "minutes": 120}


async def test_submit_creates_worklog_and_clears_edit(env):
    conn = env["ctx"].conn
    timesheet_cell_set(conn, "SUPERAPP-1", "2026-11-09", 120)
    results = await _submit(env, [ENTRY])
    assert results == [{**ENTRY, "status": "done", "error": None}]
    key, start, minutes = env["tracker"].created[0]
    assert (key, start.isoformat(), minutes) == ("SUPERAPP-1", "2026-11-09T09:00:00+03:00", 120)
    assert timesheet_cells(conn, ["2026-11-09"]) == {}
    rows = worklog_submissions(conn, ["2026-11-09"])
    assert [(r["status"], r["tracker_id"]) for r in rows] == [("done", "101")]


async def test_repeated_submit_is_skipped(env):
    await _submit(env, [ENTRY])
    results = await _submit(env, [ENTRY])
    assert results[0]["status"] == "skipped"
    assert len(env["tracker"].created) == 1


async def test_submit_counts_journal_entries_not_yet_visible_in_tracker(env):
    await _submit(env, [{**ENTRY, "minutes": 300}])
    env["tracker"].worklogs.clear()
    results = await _submit(env, [{**ENTRY, "key": "SUPERAPP-2", "minutes": 240}])
    assert results[0]["status"] == "failed"
    assert results[0]["error"] == "превышена норма дня"
    payload = await _get(env)
    assert payload["days"][0]["logged"] == 300


async def test_submit_rejects_over_norm_including_batch(env):
    env["tracker"].add("OTHER-1", date(2026, 11, 9), 300)
    results = await _submit(env, [
        {"key": "SUPERAPP-1", "date": "2026-11-09", "minutes": 120},
        {"key": "SUPERAPP-2", "date": "2026-11-09", "minutes": 120},
        {"key": "SUPERAPP-3", "date": "2026-11-10", "minutes": 480},
    ])
    assert [r["status"] for r in results] == ["done", "failed", "done"]


async def test_submit_timeout_confirmed_by_reconciliation(env):
    env["tracker"].mode = "timeout_created"
    results = await _submit(env, [ENTRY])
    assert results[0]["status"] == "done"
    rows = worklog_submissions(env["ctx"].conn, ["2026-11-09"])
    assert [(r["status"], r["tracker_id"]) for r in rows] == [("done", "101")]


async def test_submit_timeout_lost_stays_unknown_then_resolves_on_next_submit(env):
    env["tracker"].mode = "timeout_lost"
    results = await _submit(env, [ENTRY])
    assert results[0]["status"] == "unknown"
    payload = await _get(env)
    assert _rows(payload)["SUPERAPP-1"]["cells"]["2026-11-09"]["submission"] == "unknown"
    env["tracker"].mode = "ok"
    results = await _submit(env, [ENTRY])
    assert results[0]["status"] == "unknown"
    _age_journal(env, minutes=3)
    results = await _submit(env, [ENTRY])
    assert results[0]["status"] == "done"
    statuses = [r["status"] for r in worklog_submissions(env["ctx"].conn, ["2026-11-09"])]
    assert statuses == ["failed", "done"]
    assert len(env["tracker"].created) == 2


async def test_unknown_that_actually_landed_is_reconciled_and_not_duplicated(env):
    env["tracker"].mode = "timeout_lost"
    await _submit(env, [ENTRY])
    env["tracker"].add("SUPERAPP-1", date(2026, 11, 9), 120)
    env["tracker"].mode = "ok"
    results = await _submit(env, [ENTRY])
    assert results[0]["status"] == "skipped"
    assert len(env["tracker"].created) == 1
    assert [r["status"] for r in worklog_submissions(env["ctx"].conn, ["2026-11-09"])] == ["done"]


async def test_reconciliation_ignores_old_matching_worklogs(env):
    old = Worklog("1", "SUPERAPP-1", date(2026, 11, 9), 120, datetime.now(UTC) - timedelta(days=1))
    env["tracker"].worklogs.append(old)
    env["tracker"].mode = "timeout_lost"
    results = await _submit(env, [ENTRY])
    assert results[0]["status"] == "unknown"


async def test_submit_api_error_is_failed(env):
    env["tracker"].mode = "error"
    results = await _submit(env, [ENTRY])
    assert results[0] == {**ENTRY, "status": "failed", "error": "Tracker: ошибка 422: bad"}
    payload = await _get(env)
    assert _rows(payload)["SUPERAPP-1"]["cells"]["2026-11-09"]["submission"] == "failed"


async def test_submit_tracker_read_failure_fails_everything(env):
    env["tracker"].fail_reads = TrackerApiError("Tracker недоступен: ConnectError")
    results = await _submit(env, [ENTRY, {**ENTRY, "date": "2026-11-10"}])
    assert [r["status"] for r in results] == ["failed", "failed"]
    assert env["tracker"].created == []


@pytest.mark.parametrize(
    "body",
    [
        {"entries": []},
        {"entries": "x"},
        {"entries": [1]},
        {"entries": [ENTRY, ENTRY]},
        {"entries": [{**ENTRY, "minutes": 0}]},
        {"entries": [{**ENTRY, "minutes": 50}]},
        {"entries": [{**ENTRY, "key": "SUPERAPP-1/../x"}]},
        {"entries": [{**ENTRY, "key": "OTHER-1"}]},
        {"entries": [{**ENTRY, "date": "2026-11-11"}]},
        {"entries": [{**ENTRY, "date": "2026-10-30"}]},
        {"entries": [{**ENTRY, "date": "2026-11-04"}]},
        {"entries": [{"key": f"SUPERAPP-{n}", "date": "2026-11-09", "minutes": 30} for n in range(51)]},
    ],
)
async def test_submit_validation(env, body):
    response = await env["client"].post("/api/timesheet/submit", json=body, headers=H)
    assert response.status_code == 422
    assert env["tracker"].created == []


@pytest.mark.parametrize(
    "tracker",
    [TrackerConfig(), TrackerConfig(enabled=True, org_id="1", token_command="printf tok")],
)
async def test_submit_forbidden_when_disabled(env, tracker):
    env["ctx"].config = env["ctx"].config.model_copy(update={"tracker": tracker})
    response = await env["client"].post("/api/timesheet/submit", json={"entries": [ENTRY]}, headers=H)
    assert response.status_code == 403
    assert env["tracker"].created == []


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("PUT", "/api/timesheet/cell"),
        ("POST", "/api/timesheet/tasks"),
        ("POST", "/api/timesheet/submit"),
        ("DELETE", "/api/timesheet/cells?week=2026-W46"),
    ],
)
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-Requested-With": "XMLHttpRequest"},
        {**H, "Origin": "https://evil.example.com"},
        {**H, "Sec-Fetch-Site": "cross-site"},
        {**H, "Host": "evil.example.com"},
    ],
)
async def test_mutations_require_same_origin_and_header(env, method, path, headers):
    response = await env["client"].request(method, path, json={"entries": [ENTRY], **ENTRY, "week": "2026-W46"}, headers=headers)
    assert response.status_code == 403
    assert env["tracker"].created == []
    assert timesheet_cells(env["ctx"].conn, ["2026-11-09"]) == {}


async def test_foreign_host_cannot_read_timesheet(env):
    response = await env["client"].get("/api/timesheet", headers={"Host": "evil.example.com"})
    assert response.status_code == 403


async def test_default_tracker_factory_reports_token_failure(ctx):
    from lpdev.web.timesheet_routes import _default_tracker_factory

    client = _default_tracker_factory(TrackerConfig(enabled=True, org_id="1", token_command="exit 3"))
    try:
        with pytest.raises(TrackerApiError, match="не удалось получить токен Tracker"):
            await client.myself()
    finally:
        await client.aclose()


async def test_reconciled_cell_is_skipped_even_after_the_duplicate_window(env):
    env["tracker"].mode = "timeout_lost"
    await _submit(env, [ENTRY])
    _age_journal(env, minutes=11)
    env["tracker"].worklogs.append(
        Worklog("900", "SUPERAPP-1", date(2026, 11, 9), 120, datetime.now(UTC) - timedelta(minutes=10))
    )
    env["tracker"].mode = "ok"
    results = await _submit(env, [ENTRY])
    assert results[0] == {**ENTRY, "status": "skipped", "error": "уже списано, подтверждено сверкой"}
    assert len(env["tracker"].created) == 1


async def test_failed_reconciliation_read_blocks_the_post(env):
    env["tracker"].mode = "timeout_lost"
    await _submit(env, [ENTRY])
    _age_journal(env, minutes=5)
    env["tracker"].mode = "ok"
    env["tracker"].fail_issue_worklogs = True
    results = await _submit(env, [ENTRY, {**ENTRY, "key": "SUPERAPP-2"}])
    assert results[0] == {**ENTRY, "status": "unknown", "error": "не удалось сверить с Tracker, повторите позже"}
    assert results[1]["status"] == "done"
    assert [c[0] for c in env["tracker"].created] == ["SUPERAPP-1", "SUPERAPP-2"]


async def test_fresh_unknown_without_match_keeps_blocking(env):
    env["tracker"].mode = "timeout_lost"
    await _submit(env, [ENTRY])
    await _get(env)
    statuses = [r["status"] for r in worklog_submissions(env["ctx"].conn, ["2026-11-09"])]
    assert statuses == ["unknown"]


async def test_get_reconciles_open_rows_after_restart(env):
    from lpdev.db import worklog_submission_create

    conn = env["ctx"].conn
    worklog_submission_create(conn, "SUPERAPP-1", "2026-11-09", 120)
    worklog_submission_create(conn, "SUPERAPP-2", "2026-11-10", 60)
    _age_journal(env, minutes=5)
    env["tracker"].worklogs.append(
        Worklog("900", "SUPERAPP-1", date(2026, 11, 9), 120, datetime.now(UTC) - timedelta(minutes=4))
    )
    timesheet_cell_set(conn, "SUPERAPP-1", "2026-11-09", 120)
    payload = await _get(env)
    rows = worklog_submissions(conn, ["2026-11-09", "2026-11-10"])
    assert [(r["issue_key"], r["status"], r["tracker_id"]) for r in rows] == [
        ("SUPERAPP-1", "done", "900"), ("SUPERAPP-2", "failed", None)
    ]
    cells = _rows(payload)
    assert cells["SUPERAPP-1"]["cells"]["2026-11-09"]["submission"] is None
    assert cells["SUPERAPP-1"]["cells"]["2026-11-09"]["edited"] is None
    assert cells["SUPERAPP-2"]["cells"]["2026-11-10"]["submission"] == "failed"
    results = await _submit(env, [{"key": "SUPERAPP-2", "date": "2026-11-10", "minutes": 60}])
    assert results[0]["status"] == "done"


async def test_get_reconciliation_is_capped(env):
    from lpdev.db import worklog_submission_create

    conn = env["ctx"].conn
    for n in range(25):
        worklog_submission_create(conn, f"SUPERAPP-{n + 1}", "2026-11-09", 30)
    _age_journal(env, minutes=5)
    await _get(env)
    statuses = [r["status"] for r in worklog_submissions(conn, ["2026-11-09"])]
    assert statuses.count("failed") == 20
    assert statuses.count("pending") == 5


async def test_get_skips_reconciliation_while_submit_is_running(env):
    from lpdev.db import worklog_submission_create

    conn = env["ctx"].conn
    worklog_submission_create(conn, "SUPERAPP-1", "2026-11-09", 120)
    _age_journal(env, minutes=5)
    async with env["app"].state.timesheet.submit_lock:
        payload = await _get(env)
    assert _rows(payload)["SUPERAPP-1"]["cells"]["2026-11-09"]["submission"] == "pending"


async def test_get_does_not_reconcile_when_tracker_disabled(env):
    from lpdev.db import worklog_submission_create

    conn = env["ctx"].conn
    worklog_submission_create(conn, "SUPERAPP-1", "2026-11-09", 120)
    _age_journal(env, minutes=5)
    env["ctx"].config = env["ctx"].config.model_copy(update={"tracker": TrackerConfig(queues=["SUPERAPP"])})
    await _get(env)
    assert [r["status"] for r in worklog_submissions(conn, ["2026-11-09"])] == ["pending"]


async def test_create_without_id_is_confirmed_by_reconciliation(env):
    env["tracker"].mode = "no_id"
    results = await _submit(env, [ENTRY])
    assert results[0]["status"] == "done"
    rows = worklog_submissions(env["ctx"].conn, ["2026-11-09"])
    assert [(r["status"], r["tracker_id"]) for r in rows] == [("done", "101")]


def test_matches_requires_created_at():
    from lpdev.web.timesheet_routes import _matches

    worklog = Worklog("1", "SUPERAPP-1", date(2026, 11, 9), 30, None)
    assert not _matches(worklog, "SUPERAPP-1", date(2026, 11, 9), 30, datetime.now(UTC), set())


@pytest.mark.parametrize(
    "error",
    [
        TrackerApiError("Tracker: не найдено", 404),
        TrackerApiError("Tracker: нет доступа (проверьте org_id, org_header и scope токена)", 403),
        TrackerApiError("Tracker: неожиданный редирект 301", 301),
    ],
)
async def test_permanent_reconcile_errors_fail_the_row(env, error):
    env["tracker"].mode = "timeout_lost"
    await _submit(env, [ENTRY])
    _age_journal(env, minutes=5)
    env["tracker"].issue_worklogs_error = error
    await _get(env)
    row = worklog_submissions(env["ctx"].conn, ["2026-11-09"])[0]
    assert row["status"] == "failed"
    assert row["error"] == f"сверка невозможна: {error.message}"


async def test_transient_reconcile_errors_count_attempts(env):
    from lpdev.db import worklog_submission_create

    conn = env["ctx"].conn
    worklog_submission_create(conn, "SUPERAPP-1", "2026-11-09", 120)
    _age_journal(env, minutes=5)
    env["tracker"].issue_worklogs_error = TrackerApiError("Tracker: ошибка 500", 500)
    await _get(env)
    await _get(env)
    row = worklog_submissions(conn, ["2026-11-09"])[0]
    assert (row["status"], row["reconcile_attempts"]) == ("pending", 2)


async def test_get_reconciles_least_attempted_rows_first(env):
    from lpdev.db import worklog_submission_create

    conn = env["ctx"].conn
    ids = [worklog_submission_create(conn, f"SUPERAPP-{n + 1}", "2026-11-09", 30) for n in range(25)]
    _age_journal(env, minutes=5)
    conn.execute(f"UPDATE worklog_submission SET reconcile_attempts = 7 WHERE id <= {ids[19]}")
    await _get(env)
    assert env["tracker"].issue_worklogs_calls[:5] == [f"SUPERAPP-{n}" for n in range(21, 26)]
    assert len(env["tracker"].issue_worklogs_calls) == 20


async def test_get_does_not_poll_fresh_rows(env):
    from lpdev.db import worklog_submission_create

    worklog_submission_create(env["ctx"].conn, "SUPERAPP-1", "2026-11-09", 120)
    await _get(env)
    assert env["tracker"].issue_worklogs_calls == []


async def test_get_reconciliation_is_time_boxed_and_releases_the_lock(env, monkeypatch):
    from lpdev.db import worklog_submission_create

    monkeypatch.setattr("lpdev.web.timesheet_routes.RECONCILE_ON_READ_TIMEOUT_SEC", 0.05)
    worklog_submission_create(env["ctx"].conn, "SUPERAPP-1", "2026-11-09", 120)
    _age_journal(env, minutes=5)
    env["tracker"].issue_worklogs_delay = 5
    started = asyncio.get_running_loop().time()
    payload = await _get(env)
    assert asyncio.get_running_loop().time() - started < 2
    assert _rows(payload)["SUPERAPP-1"]["cells"]["2026-11-09"]["submission"] == "pending"
    assert not env["app"].state.timesheet.submit_lock.locked()


def _ev_push(ref, title, at="2026-11-09T09:00:00Z", project=10, count=1, action_name="pushed to"):
    return {"project_id": project, "action_name": action_name, "target_type": None, "target_iid": None,
            "target_title": "lemanapro-mobile", "created_at": at,
            "push_data": {"commit_count": count, "action": "pushed", "ref_type": "branch", "ref": ref, "commit_title": title}}


def _ev_mr(action, iid, title, at="2026-11-09T11:00:00Z", project=10):
    return {"project_id": project, "action_name": action, "target_type": "MergeRequest", "target_iid": iid,
            "target_title": title, "created_at": at}


def _ev_note(iid, title, at="2026-11-09T12:00:00Z", project=10):
    return {"project_id": project, "action_name": "commented on", "target_type": "DiffNote", "target_iid": None,
            "target_title": title, "created_at": at, "note": {"noteable_type": "MergeRequest", "noteable_iid": iid}}


def _gl_mr(iid, title, branch, author, project=10):
    return {"iid": iid, "project_id": project, "title": title, "source_branch": branch,
            "web_url": f"https://gitlab.example.com/g/p/-/merge_requests/{iid}", "author": {"username": author}}


async def test_push_without_key_is_attributed_through_the_branch_merge_request(env):
    fake = env["fake_gitlab"]
    fake.branch_mrs[(10, "task/remove_old_snackbar")] = [
        _gl_mr(5172, "SUPERAPP-10395: Remove old snackbar", "task/remove_old_snackbar", "me")
    ]
    env["gitlab"].events = [
        _ev_push("task/remove_old_snackbar", "same behaviour", action_name="pushed new"),
        _ev_push("task/remove_old_snackbar", "ktlint"),
        _ev_push("develop", "Merge branch 'task/remove_old_snackbar' into 'develop'"),
        {**_ev_push("task/remove_old_snackbar", ""), "action_name": "deleted",
         "push_data": {"commit_count": 0, "action": "removed", "ref_type": "branch", "ref": "task/remove_old_snackbar"}},
    ]
    payload = await _get(env)
    rows = _rows(payload)
    assert rows["SUPERAPP-10395"]["sources"] == ["push"]
    assert rows["SUPERAPP-10395"]["title"] == "Remove old snackbar"
    assert payload["unkeyed"] is None
    assert fake.calls.count(("branch", 10, "task/remove_old_snackbar")) == 1
    env["app"].state.timesheet.events.clear()
    await _get(env)
    assert fake.calls.count(("branch", 10, "task/remove_old_snackbar")) == 1
    assert fake.calls.count(("project", "10")) == 1


async def test_merge_request_from_events_resolves_pushes_without_branch_search(env):
    fake = env["fake_gitlab"]
    fake.mrs[(10, 77)] = _gl_mr(77, "SUPERAPP-500: Gesture", "task/ios_back_gesture", "me")
    env["gitlab"].events = [
        _ev_push("task/ios_back_gesture", "Fix gesture", count=2),
        _ev_mr("accepted", 77, "SUPERAPP-500: Gesture"),
    ]
    rows = _rows(await _get(env))
    assert rows["SUPERAPP-500"]["sources"] == ["mr", "push"]
    assert not any(call[0] == "branch" for call in env["fake_gitlab"].calls)


async def test_unkeyed_branch_is_grouped_and_links_to_its_merge_request(env):
    fake = env["fake_gitlab"]
    fake.branch_mrs[(10, "task/cleanup")] = [_gl_mr(90, "Cleanup", "task/cleanup", "me")]
    env["gitlab"].events = [_ev_push("task/cleanup", "one"), _ev_push("task/cleanup", "two"), _ev_push("task/lonely", "x")]
    payload = await _get(env)
    assert payload["unkeyed"]["items"] == [
        {"title": "Cleanup", "url": "https://gitlab.example.com/g/p/-/merge_requests/90"},
        {"title": "task/lonely", "url": None},
    ]


async def test_reviews_on_foreign_merge_requests_are_ignored(env):
    fake = env["fake_gitlab"]
    fake.mrs[(10, 6)] = _gl_mr(6, "SUPERAPP-10126: Their feature", "feature/SUPERAPP-10126", "colleague")
    fake.mrs[(10, 8)] = _gl_mr(8, "Расширить скоупы источников для пропуска стадии скриншот тестов", "task/scopes", "colleague")
    env["ctx"].conn.execute("UPDATE mr SET title = 'SUPERAPP-10349: Mine', author_username = 'me' WHERE iid = 1")
    env["gitlab"].events = [
        _ev_mr("approved", 6, "SUPERAPP-10126: Their feature"),
        _ev_note(6, "SUPERAPP-10126: Their feature"),
        _ev_mr("accepted", 6, "SUPERAPP-10126: Their feature"),
        _ev_mr("approved", 8, "Расширить скоупы источников для пропуска стадии скриншот тестов"),
        _ev_note(8, "Расширить скоупы источников для пропуска стадии скриншот тестов"),
        _ev_note(1, "SUPERAPP-10349: Mine"),
    ]
    env["ctx"].config = env["ctx"].config.model_copy(update={"tracker": TrackerConfig(queues=["SUPERAPP"])})
    payload = await _get(env)
    rows = _rows(payload)
    assert set(rows) == {"SUPERAPP-10349"}
    assert rows["SUPERAPP-10349"]["sources"] == ["comment"]
    assert payload["unkeyed"] is None
    assert not any(call == ("mr", 10, 1) for call in fake.calls)


async def test_review_events_are_ignored_when_the_author_cannot_be_resolved(env, caplog):
    fake = env["fake_gitlab"]
    fake.fail.add("mr")
    env["gitlab"].events = [_ev_note(6, "SUPERAPP-10300: Something")]
    env["ctx"].config = env["ctx"].config.model_copy(update={"tracker": TrackerConfig(queues=["SUPERAPP"])})
    payload = await _get(env)
    assert payload["rows"] == []
    assert "merge request 10!6 lookup failed" in caplog.text


async def test_review_events_are_ignored_when_my_username_is_unknown(env):
    fake = env["fake_gitlab"]
    fake.fail.add("user")
    env["ctx"].conn.execute("UPDATE mr SET title = 'SUPERAPP-10349: Mine', author_username = 'me' WHERE iid = 1")
    env["gitlab"].events = [_ev_note(1, "SUPERAPP-10349: Mine"), _ev_push("feature/SUPERAPP-1", "x")]
    env["ctx"].config = env["ctx"].config.model_copy(update={"tracker": TrackerConfig(queues=["SUPERAPP"])})
    rows = _rows(await _get(env))
    assert set(rows) == {"SUPERAPP-1"}


async def test_pushes_to_the_project_default_and_configured_branches_are_ignored(env):
    fake = env["fake_gitlab"]
    fake.default_branches[10] = "trunk"
    env["gitlab"].events = [_ev_push("trunk", "SUPERAPP-1 direct"), _ev_push("stable/x", "SUPERAPP-2 fix"),
                            _ev_push("hotfix/8.1", "SUPERAPP-3 fix")]
    tracker = TrackerConfig(queues=["SUPERAPP"], ignored_push_branches=["stable/*"])
    env["ctx"].config = env["ctx"].config.model_copy(update={"tracker": tracker})
    rows = _rows(await _get(env))
    assert set(rows) == {"SUPERAPP-3"}


async def test_gitlab_lookup_failures_degrade_gracefully(env):
    fake = env["fake_gitlab"]
    fake.fail |= {"project", "branch"}
    env["gitlab"].events = [_ev_push("task/x", "no key"), _ev_push("feature/SUPERAPP-1", "x")]
    payload = await _get(env)
    assert set(_rows(payload)) == {"SUPERAPP-1", "SUPERAPP-5"}
    assert payload["unkeyed"]["items"] == [{"title": "task/x", "url": None}]


async def test_gitlab_lookups_are_capped_per_request(env, monkeypatch):
    monkeypatch.setattr("lpdev.web.timesheet_routes.MAX_GITLAB_LOOKUPS", 3)
    env["gitlab"].events = [_ev_push(f"task/b{n}", "x") for n in range(10)]
    await _get(env)
    assert len([call for call in env["fake_gitlab"].calls if call[0] != "user"]) == 3


def _comment(text, login=None, display="Someone"):
    author = {"display": display}
    if login:
        author["login"] = login
    return {"id": 1, "text": text, "createdBy": author}


MR_URL = "https://gitlab.example.com/g/p/-/merge_requests/{}"


def _linked(env, key, *comments, assignee="me"):
    env["tracker"].linked_issues.append({"key": key, "assignee": {"login": assignee, "id": "5"}})
    env["tracker"].comments[key] = list(comments)


async def test_bot_comment_links_unkeyed_branch_push_and_merge_request(env):
    fake = env["fake_gitlab"]
    fake.branch_mrs[(10, "task/snackbar")] = [{**_gl_mr(5175, "Remove snackbar", "task/snackbar", "me"), "description": ""}]
    fake.mrs[(10, 5176)] = _gl_mr(5176, "Gesture", "task/gesture", "me")
    _linked(env, "SUPERAPP-10397", _comment(f"PR:   {MR_URL.format(5175)}", login="ya360-pr-check"))
    _linked(env, "SUPERAPP-10398", _comment(f"PR: {MR_URL.format(5176)}", login="ya360-pr-check"))
    env["gitlab"].events = [_ev_push("task/snackbar", "ktlint"), _ev_mr("opened", 5176, "Gesture")]
    payload = await _get(env)
    rows = _rows(payload)
    assert rows["SUPERAPP-10397"]["sources"] == ["push"]
    assert rows["SUPERAPP-10398"]["sources"] == ["mr"]
    assert payload["unkeyed"] is None
    query, limit = env["tracker"].link_queries[0]
    assert query == 'Assignee: me() Updated: >= "2026-11-02" "Sort by": Updated DESC'
    assert limit == 49


async def test_human_comments_with_several_links(env):
    fake = env["fake_gitlab"]
    for iid in (1, 2, 3):
        fake.mrs[(10, 100 + iid)] = _gl_mr(100 + iid, f"Part {iid}", f"task/part{iid}", "me")
    _linked(
        env, "SUPERAPP-20",
        _comment(f"Сделал {MR_URL.format(101)}/diffs и {MR_URL.format(102)}#note_7", login="dev"),
        _comment(f"Ещё [MR]({MR_URL.format(103)}?tab=pipelines)", display="Dev Person"),
    )
    env["gitlab"].events = [_ev_mr("opened", 100 + iid, f"Part {iid}") for iid in (1, 2, 3)]
    payload = await _get(env)
    assert set(_rows(payload)) == {"SUPERAPP-20", "SUPERAPP-5"}
    assert payload["unkeyed"] is None


async def test_conflicting_links_prefer_the_bot_or_are_dropped(env):
    fake = env["fake_gitlab"]
    fake.mrs[(10, 201)] = _gl_mr(201, "Bot wins", "task/bot", "me")
    fake.mrs[(10, 202)] = _gl_mr(202, "Ambiguous", "task/ambiguous", "me")
    _linked(env, "SUPERAPP-31", _comment(MR_URL.format(201), login="dev"), _comment(MR_URL.format(202), login="dev"))
    _linked(env, "SUPERAPP-32", _comment(MR_URL.format(201), login="ya360-pr-check"), _comment(MR_URL.format(202), login="dev2"))
    env["gitlab"].events = [_ev_mr("opened", 201, "Bot wins"), _ev_mr("opened", 202, "Ambiguous")]
    payload = await _get(env)
    assert set(_rows(payload)) == {"SUPERAPP-32", "SUPERAPP-5"}
    assert payload["unkeyed"]["items"] == [{"title": "Ambiguous", "url": MR_URL.format(202)}]


async def test_links_from_foreign_hosts_and_other_queues_are_ignored(env):
    env["fake_gitlab"].mrs[(10, 301)] = _gl_mr(301, "Elsewhere", "task/elsewhere", "me")
    _linked(env, "SUPERAPP-40", _comment("https://evil.example.com/g/p/-/merge_requests/301", login="ya360-pr-check"))
    _linked(env, "OTHER-41", _comment(MR_URL.format(301), login="ya360-pr-check"))
    env["gitlab"].events = [_ev_mr("opened", 301, "Elsewhere")]
    payload = await _get(env)
    assert "SUPERAPP-40" not in _rows(payload)
    assert payload["unkeyed"]["items"] == [{"title": "Elsewhere", "url": MR_URL.format(301)}]


async def test_description_closing_keyword_links_merge_request(env):
    fake = env["fake_gitlab"]
    fake.mrs[(10, 401)] = {**_gl_mr(401, "No key", "task/nokey", "me"), "description": "Closes SUPERAPP-50, see SUPERAPP-51"}
    env["gitlab"].events = [_ev_mr("opened", 401, "No key")]
    rows = _rows(await _get(env))
    assert "SUPERAPP-50" in rows
    assert "SUPERAPP-51" not in rows


async def test_links_are_not_used_when_tracker_is_disabled(env):
    env["fake_gitlab"].mrs[(10, 501)] = _gl_mr(501, "No key", "task/nokey", "me")
    _linked(env, "SUPERAPP-60", _comment(MR_URL.format(501), login="ya360-pr-check"))
    env["ctx"].config = env["ctx"].config.model_copy(update={"tracker": TrackerConfig(queues=["SUPERAPP"])})
    env["gitlab"].events = [_ev_mr("opened", 501, "No key")]
    payload = await _get(env)
    assert payload["rows"] == []
    assert env["tracker"].link_queries == []


async def test_link_read_failures_skip_the_source(env, caplog):
    env["fake_gitlab"].mrs[(10, 601)] = _gl_mr(601, "No key", "task/nokey", "me")
    _linked(env, "SUPERAPP-70", _comment(MR_URL.format(601), login="ya360-pr-check"))
    _linked(env, "SUPERAPP-71", _comment(MR_URL.format(601), login="dev"))
    env["tracker"].failing_comments.add("SUPERAPP-71")
    env["gitlab"].events = [_ev_mr("opened", 601, "No key")]
    assert "SUPERAPP-70" in _rows(await _get(env))
    assert "comments of SUPERAPP-71 skipped" in caplog.text
    env["app"].state.timesheet.links.clear()
    env["tracker"].fail_link_search = True
    payload = await _get(env)
    assert "SUPERAPP-70" not in _rows(payload)
    assert "tracker merge request links skipped" in caplog.text


async def test_links_are_cached(env):
    await _get(env)
    await _get(env)
    assert len(env["tracker"].link_queries) == 1


async def test_link_to_a_project_outside_the_local_database_uses_the_project_path(env):
    fake = env["fake_gitlab"]
    fake.mrs[(77, 9)] = _gl_mr(9, "Other repo", "task/other", "me", project=77)
    fake.paths = {77: "Other/Repo"}
    _linked(env, "SUPERAPP-80", _comment("https://gitlab.example.com/other/repo/-/merge_requests/9", login="dev"))
    env["gitlab"].events = [_ev_mr("opened", 9, "Other repo", project=77)]
    assert "SUPERAPP-80" in _rows(await _get(env))


async def test_reconcile_attempt_is_counted_before_the_request(env, monkeypatch):
    from lpdev.db import worklog_submission_create

    monkeypatch.setattr("lpdev.web.timesheet_routes.RECONCILE_ON_READ_TIMEOUT_SEC", 0.05)
    conn = env["ctx"].conn
    worklog_submission_create(conn, "SUPERAPP-1", "2026-11-09", 120)
    worklog_submission_create(conn, "SUPERAPP-2", "2026-11-09", 60)
    _age_journal(env, minutes=5)
    env["tracker"].issue_worklogs_delay = 5
    await _get(env)
    await _get(env)
    assert env["tracker"].issue_worklogs_calls == ["SUPERAPP-1", "SUPERAPP-2"]
    rows = worklog_submissions(conn, ["2026-11-09"])
    assert [r["reconcile_attempts"] for r in rows] == [1, 1]


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = _Clock()
    monkeypatch.setattr("lpdev.web.timesheet_routes.time.monotonic", fake.monotonic)
    return fake


async def test_user_lookup_failure_is_reported_and_cached(env, clock):
    fake = env["fake_gitlab"]
    fake.fail.add("user")
    payload = await _get(env)
    assert payload["warnings"] == ["GitLab не отдал текущего пользователя: комментарии, апрувы и мержи в своих MR не учтены"]
    env["app"].state.timesheet.events.clear()
    await _get(env)
    assert fake.calls.count(("user",)) == 1
    clock.now += 61
    fake.fail.clear()
    env["app"].state.timesheet.events.clear()
    payload = await _get(env)
    assert payload["warnings"] == []
    assert fake.calls.count(("user",)) == 2


async def test_failed_project_and_merge_request_lookups_are_cached_for_a_minute(env, clock):
    fake = env["fake_gitlab"]
    fake.fail |= {"project", "mr"}
    env["gitlab"].events = [_ev_push("task/x", "SUPERAPP-1 x"), _ev_note(66, "SUPERAPP-2 y")]
    await _get(env)
    await _get(env)
    assert fake.calls.count(("project", "10")) == 1
    assert fake.calls.count(("mr", 10, 66)) == 1
    clock.now += 61
    await _get(env)
    assert fake.calls.count(("project", "10")) == 2
    assert fake.calls.count(("mr", 10, 66)) == 2


async def test_failed_branch_lookups_are_cached_and_do_not_spend_the_budget(env, clock, monkeypatch):
    monkeypatch.setattr("lpdev.web.timesheet_routes.MAX_GITLAB_LOOKUPS", 3)
    fake = env["fake_gitlab"]
    fake.fail.add("branch")
    env["gitlab"].events = [_ev_push("task/a", "x"), _ev_push("task/b", "x")]
    await _get(env)
    fake.fail.clear()
    env["app"].state.timesheet.events.clear()
    env["gitlab"].events = [_ev_push("task/a", "x"), _ev_push("task/b", "x"), _ev_push("task/c", "x")]
    await _get(env)
    branch_calls = [call for call in fake.calls if call[0] == "branch"]
    assert branch_calls == [("branch", 10, "task/a"), ("branch", 10, "task/b"), ("branch", 10, "task/c")]


async def test_in_progress_issues_are_requested_by_configured_statuses(env):
    await _get(env)
    assert env["tracker"].my_issue_queries == [(("development", "codereview"), ("SUPERAPP",), 5)]
    tracker = TRACKER.model_copy(update={"in_progress_statuses": ["codereview"], "queues": []})
    env["ctx"].config = env["ctx"].config.model_copy(update={"tracker": tracker})
    payload = await _get(env)
    assert env["tracker"].my_issue_queries[-1] == (("codereview",), (), 5)
    assert "in_progress" in _rows(payload)["OTHER-1"]["sources"]
    assert "SUPERAPP-5" not in _rows(payload) or "in_progress" not in _rows(payload)["SUPERAPP-5"]["sources"]


def _history(*changes):
    return [(datetime.fromisoformat(at), src, dst) for at, src, dst in changes]


async def test_rows_show_development_and_review_activity_within_the_week(env):
    env["tracker"].histories["SUPERAPP-1"] = _history(
        ("2026-10-20T10:00:00+03:00", "todo", "development"),
        ("2026-10-21T10:00:00+03:00", "development", "todo"),
        ("2026-11-06T10:00:00+03:00", "todo", "development"),
        ("2026-11-09T12:00:00+03:00", "development", "codereview"),
    )
    payload = await _get(env)
    rows = _rows(payload)
    assert rows["SUPERAPP-1"]["activity"] == [
        {"status": "development", "letter": "d", "from": "2026-11-06", "to": "2026-11-09", "open": False},
        {"status": "codereview", "letter": "r", "from": "2026-11-09", "to": "2026-11-10", "open": True},
    ]
    assert rows["SUPERAPP-5"]["activity"] == []


async def test_rows_carry_the_current_tracker_status(env):
    env["tracker"].issues[0]["status"]["display"] = "Разработка"
    payload = await _get(env)
    rows = _rows(payload)
    assert rows["SUPERAPP-5"]["status"] == {"key": "development", "display": "Разработка"}
    assert all(row["status"] is None or row["status"]["key"] for row in rows.values())


async def test_activity_is_cached_and_failures_are_empty(env, caplog):
    env["tracker"].failing_histories.add("SUPERAPP-5")
    payload = await _get(env)
    assert _rows(payload)["SUPERAPP-5"]["activity"] == []
    assert "status history of SUPERAPP-5 skipped" in caplog.text
    calls = len(env["tracker"].history_calls)
    await _get(env)
    assert len(env["tracker"].history_calls) == calls


async def test_activity_requests_are_limited(env, monkeypatch):
    monkeypatch.setattr("lpdev.web.timesheet_routes.MAX_HISTORY_KEYS", 3)
    monkeypatch.setattr("lpdev.web.timesheet_routes.MAX_HISTORY_PARALLEL", 2)
    for n in range(10):
        await env["client"].post("/api/timesheet/tasks", json={"key": f"SUPERAPP-{100 + n}", "week": "2026-W46"}, headers=H)
    payload = await _get(env)
    assert len(env["tracker"].history_calls) == 3
    assert env["tracker"].history_peak <= 2
    assert all(row["activity"] == [] for row in payload["rows"][3:])


async def test_no_activity_without_tracker(env):
    env["ctx"].config = env["ctx"].config.model_copy(update={"tracker": TrackerConfig(queues=["SUPERAPP"])})
    payload = await _get(env)
    assert payload["rows"]
    assert all(row["activity"] == [] for row in payload["rows"])
    assert env["tracker"].history_calls == []


async def test_activity_disabled_by_empty_status_map(env):
    tracker = TRACKER.model_copy(update={"activity_statuses": {}})
    env["ctx"].config = env["ctx"].config.model_copy(update={"tracker": tracker})
    payload = await _get(env)
    assert all(row["activity"] == [] for row in payload["rows"])
    assert env["tracker"].history_calls == []
