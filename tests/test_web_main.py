from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import yaml

from mrwatch.config import Config, GitlabConfig, PathsConfig, ProjectConfig, ReviewConfig
from mrwatch.config import resolve_paths
from mrwatch.db import connect, create_review, finish_review, init_schema, sync_projects, upsert_mr
from mrwatch.gitlab_client import GitLabClient
from mrwatch.main import AppContext, create_app


@pytest.fixture
def ctx(tmp_path: Path):
    config = Config(
        gitlab=GitlabConfig(url="https://gitlab.example.com", pat_command="printf tok"),
        projects=[ProjectConfig(gitlab_project_id=10, slug="g/p", agent="agent-a")],
        review=ReviewConfig(max_parallel=1),
        paths=PathsConfig(
            db=str(tmp_path / "data" / "mrwatch.db"),
            workspaces=str(tmp_path / "workspaces"),
            logs=str(tmp_path / "logs"),
        ),
    )
    paths = resolve_paths(config)
    conn = connect(paths.db)
    init_schema(conn)
    mapping = sync_projects(conn, [(10, "g/p", "agent-a", False, True)])
    result = upsert_mr(
        conn, project_db_id=mapping[10], iid=1, title="MR 1", author_username="me",
        source_branch="feature", web_url="https://gitlab.example.com/g/p/-/mr/1",
        state="opened", head_sha="a" * 40, updated_at="2026-01-01T12:00:00Z",
    )
    review_id = create_review(conn, result.mr_id, "new", "a" * 40, "agent-a")
    api = GitLabClient(config.gitlab.url, "fake-token")
    yield AppContext(conn=conn, config=config, paths=paths, api=api, project_ids=mapping), review_id
    conn.close()


def _client(ctx_obj) -> httpx.AsyncClient:
    app = create_app(ctx_obj)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8123")


async def test_healthz_ok(ctx):
    ctx_obj, _ = ctx
    async with _client(ctx_obj) as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["last_poll"] is None


async def test_healthz_degraded_on_error(ctx):
    ctx_obj, _ = ctx
    from mrwatch.db import poll_state_set

    poll_state_set(ctx_obj.conn, "poll_last_error", "boom: 1")
    async with _client(ctx_obj) as client:
        response = await client.get("/healthz")
    assert response.status_code == 503
    assert response.json()["status"] == "degraded"


async def test_index_lists_reviews(ctx):
    ctx_obj, review_id = ctx
    async with _client(ctx_obj) as client:
        response = await client.get("/")
    assert response.status_code == 200
    assert "MR 1" in response.text
    assert str(review_id) in response.text


async def test_host_check_blocks_foreign_host(ctx):
    ctx_obj, _ = ctx
    app = create_app(ctx_obj)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://evil.example.com:9999"
    ) as client:
        response = await client.get("/")
    assert response.status_code == 403


async def test_review_detail_page(ctx):
    ctx_obj, review_id = ctx
    finish_review(ctx_obj.conn, review_id, "done", review_md="текст ревью <script>")
    async with _client(ctx_obj) as client:
        response = await client.get(f"/reviews/{review_id}")
    assert response.status_code == 200
    assert "текст ревью" in response.text
    assert "<script>alert" not in response.text


async def test_review_404(ctx):
    ctx_obj, _ = ctx
    async with _client(ctx_obj) as client:
        response = await client.get("/reviews/999")
    assert response.status_code == 404


async def test_retry_requires_header(ctx):
    ctx_obj, review_id = ctx
    finish_review(ctx_obj.conn, review_id, "error", error="x")
    async with _client(ctx_obj) as client:
        forbidden = await client.post(f"/reviews/{review_id}/retry")
        ok = await client.post(
            f"/reviews/{review_id}/retry", headers={"X-Requested-With": "mrwatch"}
        )
    assert forbidden.status_code == 403
    assert ok.status_code == 303
    from mrwatch.db import get_review

    assert get_review(ctx_obj.conn, review_id)["status"] == "queued"


async def test_index_shows_severity_counts(ctx):
    ctx_obj, review_id = ctx
    from mrwatch.db import finish_review

    finish_review(ctx_obj.conn, review_id, "done", review_md="ok", counts=(1, 2, 0, 3))
    async with _client(ctx_obj) as client:
        response = await client.get("/")
    assert ">1</span>" in response.text
    assert ">2</span>" in response.text
    assert ">3</span>" in response.text
    assert "крит" in response.text  # легенда


async def test_force_check_requires_header(ctx, monkeypatch):
    ctx_obj, _ = ctx
    async with _client(ctx_obj) as client:
        response = await client.post("/force-check")
    assert response.status_code == 403


async def test_force_check_reloads_config_and_wakes_poller(ctx, monkeypatch, tmp_path):
    ctx_obj, _ = ctx
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
gitlab:
  url: https://gitlab.example.com
  pat_command: "printf tok"
projects:
  - gitlab_project_id: 10
    slug: g/p
    agent: agent-a
    re_review_on_push: true
"""
    )
    monkeypatch.setenv("MRWATCH_CONFIG", str(config_path))

    class FakePoller:
        def __init__(self):
            self.reloaded = None
            self.woke = False

        def reload(self, config, project_ids):
            self.reloaded = (config, project_ids)

        def wake(self):
            self.woke = True

    app = create_app(ctx_obj)
    fake = FakePoller()
    app.state.poller = fake
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8123"
    ) as client:
        response = await client.post(
            "/force-check", headers={"X-Requested-With": "mrwatch"}
        )
    assert response.status_code == 303
    assert fake.woke is True
    assert fake.reloaded is not None
    assert 10 in fake.reloaded[1]
    assert ctx_obj.config.gitlab.url == "https://gitlab.example.com"
    assert 10 in ctx_obj.project_ids


async def test_force_check_bad_config_400(ctx, monkeypatch, tmp_path):
    ctx_obj, _ = ctx
    bad = tmp_path / "bad.yaml"
    bad.write_text("gitlab: [unclosed\n  :::")
    monkeypatch.setenv("MRWATCH_CONFIG", str(bad))
    async with _client(ctx_obj) as client:
        response = await client.post(
            "/force-check", headers={"X-Requested-With": "mrwatch"}
        )
    assert response.status_code == 400


async def test_settings_page_renders(ctx, tmp_path, monkeypatch):
    ctx_obj, _ = ctx
    monkeypatch.setenv("MRWATCH_CONFIG", str(tmp_path / "cfg.yaml"))
    (tmp_path / "cfg.yaml").write_text(
        "gitlab:\n"
        "  url: https://gitlab.example.com\n"
        "projects:\n"
        "  - gitlab_project_id: 10\n"
        "    slug: g/p\n"
        "    agent: agent-a\n",
        encoding="utf-8",
    )
    async with _client(ctx_obj) as client:
        resp = await client.get("/settings")
    assert resp.status_code == 200
    assert "g/p" in resp.text


async def test_settings_save_requires_header(ctx, tmp_path, monkeypatch):
    ctx_obj, _ = ctx
    monkeypatch.setenv("MRWATCH_CONFIG", str(tmp_path / "cfg.yaml"))
    async with _client(ctx_obj) as client:
        resp = await client.post("/settings", json={})
    assert resp.status_code == 403


async def _write_config(tmp_path):
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        "gitlab:\n"
        "  url: https://gitlab.example.com\n"
        "  poll_interval_sec: 60\n"
        "projects:\n"
        "  - gitlab_project_id: 10\n"
        "    slug: g/p\n"
        "    agent: agent-a\n",
        encoding="utf-8",
    )
    return cfg


async def test_settings_save_updates_config_and_projects(ctx, tmp_path, monkeypatch):
    ctx_obj, _ = ctx
    cfg = await _write_config(tmp_path)
    monkeypatch.setenv("MRWATCH_CONFIG", str(cfg))
    payload = {
        "gitlab": {"poll_interval_sec": 30},
        "review": {"timeout_sec": 900, "max_parallel": 2, "clone_depth": 50,
                   "workspace_ttl_days": 3},
        "projects": [
            {"gitlab_project_id": 10, "slug": "g/p", "agent": "agent-a",
             "re_review_on_push": True},
            {"gitlab_project_id": 11, "slug": "g/q", "agent": "agent-a",
             "re_review_on_push": False},
        ],
    }
    async with _client(ctx_obj) as client:
        resp = await client.post(
            "/settings", json=payload, headers={"X-Requested-With": "mrwatch"}
        )
    assert resp.status_code == 303
    import yaml as _yaml

    saved = _yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert saved["gitlab"]["poll_interval_sec"] == 30
    assert len(saved["projects"]) == 2
    assert saved["projects"][0]["re_review_on_push"] is True
    assert ctx_obj.config.gitlab.poll_interval_sec == 30
    assert 11 in ctx_obj.project_ids
    assert (tmp_path / "cfg.yaml.bak").is_file()


async def test_settings_save_invalid_rejected(ctx, tmp_path, monkeypatch):
    ctx_obj, _ = ctx
    cfg = await _write_config(tmp_path)
    monkeypatch.setenv("MRWATCH_CONFIG", str(cfg))
    payload = {
        "gitlab": {"poll_interval_sec": 0},
        "review": {},
        "projects": [{"gitlab_project_id": 10, "slug": "g/p", "agent": "a",
                      "re_review_on_push": False}],
    }
    async with _client(ctx_obj) as client:
        resp = await client.post(
            "/settings", json=payload, headers={"X-Requested-With": "mrwatch"}
        )
    assert resp.status_code == 400
    saved = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert saved["gitlab"]["poll_interval_sec"] == 60


class _ProjectResolver:
    def __init__(self, by_path):
        self._by_path = by_path

    async def get_project_by_path(self, path):
        if path not in self._by_path:
            import json as _json
            from fastapi.responses import PlainTextResponse
            raise LookupError(path)
        return self._by_path[path]


async def test_settings_save_resolves_slug_from_url(ctx, tmp_path, monkeypatch):
    ctx_obj, _ = ctx
    cfg = await _write_config(tmp_path)
    monkeypatch.setenv("MRWATCH_CONFIG", str(cfg))
    ctx_obj.api = _ProjectResolver({
        "g/q": {"id": 99, "path_with_namespace": "g/q"},
    })
    payload = {
        "gitlab": {"poll_interval_sec": 60},
        "review": {},
        "projects": [
            {"gitlab_project_id": 10, "slug": "g/p", "agent": "agent-a",
             "re_review_on_push": False},
            {"gitlab_project_id": None, "slug": "https://gitlab.example.com/g/q",
             "agent": "agent-a", "re_review_on_push": False},
        ],
    }
    async with _client(ctx_obj) as client:
        resp = await client.post(
            "/settings", json=payload, headers={"X-Requested-With": "mrwatch"}
        )
    assert resp.status_code == 303
    saved = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert saved["projects"][1]["gitlab_project_id"] == 99
    assert saved["projects"][1]["slug"] == "g/q"


async def test_settings_save_resolve_failure_400(ctx, tmp_path, monkeypatch):
    ctx_obj, _ = ctx
    cfg = await _write_config(tmp_path)
    monkeypatch.setenv("MRWATCH_CONFIG", str(cfg))
    ctx_obj.api = _ProjectResolver({})
    payload = {
        "gitlab": {"poll_interval_sec": 60},
        "review": {},
        "projects": [
            {"gitlab_project_id": None, "slug": "g/unknown", "agent": "agent-a",
             "re_review_on_push": False},
        ],
    }
    async with _client(ctx_obj) as client:
        resp = await client.post(
            "/settings", json=payload, headers={"X-Requested-With": "mrwatch"}
        )
    assert resp.status_code == 400
    saved = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert len(saved["projects"]) == 1


def test_normalize_slug_variants():
    from mrwatch.web.routes import _normalize_slug

    url = "https://gitlab.example.com"
    assert _normalize_slug("https://gitlab.example.com/g/p/", url) == "g/p"
    assert _normalize_slug("http://gitlab.example.com/g/p.git", url) == "g/p"
    assert _normalize_slug("gitlab.example.com/g/p", url) == "g/p"
    assert _normalize_slug("https://gitlab.example.com/sub/gitlab-x/p", url) == "sub/gitlab-x/p"
    assert _normalize_slug("https://other.host/g/p", url) == "g/p"
    assert _normalize_slug("g/p", url) == "g/p"
    sub = "https://gitlab.example.com/gitlab"
    assert _normalize_slug("https://gitlab.example.com/gitlab/g/p", sub) == "g/p"


class _TreeApi:
    def __init__(self, tree):
        self._tree = tree

    async def get_project_by_path(self, path):
        if path == "g/p":
            return {"id": 10, "default_branch": "develop"}
        raise LookupError(path)

    async def get_project_tree(self, pid, path, ref):
        return self._tree.get(path, [])


async def test_project_agents_returns_only_global_readonly(ctx, monkeypatch):
    ctx_obj, _ = ctx
    monkeypatch.setattr("mrwatch.web.routes.list_readonly_agents", lambda: ["glob"])
    ctx_obj.api = _TreeApi({".opencode/agents": [{"type": "blob", "name": "evil.md"}]})
    async with _client(ctx_obj) as client:
        resp = await client.get("/project-agents", params={"slug": "g/p"})
    assert resp.status_code == 200
    assert resp.json()["agents"] == ["glob"]


async def test_settings_save_preserves_unknown_sections(ctx, tmp_path, monkeypatch):
    ctx_obj, _ = ctx
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        "gitlab:\n"
        "  url: https://gitlab.example.com\n"
        "  pat_command: printf tok\n"
        "projects:\n"
        "  - gitlab_project_id: 10\n"
        "    slug: g/p\n"
        "    agent: agent-a\n"
        "opencode:\n"
        "  bin: /custom/opencode\n"
        "paths:\n"
        "  db: /custom/db.sqlite\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MRWATCH_CONFIG", str(cfg))
    payload = {
        "gitlab": {"poll_interval_sec": 30},
        "review": {},
        "projects": [{"gitlab_project_id": 10, "slug": "g/p", "agent": "agent-a",
                      "re_review_on_push": False}],
    }
    async with _client(ctx_obj) as client:
        resp = await client.post(
            "/settings", json=payload, headers={"X-Requested-With": "mrwatch"}
        )
    assert resp.status_code == 303
    saved = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert saved["gitlab"]["pat_command"] == "printf tok"
    assert saved["gitlab"]["url"] == "https://gitlab.example.com"
    assert saved["opencode"]["bin"] == "/custom/opencode"
    assert saved["paths"]["db"] == "/custom/db.sqlite"


async def test_settings_bad_body_400(ctx, tmp_path, monkeypatch):
    ctx_obj, _ = ctx
    cfg = await _write_config(tmp_path)
    monkeypatch.setenv("MRWATCH_CONFIG", str(cfg))
    async with _client(ctx_obj) as client:
        resp = await client.post(
            "/settings",
            content=b"not json",
            headers={"X-Requested-With": "mrwatch", "Content-Type": "application/json"},
        )
    assert resp.status_code == 400
    saved = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert saved["gitlab"]["poll_interval_sec"] == 60
