from __future__ import annotations

import asyncio
import os
import sqlite3
import stat
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from mrwatch import main as main_module
from mrwatch import opencode_runner, poller as poller_module, worker as worker_module
from mrwatch.config import (
    Config,
    GitlabConfig,
    OpenCodeConfig,
    PathsConfig,
    ProjectConfig,
    ReviewConfig,
    resolve_paths,
)
from mrwatch.db import (
    MR_CURSOR_KEY,
    SCAN_DONE_KEY,
    backup,
    claim_next_review,
    connect,
    create_review,
    finish_review,
    get_review,
    init_schema,
    poll_state_get,
    poll_state_set,
    rebase_review_head,
    requeue_review,
    retry_review,
    sync_projects,
    todo_mark_seen,
    todo_seen_ids,
    upsert_mr,
)
from mrwatch.git_ops import (
    GitEnv,
    GitOpsError,
    HeadMismatchError,
    cleanup_old_review_logs,
    cleanup_old_workspaces,
    cleanup_stale_askpass,
    clone_for_review,
    remove_workspace,
    run_git,
)
from mrwatch.gitlab_client import GitlabApiError
from mrwatch.main import AppContext, create_app
from mrwatch.opencode_runner import RunnerEnv, run_review
from mrwatch.poller import Poller

from tests.test_poller import StubApi, _config as poller_config, _mr, _detail
from tests.test_worker import (
    _fake_opencode,
    _git,
    _queued_review,
    _worker,
    origin,  # noqa: F401
)

USER_ENV = {"PATH": "/usr/bin:/bin", "HOME": str(Path.home())}
NO_ENV = GitEnv(pat_command="", base_env=USER_ENV)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _wait_for(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


async def _wait_dead(pid: int) -> None:
    await _wait_for(lambda: not _pid_alive(pid), timeout=10.0)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "test.db")
    init_schema(c)
    yield c
    c.close()


@pytest.fixture
def db(tmp_path):
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    c = connect(tmp_path / "data" / "mrwatch.db")
    init_schema(c)
    yield c
    c.close()


def _seed(conn, enabled=True, sha="a" * 40):
    mapping = sync_projects(conn, [(10, "g/p", "agent-a", False, enabled)])
    result = upsert_mr(
        conn, mapping[10], 1, "t", "u", "b", "http://x", "opened", sha, "2026-01-01T12:00:00Z"
    )
    return mapping, result.mr_id


# ---------- backup ----------

async def test_backup_works_through_to_thread(conn, tmp_path):
    _, mr_id = _seed(conn)
    review_id = create_review(conn, mr_id, "new", "a" * 40, "agent-a")
    finish_review(conn, review_id, "done", review_md="ok")

    dest = await asyncio.to_thread(backup, tmp_path / "test.db", tmp_path / "backups")

    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    check = sqlite3.connect(dest)
    try:
        assert check.execute("SELECT review_md FROM review").fetchone()[0] == "ok"
    finally:
        check.close()


async def test_backup_loop_writes_file(tmp_path, monkeypatch):
    path = tmp_path / "data" / "m.db"
    path.parent.mkdir()
    c = connect(path)
    init_schema(c)
    ctx = AppContext(
        conn=c, config=None, paths=type("P", (), {"db": path})(), api=None, project_ids={}
    )
    monkeypatch.setattr(main_module, "BACKUP_INTERVAL_SEC", 0)
    task = asyncio.create_task(main_module._backup_loop(ctx))
    await _wait_for(lambda: any((path.parent / "backups").glob("mrwatch-*.db")))
    task.cancel()
    c.close()


# ---------- cancellation ----------

def _fake_git_bin(tmp_path: Path, body: str) -> GitEnv:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "git"
    script.write_text(body)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return GitEnv(pat_command="", base_env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(Path.home())})


async def test_run_git_cancel_kills_process_group(tmp_path):
    pid_file = tmp_path / "pid"
    child_file = tmp_path / "child"
    env = _fake_git_bin(
        tmp_path, f"#!/bin/sh\nsleep 60 &\necho $! > {child_file}\necho $$ > {pid_file}\nwait\n"
    )
    task = asyncio.create_task(run_git(["git", "status"], tmp_path, env, harden=False))
    await _wait_for(lambda: pid_file.exists() and child_file.read_text().strip() != "")
    pid, child = int(pid_file.read_text()), int(child_file.read_text())

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await _wait_dead(pid)
    await _wait_dead(child)


async def test_run_git_output_limit_kills(tmp_path, monkeypatch):
    from mrwatch import git_ops

    monkeypatch.setattr(git_ops, "GIT_HARD_LIMIT_BYTES", 10_000)
    pid_file = tmp_path / "pid"
    env = _fake_git_bin(tmp_path, f"#!/bin/sh\necho $$ > {pid_file}\nexec yes\n")

    with pytest.raises(GitOpsError, match="exceeded"):
        await run_git(["git", "status"], tmp_path, env, harden=False)

    await _wait_dead(int(pid_file.read_text()))


async def test_run_review_cancel_kills_opencode(tmp_path):
    pid_file = tmp_path / "pid"
    bin_path = _fake_opencode(tmp_path, f"#!/bin/sh\necho $$ > {pid_file}\nexec sleep 60\n")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (tmp_path / "tmp").mkdir()
    task = asyncio.create_task(run_review(
        env=RunnerEnv(opencode_bin=bin_path), workspace=workspace, agent="a", prompt="p",
        timeout_sec=60, log_path=tmp_path / "logs" / "r.log", tmpdir=tmp_path / "tmp",
    ))
    await _wait_for(pid_file.exists)
    pid = int(pid_file.read_text())

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await _wait_dead(pid)


async def test_run_review_output_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(opencode_runner, "OUTPUT_HARD_LIMIT_BYTES", 10_000)
    pid_file = tmp_path / "pid"
    bin_path = _fake_opencode(tmp_path, f"#!/bin/sh\necho $$ > {pid_file}\nexec yes\n")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (tmp_path / "tmp").mkdir()

    result = await run_review(
        env=RunnerEnv(opencode_bin=bin_path), workspace=workspace, agent="a", prompt="p",
        timeout_sec=60, log_path=tmp_path / "logs" / "r.log", tmpdir=tmp_path / "tmp",
    )

    assert "exceeded" in result.error
    await _wait_dead(int(pid_file.read_text()))


async def test_worker_cancel_kills_opencode_and_removes_workspace(tmp_path, db, origin):
    review_id = _queued_review(db, origin)
    pid_file = tmp_path / "pid"
    bin_path = _fake_opencode(tmp_path, f"#!/bin/sh\necho $$ > {pid_file}\nexec sleep 60\n")
    worker = _worker(tmp_path, db, origin, bin_path)
    task = asyncio.create_task(worker.process_one())
    await _wait_for(pid_file.exists, timeout=20)
    pid = int(pid_file.read_text())

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not _pid_alive(pid) or await _wait_dead(pid) is None
    assert not (tmp_path / "workspaces" / "10").exists()
    assert get_review(db, review_id)["status"] == "reviewing"


# ---------- supervision ----------

async def test_worker_loop_survives_exceptions(tmp_path, db, origin, monkeypatch):
    monkeypatch.setattr(worker_module, "ERROR_PAUSE_SEC", 0.01)
    worker = _worker(tmp_path, db, origin, "/bin/true")
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 4:
            raise RuntimeError("boom")
        await asyncio.sleep(0.01)
        return False

    worker.process_one = flaky
    monkeypatch.setattr(worker_module, "IDLE_SLEEP_SEC", 0.01)
    task = asyncio.create_task(worker.run())
    await _wait_for(lambda: calls["n"] >= 6)
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_worker_wakes_on_event(tmp_path, db, origin, monkeypatch):
    monkeypatch.setattr(worker_module, "IDLE_SLEEP_SEC", 30.0)
    event = asyncio.Event()
    worker = _worker(tmp_path, db, origin, "/bin/true")
    worker._work_event = event
    calls = {"n": 0}

    async def counting():
        calls["n"] += 1
        return False

    worker.process_one = counting
    task = asyncio.create_task(worker.run())
    await _wait_for(lambda: calls["n"] == 1)
    event.set()
    await _wait_for(lambda: calls["n"] == 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_poller_loop_survives_error_recording_failure(conn, monkeypatch):
    api = StubApi()
    api.fail_todos = True
    config = poller_config()
    config = config.model_copy(
        update={"gitlab": config.gitlab.model_copy(update={"poll_interval_sec": 1})}
    )
    mapping = sync_projects(conn, [(10, "g/p", "agent-a", False, True)])
    p = Poller(conn, api, config, mapping)
    monkeypatch.setattr(poller_module, "ERROR_PAUSE_SEC", 0.01)
    monkeypatch.setattr(
        poller_module, "poll_state_set",
        lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("locked")),
    )
    ticks = {"n": 0}
    original = p.tick

    async def counting():
        ticks["n"] += 1
        return await original()

    p.tick = counting
    p._config = config.model_copy(
        update={"gitlab": config.gitlab.model_copy(update={"poll_interval_sec": 1})}
    )
    task = asyncio.create_task(p.run())
    await _wait_for(lambda: ticks["n"] >= 1)
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_supervise_logs_dead_task(caplog):
    async def die():
        raise RuntimeError("kaput")

    task = main_module._supervise(asyncio.create_task(die(), name="mrwatch-test"))
    with caplog.at_level("ERROR"):
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
    assert any("mrwatch-test" in r.getMessage() and "died" in r.getMessage() for r in caplog.records)


async def test_supervise_ignores_cancelled(caplog):
    task = main_module._supervise(asyncio.create_task(asyncio.sleep(10), name="mrwatch-c"))
    await asyncio.sleep(0)
    task.cancel()
    with caplog.at_level("ERROR"):
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
    assert not caplog.records


def _app_ctx(tmp_path, max_parallel=1):
    config = Config(
        gitlab=GitlabConfig(url="https://gitlab.example.com", pat_command="printf tok"),
        projects=[ProjectConfig(gitlab_project_id=10, slug="g/p", agent="agent-a")],
        review=ReviewConfig(max_parallel=max_parallel),
        paths=PathsConfig(
            db=str(tmp_path / "data" / "mrwatch.db"),
            workspaces=str(tmp_path / "workspaces"),
            logs=str(tmp_path / "logs"),
        ),
    )
    paths = resolve_paths(config)
    c = connect(paths.db)
    init_schema(c)
    mapping = sync_projects(c, [(10, "g/p", "agent-a", False, True)])
    from mrwatch.gitlab_client import GitLabClient

    return AppContext(
        conn=c, config=config, paths=paths,
        api=GitLabClient(config.gitlab.url, "tok"), project_ids=mapping,
    )


def _http(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8123")


async def test_healthz_503_when_task_dead(tmp_path):
    ctx = _app_ctx(tmp_path)
    app = create_app(ctx)

    async def die():
        return None

    dead = asyncio.create_task(die(), name="mrwatch-worker-0")
    await dead
    alive = asyncio.create_task(asyncio.sleep(30), name="mrwatch-poller")
    app.state.tasks = [dead, alive]
    async with _http(app) as client:
        response = await client.get("/healthz")
    alive.cancel()
    body = response.json()
    assert response.status_code == 503
    assert body["status"] == "degraded"
    assert body["dead_tasks"] == ["mrwatch-worker-0"]
    assert {"last_poll", "last_error"} <= set(body)


async def test_healthz_ok_when_tasks_alive(tmp_path):
    ctx = _app_ctx(tmp_path)
    app = create_app(ctx)
    alive = asyncio.create_task(asyncio.sleep(30), name="mrwatch-poller")
    app.state.tasks = [alive]
    async with _http(app) as client:
        response = await client.get("/healthz")
    alive.cancel()
    assert response.status_code == 200
    assert response.json()["dead_tasks"] == []


async def test_lifespan_starts_supervised_tasks(tmp_path):
    ctx = _app_ctx(tmp_path, max_parallel=2)
    app = create_app(ctx)
    async with app.router.lifespan_context(app):
        assert len(app.state.tasks) == 4
        assert app.state.worker_count == 2
        assert isinstance(app.state.work_event, asyncio.Event)
    assert all(t.done() for t in app.state.tasks)


# ---------- head_sha ----------

def _advance_mr_head(origin: Path) -> tuple[str, str]:
    old = _git("rev-parse", "merge-requests/1/head", cwd=origin).strip()
    _git("checkout", "-q", "feature", cwd=origin)
    (origin / "app.py").write_text("print('second')\n")
    _git("add", ".", cwd=origin)
    _git("commit", "-qm", "more", cwd=origin)
    new = _git("rev-parse", "feature", cwd=origin).strip()
    _git("checkout", "-q", "main", cwd=origin)
    _git("update-ref", "refs/merge-requests/1/head", new, cwd=origin)
    return old, new


async def test_clone_checks_out_expected_old_sha(origin, tmp_path):
    old, new = _advance_mr_head(origin)
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()

    ws = await clone_for_review(
        tmp_path / "ws", str(origin), 1, "main", 5, NO_ENV, tmpdir, harden=False, expected_sha=old
    )

    assert _git("rev-parse", "HEAD", cwd=ws).strip() == old


async def test_clone_head_mismatch_reports_actual_and_cleans(origin, tmp_path):
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    actual = _git("rev-parse", "merge-requests/1/head", cwd=origin).strip()

    with pytest.raises(HeadMismatchError) as exc_info:
        await clone_for_review(
            tmp_path / "ws", str(origin), 1, "main", 5, NO_ENV, tmpdir,
            harden=False, expected_sha="f" * 40,
        )

    assert exc_info.value.actual == actual
    assert not (tmp_path / "ws").exists()


async def test_clone_cancel_removes_workspace(tmp_path):
    env = _fake_git_bin(tmp_path, "#!/bin/sh\nexec sleep 60\n")
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    task = asyncio.create_task(clone_for_review(
        tmp_path / "ws", "x", 1, None, 5, env, tmpdir, harden=False,
    ))
    await _wait_for(lambda: (tmp_path / "ws").exists())
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not (tmp_path / "ws").exists()


async def test_worker_uses_review_head_sha_when_head_moved(tmp_path, db, origin):
    review_id = _queued_review(db, origin)
    old, new = _advance_mr_head(origin)
    out = '{"sessionID":"s","part":{"type":"text","text":"OK"}}'
    bin_path = _fake_opencode(tmp_path, f"#!/bin/sh\nprintf '%s' '{out}'\n")
    worker = _worker(tmp_path, db, origin, bin_path)
    worker._api.detail["sha"] = new

    await worker.process_one()

    review = get_review(db, review_id)
    assert review["status"] == "done"
    assert review["head_sha"] == old


async def test_worker_rebases_review_to_current_head_when_old_sha_gone(tmp_path, db, origin, monkeypatch):
    monkeypatch.setattr(worker_module, "CLONE_RETRY_BACKOFF_SEC", 0.0)
    review_id = _queued_review(db, origin)
    db.execute("UPDATE review SET head_sha = ? WHERE id = ?", ("f" * 40, review_id))
    db.commit()
    actual = _git("rev-parse", "merge-requests/1/head", cwd=origin).strip()
    out = '{"sessionID":"s","part":{"type":"text","text":"OK"}}'
    bin_path = _fake_opencode(tmp_path, f"#!/bin/sh\nprintf '%s' '{out}'\n")
    worker = _worker(tmp_path, db, origin, bin_path)

    await worker.process_one()
    row = get_review(db, review_id)
    assert row["status"] == "queued"
    assert row["head_sha"] == actual

    await worker.process_one()
    assert get_review(db, review_id)["status"] == "done"


async def test_worker_head_moved_duplicate_review_errors(tmp_path, db, origin):
    review_id = _queued_review(db, origin)
    actual = _git("rev-parse", "merge-requests/1/head", cwd=origin).strip()
    db.execute("UPDATE review SET head_sha = ? WHERE id = ?", ("f" * 40, review_id))
    db.commit()
    other = create_review(db, 1, "new", actual, "agent-a")
    assert other is not None
    worker = _worker(tmp_path, db, origin, "/bin/true")

    await worker.process_one()

    row = get_review(db, review_id)
    assert row["status"] == "error"
    assert "already exists" in row["error"]


def test_rebase_review_head_unique_conflict(conn):
    _, mr_id = _seed(conn)
    first = create_review(conn, mr_id, "new", "a" * 40, "agent-a")
    second = create_review(conn, mr_id, "new", "b" * 40, "agent-a")

    assert rebase_review_head(conn, second, "c" * 40) is True
    assert rebase_review_head(conn, second, "a" * 40) is False
    assert get_review(conn, second)["head_sha"] == "c" * 40
    assert first is not None


# ---------- hot reload ----------

async def test_worker_reads_config_from_provider(tmp_path, db, origin):
    _queued_review(db, origin)
    argv_log = tmp_path / "argv.txt"
    out = '{"sessionID":"s","part":{"type":"text","text":"OK"}}'
    bin_path = _fake_opencode(
        tmp_path, f"#!/bin/sh\necho \"$@\" >> {argv_log}\nprintf '%s' '{out}'\n"
    )
    worker = _worker(tmp_path, db, origin, bin_path)
    fresh = worker._static_config.model_copy(deep=True)
    fresh.opencode.model = "hot/reloaded"
    holder = {"config": worker._static_config}
    worker._config_provider = lambda: holder["config"]
    holder["config"] = fresh

    await worker.process_one()

    assert "--model hot/reloaded" in argv_log.read_text()


def test_opencode_bin_expanded_by_validator():
    assert OpenCodeConfig(bin="~/x/oc").bin == str(Path.home() / "x" / "oc")
    assert OpenCodeConfig().bin == str(Path.home() / ".opencode" / "bin" / "opencode")
    raw = {
        "gitlab": {"url": "https://g.example.com"},
        "projects": [{"gitlab_project_id": 1, "slug": "a/b", "agent": "a"}],
        "opencode": {"bin": "~/bin/oc"},
    }
    assert Config.model_validate(raw).opencode.bin == str(Path.home() / "bin" / "oc")


async def _post_settings(ctx, tmp_path, monkeypatch, payload, app=None):
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        "gitlab:\n  url: https://gitlab.example.com\n  poll_interval_sec: 60\n"
        "projects:\n  - gitlab_project_id: 10\n    slug: g/p\n    agent: agent-a\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MRWATCH_CONFIG", str(cfg))
    app = app or create_app(ctx)
    async with _http(app) as client:
        return await client.post(
            "/settings", json=payload, headers={"X-Requested-With": "mrwatch"}
        ), cfg


PROJECTS = [{"gitlab_project_id": 10, "slug": "g/p", "agent": "agent-a", "re_review_on_push": False}]


async def test_settings_max_parallel_change_needs_restart(tmp_path, monkeypatch, caplog):
    ctx = _app_ctx(tmp_path)
    app = create_app(ctx)
    app.state.worker_count = 1
    payload = {"gitlab": {}, "review": {"max_parallel": 4}, "projects": PROJECTS}

    with caplog.at_level("WARNING"):
        resp, _ = await _post_settings(ctx, tmp_path, monkeypatch, payload, app)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/settings?saved=1&restart=1"
    assert any("restart required" in r.getMessage() for r in caplog.records)
    assert ctx.config.review.max_parallel == 4


async def test_settings_same_max_parallel_no_restart_flag(tmp_path, monkeypatch):
    ctx = _app_ctx(tmp_path)
    app = create_app(ctx)
    app.state.worker_count = 1
    payload = {"gitlab": {}, "review": {"max_parallel": 1}, "projects": PROJECTS}

    resp, _ = await _post_settings(ctx, tmp_path, monkeypatch, payload, app)

    assert resp.headers["location"] == "/settings?saved=1"


async def test_settings_page_shows_restart_notice(tmp_path, monkeypatch):
    ctx = _app_ctx(tmp_path)
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        "gitlab:\n  url: https://gitlab.example.com\nprojects:\n"
        "  - gitlab_project_id: 10\n    slug: g/p\n    agent: agent-a\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MRWATCH_CONFIG", str(cfg))
    async with _http(create_app(ctx)) as client:
        plain = await client.get("/settings?saved=1")
        restart = await client.get("/settings?saved=1&restart=1")
    assert "после перезапуска" not in plain.text
    assert "после перезапуска" in restart.text


async def test_settings_save_normalizes_opencode_bin(tmp_path, monkeypatch):
    ctx = _app_ctx(tmp_path)
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        "gitlab:\n  url: https://gitlab.example.com\nprojects:\n"
        "  - gitlab_project_id: 10\n    slug: g/p\n    agent: agent-a\n"
        "opencode:\n  bin: ~/custom/opencode\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MRWATCH_CONFIG", str(cfg))
    payload = {"gitlab": {}, "review": {}, "projects": PROJECTS}
    async with _http(create_app(ctx)) as client:
        resp = await client.post("/settings", json=payload, headers={"X-Requested-With": "mrwatch"})
    assert resp.status_code == 303
    assert ctx.config.opencode.bin == str(Path.home() / "custom" / "opencode")


# ---------- db ----------

def test_retry_review_resets_attempt(conn):
    _, mr_id = _seed(conn)
    review_id = create_review(conn, mr_id, "new", "a" * 40, "agent-a")
    claim_next_review(conn)
    finish_review(conn, review_id, "error", error="x")
    assert get_review(conn, review_id)["attempt"] == 1

    assert retry_review(conn, review_id) is True

    row = get_review(conn, review_id)
    assert row["attempt"] == 0
    assert row["not_before"] is None


def test_requeue_backoff_blocks_claim_until_expired(conn):
    _, mr_id = _seed(conn)
    review_id = create_review(conn, mr_id, "new", "a" * 40, "agent-a")
    claim_next_review(conn)

    requeue_review(conn, review_id, delay_sec=60)
    assert claim_next_review(conn) is None

    requeue_review(conn, review_id, delay_sec=0)
    assert claim_next_review(conn)["id"] == review_id


def test_backoff_does_not_block_other_reviews(conn):
    mapping, mr_id = _seed(conn)
    first = create_review(conn, mr_id, "new", "a" * 40, "agent-a")
    second = create_review(conn, mr_id, "push", "a" * 40, "agent-a")
    claim_next_review(conn)
    requeue_review(conn, first, delay_sec=60)

    assert claim_next_review(conn)["id"] == second


def test_claim_skips_disabled_project(conn):
    mapping, mr_id = _seed(conn)
    create_review(conn, mr_id, "new", "a" * 40, "agent-a")
    sync_projects(conn, [(99, "g/o", "agent-a", False, True)])

    assert claim_next_review(conn) is None

    sync_projects(conn, [(10, "g/p", "agent-a", False, True)])
    assert claim_next_review(conn) is not None


def test_review_mr_index_exists(conn):
    names = {r[1] for r in conn.execute("PRAGMA index_list(review)")}
    assert "idx_review_mr" in names


def test_migration_v3_to_v4_moves_legacy_cursor(tmp_path):
    c = connect(tmp_path / "old.db")
    init_schema(c)
    sync_projects(c, [(10, "g/p", "a", False, True), (20, "g/q", "a", False, True)])
    c.execute("DROP INDEX idx_review_mr")
    c.execute("DROP TABLE todo_seen")
    c.execute("ALTER TABLE review DROP COLUMN not_before")
    poll_state_set(c, "mr_cursor", "2026-01-01T00:00:00+00:00")
    poll_state_set(c, SCAN_DONE_KEY.format(10), "x")
    c.execute("PRAGMA user_version = 3")
    c.commit()

    init_schema(c)

    assert poll_state_get(c, MR_CURSOR_KEY.format(10)) == "2026-01-01T00:00:00+00:00"
    assert poll_state_get(c, MR_CURSOR_KEY.format(20)) is None
    assert poll_state_get(c, "mr_cursor") is None
    assert c.execute("PRAGMA user_version").fetchone()[0] == 8
    assert {r[1] for r in c.execute("PRAGMA table_info(review)")} >= {"not_before"}
    c.close()


def test_todo_seen_roundtrip(conn):
    todo_mark_seen(conn, 5)
    todo_mark_seen(conn, 5)
    assert todo_seen_ids(conn) == {5}


# ---------- poller ----------

def _poller(conn, api, re_review=False, work_event=None):
    config = poller_config(re_review_on_push=re_review)
    mapping = sync_projects(conn, [(10, "g/p", "agent-a", re_review, True)])
    return Poller(conn, api, config, mapping, work_event=work_event)


def _reviews(conn):
    return conn.execute("SELECT * FROM review ORDER BY id").fetchall()


async def test_todo_not_reprocessed_and_no_mr_refetch(conn):
    api = StubApi()
    api.details[(10, 5)] = _detail(10, 5, reviewers=["me"])
    api.todos = [{"id": 77, "target_type": "MergeRequest", "action_name": "mentioned",
                  "target": {"project_id": 10, "iid": 5}}]
    fetched = []
    original = api.get_project_mr

    async def spy(pid, iid):
        fetched.append((pid, iid))
        return await original(pid, iid)

    api.get_project_mr = spy
    p = _poller(conn, api)

    assert await p.tick() == 1
    assert await p.tick() == 0
    assert fetched == [(10, 5)]
    assert not hasattr(api, "mark_todo_done")


async def test_todo_for_closed_mr_makes_no_review(conn):
    api = StubApi()
    api.details[(10, 5)] = _detail(10, 5, reviewers=["me"], state="merged")
    api.todos = [{"id": 80, "target_type": "MergeRequest", "action_name": "mentioned",
                  "target": {"project_id": 10, "iid": 5}}]
    p = _poller(conn, api)

    assert await p.tick() == 0
    assert _reviews(conn) == []
    assert todo_seen_ids(conn) == {80}


async def test_todo_fetch_failure_keeps_todo_pending(conn):
    api = StubApi()
    api.todos = [{"id": 81, "target_type": "MergeRequest", "action_name": "mentioned",
                  "target": {"project_id": 10, "iid": 5}}]
    p = _poller(conn, api)

    assert await p.tick() == 0
    assert todo_seen_ids(conn) == set()


async def test_sweep_cursor_advances_over_foreign_mrs(conn):
    api = StubApi()
    p = _poller(conn, api)
    await p.tick()
    poll_state_set(conn, MR_CURSOR_KEY.format(10), "2026-01-01T00:00:00+00:00")
    api.mrs = [_mr(10, 1, author="other", updated_at="2026-03-01T10:00:00Z")]

    await p.tick()

    cursor = datetime.fromisoformat(poll_state_get(conn, MR_CURSOR_KEY.format(10)))
    assert cursor == datetime(2026, 3, 1, 10, 0, tzinfo=UTC) - timedelta(seconds=120)
    assert api.project_calls[-1] == (10, "2026-01-01T00:00:00+00:00", None)


async def test_cursors_are_per_project(conn):
    api = StubApi()
    config = poller_config()
    config.projects.append(ProjectConfig(gitlab_project_id=20, slug="g/q", agent="agent-a"))
    mapping = sync_projects(
        conn, [(10, "g/p", "agent-a", False, True), (20, "g/q", "agent-a", False, True)]
    )
    p = Poller(conn, api, config, mapping)
    api.mrs = [_mr(10, 1, updated_at="2026-02-01T00:00:00Z"), _mr(20, 2, updated_at="2026-05-01T00:00:00Z")]

    await p.tick()

    c10 = datetime.fromisoformat(poll_state_get(conn, MR_CURSOR_KEY.format(10)))
    c20 = datetime.fromisoformat(poll_state_get(conn, MR_CURSOR_KEY.format(20)))
    assert c10 < c20


async def test_full_scan_does_not_roll_cursor_back(conn):
    api = StubApi()
    p = _poller(conn, api)
    poll_state_set(conn, MR_CURSOR_KEY.format(10), "2026-06-01T00:00:00+00:00")
    api.mrs = [_mr(10, 1, updated_at="2026-01-01T00:00:00Z")]

    await p.tick()

    assert poll_state_get(conn, MR_CURSOR_KEY.format(10)) == "2026-06-01T00:00:00+00:00"


async def test_head_sha_none_mr_skipped_tick_survives(conn):
    api = StubApi()
    broken = _mr(10, 1, author="me")
    broken["sha"] = None
    api.mrs = [broken, _mr(10, 2, author="me")]
    p = _poller(conn, api)

    assert await p.tick() == 1
    assert [r["head_sha"] for r in _reviews(conn)] == ["a" * 40]
    iids = [r["iid"] for r in conn.execute("SELECT iid FROM mr")]
    assert iids == [2]


async def test_sweep_head_sha_none_skipped(conn):
    api = StubApi()
    p = _poller(conn, api)
    await p.tick()
    broken = _mr(10, 1, author="me")
    broken["sha"] = None
    api.mrs = [broken]

    assert await p.tick() == 0


async def test_single_mr_failure_does_not_abort_tick(conn, monkeypatch):
    api = StubApi()
    api.mrs = [_mr(10, 1, author="me"), _mr(10, 2, author="me")]
    p = _poller(conn, api)
    original = p._enqueue
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db hiccup")
        return original(*args, **kwargs)

    p._enqueue = flaky

    assert await p.tick() == 1


async def test_author_null_does_not_crash(conn):
    api = StubApi()
    mr = _mr(10, 1, reviewers=["me"])
    mr["author"] = None
    api.mrs = [mr]
    p = _poller(conn, api)

    assert await p.tick() == 1

    api.mrs = [dict(mr, updated_at="2026-02-01T00:00:00Z", sha="b" * 40)]
    await p.tick()


async def test_push_on_closed_mr_no_review(conn):
    api = StubApi()
    api.mrs = [_mr(10, 1, author="me")]
    p = _poller(conn, api, re_review=True)
    await p.tick()
    api.mrs = [dict(_mr(10, 1, sha="b" * 40, author="me", updated_at="2026-02-01T00:00:00Z"), state="closed")]

    assert await p.tick() == 0
    assert [r["trigger"] for r in _reviews(conn)] == ["new"]


async def test_sweep_error_propagates_after_other_projects(conn):
    api = StubApi()
    config = poller_config()
    config.projects.append(ProjectConfig(gitlab_project_id=20, slug="g/q", agent="agent-a"))
    mapping = sync_projects(
        conn, [(10, "g/p", "agent-a", False, True), (20, "g/q", "agent-a", False, True)]
    )
    p = Poller(conn, api, config, mapping)
    await p.tick()
    poll_state_set(conn, MR_CURSOR_KEY.format(10), "2026-01-01T00:00:00+00:00")
    poll_state_set(conn, MR_CURSOR_KEY.format(20), "2026-01-01T00:00:00+00:00")
    api.mrs = [_mr(20, 3, author="me")]
    original = api.get_project_merge_requests

    async def failing(project_id, updated_after=None, state="opened"):
        if project_id == 10:
            raise GitlabApiError("down", status=500)
        return await original(project_id, updated_after, state)

    api.get_project_merge_requests = failing

    with pytest.raises(GitlabApiError):
        await p.tick()
    assert len(_reviews(conn)) == 1


async def test_work_event_set_even_if_sweep_raises(conn):
    api = StubApi()
    event = asyncio.Event()
    config = poller_config()
    config.projects.append(ProjectConfig(gitlab_project_id=20, slug="g/q", agent="agent-a"))
    mapping = sync_projects(
        conn, [(10, "g/p", "agent-a", False, True), (20, "g/q", "agent-a", False, True)]
    )
    p = Poller(conn, api, config, mapping, work_event=event)
    await p.tick()
    poll_state_set(conn, MR_CURSOR_KEY.format(10), "2026-01-01T00:00:00+00:00")
    poll_state_set(conn, MR_CURSOR_KEY.format(20), "2026-01-01T00:00:00+00:00")
    api.mrs = [_mr(10, 1, author="me")]
    original = api.get_project_merge_requests

    async def failing(project_id, updated_after=None, state="opened"):
        if project_id == 20:
            raise GitlabApiError("down", status=500)
        return await original(project_id, updated_after, state)

    api.get_project_merge_requests = failing

    with pytest.raises(GitlabApiError):
        await p.tick()
    assert event.is_set()


# ---------- maintenance ----------

def test_cleanup_old_review_logs(tmp_path):
    old = tmp_path / "review-1.log"
    fresh = tmp_path / "review-2.log"
    other = tmp_path / "app.log"
    for f in (old, fresh, other):
        f.write_text("x")
    stale = time.time() - 20 * 86400
    os.utime(old, (stale, stale))
    os.utime(other, (stale, stale))

    assert cleanup_old_review_logs(tmp_path, 14) == 1
    assert not old.exists() and fresh.exists() and other.exists()


def test_cleanup_stale_askpass(tmp_path):
    (tmp_path / "askpass-abc.sh").write_text("x")
    (tmp_path / "keep.txt").write_text("x")

    assert cleanup_stale_askpass(tmp_path) == 1
    assert (tmp_path / "keep.txt").exists()
    assert not (tmp_path / "askpass-abc.sh").exists()


def test_remove_workspace_prunes_empty_parents(tmp_path):
    root = tmp_path / "workspaces"
    ws = root / "10" / "1" / "7"
    ws.mkdir(parents=True)
    (ws / "f").write_text("x")
    sibling = root / "10" / "2" / "8"
    sibling.mkdir(parents=True)

    remove_workspace(ws, stop_at=root)

    assert not (root / "10" / "1").exists()
    assert sibling.exists()
    assert root.exists()


def test_cleanup_old_workspaces_removes_empty_dirs(tmp_path):
    root = tmp_path / "workspaces"
    (root / "10" / "1").mkdir(parents=True)
    (root / "11").mkdir()

    cleanup_old_workspaces(root, 3)

    assert list(root.iterdir()) == []


async def test_worker_success_prunes_workspace_dirs(tmp_path, db, origin):
    _queued_review(db, origin)
    out = '{"sessionID":"s","part":{"type":"text","text":"OK"}}'
    bin_path = _fake_opencode(tmp_path, f"#!/bin/sh\nprintf '%s' '{out}'\n")
    worker = _worker(tmp_path, db, origin, bin_path)

    await worker.process_one()

    assert list((tmp_path / "workspaces").iterdir()) == []


def test_review_config_log_ttl_default():
    assert ReviewConfig().log_ttl_days == 14
    with pytest.raises(ValueError):
        ReviewConfig(log_ttl_days=0)


# ---------- API ----------

async def test_retry_unknown_review_404(tmp_path):
    ctx = _app_ctx(tmp_path)
    async with _http(create_app(ctx)) as client:
        resp = await client.post("/reviews/999/retry", headers={"X-Requested-With": "mrwatch"})
    assert resp.status_code == 404


@pytest.mark.parametrize("status", ["queued", "cloning", "reviewing"])
async def test_retry_active_review_409(tmp_path, status):
    ctx = _app_ctx(tmp_path)
    _, mr_id = _seed(ctx.conn)
    review_id = create_review(ctx.conn, mr_id, "new", "a" * 40, "agent-a")
    ctx.conn.execute("UPDATE review SET status = ? WHERE id = ?", (status, review_id))
    ctx.conn.commit()
    async with _http(create_app(ctx)) as client:
        resp = await client.post(
            f"/reviews/{review_id}/retry", headers={"X-Requested-With": "mrwatch"}
        )
    assert resp.status_code == 409
    assert get_review(ctx.conn, review_id)["status"] == status


async def test_retry_wakes_workers(tmp_path):
    ctx = _app_ctx(tmp_path)
    _, mr_id = _seed(ctx.conn)
    review_id = create_review(ctx.conn, mr_id, "new", "a" * 40, "agent-a")
    finish_review(ctx.conn, review_id, "error", error="x")
    app = create_app(ctx)
    app.state.work_event = asyncio.Event()
    async with _http(app) as client:
        resp = await client.post(
            f"/reviews/{review_id}/retry", headers={"X-Requested-With": "mrwatch"}
        )
    assert resp.status_code == 303
    assert app.state.work_event.is_set()
    assert get_review(ctx.conn, review_id)["status"] == "queued"


@pytest.mark.parametrize("body", [[], "text", 5, None])
async def test_settings_non_dict_body_400(tmp_path, monkeypatch, body):
    ctx = _app_ctx(tmp_path)
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("gitlab:\n  url: https://x.example.com\n", encoding="utf-8")
    monkeypatch.setenv("MRWATCH_CONFIG", str(cfg))
    async with _http(create_app(ctx)) as client:
        resp = await client.post("/settings", json=body, headers={"X-Requested-With": "mrwatch"})
    assert resp.status_code == 400


class _FailingResolver:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def get_project_by_path(self, path):
        raise self.error


@pytest.mark.parametrize(
    "error,expected",
    [
        (GitlabApiError("nf", status=404), 400),
        (GitlabApiError("auth", status=403), 400),
        (GitlabApiError("boom", status=503), 502),
        (GitlabApiError("request failed: ConnectError"), 502),
    ],
)
async def test_settings_resolve_error_codes(tmp_path, monkeypatch, error, expected):
    ctx = _app_ctx(tmp_path)
    ctx.api = _FailingResolver(error)
    payload = {
        "gitlab": {}, "review": {},
        "projects": [{"gitlab_project_id": None, "slug": "g/new", "agent": "agent-a"}],
    }
    resp, _ = await _post_settings(ctx, tmp_path, monkeypatch, payload)
    assert resp.status_code == expected


async def test_api_review_json(tmp_path):
    ctx = _app_ctx(tmp_path)
    _, mr_id = _seed(ctx.conn)
    review_id = create_review(ctx.conn, mr_id, "new", "a" * 40, "agent-a")
    finish_review(ctx.conn, review_id, "error", error="bad")
    async with _http(create_app(ctx)) as client:
        resp = await client.get(f"/api/reviews/{review_id}")
        missing = await client.get("/api/reviews/999")
    body = resp.json()
    assert set(body) == {"id", "status", "error", "finished_at", "verdict"}
    assert body["status"] == "error" and body["error"] == "bad"
    assert body["finished_at"] is not None
    assert missing.status_code == 404


async def test_api_mrs_json(tmp_path):
    ctx = _app_ctx(tmp_path)
    _, mr_id = _seed(ctx.conn)
    review_id = create_review(ctx.conn, mr_id, "new", "a" * 40, "agent-a")
    finish_review(ctx.conn, review_id, "done", review_md="x", counts=(1, 0, 2, 0))
    async with _http(create_app(ctx)) as client:
        resp = await client.get("/api/mrs")
    rows = resp.json()
    assert len(rows) == 1
    row = rows[0]
    assert row["latest_review_id"] == review_id
    assert row["latest_review_status"] == "done"
    assert row["crit_count"] == 1 and row["minor_count"] == 2
    assert row["web_url"] == "http://x"
    assert row["path_with_namespace"] == "g/p"


async def test_templates_use_json_endpoints(tmp_path):
    ctx = _app_ctx(tmp_path)
    _, mr_id = _seed(ctx.conn)
    review_id = create_review(ctx.conn, mr_id, "new", "a" * 40, "agent-a")
    async with _http(create_app(ctx)) as client:
        index = await client.get("/")
        review = await client.get(f"/reviews/{review_id}")
    assert "/api/mrs" in index.text
    assert f"/api/reviews/{review_id}" in review.text
