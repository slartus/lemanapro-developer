from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from mrwatch.config import Config, GitlabConfig, ProjectConfig, ReviewConfig
from mrwatch.db import (
    claim_next_review,
    connect,
    get_review,
    init_schema,
    list_reviews,
    poll_state_get,
    poll_state_set,
    sync_projects,
)
from mrwatch.gitlab_client import GitlabApiError
from mrwatch.db import SCAN_DONE_KEY, list_mrs
from mrwatch.db import MR_CURSOR_KEY, todo_seen_ids
from mrwatch.poller import Poller

CURSOR_KEY = MR_CURSOR_KEY.format(10)

USER = {"id": 1, "username": "me"}


class StubApi:
    def __init__(self) -> None:
        self.user = dict(USER)
        self.todos: list[dict] = []
        self.mrs: list[dict] = []
        self.details: dict[tuple[int, int], dict] = {}
        self.notes: dict[tuple[int, int], list[dict]] = {}
        self.project_calls: list[tuple] = []
        self.fail_todos = False
        self.fail_notes = False

    async def get_user(self):
        return dict(self.user)

    async def get_todos(self):
        if self.fail_todos:
            raise GitlabApiError("todos down", status=500)
        return list(self.todos)

    async def get_project_merge_requests(self, project_id: int, updated_after=None, state="opened"):
        self.project_calls.append((project_id, updated_after, state))
        return [m for m in self.mrs if m.get("project_id") == project_id]

    async def get_mr_notes(self, project_id: int, iid: int):
        if self.fail_notes:
            raise GitlabApiError("notes down", status=500)
        return list(self.notes.get((project_id, iid), []))

    async def get_project_mr(self, project_id: int, iid: int):
        return dict(self.details[(project_id, iid)])


def _detail(gitlab_pid, iid, sha="a" * 40, author="other", reviewers=(), state="opened"):
    return {
        "iid": iid,
        "title": f"MR {iid}",
        "author": {"username": author, "name": f"Name {author}"},
        "source_branch": "feature",
        "web_url": f"https://gitlab.example.com/g/p/-/merge_requests/{iid}",
        "state": state,
        "sha": sha,
        "updated_at": "2026-01-01T12:00:00Z",
        "reviewers": [{"username": r} for r in reviewers],
    }


def _mr(gitlab_pid, iid, sha="a" * 40, author="other", reviewers=(), updated_at="2026-01-01T12:00:00Z", description=None):
    return _detail(gitlab_pid, iid, sha=sha, author=author, reviewers=reviewers) | {
        "project_id": gitlab_pid,
        "updated_at": updated_at,
        "description": description,
    }


def _config(re_review_on_push=False):
    return Config(
        gitlab=GitlabConfig(url="https://gitlab.example.com/", pat_command="printf x"),
        projects=[
            ProjectConfig(
                gitlab_project_id=10,
                slug="g/p",
                agent="agent-a",
                re_review_on_push=re_review_on_push,
            )
        ],
        review=ReviewConfig(),
    )


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "test.db")
    init_schema(c)
    yield c
    c.close()


def _make_poller(conn, api, config=None, work_event=None):
    config = config or _config()
    re_review = config.projects[0].re_review_on_push
    mapping = sync_projects(conn, [(10, "g/p", "agent-a", re_review, True)])
    return Poller(conn, api, config, mapping, work_event=work_event)


async def test_todo_triggers_review_and_is_remembered(conn):
    api = StubApi()
    api.details[(10, 5)] = _detail(10, 5, reviewers=["me"])
    api.todos = [{"id": 77, "target_type": "MergeRequest", "action_name": "mentioned",
                  "target": {"project_id": 10, "iid": 5}}]
    poller = _make_poller(conn, api)

    created = await poller.tick()

    assert created == 1
    assert todo_seen_ids(conn) == {77}
    reviews = list_reviews(conn)
    assert len(reviews) == 1
    assert reviews[0]["trigger"] == "new"
    assert reviews[0]["agent"] == "agent-a"


async def test_todo_foreign_project_ignored(conn):
    api = StubApi()
    api.details[(99, 5)] = _detail(99, 5)
    api.todos = [{"id": 78, "target_type": "MergeRequest", "action_name": "mentioned",
                  "target": {"project_id": 99, "iid": 5}}]
    poller = _make_poller(conn, api)

    created = await poller.tick()

    assert created == 0
    assert todo_seen_ids(conn) == set()
    assert list_reviews(conn) == []


async def test_full_scan_my_mr_triggers_review(conn):
    api = StubApi()
    api.mrs = [_mr(10, 1, author="me")]
    poller = _make_poller(conn, api)

    created = await poller.tick()

    assert created == 1
    reviews = list_reviews(conn)
    assert reviews[0]["trigger"] == "new"


async def test_full_scan_foreign_mr_no_review(conn):
    api = StubApi()
    api.mrs = [_mr(10, 1, author="other", reviewers=[])]
    poller = _make_poller(conn, api)

    created = await poller.tick()

    assert created == 0
    assert list_reviews(conn) == []


async def test_full_scan_reviewer_mr_triggers_review(conn):
    api = StubApi()
    api.mrs = [_mr(10, 1, author="other", reviewers=["me"])]
    poller = _make_poller(conn, api)

    assert await poller.tick() == 1


async def test_full_scan_mention_in_notes_triggers_review(conn):
    api = StubApi()
    api.mrs = [_mr(10, 2, author="other", reviewers=[])]
    api.notes[(10, 2)] = [{"body": "посмотри пожалуйста @me"}]
    poller = _make_poller(conn, api)

    assert await poller.tick() == 1
    assert list_reviews(conn)[0]["trigger"] == "new"


async def test_full_scan_mention_in_description_triggers_review(conn):
    api = StubApi()
    api.mrs = [_mr(10, 3, author="other", reviewers=[], description="cc @me")]
    poller = _make_poller(conn, api)

    assert await poller.tick() == 1


async def test_full_scan_mention_substring_not_matched(conn):
    api = StubApi()
    api.mrs = [_mr(10, 4, author="other", reviewers=[])]
    api.notes[(10, 4)] = [{"body": "email me@example.com"}]
    poller = _make_poller(conn, api)

    assert await poller.tick() == 0


async def test_full_scan_notes_failure_not_fatal(conn):
    api = StubApi()
    api.fail_notes = True
    api.mrs = [_mr(10, 5, author="other", reviewers=[])]
    poller = _make_poller(conn, api)

    assert await poller.tick() == 0
    assert poll_state_get(conn, CURSOR_KEY) is not None


async def test_push_re_review_flag(conn):
    api = StubApi()
    api.mrs = [_mr(10, 1, author="me")]
    poller = _make_poller(conn, api, _config(re_review_on_push=False))
    await poller.tick()
    api.mrs = [_mr(10, 1, sha="b" * 40, author="me")]
    assert await poller.tick() == 0

    api2 = StubApi()
    api2.mrs = [_mr(10, 1, author="me")]
    poller2 = _make_poller(conn, api2, _config(re_review_on_push=True))
    await poller2.tick()
    api2.mrs = [_mr(10, 1, sha="b" * 40, author="me")]
    assert await poller2.tick() == 1
    reviews = list_reviews(conn)
    assert reviews[0]["trigger"] == "push"


async def test_cursor_written_with_overlap(conn):
    api = StubApi()
    api.mrs = [_mr(10, 1, updated_at="2026-01-01T12:00:00Z")]
    poller = _make_poller(conn, api)

    await poller.tick()

    cursor = poll_state_get(conn, CURSOR_KEY)
    assert cursor is not None
    expected = datetime.fromisoformat("2026-01-01T12:00:00+00:00") - timedelta(seconds=120)
    assert datetime.fromisoformat(cursor) == expected


async def test_cursor_kept_when_nothing_new(conn):
    api = StubApi()
    poller = _make_poller(conn, api)
    poll_state_set(conn, SCAN_DONE_KEY.format(10), "2026-01-01T00:00:00+00:00")
    poll_state_set(conn, CURSOR_KEY, "2026-01-01T00:00:00+00:00")

    await poller.tick()

    assert poll_state_get(conn, CURSOR_KEY) == "2026-01-01T00:00:00+00:00"


async def test_full_scan_cursor_used_by_sweep(conn):
    api = StubApi()
    api.mrs = [_mr(10, 1, author="other", updated_at="2026-01-01T12:00:00Z")]
    poller = _make_poller(conn, api)
    seen = {}
    original = api.get_project_merge_requests

    async def spy(project_id, updated_after=None, state="opened"):
        if updated_after is not None:
            seen["cursor"] = updated_after
            seen["state"] = state
        return await original(project_id, updated_after, state)

    api.get_project_merge_requests = spy
    await poller.tick()
    await poller.tick()

    assert seen["cursor"] == "2026-01-01T11:58:00+00:00"


async def test_failed_todo_tick_creates_no_reviews(conn):
    api = StubApi()
    api.fail_todos = True
    poller = _make_poller(conn, api)

    with pytest.raises(GitlabApiError):
        await poller.tick()

    assert claim_next_review(conn) is None
    assert list_reviews(conn) == []


async def test_run_loop_records_error_and_clears_on_success(conn):
    api = StubApi()
    api.fail_todos = True
    config = _config()
    config = config.model_copy(
        update={"gitlab": config.gitlab.model_copy(update={"poll_interval_sec": 1})}
    )
    mapping = sync_projects(conn, [(10, "g/p", "agent-a", False, True)])
    poller = Poller(conn, api, config, mapping)

    task = asyncio.create_task(poller.run())
    await asyncio.sleep(0.15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert poll_state_get(conn, "poll_last_error") is not None
    assert poll_state_get(conn, "poll_last_ok") is None

    api.fail_todos = False
    task = asyncio.create_task(poller.run())
    await asyncio.sleep(0.15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert poll_state_get(conn, "poll_last_error") is None
    assert poll_state_get(conn, "poll_last_ok") is not None


async def test_work_event_set_on_new_reviews(conn):
    api = StubApi()
    api.mrs = [_mr(10, 1, author="me")]
    work_event = asyncio.Event()
    poller = _make_poller(conn, api, work_event=work_event)

    await poller.tick()

    assert work_event.is_set()
    assert get_review(conn, 1)["status"] == "queued"


async def test_full_scan_new_project_picked_up_later(conn):
    api = StubApi()
    api.mrs = [_mr(10, 1, author="me")]
    poller = _make_poller(conn, api)

    assert await poller.tick() == 1
    calls = {"n": 0}
    original = api.get_project_merge_requests

    async def counting(project_id, updated_after=None, state="opened"):
        if updated_after is None:
            calls["n"] += 1
        return await original(project_id, updated_after, state)

    api.get_project_merge_requests = counting
    await poller.tick()
    scanned_first_round = calls["n"]
    assert scanned_first_round == 0

    api.mrs = [_mr(10, 1, author="me"), _mr(20, 9, author="me")]
    config = _config()
    config.projects.append(
        ProjectConfig(gitlab_project_id=20, slug="g/p2", agent="agent-a")
    )
    mapping = sync_projects(conn, [(10, "g/p", "agent-a", True, True), (20, "g/p2", "agent-a", True, True)])
    poller2 = Poller(conn, api, config, mapping)

    created = await poller2.tick()
    assert created == 1
    reviews = list_reviews(conn)
    assert any(r["iid"] == 9 for r in reviews)


async def test_sweep_picks_up_merged_state(conn):
    api = StubApi()
    api.mrs = [_mr(10, 1, author="me", updated_at="2026-01-01T12:00:00Z")]
    poller = _make_poller(conn, api)
    await poller.tick()

    api.mrs = [dict(_mr(10, 1, author="me", updated_at="2026-01-02T12:00:00Z"), state="merged")]
    poll_state_set(conn, CURSOR_KEY, "2026-01-01T11:58:00+00:00")
    created = await poller.tick()
    assert created == 0

    row = conn.execute("SELECT state FROM mr WHERE iid = 1").fetchone()
    assert row["state"] == "merged"


async def test_list_mrs_hides_merged(conn):
    api = StubApi()
    api.mrs = [_mr(10, 1, author="me", updated_at="2026-01-01T12:00:00Z")]
    poller = _make_poller(conn, api)
    await poller.tick()

    api.mrs = [dict(_mr(10, 1, author="me", updated_at="2026-01-02T12:00:00Z"), state="merged")]
    poll_state_set(conn, CURSOR_KEY, "2026-01-01T11:58:00+00:00")
    await poller.tick()

    assert list_mrs(conn) == []


async def test_sweep_fresh_merged_mr_no_review(conn):
    api = StubApi()
    poller = _make_poller(conn, api)
    poll_state_set(conn, SCAN_DONE_KEY.format(10), "2026-01-01T00:00:00+00:00")
    poll_state_set(conn, CURSOR_KEY, "2026-01-01T11:58:00+00:00")
    api.mrs = [
        dict(_mr(10, 1, author="me", updated_at="2026-01-02T12:00:00Z"), state="merged")
    ]

    created = await poller.tick()

    assert created == 0
    assert list_reviews(conn) == []


async def test_readded_project_scan_marker_invalidated(conn):
    api = StubApi()
    api.mrs = [_mr(10, 1, author="other", reviewers=[])]
    poller = _make_poller(conn, api)
    await poller.tick()
    assert poll_state_get(conn, SCAN_DONE_KEY.format(10)) is not None

    sync_projects(conn, [])
    sync_projects(conn, [(10, "g/p", "agent-a", True, True)])

    assert poll_state_get(conn, SCAN_DONE_KEY.format(10)) is None


def _roles(conn, iid):
    row = conn.execute("SELECT my_roles FROM mr WHERE iid = ?", (iid,)).fetchone()
    return row["my_roles"]


@pytest.mark.parametrize(
    "action,reviewers,expected",
    [
        ("mentioned", (), "mentioned"),
        ("directly_addressed", (), "mentioned"),
        ("review_requested", ("me",), "reviewer"),
        ("review_requested", (), ""),
    ],
)
async def test_roles_from_todo_action(conn, action, reviewers, expected):
    api = StubApi()
    api.details[(10, 5)] = _detail(10, 5, reviewers=reviewers)
    api.todos = [{"id": 1, "target_type": "MergeRequest", "action_name": action,
                  "target": {"project_id": 10, "iid": 5}}]
    await _make_poller(conn, api).tick()
    assert _roles(conn, 5) == expected


async def test_roles_from_mr_data_and_removal(conn):
    api = StubApi()
    mr = _mr(10, 1, author="me", reviewers=["me"])
    mr["assignees"] = [{"username": "me"}]
    api.mrs = [mr]
    poller = _make_poller(conn, api)
    await poller.tick()
    assert _roles(conn, 1) == "assignee,author,reviewer"

    changed = _mr(10, 1, author="other", reviewers=[], updated_at="2026-01-02T12:00:00Z")
    changed["assignees"] = []
    api.mrs = [changed]
    await poller.tick()
    assert _roles(conn, 1) == ""


async def test_mention_role_kept_while_open_and_union(conn):
    api = StubApi()
    api.details[(10, 5)] = _detail(10, 5)
    api.todos = [{"id": 1, "target_type": "MergeRequest", "action_name": "mentioned",
                  "target": {"project_id": 10, "iid": 5}}]
    poller = _make_poller(conn, api)
    await poller.tick()
    api.details[(10, 5)] = _detail(10, 5, reviewers=["me"])
    api.todos = [{"id": 2, "target_type": "MergeRequest", "action_name": "review_requested",
                  "target": {"project_id": 10, "iid": 5}}]
    await poller.tick()
    assert _roles(conn, 5) == "mentioned,reviewer"


async def test_assigned_todo_role_from_detail(conn):
    api = StubApi()
    detail = _detail(10, 5)
    detail["assignees"] = [{"username": "me"}]
    api.details[(10, 5)] = detail
    api.todos = [{"id": 1, "target_type": "MergeRequest", "action_name": "assigned",
                  "target": {"project_id": 10, "iid": 5}}]
    await _make_poller(conn, api).tick()
    assert _roles(conn, 5) == "assignee"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("cc @me", True),
        ("@me, please look", True),
        ("@me2 look", False),
        ("x@me.ru", False),
        ("@me.ru", False),
        ("@me-bot", False),
        (None, False),
    ],
)
def test_has_mention(text, expected):
    from mrwatch.poller import _has_mention

    assert _has_mention(text, "me") is expected


async def test_missing_keys_keep_roles(conn):
    api = StubApi()
    mr = _mr(10, 1, author="other", reviewers=["me"])
    mr["assignees"] = [{"username": "me"}]
    api.mrs = [mr]
    poller = _make_poller(conn, api)
    await poller.tick()
    assert _roles(conn, 1) == "assignee,reviewer"

    partial = _mr(10, 1, author="other", updated_at="2026-01-02T12:00:00Z")
    del partial["reviewers"]
    api.mrs = [partial]
    await poller.tick()
    assert _roles(conn, 1) == "assignee,reviewer"

    empty = _mr(10, 1, author="other", reviewers=[], updated_at="2026-01-03T12:00:00Z")
    api.mrs = [empty]
    await poller.tick()
    assert _roles(conn, 1) == "assignee"


async def test_mine_mention_uses_description_without_notes(conn):
    api = StubApi()
    api.mrs = [_mr(10, 1, author="me", description="cc @me")]
    api.notes[(10, 1)] = []
    await _make_poller(conn, api).tick()
    assert "mentioned" in _roles(conn, 1).split(",")


async def test_roles_backfill_once_without_reviews(conn):
    api = StubApi()
    mr = _mr(10, 1, author="other", reviewers=["me"])
    api.mrs = [mr]
    poller = _make_poller(conn, api)
    await poller.tick()
    conn.execute("UPDATE mr SET my_roles = ''")
    conn.execute("DELETE FROM review")
    conn.execute("DELETE FROM poll_state WHERE key LIKE 'roles_backfill_v5:%'")
    conn.commit()
    api.mrs = [_mr(10, 1, author="other", reviewers=["me"], updated_at="2026-01-01T00:00:00Z")]
    api.project_calls.clear()

    await poller.tick()
    assert _roles(conn, 1) == "reviewer"
    assert list_reviews(conn) == []
    assert len([c for c in api.project_calls if c[2] == "opened" and c[1] is None]) == 1

    api.project_calls.clear()
    await poller.tick()
    assert all(c[1] is not None for c in api.project_calls)
