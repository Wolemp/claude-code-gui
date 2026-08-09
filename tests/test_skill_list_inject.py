""" — skills list conditional injection self-test.

Verifies filter_skill_list() top-N default + /<command> full-list expansion +
footer string format.

Run via:
  - python tests/test_skill_list_inject.py
  - python main.py --md-watch-test    (aggregated)
  - pytest tests/test_skill_list_inject.py
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
        filter_skill_list,
        format_skill_list_footer,
        SKILLS_DEFAULT_TOP_N,
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

    # Build a 12-skill catalog (more than the default top-5 cap).
    skills = [{"name": f"skill_{i:02d}", "desc": f"d{i}"} for i in range(12)]

    # ---- test_skill_list_default_5: normal turn, no slash command ----
    out, suppressed = filter_skill_list(skills, user_message="please help me")
    check("default_5[count]", len(out), SKILLS_DEFAULT_TOP_N)
    check("default_5[suppressed]", suppressed, 12 - SKILLS_DEFAULT_TOP_N)
    # First-come order preserved (no recently_invoked / role_defaults).
    check("default_5[first name]", out[0]["name"], "skill_00")
    check("default_5[last name]", out[-1]["name"], f"skill_{SKILLS_DEFAULT_TOP_N - 1:02d}")

    # ---- test_skill_list_command_full: /<command> → expand prefix matches ----
    out, suppressed = filter_skill_list(skills, user_message="/skill_07 what is this")
    # All 12 names contain "skill_07"? No — only skill_07. Match exact name.
    check("command_full[exact one]", len(out), 1)
    check("command_full[matched name]", out[0]["name"], "skill_07")
    check("command_full[suppressed=0 in cmd-mode]", suppressed, 0)

    # /<command> with shared prefix expands to all matches (substring match).
    skills_named = [
        {"name": "audit"}, {"name": "audit-quick"}, {"name": "audit-deep"},
        {"name": "deploy"}, {"name": "ship"},
    ]
    out, _ = filter_skill_list(skills_named, user_message="/audit run now")
    matched = sorted(s["name"] for s in out)
    check("command_full[prefix all 3]", matched, ["audit", "audit-deep", "audit-quick"])

    # /<command> with no matching skill → fall through to top-N.
    out, suppressed = filter_skill_list(
        skills_named, user_message="/nonexistent action"
    )
    check("command_full[no-match → top-N]", len(out), len(skills_named))
    check("command_full[no-match suppressed=0]", suppressed, 0)

    # ---- recently_invoked + role_defaults ranking ----
    out, _ = filter_skill_list(
        skills,
        user_message="hi",
        recently_invoked=["skill_09", "skill_05"],
        role_defaults=["skill_10"],
        top_n=4,
    )
    check("ranked[count]", len(out), 4)
    # recently_invoked first, then role_defaults, then originals.
    check("ranked[order]", [s["name"] for s in out],
          ["skill_09", "skill_05", "skill_10", "skill_00"])

    # ---- top_n=0 edge case ----
    out, suppressed = filter_skill_list(skills, user_message="x", top_n=0)
    check("top_n=0[count]", len(out), 0)
    check("top_n=0[suppressed]", suppressed, 12)

    # ---- empty input ----
    out, suppressed = filter_skill_list([])
    check("empty[count]", len(out), 0)
    check("empty[suppressed]", suppressed, 0)

    # ---- footer format ----
    check("footer[N=7]", format_skill_list_footer(7),
          "(N more skills available — use /list to view) [N=7]")
    check("footer[N=0]", format_skill_list_footer(0), "")
    check("footer[N=-3]", format_skill_list_footer(-3), "")

    return {"passed": passed, "failed": failed, "details": details}


def test_skill_list_default_5() -> None:
    """Pytest entry: top-N default."""
    result = run_self_test()
    failures = [d for d in result["details"] if d.startswith("FAIL")]
    assert not failures, "\n".join(failures)


def test_skill_list_command_full() -> None:
    """Pytest entry: /<command> full-list expansion (covered by run_self_test)."""
    result = run_self_test()
    failures = [d for d in result["details"] if d.startswith("FAIL") and "command" in d]
    assert not failures, "\n".join(failures)


if __name__ == "__main__":
    r = run_self_test()
    for line in r["details"]:
        print(line)
    total = r["passed"] + r["failed"]
    print(f"[skill-list-test] {r['passed']}/{total} passed")
    sys.exit(0 if r["failed"] == 0 else 1)
