from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

MAX_REVIEW_MD_LENGTH = 1_000_000
TRUNCATION_MARKER = "...[truncated]"
SCHEMA_VERSION = 5

DERIVED_ROLES = frozenset({"reviewer", "assignee", "author"})
MENTIONED_ROLE = "mentioned"


SEVERITY_COLUMNS = ("crit_count", "major_count", "minor_count", "nit_count")


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class MrUpsertResult:
    mr_id: int
    inserted: bool
    head_sha_changed: bool
    state_changed: bool


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < 1:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS project (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            gitlab_project_id INTEGER NOT NULL UNIQUE,
            path_with_namespace TEXT NOT NULL DEFAULT '',
            agent TEXT NOT NULL,
            re_review_on_push INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS mr (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL REFERENCES project(id),
            iid INTEGER NOT NULL,
            title TEXT,
            author_username TEXT,
            source_branch TEXT,
            web_url TEXT,
            state TEXT,
            head_sha TEXT,
            first_seen_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(project_id, iid)
        );

        CREATE TABLE IF NOT EXISTS review (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mr_id INTEGER NOT NULL REFERENCES mr(id),
            "trigger" TEXT NOT NULL CHECK("trigger" IN ('new','push','manual')),
            head_sha TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued'
                CHECK(status IN ('queued','cloning','reviewing','done','error')),
            agent TEXT NOT NULL,
            session_id TEXT,
            review_md TEXT,
            exit_code INTEGER,
            error TEXT,
            attempt INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            UNIQUE(mr_id, head_sha, "trigger")
        );

        CREATE TABLE IF NOT EXISTS poll_state (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_review_status ON review(status);
        CREATE INDEX IF NOT EXISTS idx_mr_project_iid ON mr(project_id, iid);
        """
    )
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
    if version < 2:
        _run_migration(conn, 2, _migrate_to_v2)
    if version < 3:
        _run_migration(conn, 3, _migrate_to_v3)
    if version < 4:
        _run_migration(conn, 4, _migrate_to_v4)
    if version < 5:
        _run_migration(conn, 5, _migrate_to_v5)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def _run_migration(conn: sqlite3.Connection, target: int, migrate) -> None:
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        migrate(conn)
        conn.execute(f"PRAGMA user_version = {target}")
    except BaseException:
        conn.rollback()
        raise
    conn.commit()


def _add_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _migrate_to_v2(conn: sqlite3.Connection) -> None:
    for column in SEVERITY_COLUMNS:
        _add_column(conn, "review", column, "INTEGER NOT NULL DEFAULT 0")


def _migrate_to_v3(conn: sqlite3.Connection) -> None:
    _add_column(conn, "mr", "author_name", "TEXT")


def _migrate_to_v5(conn: sqlite3.Connection) -> None:
    _add_column(conn, "mr", "my_roles", "TEXT NOT NULL DEFAULT ''")


def _parse_roles(value: str | None) -> set[str]:
    return {role for role in (value or "").split(",") if role}


def _merge_roles(
    existing: str | None,
    derived_roles: frozenset[str] | set[str] | None,
    signal_roles: frozenset[str] | set[str] | None,
    state: str | None,
    derived_scope: frozenset[str] | set[str] = DERIVED_ROLES,
) -> str:
    roles = _parse_roles(existing)
    if derived_roles is not None:
        roles = (roles - set(derived_scope)) | set(derived_roles)
    roles |= set(signal_roles or ())
    if state not in (None, "opened", "locked"):
        roles.discard(MENTIONED_ROLE)
    return ",".join(sorted(roles))


SCAN_DONE_KEY = "mr_scan_done:{}"
MR_CURSOR_KEY = "mr_cursor:{}"
ROLES_BACKFILL_KEY = "roles_backfill_v5:{}"
LEGACY_CURSOR_KEY = "mr_cursor"


def _migrate_to_v4(conn: sqlite3.Connection) -> None:
    _add_column(conn, "review", "not_before", "TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_review_mr ON review(mr_id)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS todo_seen "
        "(todo_id INTEGER PRIMARY KEY, seen_at TEXT NOT NULL)"
    )
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if not {"poll_state", "project"} <= tables:
        return
    legacy = conn.execute(
        "SELECT value FROM poll_state WHERE key = ?", (LEGACY_CURSOR_KEY,)
    ).fetchone()
    if legacy is not None:
        for row in conn.execute("SELECT gitlab_project_id FROM project").fetchall():
            gitlab_pid = row[0]
            scanned = conn.execute(
                "SELECT 1 FROM poll_state WHERE key = ?", (SCAN_DONE_KEY.format(gitlab_pid),)
            ).fetchone()
            if scanned is not None:
                conn.execute(
                    "INSERT OR IGNORE INTO poll_state (key, value) VALUES (?, ?)",
                    (MR_CURSOR_KEY.format(gitlab_pid), legacy[0]),
                )
        conn.execute("DELETE FROM poll_state WHERE key = ?", (LEGACY_CURSOR_KEY,))


def update_mr_roles(
    conn: sqlite3.Connection,
    project_db_id: int,
    iid: int,
    derived_roles: frozenset[str] | set[str],
    derived_scope: frozenset[str] | set[str] = DERIVED_ROLES,
) -> None:
    row = conn.execute(
        "SELECT id, state, my_roles FROM mr WHERE project_id = ? AND iid = ?",
        (project_db_id, iid),
    ).fetchone()
    if row is None:
        return
    merged = _merge_roles(row["my_roles"], derived_roles, None, row["state"], derived_scope)
    conn.execute("UPDATE mr SET my_roles = ? WHERE id = ?", (merged, row["id"]))
    conn.commit()


def sync_projects(
    conn: sqlite3.Connection,
    projects: list[tuple[int, str, str, bool, bool]],
) -> dict[int, int]:
    mapping: dict[int, int] = {}
    listed = {p[0] for p in projects}
    if listed:
        placeholders = ",".join("?" for _ in listed)
        conn.execute(
            f"UPDATE project SET enabled = 0 WHERE gitlab_project_id NOT IN ({placeholders})",
            tuple(listed),
        )
    else:
        conn.execute("UPDATE project SET enabled = 0")
    for gitlab_project_id, path_with_namespace, agent, re_review_on_push, enabled in projects:
        row = conn.execute(
            "SELECT id, enabled FROM project WHERE gitlab_project_id = ?",
            (gitlab_project_id,),
        ).fetchone()
        if row is None:
            poll_state_delete(conn, SCAN_DONE_KEY.format(gitlab_project_id))
            poll_state_delete(conn, MR_CURSOR_KEY.format(gitlab_project_id))
            cursor = conn.execute(
                """
                INSERT INTO project
                    (gitlab_project_id, path_with_namespace, agent, re_review_on_push, enabled)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    gitlab_project_id,
                    path_with_namespace,
                    agent,
                    int(re_review_on_push),
                    int(enabled),
                ),
            )
            mapping[gitlab_project_id] = int(cursor.lastrowid)
        else:
            if enabled and not row["enabled"]:
                poll_state_delete(conn, SCAN_DONE_KEY.format(gitlab_project_id))
                poll_state_delete(conn, MR_CURSOR_KEY.format(gitlab_project_id))
            conn.execute(
                """
                UPDATE project
                SET path_with_namespace = ?, agent = ?, re_review_on_push = ?, enabled = ?
                WHERE id = ?
                """,
                (
                    path_with_namespace,
                    agent,
                    int(re_review_on_push),
                    int(enabled),
                    row["id"],
                ),
            )
            mapping[gitlab_project_id] = int(row["id"])
    conn.commit()
    return mapping


def upsert_mr(
    conn: sqlite3.Connection,
    project_db_id: int,
    iid: int,
    title: str | None,
    author_username: str | None,
    source_branch: str | None,
    web_url: str | None,
    state: str | None,
    head_sha: str | None,
    updated_at: str | None,
    author_name: str | None = None,
    derived_roles: frozenset[str] | set[str] | None = None,
    signal_roles: frozenset[str] | set[str] | None = None,
    derived_scope: frozenset[str] | set[str] = DERIVED_ROLES,
) -> MrUpsertResult:
    row = conn.execute(
        "SELECT id, head_sha, state, my_roles FROM mr WHERE project_id = ? AND iid = ?",
        (project_db_id, iid),
    ).fetchone()
    if row is None:
        cursor = conn.execute(
            """
            INSERT INTO mr
                (project_id, iid, title, author_username, author_name, source_branch,
                 web_url, state, head_sha, first_seen_at, updated_at, my_roles)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_db_id,
                iid,
                title,
                author_username,
                author_name,
                source_branch,
                web_url,
                state,
                head_sha,
                utcnow_iso(),
                updated_at or utcnow_iso(),
                _merge_roles(None, derived_roles, signal_roles, state),
            ),
        )
        conn.commit()
        return MrUpsertResult(
            mr_id=int(cursor.lastrowid),
            inserted=True,
            head_sha_changed=True,
            state_changed=True,
        )
    head_sha_changed = head_sha is not None and row["head_sha"] != head_sha
    state_changed = state is not None and row["state"] != state
    conn.execute(
        """
        UPDATE mr
        SET title = ?, author_username = ?, author_name = COALESCE(?, author_name),
            source_branch = ?, web_url = ?,
            state = COALESCE(?, state), head_sha = COALESCE(?, head_sha), updated_at = ?,
            my_roles = ?
        WHERE id = ?
        """,
        (
            title,
            author_username,
            author_name,
            source_branch,
            web_url,
            state,
            head_sha,
            updated_at or utcnow_iso(),
            _merge_roles(
                row["my_roles"], derived_roles, signal_roles, state or row["state"],
                derived_scope,
            ),
            row["id"],
        ),
    )
    conn.commit()
    return MrUpsertResult(
        mr_id=int(row["id"]),
        inserted=False,
        head_sha_changed=head_sha_changed,
        state_changed=state_changed,
    )


def create_review(
    conn: sqlite3.Connection,
    mr_id: int,
    trigger: str,
    head_sha: str,
    agent: str,
) -> int | None:
    cursor = conn.execute(
        """
        INSERT INTO review (mr_id, "trigger", head_sha, status, agent, created_at)
        VALUES (?, ?, ?, 'queued', ?, ?)
        ON CONFLICT(mr_id, head_sha, "trigger") DO NOTHING
        """,
        (mr_id, trigger, head_sha, agent, utcnow_iso()),
    )
    conn.commit()
    if cursor.rowcount == 0:
        return None
    return int(cursor.lastrowid)


def review_exists_for_head(conn: sqlite3.Connection, mr_id: int, head_sha: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM review WHERE mr_id = ? AND head_sha = ? LIMIT 1", (mr_id, head_sha)
    ).fetchone()
    return row is not None


def claim_next_review(conn: sqlite3.Connection) -> sqlite3.Row | None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            """
            SELECT review.id FROM review
            JOIN mr ON mr.id = review.mr_id
            JOIN project ON project.id = mr.project_id
            WHERE review.status = 'queued' AND project.enabled = 1
                AND (review.not_before IS NULL OR review.not_before <= ?)
            ORDER BY review.id LIMIT 1
            """,
            (utcnow_iso(),),
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        review_id = row["id"]
        conn.execute(
            'UPDATE review SET status = ?, started_at = ?, attempt = attempt + 1 WHERE id = ?',
            ("cloning", utcnow_iso(), review_id),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return conn.execute("SELECT * FROM review WHERE id = ?", (review_id,)).fetchone()


def finish_review(
    conn: sqlite3.Connection,
    review_id: int,
    status: str,
    review_md: str | None = None,
    session_id: str | None = None,
    exit_code: int | None = None,
    error: str | None = None,
    finished: bool = True,
    counts: tuple[int, int, int, int] = (0, 0, 0, 0),
) -> None:
    if review_md is not None and len(review_md) > MAX_REVIEW_MD_LENGTH:
        review_md = review_md[:MAX_REVIEW_MD_LENGTH] + TRUNCATION_MARKER
    conn.execute(
        """
        UPDATE review
        SET status = ?, review_md = ?, session_id = ?, exit_code = ?, error = ?,
            finished_at = ?, crit_count = ?, major_count = ?, minor_count = ?, nit_count = ?
        WHERE id = ?
        """,
        (
            status,
            review_md,
            session_id,
            exit_code,
            error,
            utcnow_iso() if finished else None,
            *counts,
            review_id,
        ),
    )
    conn.commit()


def retry_review(conn: sqlite3.Connection, review_id: int) -> bool:
    cursor = conn.execute(
        """
        UPDATE review
        SET status = 'queued', review_md = NULL, session_id = NULL, exit_code = NULL,
            error = NULL, started_at = NULL, finished_at = NULL,
            attempt = 0, not_before = NULL,
            crit_count = 0, major_count = 0, minor_count = 0, nit_count = 0
        WHERE id = ? AND status IN ('error', 'done')
        """,
        (review_id,),
    )
    conn.commit()
    return cursor.rowcount > 0


def rebase_review_head(conn: sqlite3.Connection, review_id: int, head_sha: str) -> bool:
    try:
        conn.execute("UPDATE review SET head_sha = ? WHERE id = ?", (head_sha, review_id))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        return False
    return True


def mark_reviewing(conn: sqlite3.Connection, review_id: int) -> None:
    conn.execute(
        "UPDATE review SET status = 'reviewing' WHERE id = ?",
        (review_id,),
    )
    conn.commit()


def requeue_review(conn: sqlite3.Connection, review_id: int, delay_sec: float = 0.0) -> None:
    not_before = None
    if delay_sec > 0:
        not_before = (
            (datetime.now(UTC) + timedelta(seconds=delay_sec))
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
    conn.execute(
        """
        UPDATE review
        SET status = 'queued', error = NULL, started_at = NULL, finished_at = NULL,
            not_before = ?
        WHERE id = ?
        """,
        (not_before, review_id),
    )
    conn.commit()


def request_review(
    conn: sqlite3.Connection,
    mr_id: int,
    trigger: str,
    head_sha: str,
    agent: str,
) -> int | None:
    review_id = create_review(conn, mr_id, trigger, head_sha, agent)
    if review_id is not None:
        return review_id
    row = conn.execute(
        'SELECT id, status FROM review WHERE mr_id = ? AND head_sha = ? AND "trigger" = ?',
        (mr_id, head_sha, trigger),
    ).fetchone()
    if row is not None and row["status"] in ("done", "error") and retry_review(conn, row["id"]):
        return int(row["id"])
    return None


def requeue_incomplete(conn: sqlite3.Connection) -> int:
    cursor = conn.execute(
        "UPDATE review SET status = 'queued' WHERE status IN ('queued', 'cloning', 'reviewing')"
    )
    conn.commit()
    return cursor.rowcount


def get_review(conn: sqlite3.Connection, review_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM review WHERE id = ?", (review_id,)).fetchone()


def list_reviews(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT review.*, mr.iid, mr.title, project.path_with_namespace
        FROM review
        JOIN mr ON mr.id = review.mr_id
        JOIN project ON project.id = mr.project_id
        ORDER BY review.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def list_mrs(conn: sqlite3.Connection, limit: int = 200) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT mr.*, project.path_with_namespace, project.gitlab_project_id,
            COALESCE(latest_done.crit_count, 0) AS crit_count,
            COALESCE(latest_done.major_count, 0) AS major_count,
            COALESCE(latest_done.minor_count, 0) AS minor_count,
            COALESCE(latest_done.nit_count, 0) AS nit_count,
            latest_done.id AS latest_done_review_id,
            latest_any.id AS latest_review_id,
            latest_any.status AS latest_review_status
        FROM mr
        JOIN project ON project.id = mr.project_id
        LEFT JOIN (
            SELECT r.mr_id, r.id, r.status, r.crit_count, r.major_count,
                   r.minor_count, r.nit_count
            FROM review r
            JOIN (
                SELECT mr_id, MAX(id) AS max_id FROM review
                GROUP BY mr_id
            ) last ON last.max_id = r.id
        ) latest_any ON latest_any.mr_id = mr.id
        LEFT JOIN (
            SELECT r.mr_id, r.id, r.crit_count, r.major_count, r.minor_count, r.nit_count
            FROM review r
            JOIN (
                SELECT mr_id, MAX(id) AS max_id FROM review
                WHERE status = 'done'
                GROUP BY mr_id
            ) last ON last.max_id = r.id
        ) latest_done ON latest_done.mr_id = mr.id
        WHERE mr.state IS NULL OR mr.state IN ('opened', 'locked')
        ORDER BY mr.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def poll_state_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM poll_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row is not None else None


def poll_state_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO poll_state (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )
    conn.commit()


def poll_state_delete(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM poll_state WHERE key = ?", (key,))
    conn.commit()


def todo_seen_ids(conn: sqlite3.Connection) -> set[int]:
    return {row[0] for row in conn.execute("SELECT todo_id FROM todo_seen")}


def todo_mark_seen(conn: sqlite3.Connection, todo_id: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO todo_seen (todo_id, seen_at) VALUES (?, ?)",
        (todo_id, utcnow_iso()),
    )
    conn.commit()


def backup(db_path: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    dest = backup_dir / f"mrwatch-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.db"
    source = sqlite3.connect(db_path)
    try:
        dest_conn = sqlite3.connect(dest)
        try:
            source.backup(dest_conn)
        finally:
            dest_conn.close()
    finally:
        source.close()
    dest.chmod(0o600)
    return dest
