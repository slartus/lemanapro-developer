from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from lpdev.opencode_runner import MAX_VERDICT_INPUT_CHARS, parse_verdict

MAX_REVIEW_MD_LENGTH = MAX_VERDICT_INPUT_CHARS
TRUNCATION_MARKER = "...[truncated]"
SCHEMA_VERSION = 14

DERIVED_ROLES = frozenset({"reviewer", "assignee", "author"})
MENTIONED_ROLE = "mentioned"
READY_REVIEW_ROLES = frozenset({"reviewer", "author", MENTIONED_ROLE})


DRAFT_CANCEL_ERROR = "отменено: MR переведён в draft"

MAX_QUESTIONS_PER_REVIEW = 50

SEVERITY_COLUMNS = ("crit_count", "major_count", "minor_count", "nit_count")


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def deferred_iso(delay_sec: float) -> str | None:
    if delay_sec <= 0:
        return None
    return (
        (datetime.now(UTC) + timedelta(seconds=delay_sec))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def superseded_error(new_sha: str) -> str:
    return f"отменено: в MR новый коммит {new_sha[:12]}"


@dataclass(frozen=True)
class MrUpsertResult:
    mr_id: int
    inserted: bool
    head_sha_changed: bool
    became_ready: bool = False
    became_draft: bool = False


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
                CHECK(status IN ('queued','cloning','reviewing','done','error','cancelled')),
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
    if version < 6:
        _run_migration(conn, 6, _migrate_to_v6)
    if version < 7:
        _run_migration(conn, 7, _migrate_to_v7)
    if version < 8:
        _run_migration(conn, 8, _migrate_to_v8)
    if version < 9:
        _run_migration(conn, 9, _migrate_to_v9)
    if version < 10:
        _run_migration(conn, 10, _migrate_to_v10)
    if version < 11:
        _run_migration(conn, 11, _migrate_to_v11)
    if version < 12:
        _run_migration(conn, 12, _migrate_to_v12)
    if version < 13:
        _run_migration(conn, 13, _migrate_to_v13)
    if version < 14:
        _run_migration(conn, 14, _migrate_to_v14)
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


def _migrate_to_v6(conn: sqlite3.Connection) -> None:
    _add_column(conn, "mr", "is_draft", "INTEGER NOT NULL DEFAULT 0")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "poll_state" in tables:
        conn.execute("DELETE FROM poll_state WHERE key LIKE 'roles_backfill_v5:%'")


def _migrate_to_v8(conn: sqlite3.Connection) -> None:
    _add_column(conn, "mr", "mention_url", "TEXT")


def _migrate_to_v7(conn: sqlite3.Connection) -> None:
    _add_column(conn, "review", "verdict", "TEXT")
    _backfill_verdicts(conn)


VERDICT_BACKFILL_BATCH = 200


def _backfill_verdicts(conn: sqlite3.Connection) -> None:
    last_id = 0
    while True:
        rows = conn.execute(
            "SELECT id, review_md FROM review WHERE status = 'done' AND verdict IS NULL AND id > ? "
            "ORDER BY id LIMIT ?",
            (last_id, VERDICT_BACKFILL_BATCH),
        ).fetchall()
        if not rows:
            return
        for row in rows:
            verdict = parse_verdict(row[1])
            if verdict is not None:
                conn.execute("UPDATE review SET verdict = ? WHERE id = ?", (verdict, row[0]))
        last_id = rows[-1][0]


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
    is_draft: bool | None = None,
) -> None:
    row = conn.execute(
        "SELECT id, state, my_roles FROM mr WHERE project_id = ? AND iid = ?",
        (project_db_id, iid),
    ).fetchone()
    if row is None:
        return
    merged = _merge_roles(row["my_roles"], derived_roles, None, row["state"], derived_scope)
    conn.execute(
        "UPDATE mr SET my_roles = ?, is_draft = COALESCE(?, is_draft) WHERE id = ?",
        (merged, None if is_draft is None else int(is_draft), row["id"]),
    )
    conn.commit()


def set_mention_url(conn: sqlite3.Connection, mr_id: int, url: str) -> None:
    conn.execute("UPDATE mr SET mention_url = ? WHERE id = ?", (url, mr_id))
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
    is_draft: bool | None = None,
) -> MrUpsertResult:
    row = conn.execute(
        "SELECT id, head_sha, state, my_roles, is_draft FROM mr WHERE project_id = ? AND iid = ?",
        (project_db_id, iid),
    ).fetchone()
    if row is None:
        cursor = conn.execute(
            """
            INSERT INTO mr
                (project_id, iid, title, author_username, author_name, source_branch,
                 web_url, state, head_sha, first_seen_at, updated_at, my_roles, is_draft)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                int(bool(is_draft)),
            ),
        )
        conn.commit()
        return MrUpsertResult(
            mr_id=int(cursor.lastrowid),
            inserted=True,
            head_sha_changed=True,
        )
    head_sha_changed = head_sha is not None and row["head_sha"] != head_sha
    was_draft = bool(row["is_draft"])
    conn.execute(
        """
        UPDATE mr
        SET title = ?, author_username = ?, author_name = COALESCE(?, author_name),
            source_branch = ?, web_url = ?,
            state = COALESCE(?, state), head_sha = COALESCE(?, head_sha), updated_at = ?,
            my_roles = ?, is_draft = COALESCE(?, is_draft)
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
            None if is_draft is None else int(is_draft),
            row["id"],
        ),
    )
    conn.commit()
    return MrUpsertResult(
        mr_id=int(row["id"]),
        inserted=False,
        head_sha_changed=head_sha_changed,
        became_ready=was_draft and is_draft is False,
        became_draft=not was_draft and is_draft is True,
    )


def create_review(
    conn: sqlite3.Connection,
    mr_id: int,
    trigger: str,
    head_sha: str,
    agent: str,
    delay_sec: float = 0.0,
) -> int | None:
    cursor = conn.execute(
        """
        INSERT INTO review (mr_id, "trigger", head_sha, status, agent, created_at, not_before)
        VALUES (?, ?, ?, 'queued', ?, ?, ?)
        ON CONFLICT(mr_id, head_sha, "trigger") DO NOTHING
        """,
        (mr_id, trigger, head_sha, agent, utcnow_iso(), deferred_iso(delay_sec)),
    )
    conn.commit()
    if cursor.rowcount == 0:
        return None
    return int(cursor.lastrowid)


def mr_wants_review(conn: sqlite3.Connection, mr_id: int) -> bool:
    row = conn.execute("SELECT my_roles FROM mr WHERE id = ?", (mr_id,)).fetchone()
    if row is None:
        return False
    if _parse_roles(row["my_roles"]) & READY_REVIEW_ROLES:
        return True
    return conn.execute(
        "SELECT 1 FROM review WHERE mr_id = ? LIMIT 1", (mr_id,)
    ).fetchone() is not None


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


def _migrate_to_v9(conn: sqlite3.Connection) -> None:
    _add_column(conn, "review", "snippets_json", "TEXT")


REVIEW_V10_COLUMNS = (
    'id, mr_id, "trigger", head_sha, status, agent, session_id, review_md, exit_code, error, '
    "attempt, created_at, started_at, finished_at, crit_count, major_count, minor_count, "
    "nit_count, not_before, verdict, snippets_json"
)


def _migrate_to_v10(conn: sqlite3.Connection) -> None:
    seq_row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'review'").fetchone()
    conn.execute("DROP TABLE IF EXISTS review_new")
    conn.execute(
        """
        CREATE TABLE review_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mr_id INTEGER NOT NULL REFERENCES mr(id),
            "trigger" TEXT NOT NULL CHECK("trigger" IN ('new','push','manual')),
            head_sha TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued'
                CHECK(status IN ('queued','cloning','reviewing','done','error','cancelled')),
            agent TEXT NOT NULL,
            session_id TEXT,
            review_md TEXT,
            exit_code INTEGER,
            error TEXT,
            attempt INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            crit_count INTEGER NOT NULL DEFAULT 0,
            major_count INTEGER NOT NULL DEFAULT 0,
            minor_count INTEGER NOT NULL DEFAULT 0,
            nit_count INTEGER NOT NULL DEFAULT 0,
            not_before TEXT,
            verdict TEXT,
            snippets_json TEXT,
            UNIQUE(mr_id, head_sha, "trigger")
        )
        """
    )
    conn.execute(
        f"INSERT INTO review_new ({REVIEW_V10_COLUMNS}) SELECT {REVIEW_V10_COLUMNS} FROM review"
    )
    conn.execute("DROP TABLE review")
    conn.execute("ALTER TABLE review_new RENAME TO review")
    if seq_row is not None:
        conn.execute(
            "UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = 'review'", (seq_row[0],)
        )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_review_status ON review(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_review_mr ON review(mr_id)")


def _migrate_to_v11(conn: sqlite3.Connection) -> None:
    _add_column(conn, "review", "extra_instructions", "TEXT")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS review_question (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            review_id INTEGER NOT NULL,
            question TEXT NOT NULL,
            answer_md TEXT,
            status TEXT NOT NULL DEFAULT 'queued'
                CHECK(status IN ('queued','running','done','error','cancelled')),
            root_session_id TEXT,
            parent_session_id TEXT,
            session_id TEXT,
            exit_code INTEGER,
            error TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_review_question_review ON review_question(review_id, id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_review_question_status ON review_question(status)"
    )


def _migrate_to_v12(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS radar_snapshot (
            id INTEGER PRIMARY KEY CHECK(id = 1),
            version INTEGER NOT NULL,
            fetched_at TEXT,
            failed_at TEXT,
            error TEXT,
            dashboard_json TEXT,
            thread_cache_json TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ui_state (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )


def _migrate_to_v13(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS timesheet_cell (
            issue_key TEXT NOT NULL,
            day TEXT NOT NULL,
            minutes INTEGER NOT NULL CHECK(minutes >= 0),
            updated_at TEXT NOT NULL,
            PRIMARY KEY(issue_key, day)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS timesheet_task (
            issue_key TEXT NOT NULL,
            week_start TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(issue_key, week_start)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS worklog_submission (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            issue_key TEXT NOT NULL,
            day TEXT NOT NULL,
            minutes INTEGER NOT NULL CHECK(minutes > 0),
            status TEXT NOT NULL CHECK(status IN ('pending','done','failed','unknown')),
            tracker_id TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_worklog_submission_day ON worklog_submission(day, issue_key)"
    )


def _migrate_to_v14(conn: sqlite3.Connection) -> None:
    _add_column(conn, "worklog_submission", "reconcile_attempts", "INTEGER NOT NULL DEFAULT 0")


def finish_review(
    conn: sqlite3.Connection,
    review_id: int,
    status: str,
    review_md: str | None = None,
    session_id: str | None = None,
    exit_code: int | None = None,
    error: str | None = None,
    counts: tuple[int, int, int, int] = (0, 0, 0, 0),
    verdict: str | None = None,
    snippets_json: str | None = None,
) -> None:
    if review_md is not None and len(review_md) > MAX_REVIEW_MD_LENGTH:
        review_md = review_md[:MAX_REVIEW_MD_LENGTH] + TRUNCATION_MARKER
    conn.execute(
        """
        UPDATE review
        SET status = ?, review_md = ?, session_id = ?, exit_code = ?, error = ?,
            finished_at = ?, crit_count = ?, major_count = ?, minor_count = ?, nit_count = ?,
            verdict = ?, snippets_json = ?
        WHERE id = ? AND status IN ('queued', 'cloning', 'reviewing')
        """,
        (
            status,
            review_md,
            session_id,
            exit_code,
            error,
            utcnow_iso(),
            *counts,
            verdict,
            snippets_json,
            review_id,
        ),
    )
    conn.commit()


def retry_review(
    conn: sqlite3.Connection, review_id: int, instructions: str | None = None
) -> bool:
    cursor = conn.execute(
        """
        UPDATE review
        SET status = 'queued', review_md = NULL, session_id = NULL, exit_code = NULL,
            error = NULL, started_at = NULL, finished_at = NULL,
            attempt = 0, not_before = NULL,
            crit_count = 0, major_count = 0, minor_count = 0, nit_count = 0,
            verdict = NULL, snippets_json = NULL, extra_instructions = ?,
            agent = COALESCE(
                (SELECT project.agent FROM project
                 JOIN mr ON mr.project_id = project.id
                 WHERE mr.id = review.mr_id),
                agent
            )
        WHERE id = ? AND status IN ('error', 'done', 'cancelled')
            AND NOT EXISTS (
                SELECT 1 FROM review_question
                WHERE review_question.review_id = review.id
                    AND review_question.status IN ('queued', 'running')
            )
        """,
        (instructions or None, review_id),
    )
    conn.commit()
    return cursor.rowcount > 0


def rebase_review_head(conn: sqlite3.Connection, review_id: int, head_sha: str) -> bool:
    try:
        cursor = conn.execute(
            "UPDATE review SET head_sha = ? "
            "WHERE id = ? AND status IN ('queued', 'cloning', 'reviewing')",
            (head_sha, review_id),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        return False
    return cursor.rowcount > 0


def rebase_and_defer_review(
    conn: sqlite3.Connection, review_id: int, head_sha: str, delay_sec: float = 0.0
) -> bool:
    try:
        cursor = conn.execute(
            """
            UPDATE review
            SET head_sha = ?, status = 'queued', attempt = 0, not_before = ?,
                error = NULL, started_at = NULL, finished_at = NULL
            WHERE id = ? AND status IN ('queued', 'cloning', 'reviewing')
            """,
            (head_sha, deferred_iso(delay_sec), review_id),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        return False
    return cursor.rowcount > 0


def mark_reviewing(conn: sqlite3.Connection, review_id: int) -> None:
    conn.execute(
        "UPDATE review SET status = 'reviewing' "
        "WHERE id = ? AND status IN ('cloning', 'reviewing')",
        (review_id,),
    )
    conn.commit()


def requeue_review(conn: sqlite3.Connection, review_id: int, delay_sec: float = 0.0) -> None:
    not_before = deferred_iso(delay_sec)
    conn.execute(
        """
        UPDATE review
        SET status = 'queued', error = NULL, started_at = NULL, finished_at = NULL,
            not_before = ?
        WHERE id = ? AND status IN ('queued', 'cloning', 'reviewing')
        """,
        (not_before, review_id),
    )
    conn.commit()


def requeue_incomplete(conn: sqlite3.Connection) -> int:
    cursor = conn.execute(
        "UPDATE review SET status = 'queued' WHERE status IN ('queued', 'cloning', 'reviewing')"
    )
    conn.execute(
        "UPDATE review_question SET status = 'queued', started_at = NULL, "
        "parent_session_id = NULL WHERE status = 'running'"
    )
    conn.commit()
    return cursor.rowcount


def create_question(conn: sqlite3.Connection, review_id: int, question: str) -> int | None:
    cursor = conn.execute(
        """
        INSERT INTO review_question (review_id, question, status, root_session_id, created_at)
        SELECT review.id, ?, 'queued', review.session_id, ?
        FROM review
        WHERE review.id = ? AND review.status = 'done' AND review.session_id IS NOT NULL
            AND NOT EXISTS (
                SELECT 1 FROM review_question q
                WHERE q.review_id = review.id AND q.status IN ('queued', 'running')
            )
            AND (SELECT COUNT(*) FROM review_question q WHERE q.review_id = review.id) < ?
        """,
        (question, utcnow_iso(), review_id, MAX_QUESTIONS_PER_REVIEW),
    )
    conn.commit()
    if cursor.rowcount == 0:
        return None
    return int(cursor.lastrowid)


def question_rejection_reason(conn: sqlite3.Connection, review_id: int) -> str:
    review = get_review(conn, review_id)
    if review is None or review["status"] != "done":
        return "вопросы доступны только для завершённого ревью"
    if not review["session_id"]:
        return "у ревью нет сессии opencode"
    if has_active_question(conn, review_id):
        return "предыдущий вопрос ещё обрабатывается, дождитесь ответа"
    return f"достигнут лимит вопросов к ревью ({MAX_QUESTIONS_PER_REVIEW})"


def has_active_question(conn: sqlite3.Connection, review_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM review_question WHERE review_id = ? AND status IN ('queued', 'running') "
        "LIMIT 1",
        (review_id,),
    ).fetchone()
    return row is not None


def claim_next_question(conn: sqlite3.Connection) -> sqlite3.Row | None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT id, review_id, root_session_id FROM review_question "
            "WHERE status = 'queued' ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        parent = conn.execute(
            """
            SELECT session_id FROM review_question
            WHERE review_id = ? AND root_session_id = ? AND status = 'done'
                AND session_id IS NOT NULL AND id < ?
            ORDER BY id DESC LIMIT 1
            """,
            (row["review_id"], row["root_session_id"], row["id"]),
        ).fetchone()
        conn.execute(
            "UPDATE review_question SET status = 'running', started_at = ?, "
            "parent_session_id = ? WHERE id = ?",
            (utcnow_iso(), parent["session_id"] if parent is not None else None, row["id"]),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return conn.execute("SELECT * FROM review_question WHERE id = ?", (row["id"],)).fetchone()


def finish_question(
    conn: sqlite3.Connection,
    question_id: int,
    status: str,
    answer_md: str | None = None,
    session_id: str | None = None,
    exit_code: int | None = None,
    error: str | None = None,
) -> None:
    if answer_md is not None and len(answer_md) > MAX_REVIEW_MD_LENGTH:
        answer_md = answer_md[:MAX_REVIEW_MD_LENGTH] + TRUNCATION_MARKER
    conn.execute(
        """
        UPDATE review_question
        SET status = ?, answer_md = ?, session_id = ?, exit_code = ?, error = ?, finished_at = ?
        WHERE id = ? AND status IN ('queued', 'running')
        """,
        (status, answer_md, session_id, exit_code, error, utcnow_iso(), question_id),
    )
    conn.commit()


def list_questions(conn: sqlite3.Connection, review_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM review_question WHERE review_id = ? ORDER BY id", (review_id,)
    ).fetchall()


def get_review(conn: sqlite3.Connection, review_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM review WHERE id = ?", (review_id,)).fetchone()


def list_mrs(conn: sqlite3.Connection, limit: int = 200) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT mr.*, project.path_with_namespace, project.gitlab_project_id,
            COALESCE(latest_done.crit_count, 0) AS crit_count,
            COALESCE(latest_done.major_count, 0) AS major_count,
            COALESCE(latest_done.minor_count, 0) AS minor_count,
            COALESCE(latest_done.nit_count, 0) AS nit_count,
            latest_done.verdict AS verdict,
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
            SELECT r.mr_id, r.id, r.crit_count, r.major_count, r.minor_count, r.nit_count,
                   r.verdict
            FROM review r
            JOIN (
                SELECT mr_id, MAX(id) AS max_id FROM review
                WHERE status = 'done'
                GROUP BY mr_id
            ) last ON last.max_id = r.id
        ) latest_done ON latest_done.mr_id = mr.id
        WHERE mr.state IS NULL OR mr.state IN ('opened', 'locked')
        ORDER BY julianday(mr.updated_at) IS NULL, julianday(mr.updated_at) DESC, mr.id DESC
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
    dest = backup_dir / f"lpdev-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.db"
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


def radar_snapshot_get(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM radar_snapshot WHERE id = 1").fetchone()


def radar_snapshot_save_success(
    conn: sqlite3.Connection, fetched_at: str, dashboard_json: str, thread_cache_json: str
) -> int:
    with conn:
        row = conn.execute("SELECT version FROM radar_snapshot WHERE id = 1").fetchone()
        version = (row["version"] if row else 0) + 1
        conn.execute(
            """
            INSERT INTO radar_snapshot (id, version, fetched_at, failed_at, error, dashboard_json, thread_cache_json)
            VALUES (1, ?, ?, NULL, NULL, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                version = excluded.version, fetched_at = excluded.fetched_at, failed_at = NULL,
                error = NULL, dashboard_json = excluded.dashboard_json,
                thread_cache_json = excluded.thread_cache_json
            """,
            (version, fetched_at, dashboard_json, thread_cache_json),
        )
    return version


def radar_snapshot_save_failure(conn: sqlite3.Connection, failed_at: str, error: str) -> int:
    with conn:
        row = conn.execute("SELECT version FROM radar_snapshot WHERE id = 1").fetchone()
        version = (row["version"] if row else 0) + 1
        conn.execute(
            """
            INSERT INTO radar_snapshot (id, version, failed_at, error) VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                version = excluded.version, failed_at = excluded.failed_at, error = excluded.error
            """,
            (version, failed_at, error),
        )
    return version


def ui_state_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value_json FROM ui_state WHERE key = ?", (key,)).fetchone()
    return row["value_json"] if row else None


def ui_state_set(conn: sqlite3.Connection, key: str, value_json: str) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO ui_state (key, value_json, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at
            """,
            (key, value_json, utcnow_iso()),
        )


def timesheet_cells(conn: sqlite3.Connection, days: list[str]) -> dict[tuple[str, str], int]:
    if not days:
        return {}
    rows = conn.execute(
        f"SELECT issue_key, day, minutes FROM timesheet_cell WHERE day IN ({','.join('?' * len(days))})",
        days,
    ).fetchall()
    return {(row["issue_key"], row["day"]): row["minutes"] for row in rows}


def timesheet_cell_set(conn: sqlite3.Connection, issue_key: str, day: str, minutes: int | None) -> None:
    with conn:
        if minutes is None:
            conn.execute("DELETE FROM timesheet_cell WHERE issue_key = ? AND day = ?", (issue_key, day))
            return
        conn.execute(
            """
            INSERT INTO timesheet_cell (issue_key, day, minutes, updated_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(issue_key, day) DO UPDATE SET minutes = excluded.minutes, updated_at = excluded.updated_at
            """,
            (issue_key, day, minutes, utcnow_iso()),
        )


def timesheet_cells_clear(conn: sqlite3.Connection, days: list[str]) -> int:
    if not days:
        return 0
    with conn:
        cursor = conn.execute(
            f"DELETE FROM timesheet_cell WHERE day IN ({','.join('?' * len(days))})", days
        )
    return cursor.rowcount


def timesheet_task_add(conn: sqlite3.Connection, issue_key: str, week_start: str) -> None:
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO timesheet_task (issue_key, week_start, created_at) VALUES (?, ?, ?)",
            (issue_key, week_start, utcnow_iso()),
        )


def timesheet_tasks(conn: sqlite3.Connection, week_start: str) -> list[str]:
    rows = conn.execute(
        "SELECT issue_key FROM timesheet_task WHERE week_start = ? ORDER BY created_at, issue_key", (week_start,)
    ).fetchall()
    return [row["issue_key"] for row in rows]


def worklog_submission_create(conn: sqlite3.Connection, issue_key: str, day: str, minutes: int) -> int:
    with conn:
        cursor = conn.execute(
            "INSERT INTO worklog_submission (issue_key, day, minutes, status, created_at) VALUES (?, ?, ?, 'pending', ?)",
            (issue_key, day, minutes, utcnow_iso()),
        )
    return cursor.lastrowid


def worklog_submission_finish(
    conn: sqlite3.Connection,
    submission_id: int,
    status: str,
    tracker_id: str | None = None,
    error: str | None = None,
) -> None:
    with conn:
        conn.execute(
            "UPDATE worklog_submission SET status = ?, tracker_id = ?, error = ?, updated_at = ? WHERE id = ?",
            (status, tracker_id, error, utcnow_iso(), submission_id),
        )


def worklog_submission_attempted(conn: sqlite3.Connection, submission_id: int) -> None:
    with conn:
        conn.execute(
            "UPDATE worklog_submission SET reconcile_attempts = reconcile_attempts + 1, updated_at = ? WHERE id = ?",
            (utcnow_iso(), submission_id),
        )


def worklog_submissions(conn: sqlite3.Connection, days: list[str]) -> list[sqlite3.Row]:
    if not days:
        return []
    return conn.execute(
        f"SELECT * FROM worklog_submission WHERE day IN ({','.join('?' * len(days))}) ORDER BY id",
        days,
    ).fetchall()
