"""Tests for the Yolo auto-accept gate (`_eval_yolo_prompt`).

The gate decides whether Yolo Mode may auto-approve a CLI permission prompt.
Contract (see main._YOLO_DANGER_PATTERNS):
  - Routine work (cd, ls, powershell, git status, npm install, …) auto-accepts.
  - Only genuinely important / hard-to-reverse ops stop: destructive /
    deploy / publish / secrets / kill-by-name.

Run: pytest tests/test_yolo_gate.py   (or: python tests/test_yolo_gate.py)
"""
from __future__ import annotations

import sys
from pathlib import Path

_PARENT = Path(__file__).resolve().parent.parent
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))

# Typical Claude Code permission menu: first option approves.
_YES_MENU = [[1, "Yes"], [2, "Yes, and don't ask again"], [3, "No, tell Claude"]]

# Commands that MUST auto-accept under Yolo. The last two are explicit
# regression guards for the over-broad patterns removed 2026-06-07.
_ROUTINE = [
    "cd d:\\repos\\production-app",   # "production" in a path
    'powershell -command "get-childitem -recurse"',
    "set-location ..",
    "ls -la",
    "dir /b",
    "get-content config.json",
    "cat readme.md",
    "grep -rn todo .",
    "select-string -pattern todo *.py",
    "git status",
    "git log --oneline -10",
    "git diff",
    "git add -a",
    'git commit -m "wip"',
    "python main.py",
    "node server.js",
    "pytest tests/",
    "mkdir build",
    "remove-item temp.txt -force",                   # single-file delete (not -recurse)
    "npm run build",
    "npm install --production",                       # was a false-stop via "--prod"
    "npm cache clean --force",                        # was a false-stop via "--force"
]

# Commands that MUST stop (return danger_keyword).
_DANGEROUS = [
    "rm -rf /tmp/x",
    "remove-item -recurse build",
    "rmdir /s /q dist",
    "git reset --hard head~3",
    "git clean -fd",
    "git push --force origin main",
    "git push -f",
    "git filter-branch --tree-filter x",
    "drop table users",
    "truncate table sessions",
    "delete from accounts where 1=1",
    "vercel --prod",
    "vercel deploy",
    "npm publish",
    "twine upload dist/*",
    "gh release create v1.0",
    "pip uninstall requests",
    "gh auth logout",
    "gh secret delete api_key",
    "stop-process -name node",
    "taskkill /im chrome.exe /f",
]


def _gate(cmd: str):
    from main import _eval_yolo_prompt
    prompt = f"do you want to run this command?\n{cmd}\n1. yes 2. no".lower()
    return _eval_yolo_prompt(prompt, _YES_MENU)


def test_routine_commands_auto_accept():
    for cmd in _ROUTINE:
        ok, reason, _ = _gate(cmd)
        assert ok, f"routine command should auto-accept but stopped ({reason}): {cmd}"


def test_dangerous_commands_stop():
    for cmd in _DANGEROUS:
        ok, reason, payload = _gate(cmd)
        assert not ok and reason == "danger_keyword", (
            f"dangerous command should stop: {cmd} -> ok={ok} reason={reason} {payload}"
        )


def test_removed_overbroad_patterns():
    """The 2026-06-07 removals must not reappear as danger patterns."""
    from main import _YOLO_DANGER_PATTERNS
    for gone in ("production", "--prod", "--force"):
        assert gone not in _YOLO_DANGER_PATTERNS, f"over-broad pattern back: {gone!r}"


def test_menu_shape_gates():
    from main import _eval_yolo_prompt
    # Fewer than two choices: cannot safely auto-accept.
    ok, reason, _ = _eval_yolo_prompt("do you want to proceed? 1. yes", [[1, "Yes"]])
    assert not ok and reason == "no_choices"
    # First option is not an approval: do not accept.
    ok, reason, _ = _eval_yolo_prompt(
        "proceed?\n1. no 2. yes", [[1, "No"], [2, "Yes"]]
    )
    assert not ok and reason == "no_approve_marker"


if __name__ == "__main__":
    test_routine_commands_auto_accept()
    test_dangerous_commands_stop()
    test_removed_overbroad_patterns()
    test_menu_shape_gates()
    print("OK: all yolo-gate tests passed")
