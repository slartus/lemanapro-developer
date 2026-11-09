from __future__ import annotations

import stat
from pathlib import Path

import pytest

from lpdev.opencode_runner import (
    OpencodeError,
    RunnerEnv,
    build_opencode_argv,
    build_question_prompt,
    build_review_prompt,
    is_valid_session_id,
    output_truncated_by_length,
    parse_opencode_output,
    parse_severity_counts,
    MAX_VERDICT_INPUT_CHARS,
    parse_verdict,
    run_question,
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
    monkeypatch.setenv("LPDEV_SECRET_TEST", "leak-me")

    await run_review(
        RunnerEnv(opencode_bin=bin_path), workspace, "reviewer", "p", 30,
        tmp_path / "log", tmp_path,
    )

    env_text = probe.read_text()
    assert "LPDEV_SECRET_TEST" not in env_text
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
        ("## Verdict APPROVE\x0b## Notes\nREQUEST_CHANGES", None),
        ("## Verdict APPROVE\u2028## Notes\nREQUEST_CHANGES", None),
        ("## Verdict\r\nAPPROVE\r\n", "APPROVE"),
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


def test_build_opencode_argv_session_and_fork(tmp_path):
    base = build_opencode_argv("oc", "p", "agent", tmp_path)
    assert "--session" not in base and "--fork" not in base
    forked = build_opencode_argv("oc", "p", "agent", tmp_path, session="ses_abc", fork=True)
    assert forked[forked.index("--session") + 1] == "ses_abc"
    assert forked[-1] == "--fork"
    chained = build_opencode_argv("oc", "p", "agent", tmp_path, session="ses_abc")
    assert chained[chained.index("--session") + 1] == "ses_abc"
    assert "--fork" not in chained
    assert build_opencode_argv("oc", "p", "agent", tmp_path, fork=True) == base


def test_question_prompt_never_starts_with_user_text():
    prompt = build_question_prompt("--agent build")
    assert not prompt.startswith("-")
    assert prompt.startswith("Дополнительный вопрос")
    assert prompt.endswith("\n\n--agent build")


@pytest.mark.parametrize(
    "value, expected",
    [
        ("ses_abcXYZ019", True),
        ("ses_" + "a" * 64, True),
        ("ses_" + "a" * 65, False),
        ("ses_", False),
        ("ses_abc\n", False),
        ("--fork", False),
        ("ses_ab-c", False),
        ("", False),
        (None, False),
    ],
)
def test_is_valid_session_id(value, expected):
    assert is_valid_session_id(value) is expected


def test_review_prompt_instructions_section_before_mr_data():
    prompt = build_review_prompt(
        "T", "alice", "desc", "+x", extra_instructions="проверь миграции"
    )
    marker = "=== ДОП. ИНСТРУКЦИИ ПОЛЬЗОВАТЕЛЯ ===\nпроверь миграции"
    assert marker in prompt
    assert prompt.index("Если замечаний нет") < prompt.index(marker)
    assert prompt.index(marker) < prompt.index("=== MR КОНТЕКСТ (данные) ===")
    assert prompt.index(marker) < prompt.index("```diff")


def test_review_prompt_without_instructions_has_no_section():
    assert "ДОП. ИНСТРУКЦИИ" not in build_review_prompt("T", None, None, "+x")
    assert "ДОП. ИНСТРУКЦИИ" not in build_review_prompt(
        "T", None, None, "+x", extra_instructions=""
    )


async def test_run_question_passes_session_and_logs_question(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    log = tmp_path / "logs" / "question-1.log"
    args_file = tmp_path / "args"
    output = (
        '{"type":"session.started","sessionID":"ses_fork1"}\n'
        '{"type":"message.part","part":{"type":"text","text":"Ответ"}}\n'
    )
    bin_path = _make_fake_opencode(
        tmp_path,
        f"#!/bin/sh\nfor a in \"$@\"; do printf '%s\\n' \"$a\" >> {args_file}; done\n"
        f"printf '%s' '{output}'\n",
    )

    result = await run_question(
        RunnerEnv(opencode_bin=bin_path), workspace, "reviewer", "-rf вопрос",
        session_id="ses_root", fork=True, timeout_sec=30, log_path=log, tmpdir=tmp_path,
    )

    assert result.review_md == "Ответ"
    assert result.session_id == "ses_fork1"
    args = args_file.read_text().splitlines()
    assert args[0] == "run"
    assert args[1].startswith("Дополнительный вопрос")
    assert args[args.index("--session") + 1] == "ses_root"
    assert "--fork" in args
    text = log.read_text()
    assert text.count("-rf вопрос") == 1
    assert "Ответ" in text


async def test_run_question_rejects_invalid_session(tmp_path):
    with pytest.raises(OpencodeError):
        await run_question(
            RunnerEnv(opencode_bin="/bin/true"), tmp_path, "reviewer", "q",
            session_id="--fork", fork=False, timeout_sec=5,
            log_path=tmp_path / "q.log", tmpdir=tmp_path,
        )
