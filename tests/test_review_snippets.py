from __future__ import annotations

import json
import os
import sqlite3

import pytest

from mrwatch import review_findings as rf
from mrwatch.db import (
    SCHEMA_VERSION,
    connect,
    finish_review,
    get_review,
    init_schema,
    retry_review,
)
from mrwatch.review_findings import (
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
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 9
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

    monkeypatch.setattr("mrwatch.db._migrate_to_v9", boom)
    with pytest.raises(sqlite3.OperationalError):
        init_schema(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 8
    monkeypatch.undo()
    init_schema(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 9
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

    import mrwatch.worker as worker_mod

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
