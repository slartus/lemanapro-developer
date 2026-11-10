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
            {"key": "SUPERAPP-5", "summary": "Doing it", "status": {"key": "inProgress"}},
            {"key": "SUPERAPP-6", "summary": "Backlog", "status": {"key": "open"}},
            {"key": "OTHER-1", "summary": "Other queue", "status": {"key": "inProgress"}},
        ]
        self.titles = {"SUPERAPP-1": "Login screen"}
        self.created: list[tuple[str, datetime, int]] = []
        self.mode = "ok"
        self.fail_reads: TrackerApiError | None = None
        self.fail_issue_worklogs = False
        self.issue_worklogs_error: TrackerApiError | None = None
        self.issue_worklogs_delay = 0.0
        self.issue_worklogs_calls: list[str] = []
        self.closed = False
        self.next_id = 100

    def _read(self) -> None:
        if self.fail_reads is not None:
            raise self.fail_reads

    async def myself(self):
        self._read()
        return {"login": "me", "uid": 5}

    async def search_my_issues(self):
        self._read()
        return self.issues

    async def get_issues(self, keys):
        self._read()
        return {key: {"key": key, "summary": self.titles[key]} for key in keys if key in self.titles}

    async def search_worklogs(self, created_by, start_from, start_to):
        self._read()
        assert created_by == "me"
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
        assert authors is not None and "me" in authors
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


EVENTS = [
    {"action_name": "pushed to", "created_at": "2026-11-09T09:00:00Z", "project_id": 10,
     "push_data": {"ref": "feature/SUPERAPP-1-login", "commit_title": "wip", "commit_count": 1}},
    {"action_name": "pushed to", "created_at": "2026-11-09T10:00:00Z", "project_id": 10,
     "push_data": {"ref": "main", "commit_title": "Bump", "commit_count": 1}},
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
        return EVENTS

    ctx_obj.api.get_my_events = get_my_events
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
        "gitlab": get_my_events, "gitlab_calls": gitlab_calls, "factory_calls": factory_calls,
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
    assert payload["unkeyed"]["items"] == [{"title": "main: Bump", "url": None}]
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
