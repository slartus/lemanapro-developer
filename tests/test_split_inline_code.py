from __future__ import annotations

import time

import pytest

from mrwatch.review_findings import split_inline_code


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a `b` c", [("a ", False), ("b", True), (" c", False)]),
        ("``x`` y `z", [("x", True), (" y `z", False)]),
        ("`a``b`", [("a``b", True)]),
        ("` ` q", [("` ` q", False)]),
        ("`` a `` `b", [("a", True), (" `b", False)]),
        ("plain", [("plain", False)]),
        ("` a `", [("a", True)]),
        ("```x``` ``y`", [("x", True), (" ``y`", False)]),
        ("`a ``b`` c`", [("a ``b`` c", True)]),
        ("`x", [("`x", False)]),
    ],
)
def test_split_inline_code_segments(text, expected):
    assert split_inline_code(text) == expected


def test_split_inline_code_many_unmatched_widths_is_linear():
    head = "".join("`" * width + "x" for width in range(2, 900))
    text = head + "`x" * ((1_000_000 - len(head)) // 2)
    started = time.perf_counter()
    split_inline_code(text)
    assert time.perf_counter() - started < 1.0
