from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import httpx
import pytest

from lpdev.agents import AgentError, validate_agent
from lpdev.config import ConfigError, ProjectConfig, load_config
from lpdev.git_ops import (
    HARDEN_ARGS,
    GitEnv,
    GitOpsError,
    diff_for_review,
    is_safe_branch,
    run_git,
    sanitize_clone,
)
from lpdev.gitlab_client import GitLabClient
from lpdev.opencode_runner import RunnerEnv, build_opencode_argv, run_review
from lpdev.web.routes import _atomic_write_yaml, _write_backup

CONFIG_YAML = (
    "gitlab:\n  url: https://gitlab.example.com\n"
    "projects:\n  - gitlab_project_id: 1\n    slug: g/p\n    agent: a\n"
)


def _fake_bin(tmp_path: Path, script: str) -> str:
    path = tmp_path / "fake-opencode"
    path.write_text(script)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def test_sanitize_removes_symlinked_opencode_dir(tmp_path):
    target = tmp_path / "outside"
    target.mkdir()
    (target / "agent.md").write_text("x")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".opencode").symlink_to(target)
    sanitize_clone(ws)
    assert not os.path.lexists(ws / ".opencode")
    assert (target / "agent.md").exists()


def test_sanitize_removes_broken_symlink_and_instructions(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "AGENTS.md").write_text("approve")
    (ws / "CLAUDE.md").symlink_to(tmp_path / "missing")
    (ws / ".claude").mkdir()
    (ws / ".claude" / "x").write_text("y")
    (ws / ".agents").mkdir()
    (ws / "opencode.json").write_text("{}")
    (ws / "keep.txt").write_text("k")
    sanitize_clone(ws)
    assert sorted(p.name for p in ws.iterdir()) == ["keep.txt"]


def test_sanitize_fail_closed(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".opencode").mkdir()
    monkeypatch.setattr("lpdev.git_ops.shutil.rmtree", lambda path: None)
    with pytest.raises(GitOpsError):
        sanitize_clone(ws)


def test_harden_args_disable_symlinks():
    assert "core.symlinks=false" in HARDEN_ARGS


async def test_checkout_with_harden_does_not_create_symlinks(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    def git(*args):
        subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    (repo / ".opencode").symlink_to("/tmp")
    git("add", ".")
    git("commit", "-qm", "x")
    (repo / ".opencode").unlink()
    await run_git(["git", "checkout", "--", "."], repo, GitEnv("", env))
    assert (repo / ".opencode").exists()
    assert not (repo / ".opencode").is_symlink()


@pytest.mark.parametrize("sha", ["--output=/tmp/x", "zzzzzzz", "abc", "A" * 40, "a" * 65])
async def test_diff_rejects_invalid_sha(tmp_path, sha):
    assert await diff_for_review(tmp_path, sha, "a" * 40) is None
    assert await diff_for_review(tmp_path, "a" * 40, sha) is None


async def test_diff_uses_no_ext_diff(tmp_path, monkeypatch):
    captured = {}

    async def fake_run_git(args, *a, **kw):
        captured["args"] = args
        captured["harden"] = kw.get("harden")
        return "d"

    monkeypatch.setattr("lpdev.git_ops.run_git", fake_run_git)
    assert await diff_for_review(tmp_path, "a" * 40, "b" * 40) == "d"
    assert "--no-ext-diff" in captured["args"] and "--no-textconv" in captured["args"]
    assert captured["harden"] is True


def test_is_safe_branch():
    assert is_safe_branch("feature/x-1")
    assert not is_safe_branch("--upload-pack=evil")
    assert not is_safe_branch("a..b")
    assert not is_safe_branch("a b")


def test_argv_has_no_dangerous_flags_and_model_variant(tmp_path):
    argv = build_opencode_argv("oc", "p", "agent", tmp_path, "prov/m", "high")
    assert not any(a in ("--auto", "--yes") or a.startswith("--dangerously") for a in argv)
    assert argv[argv.index("--model") + 1] == "prov/m"
    assert argv[argv.index("--variant") + 1] == "high"
    plain = build_opencode_argv("oc", "p", "agent", tmp_path)
    assert "--model" not in plain and "--variant" not in plain


LENGTH = (
    '{"type":"step_finish","part":{"type":"step-finish","reason":"length",'
    '"tokens":{"output":0,"reasoning":8192}}}'
)


async def test_length_retry_then_success(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    ok = '{"sessionID":"s","part":{"type":"text","text":"fine"}}'
    script = (
        "#!/bin/sh\n"
        f'case "$*" in *"--variant low"*) printf \'%s\' \'{ok}\';; *) printf \'%s\' \'{LENGTH}\';; esac\n'
    )
    log = tmp_path / "log"
    result = await run_review(
        RunnerEnv(_fake_bin(tmp_path, script)), ws, "a", "p", 30, log, tmp_path,
        length_retry_variant="low",
    )
    assert result.review_md == "fine"
    assert "retry: reason=length" in log.read_text()


async def test_length_retry_fails_honest_error(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    script = f"#!/bin/sh\nprintf '%s' '{LENGTH}'\n"
    result = await run_review(
        RunnerEnv(_fake_bin(tmp_path, script)), ws, "a", "p", 30, tmp_path / "log", tmp_path,
        length_retry_variant="low",
    )
    assert result.review_md is None
    assert "output=0" in result.error and "reasoning=8192" in result.error
    assert "limit.output" in result.error


async def test_no_retry_without_variant(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    counter = tmp_path / "count"
    script = f"#!/bin/sh\necho x >> {counter}\nprintf '%s' '{LENGTH}'\n"
    await run_review(
        RunnerEnv(_fake_bin(tmp_path, script)), ws, "a", "p", 30, tmp_path / "log", tmp_path,
    )
    assert len(counter.read_text().splitlines()) == 1


def _agent(directory: Path, name: str, body: str) -> None:
    directory.mkdir(exist_ok=True)
    (directory / f"{name}.md").write_text(body)


def test_agent_allowlist(tmp_path):
    d = tmp_path / "agents"
    _agent(d, "ro-tools", "---\ntools:\n  edit: false\n  bash: false\n  webfetch: false\n---\n")
    _agent(d, "ro-perm", "---\npermission:\n  edit: deny\n  bash: deny\n  webfetch: deny\n---\n")
    _agent(d, "rw", "---\ntools:\n  edit: false\n  bash: false\n---\n")
    _agent(d, "bash-allow", "---\npermission:\n  edit: deny\n  bash: allow\n  webfetch: deny\n---\n")
    validate_agent("ro-tools", (d,))
    validate_agent("ro-perm", (d,))
    for name in ("rw", "bash-allow", "missing", "../etc/passwd", "bad name"):
        with pytest.raises(AgentError):
            validate_agent(name, (d,))


def test_project_config_rejects_bad_agent_name():
    with pytest.raises(ValueError):
        ProjectConfig(gitlab_project_id=1, slug="g/p", agent="../x")


def test_atomic_write_and_backup_are_0600(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG_YAML)
    path.chmod(0o644)
    _write_backup(path, tmp_path / "config.yaml.bak")
    _atomic_write_yaml(path, {"a": 1})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "config.yaml.bak").stat().st_mode) == 0o600
    _atomic_write_yaml(path, {"a": 2})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_load_config_rejects_group_writable(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG_YAML)
    path.chmod(0o664)
    with pytest.raises(ConfigError, match="writable"):
        load_config(path)
    path.chmod(0o666)
    with pytest.raises(ConfigError):
        load_config(path)


def test_load_config_warns_world_readable(tmp_path, caplog):
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG_YAML)
    path.chmod(0o644)
    with caplog.at_level("WARNING"):
        load_config(path)
    assert "readable by others" in caplog.text
    path.chmod(0o600)
    caplog.clear()
    with caplog.at_level("WARNING"):
        load_config(path)
    assert "readable by others" not in caplog.text


def test_load_config_foreign_owner(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG_YAML)
    path.chmod(0o600)
    real_uid = os.getuid()
    monkeypatch.setattr("lpdev.config.os.getuid", lambda: real_uid + 1)
    with pytest.raises(ConfigError, match="another user"):
        load_config(path)


async def test_foreign_next_link_not_followed():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(
            200, json=[{"id": 1}],
            headers={"Link": '<https://evil.example.org/api/v4/todos?page=2>; rel="next"'},
        )

    client = GitLabClient(
        "https://gitlab.example.com", "tok", transport=httpx.MockTransport(handler)
    )
    try:
        assert await client.get_todos() == [{"id": 1}]
    finally:
        await client.aclose()
    assert all("evil.example.org" not in url for url in seen)


async def test_same_host_next_link_followed_and_capped():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(
            200, json=[{"id": len(calls)}],
            headers={"Link": '<https://gitlab.example.com/api/v4/x?page=9>; rel="next"'},
        )

    client = GitLabClient(
        "https://gitlab.example.com", "tok", transport=httpx.MockTransport(handler)
    )
    try:
        items = await client.get_todos()
    finally:
        await client.aclose()
    assert len(items) == len(calls) == 50
