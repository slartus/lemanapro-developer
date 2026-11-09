import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from helpers import list_reviews
from lpdev.db import (
    MAX_REVIEW_MD_LENGTH,
    backup,
    cancel_draft_reviews,
    MAX_QUESTIONS_PER_REVIEW,
    claim_next_question,
    claim_next_review,
    connect,
    create_question,
    create_review,
    finish_question,
    finish_review,
    get_review,
    has_active_question,
    init_schema,
    list_mrs,
    list_questions,
    question_rejection_reason,
    mark_reviewing,
    mr_wants_review,
    poll_state_get,
    poll_state_set,
    rebase_and_defer_review,
    rebase_review_head,
    requeue_incomplete,
    requeue_review,
    retry_review,
    supersede_reviews,
    sync_projects,
    upsert_mr,
)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "test.db")
    init_schema(c)
    yield c
    c.close()


def _sync_projects(conn, agent="agent-a"):
    return sync_projects(conn, [(10, "group/proj", agent, False, True)])


def _upsert_mr(
    conn,
    project_db_id,
    iid=1,
    head_sha="a" * 40,
    state="opened",
    title="MR title",
):
    return upsert_mr(
        conn,
        project_db_id=project_db_id,
        iid=iid,
        title=title,
        author_username="alice",
        source_branch="feature",
        web_url=f"https://gitlab.example.com/group/proj/-/merge_requests/{iid}",
        state=state,
        head_sha=head_sha,
        updated_at="2026-01-01T00:00:00Z",
    )


def test_connect_pragmas(tmp_path):
    conn = connect(tmp_path / "pragma.db")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert conn.row_factory is sqlite3.Row
    finally:
        conn.close()


def test_init_schema_twice(tmp_path):
    db_path = tmp_path / "schema.db"
    conn = connect(db_path)
    try:
        init_schema(conn)
        init_schema(conn)
        tables = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert {"project", "mr", "review", "poll_state"} <= tables
    finally:
        conn.close()


def test_sync_projects_create_and_update(conn):
    mapping = _sync_projects(conn)
    assert list(mapping.keys()) == [10]
    project_id = mapping[10]
    row = conn.execute("SELECT * FROM project WHERE id = ?", (project_id,)).fetchone()
    assert row["path_with_namespace"] == "group/proj"
    assert row["agent"] == "agent-a"
    assert row["re_review_on_push"] == 0
    assert row["enabled"] == 1

    sync_projects(conn, [(10, "group/renamed", "agent-b", True, False)])
    row = conn.execute("SELECT * FROM project WHERE id = ?", (project_id,)).fetchone()
    assert row["path_with_namespace"] == "group/renamed"
    assert row["agent"] == "agent-b"
    assert row["re_review_on_push"] == 1
    assert row["enabled"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM project").fetchone()["c"] == 1


def test_upsert_mr_insert_then_update(conn):
    project_id = _sync_projects(conn)[10]

    result = _upsert_mr(conn, project_id)
    assert result.inserted is True
    assert result.head_sha_changed is True

    result = _upsert_mr(conn, project_id, head_sha="a" * 40)
    assert result.inserted is False
    assert result.head_sha_changed is False

    result = _upsert_mr(conn, project_id, head_sha="b" * 40, state="merged")
    assert result.inserted is False
    assert result.head_sha_changed is True

    row = conn.execute("SELECT * FROM mr WHERE id = ?", (result.mr_id,)).fetchone()
    assert row["head_sha"] == "b" * 40
    assert row["state"] == "merged"
    assert conn.execute("SELECT COUNT(*) c FROM mr").fetchone()["c"] == 1


def test_create_review_duplicate_returns_none(conn):
    project_id = _sync_projects(conn)[10]
    mr_id = _upsert_mr(conn, project_id).mr_id

    review_id = create_review(conn, mr_id, "new", "a" * 40, "agent-a")
    assert review_id is not None
    assert create_review(conn, mr_id, "new", "a" * 40, "agent-a") is None
    assert create_review(conn, mr_id, "push", "a" * 40, "agent-a") is not None
    assert conn.execute("SELECT COUNT(*) c FROM review").fetchone()["c"] == 2


def test_claim_next_review(conn):
    project_id = _sync_projects(conn)[10]
    mr_id = _upsert_mr(conn, project_id).mr_id
    first_id = create_review(conn, mr_id, "new", "a" * 40, "agent-a")

    row = claim_next_review(conn)
    assert row is not None
    assert row["id"] == first_id
    assert row["status"] == "cloning"
    assert row["attempt"] == 1
    assert row["started_at"] is not None

    assert claim_next_review(conn) is None


def test_claim_next_review_respects_order(conn):
    project_id = _sync_projects(conn)[10]
    mr_id = _upsert_mr(conn, project_id).mr_id
    first_id = create_review(conn, mr_id, "new", "a" * 40, "agent-a")
    create_review(conn, mr_id, "push", "b" * 40, "agent-a")

    assert claim_next_review(conn)["id"] == first_id


def test_finish_review_writes_fields_and_truncates(conn):
    project_id = _sync_projects(conn)[10]
    mr_id = _upsert_mr(conn, project_id).mr_id
    review_id = create_review(conn, mr_id, "new", "a" * 40, "agent-a")

    big_md = "x" * (MAX_REVIEW_MD_LENGTH + 500)
    finish_review(conn, review_id, "done", review_md=big_md, session_id="sess-1", exit_code=0)
    row = get_review(conn, review_id)
    assert row["status"] == "done"
    assert row["session_id"] == "sess-1"
    assert row["exit_code"] == 0
    assert row["finished_at"] is not None
    assert len(row["review_md"]) == MAX_REVIEW_MD_LENGTH + len("...[truncated]")
    assert row["review_md"].endswith("...[truncated]")

    review_id2 = create_review(conn, mr_id, "push", "b" * 40, "agent-a")
    finish_review(conn, review_id2, "error", error="boom")
    row2 = get_review(conn, review_id2)
    assert row2["status"] == "error"
    assert row2["error"] == "boom"
    assert row2["review_md"] is None


def test_retry_review_from_error_and_done(conn):
    project_id = _sync_projects(conn)[10]
    mr_id = _upsert_mr(conn, project_id).mr_id
    review_id = create_review(conn, mr_id, "new", "a" * 40, "agent-a")

    assert retry_review(conn, review_id) is False

    claim_next_review(conn)
    finish_review(conn, review_id, "error", error="fail")
    started_at_before = get_review(conn, review_id)["started_at"]
    assert started_at_before is not None

    assert retry_review(conn, review_id) is True
    row = get_review(conn, review_id)
    assert row["status"] == "queued"
    assert row["error"] is None
    assert row["review_md"] is None
    assert row["session_id"] is None
    assert row["exit_code"] is None
    assert row["started_at"] is None
    assert row["finished_at"] is None

    finish_review(conn, review_id, "done", review_md="ok", session_id="s2")
    assert retry_review(conn, review_id) is True
    row = get_review(conn, review_id)
    assert row["status"] == "queued"
    assert row["review_md"] is None

    claim_next_review(conn)
    assert retry_review(conn, review_id) is False


def test_upsert_mr_update_keeps_old_when_none(conn):
    project_id = _sync_projects(conn)[10]
    mr_id = _upsert_mr(conn, project_id).mr_id

    result = upsert_mr(
        conn,
        project_db_id=project_id,
        iid=1,
        title=None,
        author_username=None,
        source_branch=None,
        web_url=None,
        state=None,
        head_sha=None,
        updated_at=None,
    )
    assert result.inserted is False
    assert result.head_sha_changed is False

    row = conn.execute("SELECT * FROM mr WHERE id = ?", (mr_id,)).fetchone()
    assert row["head_sha"] == "a" * 40
    assert row["state"] == "opened"


def test_mr_requires_existing_project(conn):
    with pytest.raises(sqlite3.IntegrityError):
        upsert_mr(
            conn,
            project_db_id=999,
            iid=1,
            title="t",
            author_username=None,
            source_branch=None,
            web_url=None,
            state="opened",
            head_sha="a" * 40,
            updated_at=None,
        )


def test_requeue_incomplete(conn):
    project_id = _sync_projects(conn)[10]
    mr_id = _upsert_mr(conn, project_id).mr_id
    first_id = create_review(conn, mr_id, "new", "a" * 40, "agent-a")
    create_review(conn, mr_id, "push", "b" * 40, "agent-a")

    claimed = claim_next_review(conn)
    assert claimed["id"] == first_id
    assert get_review(conn, first_id)["status"] == "cloning"
    attempt_before = get_review(conn, first_id)["attempt"]

    assert requeue_incomplete(conn) == 2
    rows = conn.execute("SELECT status FROM review").fetchall()
    assert all(r["status"] == "queued" for r in rows)
    assert get_review(conn, first_id)["attempt"] == attempt_before


def test_poll_state_set_get(conn):
    assert poll_state_get(conn, "cursor") is None
    poll_state_set(conn, "cursor", "2026-01-01T00:00:00Z")
    assert poll_state_get(conn, "cursor") == "2026-01-01T00:00:00Z"
    poll_state_set(conn, "cursor", "2026-01-02T00:00:00Z")
    assert poll_state_get(conn, "cursor") == "2026-01-02T00:00:00Z"
    poll_state_set(conn, "other", "v1")
    assert poll_state_get(conn, "cursor") == "2026-01-02T00:00:00Z"
    assert poll_state_get(conn, "other") == "v1"


def test_backup_creates_file(conn, tmp_path):
    project_id = _sync_projects(conn)[10]
    mr_id = _upsert_mr(conn, project_id).mr_id
    review_id = create_review(conn, mr_id, "new", "a" * 40, "agent-a")
    finish_review(conn, review_id, "done", review_md="ok")

    dest = backup(tmp_path / "test.db", tmp_path / "backups")
    assert isinstance(dest, Path)
    assert dest.is_file()
    assert dest.name.startswith("lpdev-")
    assert dest.name.endswith(".db")

    check = connect(dest)
    try:
        row = check.execute("SELECT review_md FROM review WHERE id = ?", (review_id,)).fetchone()
        assert row["review_md"] == "ok"
    finally:
        check.close()


def test_list_reviews_and_mrs(conn):
    project_id = _sync_projects(conn)[10]
    mr1 = _upsert_mr(conn, project_id, iid=1)
    mr2 = _upsert_mr(conn, project_id, iid=2, head_sha="b" * 40)
    create_review(conn, mr1.mr_id, "new", "a" * 40, "agent-a")
    create_review(conn, mr2.mr_id, "new", "b" * 40, "agent-a")

    reviews = list_reviews(conn)
    assert len(reviews) == 2
    assert reviews[0]["iid"] == 2
    assert reviews[0]["path_with_namespace"] == "group/proj"
    assert reviews[0]["title"] == "MR title"

    mrs = list_mrs(conn)
    assert len(mrs) == 2
    assert mrs[0]["iid"] == 2
    assert mrs[0]["path_with_namespace"] == "group/proj"
    assert mrs[0]["gitlab_project_id"] == 10


def test_migration_v1_to_v2_adds_severity_columns(tmp_path):
    import sqlite3

    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE mr (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER, iid INTEGER, title TEXT, author_username TEXT,
            source_branch TEXT, web_url TEXT, state TEXT, head_sha TEXT,
            first_seen_at TEXT, updated_at TEXT
        );
        CREATE TABLE review (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mr_id INTEGER, "trigger" TEXT, head_sha TEXT,
            status TEXT, agent TEXT, session_id TEXT, review_md TEXT,
            exit_code INTEGER, error TEXT, attempt INTEGER DEFAULT 0,
            created_at TEXT, started_at TEXT, finished_at TEXT
        );
        """
    )
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()

    conn = connect(db_path)
    init_schema(conn)
    columns = {r[1] for r in conn.execute("PRAGMA table_info(review)")}
    assert {"crit_count", "major_count", "minor_count", "nit_count"} <= columns
    mr_columns = {r[1] for r in conn.execute("PRAGMA table_info(mr)")}
    assert "author_name" in mr_columns
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 11
    assert "my_roles" in mr_columns
    conn.close()


def test_upsert_mr_stores_author_name(tmp_path):
    from lpdev.db import connect, init_schema, sync_projects, upsert_mr

    conn = connect(tmp_path / "t.db")
    init_schema(conn)
    mapping = sync_projects(conn, [(10, "g/p", "agent-a", False, True)])
    upsert_mr(
        conn, mapping[10], 1, "t", "u1", "b", "http://x", "opened", "a" * 40,
        "2026-01-01T12:00:00Z", author_name="Иван Петров",
    )
    upsert_mr(
        conn, mapping[10], 1, "t", "u1", "b", "http://x", "opened", "a" * 40,
        "2026-01-01T13:00:00Z", author_name="Иван Петров",
    )
    row = conn.execute("SELECT author_name FROM mr WHERE iid = 1").fetchone()
    assert row["author_name"] == "Иван Петров"
    conn.close()


def test_migration_v5_idempotent_and_keeps_data(tmp_path):
    from lpdev.db import _migrate_to_v5, _run_migration

    conn = connect(tmp_path / "t.db")
    init_schema(conn)
    mapping = sync_projects(conn, [(10, "g/p", "agent-a", False, True)])
    upsert_mr(
        conn, mapping[10], 1, "t", "u1", "b", "http://x", "opened", "a" * 40,
        "2026-01-01T12:00:00Z", signal_roles={"mentioned"},
    )
    conn.execute("PRAGMA user_version = 4")
    conn.commit()
    init_schema(conn)
    _run_migration(conn, 5, _migrate_to_v5)
    init_schema(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 11
    assert conn.execute("SELECT my_roles FROM mr").fetchone()["my_roles"] == "mentioned"
    conn.close()


def test_roles_union_recompute_and_clear_on_close(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_schema(conn)
    mapping = sync_projects(conn, [(10, "g/p", "agent-a", False, True)])

    def put(state="opened", derived=None, signal=None):
        upsert_mr(
            conn, mapping[10], 1, "t", "u1", "b", "http://x", state, "a" * 40,
            "2026-01-01T12:00:00Z", derived_roles=derived, signal_roles=signal,
        )
        return conn.execute("SELECT my_roles FROM mr").fetchone()["my_roles"]

    assert put(derived={"reviewer", "author"}) == "author,reviewer"
    assert put(signal={"mentioned"}) == "author,mentioned,reviewer"
    assert put(derived={"author"}) == "author,mentioned"
    assert put(derived=set()) == "mentioned"
    assert put(state="merged", derived=set()) == ""
    conn.close()


def test_migration_v6_adds_is_draft_and_is_idempotent(tmp_path):
    conn = connect(tmp_path / "v5.db")
    init_schema(conn)
    conn.execute("ALTER TABLE mr DROP COLUMN is_draft")
    conn.execute("PRAGMA user_version = 5")
    conn.commit()
    init_schema(conn)
    init_schema(conn)
    columns = {r[1]: r for r in conn.execute("PRAGMA table_info(mr)")}
    assert columns["is_draft"][3] == 1
    assert columns["is_draft"][4] == "0"
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 11
    conn.execute("PRAGMA user_version = 5")
    conn.commit()
    init_schema(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 11
    conn.close()


def test_migration_v7_adds_verdict_backfills_and_is_idempotent(tmp_path):
    conn = connect(tmp_path / "v6.db")
    init_schema(conn)
    conn.execute("ALTER TABLE review DROP COLUMN verdict")
    conn.execute("PRAGMA user_version = 6")
    conn.execute("INSERT INTO project (id, gitlab_project_id, path_with_namespace, agent) VALUES (1, 1, 'g/p', 'a')")
    conn.execute("INSERT INTO mr (id, project_id, iid, head_sha, updated_at, first_seen_at) VALUES (1, 1, 1, 'x', 't', 't')")
    samples = [
        ("done", "## Verdict\nREQUEST_CHANGES"),
        ("done", "## Verdict\nAPPROVE_WITH_NITS"),
        ("done", "нет вердикта"),
        ("done", None),
        ("error", "## Verdict\nAPPROVE"),
    ]
    for index, (status, md) in enumerate(samples):
        conn.execute(
            "INSERT INTO review (mr_id, \"trigger\", head_sha, status, agent, review_md, created_at) "
            "VALUES (1, 'new', ?, ?, 'a', ?, 't')",
            (f"sha{index}", status, md),
        )
    conn.commit()
    init_schema(conn)
    init_schema(conn)
    verdicts = [r[0] for r in conn.execute("SELECT verdict FROM review ORDER BY id")]
    assert verdicts == ["REQUEST_CHANGES", "APPROVE_WITH_NITS", None, None, None]
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 11
    conn.execute("PRAGMA user_version = 6")
    conn.commit()
    init_schema(conn)
    assert [r[0] for r in conn.execute("SELECT verdict FROM review ORDER BY id")] == verdicts
    conn.close()


def test_migration_v7_backfills_in_batches(tmp_path):
    conn = connect(tmp_path / "many.db")
    init_schema(conn)
    conn.execute("ALTER TABLE review DROP COLUMN verdict")
    conn.execute("PRAGMA user_version = 6")
    conn.execute("INSERT INTO project (id, gitlab_project_id, path_with_namespace, agent) VALUES (1, 1, 'g/p', 'a')")
    conn.execute("INSERT INTO mr (id, project_id, iid, head_sha, updated_at, first_seen_at) VALUES (1, 1, 1, 'x', 't', 't')")
    for index in range(450):
        conn.execute(
            "INSERT INTO review (mr_id, \"trigger\", head_sha, status, agent, review_md, created_at) "
            "VALUES (1, 'new', ?, 'done', 'a', '## Verdict\nAPPROVE', 't')",
            (f"sha{index}",),
        )
    conn.commit()
    init_schema(conn)
    assert conn.execute("SELECT COUNT(*) FROM review WHERE verdict = 'APPROVE'").fetchone()[0] == 450
    conn.close()


def test_finish_review_stores_verdict_and_retry_clears_it(tmp_path):
    conn = connect(tmp_path / "v.db")
    init_schema(conn)
    mapping = sync_projects(conn, [(10, "g/p", "agent-a", False, True)])
    mr = upsert_mr(
        conn, project_db_id=mapping[10], iid=1, title="t", author_username="me",
        source_branch="f", web_url="https://x/1", state="opened", head_sha="a" * 40,
        updated_at="2026-01-01T12:00:00Z",
    )
    rid = create_review(conn, mr.mr_id, "new", "a" * 40, "agent-a")
    finish_review(conn, rid, "done", review_md="x", verdict="REQUEST_CHANGES")
    assert get_review(conn, rid)["verdict"] == "REQUEST_CHANGES"
    assert list_mrs(conn)[0]["verdict"] == "REQUEST_CHANGES"
    assert retry_review(conn, rid)
    assert get_review(conn, rid)["verdict"] is None
    conn.close()


def test_migration_v8_adds_mention_url_idempotently(tmp_path):
    conn = connect(tmp_path / "v7.db")
    init_schema(conn)
    conn.execute("ALTER TABLE mr DROP COLUMN mention_url")
    conn.execute("PRAGMA user_version = 7")
    conn.commit()
    init_schema(conn)
    init_schema(conn)
    columns = {r[1]: r for r in conn.execute("PRAGMA table_info(mr)")}
    assert columns["mention_url"][3] == 0
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 11
    conn.execute("PRAGMA user_version = 7")
    conn.commit()
    init_schema(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 11
    conn.close()


def test_verdict_backfill_skips_truncated_review_md(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_schema(conn)
    conn.execute("ALTER TABLE review DROP COLUMN verdict")
    conn.execute("PRAGMA user_version = 6")
    conn.execute("INSERT INTO project (id, gitlab_project_id, path_with_namespace, agent) VALUES (1, 1, 'g/p', 'a')")
    conn.execute("INSERT INTO mr (id, project_id, iid, head_sha, updated_at, first_seen_at) VALUES (1, 1, 1, 'x', 't', 't')")
    truncated = "## Verdict\nAPPROVE\n" + "x" * MAX_REVIEW_MD_LENGTH
    truncated = truncated[:MAX_REVIEW_MD_LENGTH] + "...[truncated]"
    conn.execute(
        "INSERT INTO review (mr_id, \"trigger\", head_sha, status, agent, review_md, created_at) "
        "VALUES (1, 'new', 'sha', 'done', 'a', ?, 't')",
        (truncated,),
    )
    conn.commit()
    init_schema(conn)
    assert len(truncated) > MAX_REVIEW_MD_LENGTH
    assert conn.execute("SELECT verdict FROM review").fetchone()[0] is None
    conn.close()


def test_retry_review_uses_current_project_agent(conn):
    project_id = _sync_projects(conn, "agent-a")[10]
    mr_id = _upsert_mr(conn, project_id).mr_id
    review_id = create_review(conn, mr_id, "new", "a" * 40, "agent-a")
    claim_next_review(conn)
    finish_review(conn, review_id, "error", error="fail")

    _sync_projects(conn, "agent-b")
    assert retry_review(conn, review_id) is True
    assert get_review(conn, review_id)["agent"] == "agent-b"

    claim_next_review(conn)
    finish_review(conn, review_id, "done", review_md="ok")
    assert retry_review(conn, review_id) is True
    assert get_review(conn, review_id)["agent"] == "agent-b"


def test_retry_review_keeps_agent_when_project_missing(conn):
    project_id = _sync_projects(conn, "agent-a")[10]
    mr_id = _upsert_mr(conn, project_id).mr_id
    review_id = create_review(conn, mr_id, "new", "a" * 40, "agent-a")
    claim_next_review(conn)
    finish_review(conn, review_id, "error", error="fail")

    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("DELETE FROM project WHERE id = ?", (project_id,))
    conn.commit()

    assert retry_review(conn, review_id) is True
    assert get_review(conn, review_id)["agent"] == "agent-a"


V9_REVIEW_DDL = """
CREATE TABLE review (
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
ALTER TABLE review ADD COLUMN crit_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE review ADD COLUMN major_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE review ADD COLUMN minor_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE review ADD COLUMN nit_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE review ADD COLUMN not_before TEXT;
ALTER TABLE review ADD COLUMN verdict TEXT;
ALTER TABLE review ADD COLUMN snippets_json TEXT;
CREATE INDEX idx_review_status ON review(status);
CREATE INDEX idx_review_mr ON review(mr_id);
"""


def _downgrade_review_to_v9(conn):
    conn.execute("DROP TABLE review")
    conn.executescript(V9_REVIEW_DDL)
    conn.execute("PRAGMA user_version = 9")
    conn.commit()


def _review_indexes(conn):
    return {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'review' "
            "AND name LIKE 'idx_%'"
        )
    }


def test_fresh_schema_accepts_cancelled(conn):
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 11
    project_id = _sync_projects(conn)[10]
    mr_id = _upsert_mr(conn, project_id).mr_id
    review_id = create_review(conn, mr_id, "new", "a" * 40, "agent-a")
    conn.execute("UPDATE review SET status = 'cancelled' WHERE id = ?", (review_id,))
    assert get_review(conn, review_id)["status"] == "cancelled"
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE review SET status = 'bogus' WHERE id = ?", (review_id,))
    assert _review_indexes(conn) == {"idx_review_status", "idx_review_mr"}


def test_migration_v9_to_v10_keeps_data_indexes_and_unique(tmp_path):
    conn = connect(tmp_path / "v9.db")
    init_schema(conn)
    _downgrade_review_to_v9(conn)
    project_id = _sync_projects(conn)[10]
    mr_id = _upsert_mr(conn, project_id).mr_id
    first = create_review(conn, mr_id, "new", "a" * 40, "agent-a")
    second = create_review(conn, mr_id, "push", "b" * 40, "agent-a")
    third = create_review(conn, mr_id, "manual", "c" * 40, "agent-a")
    finish_review(
        conn, first, "done", review_md="ok", session_id="s1", exit_code=0,
        counts=(1, 2, 3, 4), verdict="APPROVE", snippets_json="[]",
    )
    conn.execute("UPDATE review SET not_before = 'later' WHERE id = ?", (second,))
    conn.execute("DELETE FROM review WHERE id = ?", (third,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE review SET status = 'cancelled' WHERE id = ?", (second,))
    before = [tuple(r) for r in conn.execute("SELECT * FROM review ORDER BY id")]

    init_schema(conn)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == 11
    assert [tuple(r) for r in conn.execute("SELECT * FROM review ORDER BY id")] == [
        row + (None,) for row in before
    ]
    assert _review_indexes(conn) == {"idx_review_status", "idx_review_mr"}
    assert create_review(conn, mr_id, "new", "a" * 40, "agent-a") is None
    conn.execute("UPDATE review SET status = 'cancelled' WHERE id = ?", (second,))
    assert get_review(conn, second)["status"] == "cancelled"
    assert create_review(conn, mr_id, "new", "d" * 40, "agent-a") > third
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "review_new" not in tables
    conn.close()


def _mr_with_reviews(conn):
    project_id = _sync_projects(conn)[10]
    return _upsert_mr(conn, project_id).mr_id


def _set_status(conn, review_id, status):
    conn.execute("UPDATE review SET status = ? WHERE id = ?", (status, review_id))


def test_supersede_rebases_queued_review(conn):
    mr_id = _mr_with_reviews(conn)
    review_id = create_review(conn, mr_id, "new", "a" * 40, "agent-a")
    conn.execute(
        "UPDATE review SET attempt = 2, not_before = '2999-01-01T00:00:00Z' WHERE id = ?",
        (review_id,),
    )

    result = supersede_reviews(conn, mr_id, "b" * 40)

    assert result.rebased == [review_id]
    assert result.cancelled_active == [] and result.cancelled_queued == []
    assert result.touched
    row = get_review(conn, review_id)
    assert row["status"] == "queued"
    assert row["head_sha"] == "b" * 40
    assert row["attempt"] == 0
    assert row["not_before"] is None


def test_supersede_cancels_queued_on_conflict(conn):
    mr_id = _mr_with_reviews(conn)
    stale = create_review(conn, mr_id, "new", "a" * 40, "agent-a")
    existing = create_review(conn, mr_id, "new", "b" * 40, "agent-a")

    result = supersede_reviews(conn, mr_id, "b" * 40)

    assert result.cancelled_queued == [stale]
    assert result.rebased == [] and result.cancelled_active == []
    row = get_review(conn, stale)
    assert row["status"] == "cancelled"
    assert row["head_sha"] == "a" * 40
    assert row["finished_at"] is not None
    assert get_review(conn, existing)["status"] == "queued"


@pytest.mark.parametrize("status", ["cloning", "reviewing"])
def test_supersede_cancels_active(conn, status):
    mr_id = _mr_with_reviews(conn)
    review_id = create_review(conn, mr_id, "new", "a" * 40, "agent-a")
    _set_status(conn, review_id, status)

    result = supersede_reviews(conn, mr_id, "b" * 40)

    assert result.cancelled_active == [review_id]
    row = get_review(conn, review_id)
    assert row["status"] == "cancelled"
    assert row["head_sha"] == "a" * 40
    assert row["finished_at"] is not None
    assert row["error"] == f"отменено: в MR новый коммит {'b' * 12}"


def test_supersede_skips_finished_and_same_head(conn):
    mr_id = _mr_with_reviews(conn)
    done = create_review(conn, mr_id, "new", "a" * 40, "agent-a")
    finish_review(conn, done, "done", review_md="ok")
    failed = create_review(conn, mr_id, "manual", "a" * 40, "agent-a")
    finish_review(conn, failed, "error", error="boom")
    current = create_review(conn, mr_id, "push", "b" * 40, "agent-a")
    _set_status(conn, current, "reviewing")

    result = supersede_reviews(conn, mr_id, "b" * 40)

    assert not result.touched
    assert get_review(conn, done)["status"] == "done"
    assert get_review(conn, failed)["status"] == "error"
    assert get_review(conn, current)["status"] == "reviewing"


def test_late_writes_do_not_override_cancelled(conn):
    mr_id = _mr_with_reviews(conn)
    review_id = create_review(conn, mr_id, "new", "a" * 40, "agent-a")
    _set_status(conn, review_id, "reviewing")
    supersede_reviews(conn, mr_id, "b" * 40)
    cancelled = dict(get_review(conn, review_id))

    finish_review(conn, review_id, "done", review_md="late")
    mark_reviewing(conn, review_id)
    requeue_review(conn, review_id)
    assert rebase_review_head(conn, review_id, "c" * 40) is False

    assert dict(get_review(conn, review_id)) == cancelled


def test_retry_review_from_cancelled(conn):
    mr_id = _mr_with_reviews(conn)
    review_id = create_review(conn, mr_id, "new", "a" * 40, "agent-a")
    _set_status(conn, review_id, "cloning")
    supersede_reviews(conn, mr_id, "b" * 40)

    assert retry_review(conn, review_id) is True

    row = get_review(conn, review_id)
    assert row["status"] == "queued"
    assert row["error"] is None
    assert row["finished_at"] is None


def test_list_mrs_ignores_cancelled_for_verdict(conn):
    mr_id = _mr_with_reviews(conn)
    done = create_review(conn, mr_id, "new", "a" * 40, "agent-a")
    finish_review(conn, done, "done", review_md="ok", counts=(1, 0, 0, 0), verdict="APPROVE")
    active = create_review(conn, mr_id, "push", "b" * 40, "agent-a")
    _set_status(conn, active, "reviewing")
    conn.execute("UPDATE review SET verdict = 'REQUEST_CHANGES', crit_count = 9 WHERE id = ?", (active,))
    supersede_reviews(conn, mr_id, "c" * 40)

    row = list_mrs(conn)[0]
    assert row["latest_done_review_id"] == done
    assert row["verdict"] == "APPROVE"
    assert row["crit_count"] == 1
    assert row["latest_review_status"] == "cancelled"



def _upsert_draft(conn, project_db_id, is_draft):
    return upsert_mr(
        conn,
        project_db_id=project_db_id,
        iid=1,
        title="MR title",
        author_username="alice",
        source_branch="feature",
        web_url="https://gitlab.example.com/group/proj/-/merge_requests/1",
        state="opened",
        head_sha="a" * 40,
        updated_at="2026-01-01T00:00:00Z",
        is_draft=is_draft,
    )


def test_upsert_reports_draft_transitions(conn):
    project_id = _sync_projects(conn)[10]
    inserted = _upsert_draft(conn, project_id, True)
    assert not inserted.became_ready and not inserted.became_draft

    unknown = _upsert_draft(conn, project_id, None)
    assert not unknown.became_ready and not unknown.became_draft

    ready = _upsert_draft(conn, project_id, False)
    assert ready.became_ready and not ready.became_draft

    still_ready = _upsert_draft(conn, project_id, None)
    assert not still_ready.became_ready and not still_ready.became_draft

    drafted = _upsert_draft(conn, project_id, True)
    assert drafted.became_draft and not drafted.became_ready

    still_draft = _upsert_draft(conn, project_id, True)
    assert not still_draft.became_ready and not still_draft.became_draft


def test_cancel_draft_reviews_cancels_active_only(conn):
    mr_id = _mr_with_reviews(conn)
    done = create_review(conn, mr_id, "new", "a" * 40, "agent-a")
    finish_review(conn, done, "done", review_md="ok")
    queued = create_review(conn, mr_id, "push", "a" * 40, "agent-a")
    cloning = create_review(conn, mr_id, "manual", "a" * 40, "agent-a")
    _set_status(conn, cloning, "cloning")
    reviewing = create_review(conn, mr_id, "new", "b" * 40, "agent-a")
    _set_status(conn, reviewing, "reviewing")

    result = cancel_draft_reviews(conn, mr_id)

    assert result.cancelled_active == [cloning, reviewing]
    assert result.cancelled_queued == [queued]
    assert result.rebased == []
    for review_id in (queued, cloning, reviewing):
        row = get_review(conn, review_id)
        assert row["status"] == "cancelled"
        assert row["finished_at"] is not None
        assert row["error"] == "отменено: MR переведён в draft"
    assert get_review(conn, done)["status"] == "done"
    assert not cancel_draft_reviews(conn, mr_id).touched


def test_mr_wants_review_by_roles_or_history(conn):
    project_id = _sync_projects(conn)[10]
    mr_id = _upsert_mr(conn, project_id).mr_id
    assert not mr_wants_review(conn, mr_id)
    assert not mr_wants_review(conn, mr_id + 100)

    conn.execute("UPDATE mr SET my_roles = 'assignee' WHERE id = ?", (mr_id,))
    assert not mr_wants_review(conn, mr_id)
    for roles in ("reviewer", "author", "assignee,mentioned"):
        conn.execute("UPDATE mr SET my_roles = ? WHERE id = ?", (roles, mr_id))
        assert mr_wants_review(conn, mr_id)

    conn.execute("UPDATE mr SET my_roles = '' WHERE id = ?", (mr_id,))
    create_review(conn, mr_id, "manual", "a" * 40, "agent-a")
    assert mr_wants_review(conn, mr_id)


def _done_review(conn, session_id="ses_root1", head_sha="a" * 40, trigger="new"):
    project_id = _sync_projects(conn)[10]
    mr_id = _upsert_mr(conn, project_id, head_sha=head_sha).mr_id
    review_id = create_review(conn, mr_id, trigger, head_sha, "agent-a")
    finish_review(conn, review_id, "done", review_md="ok", session_id=session_id)
    return review_id


def _question_status(conn, question_id):
    return conn.execute(
        "SELECT status FROM review_question WHERE id = ?", (question_id,)
    ).fetchone()[0]


def test_migration_v10_to_v11_keeps_data_and_adds_question_table(tmp_path):
    conn = connect(tmp_path / "v10.db")
    init_schema(conn)
    conn.execute("DROP TABLE review_question")
    conn.execute("ALTER TABLE review DROP COLUMN extra_instructions")
    conn.execute("PRAGMA user_version = 10")
    review_id = _done_review(conn)
    before = tuple(conn.execute("SELECT * FROM review WHERE id = ?", (review_id,)).fetchone())

    init_schema(conn)
    init_schema(conn)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == 11
    after = tuple(conn.execute("SELECT * FROM review WHERE id = ?", (review_id,)).fetchone())
    assert after == before + (None,)
    assert create_question(conn, review_id, "почему?") is not None
    indexes = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'review_question'"
            " AND name LIKE 'idx_%'"
        )
    }
    assert indexes == {"idx_review_question_review", "idx_review_question_status"}
    conn.close()


def test_fresh_schema_has_questions_and_instructions(conn):
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 11
    columns = {r[1] for r in conn.execute("PRAGMA table_info(review)")}
    assert "extra_instructions" in columns
    review_id = _done_review(conn)
    question_id = create_question(conn, review_id, "q")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE review_question SET status = 'bogus' WHERE id = ?", (question_id,))


def test_create_question_requires_done_review_with_session(conn):
    project_id = _sync_projects(conn)[10]
    mr_id = _upsert_mr(conn, project_id).mr_id
    review_id = create_review(conn, mr_id, "new", "a" * 40, "agent-a")
    assert create_question(conn, review_id, "q") is None
    finish_review(conn, review_id, "error", error="x", session_id="ses_x")
    assert create_question(conn, review_id, "q") is None
    _set_status(conn, review_id, "done")
    conn.execute("UPDATE review SET session_id = NULL WHERE id = ?", (review_id,))
    assert create_question(conn, review_id, "q") is None
    assert "сессии" in question_rejection_reason(conn, review_id)
    assert create_question(conn, 999, "q") is None


def test_create_question_snapshots_root_and_blocks_second_active(conn):
    review_id = _done_review(conn)
    first = create_question(conn, review_id, "первый")
    row = list_questions(conn, review_id)[0]
    assert row["id"] == first
    assert row["status"] == "queued"
    assert row["root_session_id"] == "ses_root1"
    assert create_question(conn, review_id, "второй") is None
    assert has_active_question(conn, review_id)
    assert "обрабатывается" in question_rejection_reason(conn, review_id)
    claim_next_question(conn)
    assert create_question(conn, review_id, "второй") is None
    finish_question(conn, first, "done", answer_md="ответ", session_id="ses_f1")
    assert create_question(conn, review_id, "второй") is not None


def test_create_question_limit(conn):
    review_id = _done_review(conn)
    for i in range(MAX_QUESTIONS_PER_REVIEW):
        conn.execute(
            "INSERT INTO review_question (review_id, question, status, created_at) "
            "VALUES (?, ?, 'done', 'now')",
            (review_id, f"q{i}"),
        )
    assert create_question(conn, review_id, "лишний") is None
    assert "лимит" in question_rejection_reason(conn, review_id)


def test_claim_next_question_parent_chain(conn):
    review_id = _done_review(conn)
    first = create_question(conn, review_id, "1")
    claimed = claim_next_question(conn)
    assert claimed["id"] == first
    assert claimed["status"] == "running"
    assert claimed["started_at"] is not None
    assert claimed["parent_session_id"] is None
    finish_question(conn, first, "done", answer_md="a1", session_id="ses_f1")

    second = create_question(conn, review_id, "2")
    claimed = claim_next_question(conn)
    assert claimed["id"] == second
    assert claimed["parent_session_id"] == "ses_f1"
    finish_question(conn, second, "error", error="boom")

    third = create_question(conn, review_id, "3")
    claimed = claim_next_question(conn)
    assert claimed["id"] == third
    assert claimed["parent_session_id"] == "ses_f1"
    assert claim_next_question(conn) is None


def test_claim_next_question_ignores_previous_review_version(conn):
    review_id = _done_review(conn)
    first = create_question(conn, review_id, "1")
    claim_next_question(conn)
    finish_question(conn, first, "done", answer_md="a1", session_id="ses_f1")
    assert retry_review(conn, review_id)
    finish_review(conn, review_id, "done", review_md="ok2", session_id="ses_root2")

    second = create_question(conn, review_id, "2")
    claimed = claim_next_question(conn)
    assert claimed["id"] == second
    assert claimed["root_session_id"] == "ses_root2"
    assert claimed["parent_session_id"] is None


def test_finish_question_truncates_and_keeps_cancelled(conn):
    review_id = _done_review(conn)
    first = create_question(conn, review_id, "1")
    claim_next_question(conn)
    finish_question(conn, first, "done", answer_md="x" * (MAX_REVIEW_MD_LENGTH + 10))
    answer = list_questions(conn, review_id)[0]["answer_md"]
    assert len(answer) == MAX_REVIEW_MD_LENGTH + len("...[truncated]")

    second = create_question(conn, review_id, "2")
    conn.execute("UPDATE review_question SET status = 'cancelled' WHERE id = ?", (second,))
    finish_question(conn, second, "done", answer_md="late")
    row = list_questions(conn, review_id)[1]
    assert row["status"] == "cancelled"
    assert row["answer_md"] is None


def test_retry_review_blocked_by_active_question(conn):
    review_id = _done_review(conn)
    question_id = create_question(conn, review_id, "q")
    assert retry_review(conn, review_id) is False
    claim_next_question(conn)
    assert retry_review(conn, review_id) is False
    assert get_review(conn, review_id)["status"] == "done"
    finish_question(conn, question_id, "done", answer_md="a")
    assert retry_review(conn, review_id) is True


def test_retry_review_writes_and_clears_instructions(conn):
    review_id = _done_review(conn)
    assert retry_review(conn, review_id, "проверь тесты")
    assert get_review(conn, review_id)["extra_instructions"] == "проверь тесты"
    finish_review(conn, review_id, "done", review_md="ok", session_id="ses_r")
    assert retry_review(conn, review_id, "")
    assert get_review(conn, review_id)["extra_instructions"] is None
    finish_review(conn, review_id, "done", review_md="ok", session_id="ses_r")
    retry_review(conn, review_id, "x")
    finish_review(conn, review_id, "done", review_md="ok", session_id="ses_r")
    assert retry_review(conn, review_id)
    assert get_review(conn, review_id)["extra_instructions"] is None


def test_instructions_survive_requeue_rebase_and_supersede(conn):
    review_id = _done_review(conn)
    retry_review(conn, review_id, "фокус на безопасности")
    claim_next_review(conn)
    requeue_review(conn, review_id)
    claim_next_review(conn)
    assert rebase_review_head(conn, review_id, "b" * 40)
    requeue_review(conn, review_id)
    mr_id = get_review(conn, review_id)["mr_id"]
    supersede_reviews(conn, mr_id, "c" * 40)
    review = get_review(conn, review_id)
    assert review["head_sha"] == "c" * 40
    assert review["extra_instructions"] == "фокус на безопасности"


def test_requeue_incomplete_requeues_running_questions(conn):
    review_id = _done_review(conn)
    question_id = create_question(conn, review_id, "1")
    claim_next_question(conn)
    assert _question_status(conn, question_id) == "running"
    requeue_incomplete(conn)
    row = list_questions(conn, review_id)[0]
    assert row["status"] == "queued"
    assert row["started_at"] is None
    assert claim_next_question(conn)["id"] == question_id


def _delay_sec(row) -> float:
    not_before = datetime.fromisoformat(row["not_before"].replace("Z", "+00:00"))
    return (not_before - datetime.now(UTC)).total_seconds()


def test_create_review_with_delay(conn):
    mr_id = _mr_with_reviews(conn)
    deferred = create_review(conn, mr_id, "push", "a" * 40, "agent-a", delay_sec=120)
    immediate = create_review(conn, mr_id, "new", "a" * 40, "agent-a")

    assert 110 < _delay_sec(get_review(conn, deferred)) <= 120
    assert get_review(conn, immediate)["not_before"] is None


def test_claim_next_review_waits_for_not_before(conn):
    mr_id = _mr_with_reviews(conn)
    deferred = create_review(conn, mr_id, "push", "a" * 40, "agent-a", delay_sec=120)

    assert claim_next_review(conn) is None
    conn.execute(
        "UPDATE review SET not_before = '2000-01-01T00:00:00.000Z' WHERE id = ?", (deferred,)
    )
    conn.commit()
    assert claim_next_review(conn)["id"] == deferred


def test_supersede_with_delay_defers_rebased_review(conn):
    mr_id = _mr_with_reviews(conn)
    queued = create_review(conn, mr_id, "push", "a" * 40, "agent-a", delay_sec=5)
    active = create_review(conn, mr_id, "new", "a" * 40, "agent-a")
    _set_status(conn, active, "reviewing")

    result = supersede_reviews(conn, mr_id, "b" * 40, delay_sec=300)

    assert result.rebased == [queued]
    assert result.cancelled_active == [active]
    assert 290 < _delay_sec(get_review(conn, queued)) <= 300
    assert get_review(conn, active)["not_before"] is None


def test_retry_review_clears_not_before(conn):
    mr_id = _mr_with_reviews(conn)
    review_id = create_review(conn, mr_id, "push", "a" * 40, "agent-a", delay_sec=300)
    finish_review(conn, review_id, "cancelled", error="x")

    assert retry_review(conn, review_id) is True
    assert get_review(conn, review_id)["not_before"] is None


def test_rebase_and_defer_review(conn):
    mr_id = _mr_with_reviews(conn)
    review_id = create_review(conn, mr_id, "new", "a" * 40, "agent-a")
    conn.execute(
        "UPDATE review SET extra_instructions = 'focus', attempt = 2, status = 'cloning', "
        "started_at = 'x' WHERE id = ?",
        (review_id,),
    )
    conn.commit()

    assert rebase_and_defer_review(conn, review_id, "b" * 40, delay_sec=60) is True

    row = get_review(conn, review_id)
    assert row["status"] == "queued"
    assert row["head_sha"] == "b" * 40
    assert row["attempt"] == 0
    assert row["started_at"] is None
    assert row["extra_instructions"] == "focus"
    assert 50 < _delay_sec(row) <= 60


def test_rebase_and_defer_review_conflict_and_finished(conn):
    mr_id = _mr_with_reviews(conn)
    review_id = create_review(conn, mr_id, "new", "a" * 40, "agent-a")
    create_review(conn, mr_id, "new", "b" * 40, "agent-a")

    assert rebase_and_defer_review(conn, review_id, "b" * 40) is False
    assert get_review(conn, review_id)["head_sha"] == "a" * 40

    finish_review(conn, review_id, "done", review_md="ok")
    assert rebase_and_defer_review(conn, review_id, "c" * 40) is False
    assert get_review(conn, review_id)["status"] == "done"
