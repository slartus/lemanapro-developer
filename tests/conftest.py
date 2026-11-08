from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mrwatch.config import Config, GitlabConfig, PathsConfig, ProjectConfig, ReviewConfig, resolve_paths
from mrwatch.db import connect, create_review, init_schema, sync_projects, upsert_mr
from mrwatch.gitlab_client import GitLabClient
from mrwatch.main import AppContext

GIT_TEST_ENV = {
    "PATH": "/usr/bin:/bin",
    "HOME": str(Path.home()),
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def run_git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True, env=GIT_TEST_ENV)
    return result.stdout


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    origin = tmp_path / "origin"
    origin.mkdir()
    run_git("init", "-q", "-b", "main", cwd=origin)
    (origin / "app.py").write_text("print('hi')\n")
    run_git("add", ".", cwd=origin)
    run_git("commit", "-qm", "init", cwd=origin)
    run_git("branch", "feature", cwd=origin)
    run_git("checkout", "-q", "feature", cwd=origin)
    (origin / "app.py").write_text("print('hello')\n")
    run_git("add", ".", cwd=origin)
    run_git("commit", "-qm", "feature work", cwd=origin)
    feature_sha = run_git("rev-parse", "feature", cwd=origin).strip()
    run_git("checkout", "-q", "main", cwd=origin)
    run_git("update-ref", "refs/merge-requests/1/head", feature_sha, cwd=origin)
    return origin


@pytest.fixture
def db(tmp_path: Path):
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    conn = connect(tmp_path / "data" / "mrwatch.db")
    conn.execute("PRAGMA foreign_keys=ON")
    init_schema(conn)
    yield conn
    conn.close()


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "test.db")
    init_schema(c)
    yield c
    c.close()


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
