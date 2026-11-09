from __future__ import annotations

import json
import os
import sqlite3

import pytest

from lpdev import review_findings as rf
from lpdev.db import (
    SCHEMA_VERSION,
    connect,
    finish_review,
    get_review,
    init_schema,
    retry_review,
)
from lpdev.review_findings import (
    extract_snippets,
    load_snippets,
    normalize_path,
    parse_diff_hunks,
    parse_findings,
)
from tests.test_web_main import _client
from tests.test_worker import _fake_opencode, _queued_review, _worker


def test_parse_reference_formats():
    md = (
        "- [BLOCKER] src/A.kt:42 — x — y\n"
        "* [MAJOR] `src/B.kt:10-20` — x\n"
        "- [MINOR] **src/C.py:5** — x\n"
        "- [NIT] src/D.py — без строки\n"
        "- [NIT] Просто текст — без пути\n"
        "- [MINOR] a/src/E.py:7, ещё\n"
    )
    found = parse_findings(md)
    assert [(f.index, f.severity, f.path, f.start, f.end) for f in found] == [
        (0, "BLOCKER", "src/A.kt", 42, 42),
        (1, "MAJOR", "src/B.kt", 10, 20),
        (2, "MINOR", "src/C.py", 5, 5),
        (3, "NIT", "src/D.py", None, None),
        (4, "NIT", None, None, None),
        (5, "MINOR", "a/src/E.py", 7, 7),
    ]


def test_parse_ignores_fence_and_quote():
    md = "```\n- [MAJOR] a.py:1 — x\n```\n> - [MAJOR] b.py:2 — x\n~~~~\n- [NIT] c.py:3\n~~~~\n- [NIT] d.py:4 — x\n"
    assert [f.path for f in parse_findings(md)] == ["d.py"]


def test_parse_limits():
    md = "\n".join(f"- [NIT] f{i}.py:{i + 1} — x" for i in range(50))
    assert len(parse_findings(md)) == rf.MAX_FINDINGS
    assert parse_findings("x" * (rf.MAX_PARSE_INPUT_CHARS + 1)) == []
    assert parse_findings(None) == []
    assert parse_findings("- [NIT] a.py:99999999999 — x")[0].start is None
    assert parse_findings("- [NIT] a.py:9-3 — x")[0].end == 9


DIFF = (
    "diff --git a/app.py b/app.py\n"
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1,3 +1,4 @@\n"
    " one\n"
    "-two\n"
    "+TWO\n"
    "+two-b\n"
    " three\n"
    "\\ No newline at end of file\n"
    "@@ -50,2 +51,2 @@\n"
    " keep\n"
    "-old\n"
    "+new\n"
    "diff --git a/gone.py b/gone.py\n"
    "--- a/gone.py\n"
    "+++ /dev/null\n"
    "@@ -1 +0,0 @@\n"
    "-x\n"
)


def test_parse_diff_hunks():
    hunks = parse_diff_hunks(DIFF)
    assert set(hunks) == {"app.py"}
    assert len(hunks["app.py"]) == 2
    assert [(ln.new_no, ln.text) for ln in hunks["app.py"][0].lines] == [
        (1, " one"), (None, "-two"), (2, "+TWO"), (3, "+two-b"), (4, " three"),
    ]


def test_snippet_from_diff_has_priority_over_file(tmp_path):
    (tmp_path / "app.py").write_text("\n".join(f"file{i}" for i in range(1, 100)))
    md = "- [MAJOR] b/app.py:3 — x\n- [MINOR] app.py:52 — x\n"
    snippets = extract_snippets(md, DIFF, tmp_path)
    assert [s["source"] for s in snippets] == ["diff", "diff"]
    assert snippets[0]["code"] == " one\n-two\n+TWO\n+two-b\n three"
    assert snippets[0]["path"] == "app.py"
    assert snippets[1]["code"] == " keep\n-old\n+new"


def test_diff_context_trimmed_to_six_lines():
    body = "".join(f" c{i}\n" for i in range(1, 41))
    diff = f"+++ b/f.py\n@@ -1,40 +1,40 @@\n{body}"
    snippet = extract_snippets("- [NIT] f.py:20 — x", diff, "/nonexistent")[0]
    lines = snippet["code"].split("\n")
    assert lines[0] == " c14" and lines[-1] == " c26" and len(lines) == 13


def test_snippet_from_file_fallback(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "m.py").write_text("\n".join(f"l{i}" for i in range(1, 31)) + "\n")
    snippets = extract_snippets("- [NIT] src/m.py:10-11 — x\n- [NIT] src/m.py:99 — x", None, tmp_path)
    assert len(snippets) == 1
    assert snippets[0]["source"] == "file"
    assert snippets[0]["code"].split("\n") == [f"l{i}" for i in range(6, 16)]
    assert (snippets[0]["start"], snippets[0]["end"]) == (10, 11)


def test_file_not_found_no_snippet(tmp_path):
    assert extract_snippets("- [NIT] nope.py:1 — x", None, tmp_path) == []


@pytest.mark.parametrize("path", ["../x.py", "a/../../x.py", "/etc/passwd", "a\\b.py", "x\0.py", ".git/config", ""])
def test_normalize_rejects(path):
    assert normalize_path(path) is None


def test_path_traversal_and_absolute_denied(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (tmp_path / "secret.txt").write_text("secret\n")
    md = "- [NIT] ../secret.txt:1 — x\n- [NIT] a/../../secret.txt:1 — x\n- [NIT] " + f"{tmp_path}/secret.txt:1 — x\n"
    assert extract_snippets(md, None, ws) == []


def test_symlink_outside_and_inside_denied(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret\n")
    (ws / "real.py").write_text("ok\n")
    os.symlink(outside, ws / "out.py")
    os.symlink(ws / "real.py", ws / "in.py")
    (tmp_path / "outdir").mkdir()
    (tmp_path / "outdir" / "f.py").write_text("hidden\n")
    os.symlink(tmp_path / "outdir", ws / "linkdir")
    md = "- [NIT] out.py:1 — x\n- [NIT] in.py:1 — x\n- [NIT] linkdir/f.py:1 — x\n- [NIT] real.py:1 — x\n"
    snippets = extract_snippets(md, None, ws)
    assert [s["path"] for s in snippets] == ["real.py"]


def test_directory_binary_and_huge_denied(tmp_path):
    (tmp_path / "d.py").mkdir()
    (tmp_path / "bin.py").write_bytes(b"ab\0cd\nline\n")
    (tmp_path / "huge.py").write_bytes(b"x\n" * (rf.MAX_FILE_BYTES // 2 + 10))
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("token\n")
    md = "".join(f"- [NIT] {p}:1 — x\n" for p in ("d.py", "bin.py", "huge.py", ".git/config"))
    assert extract_snippets(md, None, tmp_path) == []


def test_invalid_utf8_replaced(tmp_path):
    (tmp_path / "u.py").write_bytes(b"caf\xe9\n")
    assert extract_snippets("- [NIT] u.py:1 — x", None, tmp_path)[0]["code"] == "caf�"


def test_snippet_size_limits(tmp_path):
    (tmp_path / "long.py").write_text("\n".join("y" * 300 for _ in range(200)))
    snippet = extract_snippets("- [NIT] long.py:10-150 — x", None, tmp_path)[0]
    assert len(snippet["code"]) <= rf.MAX_SNIPPET_CHARS
    assert len(snippet["code"].split("\n")) <= rf.MAX_SNIPPET_LINES
    (tmp_path / "wide.py").write_text("z" * 20000)
    assert len(extract_snippets("- [NIT] wide.py:1 — x", None, tmp_path)[0]["code"]) == rf.MAX_SNIPPET_CHARS


def test_total_limit(tmp_path, monkeypatch):
    (tmp_path / "t.py").write_text("\n".join("w" * 100 for _ in range(100)))
    monkeypatch.setattr(rf, "MAX_TOTAL_CHARS", 1000)
    md = "\n".join("- [NIT] t.py:50 — x" for _ in range(30))
    snippets = extract_snippets(md, None, tmp_path)
    assert 0 < len(snippets) < 30
    assert sum(len(s["code"]) for s in snippets) <= 1000


def test_migration_v9_idempotent(tmp_path):
    conn = connect(tmp_path / "m.db")
    init_schema(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 11
    conn.execute("PRAGMA user_version = 8")
    conn.commit()
    init_schema(conn)
    init_schema(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(review)")]
    assert cols.count("snippets_json") == 1
    conn.close()


def test_migration_v9_failure_rolls_back(tmp_path, monkeypatch):
    conn = connect(tmp_path / "m.db")
    init_schema(conn)
    conn.execute("ALTER TABLE review DROP COLUMN snippets_json")
    conn.execute("PRAGMA user_version = 8")
    conn.commit()

    def boom(*a, **k):
        raise sqlite3.OperationalError("boom")

    monkeypatch.setattr("lpdev.db._migrate_to_v9", boom)
    with pytest.raises(sqlite3.OperationalError):
        init_schema(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 8
    monkeypatch.undo()
    init_schema(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 11
    conn.close()


def test_finish_stores_and_retry_clears(ctx):
    ctx_obj, review_id = ctx
    payload = rf.dump_snippets([{"index": 0}])
    finish_review(ctx_obj.conn, review_id, "done", review_md="x", snippets_json=payload)
    assert get_review(ctx_obj.conn, review_id)["snippets_json"] == payload
    assert retry_review(ctx_obj.conn, review_id)
    assert get_review(ctx_obj.conn, review_id)["snippets_json"] is None


def test_load_snippets_rejects_garbage():
    assert load_snippets("not json") == []
    assert load_snippets('{"a": 1}') == []
    assert load_snippets('[1, {"code": 1}]') == []
    assert load_snippets(None) == []


async def test_review_page_escapes_snippets(ctx):
    ctx_obj, review_id = ctx
    snippets = [
        {"index": 0, "severity": "MAJOR", "path": "<b>p.py</b>", "start": 1, "end": 2,
         "source": "diff", "code": "+<script>alert(1)</script>\n-old\n ctx"},
        {"index": 1, "severity": "NIT", "path": "q.py", "start": 3, "end": 3,
         "source": "file", "code": "+not diff"},
    ]
    finish_review(ctx_obj.conn, review_id, "done", review_md="ok", snippets_json=json.dumps(snippets))
    async with _client(ctx_obj) as client:
        page = await client.get(f"/reviews/{review_id}")
    text = page.text
    assert "Код к находкам" in text
    assert "<script>alert(1)" not in text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text
    assert "&lt;b&gt;p.py&lt;/b&gt;:1-2" in text
    assert 'class="cl add">+&lt;script' in text
    assert 'class="cl del">-old' in text
    assert 'class="cl">+not diff' in text


async def test_review_page_without_snippets(ctx):
    ctx_obj, review_id = ctx
    finish_review(ctx_obj.conn, review_id, "done", review_md="ok")
    async with _client(ctx_obj) as client:
        page = await client.get(f"/reviews/{review_id}")
    assert "Код к находкам" not in page.text


async def test_worker_extracts_snippets_before_workspace_removal(tmp_path, db, origin):
    review_id = _queued_review(db, origin)
    text = "- [MAJOR] app.py:1 — плохо — чинить"
    ndjson = json.dumps({"type": "message.part", "part": {"type": "text", "text": text}})
    opencode_bin = _fake_opencode(tmp_path, f"#!/bin/sh\ncat <<'EOF'\n{ndjson}\nEOF\n")
    worker = _worker(tmp_path, db, origin, opencode_bin)
    seen: list[bool] = []
    original = rf.extract_snippets

    def spy(review_md, diff, workspace):
        seen.append(os.path.isdir(workspace))
        return original(review_md, diff, workspace)

    import lpdev.worker as worker_mod

    worker_mod.extract_snippets = spy
    try:
        assert await worker.process_one() is True
    finally:
        worker_mod.extract_snippets = original
    assert seen == [True]
    review = get_review(db, review_id)
    assert review["status"] == "done"
    stored = load_snippets(review["snippets_json"])
    assert len(stored) == 1
    assert stored[0]["source"] == "diff"
    assert "+print('hello')" in stored[0]["code"]
    assert not (tmp_path / "workspaces" / "10" / "1" / str(review_id)).exists()


@pytest.mark.parametrize("path", [".GIT/config", ".Git/HEAD", "a/.gIt/x", "sub/../.git/config"])
def test_normalize_path_rejects_git_case_variants(path):
    assert normalize_path(path) is None


def test_read_regular_file_rejects_forbidden_directory_identity(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "f.txt").write_text("data")
    info = os.stat(tmp_path / "sub")
    root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert rf._read_regular_file(root_fd, ["sub", "f.txt"], (info.st_dev, info.st_ino)) is None
        assert rf._read_regular_file(root_fd, ["sub", "f.txt"], (0, 0)) == b"data"
    finally:
        os.close(root_fd)


def test_read_regular_file_default_guard_blocks_git_dir(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("secret")
    root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert rf._read_regular_file(root_fd, [".git", "config"]) is None
    finally:
        os.close(root_fd)


def test_severity_counts_ignore_fenced_blocks():
    from lpdev.opencode_runner import parse_severity_counts

    md = "- [MAJOR] a.py:1 — x\n\n```diff\n- [MAJOR] fix\n```\n"
    assert parse_severity_counts(md) == (0, 1, 0, 0)


def test_iter_findings_not_limited():
    md = "\n".join(f"- [NIT] a.py:{i} — x" for i in range(1, rf.MAX_FINDINGS + 6))
    assert len(list(rf.iter_findings(md))) == rf.MAX_FINDINGS + 5
    assert len(parse_findings(md)) == rf.MAX_FINDINGS


def test_snippet_line_numbers_match_git(tmp_path):
    (tmp_path / "f.txt").write_bytes(b"one\x0ctwo\nthree\xc2\x85x\xe2\x80\xa8y\nfour\r\nfive\n")
    snippets = extract_snippets("- [NIT] f.txt:3 — x", None, tmp_path)
    assert snippets[0]["code"].split("\n")[2] == "four"
    assert "\r" not in snippets[0]["code"]


def test_parse_diff_hunks_filters_wanted():
    full = parse_diff_hunks(DIFF)
    some = next(iter(full))
    filtered = parse_diff_hunks(DIFF, {some})
    assert set(filtered) == {some}
    assert len(filtered[some]) == len(full[some])


def test_parse_diff_hunks_foreign_file_body_not_parsed_as_header():
    diff = (
        "--- a/other.py\n+++ b/other.py\n@@ -1,2 +1,3 @@\n"
        " x\n++ b/app.py\n+y\n"
        "@@ -9 +9 @@\n-q\n+r\n"
        "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-a\n+mine\n"
    )
    for wanted in (None, {"app.py"}):
        hunks = parse_diff_hunks(diff, wanted)
        assert [ln.text for h in hunks["app.py"] for ln in h.lines] == ["-a", "+mine"]


def test_severity_counts_truncate_oversized_review():
    from lpdev.opencode_runner import parse_severity_counts

    head = "- [MAJOR] a.py:1 — x\n- [NIT] b.py:2 — y\n"
    md = head + "z" * rf.MAX_PARSE_INPUT_CHARS + "\n- [BLOCKER] c.py:3 — tail\n"
    assert parse_severity_counts(md) == (0, 1, 0, 1)


def test_finding_line_and_snippet_line_with_fence_and_quote(tmp_path):
    (tmp_path / "m.py").write_text("\n".join(f"l{i}" for i in range(1, 30)))
    md = "intro\r\n```\r\n- [MAJOR] x.py:1 — fake\r\n```\r\n> - [NIT] y.py:1\r\n- [MINOR] m.py:5 — real\r\n"
    found = parse_findings(md)
    assert [(f.index, f.line) for f in found] == [(0, 5)]
    snippets = extract_snippets(md, None, tmp_path)
    assert snippets[0]["line"] == 5
    assert rf.split_review_lines(md)[5].startswith("- [MINOR]")


def test_load_snippets_old_record_without_line():
    raw = json.dumps([{"index": 0, "severity": "NIT", "path": "a.py", "start": 1, "end": 1, "source": "file", "code": "x"}])
    assert load_snippets(raw)[0]["line"] is None


def _sn(index, severity, path, line, code="c"):
    return {"index": index, "line": line, "severity": severity, "path": path,
            "start": 1, "end": 1, "source": "file", "code": code}


def test_layout_inserts_after_finding_line():
    md = "head\n- [MAJOR] a.py:1 — x\n  fix: y\n- [NIT] b.py:2 — z"
    rows, orphans = rf.layout_review(md, [_sn(0, "MAJOR", "a.py", 1), _sn(1, "NIT", "b.py", 3)])
    assert orphans == []
    assert [len(r["snippets"]) for r in rows] == [0, 1, 0, 1]
    assert rows[1]["css"] == "major"


def test_layout_orphans_old_and_forged():
    md = "- [MAJOR] a.py:1 — x\ntext\n- [NIT] b.py:2 — z"
    snippets = [
        _sn(0, "MAJOR", "a.py", None),
        _sn(0, "MAJOR", "a.py", 1),
        _sn(0, "MAJOR", "a.py", 2),
        _sn(1, "NIT", "b.py", 0),
        _sn(1, "MINOR", "b.py", 2),
        _sn(1, "NIT", "other.py", 2),
        _sn(1, "NIT", "b.py", 99),
    ]
    rows, orphans = rf.layout_review(md, snippets)
    assert len(orphans) == 7
    assert [len(r["snippets"]) for r in rows] == [0, 0, 0]


async def test_review_page_inline_snippet_order_and_escape(ctx):
    ctx_obj, review_id = ctx
    md = "- [MAJOR] a.py:1 — <script>x</script>\n  fix: do\n- [NIT] z.py:1 — q"
    snippets = [
        dict(_sn(0, "MAJOR", "a.py", 0), source="diff", code="+<script>alert(1)</script>\n-old"),
        _sn(1, "NIT", "z.py", 7, code="orphan-code"),
    ]
    finish_review(ctx_obj.conn, review_id, "done", review_md=md, snippets_json=json.dumps(snippets))
    async with _client(ctx_obj) as client:
        text = (await client.get(f"/reviews/{review_id}")).text
    assert "<script>alert(1)" not in text and "<script>x</script>" not in text
    assert text.index("&lt;script&gt;x") < text.index('class="cl add">+&lt;script') < text.index("fix: do")
    assert text.index("orphan-code") > text.index("Код к находкам")
    assert text.count("Код к находкам") == 1


def _kinds(rows):
    return [r["kind"] for r in rows]


def test_layout_fence_with_lang_and_without():
    rows, _ = rf.layout_review("a\n```js\nx = 1\n\ny\n```\nb\n```\nplain\n```", [])
    assert _kinds(rows) == ["line", "code", "line", "code"]
    assert (rows[1]["lang"], rows[1]["lines"]) == ("js", ["x = 1", "", "y"])
    assert (rows[3]["lang"], rows[3]["lines"]) == ("", ["plain"])


def test_layout_tilde_and_long_fence():
    rows, _ = rf.layout_review("~~~yaml\na: 1\n```\n~~~\n````kotlin\n```\nx\n```\n````\nend", [])
    assert _kinds(rows) == ["code", "code", "line"]
    assert rows[0]["lines"] == ["a: 1", "```"]
    assert rows[1]["lines"] == ["```", "x", "```"]
    assert rows[1]["lang"] == "kotlin"


def test_layout_indented_fence_strips_prefix():
    rows, _ = rf.layout_review("- item\n  ```js\n  if (a) {\n    b();\n  }\n  ```\n- next", [])
    assert _kinds(rows) == ["line", "code", "line"]
    assert rows[1]["lines"] == ["if (a) {", "  b();", "}"]
    assert rows[1]["indent"] == 2


def test_layout_unclosed_fence_runs_to_end():
    rows, _ = rf.layout_review("a\n```py\nx\ny", [])
    assert _kinds(rows) == ["line", "code"]
    assert rows[1]["lines"] == ["x", "y"]


def test_layout_forged_lang_dropped():
    rows, _ = rf.layout_review('```"><script>alert(1)</script>\nx\n```\n```' + "a" * 21 + "\ny\n```", [])
    assert [r["lang"] for r in rows] == ["", ""]


def test_layout_finding_inside_fence_not_highlighted():
    rows, _ = rf.layout_review("```\n- [MAJOR] a.py:1 — x\n```", [])
    assert rows[0]["css"] == "" and rows[0]["lines"] == ["- [MAJOR] a.py:1 — x"]


def test_layout_snippet_stays_with_finding_before_fix_block():
    md = "intro\n```\nskip\n```\n- [MAJOR] a.py:1 — x\n```js\nfix\n```"
    rows, orphans = rf.layout_review(md, [_sn(0, "MAJOR", "a.py", 4)])
    assert orphans == []
    assert _kinds(rows) == ["line", "code", "line", "code"]
    assert len(rows[2]["snippets"]) == 1 and rows[3]["snippets"] == []


def test_inline_code_segments():
    assert rf.split_inline_code("a `x` b") == [("a ", False), ("x", True), (" b", False)]
    assert rf.split_inline_code("a ``x ` y`` b") == [("a ", False), ("x ` y", True), (" b", False)]
    assert rf.split_inline_code("a `x b") == [("a `x b", False)]
    assert rf.split_inline_code("``a` b") == [("``a` b", False)]
    assert rf.split_inline_code("") == [("", False)]
    assert rf.split_inline_code("`` ``") == [("`` ``", False)]


async def test_review_page_renders_code_blocks_and_escapes(ctx):
    ctx_obj, review_id = ctx
    md = (
        "- [MAJOR] a.py:1 — use `<script>x</script>` here\n"
        "  ```\"><script>bad</script>\n  <b>raw</b>\n  ```\n"
        "```js\nlet a = '<img src=x>'\n```\ntail `open"
    )
    finish_review(ctx_obj.conn, review_id, "done", review_md=md)
    async with _client(ctx_obj) as client:
        text = (await client.get(f"/reviews/{review_id}")).text
    assert "<script>x</script>" not in text and "<script>bad" not in text
    assert "<b>raw</b>" not in text and "<img src=x>" not in text
    assert "<code>&lt;script&gt;x&lt;/script&gt;</code>" in text
    assert '<span class="code-lang">js</span>' in text
    assert text.count('<pre class="code-block">') == 2
    assert "tail `open" in text
    assert "```" not in text


async def test_review_page_without_fence_unchanged(ctx):
    ctx_obj, review_id = ctx
    finish_review(ctx_obj.conn, review_id, "done", review_md="- [NIT] a.py:1 — x\nplain <b>t</b>")
    async with _client(ctx_obj) as client:
        text = (await client.get(f"/reviews/{review_id}")).text
    assert '<span class="ln nit">- [NIT] a.py:1 — x</span><span class="ln">plain &lt;b&gt;t&lt;/b&gt;</span>' in text
    assert "code-block" not in text.split("</style>")[1]


async def test_review_page_heading_only_for_markdown_headings(ctx):
    ctx_obj, review_id = ctx
    md = "## Summary\n#123 fix\n#!/bin/sh\n####### x\n###### six"
    finish_review(ctx_obj.conn, review_id, "done", review_md=md)
    async with _client(ctx_obj) as client:
        text = (await client.get(f"/reviews/{review_id}")).text
    assert '<span class="ln h">## Summary</span>' in text
    assert '<span class="ln h">###### six</span>' in text
    assert '<span class="ln">#123 fix</span>' in text
    assert '<span class="ln">#!/bin/sh</span>' in text
    assert '<span class="ln">####### x</span>' in text
