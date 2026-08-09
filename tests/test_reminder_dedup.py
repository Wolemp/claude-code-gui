""" — system reminder dedup self-test.

Verifies ReminderDedup: per-kind suppression within MIN_INTERVAL_TURNS,
critical-flag bypass, reset.

Run via:
  - python tests/test_reminder_dedup.py
  - python main.py --md-watch-test    (aggregated)
  - pytest tests/test_reminder_dedup.py
"""
from __future__ import annotations

import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_PARENT = _THIS_DIR.parent
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))


def run_self_test() -> dict:
    from main import ReminderDedup, REMINDER_MIN_INTERVAL_TURNS

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

    # ---- test_reminder_dedup_normal: same kind suppressed within interval ----
    d = ReminderDedup(min_interval_turns=5)
    # Turn 1: should inject (first time).
    check("dedup[turn 1]", d.should_inject("task-tool-hint", 1), True)
    # Turns 2-5: within interval, should suppress.
    check("dedup[turn 2 suppress]", d.should_inject("task-tool-hint", 2), False)
    check("dedup[turn 3 suppress]", d.should_inject("task-tool-hint", 3), False)
    check("dedup[turn 4 suppress]", d.should_inject("task-tool-hint", 4), False)
    check("dedup[turn 5 suppress]", d.should_inject("task-tool-hint", 5), False)
    # Turn 6: 5 turns since turn 1, should re-inject.
    check("dedup[turn 6 re-inject]", d.should_inject("task-tool-hint", 6), True)
    # Turn 7: should suppress again.
    check("dedup[turn 7 suppress]", d.should_inject("task-tool-hint", 7), False)

    # ---- Different kinds tracked independently ----
    d2 = ReminderDedup(min_interval_turns=5)
    check("dedup[kindA turn 1]", d2.should_inject("kindA", 1), True)
    check("dedup[kindB turn 1]", d2.should_inject("kindB", 1), True)
    check("dedup[kindA turn 2 suppress]", d2.should_inject("kindA", 2), False)
    check("dedup[kindB turn 2 suppress]", d2.should_inject("kindB", 2), False)

    # ---- test_reminder_critical_always: critical bypasses dedup ----
    d3 = ReminderDedup(min_interval_turns=5)
    check("crit[turn 1]", d3.should_inject("security-alert", 1, critical=True), True)
    check("crit[turn 2]", d3.should_inject("security-alert", 2, critical=True), True)
    check("crit[turn 3]", d3.should_inject("security-alert", 3, critical=True), True)
    # Non-critical follow-up immediately after — should suppress (timestamp
    # was bumped by the critical injection).
    check("crit[non-crit follows suppressed]",
          d3.should_inject("security-alert", 4, critical=False), False)

    # ---- reset() ----
    d4 = ReminderDedup(min_interval_turns=5)
    d4.should_inject("a", 1)
    d4.should_inject("b", 1)
    d4.reset("a")
    check("reset[a re-fires turn 2]", d4.should_inject("a", 2), True)
    check("reset[b still suppressed]", d4.should_inject("b", 2), False)
    d4.reset()
    check("reset[all clears, b re-fires]", d4.should_inject("b", 3), True)

    # ---- Empty kind always emits (defensive) ----
    d5 = ReminderDedup(min_interval_turns=5)
    check("empty_kind[1]", d5.should_inject("", 1), True)
    check("empty_kind[2]", d5.should_inject("", 2), True)

    # ---- min_interval_turns=1 means every turn re-injects ----
    d6 = ReminderDedup(min_interval_turns=1)
    check("interval=1[turn 1]", d6.should_inject("k", 1), True)
    check("interval=1[turn 2]", d6.should_inject("k", 2), True)
    check("interval=1[turn 3]", d6.should_inject("k", 3), True)

    # ---- min_interval_turns=0 clamps to 1 (defensive) ----
    d7 = ReminderDedup(min_interval_turns=0)
    check("interval=0[turn 1]", d7.should_inject("k", 1), True)
    check("interval=0[turn 2]", d7.should_inject("k", 2), True)

    # ---- Default constant integrity ----
    check("default[constant]", REMINDER_MIN_INTERVAL_TURNS, 5)

    return {"passed": passed, "failed": failed, "details": details}


def test_reminder_dedup_passes() -> None:
    result = run_self_test()
    failures = [d for d in result["details"] if d.startswith("FAIL")]
    assert not failures, "\n".join(failures)


if __name__ == "__main__":
    r = run_self_test()
    for line in r["details"]:
        print(line)
    total = r["passed"] + r["failed"]
    print(f"[reminder-dedup-test] {r['passed']}/{total} passed")
    sys.exit(0 if r["failed"] == 0 else 1)
