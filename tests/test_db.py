import sqlite3
from pathlib import Path

import pytest

from helpers import list_reviews
from mrwatch.db import (
    MAX_REVIEW_MD_LENGTH,
    backup,
    claim_next_review,
    connect,
    create_review,
    finish_review,
    get_review,
    init_schema,
    list_mrs,
    poll_state_get,
    poll_state_set,
    requeue_incomplete,
    retry_review,
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
    assert dest.name.startswith("mrwatch-")
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
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 9
    assert "my_roles" in mr_columns
    conn.close()


def test_upsert_mr_stores_author_name(tmp_path):
    from mrwatch.db import connect, init_schema, sync_projects, upsert_mr

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
    from mrwatch.db import _migrate_to_v5, _run_migration

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
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 9
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
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 9
    conn.execute("PRAGMA user_version = 5")
    conn.commit()
    init_schema(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 9
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
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 9
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
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 9
    conn.execute("PRAGMA user_version = 7")
    conn.commit()
    init_schema(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 9
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
