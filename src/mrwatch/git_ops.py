from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mrwatch.procutil import OutputLimitExceeded, communicate_capped, kill_group as _kill_group, kill_on_exit

logger = logging.getLogger(__name__)

GIT_COMMAND_TIMEOUT_SEC = 600
GIT_HARD_LIMIT_BYTES = 50_000_000
GIT_OUTPUT_CAP_BYTES = 200_000
DIFF_OUTPUT_CAP_BYTES = 512_000
HARDEN_ARGS = (
    "-c", "core.hooksPath=/dev/null",
    "-c", "protocol.file.allow=never",
    "-c", "transfer.fsckObjects=true",
    "-c", "core.symlinks=false",
)
SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/@+\-]*$")
SANITIZE_NAMES = (
    ".opencode",
    "opencode.json",
    "opencode.jsonc",
    "AGENTS.md",
    "CLAUDE.md",
    "CONTEXT.md",
    ".claude",
    ".agents",
)
NESTED_CONTEXT_NAMES = frozenset({"AGENTS.md", "CLAUDE.md", "CONTEXT.md"})
_ROOT_FORBIDDEN = frozenset(n.casefold() for n in SANITIZE_NAMES)
_NESTED_FORBIDDEN = frozenset(n.casefold() for n in NESTED_CONTEXT_NAMES)
BASE_REF = "refs/mrwatch/base"
_GLPAT_RE = re.compile(r"glpat-[A-Za-z0-9_\-]+")
_BEARER_RE = re.compile(r"Bearer\s+\S+", re.IGNORECASE)


class GitOpsError(Exception):
    pass


class HeadMismatchError(GitOpsError):
    def __init__(self, expected: str, actual: str) -> None:
        super().__init__(f"MR head moved: expected {expected[:12]}, got {actual[:12]}")
        self.expected = expected
        self.actual = actual


@dataclass(frozen=True)
class GitEnv:
    pat_command: str
    base_env: dict[str, str]


def make_git_env(pat_command: str, tmpdir: Path) -> GitEnv:
    return GitEnv(
        pat_command=pat_command,
        base_env={
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(Path.home()),
            "TMPDIR": str(tmpdir),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_LFS_SKIP_SMUDGE": "1",
        },
    )


def build_clone_args(repo_url: str, dest: Path, depth: int) -> list[str]:
    return [
        "git",
        "clone",
        "--depth", str(depth),
        "--no-checkout",
        "--single-branch",
        repo_url,
        str(dest),
    ]


def is_safe_branch(name: str) -> bool:
    return bool(BRANCH_RE.fullmatch(name)) and ".." not in name and not name.endswith((".lock", "/", "."))


def mask_secrets(text: str) -> str:
    return _BEARER_RE.sub("Bearer ***", _GLPAT_RE.sub("glpat-***", text))


async def run_git(
    args: list[str],
    cwd: Path,
    env: GitEnv,
    harden: bool = True,
    timeout_sec: int = GIT_COMMAND_TIMEOUT_SEC,
    output_cap: int = GIT_OUTPUT_CAP_BYTES,
    tmpdir: Path | None = None,
) -> str:
    proc_env = dict(env.base_env)
    askpass_path: Path | None = None
    if env.pat_command:
        if tmpdir is None:
            raise GitOpsError("tmpdir required when pat_command is set")
        askpass_path = tmpdir / f"askpass-{uuid.uuid4().hex}.sh"
        askpass_path.write_text(f"#!/bin/sh\nexec {env.pat_command}\n", encoding="utf-8")
        askpass_path.chmod(0o700)
        proc_env["GIT_ASKPASS"] = str(askpass_path)
    full_args = ["git", *(HARDEN_ARGS if harden else ()), *args[1:]]
    try:
        proc = await asyncio.create_subprocess_exec(
            *full_args,
            cwd=cwd,
            env=proc_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                communicate_capped(proc, GIT_HARD_LIMIT_BYTES), timeout=timeout_sec
            )
        except asyncio.TimeoutError:
            await _kill_group(proc)
            raise GitOpsError(f"git timed out after {timeout_sec}s: {' '.join(args[1:4])}")
        except OutputLimitExceeded:
            await _kill_group(proc)
            raise GitOpsError(f"git output exceeded {GIT_HARD_LIMIT_BYTES} bytes: {' '.join(args[1:4])}")
        except BaseException:
            await kill_on_exit(proc)
            raise
    finally:
        if askpass_path is not None and askpass_path.exists():
            askpass_path.unlink()
    out = stdout[:output_cap].decode("utf-8", "replace")
    err = stderr[:output_cap].decode("utf-8", "replace")
    if proc.returncode != 0:
        tail = mask_secrets(err.strip()[-500:])
        raise GitOpsError(f"git {' '.join(args[1:3])} failed (exit {proc.returncode}): {tail}")
    return mask_secrets(out)


async def clone_for_review(
    workspace: Path,
    repo_url: str,
    mr_iid: int,
    target_branch: str | None,
    depth: int,
    env: GitEnv,
    tmpdir: Path,
    harden: bool = True,
    expected_sha: str | None = None,
) -> Path:
    workspace = Path(workspace).resolve()
    if os.path.lexists(workspace):
        await asyncio.to_thread(remove_workspace, workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    try:
        await run_git(build_clone_args(repo_url, workspace, depth), tmpdir, env, harden=harden, tmpdir=tmpdir)
        await run_git(
            ["git", "fetch", "--depth", str(depth), "origin", f"merge-requests/{mr_iid}/head"],
            workspace, env, harden=harden, tmpdir=tmpdir,
        )
        if expected_sha:
            await _pin_fetch_head(workspace, expected_sha, depth, env, tmpdir, harden)
        await run_git(["git", "checkout", "-q", "FETCH_HEAD"], workspace, env, harden=harden, tmpdir=tmpdir)
        if expected_sha:
            head = (await run_git(
                ["git", "rev-parse", "HEAD"], workspace, env, harden=harden, tmpdir=tmpdir
            )).strip()
            if head != expected_sha:
                raise HeadMismatchError(expected_sha, head)
        if target_branch and not is_safe_branch(target_branch):
            logger.warning("base branch skipped, unsafe name: %r", target_branch)
        elif target_branch:
            refspec = f"refs/heads/{target_branch}:{BASE_REF}"
            try:
                await run_git(
                    ["git", "fetch", "--depth", str(depth), "origin", refspec],
                    workspace, env, harden=harden, tmpdir=tmpdir,
                )
            except GitOpsError as exc:
                logger.warning("base branch fetch failed (%s): %s", target_branch, exc)
        try:
            await run_git(["git", "remote", "remove", "origin"], workspace, env, harden=harden, tmpdir=tmpdir)
        except GitOpsError as exc:
            logger.warning("remote remove failed: %s", exc)
        try:
            await asyncio.to_thread(sanitize_clone, workspace)
        except (GitOpsError, OSError) as exc:
            raise GitOpsError(f"workspace sanitize failed: {exc}") from exc
    except BaseException:
        await _discard_workspace(workspace)
        raise
    return workspace


async def _discard_workspace(workspace: Path) -> None:
    await asyncio.shield(asyncio.to_thread(remove_workspace, workspace))


async def _pin_fetch_head(
    workspace: Path, expected_sha: str, depth: int, env: GitEnv, tmpdir: Path, harden: bool
) -> None:
    fetched = (await run_git(
        ["git", "rev-parse", "FETCH_HEAD"], workspace, env, harden=harden, tmpdir=tmpdir
    )).strip()
    if fetched == expected_sha:
        return
    if not SHA_RE.fullmatch(expected_sha):
        raise HeadMismatchError(expected_sha, fetched)
    try:
        await run_git(
            ["git", "fetch", "--depth", str(depth), "origin", expected_sha],
            workspace, env, harden=harden, tmpdir=tmpdir,
        )
    except GitOpsError as exc:
        logger.warning("expected sha %s not fetchable: %s", expected_sha[:12], exc)
        raise HeadMismatchError(expected_sha, fetched) from exc


def _raise_walk_error(exc: OSError) -> None:
    raise exc


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def sanitize_clone(workspace: Path) -> None:
    leftovers: list[str] = []
    for root, dirs, files in os.walk(workspace, followlinks=False, onerror=_raise_walk_error):
        if ".git" in dirs and Path(root) == workspace:
            dirs.remove(".git")
        forbidden = _ROOT_FORBIDDEN if Path(root) == workspace else _NESTED_FORBIDDEN
        for name in [n for n in dirs + files if n.casefold() in forbidden]:
            path = Path(root) / name
            _remove_path(path)
            if name in dirs:
                dirs.remove(name)
            if os.path.lexists(path):
                leftovers.append(str(path))
    if leftovers:
        raise GitOpsError(f"sanitize left forbidden paths: {', '.join(leftovers)}")


async def diff_for_review(
    workspace: Path,
    base_sha: str | None,
    head_sha: str | None,
) -> str | None:
    if not base_sha or not head_sha:
        return None
    if not SHA_RE.fullmatch(base_sha) or not SHA_RE.fullmatch(head_sha):
        logger.warning("diff skipped, invalid sha in %s", workspace.name)
        return None
    env = GitEnv(
        pat_command="",
        base_env={
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(Path.home()),
        },
    )
    try:
        return await run_git(
            ["git", "diff", "--no-color", "--no-ext-diff", "--no-textconv", base_sha, head_sha, "--"],
            workspace,
            env,
            harden=True,
            output_cap=DIFF_OUTPUT_CAP_BYTES,
        )
    except GitOpsError as exc:
        logger.warning("diff failed in %s: %s", workspace.name, exc)
        return None


def remove_workspace(workspace: Path, stop_at: Path | None = None) -> None:
    shutil.rmtree(workspace, ignore_errors=True)
    if stop_at is None:
        return
    stop = Path(stop_at).resolve()
    parent = Path(workspace).resolve().parent
    while parent != stop and stop in parent.parents:
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def cleanup_old_workspaces(workspaces_root: Path, ttl_days: int) -> int:
    cutoff = datetime.now(UTC) - timedelta(days=ttl_days)
    removed = 0
    for review_dir in workspaces_root.glob("*/*/*"):
        if not review_dir.is_dir():
            continue
        mtime = datetime.fromtimestamp(review_dir.stat().st_mtime, UTC)
        if mtime < cutoff:
            remove_workspace(review_dir)
            removed += 1
    for pattern in ("*/*", "*"):
        for directory in workspaces_root.glob(pattern):
            if directory.is_dir() and not directory.is_symlink():
                try:
                    directory.rmdir()
                except OSError:
                    pass
    return removed


def cleanup_old_review_logs(logs_dir: Path, ttl_days: int) -> int:
    cutoff = (datetime.now(UTC) - timedelta(days=ttl_days)).timestamp()
    removed = 0
    for log in logs_dir.glob("review-*.log"):
        try:
            if log.is_file() and log.stat().st_mtime < cutoff:
                log.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def cleanup_stale_askpass(tmpdir: Path) -> int:
    removed = 0
    for path in tmpdir.glob("askpass-*.sh"):
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed
