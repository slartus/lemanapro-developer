from __future__ import annotations

import logging
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, ValidationError, ValidationInfo, field_validator, model_validator

DEFAULT_PAT_COMMAND = "security find-generic-password -s mr-watch -a gitlab -w"

_PAT_TIMEOUT_SEC = 10
AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
PRIVATE_DIR_MODE = 0o700

logger = logging.getLogger(__name__)


class ConfigError(Exception):
    pass


def normalize_https_url(value: str) -> str:
    url = value.rstrip("/")
    if not url:
        raise ValueError("url must not be empty")
    if "\\" in url or any(c.isspace() or not c.isprintable() for c in url):
        raise ValueError("url must not contain whitespace, control characters or backslashes")
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        raise ValueError("url has an invalid port") from None
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("url has an invalid port")
    if parts.scheme != "https":
        raise ValueError("url must use https://")
    if not parts.hostname:
        raise ValueError("url must contain a host")
    if "@" in parts.netloc:
        raise ValueError("url must not contain credentials")
    if parts.query or parts.fragment or "?" in url or "#" in url:
        raise ValueError("url must not contain query or fragment")
    return url


class GitlabConfig(BaseModel):
    model_config = {"extra": "forbid"}

    url: str
    # Background polling removed (on-demand only). The field is kept for config
    # compatibility; the value is accepted but ignored. 0 means "off".
    poll_interval_sec: int = Field(default=0, ge=0)
    pat_command: str = DEFAULT_PAT_COMMAND

    @field_validator("url")
    @classmethod
    def normalize_url(cls, value: str) -> str:
        return normalize_https_url(value)


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
    question_timeout_sec: int = Field(default=600, ge=1)
    clone_depth: int = Field(default=50, ge=1)
    workspace_ttl_days: int = Field(default=3, ge=1)
    log_ttl_days: int = Field(default=14, ge=1)
    push_debounce_sec: int = Field(default=120, ge=0)


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


RADAR_SECTION_IDS = (
    "reviewing",
    "assigned",
    "mentioned",
    "awaitingApproval",
    "threads",
    "authored",
    "possibleMrs",
    "brokenBranches",
)
RadarSectionId = Literal[
    "reviewing", "assigned", "mentioned", "awaitingApproval", "threads", "authored", "possibleMrs", "brokenBranches"
]


class RadarConfig(BaseModel):
    model_config = {"extra": "forbid"}

    enabled: bool = True
    # Background refresh removed (on-demand only). The field is kept for config
    # compatibility; the value is accepted but ignored. 0 means "off".
    interval_minutes: int = Field(default=0, ge=0, le=120)
    fast_refresh_while_ci: bool = True
    project_filter_mode: Literal["all", "selected"] = "all"
    selected_project_ids: list[int] = []
    ignored_check_names: list[str] = []
    approval_threshold: int = Field(default=0, ge=0, le=20)
    hidden_sections: list[RadarSectionId] = []
    node_bin: str = "node"
    timeout_sec: int = Field(default=300, ge=10)


TRACKER_QUEUE_RE = re.compile(r"[A-Z][A-Z0-9_]+")
TRACKER_STATUS_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
SHORT_DAY_REDUCTION_MINUTES = 60


class TrackerConfig(BaseModel):
    model_config = {"extra": "forbid"}

    enabled: bool = False
    submit_enabled: bool = False
    url: str = "https://api.tracker.yandex.net/v3"
    org_id: str = ""
    org_header: Literal["X-Org-Id", "X-Cloud-Org-Id"] = "X-Org-Id"
    token_command: str = ""
    queues: list[str] = []
    day_hours: int = Field(default=8, ge=1, le=24)
    step_minutes: int = Field(default=30, ge=5, le=240)
    timezone: str = "Europe/Moscow"
    ignored_push_branches: list[str] = ["develop", "main", "master", "release/*", "rc/*"]
    pr_link_bots: list[str] = ["ya360-pr-check"]
    role: Literal["developer", "qa"] = "developer"
    in_progress_statuses: list[str] = ["development", "codereview", "rework"]
    activity_statuses: dict[str, str] = {"development": "d", "codereview": "r", "rework": "w"}

    @model_validator(mode="before")
    @classmethod
    def role_defaults(cls, value: object) -> object:
        if isinstance(value, dict) and value.get("role") == "qa":
            return {"in_progress_statuses": ["testing"], "activity_statuses": {"testing": "t"}, **value}
        return value

    @field_validator("url")
    @classmethod
    def normalize_url(cls, value: str) -> str:
        return normalize_https_url(value)

    @field_validator("ignored_push_branches")
    @classmethod
    def valid_branch_patterns(cls, value: list[str]) -> list[str]:
        for pattern in value:
            if not pattern.strip() or any(c.isspace() or not c.isprintable() for c in pattern):
                raise ValueError(f"invalid branch pattern: {pattern!r}")
        return list(dict.fromkeys(value))

    @field_validator("in_progress_statuses")
    @classmethod
    def valid_statuses(cls, value: list[str]) -> list[str]:
        for status in value:
            if not TRACKER_STATUS_RE.fullmatch(status):
                raise ValueError(f"invalid status key: {status!r}")
        return list(dict.fromkeys(value))

    @field_validator("activity_statuses")
    @classmethod
    def valid_activity_statuses(cls, value: dict[str, str]) -> dict[str, str]:
        for status, letter in value.items():
            if not TRACKER_STATUS_RE.fullmatch(status) or not re.fullmatch(r"[A-Za-z]{1,2}", letter):
                raise ValueError(f"invalid activity status: {status!r} -> {letter!r}")
        return value

    @field_validator("pr_link_bots")
    @classmethod
    def valid_bot_logins(cls, value: list[str]) -> list[str]:
        for login in value:
            if not login.strip() or not login.isprintable():
                raise ValueError(f"invalid bot login: {login!r}")
        return list(dict.fromkeys(value))

    @field_validator("org_id")
    @classmethod
    def valid_org_id(cls, value: str) -> str:
        if value and not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise ValueError("org_id may contain only letters, digits, '_', '-'")
        return value

    @field_validator("queues")
    @classmethod
    def valid_queues(cls, value: list[str]) -> list[str]:
        for queue in value:
            if not TRACKER_QUEUE_RE.fullmatch(queue):
                raise ValueError(f"invalid queue key: {queue!r}")
        return list(dict.fromkeys(value))

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown timezone: {value!r}") from None
        return value

    @model_validator(mode="after")
    def consistent(self) -> "TrackerConfig":
        if (self.day_hours * 60) % self.step_minutes or SHORT_DAY_REDUCTION_MINUTES % self.step_minutes:
            raise ValueError("step_minutes must divide 60 and day_hours * 60")
        if self.enabled and not (self.org_id and self.token_command):
            raise ValueError("tracker.enabled requires org_id and token_command")
        if self.submit_enabled and not self.enabled:
            raise ValueError("tracker.submit_enabled requires tracker.enabled")
        return self


class PathsConfig(BaseModel):
    model_config = {"extra": "forbid"}

    db: str = "~/projects/lemanapro-developer/data/lpdev.db"
    workspaces: str = "~/projects/lemanapro-developer/workspaces"
    logs: str = "~/projects/lemanapro-developer/logs"


class Config(BaseModel):
    model_config = {"extra": "forbid"}

    gitlab: GitlabConfig
    projects: list[ProjectConfig] = Field(default=[], validate_default=True)
    review: ReviewConfig = ReviewConfig()
    opencode: OpenCodeConfig = OpenCodeConfig()
    radar: RadarConfig = RadarConfig()
    tracker: TrackerConfig = TrackerConfig()
    paths: PathsConfig = PathsConfig()

    @field_validator("projects")
    @classmethod
    def at_least_one_project(cls, value: list[ProjectConfig], info: ValidationInfo) -> list[ProjectConfig]:
        if not value and not (info.context or {}).get("timesheet_only"):
            raise ValueError("projects must contain at least one entry")
        return value


@dataclass(frozen=True)
class ResolvedPaths:
    db: Path
    workspaces: Path
    logs: Path


def default_config_path() -> Path:
    env_value = os.environ.get("LPDEV_CONFIG")
    if env_value:
        return Path(env_value).expanduser()
    return Path("~/.config/lemanapro-developer/config.yaml").expanduser()


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


def load_config(path: Path, timesheet_only: bool = False) -> Config:
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
        config = Config.model_validate(raw, context={"timesheet_only": timesheet_only})
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


def run_secret_command(command: str, label: str) -> str:
    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            timeout=_PAT_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"failed to get {label}: command timed out after {_PAT_TIMEOUT_SEC}s"
        ) from exc
    except OSError as exc:
        raise RuntimeError(f"failed to get {label}: {exc}") from exc
    token = proc.stdout.decode().strip()
    if proc.returncode != 0 or not token:
        reason = "empty output" if not token else f"exit code {proc.returncode}"
        raise RuntimeError(f"failed to get {label}: {reason}")
    return token


def get_pat(gitlab_cfg: GitlabConfig) -> str:
    return run_secret_command(gitlab_cfg.pat_command, "GitLab PAT")
