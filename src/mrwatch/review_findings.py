from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import islice

MAX_FINDINGS = 30
MAX_PARSE_INPUT_CHARS = 1_000_000
MAX_DIFF_INPUT_CHARS = 20_000_000
DIFF_CONTEXT_LINES = 6
FILE_CONTEXT_LINES = 4
MAX_SNIPPET_LINES = 40
MAX_SNIPPET_CHARS = 4000
MAX_TOTAL_CHARS = 60_000
MAX_FILE_BYTES = 2 * 1024 * 1024
BINARY_SNIFF_BYTES = 8192
MAX_LINE_NUMBER = 10_000_000
MAX_PATH_LENGTH = 512

FINDING_LINE = re.compile(r"^[ \t]*[-*][ \t]*\[(BLOCKER|MAJOR|MINOR|NIT)\][ \t]*(.*)$")
FENCE_OPEN = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
QUOTE_LINE = re.compile(r"^[ \t]{0,3}>")
REFERENCE = re.compile(
    r"^[`*_]*(?P<path>[^\s`:*]+?)(?::(?P<start>\d+)(?:[-–](?P<end>\d+))?)?[`*_]*(?=[\s,;)—–]|$)"
)
HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass(frozen=True)
class Finding:
    index: int
    severity: str
    path: str | None
    start: int | None
    end: int | None


@dataclass
class _DiffLine:
    new_no: int | None
    text: str


@dataclass
class _Hunk:
    new_start: int
    new_count: int
    lines: list[_DiffLine]


def iter_findings(review_md: str | None) -> Iterator[Finding]:
    if not review_md or len(review_md) > MAX_PARSE_INPUT_CHARS:
        return
    fence: str | None = None
    index = 0
    for raw in review_md.splitlines():
        if fence is not None:
            stripped = raw.strip()
            if stripped and stripped[0] == fence[0] and len(stripped) >= len(fence) and not stripped.strip(fence[0]):
                fence = None
            continue
        opening = FENCE_OPEN.match(raw)
        if opening:
            fence = opening.group(1)
            continue
        if QUOTE_LINE.match(raw):
            continue
        match = FINDING_LINE.match(raw)
        if not match:
            continue
        path, start, end = _parse_reference(match.group(2))
        yield Finding(index, match.group(1), path, start, end)
        index += 1


def parse_findings(review_md: str | None) -> list[Finding]:
    return list(islice(iter_findings(review_md), MAX_FINDINGS))


def _parse_reference(rest: str) -> tuple[str | None, int | None, int | None]:
    ref = REFERENCE.match(rest.strip()[:MAX_PATH_LENGTH + 32])
    if not ref:
        return None, None, None
    path = ref.group("path")
    if len(path) > MAX_PATH_LENGTH or not ("/" in path or "." in path):
        return None, None, None
    if ref.group("start") is None:
        return path, None, None
    start = int(ref.group("start")[:9])
    end = int(ref.group("end")[:9]) if ref.group("end") else start
    if start < 1 or start > MAX_LINE_NUMBER:
        return path, None, None
    if end < start:
        end = start
    return path, start, min(end, MAX_LINE_NUMBER)


def normalize_path(path: str) -> str | None:
    if not path or len(path) > MAX_PATH_LENGTH or "\0" in path or "\\" in path:
        return None
    if path.startswith("/"):
        return None
    parts = [p for p in path.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." or p.casefold() == ".git" for p in parts):
        return None
    return "/".join(parts)


def _path_candidates(path: str) -> list[str]:
    normalized = normalize_path(path)
    if normalized is None:
        return []
    candidates = [normalized]
    head, _, tail = normalized.partition("/")
    if head in ("a", "b") and tail:
        candidates.append(tail)
    return candidates


def parse_diff_hunks(diff: str | None, wanted: set[str] | None = None) -> dict[str, list[_Hunk]]:
    files: dict[str, list[_Hunk]] = {}
    if not diff or len(diff) > MAX_DIFF_INPUT_CHARS:
        return files
    current: list[_Hunk] | None = None
    hunk: _Hunk | None = None
    old_left = new_left = 0
    new_no = 0
    for raw in diff.split("\n"):
        if hunk is not None and (old_left > 0 or new_left > 0):
            marker = raw[:1]
            if marker == "\\":
                continue
            if marker == "+":
                hunk.lines.append(_DiffLine(new_no, raw))
                new_no += 1
                new_left -= 1
            elif marker == "-":
                hunk.lines.append(_DiffLine(None, raw))
                old_left -= 1
            else:
                hunk.lines.append(_DiffLine(new_no, " " + raw[1:] if raw else " "))
                new_no += 1
                new_left -= 1
                old_left -= 1
            continue
        if raw.startswith("+++ "):
            target = raw[4:].split("\t")[0]
            if target.startswith("b/") and (wanted is None or target[2:] in wanted):
                current = files.setdefault(target[2:], [])
            else:
                current = None
            hunk = None
            continue
        header = HUNK_HEADER.match(raw)
        if header and current is not None:
            old_left = int(header.group(2)) if header.group(2) is not None else 1
            new_left = int(header.group(4)) if header.group(4) is not None else 1
            new_no = int(header.group(3))
            hunk = _Hunk(new_no, new_left, [])
            current.append(hunk)
    return files


def _limit_snippet(lines: list[str]) -> str:
    lines = lines[:MAX_SNIPPET_LINES]
    code = "\n".join(lines)
    while len(code) > MAX_SNIPPET_CHARS and len(lines) > 1:
        lines.pop()
        code = "\n".join(lines)
    return code[:MAX_SNIPPET_CHARS]


def _diff_snippet(hunks: list[_Hunk], start: int, end: int) -> str | None:
    for hunk in hunks:
        if not (hunk.new_start <= start < hunk.new_start + hunk.new_count):
            continue
        numbers = [i for i, line in enumerate(hunk.lines) if line.new_no is not None]
        first = next((i for i in numbers if hunk.lines[i].new_no >= start), None)
        if first is None:
            return None
        last = first
        for i in numbers:
            if i >= first and hunk.lines[i].new_no <= end:
                last = i
        lo = max(0, first - DIFF_CONTEXT_LINES)
        hi = min(len(hunk.lines), last + DIFF_CONTEXT_LINES + 1)
        return _limit_snippet([line.text for line in hunk.lines[lo:hi]])
    return None


def _git_identity(root_fd: int) -> tuple[int, int] | None:
    try:
        info = os.stat(".git", dir_fd=root_fd, follow_symlinks=False)
    except OSError:
        return None
    return info.st_dev, info.st_ino


def _read_regular_file(
    root_fd: int,
    parts: list[str],
    forbidden: tuple[int, int] | None = None,
) -> bytes | None:
    opened: list[int] = []
    current = root_fd
    if forbidden is None:
        forbidden = _git_identity(root_fd)
    try:
        for name in parts[:-1]:
            current = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=current)
            opened.append(current)
            info = os.fstat(current)
            if forbidden is not None and (info.st_dev, info.st_ino) == forbidden:
                return None
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current)
        opened.append(fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            return None
        chunks: list[bytes] = []
        total = 0
        while total <= MAX_FILE_BYTES:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        data = b"".join(chunks)
        if len(data) > MAX_FILE_BYTES:
            return None
        return data
    except OSError:
        return None
    finally:
        for fd in opened:
            os.close(fd)


def _file_snippet(root_fd: int, path: str, start: int, end: int) -> str | None:
    data = _read_regular_file(root_fd, path.split("/"))
    if data is None or b"\0" in data[:BINARY_SNIFF_BYTES]:
        return None
    lines = [raw.removesuffix(b"\r").decode("utf-8", errors="replace") for raw in data.split(b"\n")]
    if lines and lines[-1] == "":
        lines.pop()
    if start > len(lines):
        return None
    lo = max(1, start - FILE_CONTEXT_LINES)
    hi = min(len(lines), end + FILE_CONTEXT_LINES)
    return _limit_snippet(lines[lo - 1:hi])


def extract_snippets(review_md: str | None, diff: str | None, workspace) -> list[dict]:
    findings = [f for f in parse_findings(review_md) if f.path and f.start is not None]
    if not findings:
        return []
    wanted = {c for f in findings for c in _path_candidates(f.path)}
    hunks_by_file = parse_diff_hunks(diff, wanted)
    root_fd: int | None = None
    try:
        root_fd = os.open(os.fspath(workspace), os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        root_fd = None
    snippets: list[dict] = []
    total = 0
    try:
        for finding in findings:
            candidates = _path_candidates(finding.path)
            code: str | None = None
            source = ""
            used = ""
            for candidate in candidates:
                hunks = hunks_by_file.get(candidate)
                if hunks:
                    code = _diff_snippet(hunks, finding.start, finding.end)
                    if code:
                        source, used = "diff", candidate
                        break
            if not code and root_fd is not None:
                for candidate in candidates:
                    code = _file_snippet(root_fd, candidate, finding.start, finding.end)
                    if code:
                        source, used = "file", candidate
                        break
            if not code:
                continue
            if total + len(code) > MAX_TOTAL_CHARS:
                break
            total += len(code)
            snippets.append({
                "index": finding.index,
                "severity": finding.severity,
                "path": used,
                "start": finding.start,
                "end": finding.end,
                "source": source,
                "code": code,
            })
    finally:
        if root_fd is not None:
            os.close(root_fd)
    return snippets


def dump_snippets(snippets: list[dict]) -> str | None:
    return json.dumps(snippets, ensure_ascii=False) if snippets else None


def load_snippets(raw: str | None) -> list[dict]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    result: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        code, path, severity = item.get("code"), item.get("path"), item.get("severity")
        start, end, index = item.get("start"), item.get("end"), item.get("index")
        if not (isinstance(code, str) and isinstance(path, str) and isinstance(severity, str)):
            continue
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in (start, end, index)):
            continue
        source = item.get("source") if item.get("source") in ("diff", "file") else "file"
        result.append({
            "index": index, "severity": severity, "path": path,
            "start": start, "end": end, "source": source, "code": code,
        })
    return result
