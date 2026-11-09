from pydantic import ValidationError
import pytest

from lpdev.config import (
    Config,
    ConfigError,
    GitlabConfig,
    default_config_path,
    get_pat,
    load_config,
)
from pathlib import Path

VALID_YAML = """
gitlab:
  url: https://gitlab.example.com/
projects:
  - gitlab_project_id: 42
    slug: group/project
    agent: mr-watch-reviewer
review:
  max_parallel: 3
"""


def test_valid_config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(VALID_YAML, encoding="utf-8")
    cfg = load_config(path)
    assert isinstance(cfg, Config)
    assert cfg.gitlab.url == "https://gitlab.example.com"
    assert cfg.gitlab.poll_interval_sec == 60
    assert cfg.projects[0].gitlab_project_id == 42
    assert cfg.projects[0].agent == "mr-watch-reviewer"
    assert cfg.projects[0].re_review_on_push is False
    assert cfg.review.max_parallel == 3
    assert cfg.review.timeout_sec == 1800
    assert cfg.review.clone_depth == 50
    assert cfg.review.workspace_ttl_days == 3
    assert cfg.opencode.bin == str(Path.home() / ".opencode" / "bin" / "opencode")
    assert cfg.paths.db == "~/projects/lemanapro-developer/data/lpdev.db"


def test_trailing_slash_stripped():
    assert GitlabConfig(url="https://gitlab.example.com/").url == "https://gitlab.example.com"
    assert GitlabConfig(url="https://gitlab.example.com///").url == "https://gitlab.example.com"


def test_trailing_slash_stripped_in_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(VALID_YAML, encoding="utf-8")
    assert load_config(path).gitlab.url == "https://gitlab.example.com"


def test_broken_yaml_raises_config_error(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("gitlab: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_missing_file_raises_config_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "absent.yaml")


def test_empty_projects_raises_config_error(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "gitlab:\n  url: https://gitlab.example.com\nprojects: []\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_config(path)


def test_missing_projects_section_raises_config_error(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "gitlab:\n  url: https://gitlab.example.com\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_config(path)


def test_unknown_key_raises_config_error(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(VALID_YAML + "\npoll_intervall: 5\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_get_pat_success():
    gitlab = GitlabConfig(url="https://gitlab.example.com", pat_command="printf testtoken123")
    assert get_pat(gitlab) == "testtoken123"


def test_get_pat_failure_raises_runtime_error():
    gitlab = GitlabConfig(url="https://gitlab.example.com", pat_command="exit 3")
    with pytest.raises(RuntimeError):
        get_pat(gitlab)


def test_get_pat_empty_output_raises_runtime_error():
    gitlab = GitlabConfig(url="https://gitlab.example.com", pat_command="true")
    with pytest.raises(RuntimeError):
        get_pat(gitlab)


def test_get_pat_error_message_contains_no_token():
    gitlab = GitlabConfig(url="https://gitlab.example.com", pat_command="echo leakedtoken >&2; exit 1")
    with pytest.raises(RuntimeError) as exc_info:
        get_pat(gitlab)
    assert "leakedtoken" not in str(exc_info.value)


def test_default_config_path_env_override(monkeypatch):
    monkeypatch.setenv("LPDEV_CONFIG", "/tmp/custom.yaml")
    assert default_config_path() == Path("/tmp/custom.yaml")
    monkeypatch.delenv("LPDEV_CONFIG")
    assert str(default_config_path()).endswith(".config/lemanapro-developer/config.yaml")


@pytest.mark.parametrize(
    "url",
    [
        "https://user:pass@gitlab.example.com",
        "https://token@gitlab.example.com",
        "https://gitlab.example.com?a=1",
        "https://gitlab.example.com/#frag",
        "https://gitlab.example.com/gitlab?x=y",
        "http://gitlab.example.com",
        "ftp://gitlab.example.com",
        "gitlab.example.com",
        "https://",
        " https://host",
        "https://host\n",
        "https://ho st",
        "https://host:abc",
        "https://host:99999",
        "https://host:0",
        "https://host\\evil",
        "https://host\t",
    ],
)
def test_gitlab_url_rejected(url):
    with pytest.raises(ValidationError):
        GitlabConfig(url=url)


def test_gitlab_url_accepted():
    assert GitlabConfig(url="https://gitlab.example.com").url == "https://gitlab.example.com"
    assert GitlabConfig(url="https://gitlab.example.com:8443/").url == "https://gitlab.example.com:8443"
    assert GitlabConfig(url="https://example.com/gitlab/").url == "https://example.com/gitlab"


def test_push_debounce_default_and_validation():
    raw = {
        "gitlab": {"url": "https://gitlab.example.com"},
        "projects": [{"gitlab_project_id": 1, "slug": "a/b", "agent": "a"}],
    }
    assert Config.model_validate(raw).review.push_debounce_sec == 120
    assert Config.model_validate(raw | {"review": {"push_debounce_sec": 0}}).review.push_debounce_sec == 0
    with pytest.raises(ValidationError):
        Config.model_validate(raw | {"review": {"push_debounce_sec": -1}})
