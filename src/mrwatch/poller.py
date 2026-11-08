from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from mrwatch.config import Config
from mrwatch.db import (
    SCAN_DONE_KEY,
    create_review,
    poll_state_delete,
    poll_state_get,
    poll_state_set,
    upsert_mr,
)
from mrwatch.gitlab_client import GitLabClient

logger = logging.getLogger(__name__)

CURSOR_KEY = "mr_cursor"
CURSOR_OVERLAP_SEC = 120


def get_project_row(conn: sqlite3.Connection, project_db_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM project WHERE id = ?", (project_db_id,)).fetchone()


class Poller:
    def __init__(
        self,
        conn: sqlite3.Connection,
        api: GitLabClient,
        config: Config,
        project_ids: dict[int, int],
        queue: asyncio.Queue | None = None,
    ) -> None:
        self._conn = conn
        self._api = api
        self._config = config
        self._project_ids = project_ids
        self._queue = queue
        self._me: dict[str, Any] | None = None
        self._wake = asyncio.Event()

    async def run(self) -> None:
        while True:
            self._wake.clear()
            try:
                await self.tick()
                poll_state_set(self._conn, "poll_last_ok", datetime.now(UTC).isoformat())
                poll_state_delete(self._conn, "poll_last_error")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("poll tick failed")
                poll_state_set(self._conn, "poll_last_error", f"{type(exc).__name__}: {exc}")
            if self._wake.is_set():
                continue
            try:
                await asyncio.wait_for(
                    self._wake.wait(), timeout=self._config.gitlab.poll_interval_sec
                )
            except asyncio.TimeoutError:
                pass

    def wake(self) -> None:
        self._wake.set()

    def reload(self, config: Config, project_ids: dict[int, int]) -> None:
        self._config = config
        self._project_ids = project_ids

    async def tick(self) -> int:
        if self._me is None:
            self._me = await self._api.get_user()
        created = 0
        created += await self._process_todos()
        created += await self._full_scan()
        if poll_state_get(self._conn, CURSOR_KEY) is None:
            poll_state_set(
                self._conn, CURSOR_KEY,
                (datetime.now(UTC) - timedelta(seconds=CURSOR_OVERLAP_SEC)).isoformat(),
            )
        else:
            created += await self._sweep_merge_requests()
        if created and self._queue is not None:
            self._queue.put_nowait(created)
        return created

    def _agent_for(self, project_db_id: int) -> str:
        row = get_project_row(self._conn, project_db_id)
        if row is None:
            raise RuntimeError(f"project db row missing: {project_db_id}")
        return row["agent"]

    def _re_review_on_push(self, project_db_id: int) -> bool:
        row = get_project_row(self._conn, project_db_id)
        return bool(row["re_review_on_push"]) if row is not None else False

    async def _process_todos(self) -> int:
        created = 0
        for todo in await self._api.get_todos():
            target = todo.get("target") or {}
            if todo.get("target_type") != "MergeRequest":
                continue
            gitlab_pid = target.get("project_id")
            iid = target.get("iid")
            if gitlab_pid not in self._project_ids or iid is None:
                continue
            try:
                detail = await self._api.get_project_mr(gitlab_pid, iid)
            except Exception:
                logger.exception("failed to fetch MR %s/%s, todo kept", gitlab_pid, iid)
                continue
            result = upsert_mr(
                self._conn,
                project_db_id=self._project_ids[gitlab_pid],
                iid=detail["iid"],
                title=detail.get("title"),
                author_username=(detail.get("author") or {}).get("username"),
                author_name=(detail.get("author") or {}).get("name"),
                source_branch=detail.get("source_branch"),
                web_url=detail.get("web_url"),
                state=detail.get("state"),
                head_sha=detail.get("sha"),
                updated_at=detail.get("updated_at"),
            )
            action = todo.get("action_name")
            mentioned = action in ("mentioned", "directly_addressed")
            is_reviewer = self._username() in _reviewer_usernames(detail)
            if result.inserted or mentioned or is_reviewer:
                review_id = create_review(
                    self._conn,
                    result.mr_id,
                    "new",
                    detail.get("sha"),
                    self._agent_for(self._project_ids[gitlab_pid]),
                )
                if review_id is not None:
                    created += 1
            await self._api.mark_todo_done(todo["id"])
        return created

    async def _full_scan(self) -> int:
        created = 0
        max_updated: datetime | None = None
        username = self._username()
        for gitlab_pid, project_db_id in self._project_ids.items():
            if poll_state_get(self._conn, SCAN_DONE_KEY.format(gitlab_pid)) is not None:
                continue
            try:
                mrs = await self._api.get_project_merge_requests(gitlab_pid)
            except Exception:
                logger.exception("full scan failed for project %s", gitlab_pid)
                continue
            for mr in mrs:
                result = upsert_mr(
                    self._conn,
                    project_db_id=project_db_id,
                    iid=mr["iid"],
                    title=mr.get("title"),
                    author_username=(mr.get("author") or {}).get("username"),
                    author_name=(mr.get("author") or {}).get("name"),
                    source_branch=mr.get("source_branch"),
                    web_url=mr.get("web_url"),
                    state=mr.get("state"),
                    head_sha=mr.get("sha"),
                    updated_at=mr.get("updated_at"),
                )
                updated_at = _parse_iso(mr.get("updated_at"))
                if updated_at is not None and (max_updated is None or updated_at > max_updated):
                    max_updated = updated_at
                mine = mr.get("author", {}).get("username") == username or (
                    username in _reviewer_usernames(mr)
                )
                mentioned = False
                if not mine:
                    mentioned = await self._mentioned_in_mr(
                        gitlab_pid, mr["iid"], username, mr.get("description")
                    )
                if (mine or mentioned) and mr.get("state") in (None, "opened"):
                    review_id = create_review(
                        self._conn,
                        result.mr_id,
                        "new",
                        mr.get("sha"),
                        self._agent_for(project_db_id),
                    )
                    if review_id is not None:
                        created += 1
            poll_state_set(
                self._conn, SCAN_DONE_KEY.format(gitlab_pid),
                datetime.now(UTC).isoformat(),
            )
        if max_updated is not None:
            new_cursor = (max_updated - timedelta(seconds=CURSOR_OVERLAP_SEC)).isoformat()
            poll_state_set(self._conn, CURSOR_KEY, new_cursor)
        return created

    async def _mentioned_in_mr(
        self, gitlab_pid: int, iid: int, username: str, description: str | None
    ) -> bool:
        if description and f"@{username}" in description:
            return True
        try:
            notes = await self._api.get_mr_notes(gitlab_pid, iid)
        except Exception:
            logger.exception("notes fetch failed for %s/%s", gitlab_pid, iid)
            return False
        return any(f"@{username}" in (note.get("body") or "") for note in notes)

    async def _sweep_merge_requests(self) -> int:
        cursor = poll_state_get(self._conn, CURSOR_KEY)
        if cursor is None:
            return 0
        mrs = await self._api.get_merge_requests(cursor)
        created = 0
        max_updated: datetime | None = None
        for mr in mrs:
            gitlab_pid = mr.get("project_id")
            if gitlab_pid not in self._project_ids:
                continue
            result = upsert_mr(
                self._conn,
                project_db_id=self._project_ids[gitlab_pid],
                iid=mr["iid"],
                title=mr.get("title"),
                author_username=(mr.get("author") or {}).get("username"),
                author_name=(mr.get("author") or {}).get("name"),
                source_branch=mr.get("source_branch"),
                web_url=mr.get("web_url"),
                state=mr.get("state"),
                head_sha=mr.get("sha"),
                updated_at=mr.get("updated_at"),
            )
            updated_at = _parse_iso(mr.get("updated_at"))
            if updated_at is not None and (max_updated is None or updated_at > max_updated):
                max_updated = updated_at
            mine = mr.get("author", {}).get("username") == self._username() or (
                self._username() in _reviewer_usernames(mr)
            )
            state = mr.get("state")
            if result.inserted and mine and state in (None, "opened"):
                review_id = create_review(
                    self._conn,
                    result.mr_id,
                    "new",
                    mr.get("sha"),
                    self._agent_for(self._project_ids[gitlab_pid]),
                )
                if review_id is not None:
                    created += 1
            elif (
                not result.inserted
                and result.head_sha_changed
                and self._re_review_on_push(self._project_ids[gitlab_pid])
                and mine
            ):
                review_id = create_review(
                    self._conn,
                    result.mr_id,
                    "push",
                    mr.get("sha"),
                    self._agent_for(self._project_ids[gitlab_pid]),
                )
                if review_id is not None:
                    created += 1
        if max_updated is not None:
            new_cursor = (max_updated - timedelta(seconds=CURSOR_OVERLAP_SEC)).isoformat()
            poll_state_set(self._conn, CURSOR_KEY, new_cursor)
        return created

    def _username(self) -> str:
        if self._me is None:
            raise RuntimeError("poller user not loaded")
        return self._me["username"]


def _reviewer_usernames(mr: dict[str, Any]) -> set[str]:
    return {r.get("username") for r in (mr.get("reviewers") or []) if r.get("username")}


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
