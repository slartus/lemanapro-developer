from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

DEFAULT_PAT_COMMAND = "security find-generic-password -s mr-watch -a gitlab -w"

_PAT_TIMEOUT_SEC = 10


class ConfigError(Exception):
    pass


class GitlabConfig(BaseModel):
    model_config = {"extra": "forbid"}

    url: str
    poll_interval_sec: int = Field(default=60, ge=1)
    pat_command: str = DEFAULT_PAT_COMMAND

    @field_validator("url")
    @classmethod
    def normalize_url(cls, value: str) -> str:
        url = value.rstrip("/")
        if not url:
            raise ValueError("url must not be empty")
        if not url.startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        return url


class ProjectConfig(BaseModel):
    model_config = {"extra": "forbid"}

    gitlab_project_id: int
    slug: str
    agent: str
    re_review_on_push: bool = False


class ReviewConfig(BaseModel):
    model_config = {"extra": "forbid"}

    max_parallel: int = Field(default=2, ge=1)
    timeout_sec: int = Field(default=1800, ge=1)
    clone_depth: int = Field(default=50, ge=1)
    workspace_ttl_days: int = Field(default=3, ge=1)


class OpenCodeConfig(BaseModel):
    model_config = {"extra": "forbid"}

    bin: str = "~/.opencode/bin/opencode"


class PathsConfig(BaseModel):
    model_config = {"extra": "forbid"}

    db: str = "~/projects/mr-watch/data/mrwatch.db"
    workspaces: str = "~/projects/mr-watch/workspaces"
    logs: str = "~/projects/mr-watch/logs"


class Config(BaseModel):
    model_config = {"extra": "forbid"}

    gitlab: GitlabConfig
    projects: list[ProjectConfig]
    review: ReviewConfig = ReviewConfig()
    opencode: OpenCodeConfig = OpenCodeConfig()
    paths: PathsConfig = PathsConfig()

    @field_validator("projects")
    @classmethod
    def at_least_one_project(cls, value: list[ProjectConfig]) -> list[ProjectConfig]:
        if not value:
            raise ValueError("projects must contain at least one entry")
        return value


@dataclass(frozen=True)
class ResolvedPaths:
    db: Path
    workspaces: Path
    logs: Path


def default_config_path() -> Path:
    env_value = os.environ.get("MRWATCH_CONFIG")
    if env_value:
        return Path(env_value).expanduser()
    return Path("~/.config/mr-watch/config.yaml").expanduser()


def load_config(path: Path) -> Config:
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"config root must be a mapping, got: {type(raw).__name__}")
    try:
        config = Config.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"invalid config {path}: {exc}") from exc
    config.opencode.bin = str(Path(config.opencode.bin).expanduser())
    return config


def resolve_paths(config: Config) -> ResolvedPaths:
    paths = ResolvedPaths(
        db=Path(config.paths.db).expanduser(),
        workspaces=Path(config.paths.workspaces).expanduser(),
        logs=Path(config.paths.logs).expanduser(),
    )
    paths.db.parent.mkdir(parents=True, exist_ok=True)
    paths.workspaces.mkdir(parents=True, exist_ok=True)
    paths.logs.mkdir(parents=True, exist_ok=True)
    return paths


def get_pat(gitlab_cfg: GitlabConfig) -> str:
    try:
        proc = subprocess.run(
            gitlab_cfg.pat_command,
            shell=True,
            capture_output=True,
            timeout=_PAT_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"failed to get GitLab PAT: pat_command timed out after {_PAT_TIMEOUT_SEC}s"
        ) from exc
    except OSError as exc:
        raise RuntimeError(f"failed to get GitLab PAT: {exc}") from exc
    token = proc.stdout.decode().strip()
    if proc.returncode != 0 or not token:
        reason = "empty output" if not token else f"exit code {proc.returncode}"
        raise RuntimeError(f"failed to get GitLab PAT: {reason}")
    return token
