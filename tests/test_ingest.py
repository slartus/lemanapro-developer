from __future__ import annotations

import asyncio
import dataclasses
import json

import httpx

from helpers import list_reviews
from lpdev.db import claim_next_review, create_review, get_review, radar_snapshot_save_success
from lpdev.gitlab_client import GitLabClient
from lpdev.main import create_app
from lpdev.radar_scheduler import RadarScheduler
from test_poller import FakeRegistry, StubApi, _make_poller, _mr, _seen_foreign_mr

OLD = "2026-01-01T12:00:00Z"
NEW = "2026-01-01T12:05:00Z"


async def _active_review_on_old_head(conn):
    api = StubApi()
    registry = FakeRegistry()
    work_event = asyncio.Event()
    poller, mr_id = await _seen_foreign_mr(conn, api, registry, work_event)
    review_id = create_review(conn, mr_id, "manual", "a" * 40, "agent-a")
    assert claim_next_review(conn)["id"] == review_id
    return api, registry, work_event, poller, review_id


async def test_radar_data_reaches_the_review_flow_first_and_sweep_does_not_duplicate(conn):
    api, registry, work_event, poller, review_id = await _active_review_on_old_head(conn)
    pushed = _mr(10, 1, sha="b" * 40, updated_at=NEW)

    assert poller.ingest([pushed]) == 1
    assert registry.cancelled == [review_id]
    assert get_review(conn, review_id)["status"] == "cancelled"
    assert work_event.is_set()

    api.mrs = [pushed]
    assert await poller.tick() == 0
    fresh = [r for r in list_reviews(conn) if r["head_sha"] == "b" * 40]
    assert [(r["trigger"], r["status"]) for r in fresh] == [("push", "queued")]


async def test_stale_sweep_data_does_not_roll_back_a_newer_head(conn):
    api, registry, _, poller, review_id = await _active_review_on_old_head(conn)
    poller.ingest([_mr(10, 1, sha="b" * 40, updated_at=NEW)])

    api.mrs = [_mr(10, 1, sha="a" * 40, updated_at=OLD)]
    assert await poller.tick() == 0
    assert conn.execute("SELECT head_sha FROM mr WHERE iid = 1").fetchone()["head_sha"] == "b" * 40
    assert [r["head_sha"] for r in list_reviews(conn) if r["status"] == "queued"] == ["b" * 40]


async def test_ingest_skips_data_that_is_not_newer(conn):
    api, registry, _, poller, review_id = await _active_review_on_old_head(conn)
    assert poller.ingest([_mr(10, 1, sha="b" * 40, updated_at=OLD)]) == 0
    assert get_review(conn, review_id)["status"] != "cancelled"
    assert registry.cancelled == []


async def test_ingest_ignores_unknown_projects_unscanned_projects_and_malformed_items(conn):
    api = StubApi()
    poller = _make_poller(conn, api)
    assert poller.ingest([_mr(10, 1, author="me")]) == 0
    await poller.tick()
    before = conn.execute("SELECT COUNT(*) FROM mr").fetchone()[0]
    items = [
        _mr(99, 1, author="me"),
        {"iid": "1", "sha": "a" * 40, "project_id": 10},
        {"iid": 2, "project_id": 10},
        "garbage",
    ]
    assert poller.ingest(items) == 0
    assert conn.execute("SELECT COUNT(*) FROM mr").fetchone()[0] == before


async def test_ingest_adds_a_new_own_mr_like_the_sweep(conn):
    api = StubApi()
    poller = _make_poller(conn, api)
    await poller.tick()
    assert poller.ingest([_mr(10, 7, author="me", updated_at=NEW)]) == 1
    assert [(r["trigger"], r["status"]) for r in list_reviews(conn)] == [("new", "queued")]


async def test_poller_reports_mr_updates_after_the_first_sighting(conn):
    api = StubApi()
    seen: list[tuple[int, int]] = []
    poller = _make_poller(conn, api)
    poller._on_mr_updated = lambda pid, iid: seen.append((pid, iid))
    api.mrs = [_mr(10, 1, updated_at=OLD)]
    await poller.tick()
    await poller.tick()
    await poller.tick()
    assert seen == []
    api.mrs = [_mr(10, 1, updated_at=NEW)]
    await poller.tick()
    await poller.tick()
    assert seen == [(10, 1)]


async def test_mr_updates_seen_through_radar_do_not_wake_radar_again(conn):
    api = StubApi()
    seen: list[tuple[int, int]] = []
    poller = _make_poller(conn, api)
    poller._on_mr_updated = lambda pid, iid: seen.append((pid, iid))
    api.mrs = [_mr(10, 1, updated_at=OLD)]
    await poller.tick()
    await poller.tick()
    poller.ingest([_mr(10, 1, updated_at=NEW)])
    api.mrs = [_mr(10, 1, updated_at=NEW)]
    await poller.tick()
    assert seen == []


def _dashboard(*keys):
    return {"fetchedAt": NEW, "sections": {"authored": [{"key": key, "kind": "mr"} for key in keys]}}


async def test_radar_wakes_only_for_known_mrs_and_at_most_once_a_minute(ctx):
    ctx_obj, _ = ctx
    radar_snapshot_save_success(ctx_obj.conn, NEW, json.dumps(_dashboard("10!1")), "{}")
    now = [1000.0]
    scheduler = RadarScheduler(ctx_obj.conn, lambda: ctx_obj.config, "http://x/gl", "s", clock=lambda: now[0], startup_delay_sec=0)
    scheduler.notify_mr_updated(10, 2)
    assert not scheduler._wake.is_set()
    scheduler.notify_mr_updated(10, 1)
    assert scheduler._wake.is_set()

    scheduler._wake.clear()
    scheduler._last_refresh_started = now[0]
    now[0] += 10
    scheduler.notify_mr_updated(10, 1)
    scheduler.notify_mr_updated(10, 1)
    assert not scheduler._wake.is_set()
    assert scheduler._scheduled_wake is not None
    delay = scheduler._scheduled_wake.when() - asyncio.get_running_loop().time()
    assert 45 < delay <= 50
    scheduler._scheduled_wake.cancel()


async def test_proxy_shares_merge_request_lists_with_the_poller(ctx):
    ctx_obj, _ = ctx
    shared: list[list] = []

    class FakePoller:
        def ingest(self, items):
            shared.append(items)
            return 0

    def gitlab(request):
        if request.url.path.endswith("/merge_requests"):
            return httpx.Response(200, json=[{"iid": 1, "project_id": 10, "sha": "a" * 40}])
        return httpx.Response(200, json={"iid": 1})

    api = GitLabClient("https://gitlab.example.com", "pat", transport=httpx.MockTransport(gitlab))
    app = create_app(dataclasses.replace(ctx_obj, api=api))
    app.state.poller = FakePoller()
    headers = {"PRIVATE-TOKEN": app.state.proxy_secret}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8123") as client:
        await client.get("/gl/api/v4/merge_requests?scope=created_by_me", headers=headers)
        await client.get("/gl/api/v4/projects/10/merge_requests?state=opened", headers=headers)
        await client.get("/gl/api/v4/projects/10/merge_requests/1/approvals", headers=headers)
        await client.get("/gl/api/v4/user", headers=headers)
    assert len(shared) == 2
    assert all(items[0]["iid"] == 1 for items in shared)


async def test_proxy_response_survives_a_failing_poller(ctx):
    ctx_obj, _ = ctx

    class BrokenPoller:
        def ingest(self, items):
            raise RuntimeError("boom")

    api = GitLabClient("https://gitlab.example.com", "pat", transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[])))
    app = create_app(dataclasses.replace(ctx_obj, api=api))
    app.state.poller = BrokenPoller()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8123") as client:
        response = await client.get("/gl/api/v4/merge_requests", headers={"PRIVATE-TOKEN": app.state.proxy_secret})
    assert response.status_code == 200
    assert response.json() == []


H = {"X-Requested-With": "lpdev"}


def _review_app(ctx, gitlab=None):
    from lpdev.db import SCAN_DONE_KEY, poll_state_set
    from lpdev.poller import Poller

    ctx_obj, _ = ctx
    api = GitLabClient(
        "https://gitlab.example.com", "pat", transport=httpx.MockTransport(gitlab or (lambda r: httpx.Response(404)))
    )
    ctx_obj = dataclasses.replace(ctx_obj, api=api)
    poll_state_set(ctx_obj.conn, SCAN_DONE_KEY.format(10), NEW)
    app = create_app(ctx_obj)
    poller = Poller(ctx_obj.conn, api, ctx_obj.config, ctx_obj.project_ids)
    poller._me = {"username": "me"}
    app.state.poller = poller
    return app, ctx_obj


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8123")


async def test_start_review_requires_header_and_a_configured_project(ctx):
    app, _ = _review_app(ctx)
    async with _client(app) as client:
        no_header = await client.post("/api/mr/10/1/review")
        unknown = await client.post("/api/mr/99/1/review", headers=H)
    assert no_header.status_code == 403
    assert unknown.status_code == 404


async def test_start_review_queues_a_manual_review_once(ctx):
    app, ctx_obj = _review_app(ctx)
    ctx_obj.conn.execute("UPDATE review SET status = 'done'")
    ctx_obj.conn.commit()
    async with _client(app) as client:
        first = await client.post("/api/mr/10/1/review", headers=H)
        second = await client.post("/api/mr/10/1/review", headers=H)
    assert first.status_code == 201
    review = get_review(ctx_obj.conn, first.json()["review_id"])
    assert (review["trigger"], review["status"]) == ("manual", "queued")
    assert second.status_code == 409


async def test_start_review_requeues_a_finished_manual_review_of_the_same_head(ctx):
    app, ctx_obj = _review_app(ctx)
    ctx_obj.conn.execute("UPDATE review SET status = 'done', \"trigger\" = 'manual'")
    ctx_obj.conn.commit()
    review_id = ctx_obj.conn.execute("SELECT id FROM review").fetchone()["id"]
    async with _client(app) as client:
        response = await client.post("/api/mr/10/1/review", headers=H)
    assert response.status_code == 201
    assert response.json()["review_id"] == review_id
    assert get_review(ctx_obj.conn, review_id)["status"] == "queued"


async def test_start_review_loads_an_unknown_mr_through_the_poller(ctx):
    def gitlab(request):
        assert request.url.path == "/api/v4/projects/10/merge_requests/42"
        return httpx.Response(200, json=_mr(10, 42, author="other", updated_at=NEW) | {"project_id": 10})

    app, ctx_obj = _review_app(ctx, gitlab)
    async with _client(app) as client:
        response = await client.post("/api/mr/10/42/review", headers=H)
    assert response.status_code == 201
    row = ctx_obj.conn.execute("SELECT head_sha FROM mr WHERE iid = 42").fetchone()
    assert row["head_sha"] == "a" * 40
    assert get_review(ctx_obj.conn, response.json()["review_id"])["trigger"] == "manual"


async def test_start_review_reports_gitlab_errors_and_closed_mrs(ctx):
    app, ctx_obj = _review_app(ctx, lambda r: httpx.Response(500))
    async with _client(app) as client:
        failed = await client.post("/api/mr/10/42/review", headers=H)
        ctx_obj.conn.execute("UPDATE mr SET state = 'merged'")
        ctx_obj.conn.execute("UPDATE review SET status = 'done'")
        ctx_obj.conn.commit()
        closed = await client.post("/api/mr/10/1/review", headers=H)
    assert failed.status_code == 502
    assert closed.status_code == 409


async def test_board_lists_projects_configured_for_review(ctx):
    async with _client(create_app(ctx[0])) as client:
        page = await client.get("/")
    raw = page.text.split('<script type="application/json" id="initial-data">', 1)[1].split("</script>", 1)[0]
    assert json.loads(raw)["reviewProjects"] == [10]


async def test_todo_with_stale_detail_is_kept_for_the_next_tick(conn):
    from lpdev.db import todo_seen_ids

    api, registry, _, poller, _ = await _active_review_on_old_head(conn)
    poller.ingest([_mr(10, 1, sha="b" * 40, updated_at=NEW)])
    api.details[(10, 1)] = _mr(10, 1, sha="a" * 40, updated_at=OLD)
    api.todos = [{"id": 501, "target_type": "MergeRequest", "action_name": "mentioned",
                  "target": {"project_id": 10, "iid": 1}}]
    await poller.tick()
    assert 501 not in todo_seen_ids(conn)
    api.details[(10, 1)] = _mr(10, 1, sha="b" * 40, updated_at=NEW)
    await poller.tick()
    assert 501 in todo_seen_ids(conn)
    roles = conn.execute("SELECT my_roles FROM mr WHERE iid = 1").fetchone()["my_roles"]
    assert "mentioned" in roles


async def test_ingest_does_not_insert_closed_or_merged_mrs(conn):
    api = StubApi()
    poller = _make_poller(conn, api)
    await poller.tick()
    closed = _mr(10, 8, author="me", updated_at=NEW) | {"state": "merged"}
    assert poller.ingest([closed]) == 0
    assert conn.execute("SELECT COUNT(*) FROM mr WHERE iid = 8").fetchone()[0] == 0


async def test_radar_wakes_for_the_first_update_after_a_restart(conn):
    api = StubApi()
    api.mrs = [_mr(10, 1, updated_at=OLD)]
    first = _make_poller(conn, api)
    await first.tick()
    seen: list[tuple[int, int]] = []
    restarted = _make_poller(conn, api)
    restarted._on_mr_updated = lambda pid, iid: seen.append((pid, iid))
    await restarted.tick()
    api.mrs = [_mr(10, 1, updated_at=NEW)]
    await restarted.tick()
    assert seen == [(10, 1)]


async def test_start_review_of_my_new_mr_reports_the_review_ingest_queued(ctx):
    def gitlab(request):
        return httpx.Response(200, json=_mr(10, 43, author="me", updated_at=NEW) | {"project_id": 10})

    app, ctx_obj = _review_app(ctx, gitlab)
    async with _client(app) as client:
        response = await client.post("/api/mr/10/43/review", headers=H)
    assert response.status_code == 201
    review = get_review(ctx_obj.conn, response.json()["review_id"])
    assert (review["trigger"], review["status"]) == ("new", "queued")


async def test_start_review_uses_the_current_head_from_gitlab(ctx):
    def gitlab(request):
        return httpx.Response(200, json=_mr(10, 1, sha="c" * 40, author="other", updated_at=NEW) | {"project_id": 10})

    app, ctx_obj = _review_app(ctx, gitlab)
    ctx_obj.conn.execute("UPDATE review SET status = 'done'")
    ctx_obj.conn.commit()
    async with _client(app) as client:
        response = await client.post("/api/mr/10/1/review", headers=H)
    assert response.status_code == 201
    assert get_review(ctx_obj.conn, response.json()["review_id"])["head_sha"] == "c" * 40
