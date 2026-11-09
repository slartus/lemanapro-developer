from __future__ import annotations

import asyncio
import stat
import time
import subprocess
from pathlib import Path

import pytest

import mrwatch.worker as worker_module
from mrwatch.config import Config, GitlabConfig, PathsConfig, ProjectConfig, ReviewConfig
from mrwatch.config import resolve_paths
from mrwatch.db import (
    create_review,
    get_review,
    supersede_reviews,
    sync_projects,
    upsert_mr,
)
from mrwatch.worker import ReviewRegistry, Worker

NDJSON_OUTPUT = (
    '{"type":"session.started","sessionID":"sess-w1"}\n'
    '{"type":"message.part","part":{"type":"text","text":"ОК"}}\n'
)


def _git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home()),
             "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    )
    return result.stdout


READONLY_AGENT = (
    "---\ndescription: r\ntools:\n  edit: false\n  bash: false\n  webfetch: false\n---\nbody\n"
)


def _fake_opencode(tmp_path: Path, script: str) -> str:
    bin_path = tmp_path / "fake-opencode"
    bin_path.write_text(script)
    bin_path.chmod(bin_path.stat().st_mode | stat.S_IEXEC)
    return str(bin_path)


class StubApi:
    def __init__(self, detail: dict) -> None:
        self.detail = detail

    async def get_project_mr(self, project_id: int, iid: int) -> dict:
        return dict(self.detail)


def _config(tmp_path: Path, opencode_bin: str) -> Config:
    config = Config(
        gitlab=GitlabConfig(url="https://gitlab.example.com", pat_command="printf tok"),
        projects=[ProjectConfig(gitlab_project_id=10, slug="g/p", agent="agent-a")],
        review=ReviewConfig(clone_depth=5),
        paths=PathsConfig(
            db=str(tmp_path / "data" / "mrwatch.db"),
            workspaces=str(tmp_path / "workspaces"),
            logs=str(tmp_path / "logs"),
        ),
    )
    config.opencode.bin = opencode_bin
    return config


def _detail(base_sha: str, head_sha: str) -> dict:
    return {
        "iid": 1,
        "title": "MR 1",
        "author": {"username": "other"},
        "source_branch": "feature",
        "target_branch": "main",
        "web_url": "https://gitlab.example.com/g/p/-/merge_requests/1",
        "state": "opened",
        "sha": head_sha,
        "updated_at": "2026-01-01T12:00:00Z",
        "reviewers": [],
        "description": "описание",
        "diff_refs": {"base_sha": base_sha, "head_sha": head_sha},
    }


def _queued_review(conn, origin: Path) -> int:
    sync_projects(conn, [(10, "g/p", "agent-a", False, True)])
    head_sha = _git("rev-parse", "merge-requests/1/head", cwd=origin).strip()
    result = upsert_mr(
        conn, project_db_id=1, iid=1, title="MR 1", author_username="other",
        source_branch="feature",
        web_url="https://gitlab.example.com/g/p/-/merge_requests/1",
        state="opened", head_sha=head_sha, updated_at="2026-01-01T12:00:00Z",
    )
    review_id = create_review(conn, result.mr_id, "new", head_sha, "agent-a")
    assert review_id is not None
    return review_id


def _worker(
    tmp_path: Path,
    db_conn,
    origin: Path,
    opencode_bin: str,
    registry: ReviewRegistry | None = None,
) -> Worker:
    config = _config(tmp_path, opencode_bin)
    paths = resolve_paths(config)
    (paths.workspaces.parent / "tmp").mkdir(parents=True, exist_ok=True)
    base_sha = _git("rev-parse", "main", cwd=origin).strip()
    head_sha = _git("rev-parse", "merge-requests/1/head", cwd=origin).strip()
    api = StubApi(_detail(base_sha, head_sha))

    class LocalWorker(Worker):
        def _repo_url(self, mr) -> str:
            return str(origin)

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(exist_ok=True)
    (agents_dir / "agent-a.md").write_text(READONLY_AGENT)
    return LocalWorker(
        0, db_conn, api, config, paths, clone_harden=False, agents_dirs=(agents_dir,),
        registry=registry,
    )


async def test_worker_full_success(tmp_path, db, origin):
    review_id = _queued_review(db, origin)
    opencode_bin = _fake_opencode(
        tmp_path, f"#!/bin/sh\nprintf '%s' '{NDJSON_OUTPUT}'\n"
    )
    worker = _worker(tmp_path, db, origin, opencode_bin)

    assert await worker.process_one() is True

    review = get_review(db, review_id)
    assert review["status"] == "done"
    assert review["review_md"] == "ОК"
    assert review["session_id"] == "sess-w1"
    assert review["error"] is None
    log_file = tmp_path / "logs" / f"review-{review_id}.log"
    assert log_file.exists()
    ws_dir = tmp_path / "workspaces" / "10" / "1" / str(review_id)
    assert not ws_dir.exists()
    assert await worker.process_one() is False


async def test_worker_clone_failure_retries_then_error(tmp_path, db, origin, monkeypatch):
    monkeypatch.setattr("mrwatch.worker.CLONE_RETRY_BACKOFF_SEC", 0.0)
    review_id = _queued_review(db, origin)
    opencode_bin = _fake_opencode(tmp_path, "#!/bin/sh\ntrue\n")
    worker = _worker(tmp_path, db, origin, opencode_bin)
    db.execute("UPDATE mr SET iid = 999 WHERE id = 1")
    db.commit()

    for _ in range(3):
        await worker.process_one()

    review = get_review(db, review_id)
    assert review["status"] == "error"
    assert "clone failed" in review["error"]


async def test_worker_opencode_failure_error_no_retry(tmp_path, db, origin):
    review_id = _queued_review(db, origin)
    opencode_bin = _fake_opencode(tmp_path, "#!/bin/sh\necho broken >&2\nexit 7\n")
    worker = _worker(tmp_path, db, origin, opencode_bin)

    await worker.process_one()

    review = get_review(db, review_id)
    assert review["status"] == "error"
    assert "broken" in review["error"]


async def test_worker_counts_severities(tmp_path, db, origin):
    review_id = _queued_review(db, origin)
    global NDJSON_OUTPUT  # noqa: F824
    output = (
        '{"type":"session.started","sessionID":"sess-c1"}\n'
        '{"type":"message.part","part":{"type":"text","text":"## Findings\\n'
        "- [BLOCKER] a.py:1 — x — y\\n- [NIT] b.py:2 — x — y\\n"
        '- [MAJOR] c.py:3 — x — y\\n"}}\n'
    )
    opencode_bin = _fake_opencode(tmp_path, f"#!/bin/sh\nprintf '%s' '{output}'\n")
    worker = _worker(tmp_path, db, origin, opencode_bin)

    await worker.process_one()

    review = get_review(db, review_id)
    assert review["status"] == "done"
    assert review["crit_count"] == 1
    assert review["major_count"] == 1
    assert review["minor_count"] == 0
    assert review["nit_count"] == 1


async def test_worker_rejects_unsafe_agent(tmp_path, db, origin):
    review_id = _queued_review(db, origin)
    marker = tmp_path / "ran"
    opencode_bin = _fake_opencode(tmp_path, f"#!/bin/sh\ntouch {marker}\n")
    worker = _worker(tmp_path, db, origin, opencode_bin)
    (tmp_path / "agents" / "agent-a.md").write_text(
        "---\ntools:\n  edit: true\n  bash: false\n---\n"
    )

    await worker.process_one()

    review = get_review(db, review_id)
    assert review["status"] == "error"
    assert "agent rejected" in review["error"]
    assert not marker.exists()


async def test_worker_passes_model_variant_and_retries_on_length(tmp_path, db, origin):
    review_id = _queued_review(db, origin)
    argv_log = tmp_path / "argv.txt"
    length = (
        '{"type":"step_finish","part":{"type":"step-finish","reason":"length",'
        '"tokens":{"output":0,"reasoning":8192}}}'
    )
    ok = '{"sessionID":"s2","part":{"type":"text","text":"ОК"}}'
    script = (
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" | grep -A1 -E '^--(model|variant)$' | tr '\\n' ' ' >> {argv_log}; echo >> {argv_log}\n"
        'case "$*" in *"--variant low"*) '
        f"printf '%s' '{ok}';; *) printf '%s' '{length}';; esac\n"
    )
    opencode_bin = _fake_opencode(tmp_path, script)
    worker = _worker(tmp_path, db, origin, opencode_bin)
    worker._config.opencode.model = "prov/model"

    await worker.process_one()

    review = get_review(db, review_id)
    assert review["status"] == "done"
    lines = argv_log.read_text().splitlines()
    assert len(lines) == 2
    assert all("--model prov/model" in line for line in lines)
    assert "--variant" not in lines[0]
    assert "--variant low" in lines[1]


def _repo_url_for(base: str, path: str) -> str:
    config = Config(
        gitlab=GitlabConfig(url=base, pat_command="printf tok"),
        projects=[ProjectConfig(gitlab_project_id=10, slug="g/p", agent="agent-a")],
    )
    worker = Worker.__new__(Worker)
    worker._static_config = config
    worker._config_provider = None
    return worker._repo_url({"path_with_namespace": path})


def test_repo_url_has_no_userinfo_and_uses_https_host():
    from urllib.parse import urlsplit

    parts = urlsplit(_repo_url_for("https://gitlab.example.com", "g/p"))
    assert parts.scheme == "https"
    assert parts.hostname == "gitlab.example.com"
    assert "@" not in parts.netloc
    assert parts.username is None and parts.password is None


def test_repo_url_trailing_slash_no_double_slash():
    url = _repo_url_for("https://gitlab.example.com/", "g/p")
    assert url == "https://gitlab.example.com/g/p.git"
    assert "//" not in url.split("://", 1)[1]


def test_repo_url_subgroups_and_subpath():
    assert (
        _repo_url_for("https://example.com/gitlab/", "a/b/c/repo")
        == "https://example.com/gitlab/a/b/c/repo.git"
    )


async def _wait_until(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


def _workspace(tmp_path: Path, review_id: int) -> Path:
    return tmp_path / "workspaces" / "10" / "1" / str(review_id)


def _spy_finish(monkeypatch) -> list[int]:
    calls: list[int] = []
    real = worker_module.finish_review

    def spy(conn, review_id, *args, **kwargs):
        calls.append(review_id)
        return real(conn, review_id, *args, **kwargs)

    monkeypatch.setattr(worker_module, "finish_review", spy)
    return calls


async def test_cancel_during_clone_removes_workspace_and_worker_continues(
    tmp_path, db, origin, monkeypatch
):
    sync_projects(db, [(10, "g/p", "agent-a", False, True)])
    head_sha = _git("rev-parse", "merge-requests/1/head", cwd=origin).strip()
    mr_id = upsert_mr(
        db, project_db_id=1, iid=1, title="MR 1", author_username="other",
        source_branch="feature", web_url="https://gitlab.example.com/g/p/-/merge_requests/1",
        state="opened", head_sha="f" * 40, updated_at="2026-01-01T12:00:00Z",
    ).mr_id
    stale_id = create_review(db, mr_id, "new", "f" * 40, "agent-a")
    registry = ReviewRegistry()
    opencode_bin = _fake_opencode(tmp_path, f"#!/bin/sh\nprintf '%s' '{NDJSON_OUTPUT}'\n")
    worker = _worker(tmp_path, db, origin, opencode_bin, registry)
    finished = _spy_finish(monkeypatch)
    real_clone = worker_module.clone_for_review
    clone_started = asyncio.Event()

    async def blocking_clone(**kwargs):
        if kwargs["expected_sha"] != "f" * 40:
            return await real_clone(**kwargs)
        kwargs["workspace"].mkdir(parents=True)
        clone_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(worker_module, "clone_for_review", blocking_clone)
    run_task = asyncio.create_task(worker.run())
    try:
        await asyncio.wait_for(clone_started.wait(), timeout=10)
        assert _workspace(tmp_path, stale_id).exists()

        supersede_reviews(db, mr_id, head_sha)
        assert registry.cancel(stale_id) is True
        fresh_id = create_review(db, mr_id, "push", head_sha, "agent-a")

        await _wait_until(lambda: get_review(db, fresh_id)["status"] == "done")
    finally:
        run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)

    stale = get_review(db, stale_id)
    assert stale["status"] == "cancelled"
    assert stale["review_md"] is None
    assert stale_id not in finished
    assert not _workspace(tmp_path, stale_id).exists()
    assert not registry.was_cancelled(stale_id)
    assert registry.cancel(stale_id) is False


async def test_cancel_during_review_discards_result(tmp_path, db, origin, monkeypatch):
    review_id = _queued_review(db, origin)
    registry = ReviewRegistry()
    worker = _worker(tmp_path, db, origin, "/bin/true", registry)
    finished = _spy_finish(monkeypatch)
    started = asyncio.Event()
    observed: list[str] = []

    async def blocking_review(**kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            observed.append("killed")
            raise

    monkeypatch.setattr(worker_module, "run_review", blocking_review)
    task = asyncio.create_task(worker.process_one())
    await asyncio.wait_for(started.wait(), timeout=10)
    assert get_review(db, review_id)["status"] == "reviewing"
    assert _workspace(tmp_path, review_id).exists()

    supersede_reviews(db, get_review(db, review_id)["mr_id"], "e" * 40)
    registry.cancel(review_id)

    assert await task is True
    review = get_review(db, review_id)
    assert review["status"] == "cancelled"
    assert review["review_md"] is None
    assert observed == ["killed"]
    assert review_id not in finished
    assert not _workspace(tmp_path, review_id).exists()


async def test_worker_shutdown_propagates_cancel(tmp_path, db, origin, monkeypatch):
    review_id = _queued_review(db, origin)
    registry = ReviewRegistry()
    worker = _worker(tmp_path, db, origin, "/bin/true", registry)
    finished = _spy_finish(monkeypatch)
    started = asyncio.Event()

    async def blocking_review(**kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(worker_module, "run_review", blocking_review)
    task = asyncio.create_task(worker.process_one())
    await asyncio.wait_for(started.wait(), timeout=10)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert get_review(db, review_id)["status"] == "reviewing"
    assert finished == []
    assert not _workspace(tmp_path, review_id).exists()
    assert registry.cancel(review_id) is False

