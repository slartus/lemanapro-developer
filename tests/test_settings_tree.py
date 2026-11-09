from __future__ import annotations

import dataclasses
import json

import httpx
import pytest
import yaml

from lpdev.gitlab_client import GitLabClient
from lpdev.main import create_app

H = {"X-Requested-With": "lpdev"}
BASE_PAYLOAD = {
    "gitlab": {"poll_interval_sec": 60},
    "review": {"timeout_sec": 900, "max_parallel": 1, "clone_depth": 50, "workspace_ttl_days": 3},
    "projects": [{"gitlab_project_id": 10, "slug": "g/p", "agent": "agent-a", "re_review_on_push": False}],
}


def _config(tmp_path):
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        "gitlab:\n  url: https://gitlab.example.com\n  pat_command: printf tok\n"
        "projects:\n  - gitlab_project_id: 10\n    slug: g/p\n    agent: agent-a\n"
        "radar:\n  node_bin: /opt/node\n  timeout_sec: 77\n"
    )
    cfg.chmod(0o600)
    return cfg


def _app(ctx, gitlab=None):
    ctx_obj, _ = ctx
    api = GitLabClient("https://gitlab.example.com", "pat", transport=httpx.MockTransport(gitlab or (lambda r: httpx.Response(404))))
    ctx_obj = dataclasses.replace(ctx_obj, api=api)
    return create_app(ctx_obj), ctx_obj


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8123")


async def test_projects_api_marks_maintainer_projects_like_the_radar(ctx):
    seen = []

    def gitlab(request):
        seen.append(dict(request.url.params))
        if request.url.params["min_access_level"] == "40":
            return httpx.Response(200, json=[{"id": 1}, {"id": 2}])
        return httpx.Response(
            200,
            json=[
                {"id": 1, "path_with_namespace": "g/a", "name": "a"},
                {"id": 2, "path_with_namespace": "g/b", "name": "b", "permissions": {"project_access": None, "group_access": None}},
                {"id": 3, "path_with_namespace": "g/c", "name": "c", "permissions": {"project_access": {"access_level": 40}}},
                {"id": "bad"},
            ],
        )

    app, _ = _app(ctx, gitlab)
    async with _client(app) as client:
        response = await client.get("/api/settings/projects")
    assert response.status_code == 200
    assert response.json() == [
        {"id": 1, "path": "g/a", "name": "a", "maintainer": True},
        {"id": 2, "path": "g/b", "name": "b", "maintainer": True},
        {"id": 3, "path": "g/c", "name": "c", "maintainer": False},
    ]
    assert {params["min_access_level"] for params in seen} == {"20", "40"}
    assert all(params["membership"] == "true" and params["archived"] == "false" for params in seen)


async def test_projects_api_reports_gitlab_failure(ctx):
    app, _ = _app(ctx, lambda r: httpx.Response(401))
    async with _client(app) as client:
        response = await client.get("/api/settings/projects")
    assert response.status_code == 502


async def test_settings_save_writes_radar_section_and_keeps_other_radar_keys(ctx, tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    monkeypatch.setenv("LPDEV_CONFIG", str(cfg))
    app, ctx_obj = _app(ctx)
    radar = {
        "enabled": False,
        "interval_minutes": 7,
        "fast_refresh_while_ci": False,
        "project_filter_mode": "selected",
        "selected_project_ids": [3, 1],
        "ignored_check_names": ["approves"],
        "approval_threshold": 2,
        "hidden_sections": ["threads"],
        "node_bin": "/tmp/evil",
    }
    async with _client(app) as client:
        response = await client.post("/settings", json={**BASE_PAYLOAD, "radar": radar}, headers=H)
    assert response.status_code == 303
    saved = yaml.safe_load(cfg.read_text())["radar"]
    assert saved["interval_minutes"] == 7 and saved["selected_project_ids"] == [3, 1]
    assert saved["hidden_sections"] == ["threads"] and saved["enabled"] is False
    assert saved["node_bin"] == "/opt/node" and saved["timeout_sec"] == 77
    assert ctx_obj.config.radar.approval_threshold == 2


@pytest.mark.parametrize(
    "radar",
    [
        {"interval_minutes": 0},
        {"approval_threshold": 21},
        {"project_filter_mode": "some"},
        {"hidden_sections": ["unknown"]},
        {"selected_project_ids": ["x"]},
        {"project_filter_mode": "selected", "selected_project_ids": []},
    ],
)
async def test_settings_save_rejects_invalid_radar_values(ctx, tmp_path, monkeypatch, radar):
    cfg = _config(tmp_path)
    before = cfg.read_text()
    monkeypatch.setenv("LPDEV_CONFIG", str(cfg))
    app, _ = _app(ctx)
    async with _client(app) as client:
        response = await client.post("/settings", json={**BASE_PAYLOAD, "radar": radar}, headers=H)
    assert response.status_code == 400
    assert cfg.read_text() == before


async def test_settings_page_embeds_tree_data(ctx, tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    monkeypatch.setenv("LPDEV_CONFIG", str(cfg))
    app, _ = _app(ctx)
    async with _client(app) as client:
        page = await client.get("/settings")
        script = await client.get("/static/settings.js")
    raw = page.text.split('<script type="application/json" id="settings-data">', 1)[1].split("</script>", 1)[0]
    data = json.loads(raw)
    assert data["reviewProjects"][0]["gitlab_project_id"] == 10
    assert data["radar"]["project_filter_mode"] == "all"
    assert data["gitlabUrl"] == "https://gitlab.example.com"
    assert '<script type="module" src="/static/settings.js"></script>' in page.text
    assert script.status_code == 200


async def test_settings_save_writes_validated_radar_values(ctx, tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    monkeypatch.setenv("LPDEV_CONFIG", str(cfg))
    app, _ = _app(ctx)
    radar = {"interval_minutes": "9", "selected_project_ids": ["5"], "project_filter_mode": "selected", "fast_refresh_while_ci": "yes"}
    async with _client(app) as client:
        response = await client.post("/settings", json={**BASE_PAYLOAD, "radar": radar}, headers=H)
    assert response.status_code == 303
    saved = yaml.safe_load(cfg.read_text())["radar"]
    assert saved["interval_minutes"] == 9
    assert saved["selected_project_ids"] == [5]
    assert saved["fast_refresh_while_ci"] is True


async def test_settings_save_resolves_a_project_added_by_path(ctx, tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    monkeypatch.setenv("LPDEV_CONFIG", str(cfg))

    def gitlab(request):
        assert request.url.raw_path == b"/api/v4/projects/other%2Fteam%2Fapp"
        return httpx.Response(200, json={"id": 77, "path_with_namespace": "other/team/app"})

    app, ctx_obj = _app(ctx, gitlab)
    projects = BASE_PAYLOAD["projects"] + [
        {"gitlab_project_id": None, "slug": "other/team/app", "agent": "agent-a", "re_review_on_push": False}
    ]
    async with _client(app) as client:
        response = await client.post("/settings", json={**BASE_PAYLOAD, "projects": projects}, headers=H)
    assert response.status_code == 303
    saved = yaml.safe_load(cfg.read_text())["projects"]
    assert saved[-1]["gitlab_project_id"] == 77 and saved[-1]["slug"] == "other/team/app"
    assert 77 in ctx_obj.project_ids
