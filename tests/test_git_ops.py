from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mrwatch.git_ops import (
    GitEnv,
    build_clone_args,
    cleanup_old_workspaces,
    clone_for_review,
    diff_for_review,
    make_git_env,
    mask_secrets,
    sanitize_clone,
)


def _git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home()),
             "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    )
    return result.stdout


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    origin = tmp_path / "origin"
    origin.mkdir()
    _git("init", "-q", "-b", "main", cwd=origin)
    (origin / "app.py").write_text("print('hi')\n")
    _git("add", ".", cwd=origin)
    _git("commit", "-qm", "init", cwd=origin)
    _git("branch", "feature", cwd=origin)
    _git("checkout", "-q", "feature", cwd=origin)
    (origin / "app.py").write_text("print('hello')\n")
    (origin / "new.py").write_text("x = 1\n")
    _git("add", ".", cwd=origin)
    _git("commit", "-qm", "feature work", cwd=origin)
    _git("checkout", "-q", "main", cwd=origin)
    feature_sha = _git("rev-parse", "feature", cwd=origin).strip()
    _git("update-ref", "refs/merge-requests/1/head", feature_sha, cwd=origin)
    return origin


NO_ENV = GitEnv(pat_command="", base_env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home())})


def test_build_clone_args(tmp_path):
    args = build_clone_args("https://gitlab.example.com/g/p.git", tmp_path / "w", 50)
    assert args[0] == "git"
    assert "clone" in args
    assert "--no-checkout" in args
    assert "--single-branch" in args
    assert "--depth" in args
    assert args[-1] == str(tmp_path / "w")
    assert "-c" not in args


def test_mask_secrets():
    assert mask_secrets("token glpat-abc123 end") == "token glpat-*** end"


async def test_clone_for_review_full_flow(origin, tmp_path):
    import asyncio

    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    workspace = tmp_path / "ws"
    await clone_for_review(
        workspace=workspace,
        repo_url=str(origin),
        mr_iid=1,
        target_branch="main",
        depth=5,
        env=NO_ENV,
        tmpdir=tmpdir,
        harden=False,
    )
    content = (workspace / "app.py").read_text()
    assert content == "print('hello')\n"
    assert (workspace / "new.py").exists()
    assert not (workspace / ".mrwatch-askpass.sh").exists()


async def test_clone_failure_raises(origin, tmp_path):
    import asyncio

    from mrwatch.git_ops import GitOpsError

    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    with pytest.raises(GitOpsError):
        await clone_for_review(
            workspace=tmp_path / "ws",
            repo_url=str(tmp_path / "nonexistent"),
            mr_iid=1,
            target_branch=None,
            depth=1,
            env=NO_ENV,
            tmpdir=tmpdir,
            harden=False,
        )


async def test_diff_for_review(origin, tmp_path):
    import asyncio

    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    workspace = tmp_path / "ws"
    await clone_for_review(workspace, str(origin), 1, "main", 5, NO_ENV, tmpdir, harden=False)
    head_sha = _git("rev-parse", "HEAD", cwd=workspace).strip()
    base_sha = _git("rev-parse", "main", cwd=workspace).strip()

    diff = await diff_for_review(workspace, base_sha, head_sha)

    assert diff is not None
    assert "app.py" in diff
    assert "new.py" in diff


async def test_diff_missing_shas_returns_none(tmp_path):
    assert await diff_for_review(tmp_path, None, "abc") is None
    assert await diff_for_review(tmp_path, "abc", None) is None


async def test_diff_bad_repo_returns_none(tmp_path):
    (tmp_path / "x").write_text("y")
    assert await diff_for_review(tmp_path, "aaa", "bbb") is None


def test_sanitize_clone(tmp_path):
    (tmp_path / ".opencode").mkdir()
    (tmp_path / ".opencode" / "evil.js").write_text("x")
    (tmp_path / "opencode.json").write_text("{}")
    (tmp_path / "opencode.jsonc").write_text("{}")
    (tmp_path / "src").mkdir()

    sanitize_clone(tmp_path)

    assert not (tmp_path / ".opencode").exists()
    assert not (tmp_path / "opencode.json").exists()
    assert not (tmp_path / "opencode.jsonc").exists()
    assert (tmp_path / "src").exists()


def test_cleanup_old_workspaces(tmp_path):
    import os
    import time

    old = tmp_path / "10" / "1" / "r1"
    fresh = tmp_path / "10" / "2" / "r2"
    old.mkdir(parents=True)
    fresh.mkdir(parents=True)
    past = time.time() - 10 * 86400
    os.utime(old, (past, past))

    removed = cleanup_old_workspaces(tmp_path, ttl_days=3)

    assert removed == 1
    assert not old.exists()
    assert fresh.exists()


def test_make_git_env_no_token(tmp_path):
    env = make_git_env("printf tok", tmp_path)
    assert "GIT_TERMINAL_PROMPT" in env.base_env
    assert "GIT_LFS_SKIP_SMUDGE" in env.base_env
    assert set(env.base_env) <= {"PATH", "HOME", "TMPDIR", "GIT_TERMINAL_PROMPT", "GIT_LFS_SKIP_SMUDGE"}
    assert all("printf tok" != v for v in env.base_env.values())


async def test_clone_removes_stale_workspace_before_clone(origin, tmp_path):
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "stale.txt").write_text("old")
    await clone_for_review(workspace, str(origin), 1, "main", 5, NO_ENV, tmpdir, harden=False)
    assert not (workspace / "stale.txt").exists()
    assert (workspace / "new.py").exists()


async def test_clone_cancelled_during_sanitize_removes_workspace(origin, tmp_path, monkeypatch):
    import asyncio

    from mrwatch import git_ops

    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    workspace = tmp_path / "ws"

    def cancelled(_workspace):
        raise asyncio.CancelledError()

    monkeypatch.setattr(git_ops, "sanitize_clone", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await clone_for_review(workspace, str(origin), 1, "main", 5, NO_ENV, tmpdir, harden=False)
    assert not workspace.exists()


async def test_clone_cancelled_during_remote_remove_removes_workspace(origin, tmp_path, monkeypatch):
    import asyncio

    from mrwatch import git_ops

    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    workspace = tmp_path / "ws"
    real_run_git = git_ops.run_git

    async def run_git(args, *a, **kw):
        if args[:3] == ["git", "remote", "remove"]:
            raise asyncio.CancelledError()
        return await real_run_git(args, *a, **kw)

    monkeypatch.setattr(git_ops, "run_git", run_git)
    with pytest.raises(asyncio.CancelledError):
        await clone_for_review(workspace, str(origin), 1, "main", 5, NO_ENV, tmpdir, harden=False)
    assert not workspace.exists()


async def test_clone_cancel_during_sanitize_removes_workspace_in_thread(origin, tmp_path, monkeypatch):
    import asyncio
    import threading

    from mrwatch import git_ops

    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    workspace = tmp_path / "ws"
    removal_threads: list[int] = []
    real_remove = git_ops.remove_workspace

    def cancelled(_workspace):
        raise asyncio.CancelledError()

    def tracking_remove(path):
        removal_threads.append(threading.get_ident())
        real_remove(path)

    monkeypatch.setattr(git_ops, "sanitize_clone", cancelled)
    monkeypatch.setattr(git_ops, "remove_workspace", tracking_remove)
    with pytest.raises(asyncio.CancelledError):
        await clone_for_review(workspace, str(origin), 1, "main", 5, NO_ENV, tmpdir, harden=False)
    assert not workspace.exists()
    assert removal_threads and removal_threads[0] != threading.get_ident()


async def test_clone_failure_then_cancel_during_discard_propagates_cancel(tmp_path, monkeypatch):
    import asyncio
    import threading
    import time

    from mrwatch import git_ops

    removing = threading.Event()
    removed = threading.Event()

    def slow_remove(path, *args, **kwargs):
        removing.set()
        time.sleep(0.3)
        removed.set()

    async def failing_run_git(*args, **kwargs):
        raise git_ops.GitOpsError("clone failed")

    monkeypatch.setattr(git_ops, "remove_workspace", slow_remove)
    monkeypatch.setattr(git_ops, "run_git", failing_run_git)

    task = asyncio.create_task(
        clone_for_review(tmp_path / "ws", "u", 1, None, 1, NO_ENV, tmp_path, harden=False)
    )
    while not removing.is_set():
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
    await asyncio.to_thread(removed.wait, 2)
    assert removed.is_set()
