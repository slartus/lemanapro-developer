from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import unquote, urlsplit

from lpdev.config import Config
from lpdev.db import (
    MENTIONED_ROLE,
    MR_CURSOR_KEY,
    ROLES_BACKFILL_KEY,
    SCAN_DONE_KEY,
    MrUpsertResult,
    poll_state_delete,
    poll_state_get,
    poll_state_set,
    set_mention_url,
    todo_mark_seen,
    todo_seen_ids,
    update_mr_roles,
    upsert_mr,
)
from lpdev.gitlab_client import GitLabClient, parse_draft

logger = logging.getLogger(__name__)

CURSOR_OVERLAP_SEC = 120
MIN_REFRESH_GAP_SEC = 60.0
MENTION_ACTIONS = ("mentioned", "directly_addressed")


def get_project_row(conn: sqlite3.Connection, project_db_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM project WHERE id = ?", (project_db_id,)).fetchone()


def _author(mr: dict[str, Any]) -> dict[str, Any]:
    return mr.get("author") or {}


class Poller:
    def __init__(
        self,
        conn: sqlite3.Connection,
        api: GitLabClient,
        config: Config,
        project_ids: dict[int, int],
        on_mr_updated: Callable[[int, int], None] | None = None,
    ) -> None:
        self._conn = conn
        self._api = api
        self._config = config
        self._project_ids = project_ids
        self._me: dict[str, Any] | None = None
        self._refresh_task: asyncio.Task | None = None
        self._last_refresh: float | None = None
        self._on_mr_updated = on_mr_updated

    def request_refresh(self, force: bool = False) -> bool:
        """Schedule one poll tick in the background (on-demand mode).

        Returns False when a refresh is already running or the minimal
        gap between refreshes has not elapsed yet (unless force=True).
        """
        if self._refresh_task is not None and not self._refresh_task.done():
            return False
        loop = asyncio.get_running_loop()
        if not force and self._last_refresh is not None:
            if loop.time() - self._last_refresh < MIN_REFRESH_GAP_SEC:
                return False
        self._refresh_task = loop.create_task(self._refresh_job())
        return True

    def is_refreshing(self) -> bool:
        return self._refresh_task is not None and not self._refresh_task.done()

    async def refresh_now(self) -> None:
        if self._refresh_task is not None and not self._refresh_task.done():
            await self._refresh_task
            return
        self._refresh_task = asyncio.get_running_loop().create_task(self._refresh_job())
        await self._refresh_task

    async def _refresh_job(self) -> None:
        try:
            await self.tick()
            poll_state_set(self._conn, "poll_last_ok", datetime.now(UTC).isoformat())
            poll_state_delete(self._conn, "poll_last_error")
        except Exception as exc:
            logger.exception("poll tick failed")
            try:
                poll_state_set(self._conn, "poll_last_error", f"{type(exc).__name__}: {exc}")
            except Exception:
                logger.exception("failed to record poll error")
        finally:
            self._last_refresh = asyncio.get_running_loop().time()
            self._refresh_task = None

    def reload(self, config: Config, project_ids: dict[int, int]) -> None:
        self._config = config
        self._project_ids = project_ids

    async def tick(self) -> int:
        """Run one poll cycle. Returns the number of auto-started reviews (always 0)."""
        if self._me is None:
            self._me = await self._api.get_user()
        await self._backfill_roles()
        await self._process_todos()
        scanned: set[int] = set()
        await self._full_scan(scanned)
        error = await self._sweep_merge_requests(scanned)
        if error is not None:
            raise error
        return 0

    def _upsert(
        self,
        project_db_id: int,
        mr: dict[str, Any],
        signal_roles: frozenset[str] = frozenset(),
    ) -> MrUpsertResult | None:
        head_sha = mr.get("sha")
        if not head_sha:
            logger.warning("MR %s in project %s skipped: no head sha", mr.get("iid"), project_db_id)
            return None
        if self._is_older_than_stored(project_db_id, mr):
            logger.info("MR %s in project %s skipped: stale data", mr.get("iid"), project_db_id)
            return None
        author = _author(mr)
        return upsert_mr(
            self._conn,
            project_db_id=project_db_id,
            iid=mr["iid"],
            title=mr.get("title"),
            author_username=author.get("username"),
            author_name=author.get("name"),
            source_branch=mr.get("source_branch"),
            web_url=mr.get("web_url"),
            state=mr.get("state"),
            head_sha=head_sha,
            updated_at=mr.get("updated_at"),
            derived_roles=derive_roles(mr, self._username()),
            signal_roles=signal_roles,
            derived_scope=derived_scope(mr),
            is_draft=parse_draft(mr),
        )

    async def _backfill_roles(self) -> None:
        username = self._username()
        for gitlab_pid, project_db_id in self._project_ids.items():
            key = ROLES_BACKFILL_KEY.format(gitlab_pid)
            if poll_state_get(self._conn, key) is not None:
                continue
            if poll_state_get(self._conn, SCAN_DONE_KEY.format(gitlab_pid)) is None:
                poll_state_set(self._conn, key, datetime.now(UTC).isoformat())
                continue
            try:
                mrs = await self._api.get_project_merge_requests(gitlab_pid)
            except Exception:
                logger.exception("roles backfill failed for project %s", gitlab_pid)
                continue
            for mr in mrs:
                if mr.get("iid") is None:
                    continue
                roles = derive_roles(mr, username)
                if _has_mention(mr.get("description"), username):
                    roles = roles | {MENTIONED_ROLE}
                update_mr_roles(
                    self._conn, project_db_id, mr["iid"], roles, derived_scope(mr),
                    parse_draft(mr),
                )
            poll_state_set(self._conn, key, datetime.now(UTC).isoformat())

    async def _process_todos(self) -> None:
        seen = todo_seen_ids(self._conn)
        todos = await self._api.get_todos()
        for todo in sorted(todos, key=_todo_sort_key):
            todo_id = todo.get("id")
            if not _is_todo_id(todo_id) or todo_id in seen:
                continue
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
            try:
                if self._is_older_than_stored(self._project_ids[gitlab_pid], detail):
                    logger.info("todo %s kept: MR %s/%s detail is older than stored", todo_id, gitlab_pid, iid)
                    continue
                self._handle_todo(todo, detail, gitlab_pid)
            except Exception:
                logger.exception("todo %s (MR %s/%s) failed", todo_id, gitlab_pid, iid)
                continue
            todo_mark_seen(self._conn, todo_id)

    def _handle_todo(self, todo: dict[str, Any], detail: dict[str, Any], gitlab_pid: int) -> None:
        project_db_id = self._project_ids[gitlab_pid]
        result = self._upsert(project_db_id, detail, todo_signal_roles(todo))
        if result is None:
            return
        if detail.get("state") != "opened":
            return
        mentioned = todo.get("action_name") in MENTION_ACTIONS
        if mentioned:
            try:
                mention_url = validated_mention_url(
                    todo.get("target_url"), self._config.gitlab.url, detail.get("web_url")
                )
                if mention_url and not _is_older_mention(self._conn, result.mr_id, mention_url):
                    set_mention_url(self._conn, result.mr_id, mention_url)
            except Exception:
                logger.exception("failed to save mention url for todo %s", todo.get("id"))

    async def _full_scan(self, scanned: set[int]) -> None:
        username = self._username()
        for gitlab_pid, project_db_id in self._project_ids.items():
            if poll_state_get(self._conn, SCAN_DONE_KEY.format(gitlab_pid)) is not None:
                continue
            scan_started = datetime.now(UTC)
            try:
                mrs = await self._api.get_project_merge_requests(gitlab_pid)
            except Exception:
                logger.exception("full scan failed for project %s", gitlab_pid)
                continue
            for mr in mrs:
                try:
                    await self._scan_one(gitlab_pid, project_db_id, mr, username)
                except Exception:
                    logger.exception("full scan: MR %s/%s failed", gitlab_pid, mr.get("iid"))
            self._advance_cursor(gitlab_pid, mrs, scan_started)
            poll_state_set(
                self._conn, SCAN_DONE_KEY.format(gitlab_pid),
                datetime.now(UTC).isoformat(),
            )
            scanned.add(gitlab_pid)

    async def _scan_one(
        self, gitlab_pid: int, project_db_id: int, mr: dict[str, Any], username: str
    ) -> None:
        mine = _author(mr).get("username") == username or username in _reviewer_usernames(mr)
        mentioned = await self._mentioned_in_mr(
            gitlab_pid, mr["iid"], username, mr.get("description"), fetch_notes=not mine
        )
        self._upsert(
            project_db_id, mr, frozenset({MENTIONED_ROLE}) if mentioned else frozenset()
        )

    def _advance_cursor(
        self,
        gitlab_pid: int,
        mrs: list[dict[str, Any]],
        scan_started: datetime | None = None,
    ) -> None:
        stamps = [t for t in (_parse_iso(mr.get("updated_at")) for mr in mrs) if t is not None]
        if stamps:
            base = max(stamps)
        elif scan_started is not None:
            base = scan_started
        else:
            return
        new_cursor = base - timedelta(seconds=CURSOR_OVERLAP_SEC)
        key = MR_CURSOR_KEY.format(gitlab_pid)
        old_cursor = _parse_iso(poll_state_get(self._conn, key))
        if old_cursor is not None and old_cursor >= new_cursor:
            return
        poll_state_set(self._conn, key, new_cursor.isoformat())

    async def _mentioned_in_mr(
        self,
        gitlab_pid: int,
        iid: int,
        username: str,
        description: str | None,
        fetch_notes: bool = True,
    ) -> bool:
        if _has_mention(description, username):
            return True
        if not fetch_notes:
            return False
        try:
            notes = await self._api.get_mr_notes(gitlab_pid, iid)
        except Exception:
            logger.exception("notes fetch failed for %s/%s", gitlab_pid, iid)
            return False
        return any(_has_mention(note.get("body"), username) for note in notes)

    async def _sweep_merge_requests(self, scanned: set[int]) -> Exception | None:
        pending: list[tuple[int, int, str]] = []
        for gitlab_pid, project_db_id in self._project_ids.items():
            if gitlab_pid in scanned:
                continue
            key = MR_CURSOR_KEY.format(gitlab_pid)
            cursor = poll_state_get(self._conn, key)
            if cursor is None:
                poll_state_set(
                    self._conn, key,
                    (datetime.now(UTC) - timedelta(seconds=CURSOR_OVERLAP_SEC)).isoformat(),
                )
                continue
            pending.append((gitlab_pid, project_db_id, cursor))
        if not pending:
            return None
        group_path = self._common_group(pending)
        error: Exception | None = None
        if group_path is not None:
            try:
                mrs = await self._api.get_group_merge_requests(
                    group_path, updated_after=min(cursor for _, _, cursor in pending), state=None
                )
            except Exception:
                logger.exception("group sweep failed for %s, falling back per project", group_path)
            else:
                for gitlab_pid, project_db_id, cursor in pending:
                    project_mrs = [mr for mr in mrs if mr.get("project_id") == gitlab_pid]
                    self._sweep_project(gitlab_pid, project_db_id, project_mrs)
                return None
        for gitlab_pid, project_db_id, cursor in pending:
            try:
                mrs = await self._api.get_project_merge_requests(
                    gitlab_pid, updated_after=cursor, state=None
                )
            except Exception as exc:
                logger.exception("sweep failed for project %s", gitlab_pid)
                error = exc
                continue
            self._sweep_project(gitlab_pid, project_db_id, mrs)
        return error

    def _sweep_project(
        self, gitlab_pid: int, project_db_id: int, mrs: list[dict[str, Any]]
    ) -> None:
        for mr in mrs:
            try:
                updated = self._is_update_of_known(project_db_id, mr)
                self._sweep_one(project_db_id, mr)
                if updated and self._on_mr_updated is not None:
                    self._on_mr_updated(gitlab_pid, mr["iid"])
            except Exception:
                logger.exception("sweep: MR %s/%s failed", gitlab_pid, mr.get("iid"))
        self._advance_cursor(gitlab_pid, mrs)

    def _common_group(self, pending: list[tuple[int, int, str]]) -> str | None:
        """Longest common group path for 2+ pending projects, or None."""
        if len(pending) < 2:
            return None
        paths: list[str] = []
        for _, project_db_id, _ in pending:
            row = get_project_row(self._conn, project_db_id)
            if row is None or not row["path_with_namespace"]:
                return None
            paths.append(row["path_with_namespace"])
        segments = [path.split("/") for path in paths]
        common: list[str] = []
        for group in zip(*segments):
            if len(set(group)) != 1:
                break
            common.append(group[0])
        if len(common) >= len(segments[0]):
            common = common[:-1]
        return "/".join(common) if common else None

    def _is_update_of_known(self, project_db_id: int, mr: dict[str, Any]) -> bool:
        pair = self._stored_and_incoming(project_db_id, mr)
        if pair is None:
            return False
        stored, incoming = pair
        return stored is not None and incoming is not None and incoming > stored

    def _stored_and_incoming(
        self, project_db_id: int, mr: dict[str, Any]
    ) -> tuple[datetime | None, datetime | None] | None:
        row = self._conn.execute(
            "SELECT updated_at FROM mr WHERE project_id = ? AND iid = ?", (project_db_id, mr["iid"])
        ).fetchone()
        if row is None:
            return None
        return _parse_iso(row["updated_at"]), _parse_iso(mr.get("updated_at"))

    def _is_newer_than_stored(self, project_db_id: int, mr: dict[str, Any]) -> bool:
        pair = self._stored_and_incoming(project_db_id, mr)
        if pair is None:
            return True
        stored, incoming = pair
        return incoming is not None and (stored is None or incoming > stored)

    def _may_insert(self, project_db_id: int, mr: dict[str, Any]) -> bool:
        if mr.get("state") in (None, "opened"):
            return True
        return self._stored_and_incoming(project_db_id, mr) is not None

    def _is_older_than_stored(self, project_db_id: int, mr: dict[str, Any]) -> bool:
        pair = self._stored_and_incoming(project_db_id, mr)
        if pair is None:
            return False
        stored, incoming = pair
        return stored is not None and incoming is not None and incoming < stored

    def ingest(self, mrs: list[Any]) -> int:
        if self._me is None:
            return 0
        for mr in mrs:
            if not isinstance(mr, dict) or not isinstance(mr.get("iid"), int) or not isinstance(mr.get("sha"), str):
                continue
            gitlab_pid = mr.get("project_id")
            project_db_id = self._project_ids.get(gitlab_pid)
            if project_db_id is None or poll_state_get(self._conn, SCAN_DONE_KEY.format(gitlab_pid)) is None:
                continue
            try:
                if self._is_newer_than_stored(project_db_id, mr) and self._may_insert(project_db_id, mr):
                    self._sweep_one(project_db_id, mr)
            except Exception:
                logger.exception("ingest: MR %s/%s failed", gitlab_pid, mr.get("iid"))
        return 0

    def _sweep_one(self, project_db_id: int, mr: dict[str, Any]) -> None:
        self._upsert(project_db_id, mr)

    def _username(self) -> str:
        if self._me is None:
            raise RuntimeError("poller user not loaded")
        return self._me["username"]


def _assignee_usernames(mr: dict[str, Any]) -> set[str]:
    assignees = list(mr.get("assignees") or [])
    if mr.get("assignee"):
        assignees.append(mr["assignee"])
    return {a.get("username") for a in assignees if a.get("username")}


def _has_mention(text: str | None, username: str) -> bool:
    if not text:
        return False
    return re.search(
        rf"(?<![\w.@])@{re.escape(username)}(?![\w]|[.-]\w)", text, re.IGNORECASE
    ) is not None


def derived_scope(mr: dict[str, Any]) -> frozenset[str]:
    scope = {"author"}
    if "reviewers" in mr:
        scope.add("reviewer")
    if "assignees" in mr or "assignee" in mr:
        scope.add("assignee")
    return frozenset(scope)


def derive_roles(mr: dict[str, Any], username: str) -> frozenset[str]:
    roles: set[str] = set()
    if _author(mr).get("username") == username:
        roles.add("author")
    if username in _reviewer_usernames(mr):
        roles.add("reviewer")
    if username in _assignee_usernames(mr):
        roles.add("assignee")
    return frozenset(roles)


def _is_todo_id(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _todo_sort_key(todo: dict[str, Any]) -> int:
    todo_id = todo.get("id")
    if _is_todo_id(todo_id):
        return todo_id
    return 0


_NOTE_RE = re.compile(r"#note_(\d+)$")


def _note_number(url: str | None) -> int | None:
    match = _NOTE_RE.search(url or "")
    return int(match.group(1)) if match else None


def _is_older_mention(conn: sqlite3.Connection, mr_id: int, new_url: str) -> bool:
    row = conn.execute("SELECT mention_url FROM mr WHERE id = ?", (mr_id,)).fetchone()
    current = _note_number(row[0] if row else None)
    new = _note_number(new_url)
    return current is not None and new is not None and new < current


_DEFAULT_PORTS = {"http": 80, "https": 443}


def _origin(parts) -> tuple[str, str, int] | None:
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        return None
    try:
        port = parts.port
        host = parts.hostname
    except ValueError:
        return None
    if not host:
        return None
    try:
        host = host.lower().encode("idna").decode("ascii")
    except UnicodeError:
        return None
    if port is None:
        port = _DEFAULT_PORTS[scheme]
    elif port == 0:
        return None
    return scheme, host, port


def _safe_urlsplit(url: str):
    try:
        return urlsplit(url)
    except ValueError:
        return None


def validated_mention_url(url: Any, gitlab_url: Any, mr_web_url: Any) -> str:
    if not all(isinstance(v, str) and v for v in (url, gitlab_url, mr_web_url)):
        return ""
    if "\\" in url or "%5c" in url.lower():
        return ""
    parts = _safe_urlsplit(url)
    base = _safe_urlsplit(gitlab_url)
    mr_parts = _safe_urlsplit(mr_web_url)
    if parts is None or base is None or mr_parts is None:
        return ""
    if parts.username or parts.password:
        return ""
    origin = _origin(parts)
    if origin is None or origin != _origin(base) or origin != _origin(mr_parts):
        return ""
    base_path = base.path.rstrip("/")
    mr_path = mr_parts.path.rstrip("/")
    if not mr_path or ".." in unquote(parts.path).split("/"):
        return ""
    if base_path and not mr_path.startswith(base_path + "/"):
        return ""
    if parts.path != mr_path and not parts.path.startswith(mr_path + "/"):
        return ""
    return url


def todo_signal_roles(todo: dict[str, Any]) -> frozenset[str]:
    action = todo.get("action_name")
    if action in MENTION_ACTIONS:
        return frozenset({MENTIONED_ROLE})
    return frozenset()


def _reviewer_usernames(mr: dict[str, Any]) -> set[str]:
    return {r.get("username") for r in (mr.get("reviewers") or []) if r.get("username")}


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
