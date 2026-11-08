from __future__ import annotations

import asyncio
import logging
import shutil
import sqlite3
from collections.abc import Callable
from pathlib import Path

from mrwatch.agents import AgentError, validate_agent
from mrwatch.config import Config, ResolvedPaths
from mrwatch.db import (
    claim_next_review,
    finish_review,
    mark_reviewing,
    rebase_review_head,
    requeue_review,
)
from mrwatch.git_ops import (
    BASE_REF,
    GitEnv,
    GitOpsError,
    HeadMismatchError,
    clone_for_review,
    diff_for_review,
    is_safe_branch,
    make_git_env,
    remove_workspace,
    run_git,
)
from mrwatch.gitlab_client import GitLabClient
from mrwatch.opencode_runner import (
    OpencodeError,
    RunnerEnv,
    build_review_prompt,
    parse_severity_counts,
    parse_verdict,
    run_review,
)

logger = logging.getLogger(__name__)

CLONE_RETRY_BACKOFF_SEC = 5.0
MAX_CLONE_ATTEMPTS = 3
MIN_FREE_DISK_BYTES = 5 * 1024**3
IDLE_SLEEP_SEC = 2.0
ERROR_PAUSE_SEC = 5.0


class Worker:
    def __init__(
        self,
        worker_id: int,
        conn: sqlite3.Connection,
        api: GitLabClient,
        config: Config,
        paths: ResolvedPaths,
        work_event: asyncio.Event | None = None,
        clone_harden: bool = True,
        agents_dirs: tuple[Path, ...] | None = None,
        config_provider: Callable[[], Config] | None = None,
    ) -> None:
        self.worker_id = worker_id
        self._conn = conn
        self._api = api
        self._static_config = config
        self._config_provider = config_provider
        self._paths = paths
        self._work_event = work_event
        self._clone_harden = clone_harden
        self._agents_dirs = agents_dirs

    @property
    def _config(self) -> Config:
        if self._config_provider is not None:
            return self._config_provider()
        return self._static_config

    async def run(self) -> None:
        while True:
            if self._work_event is not None:
                self._work_event.clear()
            try:
                processed = await self.process_one()
            except Exception:
                logger.exception("worker %s: loop iteration failed", self.worker_id)
                await asyncio.sleep(ERROR_PAUSE_SEC)
                continue
            if processed:
                continue
            await self._idle()

    async def _idle(self) -> None:
        if self._work_event is None:
            await asyncio.sleep(IDLE_SLEEP_SEC)
            return
        try:
            await asyncio.wait_for(self._work_event.wait(), timeout=IDLE_SLEEP_SEC)
        except asyncio.TimeoutError:
            pass

    async def process_one(self) -> bool:
        review = claim_next_review(self._conn)
        if review is None:
            return False
        try:
            await self._process_review(review)
        except Exception:
            logger.exception("worker %s: review %s crashed", self.worker_id, review["id"])
            finish_review(
                self._conn, review["id"], "error",
                error="internal worker error (see app.log)",
            )
        return True

    def _load_mr(self, mr_id: int) -> sqlite3.Row | None:
        return self._conn.execute(
            """
            SELECT mr.*, project.gitlab_project_id, project.path_with_namespace
            FROM mr JOIN project ON project.id = mr.project_id
            WHERE mr.id = ?
            """,
            (mr_id,),
        ).fetchone()

    def _repo_url(self, mr: sqlite3.Row) -> str:
        return f"{self._config.gitlab.url}/{mr['path_with_namespace']}.git"

    def _low_disk(self) -> bool:
        usage = shutil.disk_usage(str(self._paths.workspaces))
        return usage.free < MIN_FREE_DISK_BYTES

    def _requeue_or_fail(self, review_id: int, attempt: int, error: str) -> None:
        if attempt < MAX_CLONE_ATTEMPTS:
            requeue_review(self._conn, review_id, delay_sec=CLONE_RETRY_BACKOFF_SEC * attempt)
        else:
            finish_review(self._conn, review_id, "error", error=error)

    def _handle_head_mismatch(self, review: sqlite3.Row, exc: HeadMismatchError) -> None:
        review_id = review["id"]
        logger.warning("review %s: %s", review_id, exc)
        if rebase_review_head(self._conn, review_id, exc.actual):
            self._requeue_or_fail(review_id, review["attempt"], f"MR head moved: {exc}")
        else:
            finish_review(
                self._conn, review_id, "error",
                error=f"MR head moved to {exc.actual[:12]}; a review for it already exists",
            )

    async def _resolve_base_sha(
        self, workspace: Path, detail: dict, diff_refs: dict, head_sha: str
    ) -> str | None:
        if detail.get("sha") == head_sha:
            return diff_refs.get("base_sha")
        target = detail.get("target_branch")
        if not target or not is_safe_branch(target):
            return None
        env = GitEnv(
            pat_command="",
            base_env={
                "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
                "HOME": str(Path.home()),
            },
        )
        try:
            out = await run_git(
                ["git", "merge-base", BASE_REF, head_sha],
                workspace, env,
            )
        except GitOpsError as exc:
            logger.warning("merge-base failed in %s: %s", workspace.name, exc)
            return None
        return out.strip() or None

    async def _process_review(self, review: sqlite3.Row) -> None:
        review_id = review["id"]
        config = self._config
        if self._low_disk():
            finish_review(self._conn, review_id, "error", error="low disk: <5GB free")
            return
        mr = self._load_mr(review["mr_id"])
        if mr is None:
            finish_review(self._conn, review_id, "error", error="mr row missing")
            return
        try:
            validate_agent(review["agent"], self._agents_dirs)
        except AgentError as exc:
            finish_review(self._conn, review_id, "error", error=f"agent rejected: {exc}")
            return
        try:
            detail = await self._api.get_project_mr(
                mr["gitlab_project_id"], mr["iid"]
            )
        except Exception as exc:
            logger.warning("review %s: MR detail fetch failed: %s", review_id, exc)
            self._requeue_or_fail(review_id, review["attempt"], f"MR detail fetch failed: {exc}")
            return

        workspace = (
            self._paths.workspaces
            / str(mr["gitlab_project_id"])
            / str(mr["iid"])
            / str(review_id)
        )
        repo_url = self._repo_url(mr)
        try:
            await clone_for_review(
                workspace=workspace,
                repo_url=repo_url,
                mr_iid=mr["iid"],
                target_branch=detail.get("target_branch"),
                depth=config.review.clone_depth,
                env=make_git_env(config.gitlab.pat_command, self._paths.workspaces.parent / "tmp"),
                tmpdir=self._paths.workspaces.parent / "tmp",
                harden=self._clone_harden,
                expected_sha=review["head_sha"],
            )
        except HeadMismatchError as exc:
            await asyncio.to_thread(remove_workspace, workspace, self._paths.workspaces)
            self._handle_head_mismatch(review, exc)
            return
        except Exception as exc:
            await asyncio.to_thread(remove_workspace, workspace, self._paths.workspaces)
            logger.warning("review %s: clone failed: %s", review_id, exc)
            self._requeue_or_fail(review_id, review["attempt"], f"clone failed: {exc}")
            return

        diff_refs = detail.get("diff_refs") or {}
        mark_reviewing(self._conn, review_id)
        try:
            base_sha = await self._resolve_base_sha(
                workspace, detail, diff_refs, review["head_sha"]
            )
            diff = await diff_for_review(workspace, base_sha, review["head_sha"])
            prompt = build_review_prompt(
                title=mr["title"] or "",
                author=mr["author_username"],
                description=detail.get("description"),
                diff=diff,
            )
            log_path = self._paths.logs / f"review-{review_id}.log"
            project_cfg = next(
                (p for p in config.projects if p.gitlab_project_id == mr["gitlab_project_id"]),
                None,
            )
            oc = config.opencode
            result = await run_review(
                env=RunnerEnv(opencode_bin=oc.bin),
                workspace=workspace,
                agent=review["agent"],
                prompt=prompt,
                timeout_sec=config.review.timeout_sec,
                log_path=log_path,
                tmpdir=self._paths.workspaces.parent / "tmp",
                model=(project_cfg.model if project_cfg and project_cfg.model else oc.model),
                variant=(project_cfg.variant if project_cfg and project_cfg.variant else oc.variant),
                length_retry_variant=oc.length_retry_variant,
            )
        except OpencodeError as exc:
            finish_review(self._conn, review_id, "error", error=f"opencode failed: {exc}")
            return
        finally:
            await asyncio.to_thread(remove_workspace, workspace, self._paths.workspaces)
        if result.error is not None and result.review_md is None:
            finish_review(
                self._conn, review_id, "error",
                session_id=result.session_id,
                exit_code=result.exit_code,
                error=result.error,
            )
        else:
            counts = parse_severity_counts(result.review_md)
            finish_review(
                self._conn, review_id, "done",
                review_md=result.review_md or "(empty review)",
                session_id=result.session_id,
                exit_code=result.exit_code,
                counts=counts,
                verdict=parse_verdict(result.review_md),
            )


def start_workers(
    count: int,
    conn: sqlite3.Connection,
    api: GitLabClient,
    config: Config,
    paths: ResolvedPaths,
    work_event: asyncio.Event,
    config_provider: Callable[[], Config] | None = None,
) -> list[asyncio.Task]:
    workers = [
        Worker(i, conn, api, config, paths, work_event, config_provider=config_provider)
        for i in range(count)
    ]
    return [asyncio.create_task(w.run(), name=f"mrwatch-worker-{i}") for i, w in enumerate(workers)]
