"""Tests for AutoCoordinator._compute_md_delta — focus on the byte-prefix delta guard.

A write that EXTENDS the last line of `prev` (when prev had no trailing
newline) and appends more lines is a *byte* prefix but not a *line* prefix.
Treating it as a pure append silently drops the modified last line from the
delivered delta, so the guard must require prev to end with "\\n".

Run: pytest tests/test_md_delta.py   (or: python tests/test_md_delta.py)
"""
from __future__ import annotations

import sys
from pathlib import Path

_PARENT = Path(__file__).resolve().parent.parent
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))


def _delta(prev, curr):
    from main import AutoCoordinator
    return AutoCoordinator._compute_md_delta(prev, curr, "blockers.md")


def test_clean_append_is_append():
    # prev ends with \n → genuine line-aligned append.
    body, kind, added, removed = _delta("L1\nL2\n", "L1\nL2\nL3\n")
    assert kind == "append", f"expected append, got {kind}"
    assert "L3" in body and "L1" not in body  # only the appended suffix


def test_last_line_extension_is_not_append():
    # prev has NO trailing newline; the write extends the open last line AND
    # appends. Must NOT be classified 'append' (which would drop "L2 more").
    body, kind, added, removed = _delta("L1\nL2", "L1\nL2 more\nL3")
    assert kind != "append", f"byte-prefix-but-not-line-prefix must not be append (got {kind})"
    # The modified last line must be visible in the delta, not silently dropped.
    assert "more" in body, f"extended last line dropped from delta: {body!r}"


def test_noop_and_first():
    assert _delta("a\n", "a\n")[1] == "noop"
    assert _delta("", "a\nb")[1] == "first"


if __name__ == "__main__":
    test_clean_append_is_append()
    test_last_line_extension_is_not_append()
    test_noop_and_first()
    print("OK: _compute_md_delta tests passed")
