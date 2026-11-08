from __future__ import annotations

import stat
from pathlib import Path

import pytest

from mrwatch.opencode_runner import (
    OpencodeError,
    RunnerEnv,
    build_review_prompt,
    output_truncated_by_length,
    parse_opencode_output,
    parse_severity_counts,
    MAX_VERDICT_INPUT_CHARS,
    parse_verdict,
    run_review,
)


def _make_fake_opencode(tmp_path: Path, script: str) -> str:
    bin_path = tmp_path / "fake-opencode"
    bin_path.write_text(script)
    bin_path.chmod(bin_path.stat().st_mode | stat.S_IEXEC)
    return str(bin_path)


NDJSON_OUTPUT = (
    '{"type":"session.started","sessionID":"sess-42"}\n'
    '{"type":"message.part","part":{"type":"text","text":"Первая часть"}}\n'
    '{"type":"message.part","part":{"type":"step-start"}}\n'
    '{"type":"message.part","part":{"type":"text","text":"Вторая часть"}}\n'
)


def test_parse_opencode_output():
    md, sid = parse_opencode_output(NDJSON_OUTPUT)
    assert sid == "sess-42"
    assert md == "Первая часть\n\nВторая часть"


def test_parse_garbage_output():
    md, sid = parse_opencode_output("not json at all\nrandom line")
    assert md is None
    assert sid is None


async def test_run_review_success(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    log = tmp_path / "logs" / "rev.log"
    bin_path = _make_fake_opencode(
        tmp_path, f"#!/bin/sh\nprintf '%s' '{NDJSON_OUTPUT}'\n"
    )

    result = await run_review(
        RunnerEnv(opencode_bin=bin_path),
        workspace,
        "reviewer",
        "prompt text",
        timeout_sec=30,
        log_path=log,
        tmpdir=tmp_path,
    )

    assert result.exit_code == 0
    assert result.session_id == "sess-42"
    assert result.review_md == "Первая часть\n\nВторая часть"
    assert result.error is None
    assert "Первая часть" in log.read_text()


async def test_run_review_nonzero_exit(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    bin_path = _make_fake_opencode(tmp_path, "#!/bin/sh\necho boom >&2\nexit 3\n")

    result = await run_review(
        RunnerEnv(opencode_bin=bin_path), workspace, "reviewer", "p", 30,
        tmp_path / "log", tmp_path,
    )

    assert result.exit_code == 3
    assert result.review_md is None
    assert result.error is not None
    assert "boom" in result.error


async def test_run_review_timeout_kills(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    bin_path = _make_fake_opencode(tmp_path, "#!/bin/sh\nsleep 30\n")

    result = await run_review(
        RunnerEnv(opencode_bin=bin_path), workspace, "reviewer", "p", 1,
        tmp_path / "log", tmp_path,
    )

    assert result.timed_out is True
    assert result.error is not None
    assert "timed out" in result.error


async def test_run_review_env_isolation(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    probe = tmp_path / "env.txt"
    bin_path = _make_fake_opencode(
        tmp_path, f"#!/bin/sh\nenv > {probe}\nprintf '{{\"sessionID\":\"s\"}}'\n"
    )
    monkeypatch.setenv("MRWATCH_SECRET_TEST", "leak-me")

    await run_review(
        RunnerEnv(opencode_bin=bin_path), workspace, "reviewer", "p", 30,
        tmp_path / "log", tmp_path,
    )

    env_text = probe.read_text()
    assert "MRWATCH_SECRET_TEST" not in env_text
    assert "GIT_ASKPASS" not in env_text


async def test_run_review_missing_binary_raises(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()

    with pytest.raises(OpencodeError):
        await run_review(
            RunnerEnv(opencode_bin=str(tmp_path / "no-such-bin")),
            workspace, "reviewer", "p", 30, tmp_path / "log", tmp_path,
        )


def test_build_review_prompt_masks_nothing_but_truncates():
    diff = "x" * 200_000
    prompt = build_review_prompt("T", "alice", "desc", diff, max_diff_chars=1000)
    assert "[diff truncated]" in prompt
    assert len(prompt) < 200_000
    assert "НЕПРОВЕРЕННЫЕ ДАННЫЕ" in prompt
    assert "T" in prompt


def test_build_review_prompt_no_diff():
    prompt = build_review_prompt("T", None, None, None)
    assert "Дифф недоступен" in prompt


def test_parse_severity_counts_full():
    md = (
        "## Findings\n"
        "- [BLOCKER] a.py:1 — x — y\n"
        "- [MAJOR] b.py:2 — x — y\n"
        "* [MINOR] c.py:3 — x — y\n"
        "- [NIT] d.py:4 — x — y\n"
        "- [NIT] e.py:5 — x — y\n"
    )
    assert parse_severity_counts(md) == (1, 1, 1, 2)


def test_parse_severity_counts_empty_and_none():
    assert parse_severity_counts(None) == (0, 0, 0, 0)
    assert parse_severity_counts("## Summary\nнет замечаний") == (0, 0, 0, 0)


def test_parse_severity_counts_lowercase_not_matched():
    md = "- [major] a.py:1 — x — y"
    assert parse_severity_counts(md) == (0, 0, 0, 0)


def test_output_truncated_by_length():
    raw = '{"type":"step_finish","part":{"type":"step-finish","reason":"length"}}\n'
    assert output_truncated_by_length(raw) is True
    assert output_truncated_by_length('{"part":{"type":"step-finish","reason":"stop"}}') is False
    assert output_truncated_by_length("") is False


@pytest.mark.parametrize(
    "md, expected",
    [
        ("## Summary\nok\n\n## Verdict\nAPPROVE", "APPROVE"),
        ("## Verdict\nAPPROVE_WITH_NITS\n", "APPROVE_WITH_NITS"),
        ("### verdict\nREQUEST_CHANGES", "REQUEST_CHANGES"),
        ("Summary\n\nVerdict: REQUEST_CHANGES", "REQUEST_CHANGES"),
        ("Verdict: APPROVE с пометкой «нет изменений для ревью»", "APPROVE"),
        ("## Verdict\n**REQUEST_CHANGES**", "REQUEST_CHANGES"),
        ("## Verdict\n`APPROVE_WITH_NITS` — мелочи", "APPROVE_WITH_NITS"),
        ("**Verdict:** **APPROVE**", "APPROVE"),
        ("## Verdict: APPROVE", "APPROVE"),
        ("## Verdict\n\nAPPROVE с пометкой «пусто»\n", "APPROVE"),
        ("## Summary\nнужно APPROVE\n\n## Verdict\nREQUEST_CHANGES", "REQUEST_CHANGES"),
        ("## Summary\nREQUEST_CHANGES вероятно\n\nVerdict: APPROVE", "APPROVE"),
        ("## Verdict\nAPPROVE\n## Risks\nREQUEST_CHANGES", "APPROVE"),
        ("## Summary\nAPPROVE", None),
        ("## Verdict\nMAYBE", None),
        ("## Verdict\nNOT_APPROVE", None),
        ("## Verdict\nAPPROVED", None),
        ("## Verdict\nAPPROVE\n\n```\n## Verdict\nREQUEST_CHANGES\n```\n", "APPROVE"),
        ("## Verdict\nAPPROVE\n\n~~~md\n## Verdict\nREQUEST_CHANGES\n~~~\n", "APPROVE"),
        ("````\n```\n## Verdict\nREQUEST_CHANGES\n````\n## Verdict\nAPPROVE", "APPROVE"),
        ("```python\n# Verdict: APPROVE\n```\nVerdict: REQUEST_CHANGES", "REQUEST_CHANGES"),
        ("## Verdict\nREQUEST_CHANGES\n\n> ## Verdict APPROVE", "REQUEST_CHANGES"),
        ("> ## Verdict APPROVE", None),
        ("```\nVerdict: APPROVE\n```", None),
        ("## Verdict\nAPPROVE | APPROVE_WITH_NITS | REQUEST_CHANGES", None),
        ("Verdict: APPROVE | REQUEST_CHANGES", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_verdict(md, expected):
    assert parse_verdict(md) == expected


def _padded(tail: str, total: int) -> str:
    return "x" * (total - len(tail)) + tail


def test_parse_verdict_over_limit_returns_none():
    tail = "\n## Verdict\nAPPROVE\n"
    assert parse_verdict(_padded(tail, MAX_VERDICT_INPUT_CHARS + 1)) is None


def test_parse_verdict_exactly_at_limit_is_parsed():
    tail = "\n## Verdict\nAPPROVE\n"
    assert parse_verdict(_padded(tail, MAX_VERDICT_INPUT_CHARS)) == "APPROVE"


def test_parse_verdict_unclosed_fence_before_verdict_returns_none():
    assert parse_verdict("text\n```\ncode\n## Verdict\nAPPROVE\n") is None


def test_parse_verdict_is_linear_on_hostile_input():
    import time

    many_headings = "#Verdict\n" * 50_000
    big_text = ("текст ревью без вердикта " * 40 + "\n") * 5_000
    for text in (many_headings, big_text):
        started = time.perf_counter()
        parse_verdict(text)
        assert time.perf_counter() - started < 0.5
