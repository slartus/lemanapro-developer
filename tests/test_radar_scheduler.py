from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

from lpdev.config import RadarConfig
from lpdev.db import radar_snapshot_get
from lpdev.main import create_app
from lpdev.radar_scheduler import (
    RUNNER_PATH,
    RadarRunnerError,
    RadarScheduler,
    run_node_runner,
)

API_BASE = "http://127.0.0.1:8123/gl"


def _ok(dashboard=None, active=False):
    return {
        "ok": True,
        "dashboard": dashboard or {"fetchedAt": "2026-11-09T10:00:00.000Z", "sections": {}},
        "threadCache": {"baseUrl": "https://gitlab.example.com", "entries": {}},
        "activePipelines": active,
    }


class FakeRunner:
    def __init__(self, *results):
        self.results = list(results)
        self.payloads: list[dict] = []

    async def __call__(self, payload, radar):
        self.payloads.append(payload)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _scheduler(ctx, runner, clock=None, **radar):
    ctx_obj, _ = ctx
    ctx_obj.config = ctx_obj.config.model_copy(update={"radar": RadarConfig(**radar)})
    return RadarScheduler(
        ctx_obj.conn, lambda: ctx_obj.config, API_BASE, "secret", runner=runner, clock=clock
    )


async def test_refresh_passes_settings_and_previous_snapshot(ctx):
    runner = FakeRunner(_ok({"fetchedAt": "2026-11-09T10:00:00.000Z", "username": "me"}), _ok())
    scheduler = _scheduler(
        ctx, runner, project_filter_mode="selected", selected_project_ids=[5], ignored_check_names=["approves"],
        approval_threshold=2, hidden_sections=["possibleMrs"],
    )
    await scheduler.refresh_once()
    await scheduler.refresh_once()
    first, second = runner.payloads
    assert first == {
        "baseUrl": "https://gitlab.example.com",
        "apiBase": API_BASE,
        "token": "secret",
        "settings": {
            "projectFilterMode": "selected",
            "selectedProjectIds": [5],
            "ignoredCheckNames": ["approves"],
            "approvalThreshold": 2,
            "hiddenSections": ["possibleMrs"],
        },
        "previous": None,
        "threadCache": None,
    }
    assert second["previous"] == {"fetchedAt": "2026-11-09T10:00:00.000Z", "username": "me"}
    assert second["threadCache"] == {"baseUrl": "https://gitlab.example.com", "entries": {}}


async def test_failure_keeps_the_last_dashboard_and_records_the_error(ctx):
    runner = FakeRunner(_ok(), {"ok": False, "status": 401, "error": "Токен невалиден"}, RadarRunnerError("timeout"))
    scheduler = _scheduler(ctx, runner)
    await scheduler.refresh_once()
    first_version = radar_snapshot_get(ctx[0].conn)["version"]
    assert await scheduler.refresh_once() is False
    row = radar_snapshot_get(ctx[0].conn)
    assert row["error"] == "Токен невалиден"
    assert row["failed_at"]
    assert json.loads(row["dashboard_json"])["fetchedAt"] == "2026-11-09T10:00:00.000Z"
    assert row["version"] == first_version + 1
    await scheduler.refresh_once()
    assert radar_snapshot_get(ctx[0].conn)["error"] == "timeout"


async def test_success_clears_the_previous_error(ctx):
    scheduler = _scheduler(ctx, FakeRunner({"ok": False, "error": "boom"}, _ok()))
    await scheduler.refresh_once()
    await scheduler.refresh_once()
    row = radar_snapshot_get(ctx[0].conn)
    assert row["error"] is None and row["failed_at"] is None


async def test_request_refresh_is_deduplicated_and_respects_the_gap(ctx):
    now = [1000.0]
    release = asyncio.Event()
    started = asyncio.Event()

    async def runner(payload, radar):
        started.set()
        await release.wait()
        return _ok()

    scheduler = _scheduler(ctx, runner, clock=lambda: now[0])
    assert scheduler.request_refresh() is True
    await started.wait()
    assert scheduler.request_refresh(force=True) is False
    release.set()
    for _ in range(20):
        await asyncio.sleep(0)
    now[0] += 60
    assert scheduler.request_refresh() is False
    now[0] += 60.1
    assert scheduler.request_refresh() is True


async def test_request_refresh_force_bypasses_the_gap(ctx):
    now = [1000.0]
    scheduler = _scheduler(ctx, FakeRunner(_ok(), _ok()), clock=lambda: now[0])
    assert scheduler.request_refresh() is True
    for _ in range(20):
        await asyncio.sleep(0)
    now[0] += 1
    assert scheduler.request_refresh() is False
    assert scheduler.request_refresh(force=True) is True


async def test_unexpected_runner_error_is_logged_not_raised(ctx):
    calls = []

    async def runner(payload, radar):
        calls.append(payload)
        raise ValueError("boom")

    scheduler = _scheduler(ctx, runner)
    assert scheduler.request_refresh() is True
    for _ in range(20):
        await asyncio.sleep(0)
    assert scheduler._refresh_task is None
    assert scheduler.request_refresh() is False
    assert scheduler.request_refresh(force=True) is True


def _script(tmp_path: Path, body: str) -> RadarConfig:
    script = tmp_path / "fake-node"
    script.write_text(f"#!{sys.executable}\n{body}\n")
    script.chmod(0o755)
    return RadarConfig(node_bin=str(script), timeout_sec=10)


async def test_node_runner_round_trips_json_over_stdio(tmp_path):
    radar = _script(
        tmp_path,
        "import json, sys\n"
        "payload = json.load(sys.stdin)\n"
        "assert sys.argv[1].endswith('runner.mjs')\n"
        "print(json.dumps({'ok': True, 'echo': payload['token']}))",
    )
    assert await run_node_runner({"token": "secret"}, radar) == {"ok": True, "echo": "secret"}


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("import sys\nsys.stderr.write('Bearer abc boom')\nsys.exit(3)", "кодом 3"),
        ("print('not json')", "не JSON"),
        ("print('[1]')", "не объект"),
    ],
)
async def test_node_runner_failures_are_runner_errors(tmp_path, body, message):
    with pytest.raises(RadarRunnerError, match=message) as error:
        await run_node_runner({}, _script(tmp_path, body))
    assert "abc" not in str(error.value)


async def test_node_runner_is_killed_on_timeout(tmp_path):
    radar = _script(tmp_path, "import time\ntime.sleep(30)").model_copy(update={"timeout_sec": 1})
    with pytest.raises(RadarRunnerError, match="не уложился"):
        await asyncio.wait_for(run_node_runner({}, radar), timeout=15)


async def test_node_runner_reports_missing_node():
    with pytest.raises(RadarRunnerError, match="node не найден"):
        await run_node_runner({}, RadarConfig(node_bin="/nonexistent/node"))


async def test_node_runner_survives_an_early_exit_with_a_large_input(tmp_path):
    radar = _script(tmp_path, "import sys\nsys.stderr.write('died secret-xyz')\nsys.exit(1)")
    payload = {"token": "secret-xyz", "previous": {"blob": "x" * (5 * 1024 * 1024)}}
    with pytest.raises(RadarRunnerError, match="кодом 1") as error:
        await run_node_runner(payload, radar)
    assert "secret-xyz" not in str(error.value)


async def test_node_runner_times_out_when_the_process_does_not_read_stdin(tmp_path):
    radar = _script(tmp_path, "import time\ntime.sleep(30)").model_copy(update={"timeout_sec": 1})
    with pytest.raises(RadarRunnerError, match="не уложился"):
        await asyncio.wait_for(run_node_runner({"blob": "x" * (5 * 1024 * 1024)}, radar), timeout=15)


async def test_node_runner_reports_a_binary_that_cannot_start(tmp_path):
    broken = tmp_path / "node"
    broken.write_text("not a program")
    broken.chmod(0o755)
    with pytest.raises(RadarRunnerError, match="не удалось запустить node"):
        await run_node_runner({}, RadarConfig(node_bin=str(broken)))


async def test_result_is_dropped_when_settings_change_during_the_refresh(ctx):
    ctx_obj, _ = ctx

    async def runner(payload, radar):
        ctx_obj.config = ctx_obj.config.model_copy(update={"radar": RadarConfig(approval_threshold=3)})
        return _ok()

    scheduler = _scheduler(ctx, runner)
    assert await scheduler.refresh_once() is False
    assert radar_snapshot_get(ctx_obj.conn) is None


async def test_dashboard_of_the_wrong_shape_is_an_error(ctx):
    scheduler = _scheduler(ctx, FakeRunner({"ok": True, "dashboard": [1, 2]}))
    assert await scheduler.refresh_once() is False
    assert "неверного формата" in radar_snapshot_get(ctx[0].conn)["error"]


async def test_api_radar_survives_a_corrupted_snapshot(ctx):
    ctx_obj, _ = ctx
    ctx_obj.conn.execute(
        "INSERT INTO radar_snapshot (id, version, dashboard_json) VALUES (1, 1, '{broken')"
    )
    ctx_obj.conn.commit()
    app = create_app(ctx_obj)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8123") as client:
        response = await client.get("/api/radar")
    assert response.status_code == 200
    assert response.json()["dashboard"] is None


def test_runner_path_points_to_the_radar_runner():
    assert RUNNER_PATH.is_file()


async def test_api_radar_returns_the_snapshot(ctx):
    ctx_obj, _ = ctx
    scheduler = _scheduler(ctx, FakeRunner(_ok({"fetchedAt": "2026-11-09T10:00:00.000Z", "username": "me"})))
    await scheduler.refresh_once()
    app = create_app(ctx_obj)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8123") as client:
        response = await client.get("/api/radar")
    body = response.json()
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert body["version"] == 1
    assert body["dashboard"]["username"] == "me"
    assert body["error"] is None
    assert body["enabled"] is True


async def test_api_radar_is_empty_before_the_first_refresh(ctx):
    app = create_app(ctx[0])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8123") as client:
        body = (await client.get("/api/radar")).json()
    assert body["version"] == 0 and body["dashboard"] is None


@pytest.mark.skipif(__import__("shutil").which("node") is None, reason="node is not installed")
async def test_node_runner_talks_to_gitlab_only_through_the_proxy(ctx):
    import dataclasses
    import socket

    import uvicorn

    from lpdev.gitlab_client import GitLabClient

    seen: list[httpx.Request] = []

    def gitlab(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path == "/api/graphql":
            return httpx.Response(200, json={"data": None, "errors": [{"message": "disabled"}]})
        if path == "/api/v4/user":
            return httpx.Response(200, json={"id": 1, "username": "me"})
        if path == "/api/v4/merge_requests" and request.url.params.get("scope") == "created_by_me":
            page = request.url.params.get("page")
            mr = {
                "id": 2 if page == "1" else 3, "iid": 2 if page == "1" else 3, "project_id": 10, "title": "Mine",
                "web_url": "https://gitlab.example.com/g/p/-/merge_requests/2", "author": {"id": 1, "username": "me"},
                "references": {"full": "g/p!2"}, "draft": False, "updated_at": "2026-11-09T09:00:00Z",
                "detailed_merge_status": "mergeable", "has_conflicts": False, "user_notes_count": 0,
            }
            return httpx.Response(200, json=[mr], headers={"x-next-page": "2" if page == "1" else ""})
        if path.endswith("/approvals"):
            return httpx.Response(200, json={"approved_by": [], "user_has_approved": False})
        return httpx.Response(200, json=[])

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    ctx_obj, _ = ctx
    api = GitLabClient("https://gitlab.example.com", "real-pat", transport=httpx.MockTransport(gitlab))
    app = create_app(dataclasses.replace(ctx_obj, api=api), port=port)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", log_level="warning"))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    try:
        payload = {
            "baseUrl": "https://gitlab.example.com",
            "apiBase": f"http://127.0.0.1:{port}/gl",
            "token": app.state.proxy_secret,
            "settings": {"hiddenSections": ["possibleMrs", "brokenBranches", "awaitingApproval"]},
        }
        result = await run_node_runner(payload, RadarConfig(timeout_sec=60))
    finally:
        server.should_exit = True
        await serving
    assert result["ok"] is True, result
    assert {item["key"] for item in result["dashboard"]["sections"]["authored"]} == {"10!2", "10!3"}
    assert result["dashboard"]["sections"]["authored"][0]["webUrl"].startswith("https://gitlab.example.com/")
    assert seen and all(request.headers.get("PRIVATE-TOKEN") == "real-pat" for request in seen)
    assert any(request.url.path == "/api/graphql" for request in seen)
    assert not any(app.state.proxy_secret in str(request.headers) for request in seen)
