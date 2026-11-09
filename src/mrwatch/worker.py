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
    DRAFT_CANCEL_ERROR,
    claim_next_question,
    claim_next_review,
    finish_question,
    finish_review,
    get_review,
    mark_reviewing,
    rebase_and_defer_review,
    rebase_review_head,
    requeue_review,
    superseded_error,
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
from mrwatch.gitlab_client import GitLabClient, parse_draft
from mrwatch.review_findings import dump_snippets, extract_snippets
from mrwatch.opencode_runner import (
    OpencodeError,
    RunnerEnv,
    build_review_prompt,
    is_valid_session_id,
    parse_severity_counts,
    parse_verdict,
    run_question,
    run_review,
)

logger = logging.getLogger(__name__)

CLONE_RETRY_BACKOFF_SEC = 5.0
MAX_CLONE_ATTEMPTS = 3
MIN_FREE_DISK_BYTES = 5 * 1024**3
IDLE_SLEEP_SEC = 2.0
ERROR_PAUSE_SEC = 5.0
QUESTION_HEAD_GONE_ERROR = "коммит ревью больше недоступен в репозитории"
QUESTION_CANCELLED_ERROR = "отменено"
CLOSED_CANCEL_ERROR = "отменено: MR закрыт"


class ReviewRegistry:
    def __init__(self) -> None:
        self._tasks: dict[int, asyncio.Task] = {}
        self._cancelled: set[int] = set()

    def register(self, review_id: int, task: asyncio.Task) -> None:
        self._tasks[review_id] = task

    def unregister(self, review_id: int) -> None:
        self._tasks.pop(review_id, None)
        self._cancelled.discard(review_id)

    def cancel(self, review_id: int) -> bool:
        task = self._tasks.get(review_id)
        if task is None or task.done():
            return False
        self._cancelled.add(review_id)
        task.cancel()
        return True

    def was_cancelled(self, review_id: int) -> bool:
        return review_id in self._cancelled


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
        registry: ReviewRegistry | None = None,
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
        self._registry = registry if registry is not None else ReviewRegistry()

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
        review_id = review["id"]
        task = asyncio.create_task(
            self._process_review(review), name=f"mrwatch-review-{review_id}"
        )
        self._registry.register(review_id, task)
        try:
            await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            if not self._registry.was_cancelled(review_id):
                raise
            logger.info("worker %s: review %s cancelled (MR head moved)", self.worker_id, review_id)
        except Exception:
            logger.exception("worker %s: review %s crashed", self.worker_id, review_id)
            finish_review(
                self._conn, review_id, "error",
                error="internal worker error (see app.log)",
            )
        finally:
            self._registry.unregister(review_id)
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
                self._conn, review_id, "cancelled", error=superseded_error(exc.actual)
            )

    def _reconcile_head(self, review: sqlite3.Row, detail: dict) -> bool:
        current_sha = detail.get("sha")
        if not current_sha or current_sha == review["head_sha"]:
            return False
        review_id = review["id"]
        if detail.get("state") not in (None, "opened"):
            error = CLOSED_CANCEL_ERROR
        elif parse_draft(detail) is True:
            error = DRAFT_CANCEL_ERROR
        elif rebase_and_defer_review(
            self._conn, review_id, current_sha, self._config.review.push_debounce_sec
        ):
            logger.info(
                "review %s: MR head moved to %s before start, requeued",
                review_id, current_sha[:12],
            )
            return True
        else:
            error = superseded_error(current_sha)
        logger.info("review %s: %s", review_id, error)
        finish_review(self._conn, review_id, "cancelled", error=error)
        return True

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

    async def _collect_snippets(
        self, review_md: str, diff: str | None, workspace: Path
    ) -> str | None:
        try:
            snippets = await asyncio.to_thread(extract_snippets, review_md, diff, workspace)
        except Exception as exc:
            logger.warning("snippet extraction failed in %s: %s", workspace.name, exc)
            return None
        return dump_snippets(snippets)

    async def _discard_workspace(self, workspace: Path) -> None:
        await asyncio.shield(
            asyncio.to_thread(remove_workspace, workspace, self._paths.workspaces)
        )

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
        if self._reconcile_head(review, detail):
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
        except asyncio.CancelledError:
            await self._discard_workspace(workspace)
            raise
        except Exception as exc:
            await asyncio.to_thread(remove_workspace, workspace, self._paths.workspaces)
            logger.warning("review %s: clone failed: %s", review_id, exc)
            self._requeue_or_fail(review_id, review["attempt"], f"clone failed: {exc}")
            return

        diff_refs = detail.get("diff_refs") or {}
        snippets_json: str | None = None
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
                extra_instructions=review["extra_instructions"],
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
            if result.review_md:
                snippets_json = await self._collect_snippets(result.review_md, diff, workspace)
        except OpencodeError as exc:
            finish_review(self._conn, review_id, "error", error=f"opencode failed: {exc}")
            return
        finally:
            await self._discard_workspace(workspace)
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
                snippets_json=snippets_json,
            )


class QuestionWorker(Worker):
    async def process_one(self) -> bool:
        question = claim_next_question(self._conn)
        if question is None:
            return False
        question_id = question["id"]
        task = asyncio.create_task(
            self._process_question(question), name=f"mrwatch-question-{question_id}"
        )
        self._registry.register(question_id, task)
        try:
            await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            if not self._registry.was_cancelled(question_id):
                raise
            logger.info("question %s cancelled", question_id)
            finish_question(self._conn, question_id, "cancelled", error=QUESTION_CANCELLED_ERROR)
        except Exception:
            logger.exception("question %s crashed", question_id)
            finish_question(
                self._conn, question_id, "error",
                error="internal worker error (see app.log)",
            )
        finally:
            self._registry.unregister(question_id)
        return True

    def _fail(self, question_id: int, error: str) -> None:
        finish_question(self._conn, question_id, "error", error=error)

    async def _process_question(self, question: sqlite3.Row) -> None:
        question_id = question["id"]
        config = self._config
        if self._low_disk():
            self._fail(question_id, "low disk: <5GB free")
            return
        review = get_review(self._conn, question["review_id"])
        if review is None:
            self._fail(question_id, "review row missing")
            return
        mr = self._load_mr(review["mr_id"])
        if mr is None:
            self._fail(question_id, "mr row missing")
            return
        try:
            validate_agent(review["agent"], self._agents_dirs)
        except AgentError as exc:
            self._fail(question_id, f"agent rejected: {exc}")
            return
        fork = question["parent_session_id"] is None
        session_id = question["root_session_id"] if fork else question["parent_session_id"]
        if not is_valid_session_id(session_id):
            self._fail(question_id, "некорректный id сессии opencode")
            return

        workspace = (
            self._paths.workspaces
            / str(mr["gitlab_project_id"])
            / str(mr["iid"])
            / str(review["id"])
        )
        tmpdir = self._paths.workspaces.parent / "tmp"
        try:
            try:
                await clone_for_review(
                    workspace=workspace,
                    repo_url=self._repo_url(mr),
                    mr_iid=mr["iid"],
                    target_branch=None,
                    depth=config.review.clone_depth,
                    env=make_git_env(config.gitlab.pat_command, tmpdir),
                    tmpdir=tmpdir,
                    harden=self._clone_harden,
                    expected_sha=review["head_sha"],
                )
            except HeadMismatchError as exc:
                logger.warning("question %s: %s", question_id, exc)
                self._fail(question_id, QUESTION_HEAD_GONE_ERROR)
                return
            except Exception as exc:
                logger.warning("question %s: clone failed: %s", question_id, exc)
                self._fail(question_id, f"clone failed: {exc}")
                return
            project_cfg = next(
                (p for p in config.projects if p.gitlab_project_id == mr["gitlab_project_id"]),
                None,
            )
            oc = config.opencode
            try:
                result = await run_question(
                    env=RunnerEnv(opencode_bin=oc.bin),
                    workspace=workspace,
                    agent=review["agent"],
                    question=question["question"],
                    session_id=session_id,
                    fork=fork,
                    timeout_sec=config.review.question_timeout_sec,
                    log_path=self._paths.logs / f"question-{question_id}.log",
                    tmpdir=tmpdir,
                    model=(project_cfg.model if project_cfg and project_cfg.model else oc.model),
                    variant=(
                        project_cfg.variant if project_cfg and project_cfg.variant else oc.variant
                    ),
                )
            except OpencodeError as exc:
                self._fail(question_id, f"opencode failed: {exc}")
                return
        finally:
            await self._discard_workspace(workspace)
        if result.review_md is None:
            finish_question(
                self._conn, question_id, "error",
                session_id=result.session_id,
                exit_code=result.exit_code,
                error=result.error or "opencode produced no answer",
            )
            return
        finish_question(
            self._conn, question_id, "done",
            answer_md=result.review_md,
            session_id=result.session_id,
            exit_code=result.exit_code,
        )


def start_workers(
    count: int,
    conn: sqlite3.Connection,
    api: GitLabClient,
    config: Config,
    paths: ResolvedPaths,
    work_event: asyncio.Event,
    config_provider: Callable[[], Config] | None = None,
    registry: ReviewRegistry | None = None,
) -> list[asyncio.Task]:
    registry = registry if registry is not None else ReviewRegistry()
    workers = [
        Worker(
            i, conn, api, config, paths, work_event,
            config_provider=config_provider, registry=registry,
        )
        for i in range(count)
    ]
    return [asyncio.create_task(w.run(), name=f"mrwatch-worker-{i}") for i, w in enumerate(workers)]
