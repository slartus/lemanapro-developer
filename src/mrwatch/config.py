from __future__ import annotations

import logging
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

DEFAULT_PAT_COMMAND = "security find-generic-password -s mr-watch -a gitlab -w"

_PAT_TIMEOUT_SEC = 10
AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
PRIVATE_DIR_MODE = 0o700

logger = logging.getLogger(__name__)


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
    model: str | None = None
    variant: str | None = None

    @field_validator("agent")
    @classmethod
    def valid_agent_name(cls, value: str) -> str:
        if not AGENT_NAME_RE.fullmatch(value):
            raise ValueError("agent name may contain only letters, digits, '_', '.', '-'")
        return value


class ReviewConfig(BaseModel):
    model_config = {"extra": "forbid"}

    max_parallel: int = Field(default=2, ge=1)
    timeout_sec: int = Field(default=1800, ge=1)
    clone_depth: int = Field(default=50, ge=1)
    workspace_ttl_days: int = Field(default=3, ge=1)
    log_ttl_days: int = Field(default=14, ge=1)


class OpenCodeConfig(BaseModel):
    model_config = {"extra": "forbid"}

    bin: str = Field(default="~/.opencode/bin/opencode", validate_default=True)
    model: str | None = None
    variant: str | None = None
    length_retry_variant: str | None = "low"

    @field_validator("bin")
    @classmethod
    def expand_bin(cls, value: str) -> str:
        return str(Path(value).expanduser())


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


def check_config_permissions(path: Path) -> None:
    info = path.stat()
    if info.st_uid != os.getuid():
        raise ConfigError(f"config {path} is owned by another user (uid {info.st_uid}); refusing to start")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ConfigError(
            f"config {path} is group/other-writable (mode {stat.S_IMODE(info.st_mode):o}); "
            f"fix with: chmod 600 {path}"
        )
    if info.st_mode & (stat.S_IRGRP | stat.S_IROTH):
        logger.warning(
            "config %s is readable by others (mode %o); recommended: chmod 600",
            path, stat.S_IMODE(info.st_mode),
        )


def load_config(path: Path) -> Config:
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    check_config_permissions(path)
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
    return config


def resolve_paths(config: Config) -> ResolvedPaths:
    paths = ResolvedPaths(
        db=Path(config.paths.db).expanduser(),
        workspaces=Path(config.paths.workspaces).expanduser(),
        logs=Path(config.paths.logs).expanduser(),
    )
    for directory in (paths.db.parent, paths.workspaces, paths.logs):
        directory.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
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
