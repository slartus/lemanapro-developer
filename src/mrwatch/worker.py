from __future__ import annotations

import asyncio
import logging
import shutil
import sqlite3
from pathlib import Path

from mrwatch.config import Config, ResolvedPaths
from mrwatch.db import claim_next_review, finish_review, mark_reviewing, requeue_review
from mrwatch.git_ops import GitEnv, clone_for_review, diff_for_review, make_git_env, remove_workspace
from mrwatch.gitlab_client import GitLabClient
from mrwatch.opencode_runner import (
    OpencodeError,
    RunnerEnv,
    build_review_prompt,
    collect_project_instructions,
    parse_severity_counts,
    run_review,
)

logger = logging.getLogger(__name__)

CLONE_RETRY_BACKOFF_SEC = 5.0
MAX_CLONE_ATTEMPTS = 3
MIN_FREE_DISK_BYTES = 5 * 1024**3
IDLE_SLEEP_SEC = 2.0


class Worker:
    def __init__(
        self,
        worker_id: int,
        conn: sqlite3.Connection,
        api: GitLabClient,
        config: Config,
        paths: ResolvedPaths,
        queue: asyncio.Queue | None = None,
        clone_harden: bool = True,
    ) -> None:
        self.worker_id = worker_id
        self._conn = conn
        self._api = api
        self._config = config
        self._paths = paths
        self._queue = queue
        self._clone_harden = clone_harden
        self._git_env = make_git_env(config.gitlab.pat_command, paths.workspaces.parent / "tmp")

    async def run(self) -> None:
        while True:
            processed = await self.process_one()
            if not processed:
                await asyncio.sleep(IDLE_SLEEP_SEC)

    async def process_one(self) -> bool:
        if self._queue is not None:
            try:
                await asyncio.wait_for(self._queue.get(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
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
            requeue_review(self._conn, review_id)
        else:
            finish_review(self._conn, review_id, "error", error=error)

    async def _process_review(self, review: sqlite3.Row) -> None:
        review_id = review["id"]
        if self._low_disk():
            finish_review(self._conn, review_id, "error", error="low disk: <5GB free")
            return
        mr = self._load_mr(review["mr_id"])
        if mr is None:
            finish_review(self._conn, review_id, "error", error="mr row missing")
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
                depth=self._config.review.clone_depth,
                env=self._git_env,
                tmpdir=self._paths.workspaces.parent / "tmp",
                harden=self._clone_harden,
            )
        except Exception as exc:
            logger.warning("review %s: clone failed: %s", review_id, exc)
            self._requeue_or_fail(review_id, review["attempt"], f"clone failed: {exc}")
            return

        diff_refs = detail.get("diff_refs") or {}
        mark_reviewing(self._conn, review_id)
        try:
            diff = await diff_for_review(
                workspace, diff_refs.get("base_sha"), mr["head_sha"]
            )
            prompt = build_review_prompt(
                title=mr["title"] or "",
                author=mr["author_username"],
                description=detail.get("description"),
                diff=diff,
            )
            log_path = self._paths.logs / f"review-{review_id}.log"
            result = await run_review(
                env=RunnerEnv(opencode_bin=self._config.opencode.bin),
                workspace=workspace,
                agent=review["agent"],
                prompt=prompt,
                timeout_sec=self._config.review.timeout_sec,
                log_path=log_path,
                tmpdir=self._paths.workspaces.parent / "tmp",
            )
        except OpencodeError as exc:
            finish_review(self._conn, review_id, "error", error=f"opencode failed: {exc}")
            return
        finally:
            remove_workspace(workspace)
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
            )


def start_workers(
    count: int,
    conn: sqlite3.Connection,
    api: GitLabClient,
    config: Config,
    paths: ResolvedPaths,
    queue: asyncio.Queue,
) -> list[asyncio.Task]:
    workers = [
        Worker(i, conn, api, config, paths, queue) for i in range(count)
    ]
    return [asyncio.create_task(w.run(), name=f"mrwatch-worker-{i}") for i, w in enumerate(workers)]
