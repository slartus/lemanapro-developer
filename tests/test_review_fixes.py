from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from mrwatch import main as main_module
from mrwatch.agents import AgentError, validate_agent
from mrwatch.db import (
    SCHEMA_VERSION,
    connect,
    create_review,
    get_review,
    init_schema,
    list_reviews,
    poll_state_get,
    rebase_review_head,
)
from mrwatch.git_ops import GitOpsError, sanitize_clone
from mrwatch.opencode_runner import _build_env
from mrwatch.web.routes import safe_url

from tests.test_poller import CURSOR_KEY, StubApi, _config, _make_poller, _mr, conn  # noqa: F401
from tests.test_web_main import _client, ctx  # noqa: F401
from tests.test_worker import _git, _worker, _queued_review, db, origin  # noqa: F401


def test_sanitize_removes_nested_context_files(tmp_path):
    ws = tmp_path / "ws"
    (ws / "src" / "deep").mkdir(parents=True)
    (ws / ".git").mkdir()
    (ws / "CONTEXT.md").write_text("x")
    (ws / "src" / "AGENTS.md").write_text("x")
    (ws / "src" / "deep" / "CLAUDE.md").write_text("x")
    (ws / "src" / "keep.md").write_text("x")
    sanitize_clone(ws)
    assert not (ws / "CONTEXT.md").exists()
    assert not (ws / "src" / "AGENTS.md").exists()
    assert not (ws / "src" / "deep" / "CLAUDE.md").exists()
    assert (ws / "src" / "keep.md").exists()


def test_sanitize_removes_nested_symlink_without_following(tmp_path):
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "AGENTS.md").write_text("keep")
    (ws / "src" / "AGENTS.md").symlink_to(outside)
    sanitize_clone(ws)
    assert not (ws / "src" / "AGENTS.md").is_symlink()
    assert (outside / "AGENTS.md").exists()


def test_sanitize_fails_closed_on_unreadable_dir(tmp_path):
    ws = tmp_path / "ws"
    locked = ws / "locked"
    locked.mkdir(parents=True)
    locked.chmod(0)
    try:
        with pytest.raises(OSError):
            sanitize_clone(ws)
    finally:
        locked.chmod(0o700)


def test_build_env_disables_project_config(tmp_path):
    env = _build_env("/home/x", tmp_path)
    assert env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1"
    assert env["OPENCODE_DISABLE_CLAUDE_CODE"] == "1"


def _agent(tmp_path, text):
    (tmp_path / "a.md").write_text(text)
    validate_agent("a", (tmp_path,))


def test_frontmatter_with_dashes_in_body_and_values(tmp_path):
    _agent(
        tmp_path,
        "---\ndescription: a --- b\ntools:\n  edit: false\n  bash: false\n  webfetch: false\n---\nbody --- x\n",
    )


def test_frontmatter_not_closed(tmp_path):
    with pytest.raises(AgentError):
        _agent(tmp_path, "---\ntools:\n  edit: false\n")


def test_conflicting_tools_false_and_permission_allow_not_readonly(tmp_path):
    text = (
        "---\ntools:\n  edit: false\n  bash: false\n  webfetch: false\n"
        "permission:\n  bash: allow\n---\n"
    )
    with pytest.raises(AgentError):
        _agent(tmp_path, text)


async def test_empty_full_scan_sets_cursor(conn):
    poller = _make_poller(conn, StubApi())
    before = time.time()
    await poller.tick()
    cursor = poll_state_get(conn, CURSOR_KEY)
    assert cursor is not None
    from datetime import datetime
    assert abs(datetime.fromisoformat(cursor).timestamp() - (before - 120)) < 5


async def test_push_review_skipped_when_head_already_reviewed(conn):
    api = StubApi()
    api.mrs = [_mr(10, 1, author="me")]
    poller = _make_poller(conn, api, _config(re_review_on_push=True))
    await poller.tick()
    review = list_reviews(conn)[0]
    assert rebase_review_head(conn, review["id"], "b" * 40)
    api.mrs = [_mr(10, 1, sha="b" * 40, author="me")]
    assert await poller.tick() == 0
    assert len(list_reviews(conn)) == 1


async def test_backup_due_logic(tmp_path):
    backups = tmp_path / "backups"
    assert main_module._backup_due(backups, 86400)
    backups.mkdir()
    f = backups / "mrwatch-1.db"
    f.write_text("x")
    assert not main_module._backup_due(backups, 86400)
    assert main_module._backup_due(backups, 86400, now=time.time() + 90000)


async def test_backup_loop_waits_when_recent_backup(tmp_path, monkeypatch):
    import asyncio
    path = tmp_path / "data" / "m.db"
    path.parent.mkdir()
    (path.parent / "backups").mkdir()
    (path.parent / "backups" / "mrwatch-1.db").write_text("x")
    c = connect(path)
    init_schema(c)
    ctx_obj = main_module.AppContext(
        conn=c, config=None, paths=type("P", (), {"db": path})(), api=None, project_ids={}
    )
    task = asyncio.create_task(main_module._backup_loop(ctx_obj))
    await asyncio.sleep(0.3)
    task.cancel()
    c.close()
    assert len(list((path.parent / "backups").glob("mrwatch-*.db"))) == 1


async def test_base_sha_uses_diff_refs_when_head_matches(tmp_path, db, origin):
    worker = _worker(tmp_path, db, origin, "/bin/true")
    got = await worker._resolve_base_sha(tmp_path, {"sha": "h"}, {"base_sha": "b" * 40}, "h")
    assert got == "b" * 40


async def test_base_sha_uses_merge_base_when_head_moved(tmp_path, db, origin):
    worker = _worker(tmp_path, db, origin, "/bin/true")
    ws = tmp_path / "clone"
    _git("clone", "-q", str(origin), str(ws))
    head = _git("rev-parse", "origin/feature", cwd=ws).strip()
    expected = _git("rev-parse", "origin/main", cwd=ws).strip()
    got = await worker._resolve_base_sha(
        ws, {"sha": "c" * 40, "target_branch": "main"}, {"base_sha": "d" * 40}, head
    )
    assert got == expected


def test_safe_url():
    gl = "https://gitlab.example.com"
    assert safe_url("https://gitlab.example.com/g/p/-/mr/1", gl) == "https://gitlab.example.com/g/p/-/mr/1"
    assert safe_url("javascript:alert(1)", gl) == ""
    assert safe_url("https://evil.example.org/x", gl) == ""
    assert safe_url("//gitlab.example.com/x", gl) == ""
    assert safe_url(None, gl) == ""


async def test_pages_do_not_render_foreign_links(ctx):
    ctx_obj, review_id = ctx
    ctx_obj.conn.execute("UPDATE mr SET web_url = 'javascript:alert(1)'")
    ctx_obj.conn.commit()
    async with _client(ctx_obj) as client:
        index = await client.get("/")
        detail = await client.get(f"/reviews/{review_id}")
    assert "javascript:" not in index.text.split("<script")[0]
    assert 'href="javascript:' not in detail.text


async def test_index_renders_gitlab_link(ctx):
    ctx_obj, _ = ctx
    async with _client(ctx_obj) as client:
        index = await client.get("/")
    assert 'href="https://gitlab.example.com/g/p/-/mr/1"' in index.text


async def test_settings_has_no_pull_agents(ctx, tmp_path, monkeypatch):
    ctx_obj, _ = ctx
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        "gitlab:\n  url: https://gitlab.example.com\n  pat_command: printf tok\n"
        "projects:\n  - gitlab_project_id: 10\n    slug: g/p\n    agent: agent-a\n"
    )
    monkeypatch.setattr("mrwatch.web.routes._config_path", lambda: cfg)
    async with _client(ctx_obj) as client:
        resp = await client.get("/settings")
    assert "pullAgents" not in resp.text
    assert "из репо" not in resp.text


def test_migration_rerun_after_partial_apply(tmp_path):
    conn = connect(tmp_path / "m.db")
    init_schema(conn)
    conn.execute("PRAGMA user_version = 1")
    conn.execute("ALTER TABLE review DROP COLUMN not_before")
    conn.commit()
    init_schema(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(review)")}
    assert {"not_before", "crit_count"} <= cols
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_failed_migration_rolls_back(tmp_path):
    conn = connect(tmp_path / "m.db")
    init_schema(conn)
    conn.execute("PRAGMA user_version = 3")

    def broken(c):
        c.execute("CREATE TABLE tmp_x (a)")
        raise RuntimeError("boom")

    from mrwatch.db import _run_migration
    with pytest.raises(RuntimeError):
        _run_migration(conn, 4, broken)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'tmp_x'").fetchone() is None
