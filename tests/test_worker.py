from __future__ import annotations

import asyncio
import stat
import time
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

import lpdev.worker as worker_module
from lpdev.config import Config, GitlabConfig, PathsConfig, ProjectConfig, ReviewConfig
from lpdev.config import resolve_paths
from lpdev.db import (
    claim_next_review,
    create_question,
    create_review,
    finish_review,
    get_review,
    list_questions,
    requeue_incomplete,
    supersede_reviews,
    sync_projects,
    upsert_mr,
)
from lpdev.opencode_runner import ReviewResult
from lpdev.worker import QUESTION_HEAD_GONE_ERROR, QuestionWorker, ReviewRegistry, Worker

NDJSON_OUTPUT = (
    '{"type":"session.started","sessionID":"sess-w1"}\n'
    '{"type":"message.part","part":{"type":"text","text":"ОК"}}\n'
)


def _git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home()),
             "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    )
    return result.stdout


READONLY_AGENT = (
    "---\ndescription: r\ntools:\n  edit: false\n  bash: false\n  webfetch: false\n---\nbody\n"
)


def _fake_opencode(tmp_path: Path, script: str) -> str:
    bin_path = tmp_path / "fake-opencode"
    bin_path.write_text(script)
    bin_path.chmod(bin_path.stat().st_mode | stat.S_IEXEC)
    return str(bin_path)


class StubApi:
    def __init__(self, detail: dict) -> None:
        self.detail = detail

    async def get_project_mr(self, project_id: int, iid: int) -> dict:
        return dict(self.detail)


def _config(tmp_path: Path, opencode_bin: str) -> Config:
    config = Config(
        gitlab=GitlabConfig(url="https://gitlab.example.com", pat_command="printf tok"),
        projects=[ProjectConfig(gitlab_project_id=10, slug="g/p", agent="agent-a")],
        review=ReviewConfig(clone_depth=5),
        paths=PathsConfig(
            db=str(tmp_path / "data" / "lpdev.db"),
            workspaces=str(tmp_path / "workspaces"),
            logs=str(tmp_path / "logs"),
        ),
    )
    config.opencode.bin = opencode_bin
    return config


def _detail(base_sha: str, head_sha: str) -> dict:
    return {
        "iid": 1,
        "title": "MR 1",
        "author": {"username": "other"},
        "source_branch": "feature",
        "target_branch": "main",
        "web_url": "https://gitlab.example.com/g/p/-/merge_requests/1",
        "state": "opened",
        "sha": head_sha,
        "updated_at": "2026-01-01T12:00:00Z",
        "reviewers": [],
        "description": "описание",
        "diff_refs": {"base_sha": base_sha, "head_sha": head_sha},
    }


def _queued_review(conn, origin: Path) -> int:
    sync_projects(conn, [(10, "g/p", "agent-a", False, True)])
    head_sha = _git("rev-parse", "merge-requests/1/head", cwd=origin).strip()
    result = upsert_mr(
        conn, project_db_id=1, iid=1, title="MR 1", author_username="other",
        source_branch="feature",
        web_url="https://gitlab.example.com/g/p/-/merge_requests/1",
        state="opened", head_sha=head_sha, updated_at="2026-01-01T12:00:00Z",
    )
    review_id = create_review(conn, result.mr_id, "new", head_sha, "agent-a")
    assert review_id is not None
    return review_id


def _worker(
    tmp_path: Path,
    db_conn,
    origin: Path,
    opencode_bin: str,
    registry: ReviewRegistry | None = None,
) -> Worker:
    config = _config(tmp_path, opencode_bin)
    paths = resolve_paths(config)
    (paths.workspaces.parent / "tmp").mkdir(parents=True, exist_ok=True)
    base_sha = _git("rev-parse", "main", cwd=origin).strip()
    head_sha = _git("rev-parse", "merge-requests/1/head", cwd=origin).strip()
    api = StubApi(_detail(base_sha, head_sha))

    class LocalWorker(Worker):
        def _repo_url(self, mr) -> str:
            return str(origin)

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(exist_ok=True)
    (agents_dir / "agent-a.md").write_text(READONLY_AGENT)
    return LocalWorker(
        0, db_conn, api, config, paths, clone_harden=False, agents_dirs=(agents_dir,),
        registry=registry,
    )


async def test_worker_full_success(tmp_path, db, origin):
    review_id = _queued_review(db, origin)
    opencode_bin = _fake_opencode(
        tmp_path, f"#!/bin/sh\nprintf '%s' '{NDJSON_OUTPUT}'\n"
    )
    worker = _worker(tmp_path, db, origin, opencode_bin)

    assert await worker.process_one() is True

    review = get_review(db, review_id)
    assert review["status"] == "done"
    assert review["review_md"] == "ОК"
    assert review["session_id"] == "sess-w1"
    assert review["error"] is None
    log_file = tmp_path / "logs" / f"review-{review_id}.log"
    assert log_file.exists()
    ws_dir = tmp_path / "workspaces" / "10" / "1" / str(review_id)
    assert not ws_dir.exists()
    assert await worker.process_one() is False


async def test_worker_clone_failure_retries_then_error(tmp_path, db, origin, monkeypatch):
    monkeypatch.setattr("lpdev.worker.CLONE_RETRY_BACKOFF_SEC", 0.0)
    review_id = _queued_review(db, origin)
    opencode_bin = _fake_opencode(tmp_path, "#!/bin/sh\ntrue\n")
    worker = _worker(tmp_path, db, origin, opencode_bin)
    db.execute("UPDATE mr SET iid = 999 WHERE id = 1")
    db.commit()

    for _ in range(3):
        await worker.process_one()

    review = get_review(db, review_id)
    assert review["status"] == "error"
    assert "clone failed" in review["error"]


async def test_worker_opencode_failure_error_no_retry(tmp_path, db, origin):
    review_id = _queued_review(db, origin)
    opencode_bin = _fake_opencode(tmp_path, "#!/bin/sh\necho broken >&2\nexit 7\n")
    worker = _worker(tmp_path, db, origin, opencode_bin)

    await worker.process_one()

    review = get_review(db, review_id)
    assert review["status"] == "error"
    assert "broken" in review["error"]


async def test_worker_counts_severities(tmp_path, db, origin):
    review_id = _queued_review(db, origin)
    global NDJSON_OUTPUT  # noqa: F824
    output = (
        '{"type":"session.started","sessionID":"sess-c1"}\n'
        '{"type":"message.part","part":{"type":"text","text":"## Findings\\n'
        "- [BLOCKER] a.py:1 — x — y\\n- [NIT] b.py:2 — x — y\\n"
        '- [MAJOR] c.py:3 — x — y\\n"}}\n'
    )
    opencode_bin = _fake_opencode(tmp_path, f"#!/bin/sh\nprintf '%s' '{output}'\n")
    worker = _worker(tmp_path, db, origin, opencode_bin)

    await worker.process_one()

    review = get_review(db, review_id)
    assert review["status"] == "done"
    assert review["crit_count"] == 1
    assert review["major_count"] == 1
    assert review["minor_count"] == 0
    assert review["nit_count"] == 1


async def test_worker_rejects_unsafe_agent(tmp_path, db, origin):
    review_id = _queued_review(db, origin)
    marker = tmp_path / "ran"
    opencode_bin = _fake_opencode(tmp_path, f"#!/bin/sh\ntouch {marker}\n")
    worker = _worker(tmp_path, db, origin, opencode_bin)
    (tmp_path / "agents" / "agent-a.md").write_text(
        "---\ntools:\n  edit: true\n  bash: false\n---\n"
    )

    await worker.process_one()

    review = get_review(db, review_id)
    assert review["status"] == "error"
    assert "agent rejected" in review["error"]
    assert not marker.exists()


async def test_worker_passes_model_variant_and_retries_on_length(tmp_path, db, origin):
    review_id = _queued_review(db, origin)
    argv_log = tmp_path / "argv.txt"
    length = (
        '{"type":"step_finish","part":{"type":"step-finish","reason":"length",'
        '"tokens":{"output":0,"reasoning":8192}}}'
    )
    ok = '{"sessionID":"s2","part":{"type":"text","text":"ОК"}}'
    script = (
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" | grep -A1 -E '^--(model|variant)$' | tr '\\n' ' ' >> {argv_log}; echo >> {argv_log}\n"
        'case "$*" in *"--variant low"*) '
        f"printf '%s' '{ok}';; *) printf '%s' '{length}';; esac\n"
    )
    opencode_bin = _fake_opencode(tmp_path, script)
    worker = _worker(tmp_path, db, origin, opencode_bin)
    worker._config.opencode.model = "prov/model"

    await worker.process_one()

    review = get_review(db, review_id)
    assert review["status"] == "done"
    lines = argv_log.read_text().splitlines()
    assert len(lines) == 2
    assert all("--model prov/model" in line for line in lines)
    assert "--variant" not in lines[0]
    assert "--variant low" in lines[1]


def _repo_url_for(base: str, path: str) -> str:
    config = Config(
        gitlab=GitlabConfig(url=base, pat_command="printf tok"),
        projects=[ProjectConfig(gitlab_project_id=10, slug="g/p", agent="agent-a")],
    )
    worker = Worker.__new__(Worker)
    worker._static_config = config
    worker._config_provider = None
    return worker._repo_url({"path_with_namespace": path})


def test_repo_url_has_no_userinfo_and_uses_https_host():
    from urllib.parse import urlsplit

    parts = urlsplit(_repo_url_for("https://gitlab.example.com", "g/p"))
    assert parts.scheme == "https"
    assert parts.hostname == "gitlab.example.com"
    assert "@" not in parts.netloc
    assert parts.username is None and parts.password is None


def test_repo_url_trailing_slash_no_double_slash():
    url = _repo_url_for("https://gitlab.example.com/", "g/p")
    assert url == "https://gitlab.example.com/g/p.git"
    assert "//" not in url.split("://", 1)[1]


def test_repo_url_subgroups_and_subpath():
    assert (
        _repo_url_for("https://example.com/gitlab/", "a/b/c/repo")
        == "https://example.com/gitlab/a/b/c/repo.git"
    )


async def _wait_until(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


def _workspace(tmp_path: Path, review_id: int) -> Path:
    return tmp_path / "workspaces" / "10" / "1" / str(review_id)


def _spy_finish(monkeypatch) -> list[int]:
    calls: list[int] = []
    real = worker_module.finish_review

    def spy(conn, review_id, *args, **kwargs):
        calls.append(review_id)
        return real(conn, review_id, *args, **kwargs)

    monkeypatch.setattr(worker_module, "finish_review", spy)
    return calls


async def test_cancel_during_clone_removes_workspace_and_worker_continues(
    tmp_path, db, origin, monkeypatch
):
    sync_projects(db, [(10, "g/p", "agent-a", False, True)])
    head_sha = _git("rev-parse", "merge-requests/1/head", cwd=origin).strip()
    mr_id = upsert_mr(
        db, project_db_id=1, iid=1, title="MR 1", author_username="other",
        source_branch="feature", web_url="https://gitlab.example.com/g/p/-/merge_requests/1",
        state="opened", head_sha="f" * 40, updated_at="2026-01-01T12:00:00Z",
    ).mr_id
    stale_id = create_review(db, mr_id, "new", "f" * 40, "agent-a")
    registry = ReviewRegistry()
    opencode_bin = _fake_opencode(tmp_path, f"#!/bin/sh\nprintf '%s' '{NDJSON_OUTPUT}'\n")
    worker = _worker(tmp_path, db, origin, opencode_bin, registry)
    worker._api.detail["sha"] = "f" * 40
    finished = _spy_finish(monkeypatch)
    real_clone = worker_module.clone_for_review
    clone_started = asyncio.Event()

    async def blocking_clone(**kwargs):
        if kwargs["expected_sha"] != "f" * 40:
            return await real_clone(**kwargs)
        kwargs["workspace"].mkdir(parents=True)
        clone_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(worker_module, "clone_for_review", blocking_clone)
    run_task = asyncio.create_task(worker.run())
    try:
        await asyncio.wait_for(clone_started.wait(), timeout=10)
        assert _workspace(tmp_path, stale_id).exists()

        worker._api.detail["sha"] = head_sha
        supersede_reviews(db, mr_id, head_sha)
        assert registry.cancel(stale_id) is True
        fresh_id = create_review(db, mr_id, "push", head_sha, "agent-a")

        await _wait_until(lambda: get_review(db, fresh_id)["status"] == "done")
    finally:
        run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)

    stale = get_review(db, stale_id)
    assert stale["status"] == "cancelled"
    assert stale["review_md"] is None
    assert stale_id not in finished
    assert not _workspace(tmp_path, stale_id).exists()
    assert not registry.was_cancelled(stale_id)
    assert registry.cancel(stale_id) is False


async def test_cancel_during_review_discards_result(tmp_path, db, origin, monkeypatch):
    review_id = _queued_review(db, origin)
    registry = ReviewRegistry()
    worker = _worker(tmp_path, db, origin, "/bin/true", registry)
    finished = _spy_finish(monkeypatch)
    started = asyncio.Event()
    observed: list[str] = []

    async def blocking_review(**kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            observed.append("killed")
            raise

    monkeypatch.setattr(worker_module, "run_review", blocking_review)
    task = asyncio.create_task(worker.process_one())
    await asyncio.wait_for(started.wait(), timeout=10)
    assert get_review(db, review_id)["status"] == "reviewing"
    assert _workspace(tmp_path, review_id).exists()

    supersede_reviews(db, get_review(db, review_id)["mr_id"], "e" * 40)
    registry.cancel(review_id)

    assert await task is True
    review = get_review(db, review_id)
    assert review["status"] == "cancelled"
    assert review["review_md"] is None
    assert observed == ["killed"]
    assert review_id not in finished
    assert not _workspace(tmp_path, review_id).exists()


async def test_worker_shutdown_propagates_cancel(tmp_path, db, origin, monkeypatch):
    review_id = _queued_review(db, origin)
    registry = ReviewRegistry()
    worker = _worker(tmp_path, db, origin, "/bin/true", registry)
    finished = _spy_finish(monkeypatch)
    started = asyncio.Event()

    async def blocking_review(**kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(worker_module, "run_review", blocking_review)
    task = asyncio.create_task(worker.process_one())
    await asyncio.wait_for(started.wait(), timeout=10)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert get_review(db, review_id)["status"] == "reviewing"
    assert finished == []
    assert not _workspace(tmp_path, review_id).exists()
    assert registry.cancel(review_id) is False



def _question_worker(
    tmp_path: Path, db_conn, origin: Path, registry: ReviewRegistry | None = None
) -> QuestionWorker:
    config = _config(tmp_path, "/bin/true")
    paths = resolve_paths(config)
    (paths.workspaces.parent / "tmp").mkdir(parents=True, exist_ok=True)

    class LocalQuestionWorker(QuestionWorker):
        def _repo_url(self, mr) -> str:
            return str(origin)

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(exist_ok=True)
    (agents_dir / "agent-a.md").write_text(READONLY_AGENT)
    return LocalQuestionWorker(
        0, db_conn, None, config, paths, clone_harden=False, agents_dirs=(agents_dir,),
        registry=registry,
    )


def _done_review_with_question(db_conn, origin: Path, session_id: str = "ses_root1"):
    review_id = _queued_review(db_conn, origin)
    finish_review(db_conn, review_id, "done", review_md="ok", session_id=session_id)
    question_id = create_question(db_conn, review_id, "почему так?")
    assert question_id is not None
    return review_id, question_id


def _question(db_conn, review_id: int, question_id: int):
    return next(q for q in list_questions(db_conn, review_id) if q["id"] == question_id)


def _spy_run_question(monkeypatch, answer: str = "ответ", session: str = "ses_out1"):
    calls: list[dict] = []

    async def fake(**kwargs):
        kwargs["workspace_existed"] = (kwargs["workspace"] / ".git").is_dir()
        calls.append(kwargs)
        return ReviewResult(review_md=answer, session_id=session, exit_code=0)

    monkeypatch.setattr(worker_module, "run_question", fake)
    return calls


async def test_question_first_forks_root_then_chains_parent(tmp_path, db, origin, monkeypatch):
    review_id, first = _done_review_with_question(db, origin)
    worker = _question_worker(tmp_path, db, origin)
    calls = _spy_run_question(monkeypatch, session="ses_out1")
    validated: list[str] = []
    real_validate = worker_module.validate_agent

    def spy_validate(agent, dirs=None):
        validated.append(agent)
        return real_validate(agent, dirs)

    monkeypatch.setattr(worker_module, "validate_agent", spy_validate)

    assert await worker.process_one() is True

    row = _question(db, review_id, first)
    assert row["status"] == "done"
    assert row["answer_md"] == "ответ"
    assert row["session_id"] == "ses_out1"
    assert validated == ["agent-a"]
    call = calls[0]
    assert call["session_id"] == "ses_root1"
    assert call["fork"] is True
    assert call["question"] == "почему так?"
    assert call["agent"] == "agent-a"
    assert call["workspace"] == _workspace(tmp_path, review_id)
    assert call["workspace_existed"] is True
    assert call["timeout_sec"] == 600
    assert call["log_path"].name == f"question-{first}.log"
    assert not _workspace(tmp_path, review_id).exists()

    second = create_question(db, review_id, "а ещё?")
    calls.clear()
    assert await worker.process_one() is True
    assert calls[0]["session_id"] == "ses_out1"
    assert calls[0]["fork"] is False
    assert _question(db, review_id, second)["status"] == "done"
    assert not _workspace(tmp_path, review_id).exists()
    assert await worker.process_one() is False


async def test_question_head_gone_marks_error(tmp_path, db, origin, monkeypatch):
    review_id, question_id = _done_review_with_question(db, origin)
    db.execute("UPDATE review SET head_sha = ? WHERE id = ?", ("e" * 40, review_id))
    worker = _question_worker(tmp_path, db, origin)
    calls = _spy_run_question(monkeypatch)

    assert await worker.process_one() is True

    row = _question(db, review_id, question_id)
    assert row["status"] == "error"
    assert row["error"] == QUESTION_HEAD_GONE_ERROR
    assert calls == []
    assert not _workspace(tmp_path, review_id).exists()


async def test_question_invalid_session_marks_error(tmp_path, db, origin, monkeypatch):
    review_id, question_id = _done_review_with_question(db, origin, session_id="--fork")
    worker = _question_worker(tmp_path, db, origin)
    calls = _spy_run_question(monkeypatch)

    assert await worker.process_one() is True

    row = _question(db, review_id, question_id)
    assert row["status"] == "error"
    assert "сессии" in row["error"]
    assert calls == []


async def test_question_rejects_unsafe_agent(tmp_path, db, origin, monkeypatch):
    review_id, question_id = _done_review_with_question(db, origin)
    worker = _question_worker(tmp_path, db, origin)
    (tmp_path / "agents" / "agent-a.md").write_text("---\ndescription: r\n---\nbody\n")
    calls = _spy_run_question(monkeypatch)

    assert await worker.process_one() is True

    row = _question(db, review_id, question_id)
    assert row["status"] == "error"
    assert row["error"].startswith("agent rejected")
    assert calls == []


async def test_question_opencode_without_answer_marks_error(tmp_path, db, origin, monkeypatch):
    review_id, question_id = _done_review_with_question(db, origin)
    worker = _question_worker(tmp_path, db, origin)

    async def failing(**kwargs):
        return ReviewResult(exit_code=1, error="opencode exit 1: boom")

    monkeypatch.setattr(worker_module, "run_question", failing)

    assert await worker.process_one() is True

    row = _question(db, review_id, question_id)
    assert row["status"] == "error"
    assert row["error"] == "opencode exit 1: boom"
    assert not _workspace(tmp_path, review_id).exists()


async def test_question_cancel_removes_workspace(tmp_path, db, origin, monkeypatch):
    review_id, question_id = _done_review_with_question(db, origin)
    registry = ReviewRegistry()
    worker = _question_worker(tmp_path, db, origin, registry)
    started = asyncio.Event()

    async def blocking(**kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(worker_module, "run_question", blocking)
    task = asyncio.create_task(worker.process_one())
    await asyncio.wait_for(started.wait(), timeout=10)
    assert _workspace(tmp_path, review_id).exists()

    assert registry.cancel(question_id) is True
    assert await task is True

    row = _question(db, review_id, question_id)
    assert row["status"] == "cancelled"
    assert not _workspace(tmp_path, review_id).exists()
    assert registry.cancel(question_id) is False


async def test_question_shutdown_propagates_cancel(tmp_path, db, origin, monkeypatch):
    review_id, question_id = _done_review_with_question(db, origin)
    worker = _question_worker(tmp_path, db, origin, ReviewRegistry())
    started = asyncio.Event()

    async def blocking(**kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(worker_module, "run_question", blocking)
    task = asyncio.create_task(worker.process_one())
    await asyncio.wait_for(started.wait(), timeout=10)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _question(db, review_id, question_id)["status"] == "running"
    assert not _workspace(tmp_path, review_id).exists()


async def test_review_prompt_gets_extra_instructions(tmp_path, db, origin, monkeypatch):
    review_id = _queued_review(db, origin)
    db.execute(
        "UPDATE review SET extra_instructions = ? WHERE id = ?", ("смотри SQL", review_id)
    )
    worker = _worker(tmp_path, db, origin, "/bin/true")
    prompts: list[str] = []

    async def fake_review(**kwargs):
        prompts.append(kwargs["prompt"])
        return ReviewResult(review_md="ok", session_id="ses_r", exit_code=0)

    monkeypatch.setattr(worker_module, "run_review", fake_review)
    assert await worker.process_one() is True
    assert "=== ДОП. ИНСТРУКЦИИ ПОЛЬЗОВАТЕЛЯ ===\nсмотри SQL" in prompts[0]


NEW_SHA = "b" * 40


def _spy_clone(monkeypatch) -> list[str]:
    calls: list[str] = []
    real = worker_module.clone_for_review

    async def spy(**kwargs):
        calls.append(kwargs["expected_sha"])
        return await real(**kwargs)

    monkeypatch.setattr(worker_module, "clone_for_review", spy)
    return calls


def _fake_review_ok(monkeypatch) -> None:
    async def fake_review(**kwargs):
        return ReviewResult(review_md="ok", session_id="ses_r", exit_code=0)

    monkeypatch.setattr(worker_module, "run_review", fake_review)


def _delay_sec(row) -> float:
    not_before = datetime.fromisoformat(row["not_before"].replace("Z", "+00:00"))
    return (not_before - datetime.now(UTC)).total_seconds()


async def test_head_moved_before_clone_requeues_with_debounce(tmp_path, db, origin, monkeypatch):
    review_id = _queued_review(db, origin)
    db.execute("UPDATE review SET extra_instructions = 'x' WHERE id = ?", (review_id,))
    db.commit()
    worker = _worker(tmp_path, db, origin, "/bin/true")
    worker._api.detail["sha"] = NEW_SHA
    clones = _spy_clone(monkeypatch)
    finished = _spy_finish(monkeypatch)

    assert await worker.process_one() is True

    row = get_review(db, review_id)
    assert row["status"] == "queued"
    assert row["head_sha"] == NEW_SHA
    assert row["attempt"] == 0
    assert row["extra_instructions"] == "x"
    assert 110 < _delay_sec(row) <= 120
    assert clones == []
    assert finished == []
    assert claim_next_review(db) is None


async def test_head_moved_before_clone_without_debounce_is_not_deferred(tmp_path, db, origin):
    review_id = _queued_review(db, origin)
    worker = _worker(tmp_path, db, origin, "/bin/true")
    worker._static_config.review.push_debounce_sec = 0
    worker._api.detail["sha"] = NEW_SHA

    assert await worker.process_one() is True

    row = get_review(db, review_id)
    assert row["status"] == "queued"
    assert row["not_before"] is None


async def test_head_moved_before_clone_conflict_cancels(tmp_path, db, origin, monkeypatch):
    review_id = _queued_review(db, origin)
    other = create_review(db, 1, "new", NEW_SHA, "agent-a")
    worker = _worker(tmp_path, db, origin, "/bin/true")
    worker._api.detail["sha"] = NEW_SHA
    clones = _spy_clone(monkeypatch)

    assert await worker.process_one() is True

    row = get_review(db, review_id)
    assert row["status"] == "cancelled"
    assert row["error"] == f"отменено: в MR новый коммит {NEW_SHA[:12]}"
    assert row["finished_at"] is not None
    assert get_review(db, other)["status"] == "queued"
    assert clones == []


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"state": "merged"}, "отменено: MR закрыт"),
        ({"state": "closed"}, "отменено: MR закрыт"),
        ({"draft": True}, "отменено: MR переведён в draft"),
    ],
)
async def test_head_moved_before_clone_closed_or_draft_cancels(
    tmp_path, db, origin, monkeypatch, change, error
):
    review_id = _queued_review(db, origin)
    worker = _worker(tmp_path, db, origin, "/bin/true")
    worker._api.detail.update({"sha": NEW_SHA, **change})
    clones = _spy_clone(monkeypatch)

    assert await worker.process_one() is True

    row = get_review(db, review_id)
    assert row["status"] == "cancelled"
    assert row["error"] == error
    assert row["head_sha"] != NEW_SHA
    assert clones == []


async def test_draft_with_same_head_reviews_as_usual(tmp_path, db, origin, monkeypatch):
    review_id = _queued_review(db, origin)
    worker = _worker(tmp_path, db, origin, "/bin/true")
    worker._api.detail["draft"] = True
    _fake_review_ok(monkeypatch)

    assert await worker.process_one() is True

    assert get_review(db, review_id)["status"] == "done"


async def test_clone_head_mismatch_conflict_cancels(tmp_path, db, origin):
    review_id = _queued_review(db, origin)
    actual = _git("rev-parse", "merge-requests/1/head", cwd=origin).strip()
    db.execute("UPDATE review SET head_sha = ? WHERE id = ?", ("f" * 40, review_id))
    db.commit()
    create_review(db, 1, "new", actual, "agent-a")
    worker = _worker(tmp_path, db, origin, "/bin/true")
    worker._api.detail["sha"] = "f" * 40

    assert await worker.process_one() is True

    row = get_review(db, review_id)
    assert row["status"] == "cancelled"
    assert row["error"] == f"отменено: в MR новый коммит {actual[:12]}"


async def test_review_requeued_after_restart_on_stale_head_is_rebased(
    tmp_path, db, origin, monkeypatch
):
    review_id = _queued_review(db, origin)
    db.execute("UPDATE review SET status = 'reviewing', attempt = 1 WHERE id = ?", (review_id,))
    db.commit()
    requeue_incomplete(db)
    worker = _worker(tmp_path, db, origin, "/bin/true")
    worker._api.detail["sha"] = NEW_SHA
    clones = _spy_clone(monkeypatch)

    assert await worker.process_one() is True

    row = get_review(db, review_id)
    assert row["status"] == "queued"
    assert row["head_sha"] == NEW_SHA
    assert row["not_before"] is not None
    assert clones == []
