from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import signal
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

GIT_COMMAND_TIMEOUT_SEC = 600
KILL_GRACE_SEC = 5.0
GIT_OUTPUT_CAP_BYTES = 200_000
DIFF_OUTPUT_CAP_BYTES = 512_000
HARDEN_ARGS = (
    "-c", "core.hooksPath=/dev/null",
    "-c", "protocol.file.allow=never",
    "-c", "transfer.fsckObjects=true",
)
_GLPAT_RE = re.compile(r"glpat-[A-Za-z0-9_\-]+")
_BEARER_RE = re.compile(r"Bearer\s+\S+", re.IGNORECASE)


class GitOpsError(Exception):
    pass


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


def mask_secrets(text: str) -> str:
    return _BEARER_RE.sub("Bearer ***", _GLPAT_RE.sub("glpat-***", text))


async def _kill_group(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=KILL_GRACE_SEC)
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.wait()


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
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
        except asyncio.TimeoutError:
            await _kill_group(proc)
            raise GitOpsError(f"git timed out after {timeout_sec}s: {' '.join(args[1:4])}")
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
) -> Path:
    workspace = Path(workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    try:
        await run_git(build_clone_args(repo_url, workspace, depth), tmpdir, env, harden=harden, tmpdir=tmpdir)
        await run_git(
            ["git", "fetch", "--depth", str(depth), "origin", f"merge-requests/{mr_iid}/head"],
            workspace, env, harden=harden, tmpdir=tmpdir,
        )
        await run_git(["git", "checkout", "-q", "FETCH_HEAD"], workspace, env, harden=harden, tmpdir=tmpdir)
        if target_branch:
            try:
                await run_git(
                    ["git", "fetch", "--depth", str(depth), "origin", target_branch],
                    workspace, env, harden=harden, tmpdir=tmpdir,
                )
            except GitOpsError as exc:
                logger.warning("base branch fetch failed (%s): %s", target_branch, exc)
    except GitOpsError:
        remove_workspace(workspace)
        raise
    try:
        await run_git(["git", "remote", "remove", "origin"], workspace, env, harden=harden, tmpdir=tmpdir)
    except GitOpsError as exc:
        logger.warning("remote remove failed: %s", exc)
    sanitize_clone(workspace)
    return workspace


def sanitize_clone(workspace: Path) -> None:
    for name in (".opencode", "opencode.json", "opencode.jsonc"):
        path = workspace / name
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink()


async def diff_for_review(
    workspace: Path,
    base_sha: str | None,
    head_sha: str | None,
) -> str | None:
    if not base_sha or not head_sha:
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
            ["git", "diff", "--no-color", base_sha, head_sha],
            workspace,
            env,
            harden=False,
            output_cap=DIFF_OUTPUT_CAP_BYTES,
        )
    except GitOpsError as exc:
        logger.warning("diff failed in %s: %s", workspace.name, exc)
        return None


def remove_workspace(workspace: Path) -> None:
    shutil.rmtree(workspace, ignore_errors=True)


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
    return removed
