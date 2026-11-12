""" — tool result auto-truncation self-test.

Verifies _truncate_lines + truncate_tool_result thresholds.

Run via:
  - python tests/test_tool_truncation.py
  - python main.py --md-watch-test    (aggregated)
  - pytest tests/test_tool_truncation.py
"""
from __future__ import annotations

import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_PARENT = _THIS_DIR.parent
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))


def run_self_test() -> dict:
    from main import (
        _truncate_lines,
        truncate_tool_result,
        TOOL_TRUNC_BASH_LINES,
        TOOL_TRUNC_GREP_LINES,
        TOOL_TRUNC_GENERIC_LINES,
        TOOL_TRUNC_KEEP_HEAD,
        TOOL_TRUNC_KEEP_TAIL,
    )

    details: list[str] = []
    passed = failed = 0

    def check(label: str, got, want) -> None:
        nonlocal passed, failed
        if got == want:
            passed += 1
            details.append(f"PASS  {label}: got={got!r}")
        else:
            failed += 1
            details.append(f"FAIL  {label}: got={got!r}, want={want!r}")

    # ---- test_tool_truncate_under_threshold: short output passes through ----
    short = "\n".join(f"line {i}" for i in range(50))
    check("under_thresh[bash]", truncate_tool_result("Bash", short), short)
    check("under_thresh[grep]", truncate_tool_result("Grep", short), short)
    check("under_thresh[generic]", truncate_tool_result("WebFetch", short), short)

    # ---- test_tool_truncate_bash_long: 1000-line bash → first 100 + last 100 ----
    long_text = "\n".join(f"line {i}" for i in range(1000))
    out = truncate_tool_result("Bash", long_text)
    out_lines = out.splitlines()
    # Expected: 100 head + 1 marker + 100 tail = 201 lines.
    expected_total = TOOL_TRUNC_KEEP_HEAD + 1 + TOOL_TRUNC_KEEP_TAIL
    check("bash_long[total lines]", len(out_lines), expected_total)
    check("bash_long[first line]", out_lines[0], "line 0")
    check("bash_long[head boundary]", out_lines[TOOL_TRUNC_KEEP_HEAD - 1],
          f"line {TOOL_TRUNC_KEEP_HEAD - 1}")
    # Marker line shape.
    marker = out_lines[TOOL_TRUNC_KEEP_HEAD]
    check("bash_long[marker prefix]", marker.startswith("[CLAUDE_TRUNC: kept="), True)
    check("bash_long[marker has dropped]", "dropped=" in marker, True)
    # Tail check: last line of out is last line of input.
    check("bash_long[last line]", out_lines[-1], "line 999")

    # ---- Grep threshold (300 lines) ----
    grep_text = "\n".join(f"g{i}" for i in range(500))
    grep_out = truncate_tool_result("Grep", grep_text)
    check("grep_long[truncated]", len(grep_out.splitlines()) < 500, True)
    check("grep_long[has marker]", "[CLAUDE_TRUNC:" in grep_out, True)
    grep_under = "\n".join(f"g{i}" for i in range(TOOL_TRUNC_GREP_LINES))
    check("grep_under[passthrough]", truncate_tool_result("Grep", grep_under), grep_under)

    # ---- generic threshold (500 lines) ----
    gen_text = "\n".join(f"x{i}" for i in range(700))
    gen_out = truncate_tool_result("WebFetch", gen_text)
    check("generic_long[truncated]", len(gen_out.splitlines()) < 700, True)
    check("generic_under[passthrough]",
          truncate_tool_result("WebFetch", "\n".join("y" for _ in range(400))),
          "\n".join("y" for _ in range(400)))

    # ---- Read tool is skipped (own truncation) ----
    read_text = "\n".join(f"r{i}" for i in range(2500))
    check("read_skipped[passthrough]", truncate_tool_result("Read", read_text), read_text)
    check("read_skipped[lower-case]", truncate_tool_result("read", read_text), read_text)

    # ---- Direct _truncate_lines: marker math ----
    text20 = "\n".join(f"L{i}" for i in range(20))
    out = _truncate_lines(text20, threshold=10, keep_head=3, keep_tail=2)
    out_lines = out.splitlines()
    # 3 head + 1 marker + 2 tail = 6 lines.
    check("trunc_lines[total]", len(out_lines), 6)
    check("trunc_lines[head]", out_lines[:3], ["L0", "L1", "L2"])
    check("trunc_lines[tail]", out_lines[-2:], ["L18", "L19"])
    check("trunc_lines[marker dropped=15]", "dropped=15" in out_lines[3], True)

    # Threshold == line count → unchanged (line count must EXCEED threshold).
    text20_eq = "\n".join(f"L{i}" for i in range(20))
    check("trunc_lines[at threshold]", _truncate_lines(text20_eq, threshold=20), text20_eq)

    # Empty input
    check("trunc_lines[empty]", _truncate_lines("", threshold=10), "")

    # Negative keep_* raises
    try:
        _truncate_lines("abc\n" * 30, threshold=10, keep_head=-1)
        check("trunc_lines[neg keep raises]", True, False)
    except ValueError:
        check("trunc_lines[neg keep raises]", True, True)

    # Empty/missing tool_name → generic.
    out_empty = truncate_tool_result("", "\n".join(str(i) for i in range(600)))
    check("empty_name[uses generic]", "[CLAUDE_TRUNC:" in out_empty, True)
    check("empty_name_short[passthrough]", truncate_tool_result("", "abc"), "abc")

    return {"passed": passed, "failed": failed, "details": details}


def test_tool_truncation_passes() -> None:
    result = run_self_test()
    failures = [d for d in result["details"] if d.startswith("FAIL")]
    assert not failures, "\n".join(failures)


if __name__ == "__main__":
    r = run_self_test()
    for line in r["details"]:
        print(line)
    total = r["passed"] + r["failed"]
    print(f"[tool-truncation-test] {r['passed']}/{total} passed")
    sys.exit(0 if r["failed"] == 0 else 1)
