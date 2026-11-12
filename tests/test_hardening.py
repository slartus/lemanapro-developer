from __future__ import annotations

import asyncio
import re
from pathlib import Path

import httpx
import pytest

from lpdev.db import get_review
from lpdev.git_ops import GitEnv, GitOpsError, clone_for_review
from lpdev.main import create_app
from tests.test_git_ops import NO_ENV
from tests.test_worker import (
    NDJSON_OUTPUT,
    _fake_opencode,
    _queued_review,
    _worker,
)

FAKE_TOKEN = "glpat-FAKEtoken1234567890"
INTERMEDIATE_STATUSES = ("cloning", "reviewing")


def _workspace_dir(tmp_path: Path, review_id: int) -> Path:
    return tmp_path / "workspaces" / "10" / "1" / str(review_id)


async def test_parallel_process_one_calls_process_single_queued_review_once(tmp_path, db, origin):
    review_id = _queued_review(db, origin)
    invocations = tmp_path / "invocations"
    opencode_bin = _fake_opencode(
        tmp_path, f"#!/bin/sh\necho run >> {invocations}\nprintf '%s' '{NDJSON_OUTPUT}'\n"
    )
    worker = _worker(tmp_path, db, origin, opencode_bin)

    results = await asyncio.gather(*(worker.process_one() for _ in range(8)))

    assert results.count(True) == 1
    assert results.count(False) == 7
    assert get_review(db, review_id)["status"] == "done"
    assert invocations.read_text().splitlines() == ["run"]


async def test_exception_in_processing_ends_in_error_and_cleans_workspace(tmp_path, db, origin, monkeypatch):
    review_id = _queued_review(db, origin)
    opencode_bin = _fake_opencode(tmp_path, "#!/bin/sh\ntrue\n")
    worker = _worker(tmp_path, db, origin, opencode_bin)

    async def boom(*args, **kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr("lpdev.worker.diff_for_review", boom)

    assert await worker.process_one() is True

    review = get_review(db, review_id)
    assert review["status"] == "error"
    assert review["status"] not in INTERMEDIATE_STATUSES
    assert review["error"]
    assert not _workspace_dir(tmp_path, review_id).exists()


async def test_exception_in_run_review_ends_in_error(tmp_path, db, origin, monkeypatch):
    review_id = _queued_review(db, origin)
    opencode_bin = _fake_opencode(tmp_path, "#!/bin/sh\ntrue\n")
    worker = _worker(tmp_path, db, origin, opencode_bin)

    async def boom(*args, **kwargs):
        raise ValueError("bad")

    monkeypatch.setattr("lpdev.worker.run_review", boom)

    await worker.process_one()

    assert get_review(db, review_id)["status"] == "error"
    assert not _workspace_dir(tmp_path, review_id).exists()


async def test_timeout_marks_error_with_timeout_text_and_cleans_workspace(tmp_path, db, origin):
    review_id = _queued_review(db, origin)
    opencode_bin = _fake_opencode(tmp_path, "#!/bin/sh\nexec sleep 30\n")
    worker = _worker(tmp_path, db, origin, opencode_bin)
    worker._config.review.timeout_sec = 1

    await worker.process_one()

    review = get_review(db, review_id)
    assert review["status"] == "error"
    assert "timed out after 1s" in review["error"]
    assert not _workspace_dir(tmp_path, review_id).exists()


async def test_workspace_removed_after_opencode_failure(tmp_path, db, origin):
    review_id = _queued_review(db, origin)
    opencode_bin = _fake_opencode(tmp_path, "#!/bin/sh\necho broken >&2\nexit 3\n")
    worker = _worker(tmp_path, db, origin, opencode_bin)

    await worker.process_one()

    assert get_review(db, review_id)["status"] == "error"
    assert not _workspace_dir(tmp_path, review_id).exists()


async def test_clone_leaves_no_remote_and_no_askpass_file(origin, tmp_path):
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    workspace = tmp_path / "ws"
    env = GitEnv(pat_command=f"printf {FAKE_TOKEN}", base_env=NO_ENV.base_env)

    await clone_for_review(
        workspace=workspace,
        repo_url=str(origin),
        mr_iid=1,
        target_branch="main",
        depth=5,
        env=env,
        tmpdir=tmpdir,
        harden=False,
    )

    config_text = (workspace / ".git" / "config").read_text()
    assert "[remote" not in config_text
    assert list(tmpdir.glob("askpass-*")) == []


async def test_clone_failure_error_text_masks_token(tmp_path):
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    env = GitEnv(pat_command=f"printf {FAKE_TOKEN}", base_env=NO_ENV.base_env)

    with pytest.raises(GitOpsError) as excinfo:
        await clone_for_review(
            workspace=tmp_path / "ws",
            repo_url=str(tmp_path / FAKE_TOKEN / "missing"),
            mr_iid=1,
            target_branch=None,
            depth=1,
            env=env,
            tmpdir=tmpdir,
            harden=False,
        )

    message = str(excinfo.value)
    assert FAKE_TOKEN not in message
    assert "FAKEtoken" not in message
    assert "glpat-***" in message


MUTATING_ROUTES = [
    "/force-check",
    "/reviews/1/retry",
    "/reviews/1/cancel",
    "/reviews/1/questions",
    "/settings",
    "/api/mr/1/1/review",
    "/api/mr/1/1/refresh",
    "/api/timesheet/tasks",
    "/api/timesheet/submit",
]


@pytest.mark.parametrize("path", MUTATING_ROUTES)
@pytest.mark.parametrize("headers", [{}, {"X-Requested-With": ""}, {"X-Requested-With": "XMLHttpRequest"}, {"X-Requested-With": "LPDEV"}])
async def test_mutating_routes_reject_missing_or_wrong_csrf_header(ctx, path, headers):
    ctx_obj, _ = ctx
    app = create_app(ctx_obj)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8123"
    ) as client:
        response = await client.post(path, headers=headers, json={})
    assert response.status_code == 403
    assert response.text == "forbidden"


@pytest.mark.parametrize("path", MUTATING_ROUTES)
async def test_mutating_routes_accept_correct_csrf_header(ctx, path, monkeypatch):
    def reject_config(*args, **kwargs):
        raise RuntimeError("config disabled in test")

    monkeypatch.setattr("lpdev.web.routes.load_config", reject_config)
    ctx_obj, _ = ctx
    app = create_app(ctx_obj)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8123"
    ) as client:
        response = await client.post(path, headers={"X-Requested-With": "lpdev"}, json={})
    assert not (response.status_code == 403 and response.text == "forbidden")


def test_all_post_routes_are_covered_by_csrf_parametrization(ctx):
    ctx_obj, _ = ctx
    app = create_app(ctx_obj)
    post_paths = {
        path.replace("{review_id}", "1").replace("{gitlab_project_id}", "1").replace("{iid}", "1")
        for path, methods in app.openapi()["paths"].items()
        if "post" in methods
    }
    assert post_paths == set(MUTATING_ROUTES)


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1:9999",
        "localhost:9999",
        "127.0.0.1",
        "localhost",
        "LOCALHOST:8123",
        "127.0.0.1:8123.evil.com",
        "evil.com:8123",
        "127.0.0.1:8123@evil.com",
        "",
    ],
)
@pytest.mark.parametrize("path", MUTATING_ROUTES)
async def test_host_variants_do_not_bypass_check(ctx, path, host):
    ctx_obj, _ = ctx
    app = create_app(ctx_obj)
    headers = {"Host": host, "X-Requested-With": "lpdev"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8123"
    ) as client:
        response = await client.post(path, headers=headers, json={})
        page = await client.get("/", headers={"Host": host})
    assert response.status_code == 403
    assert page.status_code == 403


async def test_x_forwarded_host_does_not_bypass_host_check(ctx):
    ctx_obj, _ = ctx
    app = create_app(ctx_obj)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8123"
    ) as client:
        response = await client.get(
            "/", headers={"Host": "evil.com", "X-Forwarded-Host": "127.0.0.1:8123"}
        )
    assert response.status_code == 403


async def test_x_forwarded_host_does_not_widen_allowed_hosts(ctx):
    ctx_obj, _ = ctx
    app = create_app(ctx_obj)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8123"
    ) as client:
        response = await client.get("/", headers={"X-Forwarded-Host": "evil.com"})
    assert response.status_code == 200


@pytest.mark.parametrize("path", MUTATING_ROUTES)
@pytest.mark.parametrize(
    "headers",
    [
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
        {"Origin": "https://evil.example.com"},
        {"Origin": "http://127.0.0.1:9999"},
        {"Origin": "null"},
    ],
)
async def test_mutating_routes_reject_cross_origin_requests(ctx, path, headers):
    ctx_obj, _ = ctx
    app = create_app(ctx_obj)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8123"
    ) as client:
        response = await client.post(path, headers={"X-Requested-With": "lpdev", **headers}, json={})
    assert response.status_code == 403
    assert response.text == "forbidden"


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Sec-Fetch-Site": "same-origin", "Origin": "http://127.0.0.1:8123"},
        {"Sec-Fetch-Site": "none"},
        {"Origin": "http://localhost:8123"},
    ],
)
async def test_force_check_accepts_same_origin_requests(ctx, headers):
    ctx_obj, _ = ctx
    app = create_app(ctx_obj)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8123"
    ) as client:
        response = await client.post("/force-check", headers={"X-Requested-With": "lpdev", **headers})
    assert response.status_code != 403


async def test_cross_site_get_of_a_page_is_allowed(ctx):
    ctx_obj, _ = ctx
    app = create_app(ctx_obj)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8123"
    ) as client:
        response = await client.get("/", headers={"Sec-Fetch-Site": "cross-site"})
    assert response.status_code == 200


@pytest.mark.parametrize("method", ["HEAD", "OPTIONS"])
async def test_safe_methods_skip_the_same_origin_check(ctx, method):
    ctx_obj, _ = ctx
    app = create_app(ctx_obj)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8123"
    ) as client:
        response = await client.request(
            method, "/healthz", headers={"Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example.com"}
        )
    assert response.status_code != 403


async def test_cross_site_get_of_settings_is_allowed(ctx, monkeypatch):
    ctx_obj, _ = ctx
    monkeypatch.setattr("lpdev.web.routes.load_config", lambda *a, **k: ctx_obj.config)
    app = create_app(ctx_obj)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8123"
    ) as client:
        response = await client.get("/settings", headers={"Sec-Fetch-Site": "cross-site"})
    assert response.status_code == 200


@pytest.mark.parametrize(
    ("raw", "leaked"),
    [
        ("Authorization: OAuth y0_AgAAAAB-secret_value", "y0_AgAAAAB"),
        ("authorization: oauth abc.def-123", "abc.def-123"),
        ("token=y0_AQAAAAAAbcdEFG-_xyz in body", "AQAAAAAAbcdEFG"),
        ("Bearer glpat-FAKEtoken1234567890", "FAKEtoken"),
    ],
)
def test_mask_secrets_hides_tracker_and_gitlab_tokens(raw, leaked):
    from lpdev.procutil import mask_secrets

    masked = mask_secrets(raw)
    assert leaked not in masked
    assert "***" in masked


def _non_safe_routes(app) -> list[tuple[str, str]]:
    from lpdev.web.gitlab_proxy import PROXY_PREFIX

    routes = []
    for path, operations in app.openapi()["paths"].items():
        if path.startswith(PROXY_PREFIX + "/"):
            continue
        concrete = re.sub(r"\{[^}]+\}", "1", path)
        for method in operations:
            if method.upper() not in {"GET", "HEAD", "OPTIONS"}:
                routes.append((method.upper(), concrete))
    return sorted(routes)


async def test_every_non_safe_route_requires_requested_with_header(ctx):
    ctx_obj, _ = ctx
    app = create_app(ctx_obj)
    routes = _non_safe_routes(app)
    assert routes
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8123"
    ) as client:
        for method, path in routes:
            response = await client.request(method, path, json={})
            assert (method, path, response.status_code) == (method, path, 403)
            assert response.text == "forbidden"


async def test_requested_with_middleware_rejects_unknown_paths_too(ctx):
    ctx_obj, _ = ctx
    app = create_app(ctx_obj)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8123"
    ) as client:
        response = await client.request("PATCH", "/api/anything", json={})
    assert response.status_code == 403


async def test_every_non_safe_route_rejects_foreign_origin_even_with_header(ctx):
    ctx_obj, _ = ctx
    app = create_app(ctx_obj)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8123"
    ) as client:
        for method, path in _non_safe_routes(app):
            response = await client.request(
                method, path, json={}, headers={"X-Requested-With": "lpdev", "Origin": "https://evil.example.com"}
            )
            assert (method, path, response.status_code) == (method, path, 403)


async def test_proxy_graphql_with_secret_does_not_need_requested_with(ctx):
    ctx_obj, _ = ctx
    sent = []

    def upstream(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"data": {}})

    from lpdev.gitlab_client import GitLabClient

    ctx_obj.api = GitLabClient("https://gitlab.example.com", "real-token", transport=httpx.MockTransport(upstream))
    app = create_app(ctx_obj)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8123"
    ) as client:
        response = await client.post(
            "/gl/api/graphql",
            headers={"Authorization": f"Bearer {app.state.proxy_secret}"},
            json={"query": "{ currentUser { id } }"},
        )
    assert response.status_code == 200
    assert len(sent) == 1
