"""
Claude Code GUI — Multi-tab desktop AI development assistant.
Full Claude Code CLI feature parity. No browser. No terminal. No API key needed.

Features:
  - Multi-tab parallel conversations (independent project/session per tab)
  - Full model catalog per tab: pinned versions (Opus/Sonnet/Haiku/Fable) plus
    the CLI's own aliases (default / best / opusplan / opus[1m] / sonnet[1m])
  - Effort control per tab (low → max, auto) + ultracode, fast mode, output style
  - All six permission modes (manual / acceptEdits / plan / auto / dontAsk /
    bypassPermissions) plus a per-tool allowlist
  - Launch Options panel: every meaningful `claude` launch flag as a GUI control,
    with a live preview of the exact argv (see LAUNCH_OPTIONS)
  - Ctrl+K command palette covering the whole Claude Code command set
  - Auto-updates the Claude Code CLI at startup (toggleable)
  - i18n: English, Japanese, Chinese, Korean
  - Session continuity across restarts
  - Ctrl+L: display clear (keep session) / Ctrl+Shift+L: new session
  - Custom CLI flags passthrough
  - Background streaming with tab notifications

Keeping this in sync with the CLI: MODEL_PINNED / MODEL_ALIASES, EFFORT_LEVELS,
PERMISSION_MODES, TOOL_GROUPS and LAUNCH_OPTIONS are the only places that encode
Claude Code's surface.

Run:   python main.py
Build: pyinstaller --onefile --windowed --name ClaudeCodeGUI main.py
"""
from __future__ import annotations

import base64
import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import webview

# Optional: PTY support for interactive permission dialogs
try:
    from winpty import PtyProcess
    import pyte
    HAS_PTY = True
except ImportError:
    HAS_PTY = False

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CONFIG_DIR = Path.home() / ".claude-code-gui"
CONFIG_FILE = CONFIG_DIR / "config.json"
TABS_FILE = CONFIG_DIR / "tabs.json"

# --- Model catalog -----------------------------------------------------------
# Two tiers, both valid values for the CLI's `--model` flag (verified against
# claude 2.1.310 — every id below is in the binary's own model table):
#
#   MODEL_PINNED  — exact versions. Reproducible: a tab pinned here keeps the
#                   same model no matter what the plan default becomes.
#   MODEL_ALIASES — resolved by the CLI at launch. These cannot be written as a
#                   pinned id: `default` clears the override and follows the
#                   plan, `best`/`opusplan` are routing policies, and the
#                   `[1m]` forms select the 1M-context variant of a family.
#
# Retired ids (Sonnet 3.5/3.7, Haiku 3.5, Opus 3) are deliberately absent —
# they 404 at the API and would hand the user a tab that cannot start.
MODEL_PINNED = [
    ("claude-opus-5-5", "Opus 5.5"),
    ("claude-sonnet-5-5", "Sonnet 5.5"),
    ("claude-haiku-5-5", "Haiku 5.5"),
    ("claude-fable-5-1", "Fable 5.1"),
    ("claude-fable-5", "Fable 5"),
    ("claude-opus-5", "Opus 5"),
    ("claude-sonnet-5", "Sonnet 5"),
    ("claude-opus-4-8", "Opus 4.8"),
    ("claude-opus-4-7", "Opus 4.7"),
    ("claude-opus-4-6", "Opus 4.6"),
    ("claude-opus-4-5", "Opus 4.5"),
    ("claude-opus-4-1", "Opus 4.1"),
    ("claude-opus-4-0", "Opus 4"),
    ("claude-sonnet-4-6", "Sonnet 4.6"),
    ("claude-sonnet-4-5", "Sonnet 4.5"),
    ("claude-sonnet-4-0", "Sonnet 4"),
    ("claude-haiku-4-5", "Haiku 4.5"),
]
MODEL_ALIASES = [
    ("default", "Default (follows plan)"),
    ("best", "Best available"),
    ("opus", "Opus (latest)"),
    ("sonnet", "Sonnet (latest)"),
    ("haiku", "Haiku (latest)"),
    ("fable", "Fable (latest)"),
    ("opusplan", "Opus plan → Sonnet exec"),
    ("opus[1m]", "Opus (1M context)"),
    ("sonnet[1m]", "Sonnet (1M context)"),
]
# Flat catalog: every accepted `--model` value. Used for validation in
# set_tab_model, for the `/model` lookup table, and for label resolution.
MODELS = MODEL_PINNED + MODEL_ALIASES
# Grouped view for the GUI dropdown (<optgroup> per tier).
MODEL_GROUPS = [
    ("pinned", MODEL_PINNED),
    ("aliases", MODEL_ALIASES),
]
# New-tab default = newest Opus, pinned. Deliberately a pinned id rather than
# the `default` alias so a tab's model never silently changes under the user
# mid-project. Referenced by ChatTab and the JS fallback, so one edit here
# propagates everywhere.
DEFAULT_MODEL = "claude-opus-5-5"
# API mode (Anthropic SDK) only accepts concrete model ids, so each CLI alias
# is mapped onto the newest pinned member of its family.
_API_ALIAS_TARGET = {
    "default": DEFAULT_MODEL, "best": DEFAULT_MODEL,
    "opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5",
    "haiku": "claude-haiku-5-5", "fable": "claude-fable-5-1",
    "opusplan": "claude-opus-5-5",
    "opus[1m]": "claude-opus-5-5", "sonnet[1m]": "claude-sonnet-5-5",
}


def _api_model_id(model: str) -> str:
    """Concrete model id for the API backend (aliases resolved)."""
    return _API_ALIAS_TARGET.get(model, model)


# `auto` is handled by omitting --effort entirely (the model picks its own
# default). The other five are the exact values the CLI's --effort accepts.
EFFORT_LEVELS = [
    ("max", "Max"), ("xhigh", "XHigh"), ("high", "High"),
    ("medium", "Medium"), ("low", "Low"), ("auto", "Auto"),
]
_VALID_EFFORTS = {e[0] for e in EFFORT_LEVELS}

# --- Permission modes --------------------------------------------------------
# Exact `--permission-mode` values (claude 2.1.310). "custom" is GUI-only: it
# means "mode default + an explicit --allowedTools allowlist".
PERMISSION_MODES = [
    ("default", "Manual"),
    ("acceptEdits", "Accept Edits"),
    ("plan", "Plan"),
    ("auto", "Auto"),
    ("dontAsk", "Don't Ask"),
    ("bypassPermissions", "Bypass Permissions"),
    ("custom", "Selected tools only"),
]
# Values that may be passed straight through to --permission-mode.
_CLI_PERMISSION_MODES = {m[0] for m in PERMISSION_MODES} - {"custom"}

# --- Built-in tools ----------------------------------------------------------
# Exact tool-name strings accepted by --tools / --allowedTools /
# --disallowedTools, grouped for the GUI. Sourced from the Claude Code tools
# reference; `TaskOutput` is omitted (removed in v2.1.277).
TOOL_GROUPS: list[tuple[str, list[str]]] = [
    ("file", ["Read", "Write", "Edit", "NotebookEdit", "Glob", "Grep", "LSP"]),
    ("exec", ["Bash", "PowerShell", "Monitor"]),
    ("web", ["WebFetch", "WebSearch"]),
    ("agents", ["Agent", "SendMessage", "ListAgents", "SubagentHandback",
                 "Workflow", "Skill", "ToolSearch"]),
    ("tasks", ["TodoWrite", "TaskCreate", "TaskGet", "TaskList", "TaskUpdate",
                "TaskStop"]),
    ("plan", ["EnterPlanMode", "ExitPlanMode", "EnterWorktree", "ExitWorktree"]),
    ("sched", ["CronCreate", "CronList", "CronDelete", "ScheduleWakeup",
                "RemoteTrigger"]),
    ("mcp", ["ListMcpResourcesTool", "ReadMcpResourceTool", "WaitForMcpServers"]),
    ("io", ["AskUserQuestion", "SendUserFile", "PushNotification", "Artifact",
             "ShareOnboardingGuide", "SendFeedback", "ReportFindings",
             "EndConversation"]),
]
CLI_TOOLS = [t for _grp, tools in TOOL_GROUPS for t in tools]
_VALID_TOOLS = set(CLI_TOOLS)

# --- Launch options ----------------------------------------------------------
# Every remaining `claude` launch flag worth a GUI control, declared once as
# data. The same table drives three things, so they cannot drift apart:
#   1. ChatTab's extra fields  (_LAUNCH_DEFAULTS, below)
#   2. the argv builder        (_launch_flags)
#   3. the Settings panel UI   (shipped to JS via get_initial_state)
#
# Columns: key, group, flag, kind, print_only
#   kind "bool"     -> emit the flag alone when the value is truthy
#        "text"     -> emit `flag value` when non-empty (one argv item)
#        "select"   -> same as text, rendered as a dropdown (choices)
#        "list"     -> emit `flag v1 v2 …` (the flag's own variadic form)
#        "optional" -> value "1" emits the bare flag; any other non-empty
#                      value emits `flag value` (flags whose arg is optional)
#   print_only: flag only valid with -p/--print, so the interactive PTY path
#               must skip it (passing it there makes the CLI refuse to start).
#
# Every flag below was verified against `claude --help` on 2.1.310. Notably
# absent: --max-turns, which is no longer a documented flag - see
# ChatTab.max_turns.
LAUNCH_OPTIONS: list[dict] = [
    # Prompt / context
    {"key": "append_system_prompt", "group": "prompt",
     "flag": "--append-system-prompt", "kind": "text"},
    {"key": "system_prompt_snapshot", "group": "prompt",
     "flag": "--system-prompt-snapshot", "kind": "select",
     "choices": ["", "on", "off"]},
    {"key": "exclude_dynamic_sections", "group": "prompt",
     "flag": "--exclude-dynamic-system-prompt-sections", "kind": "bool"},
    {"key": "autocompact", "group": "prompt", "flag": "--autocompact",
     "kind": "text"},
    # Model
    {"key": "fallback_model", "group": "model", "flag": "--fallback-model",
     "kind": "text"},
    {"key": "betas", "group": "model", "flag": "--betas", "kind": "list"},
    # Tools
    {"key": "tools", "group": "tools", "flag": "--tools", "kind": "list"},
    {"key": "disallowed_tools", "group": "tools", "flag": "--disallowedTools",
     "kind": "list"},
    {"key": "allow_dangerous_opt_in", "group": "tools",
     "flag": "--allow-dangerously-skip-permissions", "kind": "bool"},
    # Context dirs / config.
    # `extra_dirs` is NOT emitted by _launch_flags: --add-dir is variadic, and
    # a second --add-dir replaces the first rather than appending, which would
    # silently drop the project directory. _add_dir_args merges both into one
    # invocation instead; "skip_flag" keeps it out of the generic builder while
    # still rendering a control in the panel.
    {"key": "extra_dirs", "group": "config", "flag": "--add-dir",
     "kind": "list", "skip_flag": True},
    {"key": "settings_path", "group": "config", "flag": "--settings",
     "kind": "text"},
    {"key": "setting_sources", "group": "config", "flag": "--setting-sources",
     "kind": "text"},
    {"key": "agent", "group": "config", "flag": "--agent", "kind": "text"},
    {"key": "agents_json", "group": "config", "flag": "--agents",
     "kind": "text"},
    # MCP
    {"key": "mcp_config", "group": "mcp", "flag": "--mcp-config",
     "kind": "list"},
    {"key": "strict_mcp_config", "group": "mcp", "flag": "--strict-mcp-config",
     "kind": "bool"},
    # Session
    {"key": "session_name", "group": "session", "flag": "--name",
     "kind": "text"},
    {"key": "fork_session", "group": "session", "flag": "--fork-session",
     "kind": "bool"},
    {"key": "worktree", "group": "session", "flag": "--worktree",
     "kind": "optional"},
    {"key": "no_session_persistence", "group": "session",
     "flag": "--no-session-persistence", "kind": "bool"},
    # Integrations
    {"key": "ide", "group": "integr", "flag": "--ide", "kind": "bool"},
    {"key": "chrome", "group": "integr", "flag": "--chrome", "kind": "bool"},
    {"key": "brief", "group": "integr", "flag": "--brief", "kind": "bool"},
    # Troubleshooting / hardening
    {"key": "restricted", "group": "trouble", "flag": "--restricted",
     "kind": "bool"},
    {"key": "safe_mode", "group": "trouble", "flag": "--safe-mode",
     "kind": "bool"},
    {"key": "bare", "group": "trouble", "flag": "--bare", "kind": "bool"},
    {"key": "disable_slash_commands", "group": "trouble",
     "flag": "--disable-slash-commands", "kind": "bool"},
    {"key": "screen_reader", "group": "trouble", "flag": "--ax-screen-reader",
     "kind": "bool"},
    {"key": "debug", "group": "trouble", "flag": "--debug", "kind": "optional"},
    # --print-only (API/pipe path); passing these to the interactive PTY
    # makes the CLI reject the launch, hence print_only.
    {"key": "max_budget_usd", "group": "print", "flag": "--max-budget-usd",
     "kind": "text", "print_only": True},
]
# key -> default value, by kind. Feeds ChatTab's dataclass defaults and the
# serialize/deserialize round-trip without listing every field twice.
_LAUNCH_DEFAULTS: dict[str, Any] = {
    o["key"]: ([] if o["kind"] == "list" else (False if o["kind"] == "bool" else ""))
    for o in LAUNCH_OPTIONS
}
_LAUNCH_BY_KEY: dict[str, dict] = {o["key"]: o for o in LAUNCH_OPTIONS}
# Hard caps so a pasted blob can't build a multi-megabyte argv.
_LAUNCH_MAX_TEXT = 100000
_LAUNCH_MAX_ITEMS = 64

# Session toggles that have no launch flag — they are session commands only, so
# the GUI fires them into the PTY right after the session comes up (same
# mechanism as /remote-control). key -> callable(value) -> command string|None.
SESSION_TOGGLES: list[dict] = [
    {"key": "ultracode", "kind": "bool",
     "cmd": lambda v: "/effort ultracode on" if v else None},
    {"key": "fast_mode", "kind": "bool",
     "cmd": lambda v: "/fast on" if v else None},
    {"key": "output_style", "kind": "select",
     "choices": ["", "default", "Explanatory", "Learning", "Concise"],
     "cmd": lambda v: f"/output-style {v}" if v else None},
    {"key": "sandbox", "kind": "bool",
     "cmd": lambda v: "/sandbox" if v else None},
]
_TOGGLE_DEFAULTS: dict[str, Any] = {
    t["key"]: (False if t["kind"] == "bool" else "") for t in SESSION_TOGGLES
}
_TOGGLE_BY_KEY: dict[str, dict] = {t["key"]: t for t in SESSION_TOGGLES}


def sanitize_launch(raw) -> dict:
    """Coerce a persisted/JS-supplied launch dict to the declared schema.

    Returns a fresh dict containing exactly the keys this build declares, each
    coerced to its kind's type. Keys the build no longer knows about are
    dropped so they can never reach the argv builder; missing keys fall back to
    their default.
    """
    out = dict(_LAUNCH_DEFAULTS)
    if not isinstance(raw, dict):
        return out
    for key, opt in _LAUNCH_BY_KEY.items():
        if key not in raw:
            continue
        val = raw[key]
        kind = opt["kind"]
        if kind == "bool":
            out[key] = bool(val)
        elif kind == "list":
            if isinstance(val, str):
                val = val.split()
            if isinstance(val, (list, tuple)):
                items = [str(v).strip() for v in val if str(v).strip()]
                if key in ("tools", "disallowed_tools"):
                    items = [t for t in items if t in _VALID_TOOLS]
                out[key] = items[:_LAUNCH_MAX_ITEMS]
        else:  # text / select / optional
            sval = "" if val is None else str(val).strip()
            choices = opt.get("choices")
            if choices is not None and sval not in choices:
                sval = ""
            out[key] = sval[:_LAUNCH_MAX_TEXT]
    return out


def sanitize_toggles(raw) -> dict:
    """Coerce a persisted/JS-supplied session-toggle dict to the schema."""
    out = dict(_TOGGLE_DEFAULTS)
    if not isinstance(raw, dict):
        return out
    for key, tg in _TOGGLE_BY_KEY.items():
        if key not in raw:
            continue
        if tg["kind"] == "bool":
            out[key] = bool(raw[key])
        else:
            sval = str(raw[key] or "").strip()
            out[key] = sval if sval in tg.get("choices", [""]) else ""
    return out


def _launch_flags(tab, *, interactive: bool) -> list[str]:
    """Build the argv tail for `tab`'s launch options.

    Shared by PtySession.start() (interactive=True) and Api._build_cmd()
    (interactive=False, the `-p/--print` path) so the two spawn paths can never
    disagree about which flags a tab asked for. Unknown/blank values are
    skipped; `print_only` flags are dropped on the interactive path because the
    CLI rejects them there.
    """
    store = getattr(tab, "launch", None) or {}
    argv: list[str] = []
    for opt in LAUNCH_OPTIONS:
        if opt.get("print_only") and interactive:
            continue
        if opt.get("skip_flag"):
            continue
        val = store.get(opt["key"], _LAUNCH_DEFAULTS[opt["key"]])
        kind = opt["kind"]
        flag = opt["flag"]
        if kind == "bool":
            if val:
                argv.append(flag)
        elif kind == "list":
            if not isinstance(val, (list, tuple)):
                continue
            items = [str(v).strip() for v in val if str(v).strip()]
            if items:
                argv.append(flag)
                argv.extend(items[:_LAUNCH_MAX_ITEMS])
        elif kind == "optional":
            sval = str(val or "").strip()
            if sval == "1":
                argv.append(flag)
            elif sval:
                argv += [flag, sval[:_LAUNCH_MAX_TEXT]]
        else:  # text / select
            sval = str(val or "").strip()
            if sval:
                argv += [flag, sval[:_LAUNCH_MAX_TEXT]]
    return argv


def _resolve_model(text: str) -> str:
    """Resolve user-typed text to a catalog model value, or "" if unknown.

    Accepts the exact value (`claude-opus-5-5`, `opusplan`, `opus[1m]`), the
    display label (`Opus 5.5`), and loose spellings of either (`opus5.5`,
    `OPUS 5-5`). Punctuation and case are normalised away, so a user doesn't
    have to remember whether the catalog spells a version with a dot or a dash.
    """
    def norm(s: str) -> str:
        return re.sub(r"[\s._-]+", "", (s or "").strip().lower())

    key = norm(text)
    if not key:
        return ""
    lookup: dict[str, str] = {}
    # Later writes lose to earlier ones so a pinned id always wins over a
    # label collision; aliases are registered last for the same reason.
    for mid, label in MODEL_PINNED:
        lookup.setdefault(norm(mid), mid)
        lookup.setdefault(norm(label), mid)
    for mid, label in MODEL_ALIASES:
        lookup.setdefault(norm(mid), mid)
        lookup.setdefault(norm(label), mid)
    # Bare family names resolve to the CLI alias, which always tracks the
    # newest member — better than pinning whatever is newest at build time.
    for fam in ("opus", "sonnet", "haiku", "fable", "best", "default",
                 "opusplan"):
        lookup.setdefault(fam, fam)
    return lookup.get(key, "")


def _permission_mode_args(tab) -> list[str]:
    """`--permission-mode` args for `tab`, or [] when the flag must be omitted.

    Two values never reach the flag: "custom" is GUI-only (it means "mode
    default plus an explicit --allowedTools allowlist"), and "default" is not
    in the CLI's choice list at all — its spelling there is "manual", and
    omitting the flag already selects it. Some CLI versions reject "default"
    with an invalid-choice error, so it is never passed through.
    """
    pm = getattr(tab, "permission_mode", "") or "default"
    if pm in ("default", "custom"):
        return []
    if pm not in _CLI_PERMISSION_MODES:
        return []
    return ["--permission-mode", pm]


def _add_dir_args(tab) -> list[str]:
    """One `--add-dir` carrying the project dir plus any extra directories.

    `--add-dir` is variadic, so repeating the flag replaces the earlier value
    instead of adding to it. Everything has to go through a single invocation.
    """
    dirs: list[str] = []
    if getattr(tab, "project_path", ""):
        dirs.append(tab.project_path)
    store = getattr(tab, "launch", None) or {}
    for d in store.get("extra_dirs", []) or []:
        d = str(d).strip()
        if d and d not in dirs:
            dirs.append(d)
    return (["--add-dir", *dirs[:_LAUNCH_MAX_ITEMS]] if dirs else [])


def _session_toggle_cmds(tab) -> list[str]:
    """Slash commands to fire into a freshly-started PTY for `tab`'s toggles."""
    store = getattr(tab, "toggles", None) or {}
    out: list[str] = []
    for tg in SESSION_TOGGLES:
        val = store.get(tg["key"], _TOGGLE_DEFAULTS[tg["key"]])
        if tg["kind"] == "select":
            val = str(val or "").strip()
            if val and val not in tg.get("choices", []):
                continue
        try:
            cmd = tg["cmd"](val)
        except Exception:
            cmd = None
        if cmd:
            out.append(cmd)
    return out


def _validate_cmd_fields(tab) -> bool:
    """Validate the tab fields that end up in a spawned command line.

    Single gate for PtySession.start, Api._send_cli, Api.open_terminal and
    Api.preview_tab_cmd. Returns False when any field is malformed or unsafe.
    """
    # Square brackets are allowed: the `opus[1m]` / `sonnet[1m]` aliases select
    # the 1M-context variant of a family and are literal --model values. Args
    # are always passed as an argv list (never a shell string), so brackets
    # carry no injection risk here.
    if not re.fullmatch(r'[a-zA-Z0-9._\[\]-]+', tab.model or ''):
        return False
    if tab.session_id and not re.fullmatch(r'[a-zA-Z0-9_-]+', tab.session_id):
        return False
    if tab.project_path and not os.path.isdir(tab.project_path):
        return False
    if tab.system_prompt and len(tab.system_prompt) > 100000:
        return False
    if tab.custom_flags and len(tab.custom_flags) > 4096:
        return False
    return True


def _load_json(path: Path, default=None):
    if path.exists():
        try:
            return json.loads(path.read_text("utf-8"))
        except Exception:
            pass
    return default if default is not None else {}


def _save_json(path: Path, data):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


# ---------------------------------------------------------------------------
# i18n — Python-side error messages
# ---------------------------------------------------------------------------
_ERRORS = {
    "cli_not_found": {
        "en": "Claude CLI not found. Install: npm i -g @anthropic-ai/claude-code",
        "ja": "Claude CLI\u304c\u898b\u3064\u304b\u308a\u307e\u305b\u3093\u3002npm i -g @anthropic-ai/claude-code \u3067\u30a4\u30f3\u30b9\u30c8\u30fc\u30eb",
        "zh": "\u672a\u627e\u5230 Claude CLI\u3002\u8bf7\u5b89\u88c5: npm i -g @anthropic-ai/claude-code",
        "ko": "Claude CLI\ub97c \ucc3e\uc744 \uc218 \uc5c6\uc2b5\ub2c8\ub2e4. \uc124\uce58: npm i -g @anthropic-ai/claude-code",
    },
    "api_key_missing": {
        "en": "API key not set", "ja": "API\u30ad\u30fc\u304c\u672a\u8a2d\u5b9a\u3067\u3059",
        "zh": "\u672a\u8bbe\u7f6e API \u5bc6\u94a5", "ko": "API \ud0a4\uac00 \uc124\uc815\ub418\uc9c0 \uc54a\uc558\uc2b5\ub2c8\ub2e4",
    },
    "api_key_format": {
        "en": "API key must start with sk-ant-",
        "ja": "API\u30ad\u30fc\u306f sk-ant- \u3067\u59cb\u307e\u308b\u5fc5\u8981\u304c\u3042\u308a\u307e\u3059",
        "zh": "API \u5bc6\u94a5\u5fc5\u987b\u4ee5 sk-ant- \u5f00\u5934",
        "ko": "API \ud0a4\ub294 sk-ant-\ub85c \uc2dc\uc791\ud574\uc57c \ud569\ub2c8\ub2e4",
    },
    "file_too_large": {
        "en": "File too large (>500KB)", "ja": "\u30d5\u30a1\u30a4\u30eb\u304c\u5927\u304d\u3059\u304e\u307e\u3059 (>500KB)",
        "zh": "\u6587\u4ef6\u592a\u5927 (>500KB)", "ko": "\ud30c\uc77c\uc774 \ub108\ubb34 \ud07d\ub2c8\ub2e4 (>500KB)",
    },
    "invalid_launch_fields": {
        "en": "Launch settings are invalid (check model / session id / project path)",
        "ja": "\u8d77\u52d5\u8a2d\u5b9a\u304c\u4e0d\u6b63\u3067\u3059 (model / session id / project path \u3092\u78ba\u8a8d)",
        "zh": "\u542f\u52a8\u8bbe\u7f6e\u65e0\u6548 (\u8bf7\u68c0\u67e5 model / session id / project path)",
        "ko": "\uc2dc\uc791 \uc124\uc815\uc774 \uc720\ud6a8\ud558\uc9c0 \uc54a\uc2b5\ub2c8\ub2e4 (model / session id / project path \ud655\uc778)",
    },
}


# UI strings for the newer features. Merged into _ERRORS so `_t(key)` resolves
# them, and injected into the page as window.I18N (see main()).
_STRINGS_EXTRA = {
    # -- CLI auto-update ---------------------------------------------------
    "autoUpdate": {"en": "Auto-update Claude Code CLI on launch", "ja": "起動時に Claude Code CLI を自動アップデート", "zh": "启动时自动更新 Claude Code CLI", "ko": "시작 시 Claude Code CLI 자동 업데이트"},
    "autoUpdateNote": {"en": "Runs `claude update` in the background at startup. Running sessions keep their current version.", "ja": "起動時に `claude update` をバックグラウンド実行します。実行中セッションは現行バージョンを維持。", "zh": "启动时在后台运行 `claude update`。运行中的会话保持当前版本。", "ko": "시작 시 백그라운드에서 `claude update`를 실행합니다. 실행 중인 세션은 현재 버전을 유지합니다."},
    "updChecking": {"en": "Checking for Claude Code updates…", "ja": "Claude Code の更新を確認中…", "zh": "正在检查 Claude Code 更新…", "ko": "Claude Code 업데이트 확인 중…"},
    "updUpdating": {"en": "Updating Claude Code CLI…", "ja": "Claude Code CLI を更新中…", "zh": "正在更新 Claude Code CLI…", "ko": "Claude Code CLI 업데이트 중…"},
    "updLatest": {"en": "Claude Code is up to date", "ja": "Claude Code は最新です", "zh": "Claude Code 已是最新", "ko": "Claude Code가 최신입니다"},
    "updUpdated": {"en": "Claude Code updated — restart a tab's session to use it", "ja": "Claude Code を更新しました — タブのセッションを再起動すると反映されます", "zh": "Claude Code 已更新 — 重启标签会话后生效", "ko": "Claude Code를 업데이트했습니다 — 탭 세션을 재시작하면 적용됩니다"},
    "updFailed": {"en": "Claude Code update failed", "ja": "Claude Code の更新に失敗しました", "zh": "Claude Code 更新失败", "ko": "Claude Code 업데이트 실패"},
    "updNow": {"en": "Update now", "ja": "今すぐ更新", "zh": "立即更新", "ko": "지금 업데이트"},
    "cliVersion": {"en": "CLI version", "ja": "CLI バージョン", "zh": "CLI 版本", "ko": "CLI 버전"},

    # -- command palette ---------------------------------------------------
    "cmdPalette": {"en": "Command Palette", "ja": "コマンドパレット", "zh": "命令面板", "ko": "명령 팔레트"},
    "cmdPaletteHint": {"en": "Search every Claude Code command. Enter inserts it, Shift+Enter sends it.", "ja": "Claude Code の全コマンドを検索。Enter で挿入、Shift+Enter で送信。", "zh": "搜索所有 Claude Code 命令。Enter 插入，Shift+Enter 发送。", "ko": "모든 Claude Code 명령을 검색합니다. Enter는 삽입, Shift+Enter는 전송."},
    "cmdSearchPH": {"en": "Type to filter commands…", "ja": "コマンドを絞り込む…", "zh": "输入以筛选命令…", "ko": "명령 필터링…"},
    "cmdNoMatch": {"en": "No matching command", "ja": "一致するコマンドなし", "zh": "没有匹配的命令", "ko": "일치하는 명령 없음"},
    "cmdAll": {"en": "All", "ja": "すべて", "zh": "全部", "ko": "전체"},

    # command categories
    "ccSession": {"en": "Session", "ja": "セッション", "zh": "会话", "ko": "세션"},
    "ccContext": {"en": "Context & memory", "ja": "コンテキスト・メモリ", "zh": "上下文与记忆", "ko": "컨텍스트·메모리"},
    "ccModel": {"en": "Model & effort", "ja": "モデル・Effort", "zh": "模型与努力度", "ko": "모델·Effort"},
    "ccReview": {"en": "Review & quality", "ja": "レビュー・品質", "zh": "审查与质量", "ko": "리뷰·품질"},
    "ccAgents": {"en": "Agents & workflows", "ja": "エージェント・ワークフロー", "zh": "代理与工作流", "ko": "에이전트·워크플로"},
    "ccExtend": {"en": "Extend (MCP, skills, plugins)", "ja": "拡張 (MCP・スキル・プラグイン)", "zh": "扩展 (MCP·技能·插件)", "ko": "확장 (MCP·스킬·플러그인)"},
    "ccPerms": {"en": "Permissions & security", "ja": "権限・セキュリティ", "zh": "权限与安全", "ko": "권한·보안"},
    "ccPlatform": {"en": "Platforms & remote", "ja": "プラットフォーム・リモート", "zh": "平台与远程", "ko": "플랫폼·원격"},
    "ccConfig": {"en": "Configuration", "ja": "設定", "zh": "配置", "ko": "구성"},
    "ccAccount": {"en": "Account & usage", "ja": "アカウント・使用量", "zh": "账户与用量", "ko": "계정·사용량"},
    "ccInfo": {"en": "Info & help", "ja": "情報・ヘルプ", "zh": "信息与帮助", "ko": "정보·도움말"},

    # -- launch options ----------------------------------------------------
    "launchOptions": {"en": "Launch Options", "ja": "起動オプション", "zh": "启动选项", "ko": "시작 옵션"},
    "launchOptionsNote": {"en": "Applied as CLI flags when this tab's session starts. Changes need a session restart.", "ja": "このタブのセッション開始時に CLI フラグとして適用。変更はセッション再起動で反映。", "zh": "在本标签会话启动时作为 CLI 参数应用。更改需重启会话。", "ko": "이 탭의 세션 시작 시 CLI 플래그로 적용됩니다. 변경은 세션 재시작이 필요합니다."},
    "sessionToggles": {"en": "Session Toggles", "ja": "セッション切替", "zh": "会话开关", "ko": "세션 토글"},
    "sessionTogglesNote": {"en": "Applied immediately to a running session.", "ja": "実行中のセッションに即時適用されます。", "zh": "立即应用于运行中的会话。", "ko": "실행 중인 세션에 즉시 적용됩니다."},
    "previewCmd": {"en": "Preview launch command", "ja": "起動コマンドをプレビュー", "zh": "预览启动命令", "ko": "시작 명령 미리보기"},
    "restartNeeded": {"en": "Restart this tab's session to apply", "ja": "反映にはタブのセッション再起動が必要", "zh": "需重启本标签会话以生效", "ko": "적용하려면 탭 세션을 재시작하세요"},
    "remoteControl": {"en": "Remote Control (control from claude.ai)", "ja": "Remote Control (claude.ai から操作)", "zh": "远程控制 (从 claude.ai 操作)", "ko": "Remote Control (claude.ai에서 조작)"},

    # launch option groups
    "logPrompt": {"en": "Prompt & context", "ja": "プロンプト・コンテキスト", "zh": "提示与上下文", "ko": "프롬프트·컨텍스트"},
    "logModel": {"en": "Model", "ja": "モデル", "zh": "模型", "ko": "모델"},
    "logTools": {"en": "Tools", "ja": "ツール", "zh": "工具", "ko": "도구"},
    "logConfig": {"en": "Directories & config", "ja": "ディレクトリ・設定", "zh": "目录与配置", "ko": "디렉터리·설정"},
    "logMcp": {"en": "MCP", "ja": "MCP", "zh": "MCP", "ko": "MCP"},
    "logSession": {"en": "Session", "ja": "セッション", "zh": "会话", "ko": "세션"},
    "logIntegr": {"en": "Integrations", "ja": "連携", "zh": "集成", "ko": "연동"},
    "logTrouble": {"en": "Troubleshooting", "ja": "トラブルシューティング", "zh": "故障排查", "ko": "문제 해결"},
    "logPrint": {"en": "API / print mode only", "ja": "API / print モード専用", "zh": "仅 API / print 模式", "ko": "API / print 모드 전용"},

    # tool groups
    "tgFile": {"en": "Files & search", "ja": "ファイル・検索", "zh": "文件与搜索", "ko": "파일·검색"},
    "tgExec": {"en": "Shell & processes", "ja": "シェル・プロセス", "zh": "Shell 与进程", "ko": "셸·프로세스"},
    "tgWeb": {"en": "Web", "ja": "Web", "zh": "网络", "ko": "웹"},
    "tgAgents": {"en": "Agents & skills", "ja": "エージェント・スキル", "zh": "代理与技能", "ko": "에이전트·스킬"},
    "tgTasks": {"en": "Tasks & todos", "ja": "タスク・TODO", "zh": "任务与待办", "ko": "작업·할 일"},
    "tgPlan": {"en": "Plan & worktrees", "ja": "プラン・ワークツリー", "zh": "计划与工作树", "ko": "플랜·워크트리"},
    "tgSched": {"en": "Scheduling", "ja": "スケジュール", "zh": "调度", "ko": "스케줄"},
    "tgMcp": {"en": "MCP resources", "ja": "MCP リソース", "zh": "MCP 资源", "ko": "MCP 리소스"},
    "tgIo": {"en": "User interaction", "ja": "ユーザー対話", "zh": "用户交互", "ko": "사용자 상호작용"},

    # model groups
    "mgPinned": {"en": "Pinned versions", "ja": "バージョン固定", "zh": "固定版本", "ko": "버전 고정"},
    "mgAliases": {"en": "Aliases (CLI resolves)", "ja": "エイリアス (CLI が解決)", "zh": "别名 (CLI 解析)", "ko": "별칭 (CLI가 해석)"},

    # permission modes — labels match the CLI's own naming
    "pm_default": {"en": "Manual — reads only, ask for the rest", "ja": "Manual — 読取のみ自動、他は確認", "zh": "Manual — 仅读取自动，其余询问", "ko": "Manual — 읽기만 자동, 나머지는 확인"},
    "pm_acceptEdits": {"en": "Accept Edits — reads + file edits", "ja": "Accept Edits — 読取＋ファイル編集", "zh": "Accept Edits — 读取＋文件编辑", "ko": "Accept Edits — 읽기＋파일 편집"},
    "pm_plan": {"en": "Plan — read-only, plan before acting", "ja": "Plan — 読取専用、着手前に計画", "zh": "Plan — 只读，先规划", "ko": "Plan — 읽기 전용, 먼저 계획"},
    "pm_auto": {"en": "Auto — everything, with safety classifier", "ja": "Auto — 全自動 (安全分類器付き)", "zh": "Auto — 全部自动 (带安全分类器)", "ko": "Auto — 전체 자동 (안전 분류기 포함)"},
    "pm_dontAsk": {"en": "Don't Ask — never prompt, deny instead", "ja": "Don't Ask — 確認せず拒否", "zh": "Don't Ask — 不询问，直接拒绝", "ko": "Don't Ask — 묻지 않고 거부"},
    "pm_bypassPermissions": {"en": "Bypass Permissions — no checks (risky)", "ja": "Bypass Permissions — 無確認 (危険)", "zh": "Bypass Permissions — 无检查 (危险)", "ko": "Bypass Permissions — 검사 없음 (위험)"},
    "pm_custom": {"en": "Selected tools only", "ja": "選択ツールのみ", "zh": "仅选定工具", "ko": "선택한 도구만"},
    "permAuto": {"en": "Auto", "ja": "Auto", "zh": "Auto", "ko": "Auto"},
    "permNoteAuto": {"en": "Everything runs, with a background safety classifier", "ja": "全て自動実行 — 安全分類器がバックグラウンドで確認", "zh": "全部自动执行 — 后台安全分类器检查", "ko": "모두 자동 실행 — 백그라운드 안전 분류기 확인"},
    "permNoteDontAsk": {"en": "Never prompts — anything that would ask is denied", "ja": "確認せず — 確認が必要な操作は拒否されます", "zh": "不询问 — 需要询问的操作将被拒绝", "ko": "묻지 않음 — 확인이 필요한 작업은 거부됩니다"},
    "permNoteBypass": {"en": "No permission checks at all — use only in a sandbox", "ja": "権限チェックなし — サンドボックス限定で使用", "zh": "完全无权限检查 — 仅在沙箱中使用", "ko": "권한 검사 없음 — 샌드박스에서만 사용"},

    # per-option labels
    "optAppendSystemPrompt": {"en": "Append to system prompt", "ja": "システムプロンプトに追記", "zh": "追加到系统提示", "ko": "시스템 프롬프트에 추가"},
    "optSystemPromptSnapshot": {"en": "System prompt snapshot", "ja": "システムプロンプトのスナップショット", "zh": "系统提示快照", "ko": "시스템 프롬프트 스냅샷"},
    "optExcludeDynamicSections": {"en": "Move per-machine sections out of the system prompt", "ja": "マシン依存セクションをシステムプロンプト外へ", "zh": "将每机器相关段移出系统提示", "ko": "머신별 섹션을 시스템 프롬프트 밖으로"},
    "optAutocompact": {"en": "Auto-compact window (auto, 200k, 1M…)", "ja": "自動コンパクト窓 (auto, 200k, 1M…)", "zh": "自动压缩窗口 (auto, 200k, 1M…)", "ko": "자동 압축 창 (auto, 200k, 1M…)"},
    "optFallbackModel": {"en": "Fallback model(s), comma-separated", "ja": "フォールバックモデル (カンマ区切り)", "zh": "回退模型 (逗号分隔)", "ko": "폴백 모델 (쉼표 구분)"},
    "optBetas": {"en": "Beta headers (space-separated)", "ja": "Beta ヘッダー (スペース区切り)", "zh": "Beta 头 (空格分隔)", "ko": "Beta 헤더 (공백 구분)"},
    "optTools": {"en": "Enabled built-in tools (empty = default set)", "ja": "有効な組込みツール (空=既定セット)", "zh": "启用的内置工具 (空=默认集)", "ko": "활성 내장 도구 (비움=기본 세트)"},
    "optDisallowedTools": {"en": "Denied tools", "ja": "禁止ツール", "zh": "禁用工具", "ko": "차단 도구"},
    "optAllowDangerousOptIn": {"en": "Offer \"skip all permissions\" as an option", "ja": "「全権限スキップ」を選択肢として許可", "zh": "允许\"跳过全部权限\"作为选项", "ko": "\"모든 권한 건너뛰기\"를 선택지로 허용"},
    "optExtraDirs": {"en": "Extra working directories", "ja": "追加作業ディレクトリ", "zh": "额外工作目录", "ko": "추가 작업 디렉터리"},
    "optSettingsPath": {"en": "Extra settings file or JSON", "ja": "追加設定ファイル / JSON", "zh": "额外设置文件或 JSON", "ko": "추가 설정 파일 또는 JSON"},
    "optSettingSources": {"en": "Setting sources (user,project,local)", "ja": "設定ソース (user,project,local)", "zh": "设置来源 (user,project,local)", "ko": "설정 소스 (user,project,local)"},
    "optAgent": {"en": "Agent for this session", "ja": "このセッションのエージェント", "zh": "本会话的代理", "ko": "이 세션의 에이전트"},
    "optAgentsJson": {"en": "Custom agents (JSON or file path)", "ja": "カスタムエージェント (JSON / パス)", "zh": "自定义代理 (JSON 或路径)", "ko": "커스텀 에이전트 (JSON 또는 경로)"},
    "optMcpConfig": {"en": "MCP config files / JSON", "ja": "MCP 設定ファイル / JSON", "zh": "MCP 配置文件 / JSON", "ko": "MCP 설정 파일 / JSON"},
    "optStrictMcpConfig": {"en": "Use only these MCP servers", "ja": "これらの MCP サーバーのみ使用", "zh": "仅使用这些 MCP 服务器", "ko": "이 MCP 서버만 사용"},
    "optSessionName": {"en": "Session display name", "ja": "セッション表示名", "zh": "会话显示名", "ko": "세션 표시 이름"},
    "optForkSession": {"en": "Fork on resume (new session id)", "ja": "再開時にフォーク (新セッション ID)", "zh": "恢复时分叉 (新会话 ID)", "ko": "재개 시 포크 (새 세션 ID)"},
    "optWorktree": {"en": "Git worktree (1 = auto-name, or a name)", "ja": "Git worktree (1=自動命名、または名前)", "zh": "Git worktree (1=自动命名，或名称)", "ko": "Git worktree (1=자동 이름, 또는 이름)"},
    "optNoSessionPersistence": {"en": "Don't persist this session", "ja": "このセッションを保存しない", "zh": "不保存此会话", "ko": "이 세션을 저장하지 않음"},
    "optIde": {"en": "Auto-connect to IDE", "ja": "IDE に自動接続", "zh": "自动连接 IDE", "ko": "IDE 자동 연결"},
    "optChrome": {"en": "Enable Claude in Chrome", "ja": "Claude in Chrome を有効化", "zh": "启用 Claude in Chrome", "ko": "Claude in Chrome 활성화"},
    "optBrief": {"en": "Let Claude message you mid-task", "ja": "作業中のメッセージ送信を許可", "zh": "允许任务中向你发消息", "ko": "작업 중 메시지 전송 허용"},
    "optRestricted": {"en": "Restricted mode (no shell / WebFetch)", "ja": "制限モード (シェル / WebFetch なし)", "zh": "受限模式 (无 shell / WebFetch)", "ko": "제한 모드 (셸 / WebFetch 없음)"},
    "optSafeMode": {"en": "Safe mode (disable customizations)", "ja": "セーフモード (カスタマイズ無効)", "zh": "安全模式 (禁用自定义)", "ko": "세이프 모드 (커스터마이즈 비활성)"},
    "optBare": {"en": "Bare mode (needs ANTHROPIC_API_KEY)", "ja": "Bare モード (ANTHROPIC_API_KEY 必須)", "zh": "Bare 模式 (需要 ANTHROPIC_API_KEY)", "ko": "Bare 모드 (ANTHROPIC_API_KEY 필요)"},
    "optDisableSlashCommands": {"en": "Disable all skills and commands", "ja": "全スキル・コマンドを無効化", "zh": "禁用所有技能和命令", "ko": "모든 스킬·명령 비활성화"},
    "optScreenReader": {"en": "Screen-reader friendly output", "ja": "スクリーンリーダー対応出力", "zh": "适配屏幕阅读器的输出", "ko": "스크린 리더 친화 출력"},
    "optDebug": {"en": "Debug logging (1 = all, or a filter)", "ja": "デバッグログ (1=全体、またはフィルタ)", "zh": "调试日志 (1=全部，或过滤器)", "ko": "디버그 로그 (1=전체, 또는 필터)"},
    "optMaxBudgetUsd": {"en": "Max spend in USD", "ja": "最大支出 (USD)", "zh": "最大花费 (USD)", "ko": "최대 지출 (USD)"},
    "optUltracode": {"en": "Ultracode (orchestrate dynamic workflows)", "ja": "Ultracode (動的ワークフロー編成)", "zh": "Ultracode (编排动态工作流)", "ko": "Ultracode (동적 워크플로 편성)"},
    "optFastMode": {"en": "Fast mode (faster output, premium rate)", "ja": "Fast モード (高速出力・割増料金)", "zh": "快速模式 (更快输出，溢价计费)", "ko": "Fast 모드 (빠른 출력, 프리미엄 요금)"},
    "optOutputStyle": {"en": "Output style", "ja": "出力スタイル", "zh": "输出风格", "ko": "출력 스타일"},
    "optSandbox": {"en": "Sandbox mode", "ja": "サンドボックスモード", "zh": "沙箱模式", "ko": "샌드박스 모드"},
}

_ERRORS.update(_STRINGS_EXTRA)
_STRINGS = _ERRORS


# ---------------------------------------------------------------------------
# Chat Tab
# ---------------------------------------------------------------------------
@dataclass
class ChatTab:
    id: str = ""
    name: str = "New Chat"
    project_path: str = ""
    session_id: str = ""
    model: str = DEFAULT_MODEL
    effort: str = "max"
    # No longer a documented `claude` flag (gone from --help), so it is never
    # emitted. Kept only so pre-existing tabs.json entries still deserialize.
    max_turns: int = 0
    custom_flags: str = ""
    system_prompt: str = ""
    permission_mode: str = "default"
    allowed_tools: list = field(default_factory=list)
    # Launch flags (see LAUNCH_OPTIONS) and session-command toggles (see
    # SESSION_TOGGLES), held as dicts so adding an option to those tables
    # needs no change here.
    launch: dict = field(default_factory=lambda: dict(_LAUNCH_DEFAULTS))
    toggles: dict = field(default_factory=lambda: dict(_TOGGLE_DEFAULTS))
    # Expose this session to Remote Control on claude.ai. Previously typed into
    # the PTY as `/remote-control` after startup; now a real launch flag.
    remote_control: bool = True
    messages: list = field(default_factory=list)
    streaming: bool = field(default=False, repr=False)
    screen_content: str = field(default="", repr=False)
    process: Any = field(default=None, repr=False)
    pty_session: Any = field(default=None, repr=False)
    _perm_event: Any = field(default=None, repr=False)
    _perm_approved: bool = field(default=False, repr=False)

    def serialize(self) -> dict:
        return {
            "id": self.id, "name": self.name, "project_path": self.project_path,
            "session_id": self.session_id, "model": self.model, "effort": self.effort,
            "max_turns": self.max_turns, "custom_flags": self.custom_flags,
            "system_prompt": self.system_prompt,
            "permission_mode": self.permission_mode,
            "allowed_tools": self.allowed_tools,
            "launch": dict(self.launch or {}),
            "toggles": dict(self.toggles or {}),
            "remote_control": bool(self.remote_control),
            "messages": self.messages[-100:],
            "screen_content": self.screen_content[-60000:] if self.screen_content else "",
        }

    @classmethod
    def deserialize(cls, d: dict) -> "ChatTab":
        tab = cls()
        for k in ("id", "name", "project_path", "session_id", "model",
                   "effort", "max_turns", "custom_flags", "system_prompt",
                   "permission_mode", "allowed_tools", "messages",
                   "screen_content", "remote_control"):
            if k in d:
                setattr(tab, k, d[k])
        # launch/toggles: start from current defaults then overlay only the
        # keys this build still knows about. A tabs.json written by an older
        # build (missing keys) or a newer one (keys since removed) both load
        # cleanly, and a stale key can never reach the argv builder.
        tab.launch = sanitize_launch(d.get("launch"))
        tab.toggles = sanitize_toggles(d.get("toggles"))
        # A model that has since been retired from the catalog would hand the
        # user a tab that cannot start - fall back to the current default.
        if tab.model not in {m[0] for m in MODELS}:
            tab.model = DEFAULT_MODEL
        if tab.permission_mode not in {m[0] for m in PERMISSION_MODES}:
            tab.permission_mode = "default"
        if tab.effort not in _VALID_EFFORTS:
            tab.effort = "max"
        return tab


# ---------------------------------------------------------------------------
# PTY Session — terminal forwarding mode
# ---------------------------------------------------------------------------
class PtySession:
    """Persistent Claude Code interactive session via Windows ConPTY.
    Renders TUI via pyte and forwards the full screen to the GUI's terminal
    view.  No content parsing — the GUI shows the CLI exactly as-is."""

    def __init__(self, api_ref, tab):
        self.api = api_ref
        self.tab = tab
        self.pty = None
        self.screen = None
        self.stream = None
        self.running = False
        self._start_time = 0
        self._data_queue = queue.Queue()
        self._last_persist = 0

    def start(self):
        if not _validate_cmd_fields(self.tab):
            raise ValueError(self.api._t("invalid_launch_fields"))
        self.screen = pyte.HistoryScreen(120, 36, history=50000)
        self.screen.set_mode(pyte.modes.LNM)
        self.stream = pyte.Stream(self.screen)
        cmd = ["claude", "--model", self.tab.model, "--verbose"]
        # `auto` means "let the model pick" - omit the flag entirely.
        if self.tab.effort and self.tab.effort != "auto":
            cmd += ["--effort", self.tab.effort]
        if self.tab.session_id:
            cmd += ["--resume", self.tab.session_id]
        cmd += _add_dir_args(self.tab)
        # Remote Control as a launch flag. Typing `/remote-control` into the
        # PTY after startup (the old approach) raced the welcome screen and
        # silently no-op'd whenever the CLI wasn't at a prompt yet.
        if self.tab.remote_control:
            cmd += ["--remote-control"]
        pm = self.tab.permission_mode
        cmd += _permission_mode_args(self.tab)
        if pm == "custom" and self.tab.allowed_tools:
            for tool in self.tab.allowed_tools:
                if tool in _VALID_TOOLS:
                    cmd += ["--allowedTools", tool]
        cmd += _launch_flags(self.tab, interactive=True)
        if self.tab.custom_flags:
            try:
                cmd += shlex.split(self.tab.custom_flags)
            except Exception:
                pass
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        if self.tab.effort and self.tab.effort != "auto":
            # The CLI reads CLAUDE_CODE_EFFORT_LEVEL; CLAUDE_CODE_EFFORT is not
            # a name it recognises. Kept alongside the explicit --effort flag
            # so subprocesses the session spawns inherit the same level.
            env["CLAUDE_CODE_EFFORT_LEVEL"] = self.tab.effort
        self.pty = PtyProcess.spawn(cmd, dimensions=(36, 120), env=env)
        self.running = True
        self._start_time = time.time()
        threading.Thread(target=self._producer, daemon=True).start()
        threading.Thread(target=self._reader, daemon=True).start()

    def kill(self):
        self.running = False
        if self.pty:
            try:
                self.pty.terminate()
            except Exception:
                pass

    def write(self, data):
        """Send raw input to PTY (keystrokes, text, control chars)."""
        if self.pty and self.pty.isalive():
            self.pty.write(data)

    def resize(self, rows, cols):
        """Resize PTY and pyte screen to match GUI terminal size."""
        try:
            if self.pty and self.pty.isalive():
                self.pty.setwinsize(rows, cols)
            if self.screen:
                self.screen.resize(rows, cols)
        except Exception:
            pass

    def _producer(self):
        """Reads from PTY (may block) and feeds queue."""
        while self.running:
            try:
                if not self.pty or not self.pty.isalive():
                    break
                data = self.pty.read(4096)
                if data:
                    self._data_queue.put(data)
                else:
                    time.sleep(0.02)
            except (EOFError, OSError):
                break
            except Exception:
                time.sleep(0.05)
        self._data_queue.put(None)

    def _reader(self):
        """Consumes PTY data, feeds pyte, forwards rendered screen to GUI."""
        tid = self.tab.id
        prev_hash = None
        while self.running:
            try:
                data = self._data_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if data is None:
                break
            # Feed to pyte
            try:
                self.stream.feed(data)
            except Exception:
                try:
                    if isinstance(data, bytes):
                        self.stream.feed(data.decode("utf-8", errors="replace"))
                    else:
                        self.stream.feed(data.encode("utf-8", errors="replace").decode("utf-8"))
                except Exception:
                    pass
            # Fast change detection: hash screen + history length
            try:
                screen_lines = [ln.rstrip() for ln in self.screen.display]
            except (IndexError, AttributeError):
                # pyte bug: wcwidth on empty char — build display manually
                screen_lines = []
                for y in range(self.screen.lines):
                    row = ""
                    for x in range(self.screen.columns):
                        ch = self.screen.buffer[y][x]
                        row += ch.data if ch.data else ""
                    screen_lines.append(row.rstrip())
            hist_len = len(self.screen.history.top) if hasattr(self.screen, 'history') else 0
            cur_hash = hash((hist_len, tuple(screen_lines)))
            if cur_hash != prev_hash:
                prev_hash = cur_hash
                # Build full output: scrollback + current screen
                history_lines = []
                if hist_len > 0:
                    for hline in self.screen.history.top:
                        try:
                            row = "".join(hline[col].data if hline[col].data else "" for col in sorted(hline.keys())).rstrip()
                        except (IndexError, AttributeError):
                            row = ""
                        history_lines.append(row)
                while screen_lines and not screen_lines[-1]:
                    screen_lines.pop()
                all_lines = history_lines + screen_lines
                text = "\n".join(all_lines)
                self.api._js(f"onScreenUpdate('{tid}',{json.dumps(text)})")
                self.tab.screen_content = text
                if time.time() - self._last_persist > 30:
                    self._last_persist = time.time()
                    self.api._persist()
                # Extract session ID from screen (for --resume)
                if not self.tab.session_id:
                    for ln in screen_lines:
                        m = re.search(
                            r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}', ln)
                        if m:
                            self.tab.session_id = m.group(0)
                            self.api._persist()
                            break
                # Detect interactive menu → parse numbered choices from screen
                # Covers: permission prompts, feedback rating, any selection menu
                choices = []
                for ln in screen_lines:
                    m = re.match(r'^[^0-9]*(\d+)\.\s+(.+)$', ln.strip())
                    if m:
                        choices.append([int(m.group(1)), m.group(2).strip()])
                screen_text = "\n".join(screen_lines).lower()
                is_interactive = (
                    len(choices) >= 2
                    and ('?' in screen_text or 'esc' in screen_text
                         or 'allow' in screen_text or 'optional' in screen_text)
                )
                if is_interactive:
                    self.api._js(f"onPermState('{tid}',true,{json.dumps(choices)})")
                else:
                    self.api._js(f"onPermState('{tid}',false,[])")
        # PTY died — persist screen content
        self.api._persist()
        self.api._js(f"onPtyDied('{tid}')")


# ---------------------------------------------------------------------------
# File tree
# ---------------------------------------------------------------------------
_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".next",
    "dist", "build", ".cache", ".tox", ".mypy_cache", ".pytest_cache",
    "egg-info", ".eggs", ".idea", ".vscode",
}
_SKIP_FILES = {".DS_Store", "Thumbs.db", "desktop.ini"}


def _build_tree(root: str, max_depth: int = 4, _depth: int = 0) -> list:
    items = []
    try:
        entries = sorted(os.scandir(root), key=lambda e: (not e.is_dir(), e.name.lower()))
    except PermissionError:
        return items
    for entry in entries:
        if entry.is_dir():
            if entry.name in _SKIP_DIRS or entry.name.startswith("."):
                continue
            ch = _build_tree(entry.path, max_depth, _depth + 1) if _depth < max_depth else []
            items.append({"name": entry.name, "path": entry.path, "type": "dir", "children": ch})
        else:
            if entry.name in _SKIP_FILES:
                continue
            items.append({"name": entry.name, "path": entry.path, "type": "file"})
    return items


# ---------------------------------------------------------------------------
# Claude Code CLI auto-update
# ---------------------------------------------------------------------------
# Startup flow: the GUI runs `claude update` in a background thread so the
# window never waits on the network. `claude update` is already idempotent and
# non-interactive (exit 0 + "Claude Code is up to date (x.y.z)" when there is
# nothing to do), and it routes through the package manager for pkg installs,
# so there is nothing to reimplement here.
_UPDATE_TIMEOUT_S = 300.0


def _run_claude(args: list[str], timeout: float) -> tuple[int, str]:
    """Run `claude <args>` detached from any console, returning (rc, output)."""
    flags = 0
    if os.name == "nt":
        # Don't flash a console window on Windows — the GUI has no terminal.
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            ["claude", *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
            creationflags=flags,
        )
    except FileNotFoundError:
        return 127, "claude CLI not found on PATH"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {int(timeout)}s"
    except Exception as e:  # pragma: no cover - defensive
        return 1, str(e)
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode, out


def _cli_version() -> str:
    """Installed Claude Code version, or "" when the CLI isn't available."""
    if shutil.which("claude") is None:
        return ""
    rc, out = _run_claude(["--version"], timeout=15.0)
    if rc != 0:
        return ""
    # `claude --version` prints e.g. "2.1.310 (Claude Code)".
    m = re.search(r"\d+\.\d+\.\d+", out)
    return m.group(0) if m else out.splitlines()[0].strip()[:40]


def _update_cli() -> dict:
    """Run one update pass. Returns a state dict for the GUI."""
    before = _cli_version()
    if not before:
        return {"phase": "error", "from": "", "to": "", "changed": False,
                "message": "claude CLI not found on PATH"}
    rc, out = _run_claude(["update"], timeout=_UPDATE_TIMEOUT_S)
    after = _cli_version() or before
    if rc != 0:
        # A failed update is not fatal: the already-installed CLI still works,
        # so report it and let the session start on the current version.
        return {"phase": "error", "from": before, "to": after,
                "changed": after != before,
                "message": (out or f"claude update exited {rc}")[:400]}
    return {"phase": "done", "from": before, "to": after,
            "changed": after != before,
            "message": (out.splitlines()[-1].strip() if out else "")[:400]}


# ---------------------------------------------------------------------------
# Backend API
# ---------------------------------------------------------------------------
class Api:
    def __init__(self):
        self._window = None
        self._config = _load_json(CONFIG_FILE, {})
        self._tabs: dict[str, ChatTab] = {}
        self._active: str = ""
        self._counter = 0
        # CLI auto-update state (filled by the startup updater thread).
        self._cli_version = _cli_version()
        self._update_state: dict = {
            "phase": "idle",       # idle|checking|updating|done|error|disabled
            "from": self._cli_version,
            "to": "",
            "message": "",
            "changed": False,
        }
        self._restore_tabs()

    def _t(self, key: str) -> str:
        lang = self._config.get("language", "ja")
        if key in _ERRORS:
            return _ERRORS[key].get(lang, _ERRORS[key].get("en", key))
        return key

    # -- Persistence -----------------------------------------------------------

    def _restore_tabs(self):
        data = _load_json(TABS_FILE, {})
        for td in data.get("tabs", []):
            tab = ChatTab.deserialize(td)
            if tab.id:
                self._tabs[tab.id] = tab
        self._active = data.get("active", "")
        self._counter = data.get("counter", len(self._tabs))
        if not self._tabs:
            self._make_tab()
        elif self._active not in self._tabs:
            self._active = next(iter(self._tabs))

    def _persist(self):
        _save_json(TABS_FILE, {
            "tabs": [t.serialize() for t in self._tabs.values()],
            "active": self._active, "counter": self._counter,
        })

    def _make_tab(self, project: str = "") -> ChatTab:
        self._counter += 1
        tab = ChatTab(
            id=uuid.uuid4().hex[:8],
            name=Path(project).name if project else f"Chat {self._counter}",
            project_path=project,
        )
        self._tabs[tab.id] = tab
        self._active = tab.id
        return tab

    # -- Init ------------------------------------------------------------------

    def auto_start_session(self, tab_id: str):
        """Auto-start PTY session on app launch. Resume if session_id exists, else new."""
        tab = self._tabs.get(tab_id)
        if not tab:
            return
        mode = self._config.get("mode", "cli")
        if mode != "cli" or not HAS_PTY:
            return
        threading.Thread(target=self._auto_start_worker, args=(tab,), daemon=True).start()

    def _auto_start_worker(self, tab: ChatTab):
        """Worker thread: start PTY, detect dead sessions, retry as new."""
        tid = tab.id
        try:
            if tab.pty_session:
                tab.pty_session.kill()
            tab.pty_session = PtySession(self, tab)
            tab.pty_session.start()
            self._js(f"showTerminal('{tid}')")
            # Wait a moment to see if it stays alive
            time.sleep(3)
            if not tab.pty_session.pty or not tab.pty_session.pty.isalive():
                # Session died quickly — likely invalid session_id
                if tab.session_id:
                    tab.session_id = ""
                    self._persist()
                    # Retry as new session
                    tab.pty_session = PtySession(self, tab)
                    tab.pty_session.start()
                    self._js(f"showTerminal('{tid}')")
                    self._wait_and_apply_toggles(tab)
            else:
                self._wait_and_apply_toggles(tab)
        except FileNotFoundError:
            self._js(f"onStreamError('{tid}',{json.dumps(self._t('cli_not_found'))})")
        except Exception as e:
            self._js(f"onStreamError('{tid}',{json.dumps(str(e))})")

    def _wait_and_apply_toggles(self, tab: ChatTab):
        """Wait for the CLI prompt, then fire the session toggles that have no
        launch flag (ultracode, fast mode, output style, sandbox).

        Model, effort, permission mode and Remote Control all ride launch
        flags, so a tab with no toggles set returns immediately.
        """
        try:
            if not tab.pty_session or not tab.pty_session.running:
                return
            cmds = _session_toggle_cmds(tab)
            if not cmds:
                return
            # Wait (15s) for an actual prompt glyph at the end of a recent line.
            # If it never shows, settle 3s anyway so the commands don't race boot.
            ready = False
            for _ in range(150):
                try:
                    lines = tab.pty_session.screen.display
                    tail = [ln.rstrip() for ln in lines[-5:] if ln.strip()]
                    if any(ln.endswith("❯") or ">" in ln[-3:] for ln in tail):
                        time.sleep(1.0)
                        ready = True
                        break
                except Exception:
                    pass
                time.sleep(0.1)
            if not ready:
                time.sleep(3.0)
            for cmd in cmds:
                if not (tab.pty_session and tab.pty_session.running):
                    return
                tab.pty_session.write(cmd + "\r")
                time.sleep(0.6)
        except Exception:
            pass

    def get_initial_state(self) -> dict:
        cli_ok = shutil.which("claude") is not None
        return {
            "cli_available": cli_ok,
            "mode": self._config.get("mode", "cli"),
            "api_key_set": bool(self._config.get("api_key")),
            "language": self._config.get("language", ""),
            "tabs": [self._tab_info(t) for t in self._tabs.values()],
            "active_tab": self._active,
            "models": [{"id": m[0], "label": m[1]} for m in MODELS],
            # Grouped view so the dropdown can separate pinned versions from
            # CLI-resolved aliases (default / best / opusplan / ...).
            "model_groups": [
                {"id": gid, "models": [{"id": m[0], "label": m[1]} for m in grp]}
                for gid, grp in MODEL_GROUPS
            ],
            "efforts": [{"id": e[0], "label": e[1]} for e in EFFORT_LEVELS],
            "permission_modes": [
                {"id": m[0], "label": m[1]} for m in PERMISSION_MODES
            ],
            "cli_tools": CLI_TOOLS,
            "tool_groups": [
                {"id": gid, "tools": tools} for gid, tools in TOOL_GROUPS
            ],
            # Spec-driven Launch Options panel: the UI is rendered from this,
            # so adding a flag to LAUNCH_OPTIONS surfaces a control with no
            # separate HTML change.
            "launch_options": [
                {k: v for k, v in o.items() if k != "cmd"} for o in LAUNCH_OPTIONS
            ],
            "session_toggles": [
                {k: v for k, v in t.items() if k != "cmd"} for t in SESSION_TOGGLES
            ],
            "cli_version": self._cli_version,
            "auto_update": bool(self._config.get("auto_update", True)),
            "has_pty": HAS_PTY,
        }

    def _tab_info(self, t: ChatTab) -> dict:
        return {
            "id": t.id, "name": t.name, "project_path": t.project_path,
            "project_name": Path(t.project_path).name if t.project_path else "",
            "model": t.model, "effort": t.effort, "max_turns": t.max_turns,
            "custom_flags": t.custom_flags, "system_prompt": t.system_prompt,
            "permission_mode": t.permission_mode, "allowed_tools": t.allowed_tools,
            "launch": dict(t.launch or {}), "toggles": dict(t.toggles or {}),
            "remote_control": bool(t.remote_control),
            "messages": t.messages[-100:], "session_id": t.session_id,
            "screen_content": t.screen_content[-60000:] if t.screen_content else "",
        }

    # -- Language --------------------------------------------------------------

    def get_language(self) -> str:
        return self._config.get("language", "ja")

    def set_language(self, lang: str):
        self._config["language"] = lang
        _save_json(CONFIG_FILE, self._config)

    # -- Tab management --------------------------------------------------------

    def create_tab(self, project_path: str = "") -> dict:
        tab = self._make_tab(project_path)
        self._persist()
        return self._tab_info(tab)

    def close_tab(self, tab_id: str) -> dict:
        tab = self._tabs.get(tab_id)
        if tab:
            if tab.streaming and tab.process:
                try:
                    tab.process.terminate()
                except Exception:
                    pass
            if tab.pty_session:
                tab.pty_session.kill()
            del self._tabs[tab_id]
        if not self._tabs:
            self._make_tab()
        if self._active == tab_id:
            self._active = next(iter(self._tabs))
        self._persist()
        return {"active_tab": self._active}

    def rename_tab(self, tab_id: str, name: str):
        t = self._tabs.get(tab_id)
        if t and name.strip():
            t.name = name.strip()
            self._persist()

    # -- Per-tab project -------------------------------------------------------

    def select_project(self, tab_id: str) -> dict | None:
        tab = self._tabs.get(tab_id)
        if not tab:
            return None
        result = self._window.create_file_dialog(
            webview.FOLDER_DIALOG, directory=tab.project_path or ""
        )
        if result and len(result) > 0:
            path = result[0] if isinstance(result, (list, tuple)) else str(result)
            tab.project_path = path
            tab.session_id = ""
            tab.name = Path(path).name
            self._persist()
            return {"path": path, "name": tab.name}
        return None

    def get_file_tree(self, tab_id: str = "") -> list:
        tab = self._tabs.get(tab_id or self._active)
        if not tab or not tab.project_path:
            return []
        return _build_tree(tab.project_path)

    def read_file(self, path: str) -> dict:
        try:
            p = Path(path)
            if p.stat().st_size > 500_000:
                return {"ok": False, "error": self._t("file_too_large")}
            return {"ok": True, "content": p.read_text("utf-8", errors="replace"), "name": p.name}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # -- Per-tab settings ------------------------------------------------------

    def set_tab_model(self, tab_id: str, model: str):
        t = self._tabs.get(tab_id)
        # Validate against the catalog: this is directly JS-callable, and an
        # out-of-range value would be persisted and written as `/model <bad>`
        # to the PTY on this call and on every future restart.
        if t and model in {m[0] for m in MODELS}:
            t.model = model
            self._persist()
            # Auto-switch model in running PTY session
            if t.pty_session and t.pty_session.running:
                try:
                    t.pty_session.write(f"/model {model}\r")
                except Exception:
                    pass

    def set_tab_effort(self, tab_id: str, effort: str):
        t = self._tabs.get(tab_id)
        if t and effort in _VALID_EFFORTS:
            t.effort = effort
            self._persist()
            # Auto-send effort to running PTY session. `auto` clears any saved
            # level and hands depth back to the model.
            if t.pty_session and t.pty_session.running:
                try:
                    t.pty_session.write(f"/effort {effort}\r")
                except Exception:
                    pass

    def set_tab_max_turns(self, tab_id: str, n: int):
        t = self._tabs.get(tab_id)
        if t:
            t.max_turns = max(0, int(n))
            self._persist()

    # -- Launch options / session toggles -------------------------------------

    def set_tab_launch(self, tab_id: str, patch: dict) -> dict:
        """Merge a patch into a tab's launch options.

        Takes effect on the tab's next session start (these are launch flags).
        The patch is run through sanitize_launch, so a JS-side typo or a stale
        key can never reach the argv builder.
        """
        t = self._tabs.get(tab_id)
        if not t:
            return {"ok": False, "error": "no such tab"}
        if not isinstance(patch, dict):
            return {"ok": False, "error": "patch must be an object"}
        merged = dict(t.launch or {})
        merged.update(patch)
        t.launch = sanitize_launch(merged)
        self._persist()
        return {"ok": True, "launch": dict(t.launch),
                "restart_required": True}

    def set_tab_toggles(self, tab_id: str, patch: dict) -> dict:
        """Merge a patch into a tab's session toggles.

        Unlike launch options these are session commands, so anything that
        changed is applied to a live PTY immediately — no restart needed.
        """
        t = self._tabs.get(tab_id)
        if not t:
            return {"ok": False, "error": "no such tab"}
        if not isinstance(patch, dict):
            return {"ok": False, "error": "patch must be an object"}
        before = dict(t.toggles or {})
        merged = dict(before)
        merged.update(patch)
        t.toggles = sanitize_toggles(merged)
        self._persist()
        applied: list[str] = []
        if t.pty_session and t.pty_session.running:
            for tg in SESSION_TOGGLES:
                key = tg["key"]
                new = t.toggles.get(key)
                if new == before.get(key):
                    continue
                if tg["kind"] == "bool":
                    # /fast and /effort ultracode take an explicit on|off;
                    # /sandbox is a bare toggle, so only fire it when turning
                    # on (firing it again would flip it back).
                    if key == "ultracode":
                        cmd = f"/effort ultracode {'on' if new else 'off'}"
                    elif key == "fast_mode":
                        cmd = f"/fast {'on' if new else 'off'}"
                    elif new:
                        cmd = tg["cmd"](new)
                    else:
                        cmd = None
                else:
                    cmd = tg["cmd"](new)
                if not cmd:
                    continue
                try:
                    t.pty_session.write(cmd + "\r")
                    applied.append(cmd)
                    time.sleep(0.4)
                except Exception:
                    pass
        return {"ok": True, "toggles": dict(t.toggles), "applied": applied,
                "restart_required": False}

    def set_tab_remote_control(self, tab_id: str, enabled: bool) -> dict:
        t = self._tabs.get(tab_id)
        if not t:
            return {"ok": False, "error": "no such tab"}
        t.remote_control = bool(enabled)
        self._persist()
        return {"ok": True, "remote_control": t.remote_control,
                "restart_required": True}

    # -- Frontend diagnostics --------------------------------------------------

    def report_js_error(self, info: dict) -> dict:
        """Surface a frontend exception on stdout.

        The WebView swallows JS errors silently, so a broken handler used to
        show up only as a control that quietly did nothing. The frontend's
        error/unhandledrejection listeners funnel here instead.
        """
        try:
            d = info if isinstance(info, dict) else {"message": str(info)}
            where = d.get("source") or "?"
            line = d.get("line")
            loc = f"{where}:{line}" if line else where
            print(f"[js-error] {d.get('message', '')} @ {loc}", flush=True)
            stack = d.get("stack")
            if stack:
                print(f"[js-error]   {str(stack)[:1500]}", flush=True)
        except Exception:
            pass
        return {"ok": True}

    def report_ready(self, info: dict) -> dict:
        """Frontend signals that init() finished, with a feature census."""
        try:
            d = info if isinstance(info, dict) else {}
            print(
                "[boot]   frontend ready: "
                f"tabs={d.get('tabs')} models={d.get('models')} "
                f"commands={d.get('commands')} launchOptions={d.get('launchOptions')} "
                f"toggles={d.get('toggles')} tools={d.get('tools')} "
                f"permModes={d.get('permModes')}",
                flush=True,
            )
        except Exception:
            pass
        return {"ok": True}

    # -- CLI auto-update -------------------------------------------------------

    def get_update_status(self) -> dict:
        """Current auto-update phase (polled by the GUI after launch)."""
        st = dict(self._update_state)
        st["auto_update"] = bool(self._config.get("auto_update", True))
        st["cli_version"] = self._cli_version
        return st

    def set_auto_update(self, enabled: bool) -> dict:
        self._config["auto_update"] = bool(enabled)
        _save_json(CONFIG_FILE, self._config)
        return {"ok": True, "auto_update": bool(enabled)}

    def run_cli_update(self, manual: bool = True) -> dict:
        """Kick off a CLI update in the background. Returns immediately."""
        if self._update_state.get("phase") in ("checking", "updating"):
            return {"ok": True, "already_running": True}
        threading.Thread(
            target=self._update_worker, args=(bool(manual),), daemon=True
        ).start()
        return {"ok": True, "started": True}

    def start_auto_update(self) -> None:
        """Startup hook: update the CLI unless the user turned it off."""
        if not self._config.get("auto_update", True):
            self._update_state = {
                "phase": "disabled", "from": self._cli_version, "to": "",
                "changed": False, "message": "",
            }
            return
        threading.Thread(target=self._update_worker, args=(False,),
                         daemon=True).start()

    def _update_worker(self, manual: bool) -> None:
        self._update_state = {
            "phase": "checking", "from": self._cli_version, "to": "",
            "changed": False, "message": "",
        }
        self._push_update_state()
        try:
            self._update_state = {"phase": "updating",
                                   "from": self._cli_version, "to": "",
                                   "changed": False, "message": ""}
            self._push_update_state()
            result = _update_cli()
        except Exception as e:  # pragma: no cover - defensive
            result = {"phase": "error", "from": self._cli_version, "to": "",
                      "changed": False, "message": str(e)[:400]}
        result["manual"] = manual
        self._update_state = result
        if result.get("to"):
            self._cli_version = result["to"]
        self._push_update_state()

    def _push_update_state(self) -> None:
        try:
            self._js(f"onUpdateStatus({json.dumps(self.get_update_status())})")
        except Exception:
            pass

    def preview_tab_cmd(self, tab_id: str) -> dict:
        """Return the exact argv the tab's next interactive session will use.

        Lets the user see what their Launch Options actually produce before
        restarting, instead of discovering a bad flag when the session dies.
        """
        t = self._tabs.get(tab_id)
        if not t:
            return {"ok": False, "error": "no such tab"}
        if not _validate_cmd_fields(t):
            return {"ok": False, "error": self._t("invalid_launch_fields")}
        cmd = ["claude", "--model", t.model, "--verbose"]
        if t.effort and t.effort != "auto":
            cmd += ["--effort", t.effort]
        if t.session_id:
            cmd += ["--resume", t.session_id]
        cmd += _add_dir_args(t)
        if getattr(t, "remote_control", True):
            cmd += ["--remote-control"]
        cmd += _permission_mode_args(t)
        if t.permission_mode == "custom":
            for tool in t.allowed_tools or []:
                if tool in _VALID_TOOLS:
                    cmd += ["--allowedTools", tool]
        cmd += _launch_flags(t, interactive=True)
        if t.custom_flags:
            try:
                cmd += shlex.split(t.custom_flags)
            except Exception:
                pass
        return {"ok": True, "argv": cmd,
                "display": " ".join(shlex.quote(c) for c in cmd),
                "toggles": _session_toggle_cmds(t)}

    def set_tab_custom_flags(self, tab_id: str, flags: str):
        t = self._tabs.get(tab_id)
        if t:
            t.custom_flags = flags.strip()
            self._persist()

    def set_tab_system_prompt(self, tab_id: str, prompt: str):
        t = self._tabs.get(tab_id)
        if t:
            t.system_prompt = prompt.strip()
            self._persist()

    def set_tab_permission_mode(self, tab_id: str, mode: str):
        t = self._tabs.get(tab_id)
        if t and mode in {m[0] for m in PERMISSION_MODES}:
            t.permission_mode = mode
            self._persist()
            # Auto-send permission mode to running PTY session
            if mode != "custom" and t.pty_session and t.pty_session.running:
                try:
                    t.pty_session.write(f"/permissions {mode}\r")
                except Exception:
                    pass

    def set_tab_allowed_tools(self, tab_id: str, tools: list):
        t = self._tabs.get(tab_id)
        if t:
            t.allowed_tools = [x for x in tools if x in CLI_TOOLS]
            self._persist()

    # -- Global settings -------------------------------------------------------

    def save_api_key(self, key: str) -> dict:
        key = key.strip()
        if key and not key.startswith("sk-ant-"):
            return {"ok": False, "error": self._t("api_key_format")}
        self._config["api_key"] = key
        _save_json(CONFIG_FILE, self._config)
        return {"ok": True}

    def set_mode(self, mode: str):
        self._config["mode"] = mode
        _save_json(CONFIG_FILE, self._config)

    # -- Chat — clear display vs new session -----------------------------------

    def clear_display(self, tab_id: str):
        """Clear UI messages only. Session (context) is preserved."""
        t = self._tabs.get(tab_id)
        if t:
            t.messages = []
            self._persist()
            self._js(f"onDisplayCleared('{tab_id}')")

    def end_session(self, tab_id: str):
        """Stop PTY only. Keep history and session_id for resume."""
        t = self._tabs.get(tab_id)
        if t:
            if t.pty_session:
                t.pty_session.kill()
                t.pty_session = None
            self._persist()
            self._js(f"onSessionEnded('{tab_id}')")

    def new_session(self, tab_id: str):
        """Reset session_id AND clear messages. Fresh start."""
        t = self._tabs.get(tab_id)
        if t:
            t.session_id = ""
            t.messages = []
            t.screen_content = ""
            if t.pty_session:
                t.pty_session.kill()
                t.pty_session = None
            self._persist()
            self._js(f"onSessionReset('{tab_id}')")

    def save_screen_content(self, tab_id: str, content: str):
        """Save screen content from JS on window close."""
        tab = self._tabs.get(tab_id)
        if tab and content:
            tab.screen_content = content[-60000:]

    # -- Chat — send -----------------------------------------------------------

    def send_message(self, tab_id: str, message: str):
        tab = self._tabs.get(tab_id)
        if not tab:
            return
        mode = self._config.get("mode", "cli")
        # In PTY mode, pass everything (including slash commands) to CLI
        if mode == "cli" and HAS_PTY and tab.pty_session and tab.pty_session.running:
            threading.Thread(target=self._pty_send, args=(tab, message), daemon=True).start()
            return
        # Non-PTY: handle slash commands locally
        if message.startswith("/"):
            self._handle_slash(tab, message)
            return
        if mode == "api":
            if tab.streaming:
                return
            tab.streaming = True
            threading.Thread(target=self._send_api, args=(tab, message), daemon=True).start()
        elif HAS_PTY:
            # Terminal mode: send to PTY (full interactive CLI)
            threading.Thread(target=self._pty_send, args=(tab, message), daemon=True).start()
        else:
            # Fallback: stream-json (-p mode, no interactive permissions)
            if tab.streaming:
                return
            tab.streaming = True
            threading.Thread(target=self._send_cli, args=(tab, message), daemon=True).start()

    def pty_input(self, tab_id: str, data: str):
        """Direct keystroke/text input to PTY (for typing in terminal view)."""
        tab = self._tabs.get(tab_id)
        if not tab:
            return
        if tab.pty_session and tab.pty_session.running:
            tab.pty_session.write(data)
        else:
            # PTY not running — restart with text as new message
            text = data.rstrip('\r\n')
            if text:
                threading.Thread(
                    target=self._pty_send, args=(tab, text), daemon=True
                ).start()

    def pty_resize(self, tab_id: str, rows: int, cols: int):
        """Resize PTY terminal to match GUI container."""
        tab = self._tabs.get(tab_id)
        if tab and tab.pty_session:
            tab.pty_session.resize(rows, cols)

    def _handle_slash(self, tab: ChatTab, cmd: str):
        """Handle slash commands locally in the GUI."""
        tid = tab.id
        parts = cmd.strip().split(None, 1)
        command = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        if command == "/clear":
            self.clear_display(tid)
        elif command == "/compact":
            self.clear_display(tid)
            self._js(f"onSystemMsg('{tid}','info','Display cleared. Session context preserved via --resume.')")
        elif command == "/effort":
            # "ultracode" is not an effort level — it's a workflow-orchestration
            # toggle, so route it to the session-toggle path instead.
            low = arg.lower()
            if low.startswith("ultracode"):
                want = not low.endswith("off")
                self.set_tab_toggles(tid, {"ultracode": want})
                self._js(f"onSystemMsg({json.dumps(tid)},'info',"
                         f"{json.dumps('Ultracode: ' + ('on' if want else 'off'))})")
            elif low in _VALID_EFFORTS:
                tab.effort = low
                self._persist()
                if tab.pty_session and tab.pty_session.running:
                    try:
                        tab.pty_session.write(f"/effort {low}\r")
                    except Exception:
                        pass
                self._js(f"onSystemMsg({json.dumps(tid)},'info',{json.dumps(f'Effort: {tab.effort}')})")
                self._js(f"document.getElementById('selEffort').value={json.dumps(tab.effort)}")
            else:
                usage = "Usage: /effort " + "|".join(e[0] for e in EFFORT_LEVELS) + "|ultracode [on|off]"
                self._js(f"onSystemMsg({json.dumps(tid)},'info',{json.dumps(usage)})")
        elif command == "/model":
            target = _resolve_model(arg)
            if target:
                tab.model = target
                self._persist()
                label = next((m[1] for m in MODELS if m[0] == target), target)
                self._js(f"onSystemMsg({json.dumps(tid)},'info',{json.dumps(f'Model: {label}')})")
                self._js(f"document.getElementById('selModel').value={json.dumps(target)}")
                if tab.pty_session and tab.pty_session.running:
                    try:
                        tab.pty_session.write(f"/model {target}\r")
                    except Exception:
                        pass
            else:
                usage = "Usage: /model " + "|".join(a[0] for a in MODEL_ALIASES) + " (or any version from the picker)"
                self._js(f"onSystemMsg({json.dumps(tid)},'info',{json.dumps(usage)})")
        elif command == "/status":
            launch_on = [
                _LAUNCH_BY_KEY[k]["flag"]
                for k, v in (tab.launch or {}).items()
                if k in _LAUNCH_BY_KEY and v not in (False, "", [], None)
            ]
            toggles_on = [
                k for k, v in (tab.toggles or {}).items()
                if v not in (False, "", None)
            ]
            info_parts = [
                f"CLI: {self._cli_version or 'not found'}",
                f"Session: {tab.session_id or 'none'}",
                f"Model: {tab.model}",
                f"Effort: {tab.effort}",
                f"Project: {tab.project_path or 'none'}",
                f"Permission: {tab.permission_mode}",
                f"Remote Control: {'on' if getattr(tab, 'remote_control', True) else 'off'}",
                f"Launch flags: {' '.join(launch_on) if launch_on else 'none'}",
                f"Toggles: {', '.join(toggles_on) if toggles_on else 'none'}",
            ]
            self._js(f"onSystemMsg('{tid}','info',{json.dumps(chr(10).join(info_parts))})")
        elif command == "/help":
            lines = [
                "Ctrl+K — Command palette (every Claude Code command)",
                "",
                "/clear — Clear display (keep session)",
                "/compact — Clear + preserve context",
                "/effort low|medium|high|xhigh|max|auto|ultracode [on|off]",
                "/model " + "|".join(a[0] for a in MODEL_ALIASES[:6]) + "|… — Change model",
                "/status — Session, model, flags and toggles",
                "/help — This help",
                "(Other slash commands are forwarded to the Claude Code CLI.)",
                "",
                "Ctrl+L — Clear display",
                "Ctrl+Shift+L — End session (keep history)",
                "Ctrl+Shift+N — New session (full reset)",
            ]
            self._js(f"onSystemMsg('{tid}','info',{json.dumps(chr(10).join(lines))})")
        else:
            # Unknown slash — send as regular message
            self.send_message(tab.id, cmd)

    def respond_permission(self, tab_id: str, approved: bool, choice: str = "1"):
        """GUI callback: user approved/denied a tool permission request."""
        tab = self._tabs.get(tab_id)
        if not tab:
            return
        # PTY mode: send choice directly to terminal
        if tab.pty_session:
            tab.pty_session.write((choice if approved else "n") + "\r")
            return
        # Stdin pipe mode: signal the waiting thread
        if tab._perm_event:
            tab._perm_approved = approved
            tab._perm_event.set()

    def _pty_send(self, tab: ChatTab, message: str):
        """Send message to PTY terminal. Starts CLI if needed."""
        tid = tab.id
        try:
            need_new = (
                not tab.pty_session
                or not tab.pty_session.running
                or not tab.pty_session.pty
                or not tab.pty_session.pty.isalive()
            )
            if need_new:
                if tab.pty_session:
                    tab.pty_session.kill()
                tab.pty_session = PtySession(self, tab)
                tab.pty_session.start()
                self._js(f"showTerminal('{tid}')")
                # Wait for CLI to show welcome screen
                for _ in range(100):  # 10s
                    try:
                        has_content = any(ln.strip() for ln in tab.pty_session.screen.display)
                    except Exception:
                        has_content = False
                    if has_content:
                        time.sleep(1.5)  # Let welcome screen fully render
                        break
                    time.sleep(0.1)
                # Apply session toggles before the first message. Model,
                # effort, permission mode and Remote Control all ride launch
                # flags now; only the flagless toggles need typing.
                try:
                    for _cmd in _session_toggle_cmds(tab):
                        tab.pty_session.write(_cmd + "\r")
                        time.sleep(0.5)
                except Exception:
                    pass
            tab.pty_session.write(message + "\r")
        except FileNotFoundError:
            self._js(f"onStreamError('{tid}',{json.dumps(self._t('cli_not_found'))})")
        except Exception as e:
            self._js(f"onStreamError('{tid}',{json.dumps(str(e))})")

    def _build_cmd(self, tab: ChatTab, message: str) -> list:
        cmd = [
            "claude", "-p", message,
            "--output-format", "stream-json", "--verbose",
            "--model", tab.model,
        ]
        if tab.effort and tab.effort != "auto":
            cmd += ["--effort", tab.effort]
        cmd += _add_dir_args(tab)
        if tab.session_id:
            cmd += ["--resume", tab.session_id]
        if tab.system_prompt:
            cmd += ["--system-prompt", tab.system_prompt]
        # Permission mode: the CLI's native --permission-mode flag.
        cmd += _permission_mode_args(tab)
        if tab.permission_mode == "custom" and tab.allowed_tools:
            for tool in tab.allowed_tools:
                if tool in _VALID_TOOLS:
                    cmd += ["--allowedTools", tool]
        cmd += _launch_flags(tab, interactive=False)
        if tab.custom_flags:
            try:
                cmd += shlex.split(tab.custom_flags)
            except Exception:
                pass
        return cmd

    def _extract_text(self, data: dict) -> str:
        parts = []
        for blk in data.get("message", {}).get("content", []):
            if blk.get("type") == "text":
                parts.append(blk.get("text", ""))
        return "".join(parts)

    @staticmethod
    def _fmt_tool_input(name: str, inp: dict) -> str:
        if name == "Bash":
            return inp.get("command", str(inp))
        if name in ("Read", "Write"):
            return inp.get("file_path", str(inp))
        if name == "Edit":
            p = inp.get("file_path", "")
            old = inp.get("old_string", "")[:120]
            return f"{p}\n{old}{'...' if len(inp.get('old_string',''))>120 else ''}"
        if name == "Grep":
            return f"{inp.get('pattern','')}  in {inp.get('path','.')}"
        if name == "Glob":
            return inp.get("pattern", str(inp))
        return json.dumps(inp, ensure_ascii=False)[:300]

    def open_terminal(self, tab_id: str):
        """Open Claude Code in a visible terminal for permission interaction."""
        tab = self._tabs.get(tab_id)
        if not tab:
            return
        # Kill current headless process
        if tab.process:
            try:
                tab.process.kill()
            except Exception:
                pass
            tab.process = None
        tab.streaming = False
        if not _validate_cmd_fields(tab):
            self._js(f"onStreamEnd({json.dumps(tab_id)})")
            return
        cwd = os.path.abspath(tab.project_path) if tab.project_path else None
        # Build interactive command (no -p, no stream-json)
        cmd_parts = ["claude", "--model", tab.model]
        if tab.effort and tab.effort != "auto":
            cmd_parts += ["--effort", tab.effort]
        if tab.session_id:
            cmd_parts += ["--resume", tab.session_id]
        cmd_parts += _add_dir_args(tab)
        cmd_parts += _permission_mode_args(tab)
        cmd_parts += _launch_flags(tab, interactive=True)
        # Open in a new visible console window - list args, shell=False, so no
        # field can be interpreted by a shell.
        subprocess.Popen(
            cmd_parts,
            cwd=cwd,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        self._js(f"onStreamEnd({json.dumps(tab_id)})")

    def _send_cli(self, tab: ChatTab, message: str):
        tid = tab.id
        try:
            if not _validate_cmd_fields(tab):
                self._js(f"onStreamError('{tid}',{json.dumps(self._t('invalid_launch_fields'))})")
                return
            self._js(f"onStreamStart('{tid}')")
            cmd = self._build_cmd(tab, message)
            env = os.environ.copy()
            if tab.effort and tab.effort != "auto":
                env["CLAUDE_CODE_EFFORT_LEVEL"] = tab.effort
            tab.process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                # Close stdin explicitly. `claude -p <prompt>` still polls
                # stdin for piped input and waits ~3s before giving up, so an
                # inherited/open stdin cost every message a 3-second stall.
                stdin=subprocess.DEVNULL,
                cwd=tab.project_path or None,
                text=True, encoding="utf-8", errors="replace", env=env,
            )
            full = ""
            shown_tools = set()
            tool_names = {}

            while tab.streaming:
                line = tab.process.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                mt = data.get("type", "")

                # --- system init ---
                if mt == "system":
                    sub = data.get("subtype", "")
                    if sub == "init":
                        sid = data.get("session_id", "")
                        if sid:
                            tab.session_id = sid
                        self._js(f"onSystemMsg('{tid}','session_start',{json.dumps(sid)})")

                # --- content_block_start (early tool card) ---
                elif mt == "content_block_start":
                    cb = data.get("content_block", {})
                    if cb.get("type") == "tool_use":
                        tool_id = cb.get("id", "")
                        name = cb.get("name", "tool")
                        tool_names[tool_id] = name
                        if tool_id and tool_id not in shown_tools:
                            shown_tools.add(tool_id)
                            self._js(f"onToolStart('{tid}',{json.dumps(name)},'')")

                # --- content_block_delta (streaming text/thinking) ---
                elif mt == "content_block_delta":
                    delta_obj = data.get("delta", {})
                    dt = delta_obj.get("type", "")
                    if dt == "text_delta":
                        delta = delta_obj.get("text", "")
                        if delta:
                            full += delta
                            self._js(f"onStreamToken('{tid}',{json.dumps(delta)})")
                    elif dt == "thinking_delta":
                        think = delta_obj.get("thinking", "")
                        if think:
                            self._js(f"onThinking('{tid}',{json.dumps(think[:500])})")

                # --- assistant (accumulated snapshot) ---
                elif mt == "assistant":
                    content_blks = data.get("message", {}).get("content", [])
                    all_text = self._extract_text(data)
                    if all_text and len(all_text) > len(full):
                        delta = all_text[len(full):]
                        full = all_text
                        self._js(f"onStreamToken('{tid}',{json.dumps(delta)})")
                    for blk in content_blks:
                        bt = blk.get("type", "")
                        if bt == "tool_use":
                            tool_id = blk.get("id", "")
                            name = blk.get("name", "tool")
                            inp = blk.get("input", {})
                            tool_names[tool_id] = name
                            if tool_id and tool_id not in shown_tools:
                                shown_tools.add(tool_id)
                                inp_s = self._fmt_tool_input(name, inp)
                                self._js(f"onToolStart('{tid}',{json.dumps(name)},{json.dumps(inp_s)})")
                            elif inp and tool_id:
                                inp_s = self._fmt_tool_input(name, inp)
                                self._js(f"onToolUpdate('{tid}',{json.dumps(name)},{json.dumps(inp_s)})")
                        elif bt == "thinking":
                            think = blk.get("thinking", "")
                            if think:
                                self._js(f"onThinking('{tid}',{json.dumps(think[:500])})")

                # --- tool_result ---
                elif mt == "tool_result":
                    tool_id = data.get("tool_use_id", "")
                    name = tool_names.get(tool_id, "Tool")
                    raw = data.get("content", "")
                    is_err = data.get("is_error", False)
                    out = raw[:2000] if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)[:2000]
                    self._js(f"onToolResult('{tid}',{json.dumps(name)},{json.dumps(out)},{json.dumps(is_err)})")

                # --- final result ---
                elif mt == "result":
                    rt = data.get("result", "")
                    if rt and len(rt) > len(full):
                        delta = rt[len(full):]
                        full = rt
                        self._js(f"onStreamToken('{tid}',{json.dumps(delta)})")
                    sid = data.get("session_id")
                    if sid:
                        tab.session_id = sid
                    cost = data.get("cost_usd", 0)
                    dur = data.get("duration_ms", 0)
                    turns = data.get("num_turns", 0)
                    self._js(f"onResultMeta('{tid}',{json.dumps(cost)},{json.dumps(dur)},{json.dumps(turns)})")
                    if data.get("is_error"):
                        self._js(f"onStreamError('{tid}',{json.dumps(rt or 'Error')})")
                        tab.streaming = False
                        return

                # --- rate limit ---
                elif mt == "rate_limit_event":
                    rli = data.get("rate_limit_info", {})
                    status = rli.get("status", "")
                    if status != "allowed":
                        self._js(f"onSystemMsg('{tid}','rate_limit',{json.dumps(status)})")

                # Other events (content_block_stop, message_start/delta/stop,
                # ping, user) silently ignored

            try:
                tab.process.wait(timeout=5)
            except Exception:
                pass
            if full:
                tab.messages.append({"role": "user", "content": message, "ts": time.time()})
                tab.messages.append({"role": "assistant", "content": full, "ts": time.time()})
                self._persist()
            self._js(f"onStreamEnd('{tid}')")
        except FileNotFoundError:
            self._js(f"onStreamError('{tid}',{json.dumps(self._t('cli_not_found'))})")
        except Exception as e:
            self._js(f"onStreamError('{tid}',{json.dumps(str(e))})")
        finally:
            tab.streaming = False
            tab.process = None

    def _send_api(self, tab: ChatTab, message: str):
        tid = tab.id
        try:
            key = self._config.get("api_key")
            if not key:
                self._js(f"onStreamError('{tid}',{json.dumps(self._t('api_key_missing'))})")
                return
            import anthropic
            client = anthropic.Anthropic(api_key=key)
            sys_p = tab.system_prompt or "You are Claude, an AI dev assistant. Be concise. Reply in the user's language."
            if tab.project_path:
                tree = _build_tree(tab.project_path)
                sys_p += f"\n\nProject: {tab.project_path}\n{self._tree_str(tree)}"
            msgs = [{"role": m["role"], "content": m["content"]} for m in tab.messages[-10:]]
            msgs.append({"role": "user", "content": message})
            self._js(f"onStreamStart('{tid}')")
            full = ""
            with client.messages.stream(
                model=_api_model_id(tab.model), max_tokens=8192, system=sys_p, messages=msgs,
            ) as stream:
                for text in stream.text_stream:
                    if not tab.streaming:
                        break
                    full += text
                    self._js(f"onStreamToken('{tid}',{json.dumps(text)})")
            if full:
                tab.messages.append({"role": "user", "content": message, "ts": time.time()})
                tab.messages.append({"role": "assistant", "content": full, "ts": time.time()})
                self._persist()
            self._js(f"onStreamEnd('{tid}')")
        except Exception as e:
            self._js(f"onStreamError('{tid}',{json.dumps(str(e))})")
        finally:
            tab.streaming = False

    def stop_streaming(self, tab_id: str):
        t = self._tabs.get(tab_id)
        if t:
            t.streaming = False
            if t.process:
                try:
                    t.process.kill()
                except Exception:
                    pass
                try:
                    t.process.wait(timeout=2)
                except Exception:
                    pass
                t.process = None
            if t.pty_session:
                # Send Ctrl+C to interrupt, don't kill the session
                if t.pty_session.pty and t.pty_session.pty.isalive():
                    try:
                        t.pty_session.write("\x03")  # Ctrl+C
                    except Exception:
                        t.pty_session.kill()
                        t.pty_session = None
                else:
                    t.pty_session = None
            self._js(f"onStreamEnd('{tab_id}')")

    def _tree_str(self, tree, pfx="", mx=50):
        lines = []
        for it in tree:
            if len(lines) >= mx:
                lines.append(pfx + "...")
                break
            if it["type"] == "dir":
                lines.append(f"{pfx}{it['name']}/")
                sub = self._tree_str(it.get("children", []), pfx + "  ", mx - len(lines))
                if sub:
                    lines.extend(sub.split("\n"))
            else:
                lines.append(f"{pfx}{it['name']}")
        return "\n".join(l for l in lines if l)

    def _js(self, code):
        if self._window:
            try:
                self._window.evaluate_js(code)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Embedded UI
# ---------------------------------------------------------------------------
HTML = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<style>
:root{
  --bg:#0a0a1a;--sf:#12122a;--sf2:#1a1a3a;--bd:#2a2a4a;
  --ac:#6366f1;--acl:#818cf8;--tx:#e2e8f0;--txd:#94a3b8;
  --ok:#10b981;--err:#ef4444;--warn:#f59e0b;
  --fn:'Segoe UI',-apple-system,BlinkMacSystemFont,'Noto Sans JP',sans-serif;
  --mono:'Cascadia Code','Fira Code',Consolas,monospace;
}
*{margin:0;padding:0;box-sizing:border-box}
html,body{height:100%;overflow:hidden;background:var(--bg);color:var(--tx);font-family:var(--fn);font-size:14px}
.app{display:flex;height:100vh}
.sb{width:260px;min-width:260px;background:var(--sf);border-right:1px solid var(--bd);display:flex;flex-direction:column}
.sb-head{padding:14px 16px;border-bottom:1px solid var(--bd)}
.sb-head h1{font-size:15px;color:var(--acl)}
.sb-head .sub{font-size:10px;color:var(--txd);margin-top:2px}
.proj-bar{padding:8px 12px;border-bottom:1px solid var(--bd)}
.proj-btn{width:100%;padding:8px 10px;background:var(--sf2);border:1px dashed var(--bd);border-radius:8px;color:var(--tx);cursor:pointer;text-align:left;font-size:12px;transition:all .2s}
.proj-btn:hover{border-color:var(--ac)}.proj-btn.active{border-style:solid;border-color:var(--ac)}
.proj-btn .pp{font-size:10px;color:var(--txd);margin-top:2px;word-break:break-all}
.ftree{flex:1;overflow-y:auto;padding:4px 0}
.ti{display:flex;align-items:center;gap:5px;padding:3px 14px;cursor:pointer;font-size:12px;color:var(--txd);transition:background .1s;user-select:none;white-space:nowrap}
.ti:hover{background:rgba(99,102,241,.06);color:var(--tx)}
.ti-icon{width:14px;text-align:center;flex-shrink:0;font-size:11px}
.ti-name{overflow:hidden;text-overflow:ellipsis}
.sb-foot{padding:8px 12px;border-top:1px solid var(--bd);display:flex;gap:4px}
.sb-foot button{flex:1;padding:5px;background:transparent;border:1px solid var(--bd);border-radius:6px;color:var(--txd);cursor:pointer;font-size:10px;transition:all .2s}
.sb-foot button:hover{border-color:var(--ac);color:var(--tx)}
.mn{flex:1;display:flex;flex-direction:column;min-width:0}
.topbar{padding:6px 14px;border-bottom:1px solid var(--bd);display:flex;align-items:center;gap:6px;background:var(--sf);font-size:12px;flex-wrap:wrap}
.dot{width:8px;height:8px;border-radius:50%}.dot.ok{background:var(--ok)}.dot.err{background:var(--err)}.dot.warn{background:var(--warn)}
.topbar .sp{flex:1}
.topbar select{background:var(--sf2);border:1px solid var(--bd);border-radius:5px;color:var(--tx);padding:3px 6px;font-size:10px;cursor:pointer;outline:none}
.topbar select:focus{border-color:var(--ac)}
.tb-btn{background:transparent;border:1px solid var(--bd);border-radius:5px;color:var(--txd);padding:3px 8px;cursor:pointer;font-size:11px;transition:all .2s}
.tb-btn:hover{border-color:var(--ac);color:var(--tx)}
.topbar .pn{font-weight:600;font-size:12px;color:var(--acl);max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tabs{display:flex;align-items:center;background:var(--bg);border-bottom:1px solid var(--bd);overflow-x:auto;min-height:30px;padding:0 4px}
.tabs::-webkit-scrollbar{height:0}
.tab{display:flex;align-items:center;gap:4px;padding:5px 10px;font-size:11px;color:var(--txd);cursor:pointer;border-bottom:2px solid transparent;white-space:nowrap;flex-shrink:0;transition:all .15s}
.tab:hover{color:var(--tx);background:rgba(255,255,255,.02)}
.tab.act{color:var(--acl);border-bottom-color:var(--ac);background:rgba(99,102,241,.05)}
.tab .tc{display:none;font-size:9px;margin-left:4px;padding:1px 3px;border-radius:3px;color:var(--txd)}
.tab:hover .tc{display:inline-block}.tab .tc:hover{background:rgba(255,255,255,.1);color:var(--tx)}
.tab .nd{width:6px;height:6px;border-radius:50%;background:var(--ac);display:none}
.tab.notif .nd{display:inline-block;animation:pulse 1s infinite}
.tab.perm-notif .nd{display:inline-block;background:#ef4444;animation:pulse .8s infinite}
.tab.strm .tn{animation:sp 1.5s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
@keyframes sp{0%,100%{opacity:1}50%{opacity:.5}}
.tab-add{padding:3px 10px;color:var(--txd);cursor:pointer;font-size:15px;flex-shrink:0;line-height:1}
.tab-add:hover{color:var(--acl)}
.tab-rename{background:var(--bg);border:1px solid var(--ac);border-radius:3px;color:var(--tx);font-size:11px;padding:1px 4px;width:80px;outline:none}
.cc{flex:1;display:flex;position:relative;min-height:0}
.chat{flex:1;overflow-y:auto;padding:16px 20px;flex-direction:column;display:none}
.chat.vis{display:flex}
.term{flex:1;display:none;flex-direction:column;background:#0d1117;overflow:hidden}
.term.vis{display:flex}
.term-screen{flex:1;overflow:auto;padding:8px 12px;margin:0;font-family:'Cascadia Code','Consolas','Courier New',monospace;font-size:13px;line-height:1.45;color:#c9d1d9;white-space:pre;tab-size:8}
.term-screen::-webkit-scrollbar{width:6px}.term-screen::-webkit-scrollbar-thumb{background:#30363d;border-radius:3px}
.term-perm{display:none;gap:8px;padding:10px 16px;background:#1c2128;border-top:1px solid #30363d;align-items:center;flex-shrink:0}
.term-perm.vis{display:flex}
.perm-lbl{color:#8b949e;font-size:12px;margin-right:auto}
.perm-btn{padding:6px 16px;border-radius:6px;border:1px solid #30363d;background:#21262d;color:#c9d1d9;cursor:pointer;font-size:12px;transition:all .15s}
.perm-btn:hover{background:#30363d}
.perm-btn.a{border-color:#238636;color:#3fb950}.perm-btn.a:hover{background:#238636;color:#fff}
.perm-btn.s{border-color:#1f6feb;color:#58a6ff}.perm-btn.s:hover{background:#1f6feb;color:#fff}
.perm-btn.d{border-color:#da3633;color:#f85149}.perm-btn.d:hover{background:#da3633;color:#fff}
.msg{margin-bottom:14px;max-width:82%;animation:fi .2s}
@keyframes fi{from{opacity:0;transform:translateY(4px)}to{opacity:1}}
.msg.user{margin-left:auto}
.msg.user .bub{background:var(--ac);color:#fff;border-radius:14px 14px 4px 14px;padding:9px 14px}
.msg.assistant .bub{background:var(--sf2);border:1px solid var(--bd);border-radius:14px 14px 14px 4px;padding:10px 14px}
.bub{font-size:13px;line-height:1.7;word-break:break-word}
.bub pre{background:var(--bg);border:1px solid var(--bd);border-radius:6px;padding:8px 10px;margin:6px 0;overflow-x:auto;font-family:var(--mono);font-size:12px;line-height:1.5}
.bub code{font-family:var(--mono);font-size:12px;background:rgba(99,102,241,.15);padding:1px 4px;border-radius:3px}
.bub pre code{background:none;padding:0}
.bub ul,.bub ol{margin:4px 0 4px 18px}.bub li{margin:2px 0}
.bub h1,.bub h2,.bub h3{color:var(--acl);margin:8px 0 4px;font-size:14px}
.msg .ts{font-size:9px;color:var(--txd);margin-top:3px}.msg.user .ts{text-align:right}
.typing{display:flex;gap:4px;padding:6px 14px}
.typing span{width:5px;height:5px;background:var(--acl);border-radius:50%;animation:bounce .6s infinite alternate}
.typing span:nth-child(2){animation-delay:.2s}.typing span:nth-child(3){animation-delay:.4s}
@keyframes bounce{to{transform:translateY(-5px);opacity:.4}}
.ia{padding:10px 16px;border-top:1px solid var(--bd);background:var(--sf);position:relative}
.ir{display:flex;gap:6px}
.ir textarea{flex:1;background:var(--bg);border:1px solid var(--bd);border-radius:8px;padding:9px 12px;color:var(--tx);font-family:var(--fn);font-size:13px;resize:none;min-height:40px;max-height:180px;outline:none;transition:border .2s;line-height:1.5}
.ir textarea:focus{border-color:var(--ac)}.ir textarea::placeholder{color:var(--txd)}
.snd{background:var(--ac);border:none;border-radius:8px;color:#fff;width:40px;cursor:pointer;font-size:16px;transition:all .15s;display:flex;align-items:center;justify-content:center}
.snd:hover{filter:brightness(1.1)}.snd:disabled{opacity:.3;cursor:not-allowed}.snd.stop{background:var(--err)}
.ia-nav{display:flex;gap:4px;padding:2px 0;flex-wrap:wrap;align-items:center}
.nav-btn{background:none;border:1px solid #30363d;border-radius:6px;color:#8b949e;font-size:11px;padding:2px 8px;cursor:pointer;display:flex;align-items:center;gap:3px;transition:all .15s;white-space:nowrap;flex-shrink:0}
.nav-btn:hover{border-color:#58a6ff;color:#c9d1d9;background:rgba(88,166,255,.08)}
.nav-btn.nav-warn:hover{border-color:#f0883e;color:#f0883e;background:rgba(240,136,62,.08)}
.nav-btn.nav-danger:hover{border-color:#f85149;color:#f85149;background:rgba(248,81,73,.08)}
.nav-btn.nav-toggle.active{border-color:var(--ac);color:var(--ac);background:rgba(99,102,241,.12)}
.nav-ico{font-size:12px}
.nav-btn kbd{background:#21262d;border:1px solid #30363d;border-radius:3px;padding:0 3px;font-size:9px;font-family:inherit;color:#8b949e;margin-left:1px}
.nav-keys kbd{background:#21262d;border:1px solid #30363d;border-radius:3px;padding:0 3px;font-size:9px;font-family:inherit;color:#8b949e}
.nav-sep{width:1px;background:#30363d;margin:0 3px;align-self:stretch;flex-shrink:0}
.nav-keys{font-size:9px;color:#6e7681;display:flex;align-items:center;gap:2px;flex-shrink:0}
@media(max-width:900px){.nav-btn kbd{display:none}.nav-keys{display:none}}
.ih{font-size:10px;color:var(--txd);margin-top:3px}
.ac-popup{display:none;position:absolute;bottom:100%;left:12px;right:12px;background:var(--sf2);border:1px solid var(--bd);border-radius:8px;max-height:220px;overflow-y:auto;z-index:10;margin-bottom:2px;padding:4px 0;box-shadow:0 -4px 16px rgba(0,0,0,.3)}
.ac-popup.vis{display:block}
.ac-item{padding:7px 14px;cursor:pointer;display:flex;gap:10px;align-items:center;font-size:12px;transition:background .08s}
.ac-item:hover,.ac-item.sel{background:rgba(99,102,241,.15)}
.ac-cmd{color:var(--acl);font-family:var(--mono);font-weight:600;font-size:12px;min-width:80px}
.ac-desc{color:var(--txd);font-size:11px}
.wc{text-align:center;padding:40px 20px}
.wc h2{font-size:20px;color:var(--acl);margin-bottom:6px}
.wc p{color:var(--txd);font-size:12px;max-width:380px;margin:0 auto 14px;line-height:1.7}
.tips{display:grid;grid-template-columns:1fr 1fr;gap:6px;max-width:380px;margin:0 auto}
.tip{background:var(--sf2);border:1px solid var(--bd);border-radius:8px;padding:10px;text-align:left;cursor:pointer;transition:border-color .2s}
.tip:hover{border-color:var(--ac)}
.tip .emoji{font-size:16px}.tip .tt{font-size:11px;font-weight:600;margin-top:2px}.tip .td{font-size:10px;color:var(--txd)}
.vw{position:fixed;top:0;right:-45%;width:45%;height:100%;background:var(--sf);border-left:1px solid var(--bd);z-index:50;transition:right .25s;display:flex;flex-direction:column}
.vw.open{right:0}
.vw-head{padding:8px 14px;border-bottom:1px solid var(--bd);display:flex;align-items:center;justify-content:space-between}
.vw-head .fn{font-size:12px;font-weight:600;color:var(--acl)}
.vw-head button{background:none;border:none;color:var(--txd);cursor:pointer;font-size:16px}
.vw-body{flex:1;overflow:auto;padding:10px 14px}
.vw-body pre{font-family:var(--mono);font-size:12px;line-height:1.6;white-space:pre-wrap;word-break:break-all}
.mo{position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:90;display:none;align-items:center;justify-content:center}
.mo.open{display:flex}
.md{background:var(--sf);border:1px solid var(--bd);border-radius:12px;padding:24px;width:420px;max-width:90%;max-height:80vh;overflow-y:auto}
.md h3{font-size:16px;color:var(--acl);margin-bottom:12px}
.md label{display:block;font-size:11px;color:var(--txd);margin:10px 0 3px}
.md input,.md select,.md textarea{width:100%;padding:8px 10px;background:var(--bg);border:1px solid var(--bd);border-radius:6px;color:var(--tx);font-size:12px;outline:none;font-family:var(--fn)}
.md textarea{min-height:60px;resize:vertical;font-family:var(--mono);font-size:11px}
.md input:focus,.md textarea:focus{border-color:var(--ac)}
.md .nt{font-size:10px;color:var(--txd);margin-top:3px}
.md .br{display:flex;gap:6px;margin-top:16px}
.md .br button{flex:1;padding:8px;border-radius:6px;font-size:12px;cursor:pointer;border:none;transition:all .2s}
.md .bp{background:var(--ac);color:#fff}.md .bg{background:transparent;border:1px solid var(--bd);color:var(--txd)}
.md .sep{border:none;border-top:1px solid var(--bd);margin:14px 0}
.sys-info{display:flex;align-items:center;justify-content:center;padding:8px;font-size:11px;color:var(--txd);opacity:.7}
/* Tool cards */
.tc{margin:6px 0;padding:8px 12px;background:rgba(99,102,241,.08);border-left:3px solid var(--ac);border-radius:0 8px 8px 0;font-size:12px}
.tc-head{display:flex;align-items:center;gap:6px;font-size:12px;font-weight:600}
.tc-inp{margin:4px 0 0;padding:6px 8px;background:var(--bg);border-radius:4px;font-family:var(--mono);font-size:11px;max-height:80px;overflow:auto;word-break:break-all;white-space:pre-wrap}
.tc-res{margin:4px 0 0;padding:6px 8px;background:var(--bg);border-radius:4px;font-family:var(--mono);font-size:10px;max-height:150px;overflow:auto;white-space:pre-wrap;word-break:break-all;cursor:pointer}
.tc-res.coll{max-height:40px;overflow:hidden;position:relative}
.tc-res.coll::after{content:'▼ click to expand';position:absolute;bottom:0;left:0;right:0;text-align:center;background:linear-gradient(transparent,var(--bg));padding:4px;font-size:9px;color:var(--txd)}
.tc-err{border-left-color:var(--err)}
.tc-ok{border-left-color:#4a7}
.tc-perm{border-left-color:#e8a735;background:rgba(232,167,53,.1)}
.tc-perm .perm-prompt{margin:4px 0;padding:4px 8px;font-size:10px;color:var(--txd);font-family:var(--mono);background:var(--bg);border-radius:4px}
.tc-perm .perm-btns{display:flex;gap:8px;margin-top:8px;justify-content:flex-end}
.tc-perm .perm-btns button{padding:5px 16px;border:none;border-radius:6px;font-size:12px;font-weight:600;cursor:pointer;transition:background .15s}
.perm-btn-allow{background:var(--ac);color:#fff}.perm-btn-allow:hover{opacity:.85}
.perm-btn-deny{background:#555;color:#fff}.perm-btn-deny:hover{background:#777}
/* Thinking indicator */
.think{margin:6px 0;padding:6px 12px;font-size:11px;color:var(--txd);font-style:italic;border-left:2px solid var(--bd);opacity:.7}
/* Result meta */
.res-meta{display:flex;gap:12px;justify-content:flex-end;padding:4px 12px;font-size:10px;color:var(--txd);opacity:.6}
.res-meta span{display:flex;align-items:center;gap:3px}
::-webkit-scrollbar{width:5px}::-webkit-scrollbar-track{background:transparent}::-webkit-scrollbar-thumb{background:var(--bd);border-radius:3px}
</style>
</head>
<body>

<!-- Global Settings Modal -->
<div class="mo" id="globalModal"><div class="md">
  <h3 data-i18n="globalTitle">Global Settings</h3>
  <label data-i18n="language">Language</label>
  <select id="langSelect" onchange="chLang(this.value)">
    <option value="en">English</option><option value="ja">日本語</option>
    <option value="zh">中文</option><option value="ko">한국어</option>
  </select>
  <label data-i18n="backendMode">Backend Mode</label>
  <select id="modeSelect">
    <option value="cli" data-i18n="optCLI">Claude Code CLI</option>
    <option value="api" data-i18n="optAPI">Anthropic API</option>
  </select>
  <div class="nt" data-i18n="cliNote">CLI mode</div>
  <div id="apiSec" style="display:none">
    <label data-i18n="apiKey">API Key</label>
    <input type="password" id="apiKeyInput" placeholder="sk-ant-api03-..." spellcheck="false">
    <div class="nt" data-i18n="apiKeyHint">Key stored locally only</div>
  </div>
  <hr class="sep">
  <label><input type="checkbox" id="chkAutoUpdate" style="width:auto;margin-right:6px" onchange="chAutoUpdate(this.checked)"> <span data-i18n="autoUpdate">Auto-update Claude Code CLI on launch</span></label>
  <div class="nt" data-i18n="autoUpdateNote">Runs `claude update` in the background at startup.</div>
  <div style="display:flex;align-items:center;gap:8px;margin-top:6px">
    <span class="nt" style="margin:0"><span data-i18n="cliVersion">CLI version</span>: <span id="cliVerLabel" style="font-family:var(--mono);color:var(--tx)">-</span></span>
    <button class="bg" style="font-size:10px;padding:3px 8px" onclick="updateNow()" data-i18n="updNow">Update now</button>
  </div>
  <div class="br">
    <button class="bg" onclick="closeGlobal()" data-i18n="cancel">Cancel</button>
    <button class="bp" onclick="saveGlobal()" data-i18n="save">Save</button>
  </div>
</div></div>

<!-- Command Palette: every Claude Code command, searchable -->
<div class="mo" id="cmdModal"><div class="md" style="width:760px;max-width:94vw">
  <h3 data-i18n="cmdPalette">Command Palette</h3>
  <div class="nt" data-i18n="cmdPaletteHint">Search every Claude Code command.</div>
  <input type="text" id="cmdSearch" data-i18n-ph="cmdSearchPH" placeholder="Type to filter commands…"
         spellcheck="false" autocomplete="off" oninput="renderCmdList()" onkeydown="cmdKey(event)"
         style="margin-top:8px">
  <div id="cmdCats" style="display:flex;flex-wrap:wrap;gap:4px;margin-top:8px"></div>
  <div id="cmdList" style="max-height:52vh;overflow-y:auto;margin-top:8px;border:1px solid var(--bd);border-radius:6px;background:var(--bg)"></div>
  <div class="br"><button class="bp" onclick="closeCmdPalette()" data-i18n="close">Close</button></div>
</div></div>

<!-- Tab Settings Modal -->
<div class="mo" id="tabModal"><div class="md">
  <h3 data-i18n="tabTitle">Tab Settings</h3>
  <label data-i18n="customFlags">Custom CLI Flags</label>
  <input type="text" id="tsFlags" placeholder="--flag value">
  <div class="nt" data-i18n="customFlagsNote">Extra flags</div>
  <label data-i18n="sysPrompt">System Prompt</label>
  <textarea id="tsSysPrompt" data-i18n-ph="sysPromptPH" placeholder="Custom system prompt"></textarea>
  <hr class="sep">
  <label data-i18n="permMode">Permission Mode</label>
  <select id="tsPermMode" onchange="togglePermTools()"></select>
  <div class="nt" id="permNote" data-i18n="permNoteDefault">Interactive — tools require approval in GUI</div>
  <div id="permToolsBox" style="display:none;margin-top:8px">
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px 12px;max-height:240px;overflow-y:auto" id="permToolGrid"></div>
  </div>
  <hr class="sep">
  <label><input type="checkbox" id="tsRemoteControl" style="width:auto;margin-right:6px"> <span data-i18n="remoteControl">Remote Control</span></label>
  <hr class="sep">
  <!-- Session toggles: applied live to a running session -->
  <div style="display:flex;align-items:center;justify-content:space-between">
    <label style="margin:0" data-i18n="sessionToggles">Session Toggles</label>
  </div>
  <div class="nt" data-i18n="sessionTogglesNote">Applied immediately to a running session.</div>
  <div id="toggleGrid" style="margin-top:6px"></div>
  <hr class="sep">
  <!-- Launch options: CLI flags, rendered from the Python spec -->
  <div style="display:flex;align-items:center;justify-content:space-between">
    <label style="margin:0" data-i18n="launchOptions">Launch Options</label>
    <button class="bg" style="font-size:10px;padding:3px 8px" onclick="previewLaunchCmd()" data-i18n="previewCmd">Preview launch command</button>
  </div>
  <div class="nt" data-i18n="launchOptionsNote">Applied as CLI flags when this tab's session starts.</div>
  <pre id="launchPreview" style="display:none;white-space:pre-wrap;word-break:break-all;font-family:var(--mono);font-size:10px;background:var(--bg);border:1px solid var(--bd);border-radius:4px;padding:8px;margin-top:6px;max-height:140px;overflow:auto"></pre>
  <div id="launchGroups" style="margin-top:6px"></div>
  <hr class="sep">
  <label data-i18n="sessionId">Session ID</label>
  <div class="nt" id="tsSessionId" style="word-break:break-all;user-select:all">-</div>
  <div class="br">
    <button class="bg" onclick="closeTabModal()" data-i18n="close">Close</button>
    <button class="bp" onclick="saveTabModal()" data-i18n="save">Save</button>
  </div>
</div></div>

<!-- File viewer -->
<div class="vw" id="fileViewer">
  <div class="vw-head"><span class="fn" id="vwName"></span><button onclick="closeVw()">&#x2715;</button></div>
  <div class="vw-body"><pre id="vwContent"></pre></div>
</div>

<!-- Permission Dialog -->
<div class="mo" id="permModal"><div class="md">
  <h3 data-i18n="permTitle">Permission Required</h3>
  <div style="margin:12px 0">
    <div style="display:flex;align-items:center;gap:8px">
      <span style="font-size:18px">&#x1F527;</span>
      <span style="font-weight:600;font-size:14px" id="permToolName">Tool</span>
    </div>
    <div id="permDescWrap" style="margin-top:8px;padding:10px;background:var(--bg);border:1px solid var(--bd);border-radius:6px;font-family:var(--mono);font-size:11px;word-break:break-all;max-height:120px;overflow:auto" >
      <span id="permDesc"></span>
    </div>
  </div>
  <div class="br">
    <button class="bg" onclick="denyPerm()" data-i18n="permDeny">Deny</button>
    <button class="bp" onclick="allowPerm()" data-i18n="permAllow">Allow</button>
  </div>
</div></div>

<!-- App -->
<div class="app">
  <div class="sb">
    <div class="sb-head"><h1>Claude Code GUI</h1><div class="sub" data-i18n="appSub">Multi-tab AI Development</div></div>
    <div class="proj-bar">
      <button class="proj-btn" id="projBtn" onclick="selectProject()">
        <div id="projLabel" data-i18n="selProj">Select project...</div>
        <div class="pp" id="projPath"></div>
      </button>
    </div>
    <div class="ftree" id="fileTree"><div style="padding:14px;color:var(--txd);font-size:11px;text-align:center" data-i18n="treeHint">Select a project to see file tree</div></div>
    <div class="sb-foot">
      <button onclick="refreshTree()" data-i18n="refresh">Refresh</button>
      <button onclick="doClearDisplay()" data-i18n="clearDisp">Clear</button>
      <button onclick="openGlobal()" data-i18n="settings">Settings</button>
    </div>
  </div>
  <div class="mn">
    <div class="topbar">
      <div class="dot" id="stDot"></div>
      <span id="stText" style="color:var(--txd)" data-i18n="starting">Starting...</span>
      <span class="pn" id="topProj"></span>
      <span id="updPill" style="display:none;font-size:10px;padding:2px 7px;border-radius:9px;border:1px solid var(--bd);color:var(--txd);cursor:pointer" onclick="openGlobal()"></span>
      <span class="sp"></span>
      <select id="selModel" onchange="chModel(this.value)" title="Model"></select>
      <select id="selEffort" onchange="chEffort(this.value)" title="Effort"></select>
      <select id="selLang2" onchange="chLang(this.value)" title="Language" style="width:52px">
        <option value="en">EN</option><option value="ja">JA</option>
        <option value="zh">ZH</option><option value="ko">KO</option>
      </select>
      <button class="tb-btn" onclick="openCmdPalette()" data-i18n-title="cmdPalette" title="Command Palette (Ctrl+K)">&#9000;</button>
      <button class="tb-btn" onclick="openTabModal()">&#9881;</button>
    </div>
    <div class="tabs" id="tabBar"><div class="tab-add" onclick="addTab()" title="Ctrl+T">+</div></div>
    <div class="cc" id="chatContainer"></div>
    <div class="ia">
      <div class="ac-popup" id="acPopup"></div>
      <div class="ir">
        <textarea id="msgIn" data-i18n-ph="ph" placeholder="Ask Claude anything..." rows="1" onkeydown="hKey(event)" oninput="aResize(this);updateAc()"></textarea>
        <button class="snd" id="sndBtn" onclick="sndClick()">&#9654;</button>
      </div>
      <div class="ia-nav">
        <button class="nav-btn" onclick="doClearDisplay()" title="Ctrl+L"><span class="nav-ico">&#x239A;</span> <span data-i18n="btnClear">Clear</span> <kbd>Ctrl+L</kbd></button>
        <button class="nav-btn nav-warn" onclick="doEndSession()" title="Ctrl+Shift+L"><span class="nav-ico">&#x23F9;</span> <span data-i18n="btnEndSess">End Session</span> <kbd>Ctrl+Shift+L</kbd></button>
        <button class="nav-btn nav-danger" onclick="doNewSession()" title="Ctrl+Shift+N"><span class="nav-ico">&#x21BB;</span> <span data-i18n="btnNewSess">New Session</span> <kbd>Ctrl+Shift+N</kbd></button>
        <span class="nav-sep"></span>
        <button class="nav-btn nav-toggle" id="btnPlan" onclick="togglePermMode('plan')" title="Plan (read-only)"><span class="nav-ico">&#x1F4D6;</span> Plan</button>
        <button class="nav-btn nav-toggle" id="btnAcceptEdits" onclick="togglePermMode('acceptEdits')" title="Accept Edits (file ops only)"><span class="nav-ico">&#x270F;</span> Accept Edits</button>
        <button class="nav-btn nav-toggle" id="btnAuto" onclick="togglePermMode('auto')" data-i18n-title="pm_auto" title="Auto"><span class="nav-ico">&#x26A1;</span> <span data-i18n="permAuto">Auto</span></button>
        <button class="nav-btn" onclick="openCmdPalette()" data-i18n-title="cmdPalette" title="Command Palette"><span class="nav-ico">&#9000;</span> <span data-i18n="cmdPalette">Commands</span> <kbd>Ctrl+K</kbd></button>
        <span class="nav-sep"></span>
        <span class="nav-keys"><kbd>Enter</kbd> <span data-i18n="keySend">Send</span></span>
        <span class="nav-keys"><kbd>Shift+Enter</kbd> <span data-i18n="keyNewline">Newline</span></span>
        <span class="nav-keys"><kbd>Esc</kbd> <span data-i18n="keyStop">Stop</span></span>
        <span class="nav-keys"><kbd>Ctrl+T</kbd> <span data-i18n="keyNewTab">New Tab</span></span>
      </div>
    </div>
  </div>
</div>

<script>
// ============================================================
// i18n
// ============================================================
const LANGS={
en:{
appSub:'Multi-tab AI Development',selProj:'Select project...',emptyProj:'Empty project',
treeHint:'Select a project to see the file tree',
refresh:'Refresh',clearDisp:'Clear',settings:'Settings',
ph:'Ask Claude anything...',
hint:'Enter: Send / Shift+Enter: Newline / Ctrl+T: New tab / Ctrl+L: Clear display / Ctrl+Shift+L: New session',
termHint:'Enter: Send/Confirm / Esc: Interrupt / \u2191\u2193: Select menu / Tab: Toggle / Ctrl+C: Cancel / Ctrl+L: Clear',
starting:'Starting...',cliOk:'CLI connected',apiMode:'API mode',cliErr:'CLI not found — check settings',
wcTitle:'Claude Code GUI',
wcDesc:'Desktop app with full Claude Code power. No terminal needed.<br>Select a project and ask anything.',
wcT1:'Structure',wcD1:'Explain project layout',wcT2:'Code',wcD2:'Find entry points',
wcT3:'Improve',wcD3:'Refactoring ideas',wcT4:'Docs',wcD4:'Generate README',
wcQ1:'Explain the structure of this project',wcQ2:'Show the main features and entry points',
wcQ3:'Give 3 improvement suggestions',wcQ4:'Write a README',
globalTitle:'Global Settings',tabTitle:'Tab Settings',
backendMode:'Backend Mode',
optCLI:'Claude Code CLI (no API key needed)',optAPI:'Anthropic API (API key required)',
cliNote:'CLI mode: claude command installed and logged in',
apiKey:'API Key',apiKeyHint:'Key is stored locally only',
cancel:'Cancel',save:'Save',close:'Close',
maxTurns:'Max Turns',maxTurnsNote:'0 = unlimited',
customFlags:'Custom CLI Flags',customFlagsNote:'Additional flags for CLI command (advanced)',
sysPrompt:'System Prompt',sysPromptPH:'Custom system prompt (API mode)',
sessionId:'Session ID',language:'Language',
dispCleared:'Display cleared',dispClearedDesc:'Session continues. Previous context is preserved.',
sessReset:'New session started',sessResetDesc:'Previous context has been cleared.',
btnClear:'Clear',btnEndSess:'End Session',btnNewSess:'New Session',keySend:'Send',keyNewline:'Newline',keyStop:'Stop',keyNewTab:'New Tab',
sessEnded:'Session ended',sessEndedDesc:'History preserved. Send a message to start a new session.',
sessInvalid:'Previous session expired, starting new session',
permMode:'Permission Mode',permDefault:'Default (all tools)',permAcceptEdits:'Accept Edits (file ops only)',permPlan:'Plan (read-only)',permCustom:'Selected tools only',
permNoteDefault:'Interactive — tools require approval in GUI (PTY mode)',permNoteAcceptEdits:'Read, Write, Edit, Glob, Grep auto-approved',permNotePlan:'Read-only — no file writes or Bash',permNoteCustom:'Only checked tools will be allowed',
permTitle:'Permission Required',permAllow:'Allow',permDeny:'Deny',permToolLabel:'Tool',
},
ja:{
appSub:'マルチタブ AI 開発',selProj:'プロジェクトを選択...',emptyProj:'空のプロジェクト',
treeHint:'プロジェクトを選択するとファイルツリーが表示されます',
refresh:'更新',clearDisp:'クリア',settings:'設定',
ph:'Claudeに聞いてみよう...',
hint:'Enter: 送信 / Shift+Enter: 改行 / Ctrl+T: 新規タブ / Ctrl+L: 表示クリア / Ctrl+Shift+L: セッション終了',
termHint:'Enter: 送信/確定 / Esc: 中断 / \u2191\u2193: 選択メニュー操作 / Tab: 切替 / Ctrl+C: キャンセル / Ctrl+L: クリア',
starting:'起動中...',cliOk:'CLI 接続済み',apiMode:'API モード',cliErr:'CLI 未検出 — 設定を確認',
wcTitle:'Claude Code GUI',
wcDesc:'ターミナル不要でClaude Codeの全機能を使えるデスクトップアプリ。<br>プロジェクトを選んで何でも聞いてください。',
wcT1:'構成を理解',wcD1:'プロジェクト構造を解説',wcT2:'コード理解',wcD2:'エントリポイントを特定',
wcT3:'改善提案',wcD3:'リファクタリング候補',wcT4:'ドキュメント',wcD4:'README を自動生成',
wcQ1:'このプロジェクトの構成を説明して',wcQ2:'主要な機能とエントリポイントを教えて',
wcQ3:'改善できるポイントを3つ挙げて',wcQ4:'READMEを書いて',
globalTitle:'グローバル設定',tabTitle:'タブ設定',
backendMode:'バックエンドモード',
optCLI:'Claude Code CLI（APIキー不要）',optAPI:'Anthropic API（APIキー必要）',
cliNote:'CLIモード: claude コマンドでログイン済みの場合',
apiKey:'API Key',apiKeyHint:'キーはローカルにのみ保存されます',
cancel:'キャンセル',save:'保存',close:'閉じる',
maxTurns:'Max Turns（最大ターン数）',maxTurnsNote:'0 = 無制限',
customFlags:'カスタムCLIフラグ',customFlagsNote:'CLIコマンドに追加するフラグ（上級者向け）',
sysPrompt:'システムプロンプト',sysPromptPH:'カスタムシステムプロンプト（APIモード用）',
sessionId:'Session ID',language:'言語',
dispCleared:'表示をクリアしました',dispClearedDesc:'セッションは継続中。以前のコンテキストは保持されています。',
sessReset:'新しいセッションを開始しました',sessResetDesc:'以前のコンテキストはクリアされました。',
btnClear:'クリア',btnEndSess:'セッション終了',btnNewSess:'新規セッション',keySend:'送信',keyNewline:'改行',keyStop:'停止',keyNewTab:'新規タブ',
sessEnded:'セッションを終了しました',sessEndedDesc:'履歴は保持されています。メッセージを送信すると新しいセッションが開始されます。',
sessInvalid:'前回のセッションが期限切れのため、新規セッションを開始します',
permMode:'権限モード',permDefault:'デフォルト（全ツール許可）',permAcceptEdits:'Accept Edits（ファイル操作のみ）',permPlan:'Plan（読み取り専用）',permCustom:'選択したツールのみ',
permNoteDefault:'インタラクティブ — ツール使用時にGUIで承認 (PTYモード)',permNoteAcceptEdits:'Read, Write, Edit, Glob, Grep を自動承認',permNotePlan:'読み取り専用 — 書き込み・Bash不可',permNoteCustom:'チェックしたツールのみ許可されます',
permTitle:'権限の確認',permAllow:'許可',permDeny:'拒否',permToolLabel:'ツール',
},
zh:{
appSub:'多标签 AI 开发',selProj:'选择项目...',emptyProj:'空项目',
treeHint:'选择项目后显示文件树',
refresh:'刷新',clearDisp:'清除',settings:'设置',
ph:'向 Claude 提问...',
hint:'Enter: 发送 / Shift+Enter: 换行 / Ctrl+T: 新标签 / Ctrl+L: 清除显示 / Ctrl+Shift+L: 新会话',
starting:'启动中...',cliOk:'CLI 已连接',apiMode:'API 模式',cliErr:'未找到 CLI — 检查设置',
wcTitle:'Claude Code GUI',
wcDesc:'无需终端即可使用 Claude Code 全部功能的桌面应用。<br>选择项目并提问。',
wcT1:'了解结构',wcD1:'解释项目布局',wcT2:'代码分析',wcD2:'定位入口点',
wcT3:'改进建议',wcD3:'重构方案',wcT4:'生成文档',wcD4:'自动生成 README',
wcQ1:'请解释这个项目的结构',wcQ2:'告诉我主要功能和入口点',
wcQ3:'给出3个改进建议',wcQ4:'写一个README',
globalTitle:'全局设置',tabTitle:'标签设置',
backendMode:'后端模式',
optCLI:'Claude Code CLI（无需 API 密钥）',optAPI:'Anthropic API（需要 API 密钥）',
cliNote:'CLI 模式：已安装并登录 claude 命令',
apiKey:'API 密钥',apiKeyHint:'密钥仅保存在本地',
cancel:'取消',save:'保存',close:'关闭',
maxTurns:'最大轮次',maxTurnsNote:'0 = 无限制',
customFlags:'自定义 CLI 标志',customFlagsNote:'添加到 CLI 的额外标志（高级）',
sysPrompt:'系统提示词',sysPromptPH:'自定义系统提示词（API 模式）',
sessionId:'会话 ID',language:'语言',
dispCleared:'显示已清除',dispClearedDesc:'会话继续中，之前的上下文已保留。',
sessReset:'已开始新会话',sessResetDesc:'之前的上下文已清除。',
btnClear:'清除',btnEndSess:'结束会话',btnNewSess:'新会话',keySend:'发送',keyNewline:'换行',keyStop:'停止',keyNewTab:'新标签',
sessEnded:'会话已结束',sessEndedDesc:'历史已保留。发送消息以开始新会话。',
sessInvalid:'上次会话已过期，正在开始新会话',
permMode:'权限模式',permDefault:'默认（所有工具）',permAcceptEdits:'Accept Edits（仅文件操作）',permPlan:'Plan（只读）',permCustom:'仅选定的工具',
permNoteDefault:'交互式 — ツール使用はGUIで承認 (PTYモード)',permNoteAcceptEdits:'自动批准 Read, Write, Edit, Glob, Grep',permNotePlan:'只读 — 禁止写入和 Bash',permNoteCustom:'仅允许勾选的工具',
permTitle:'需要权限',permAllow:'允许',permDeny:'拒绝',permToolLabel:'工具',
},
ko:{
appSub:'멀티탭 AI 개발',selProj:'프로젝트 선택...',emptyProj:'빈 프로젝트',
treeHint:'프로젝트를 선택하면 파일 트리가 표시됩니다',
refresh:'새로고침',clearDisp:'지우기',settings:'설정',
ph:'Claude에게 물어보세요...',
hint:'Enter: 전송 / Shift+Enter: 줄바꿈 / Ctrl+T: 새 탭 / Ctrl+L: 표시 지우기 / Ctrl+Shift+L: 새 세션',
starting:'시작 중...',cliOk:'CLI 연결됨',apiMode:'API 모드',cliErr:'CLI 미발견 — 설정 확인',
wcTitle:'Claude Code GUI',
wcDesc:'터미널 없이 Claude Code의 모든 기능을 사용할 수 있는 데스크톱 앱.<br>프로젝트를 선택하고 무엇이든 물어보세요.',
wcT1:'구조 파악',wcD1:'프로젝트 구조 설명',wcT2:'코드 분석',wcD2:'진입점 파악',
wcT3:'개선 제안',wcD3:'리팩토링 후보',wcT4:'문서 생성',wcD4:'README 자동 생성',
wcQ1:'이 프로젝트의 구조를 설명해줘',wcQ2:'주요 기능과 진입점을 알려줘',
wcQ3:'개선할 점 3가지를 알려줘',wcQ4:'README를 작성해줘',
globalTitle:'전역 설정',tabTitle:'탭 설정',
backendMode:'백엔드 모드',
optCLI:'Claude Code CLI (API 키 불필요)',optAPI:'Anthropic API (API 키 필요)',
cliNote:'CLI 모드: claude 명령어 설치 및 로그인 완료 시',
apiKey:'API 키',apiKeyHint:'키는 로컬에만 저장됩니다',
cancel:'취소',save:'저장',close:'닫기',
maxTurns:'최대 턴 수',maxTurnsNote:'0 = 무제한',
customFlags:'사용자 정의 CLI 플래그',customFlagsNote:'CLI에 추가할 플래그 (고급)',
sysPrompt:'시스템 프롬프트',sysPromptPH:'커스텀 시스템 프롬프트 (API 모드)',
sessionId:'세션 ID',language:'언어',
dispCleared:'표시가 지워졌습니다',dispClearedDesc:'세션은 계속됩니다. 이전 컨텍스트가 유지됩니다.',
sessReset:'새 세션이 시작되었습니다',sessResetDesc:'이전 컨텍스트가 삭제되었습니다.',
btnClear:'지우기',btnEndSess:'세션 종료',btnNewSess:'새 세션',keySend:'전송',keyNewline:'줄바꿈',keyStop:'중지',keyNewTab:'새 탭',
sessEnded:'세션이 종료되었습니다',sessEndedDesc:'기록이 보존됩니다. 메시지를 보내면 새 세션이 시작됩니다.',
sessInvalid:'이전 세션이 만료되어 새 세션을 시작합니다',
permMode:'권한 모드',permDefault:'기본 (모든 도구)',permAcceptEdits:'Accept Edits (파일 작업만)',permPlan:'Plan (읽기 전용)',permCustom:'선택한 도구만',
permNoteDefault:'인터랙티브 — 도구 사용 시 GUI에서 승인 (PTY 모드)',permNoteAcceptEdits:'Read, Write, Edit, Glob, Grep 자동 승인',permNotePlan:'읽기 전용 — 쓰기 및 Bash 불가',permNoteCustom:'체크한 도구만 허용됩니다',
permTitle:'권한 요청',permAllow:'허용',permDeny:'거부',permToolLabel:'도구',
}
};

let curLang='en';
// Merge the Python-injected catalogue into LANGS so the keys defined here and
// the ones in _STRINGS_EXTRA live in one table. Python injects window.I18N as
// { key: { en, ja, zh, ko } }; LANGS is { lang: { key: str } }, so we flip it.
(function mergeI18n(){
  try{
    if(!window.I18N)return;
    const langs=['en','ja','zh','ko'];
    Object.keys(window.I18N).forEach(k=>{
      langs.forEach(L=>{
        if(!LANGS[L])LANGS[L]={};
        // Keys already defined above win.
        if(LANGS[L][k]===undefined){
          const v=window.I18N[k]&&window.I18N[k][L];
          if(v!==undefined)LANGS[L][k]=v;
        }
      });
    });
  }catch(e){console.warn('i18n merge failed',e);}
})();
function t(k){return (LANGS[curLang]&&LANGS[curLang][k])||LANGS.en[k]||k;}
function detectLang(){const n=(navigator.language||'').toLowerCase();if(n.startsWith('ja'))return'ja';if(n.startsWith('zh'))return'zh';if(n.startsWith('ko'))return'ko';return'en';}
function updateUI(){
  document.querySelectorAll('[data-i18n]').forEach(el=>{const v=t(el.dataset.i18n);if(v)el.textContent=v;});
  document.querySelectorAll('[data-i18n-ph]').forEach(el=>{const v=t(el.dataset.i18nPh);if(v)el.placeholder=v;});
  // Title attr: data-i18n-title="key" sets the tooltip.
  document.querySelectorAll('[data-i18n-title]').forEach(el=>{const v=t(el.dataset.i18nTitle);if(v)el.title=v;});
  // <optgroup label>: data-i18n-label="key" sets the group heading.
  document.querySelectorAll('[data-i18n-label]').forEach(el=>{const v=t(el.dataset.i18nLabel);if(v)el.label=v;});
}
async function chLang(v){
  curLang=v;
  updateUI();
  document.getElementById('langSelect').value=v;
  document.getElementById('selLang2').value=v;
  // Update dynamic per-tab welcome screens
  order.forEach(id=>{
    const wc=document.getElementById('wc_'+id);
    if(wc&&wc.style.display!=='none') rebuildWelcome(id);
  });
  await pywebview.api.set_language(v);
}

// ============================================================
// State
// ============================================================
const T={};let order=[];let act=null;let gHasPty=false;let gMode='cli';
let _tsCurId='';            // tab id currently shown in the tab-settings modal
// Specs shipped from Python (single source of truth - see LAUNCH_OPTIONS,
// SESSION_TOGGLES, TOOL_GROUPS, PERMISSION_MODES in main.py).
let LAUNCH_SPEC=[];let TOGGLE_SPEC=[];let TOOL_GROUPS=[];let PERM_MODES=[];
let gCliVersion='';let gUpdatePhase='idle';

// ============================================================
// Init
// ============================================================
async function init(){
  const s=await pywebview.api.get_initial_state();
  // Populate selectors. Models are grouped: pinned versions first, then the
  // aliases the CLI resolves itself (default / best / opusplan / *[1m]).
  const ms=document.getElementById('selModel');
  const MG_LABEL={pinned:'mgPinned',aliases:'mgAliases'};
  const mgroups=s.model_groups&&s.model_groups.length?s.model_groups:[{id:'pinned',models:s.models||[]}];
  mgroups.forEach(g=>{
    const og=document.createElement('optgroup');
    og.setAttribute('data-i18n-label',MG_LABEL[g.id]||g.id);
    og.label=t(MG_LABEL[g.id]||g.id);
    (g.models||[]).forEach(m=>{const o=document.createElement('option');o.value=m.id;o.textContent=m.label;og.appendChild(o);});
    ms.appendChild(og);
  });
  const es=document.getElementById('selEffort');
  s.efforts.forEach(e=>{const o=document.createElement('option');o.value=e.id;o.textContent=e.label;es.appendChild(o);});
  // Permission modes (exact --permission-mode values + the GUI-only "custom")
  const pmSel=document.getElementById('tsPermMode');
  PERM_MODES=s.permission_modes||[];
  PERM_MODES.forEach(m=>{
    const o=document.createElement('option');o.value=m.id;
    o.setAttribute('data-i18n','pm_'+m.id);o.textContent=t('pm_'+m.id)||m.label;
    pmSel.appendChild(o);
  });
  // Permission tool checkboxes, grouped by what the tools actually do so a
  // 45-entry list stays scannable.
  TOOL_GROUPS=s.tool_groups||[{id:'file',tools:s.cli_tools||[]}];
  const TG_LABEL={file:'tgFile',exec:'tgExec',web:'tgWeb',agents:'tgAgents',tasks:'tgTasks',plan:'tgPlan',sched:'tgSched',mcp:'tgMcp',io:'tgIo'};
  const pg=document.getElementById('permToolGrid');
  TOOL_GROUPS.forEach(g=>{
    const hd=document.createElement('div');
    hd.style.cssText='grid-column:1/-1;font-size:10px;color:var(--acl);margin-top:6px;font-weight:600';
    hd.setAttribute('data-i18n',TG_LABEL[g.id]||g.id);hd.textContent=t(TG_LABEL[g.id]||g.id);
    pg.appendChild(hd);
    (g.tools||[]).forEach(tl=>{
      const lb=document.createElement('label');lb.style.cssText='display:flex;align-items:center;gap:5px;font-size:12px;cursor:pointer';
      const cb=document.createElement('input');cb.type='checkbox';cb.value=tl;cb.className='perm-cb';
      lb.appendChild(cb);lb.appendChild(document.createTextNode(tl));pg.appendChild(lb);
    });
  });
  // Launch Options / Session Toggles panels (rendered from the Python spec)
  LAUNCH_SPEC=s.launch_options||[];
  TOGGLE_SPEC=s.session_toggles||[];
  buildLaunchPanel();
  // CLI version + auto-update. Fire-and-forget: the window must never block
  // on the network, and running sessions keep the exe they already opened.
  gCliVersion=s.cli_version||'';
  document.getElementById('chkAutoUpdate').checked=s.auto_update!==false;
  renderCliVersion();
  try{pywebview.api.start_auto_update();}catch(e){}
  // Global state
  gHasPty=!!s.has_pty;
  gMode=s.mode||'cli';
  // Language
  curLang=s.language||detectLang();
  document.getElementById('langSelect').value=curLang;
  document.getElementById('selLang2').value=curLang;
  updateUI();
  // Status
  if(s.cli_available){setStatus('ok',t('cliOk'));}
  else if(s.api_key_set){setStatus('warn',t('apiMode'));}
  else{setStatus('err',t('cliErr'));}
  // Save detected lang if new
  if(!s.language) pywebview.api.set_language(curLang);
  // Restore tabs
  s.tabs.forEach(ti=>createTabDOM(ti));
  if(s.active_tab&&T[s.active_tab])switchTab(s.active_tab);
  else if(order.length)switchTab(order[0]);
  document.getElementById('msgIn').focus();
  // Auto-start PTY session for active tab (resume or new)
  if(s.cli_available&&s.mode==='cli'&&s.has_pty&&act){
    pywebview.api.auto_start_session(act);
  }
  // Tell Python the frontend finished wiring itself up, with a census of what
  // it actually rendered. Shows up in the console log, so a panel that
  // silently failed to build is visible instead of just looking empty.
  try{
    pywebview.api.report_ready({
      tabs:order.length,
      models:(s.models||[]).length,
      commands:CMD_CATALOG.length,
      launchOptions:LAUNCH_SPEC.length,
      toggles:TOGGLE_SPEC.length,
      tools:(s.cli_tools||[]).length,
      permModes:PERM_MODES.length,
    });
  }catch(e){}
}

function setStatus(cls,txt){document.getElementById('stDot').className='dot '+cls;document.getElementById('stText').textContent=txt;}

// ============================================================
// Tab DOM
// ============================================================
function createTabDOM(info){
  const id=info.id;
  T[id]={name:info.name,model:info.model||'claude-opus-5-5',effort:info.effort||'max',
    projectPath:info.project_path||'',projectName:info.project_name||'',
    maxTurns:info.max_turns||0,customFlags:info.custom_flags||'',
    sysPrompt:info.system_prompt||'',sessionId:info.session_id||'',
    permMode:info.permission_mode||'default',allowedTools:info.allowed_tools||[],
    launch:info.launch||{},toggles:info.toggles||{},
    remoteControl:info.remote_control!==false,
    streaming:false,bubble:null,content:'',bubbleText:'',termActive:false,permPrompt:false};
  if(!order.includes(id))order.push(id);
  const bar=document.getElementById('tabBar');
  const addBtn=bar.querySelector('.tab-add');
  const tab=document.createElement('div');
  tab.className='tab';tab.dataset.tab=id;
  tab.innerHTML='<span class="nd"></span><span class="tn" ondblclick="startRename(\''+id+'\')">'+esc(info.name)+'</span><span class="tc" onclick="event.stopPropagation();closeTab(\''+id+'\')">&#x2715;</span>';
  tab.onclick=()=>switchTab(id);
  bar.insertBefore(tab,addBtn);
  const pane=document.createElement('div');
  pane.className='chat';pane.id='chat_'+id;
  buildWelcome(pane,id);
  document.getElementById('chatContainer').appendChild(pane);
  // Terminal pane (CLI mode — shows full Claude Code TUI)
  const termPane=document.createElement('div');
  termPane.className='term';termPane.id='term_'+id;
  termPane.innerHTML='<pre class="term-screen" id="screen_'+id+'"></pre><div class="term-perm" id="tperm_'+id+'"></div>';
  document.getElementById('chatContainer').appendChild(termPane);
  if(info.screen_content){T[id].termActive=true;const sc=document.getElementById('screen_'+id);if(sc)sc.textContent=info.screen_content;}
  if(info.messages&&info.messages.length){
    const wc=document.getElementById('wc_'+id);if(wc)wc.style.display='none';
    info.messages.forEach(m=>addMsg(id,m.role,m.content,false));
  }
}

function buildWelcome(pane,id){
  const wc=document.createElement('div');wc.className='wc';wc.id='wc_'+id;
  wc.innerHTML='<h2>'+t('wcTitle')+'</h2><p>'+t('wcDesc')+'</p>'
    +'<div class="tips">'
    +'<div class="tip" onclick="qSend(t(\'wcQ1\'))"><div class="emoji">&#128193;</div><div class="tt">'+t('wcT1')+'</div><div class="td">'+t('wcD1')+'</div></div>'
    +'<div class="tip" onclick="qSend(t(\'wcQ2\'))"><div class="emoji">&#128269;</div><div class="tt">'+t('wcT2')+'</div><div class="td">'+t('wcD2')+'</div></div>'
    +'<div class="tip" onclick="qSend(t(\'wcQ3\'))"><div class="emoji">&#128161;</div><div class="tt">'+t('wcT3')+'</div><div class="td">'+t('wcD3')+'</div></div>'
    +'<div class="tip" onclick="qSend(t(\'wcQ4\'))"><div class="emoji">&#128221;</div><div class="tt">'+t('wcT4')+'</div><div class="td">'+t('wcD4')+'</div></div>'
    +'</div>';
  pane.appendChild(wc);
}

function rebuildWelcome(id){
  const pane=document.getElementById('chat_'+id);
  const old=document.getElementById('wc_'+id);
  if(old)old.remove();
  buildWelcome(pane,id);
}

function switchTab(id){
  if(!T[id])return;
  if(act){
    const ot=document.querySelector('[data-tab="'+act+'"]');if(ot)ot.classList.remove('act');
    const op=document.getElementById('chat_'+act);if(op)op.classList.remove('vis');
    const oterm=document.getElementById('term_'+act);if(oterm)oterm.classList.remove('vis');
  }
  act=id;
  const tab=document.querySelector('[data-tab="'+id+'"]');
  if(tab){tab.classList.add('act');tab.classList.remove('notif');tab.classList.remove('perm-notif');}
  // Show terminal or chat based on whether PTY is active
  if(T[id].termActive){
    const tp=document.getElementById('term_'+id);if(tp){tp.classList.add('vis');const sc=tp.querySelector('.term-screen');if(sc)sc.scrollTop=sc.scrollHeight;}
  }else{
    const pane=document.getElementById('chat_'+id);
    if(pane){pane.classList.add('vis');pane.scrollTop=pane.scrollHeight;}
  }
  updateSidebar();
  const ti=T[id];
  document.getElementById('selModel').value=ti.model;
  document.getElementById('selEffort').value=ti.effort;
  document.getElementById('topProj').textContent=ti.projectName||'';
  setSndBtn(ti.streaming);
  updateHint();
  updatePermButtons();
  document.getElementById('msgIn').focus();
}

async function addTab(){const info=await pywebview.api.create_tab('');createTabDOM(info);switchTab(info.id);}
async function closeTab(id){
  if(order.length<=1)return;
  const r=await pywebview.api.close_tab(id);
  const te=document.querySelector('[data-tab="'+id+'"]');if(te)te.remove();
  const pn=document.getElementById('chat_'+id);if(pn)pn.remove();
  const tn=document.getElementById('term_'+id);if(tn)tn.remove();
  delete T[id];order=order.filter(x=>x!==id);
  if(act===id)switchTab(r.active_tab);
}
function closeCurrentTab(){if(act)closeTab(act);}
function cycleTab(dir){if(order.length<2)return;const i=order.indexOf(act);switchTab(order[(i+dir+order.length)%order.length]);}
function startRename(id){
  const tab=document.querySelector('[data-tab="'+id+'"]');const sp=tab.querySelector('.tn');
  const inp=document.createElement('input');inp.className='tab-rename';inp.value=T[id].name;
  inp.onblur=()=>endRename(id,inp);
  inp.onkeydown=(e)=>{if(e.key==='Enter')inp.blur();if(e.key==='Escape'){inp.value=T[id].name;inp.blur();}};
  sp.replaceWith(inp);inp.focus();inp.select();
}
function endRename(id,inp){
  const name=inp.value.trim()||T[id].name;T[id].name=name;
  pywebview.api.rename_tab(id,name);
  const sp=document.createElement('span');sp.className='tn';sp.textContent=name;
  sp.ondblclick=()=>startRename(id);inp.replaceWith(sp);
}

// ============================================================
// Sidebar
// ============================================================
function updateSidebar(){
  const ti=T[act];const btn=document.getElementById('projBtn');
  if(ti&&ti.projectPath){
    btn.className='proj-btn active';
    document.getElementById('projLabel').textContent=ti.projectName;
    document.getElementById('projLabel').removeAttribute('data-i18n');
    document.getElementById('projPath').textContent=ti.projectPath;
    loadTree();
  }else{
    btn.className='proj-btn';
    document.getElementById('projLabel').textContent=t('selProj');
    document.getElementById('projLabel').setAttribute('data-i18n','selProj');
    document.getElementById('projPath').textContent='';
    document.getElementById('fileTree').innerHTML='<div style="padding:14px;color:var(--txd);font-size:11px;text-align:center">'+t('treeHint')+'</div>';
  }
}
async function selectProject(){
  if(!act)return;
  const r=await pywebview.api.select_project(act);
  if(r){T[act].projectPath=r.path;T[act].projectName=r.name;T[act].name=r.name;
    const sp=document.querySelector('[data-tab="'+act+'"] .tn');if(sp)sp.textContent=r.name;
    document.getElementById('topProj').textContent=r.name;updateSidebar();}
}
async function loadTree(){
  if(!act)return;const tree=await pywebview.api.get_file_tree(act);
  const el=document.getElementById('fileTree');
  if(!tree||!tree.length){el.innerHTML='<div style="padding:14px;color:var(--txd);font-size:11px;text-align:center">'+t('emptyProj')+'</div>';return;}
  el.innerHTML=renderTree(tree,0);
}
async function refreshTree(){await loadTree();}
function renderTree(items,depth){
  return items.map(item=>{const pad=depth*14;
    if(item.type==='dir'){const did='d_'+btoa(unescape(encodeURIComponent(item.path))).replace(/[^a-zA-Z0-9]/g,'');
      return '<div class="ti" style="padding-left:'+(14+pad)+'px" onclick="togDir(\''+did+'\',this)"><span class="ti-icon">&#9658;</span><span class="ti-name">'+esc(item.name)+'/</span></div><div id="'+did+'" style="display:none">'+renderTree(item.children||[],depth+1)+'</div>';}
    const sp=item.path.replace(/\\/g,'\\\\').replace(/'/g,"\\'");
    return '<div class="ti" style="padding-left:'+(14+pad)+'px" onclick="openFile(\''+sp+'\')"><span class="ti-icon">'+fIco(item.name)+'</span><span class="ti-name">'+esc(item.name)+'</span></div>';
  }).join('');
}
function togDir(id,el){const c=document.getElementById(id);if(c){const o=c.style.display!=='none';c.style.display=o?'none':'block';el.querySelector('.ti-icon').innerHTML=o?'&#9658;':'&#9660;';}}
async function openFile(p){const r=await pywebview.api.read_file(p);if(r.ok){document.getElementById('vwName').textContent=r.name;document.getElementById('vwContent').textContent=r.content;document.getElementById('fileViewer').classList.add('open');}}
function closeVw(){document.getElementById('fileViewer').classList.remove('open');}
function fIco(n){const e=n.split('.').pop().toLowerCase();const m={py:'\u{1F40D}',js:'\u26A1',ts:'\u26A1',html:'\u{1F310}',css:'\u{1F3A8}',json:'\u{1F4C4}',md:'\u{1F4DD}',yml:'\u2699',yaml:'\u2699',toml:'\u2699',txt:'\u{1F4C4}',sql:'\u{1F5C3}',sh:'\u{1F4E6}'};return m[e]||'\u{1F4C4}';}

// ============================================================
// Chat
// ============================================================
function hKey(e){
  // Autocomplete navigation
  const acVis=document.getElementById('acPopup').classList.contains('vis');
  if(acVis){
    if(e.key==='ArrowUp'){e.preventDefault();navAc(-1);return;}
    if(e.key==='ArrowDown'){e.preventDefault();navAc(1);return;}
    if(e.key==='Tab'){e.preventDefault();selectAc(acIdx>=0?acIdx:0);return;}
    if(e.key==='Enter'&&acIdx>=0){e.preventDefault();selectAc(acIdx);return;}
    if(e.key==='Escape'){e.preventDefault();document.getElementById('acPopup').classList.remove('vis');acItems=[];return;}
  }
  // Terminal mode: forward control keys to PTY
  if(act&&T[act]&&T[act].termActive){
    const empty=!document.getElementById('msgIn').value;
    // Always forward to PTY (regardless of input content)
    if(e.key==='Escape'){e.preventDefault();pywebview.api.pty_input(act,'\x1b');return;}
    if(e.key==='Tab'&&!e.shiftKey){e.preventDefault();pywebview.api.pty_input(act,'\t');return;}
    if(e.key==='Tab'&&e.shiftKey){e.preventDefault();pywebview.api.pty_input(act,'\x1b[Z');return;}
    if(e.ctrlKey&&e.key==='c'){const sel=window.getSelection();const ta=document.getElementById('msgIn');if((sel&&sel.toString())||(ta&&ta.selectionStart!==ta.selectionEnd)){return;}e.preventDefault();pywebview.api.pty_input(act,'\x03');return;}
    if(e.ctrlKey&&e.key==='d'){e.preventDefault();pywebview.api.pty_input(act,'\x04');return;}
    if(e.ctrlKey&&e.key==='z'){e.preventDefault();pywebview.api.pty_input(act,'\x1a');return;}
    if(e.ctrlKey&&e.key==='r'){e.preventDefault();pywebview.api.pty_input(act,'\x12');return;}
    if(e.ctrlKey&&e.key==='l'){e.preventDefault();pywebview.api.pty_input(act,'\x0c');return;}
    // Only forward when input is empty (otherwise normal text editing)
    if(empty){
      if(e.key==='ArrowUp'){e.preventDefault();pywebview.api.pty_input(act,'\x1b[A');return;}
      if(e.key==='ArrowDown'){e.preventDefault();pywebview.api.pty_input(act,'\x1b[B');return;}
      if(e.key==='ArrowLeft'){e.preventDefault();pywebview.api.pty_input(act,'\x1b[D');return;}
      if(e.key==='ArrowRight'){e.preventDefault();pywebview.api.pty_input(act,'\x1b[C');return;}
      if(e.key==='Backspace'){e.preventDefault();pywebview.api.pty_input(act,'\x7f');return;}
      if(e.key==='Delete'){e.preventDefault();pywebview.api.pty_input(act,'\x1b[3~');return;}
      if(e.key==='Home'){e.preventDefault();pywebview.api.pty_input(act,'\x1b[H');return;}
      if(e.key==='End'){e.preventDefault();pywebview.api.pty_input(act,'\x1b[F');return;}
    }
  }
  if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();doSend();}
  if(e.key==='l'&&e.ctrlKey&&!e.shiftKey){e.preventDefault();doClearDisplay();return;}
  if(e.key==='L'&&e.ctrlKey&&e.shiftKey){e.preventDefault();doEndSession();return;}
  if(e.key==='N'&&e.ctrlKey&&e.shiftKey){e.preventDefault();doNewSession();return;}
}
function aResize(el){el.style.height='auto';el.style.height=Math.min(el.scrollHeight,180)+'px';}
function qSend(text){document.getElementById('msgIn').value=text;doSend();}

function doSend(){
  document.getElementById('acPopup').classList.remove('vis');acItems=[];
  if(!act)return;const ti=T[act];
  const inp=document.getElementById('msgIn');const text=inp.value.trim();
  // Terminal mode: send even if empty (Enter = confirm selection)
  if(ti.termActive||(gMode==='cli'&&gHasPty)){
    inp.value='';inp.style.height='auto';
    if(!text){pywebview.api.pty_input(act,'\r');return;}
    const wc=document.getElementById('wc_'+act);if(wc)wc.style.display='none';
    if(ti.termActive){
      // PTY running — send directly as terminal input (supports TUI text inputs like AskUserQuestion "Other")
      pywebview.api.pty_input(act,text+'\r');
    }else{
      // PTY not yet started — send_message will initialize PTY
      pywebview.api.send_message(act,text);
    }
    return;
  }
  if(!text)return;
  // API or fallback (-p) mode: chat bubble flow
  if(ti.streaming){
    pywebview.api.stop_streaming(act);
    setTimeout(()=>{
      const wc=document.getElementById('wc_'+act);if(wc)wc.style.display='none';
      addMsg(act,'user',text,true);scrollBot(act);
      pywebview.api.send_message(act,text);
    },500);
    return;
  }
  const wc=document.getElementById('wc_'+act);if(wc)wc.style.display='none';
  addMsg(act,'user',text,true);scrollBot(act);
  pywebview.api.send_message(act,text);
}
function sndClick(){
  if(act&&T[act]&&T[act].streaming){pywebview.api.stop_streaming(act);return;}
  if(act&&T[act]&&T[act].termActive){doSend();return;}
  doSend();
}

function addMsg(tabId,role,content,anim){
  const pane=document.getElementById('chat_'+tabId);if(!pane)return;
  const div=document.createElement('div');div.className='msg '+role;
  if(!anim)div.style.animation='none';
  const bub=document.createElement('div');bub.className='bub';
  bub.innerHTML=role==='assistant'?mdRender(content):esc(content).replace(/\n/g,'<br>');
  div.appendChild(bub);
  const ts=document.createElement('div');ts.className='ts';
  ts.textContent=new Date().toLocaleTimeString('ja-JP',{hour:'2-digit',minute:'2-digit'});
  div.appendChild(ts);pane.appendChild(div);return bub;
}

function setSndBtn(streaming){
  const btn=document.getElementById('sndBtn');
  if(streaming){btn.innerHTML='&#9632;';btn.className='snd stop';}
  else{btn.innerHTML='&#9654;';btn.className='snd';}
}

// ============================================================
// Clear display vs New session
// ============================================================
async function doClearDisplay(){
  if(!act)return;
  if(T[act]&&T[act].termActive){
    pywebview.api.pty_input(act,'\x0c');
    return;
  }
  await pywebview.api.clear_display(act);
}
async function doEndSession(){if(act)await pywebview.api.end_session(act);}
async function doNewSession(){if(act)await pywebview.api.new_session(act);}

function onSessionEnded(tid){
  if(T[tid]){T[tid].termActive=false;T[tid].permPrompt=false;}
  const term=document.getElementById('term_'+tid);
  if(term){term.classList.remove('vis');const p=term.querySelector('.term-perm');if(p)p.classList.remove('vis');}
  const pane=document.getElementById('chat_'+tid);
  if(pane){
    pane.classList.add('vis');
    const info=document.createElement('div');info.className='sys-info';
    info.textContent=t('sessEnded')+' — '+t('sessEndedDesc');
    pane.appendChild(info);scrollBot(tid);
  }
  if(tid===act)setSndBtn(false);
}

function onDisplayCleared(tid){
  const pane=document.getElementById('chat_'+tid);if(!pane)return;
  pane.innerHTML='';
  const info=document.createElement('div');info.className='sys-info';
  info.textContent=t('dispCleared')+' — '+t('dispClearedDesc');
  pane.appendChild(info);
}

function onSessionReset(tid){
  const pane=document.getElementById('chat_'+tid);if(!pane)return;
  pane.innerHTML='';
  if(T[tid]){T[tid].sessionId='';T[tid].termActive=false;T[tid].permPrompt=false;}
  // Hide terminal, clear screen, show chat
  const term=document.getElementById('term_'+tid);
  if(term){term.classList.remove('vis');const p=term.querySelector('.term-perm');if(p)p.classList.remove('vis');}
  const sc=document.getElementById('screen_'+tid);if(sc)sc.textContent='';
  pane.classList.add('vis');
  updateHint();
  buildWelcome(pane,tid);
  const info=document.createElement('div');info.className='sys-info';
  info.textContent=t('sessReset')+' — '+t('sessResetDesc');
  pane.insertBefore(info,pane.firstChild);
}

// ============================================================
// Terminal mode (PTY) callbacks
// ============================================================
function showTerminal(tid){
  const ti=T[tid];if(!ti)return;
  ti.termActive=true;
  _ptyStartTimes[tid]=Date.now();
  const wc=document.getElementById('wc_'+tid);if(wc)wc.style.display='none';
  const chat=document.getElementById('chat_'+tid);if(chat)chat.classList.remove('vis');
  const term=document.getElementById('term_'+tid);if(term)term.classList.add('vis');
  const sc=document.getElementById('screen_'+tid);if(sc)sc.scrollTop=sc.scrollHeight;
  updateHint();
}
function onScreenUpdate(tid,text){
  const el=document.getElementById('screen_'+tid);
  if(!el)return;
  const wasNearBottom=el.scrollHeight-el.scrollTop-el.clientHeight<50;
  const prevScroll=el.scrollTop;
  el.textContent=text;
  if(wasNearBottom)el.scrollTop=el.scrollHeight;
  else el.scrollTop=prevScroll;
  const ti=T[tid];
  if(ti&&!ti.termActive)showTerminal(tid);
}
function onPermState(tid,isPerm,choices){
  const ti=T[tid];if(!ti)return;
  ti.permPrompt=isPerm;
  const p=document.getElementById('tperm_'+tid);
  if(p){
    if(isPerm&&choices&&choices.length){
      let h='<span class="perm-lbl">Permission:</span>';
      choices.forEach(function(c,i){
        const num=c[0],label=c[1];
        const cls=/deny|no$/i.test(label)?'d':(/session|always/i.test(label)?'s':'a');
        h+='<button class="perm-btn '+cls+'" onclick="permSend('+i+')">'+num+'. '+esc(label)+'</button>';
      });
      p.innerHTML=h;p.classList.add('vis');
    }else if(isPerm){
      p.innerHTML='<span class="perm-lbl">Permission:</span><button class="perm-btn a" onclick="permSend(0)">Allow</button><button class="perm-btn d" onclick="permSend(1)">Deny</button>';
      p.classList.add('vis');
    }else{p.classList.remove('vis');}
  }
  const te=document.querySelector('[data-tab="'+tid+'"]');
  if(te){if(isPerm&&tid!==act)te.classList.add('perm-notif');else te.classList.remove('perm-notif');}
}
function permSend(idx){
  if(!act)return;
  const seq='\x1b[B'.repeat(idx)+'\r';
  pywebview.api.pty_input(act,seq);
  const p=document.getElementById('tperm_'+act);if(p)p.classList.remove('vis');
  if(T[act])T[act].permPrompt=false;
}
let _ptyStartTimes={};
function onPtyStarted(tid){_ptyStartTimes[tid]=Date.now();}
function onPtyDied(tid){
  const ti=T[tid];if(!ti)return;
  ti.streaming=false;ti.permPrompt=false;
  const p=document.getElementById('tperm_'+tid);if(p)p.classList.remove('vis');
  if(tid===act)setSndBtn(false);
  const el=document.getElementById('screen_'+tid);
  const startedAt=_ptyStartTimes[tid]||0;
  const elapsed=Date.now()-startedAt;
  // If PTY died within 10s, it likely crashed or had invalid session — auto-restart as new
  if(elapsed<10000&&startedAt>0){
    if(el)el.textContent+='\n\n--- '+t('sessInvalid')+' ---\n';
    ti.termActive=false;ti.sessionId='';
    pywebview.api.new_session(tid);
    setTimeout(()=>{pywebview.api.auto_start_session(tid);},1500);
  }else{
    // Normal exit — session ended, next message will start new PTY (with --resume if session_id exists)
    if(el)el.textContent+='\n\n--- '+t('sessEnded')+' ---\n';
    ti.termActive=false;
  }
}

// ============================================================
// Streaming callbacks (API / stream-json mode)
// ============================================================
const TOOL_ICO={Bash:'\u{1F4BB}',Read:'\u{1F4C4}',Write:'\u270F\uFE0F',Edit:'\u2702\uFE0F',Glob:'\u{1F50D}',Grep:'\u{1F50E}',WebFetch:'\u{1F310}',WebSearch:'\u{1F50D}',NotebookEdit:'\u{1F4D3}'};
function tIco(n){return TOOL_ICO[n]||'\u{1F527}';}

function _newBubble(tid){
  const ti=T[tid];const pane=document.getElementById('chat_'+tid);if(!pane||!ti)return;
  const div=document.createElement('div');div.className='msg assistant';
  const bub=document.createElement('div');bub.className='bub';
  bub.innerHTML='<div class="typing"><span></span><span></span><span></span></div>';
  div.appendChild(bub);pane.appendChild(div);
  ti.bubble=bub;ti.bubbleText='';
}

function onStreamStart(tid){
  const ti=T[tid];if(!ti)return;ti.streaming=true;ti.content='';ti.bubbleText='';
  _newBubble(tid);
  const tabEl=document.querySelector('[data-tab="'+tid+'"]');if(tabEl)tabEl.classList.add('strm');
  if(tid===act){setSndBtn(true);scrollBot(tid);}
}

function onStreamToken(tid,tok){
  const ti=T[tid];if(!ti)return;
  ti.content+=tok;ti.bubbleText+=tok;
  if(ti.bubble)ti.bubble.innerHTML=mdRender(ti.bubbleText);
  if(tid===act)scrollBot(tid);
  else{const te=document.querySelector('[data-tab="'+tid+'"]');if(te)te.classList.add('notif');}
}

function onToolStart(tid,name,inp){
  const ti=T[tid];const pane=document.getElementById('chat_'+tid);if(!pane||!ti)return;
  // Finalize current bubble if it has text
  if(ti.bubble&&ti.bubbleText){ti.bubble.innerHTML=mdRender(ti.bubbleText);}
  else if(ti.bubble&&!ti.bubbleText){ti.bubble.parentElement.remove();}
  ti.bubble=null;
  // Tool card
  const card=document.createElement('div');card.className='tc';card.id='tc_'+tid+'_'+Date.now();
  card.innerHTML='<div class="tc-head">'+tIco(name)+' <strong>'+esc(name)+'</strong> <span style="font-weight:400;opacity:.6;font-size:10px">running...</span></div>'
    +(inp?'<pre class="tc-inp">'+esc(inp)+'</pre>':'');
  pane.appendChild(card);
  // New bubble for subsequent text
  _newBubble(tid);
  scrollBot(tid);
}

function onToolUpdate(tid,name,inp){
  const pane=document.getElementById('chat_'+tid);if(!pane)return;
  const cards=pane.querySelectorAll('.tc');
  for(let i=cards.length-1;i>=0;i--){
    const hd=cards[i].querySelector('.tc-head strong');
    if(hd&&hd.textContent===name){
      let el=cards[i].querySelector('.tc-inp');
      if(inp){
        if(!el){el=document.createElement('pre');el.className='tc-inp';cards[i].appendChild(el);}
        el.textContent=inp;
      }
      break;
    }
  }
}

function onToolResult(tid,name,output,isErr){
  const pane=document.getElementById('chat_'+tid);if(!pane)return;
  const card=document.createElement('div');card.className='tc '+(isErr?'tc-err':'tc-ok');
  const hdr=isErr?'\u274C '+esc(name)+' error':'\u2705 '+esc(name)+' done';
  const coll=output.length>200?'coll':'';
  card.innerHTML='<div class="tc-head" style="font-size:11px;opacity:.8">'+hdr+'</div>'
    +'<pre class="tc-res '+coll+'" onclick="this.classList.toggle(\'coll\')">'+esc(output)+'</pre>';
  // Insert before the current streaming bubble
  const ti=T[tid];
  if(ti&&ti.bubble&&ti.bubble.parentElement){
    pane.insertBefore(card,ti.bubble.parentElement);
  }else{pane.appendChild(card);}
  scrollBot(tid);
}

function onThinking(tid,text){
  const pane=document.getElementById('chat_'+tid);if(!pane)return;
  let th=document.getElementById('think_'+tid);
  if(!th){th=document.createElement('div');th.id='think_'+tid;th.className='think';pane.appendChild(th);}
  th.textContent='\u{1F4AD} '+text;
  scrollBot(tid);
}

function onSystemMsg(tid,kind,val){
  const pane=document.getElementById('chat_'+tid);if(!pane)return;
  if(kind==='session_start'&&val){
    const ti=T[tid];if(ti)ti.sessionId=val;
  }else if(kind==='rate_limit'&&val){
    const d=document.createElement('div');d.className='tc tc-err';d.style.cssText='font-size:11px';
    d.innerHTML='<div class="tc-head">\u26A0\uFE0F <strong>Rate Limit</strong>: '+esc(val)+'</div>';
    pane.appendChild(d);scrollBot(tid);
  }else if(kind==='info'&&val){
    const d=document.createElement('div');d.className='sys-info';
    d.style.cssText='white-space:pre-line;text-align:left;padding:8px 14px;font-size:12px';
    d.textContent=val;pane.appendChild(d);scrollBot(tid);
  }
}

function onResultMeta(tid,cost,dur,turns){
  const pane=document.getElementById('chat_'+tid);if(!pane)return;
  const meta=document.createElement('div');meta.className='res-meta';
  let parts=[];
  if(cost)parts.push('<span>\u{1F4B0} $'+Number(cost).toFixed(4)+'</span>');
  if(dur)parts.push('<span>\u23F1 '+(dur/1000).toFixed(1)+'s</span>');
  if(turns)parts.push('<span>\u{1F504} '+turns+' turns</span>');
  if(parts.length){meta.innerHTML=parts.join('');pane.appendChild(meta);}
}

function onStreamEnd(tid){
  const ti=T[tid];if(!ti)return;ti.streaming=false;
  // Clean up empty bubble
  if(ti.bubble&&!ti.bubbleText){ti.bubble.parentElement.remove();}
  ti.bubble=null;
  // Remove thinking indicator
  const th=document.getElementById('think_'+tid);if(th)th.remove();
  const tabEl=document.querySelector('[data-tab="'+tid+'"]');if(tabEl)tabEl.classList.remove('strm');
  if(tid===act){setSndBtn(false);document.getElementById('msgIn').focus();}
  else{const te=document.querySelector('[data-tab="'+tid+'"]');if(te)te.classList.add('notif');}
}

function onStreamError(tid,err){
  const ti=T[tid];if(!ti)return;ti.streaming=false;
  const tabEl=document.querySelector('[data-tab="'+tid+'"]');if(tabEl)tabEl.classList.remove('strm');
  if(ti.bubble)ti.bubble.innerHTML='<span style="color:var(--err)">'+esc(err)+'</span>';
  else{
    const pane=document.getElementById('chat_'+tid);
    if(pane){const d=document.createElement('div');d.className='sys-info';d.style.color='var(--err)';d.textContent=err;pane.appendChild(d);scrollBot(tid);}
  }
  ti.bubble=null;if(tid===act)setSndBtn(false);
}

function onToolPermission(tid,toolName,toolInput,optsOrText){
  const pane=document.getElementById('chat_'+tid);if(!pane)return;
  pane.querySelectorAll('.tc-perm').forEach(el=>el.remove());
  const card=document.createElement('div');card.className='tc tc-perm';
  const ico=tIco(toolName);
  let btnsHtml='';
  // optsOrText: array of [num,label] (PTY) or string (stdin fallback)
  if(Array.isArray(optsOrText)&&optsOrText.length){
    // PTY mode: show numbered option buttons
    btnsHtml='<div class="perm-btns">';
    optsOrText.forEach(function(opt){
      const num=opt[0],label=opt[1];
      const isAllow=/allow|yes/i.test(label);
      const isDeny=/deny|no/i.test(label);
      const cls=isDeny?'perm-btn-deny':(isAllow?'perm-btn-allow':'perm-btn-allow');
      btnsHtml+='<button class="'+cls+'" onclick="respondPermChoice(\''+tid+'\','+num+',\''+esc(label)+'\','+(!isDeny)+')">'+num+'. '+esc(label)+'</button>';
    });
    btnsHtml+='</div>';
  }else{
    // Stdin fallback: simple Allow/Deny
    const prompt=typeof optsOrText==='string'?optsOrText:'';
    btnsHtml=(prompt?'<div class="perm-prompt">'+esc(prompt)+'</div>':'')
      +'<div class="perm-btns">'
      +'<button class="perm-btn-deny" onclick="respondPermChoice(\''+tid+'\',0,\''+t('permDeny')+'\',false)">'+esc(t('permDeny'))+'</button>'
      +'<button class="perm-btn-allow" onclick="respondPermChoice(\''+tid+'\',1,\''+t('permAllow')+'\',true)">'+esc(t('permAllow'))+'</button>'
      +'</div>';
  }
  card.innerHTML='<div class="tc-head">'+ico+' <strong>'+esc(toolName)+'</strong> — '+esc(t('permTitle'))+'</div>'
    +(toolInput?'<pre class="tc-inp">'+esc(toolInput)+'</pre>':'')
    +btnsHtml;
  const ti=T[tid];
  if(ti&&ti.bubble&&ti.bubble.parentElement){
    pane.insertBefore(card,ti.bubble.parentElement);
  }else{pane.appendChild(card);}
  scrollBot(tid);
}
function respondPermChoice(tid,choiceNum,label,isAllow){
  const pane=document.getElementById('chat_'+tid);
  if(pane){
    pane.querySelectorAll('.tc-perm').forEach(el=>{
      const btns=el.querySelector('.perm-btns');
      if(btns)btns.innerHTML='<span style="font-size:11px;opacity:.7">'+(isAllow?'\u2705 ':'\u274C ')+esc(label)+'</span>';
      el.classList.add(isAllow?'tc-ok':'tc-err');
    });
  }
  pywebview.api.respond_permission(tid,isAllow,String(choiceNum||'1'));
}

// ============================================================
// Permission dialog (PTY mode)
// ============================================================
function onPermissionRequest(tid,tool,desc){
  document.getElementById('permToolName').textContent=tool;
  document.getElementById('permDesc').textContent=desc||t('permToolLabel')+': '+tool;
  document.getElementById('permModal').dataset.tid=tid;
  document.getElementById('permModal').classList.add('open');
}
function allowPerm(){
  const tid=document.getElementById('permModal').dataset.tid;
  if(tid)pywebview.api.respond_permission(tid,true);
  document.getElementById('permModal').classList.remove('open');
}
function denyPerm(){
  const tid=document.getElementById('permModal').dataset.tid;
  if(tid)pywebview.api.respond_permission(tid,false);
  document.getElementById('permModal').classList.remove('open');
}

// ============================================================
// Settings
// ============================================================
function chModel(v){if(act){T[act].model=v;pywebview.api.set_tab_model(act,v);}}
function chEffort(v){if(act){T[act].effort=v;pywebview.api.set_tab_effort(act,v);}}
function togglePermMode(mode){
  if(!act)return;
  const ti=T[act];
  const newMode=(ti.permMode===mode)?'default':mode;
  ti.permMode=newMode;
  pywebview.api.set_tab_permission_mode(act,newMode);
  updatePermButtons();
}
function updatePermButtons(){
  const ti=act?T[act]:null;
  const pm=ti?ti.permMode:'default';
  [['btnPlan','plan'],['btnAcceptEdits','acceptEdits'],['btnAuto','auto']].forEach(([id,mode])=>{
    const b=document.getElementById(id);
    if(b)b.classList.toggle('active',pm===mode);
  });
}
function openGlobal(){document.getElementById('globalModal').classList.add('open');}
function closeGlobal(){document.getElementById('globalModal').classList.remove('open');}
document.getElementById('modeSelect').addEventListener('change',function(){document.getElementById('apiSec').style.display=this.value==='api'?'block':'none';});
async function saveGlobal(){
  const mode=document.getElementById('modeSelect').value;await pywebview.api.set_mode(mode);
  gMode=mode;
  if(mode==='api'){const key=document.getElementById('apiKeyInput').value;if(key)await pywebview.api.save_api_key(key);}
  closeGlobal();
  const s=await pywebview.api.get_initial_state();
  if(s.cli_available&&s.mode==='cli')setStatus('ok',t('cliOk'));
  else if(s.api_key_set)setStatus('warn',t('apiMode'));
}
// ============================================================
// Launch Options / Session Toggles — rendered from the Python spec so a flag
// added to LAUNCH_OPTIONS gets a control here with no extra markup.
// ============================================================
const LAUNCH_GROUP_LABEL={prompt:'logPrompt',model:'logModel',tools:'logTools',config:'logConfig',mcp:'logMcp',session:'logSession',integr:'logIntegr',trouble:'logTrouble',print:'logPrint'};
// key -> i18n label key. camelCase of the snake_case spec key, prefixed "opt".
function optLabelKey(key){
  return 'opt'+key.split('_').map(p=>p.charAt(0).toUpperCase()+p.slice(1)).join('');
}
function launchFieldId(key){return 'lo_'+key;}
function toggleFieldId(key){return 'tg_'+key;}

function buildLaunchPanel(){
  // Session toggles first (they act on a live session, so they're the ones a
  // user reaches for mid-task).
  const tgBox=document.getElementById('toggleGrid');
  tgBox.innerHTML='';
  TOGGLE_SPEC.forEach(tg=>{
    tgBox.appendChild(renderOptRow(tg,toggleFieldId(tg.key)));
  });
  // Launch flags, grouped.
  const box=document.getElementById('launchGroups');
  box.innerHTML='';
  const seen=[];
  LAUNCH_SPEC.forEach(o=>{if(!seen.includes(o.group))seen.push(o.group);});
  seen.forEach(gid=>{
    const det=document.createElement('details');
    det.style.cssText='border:1px solid var(--bd);border-radius:5px;margin-bottom:5px;background:var(--bg)';
    const sm=document.createElement('summary');
    sm.style.cssText='padding:6px 8px;cursor:pointer;font-size:11px;color:var(--acl);user-select:none';
    const lk=LAUNCH_GROUP_LABEL[gid]||gid;
    sm.setAttribute('data-i18n',lk);sm.textContent=t(lk);
    det.appendChild(sm);
    const body=document.createElement('div');
    body.style.cssText='padding:4px 8px 8px';
    LAUNCH_SPEC.filter(o=>o.group===gid).forEach(o=>{
      body.appendChild(renderOptRow(o,launchFieldId(o.key)));
    });
    det.appendChild(body);
    box.appendChild(det);
  });
}

// One control for one spec entry. Kinds mirror the Python side exactly:
// bool -> checkbox, list -> multi-select (tools) or space-separated text,
// select -> dropdown, text/optional -> single-line input.
function renderOptRow(o,id){
  const wrap=document.createElement('div');
  wrap.style.cssText='margin:5px 0';
  const lk=optLabelKey(o.key);
  if(o.kind==='bool'){
    const lb=document.createElement('label');
    lb.style.cssText='display:flex;align-items:center;gap:6px;font-size:11px;cursor:pointer;margin:0';
    const cb=document.createElement('input');
    cb.type='checkbox';cb.id=id;cb.style.width='auto';cb.style.margin='0';
    const sp=document.createElement('span');
    sp.setAttribute('data-i18n',lk);sp.textContent=t(lk);
    lb.appendChild(cb);lb.appendChild(sp);wrap.appendChild(lb);
    if(o.flag)wrap.appendChild(flagHint(o.flag));
    return wrap;
  }
  const lb=document.createElement('label');
  lb.style.cssText='display:block;font-size:11px;margin:0 0 3px';
  lb.setAttribute('data-i18n',lk);lb.textContent=t(lk);
  wrap.appendChild(lb);
  if(o.kind==='select'){
    const sel=document.createElement('select');sel.id=id;
    (o.choices||['']).forEach(c=>{
      const op=document.createElement('option');op.value=c;
      op.textContent=c===''?'—':c;sel.appendChild(op);
    });
    wrap.appendChild(sel);
  }else if(o.kind==='list'&&(o.key==='tools'||o.key==='disallowed_tools')){
    // Tool lists get a real multi-select so names are always spelled right —
    // a typo'd tool name is silently ignored by the CLI.
    const sel=document.createElement('select');
    sel.id=id;sel.multiple=true;sel.size=6;
    sel.style.cssText='width:100%;font-size:11px';
    TOOL_GROUPS.forEach(g=>{
      const og=document.createElement('optgroup');og.label=g.id;
      (g.tools||[]).forEach(tl=>{const op=document.createElement('option');op.value=tl;op.textContent=tl;og.appendChild(op);});
      sel.appendChild(og);
    });
    wrap.appendChild(sel);
  }else{
    const inp=document.createElement('input');
    inp.type='text';inp.id=id;inp.spellcheck=false;
    if(o.flag)inp.placeholder=o.flag;
    wrap.appendChild(inp);
  }
  if(o.flag)wrap.appendChild(flagHint(o.flag));
  return wrap;
}
function flagHint(flag){
  const h=document.createElement('div');
  h.className='nt';
  h.style.cssText='margin:2px 0 0;font-family:var(--mono);font-size:9px;opacity:.6';
  h.textContent=flag;
  return h;
}

// Read current values out of the panel, shaped for the Python sanitizers.
function collectLaunch(){
  const out={};
  LAUNCH_SPEC.forEach(o=>{
    const el=document.getElementById(launchFieldId(o.key));
    if(!el)return;
    if(o.kind==='bool'){out[o.key]=!!el.checked;}
    else if(o.kind==='list'){
      if(el.multiple){out[o.key]=Array.from(el.selectedOptions).map(x=>x.value);}
      else{out[o.key]=(el.value||'').split(/\s+/).filter(Boolean);}
    }else{out[o.key]=el.value||'';}
  });
  return out;
}
function collectToggles(){
  const out={};
  TOGGLE_SPEC.forEach(tg=>{
    const el=document.getElementById(toggleFieldId(tg.key));
    if(!el)return;
    out[tg.key]=(tg.kind==='bool')?!!el.checked:(el.value||'');
  });
  return out;
}
function fillLaunch(launch,toggles){
  LAUNCH_SPEC.forEach(o=>{
    const el=document.getElementById(launchFieldId(o.key));
    if(!el)return;
    const v=(launch||{})[o.key];
    if(o.kind==='bool'){el.checked=!!v;}
    else if(o.kind==='list'){
      const arr=Array.isArray(v)?v:[];
      if(el.multiple){Array.from(el.options).forEach(op=>{op.selected=arr.includes(op.value);});}
      else{el.value=arr.join(' ');}
    }else{el.value=v||'';}
  });
  TOGGLE_SPEC.forEach(tg=>{
    const el=document.getElementById(toggleFieldId(tg.key));
    if(!el)return;
    const v=(toggles||{})[tg.key];
    if(tg.kind==='bool')el.checked=!!v; else el.value=v||'';
  });
}

async function previewLaunchCmd(){
  const tid=_tsCurId||act;
  if(!tid)return;
  // Persist what's on screen first, so the preview reflects the panel.
  await pushLaunchFromPanel(tid);
  const pre=document.getElementById('launchPreview');
  try{
    const r=await pywebview.api.preview_tab_cmd(tid);
    if(r&&r.ok){
      let txt=r.display||'';
      if(r.toggles&&r.toggles.length)txt+='\n\n'+r.toggles.join('\n');
      pre.textContent=txt;
    }else{pre.textContent=(r&&r.error)||'error';}
  }catch(e){pre.textContent=String(e);}
  pre.style.display='block';
}

async function pushLaunchFromPanel(tid){
  try{
    const lr=await pywebview.api.set_tab_launch(tid,collectLaunch());
    if(lr&&lr.launch)T[tid].launch=lr.launch;
    const tr=await pywebview.api.set_tab_toggles(tid,collectToggles());
    if(tr&&tr.toggles)T[tid].toggles=tr.toggles;
    const rc=document.getElementById('tsRemoteControl');
    if(rc){
      const rr=await pywebview.api.set_tab_remote_control(tid,rc.checked);
      if(rr&&rr.ok)T[tid].remoteControl=rr.remote_control;
    }
  }catch(e){console.warn('pushLaunch',e);}
}

// ============================================================
// CLI auto-update
// ============================================================
function renderCliVersion(){
  const el=document.getElementById('cliVerLabel');
  if(el)el.textContent=gCliVersion||'-';
}
function onUpdateStatus(st){
  st=st||{};
  gUpdatePhase=st.phase||'idle';
  if(st.cli_version)gCliVersion=st.cli_version;
  renderCliVersion();
  const pill=document.getElementById('updPill');
  if(!pill)return;
  const show=(txt,col)=>{pill.textContent=txt;pill.style.color=col;pill.style.borderColor=col;pill.style.display='inline-block';};
  if(st.phase==='checking'){show(t('updChecking'),'var(--txd)');}
  else if(st.phase==='updating'){show(t('updUpdating'),'var(--warn)');}
  else if(st.phase==='error'){
    show(t('updFailed'),'var(--err)');
    pill.title=st.message||'';
  }
  else if(st.phase==='done'){
    if(st.changed){
      show(t('updUpdated')+' ('+(st.from||'?')+' → '+(st.to||'?')+')','var(--ok)');
      pill.title=st.message||'';
      // A fresh binary only reaches a tab on its next spawn, so say so once
      // rather than silently restarting work the user is in the middle of.
      if(act)onSystemMsg(act,'info',t('updUpdated'));
    }else{
      // Nothing to do — don't leave a permanent badge in the topbar.
      show(t('updLatest')+' '+(st.to||st.from||''),'var(--txd)');
      setTimeout(()=>{if(gUpdatePhase==='done')pill.style.display='none';},6000);
    }
  }
  else{pill.style.display='none';}
}
async function chAutoUpdate(on){
  try{await pywebview.api.set_auto_update(!!on);}catch(e){}
}
async function updateNow(){
  try{await pywebview.api.run_cli_update(true);}catch(e){}
}

// ============================================================
// Command palette — every Claude Code command, one keystroke away
// ============================================================
let cmdCat='';let cmdSel=0;let cmdRows=[];
function openCmdPalette(){
  buildCmdCats();
  const s=document.getElementById('cmdSearch');
  s.value='';cmdSel=0;
  renderCmdList();
  document.getElementById('cmdModal').classList.add('open');
  setTimeout(()=>s.focus(),30);
}
function closeCmdPalette(){document.getElementById('cmdModal').classList.remove('open');}
function buildCmdCats(){
  const box=document.getElementById('cmdCats');
  const cats=[['','cmdAll']].concat(CMD_CATS.map(c=>[c.id,c.label]));
  box.innerHTML=cats.map(([id,lk])=>
    '<button class="tb-btn" data-cat="'+esc(id)+'" onclick="setCmdCat(\''+esc(id)+'\')" style="font-size:10px;'+
    (cmdCat===id?'border-color:var(--ac);color:var(--tx)':'')+'">'+esc(t(lk))+'</button>').join('');
}
function setCmdCat(id){cmdCat=id;cmdSel=0;buildCmdCats();renderCmdList();}
function renderCmdList(){
  const q=(document.getElementById('cmdSearch').value||'').toLowerCase().trim();
  cmdRows=CMD_CATALOG.filter(c=>{
    if(cmdCat&&c.cat!==cmdCat)return false;
    if(!q)return true;
    const desc=(c.d&&(c.d[curLang]||c.d.en))||'';
    return c.cmd.toLowerCase().includes(q)||desc.toLowerCase().includes(q);
  });
  if(cmdSel>=cmdRows.length)cmdSel=Math.max(0,cmdRows.length-1);
  const box=document.getElementById('cmdList');
  if(!cmdRows.length){
    box.innerHTML='<div style="padding:14px;text-align:center;color:var(--txd);font-size:11px">'+esc(t('cmdNoMatch'))+'</div>';
    return;
  }
  box.innerHTML=cmdRows.map((c,i)=>{
    const desc=(c.d&&(c.d[curLang]||c.d.en))||'';
    return '<div class="ac-item'+(i===cmdSel?' sel':'')+'" data-i="'+i+'" onmousedown="pickCmd('+i+',event.shiftKey)" '+
      'style="display:flex;gap:10px;padding:5px 9px;cursor:pointer;align-items:baseline;border-bottom:1px solid var(--bd)">'+
      '<span class="ac-cmd" style="flex:0 0 190px;font-family:var(--mono);color:var(--acl)">'+esc(c.cmd)+'</span>'+
      '<span class="ac-desc" style="flex:1;color:var(--txd);font-size:11px">'+esc(desc)+'</span></div>';
  }).join('');
  const sel=box.querySelector('.ac-item.sel');if(sel)sel.scrollIntoView({block:'nearest'});
}
function cmdKey(e){
  if(e.key==='ArrowDown'){e.preventDefault();cmdSel=Math.min(cmdSel+1,cmdRows.length-1);renderCmdList();}
  else if(e.key==='ArrowUp'){e.preventDefault();cmdSel=Math.max(cmdSel-1,0);renderCmdList();}
  else if(e.key==='Enter'){e.preventDefault();pickCmd(cmdSel,e.shiftKey);}
  else if(e.key==='Escape'){e.preventDefault();closeCmdPalette();}
}
// Enter inserts the command into the composer (so args can be typed);
// Shift+Enter sends it straight through.
function pickCmd(i,send){
  const c=cmdRows[i];
  if(!c)return;
  closeCmdPalette();
  const inp=document.getElementById('msgIn');
  // Commands that take arguments always get inserted so the user can type
  // them; argument-less ones can be fired directly with Shift+Enter.
  if(send&&!c.args){
    inp.value=c.cmd;
    doSend();
    return;
  }
  inp.value=c.cmd+(c.args?' ':'');
  inp.focus();aResize(inp);updateAc();
}

function openTabModal(){
  if(!act)return;const ti=T[act];
  _tsCurId=act;
  document.getElementById('tsFlags').value=ti.customFlags||'';
  document.getElementById('tsSysPrompt').value=ti.sysPrompt||'';
  document.getElementById('tsSessionId').textContent=ti.sessionId||'-';
  // Permission
  document.getElementById('tsPermMode').value=ti.permMode||'default';
  const at=ti.allowedTools||[];
  document.querySelectorAll('.perm-cb').forEach(cb=>{cb.checked=at.includes(cb.value);});
  togglePermTools();
  // Launch options / session toggles / Remote Control
  const rc=document.getElementById('tsRemoteControl');
  if(rc)rc.checked=(ti.remoteControl!==false);
  fillLaunch(ti.launch||{},ti.toggles||{});
  const pv=document.getElementById('launchPreview');
  if(pv){pv.style.display='none';pv.textContent='';}
  document.getElementById('tabModal').classList.add('open');
}
function closeTabModal(){document.getElementById('tabModal').classList.remove('open');}
function togglePermTools(){
  const mode=document.getElementById('tsPermMode').value;
  document.getElementById('permToolsBox').style.display=mode==='custom'?'block':'none';
  const noteKeys={default:'permNoteDefault',acceptEdits:'permNoteAcceptEdits',plan:'permNotePlan',auto:'permNoteAuto',dontAsk:'permNoteDontAsk',bypassPermissions:'permNoteBypass',custom:'permNoteCustom'};
  document.getElementById('permNote').textContent=t(noteKeys[mode]||'permNoteDefault');
}
async function saveTabModal(){
  // Save to the tab the modal was opened for (_tsCurId), NOT the live `act` -
  // Ctrl+Tab can change `act` while the modal is open.
  const tid=_tsCurId;
  if(!tid||!T[tid])return;
  const fl=document.getElementById('tsFlags').value;
  const sp=document.getElementById('tsSysPrompt').value;
  const pm=document.getElementById('tsPermMode').value;
  const at=[];document.querySelectorAll('.perm-cb').forEach(cb=>{if(cb.checked)at.push(cb.value);});
  T[tid].customFlags=fl;T[tid].sysPrompt=sp;
  T[tid].permMode=pm;T[tid].allowedTools=at;
  await pywebview.api.set_tab_custom_flags(tid,fl);
  await pywebview.api.set_tab_system_prompt(tid,sp);
  await pywebview.api.set_tab_permission_mode(tid,pm);
  await pywebview.api.set_tab_allowed_tools(tid,at);
  await pushLaunchFromPanel(tid);
  updatePermButtons();
  closeTabModal();
}

// ============================================================
// Keyboard shortcuts// ============================================================
// Keyboard shortcuts
// ============================================================
document.addEventListener('keydown',(e)=>{
  if(e.ctrlKey&&(e.key==='k'||e.key==='K')){e.preventDefault();openCmdPalette();}
  else if(e.ctrlKey&&e.key==='t'){e.preventDefault();addTab();}
  else if(e.ctrlKey&&e.key==='w'){e.preventDefault();closeCurrentTab();}
  else if(e.ctrlKey&&!e.shiftKey&&e.key==='Tab'){e.preventDefault();cycleTab(1);}
  else if(e.ctrlKey&&e.shiftKey&&e.key==='Tab'){e.preventDefault();cycleTab(-1);}
  else if(e.key==='Escape'){
    // Terminal mode: send Escape to PTY (interrupts Claude Code)
    if(act&&T[act]&&T[act].termActive){pywebview.api.pty_input(act,'\x1b');return;}
    // Stop streaming first
    if(act&&T[act]&&T[act].streaming){pywebview.api.stop_streaming(act);return;}
    closeVw();closeGlobal();closeTabModal();closeCmdPalette();document.getElementById('permModal').classList.remove('open');
  }
});

// ============================================================
// Helpers
// ============================================================
function scrollBot(tid){const p=document.getElementById('chat_'+tid);if(p)p.scrollTop=p.scrollHeight;}
function updateHint(){}
function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML;}
function mdRender(text){
  if(!text)return'';
  let blocks=[];
  // Extract complete code blocks
  let h=text.replace(/```(\w*)\n([\s\S]*?)```/g,function(_,lang,code){
    const i=blocks.length;
    blocks.push('<pre><code>'+esc(code)+'</code></pre>');
    return'\n%%CB'+i+'%%\n';
  });
  // Partial code block at end (streaming)
  h=h.replace(/```(\w*)\n([\s\S]*)$/,function(_,lang,code){
    const i=blocks.length;
    blocks.push('<pre><code>'+esc(code)+'<span class="typing"><span></span><span></span><span></span></span></code></pre>');
    return'\n%%CB'+i+'%%';
  });
  // Escape remaining HTML
  h=esc(h);
  // Restore code blocks
  for(let i=0;i<blocks.length;i++){h=h.replace('%%CB'+i+'%%',blocks[i]);}
  // Inline code
  h=h.replace(/`([^`\n]+)`/g,'<code>$1</code>');
  // Bold + italic
  h=h.replace(/\*\*\*([^*]+)\*\*\*/g,'<strong><em>$1</em></strong>');
  h=h.replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>');
  h=h.replace(/(?<!\*)\*([^*\n]+)\*(?!\*)/g,'<em>$1</em>');
  // Headers
  h=h.replace(/^### (.+)$/gm,'<h3>$1</h3>');
  h=h.replace(/^## (.+)$/gm,'<h2>$1</h2>');
  h=h.replace(/^# (.+)$/gm,'<h1>$1</h1>');
  // Lists
  h=h.replace(/^- (.+)$/gm,'<li>$1</li>');
  h=h.replace(/^\d+\. (.+)$/gm,'<li>$1</li>');
  h=h.replace(/((?:\n?<li>.*?<\/li>)+)/g,'<ul>$1</ul>');
  // HR
  h=h.replace(/^---+$/gm,'<hr>');
  // Links
  h=h.replace(/\[([^\]]+)\]\(([^)]+)\)/g,'<a href="$2" style="color:var(--acl)" target="_blank">$1</a>');
  // Newlines
  h=h.replace(/\n/g,'<br>');
  // Clean br around block elements
  h=h.replace(/<br>\s*(<\/?(?:h[1-6]|ul|ol|li|pre|hr))/g,'$1');
  h=h.replace(/(<\/(?:h[1-6]|ul|ol|li|pre|hr)>)\s*<br>/g,'$1');
  return h;
}

// ============================================================
// Slash command autocomplete
// ============================================================
// Full command catalog, cross-checked against the Claude Code commands
// reference for the installed CLI. Commands removed upstream (/vim,
// /pr-comments, /ultraplan) are deliberately absent, as are invented names —
// a command the CLI doesn't have just wastes a turn.
//   cat  : palette category id (see CMD_CATS)
//   args : present when the command takes arguments, so the palette inserts
//          instead of firing, and the composer offers argument suggestions.
//   app  : handled inside the GUI (see Api._handle_slash) rather than forwarded.
const CMD_CATS=[
  {id:'session',label:'ccSession'},
  {id:'context',label:'ccContext'},
  {id:'model',label:'ccModel'},
  {id:'review',label:'ccReview'},
  {id:'agents',label:'ccAgents'},
  {id:'extend',label:'ccExtend'},
  {id:'perms',label:'ccPerms'},
  {id:'platform',label:'ccPlatform'},
  {id:'config',label:'ccConfig'},
  {id:'account',label:'ccAccount'},
  {id:'info',label:'ccInfo'},
];
const CMD_CATALOG=[
// --- session ---
{cmd:'/clear',cat:'session',app:1,d:{en:'Start a new conversation with empty context',ja:'コンテキストを空にして新しい会話を開始'}},
{cmd:'/compact',cat:'session',app:1,args:1,d:{en:'Free up context by summarizing the conversation so far',ja:'ここまでの会話を要約してコンテキストを解放'}},
{cmd:'/resume',cat:'session',args:1,d:{en:'Resume a conversation by ID or name, or open the picker',ja:'ID/名前でセッション再開、またはピッカーを開く'}},
{cmd:'/branch',cat:'session',args:1,d:{en:'Branch the conversation here to try a different direction',ja:'この時点で会話を分岐させ別方針を試す'}},
{cmd:'/fork',cat:'session',args:1,d:{en:'Copy this conversation into a new background session',ja:'この会話を新しいバックグラウンドセッションに複製'}},
{cmd:'/subtask',cat:'session',args:1,d:{en:'Spawn a forked subagent that inherits the full conversation',ja:'会話全体を引き継ぐフォーク型サブエージェントを起動'}},
{cmd:'/background',cat:'session',args:1,d:{en:'Detach this session to run as a background agent',ja:'セッションをバックグラウンドエージェントとして切り離す'}},
{cmd:'/stop',cat:'session',d:{en:'Stop the background session you are attached to',ja:'接続中のバックグラウンドセッションを停止'}},
{cmd:'/tasks',cat:'session',d:{en:'View and manage background work in this session',ja:'このセッションのバックグラウンド作業を確認・管理'}},
{cmd:'/rename',cat:'session',args:1,d:{en:'Rename the session and show the name on the prompt bar',ja:'セッション名を変更しプロンプトバーに表示'}},
{cmd:'/export',cat:'session',args:1,d:{en:'Export the conversation as plain text',ja:'会話をプレーンテキストで書き出す'}},
{cmd:'/copy',cat:'session',args:1,d:{en:'Copy the last assistant response to the clipboard',ja:'直前の応答をクリップボードにコピー'}},
{cmd:'/diff',cat:'session',d:{en:'Review working-tree changes, including Claude edits',ja:'Claude の編集を含む作業ツリーの差分を確認'}},
{cmd:'/rewind',cat:'session',d:{en:'Rewind the conversation and/or code to an earlier point',ja:'会話・コードを以前の状態に巻き戻す'}},
{cmd:'/recap',cat:'session',d:{en:'Generate a one-line summary of the session',ja:'セッションの一行要約を生成'}},
{cmd:'/btw',cat:'session',args:1,d:{en:'Ask a side question without adding to the conversation',ja:'会話に残さず脇道の質問をする'}},
{cmd:'/focus',cat:'session',d:{en:'Toggle the focus view (last prompt + tool summary only)',ja:'フォーカス表示 (直近プロンプト＋要約のみ) を切替'}},
{cmd:'/goal',cat:'session',args:1,d:{en:'Keep Claude working until a condition is met',ja:'条件を満たすまで作業を継続させる'}},
{cmd:'/plan',cat:'session',args:1,d:{en:'Enter plan mode directly from the prompt',ja:'プロンプトから直接プランモードに入る'}},
{cmd:'/cd',cat:'session',args:1,d:{en:'Move this session to a new working directory',ja:'セッションの作業ディレクトリを移動'}},
{cmd:'/add-dir',cat:'session',args:1,d:{en:'Add a working directory for file access',ja:'ファイルアクセス用の作業ディレクトリを追加'}},
{cmd:'/exit',cat:'session',d:{en:'Exit the CLI',ja:'CLI を終了'}},
// --- context & memory ---
{cmd:'/context',cat:'context',args:1,d:{en:'Visualize current context usage as a grid',ja:'現在のコンテキスト使用量をグリッド表示'}},
{cmd:'/memory',cat:'context',d:{en:'Edit CLAUDE.md files and manage auto memory',ja:'CLAUDE.md 編集と自動メモリの管理'}},
{cmd:'/autocompact',cat:'context',args:1,d:{en:'Set the auto-compact window (auto, 200k, 1M...)',ja:'自動コンパクトの窓を設定 (auto, 200k, 1M)'}},
{cmd:'/init',cat:'context',d:{en:'Initialize the project with a CLAUDE.md guide',ja:'CLAUDE.md ガイドでプロジェクトを初期化'}},
// --- model & effort ---
{cmd:'/model',cat:'model',app:1,args:['default','best','opus','sonnet','haiku','fable','opusplan','opus[1m]','sonnet[1m]'],d:{en:'Switch the model and save it as the new default',ja:'モデルを切替え、新規セッションの既定に保存'}},
{cmd:'/effort',cat:'model',app:1,args:['low','medium','high','xhigh','max','auto','ultracode'],d:{en:'Set the effort level (low-max, auto, or ultracode)',ja:'Effort レベルを設定 (low〜max / auto / ultracode)'}},
{cmd:'/fast',cat:'model',args:['on','off'],d:{en:'Toggle fast mode (faster output, premium rate)',ja:'Fast モード切替 (高速出力・割増料金)'}},
{cmd:'/advisor',cat:'model',args:1,d:{en:'Consult a second model for guidance at key moments',ja:'要所で第二モデルに助言を求める'}},
{cmd:'/output-style',cat:'model',args:['default','Explanatory','Learning','Concise'],d:{en:'List output styles or switch to one',ja:'出力スタイルの一覧／切替'}},
// --- review & quality ---
{cmd:'/code-review',cat:'review',args:['low','medium','high','xhigh','max','ultra','--fix','--comment'],d:{en:'Review the current diff, a PR, a branch, or a path',ja:'差分・PR・ブランチ・パスをレビュー'}},
{cmd:'/review',cat:'review',args:1,d:{en:'Alias for /code-review',ja:'/code-review のエイリアス'}},
{cmd:'/security-review',cat:'review',d:{en:'Analyze branch changes for security vulnerabilities',ja:'ブランチの変更を脆弱性観点で解析'}},
{cmd:'/ultrareview',cat:'review',args:1,d:{en:'Deep multi-agent code review in a cloud sandbox',ja:'クラウドでのマルチエージェント徹底レビュー'}},
{cmd:'/simplify',cat:'review',args:1,d:{en:'Simplify the changed code (quality only, no bug hunt)',ja:'変更コードを簡素化 (品質のみ・バグ探索なし)'}},
{cmd:'/verify',cat:'review',d:{en:'Verify a change against the running app',ja:'実行中アプリで変更を検証'}},
{cmd:'/debug',cat:'review',args:1,d:{en:'Investigate a bug systematically',ja:'バグを体系的に調査'}},
{cmd:'/run',cat:'review',d:{en:'Launch and drive this project app',ja:'このプロジェクトのアプリを起動・操作'}},
{cmd:'/doctor',cat:'review',args:1,d:{en:'Check the health of your Claude Code setup',ja:'Claude Code 構成の健全性を診断'}},
{cmd:'/autofix-pr',cat:'review',args:1,d:{en:'Cloud session that pushes fixes when CI fails',ja:'CI 失敗時に修正を push するクラウドセッション'}},
// --- agents & workflows ---
{cmd:'/agents',cat:'agents',d:{en:'Create or manage subagents',ja:'サブエージェントの作成・管理'}},
{cmd:'/list-agents',cat:'agents',d:{en:'List agents Claude can message, with their names',ja:'メッセージ送信可能なエージェント一覧'}},
{cmd:'/workflows',cat:'agents',d:{en:'Watch, pause, resume, or save dynamic workflows',ja:'動的ワークフローの監視・一時停止・保存'}},
{cmd:'/deep-research',cat:'agents',args:1,d:{en:'Multi-agent deep research on a question',ja:'質問に対するマルチエージェント徹底調査'}},
{cmd:'/batch',cat:'agents',args:1,d:{en:'Fan one instruction out across many items',ja:'一つの指示を多数の対象へ並列展開'}},
{cmd:'/loop',cat:'agents',args:1,d:{en:'Run a prompt on a recurring interval',ja:'プロンプトを一定間隔で繰り返し実行'}},
{cmd:'/schedule',cat:'agents',args:1,d:{en:'Create, list, or run cloud scheduled tasks',ja:'クラウド定期タスクの作成・一覧・実行'}},
{cmd:'/workflow-authoring',cat:'agents',d:{en:'Load the reference for writing dynamic workflows',ja:'動的ワークフロー作成リファレンスを読み込む'}},
{cmd:'/run-skill-generator',cat:'agents',d:{en:'Record how to build and launch this app as a skill',ja:'アプリのビルド・起動手順をスキル化'}},
// --- extend (MCP, skills, plugins) ---
{cmd:'/mcp',cat:'extend',args:['reconnect','enable','disable'],d:{en:'Manage MCP server connections and OAuth',ja:'MCP サーバー接続と OAuth の管理'}},
{cmd:'/skills',cat:'extend',d:{en:'List available skills',ja:'利用可能なスキル一覧'}},
{cmd:'/skill-doctor',cat:'extend',d:{en:'Show what each skill costs in context and how often it is used',ja:'各スキルのコンテキストコストと使用頻度を表示'}},
{cmd:'/reload-skills',cat:'extend',d:{en:'Re-scan skill directories without restarting',ja:'再起動なしでスキルを再スキャン'}},
{cmd:'/plugin',cat:'extend',args:1,d:{en:'Manage Claude Code plugins',ja:'Claude Code プラグインの管理'}},
{cmd:'/reload-plugins',cat:'extend',args:['--force'],d:{en:'Reload active plugins to apply pending changes',ja:'有効なプラグインを再読込して変更を反映'}},
{cmd:'/plugin-authoring',cat:'extend',d:{en:'Load the reference for writing a mod',ja:'mod 作成リファレンスを読み込む'}},
{cmd:'/hooks',cat:'extend',d:{en:'View hook configurations',ja:'フック設定を表示'}},
{cmd:'/artifacts',cat:'extend',d:{en:'List, attach, or open your artifacts',ja:'アーティファクトの一覧・添付・表示'}},
{cmd:'/artifact-capabilities',cat:'extend',d:{en:'Load the artifact runtime-capability reference',ja:'アーティファクト実行機能リファレンスを読み込む'}},
{cmd:'/artifact-diagramming',cat:'extend',d:{en:'Load artifact diagramming guidance',ja:'アーティファクト図解ガイドを読み込む'}},
{cmd:'/dataviz',cat:'extend',args:1,d:{en:'Build a chart or dashboard that reads as one system',ja:'統一感のあるチャート／ダッシュボードを作成'}},
{cmd:'/design',cat:'extend',args:1,d:{en:'Produce a distinctive, production-grade UI design',ja:'独自性のある実用レベルの UI デザインを作成'}},
{cmd:'/design-sync',cat:'extend',args:1,d:{en:'Sync with your design system',ja:'デザインシステムと同期'}},
{cmd:'/design-login',cat:'extend',d:{en:'Authorize design-system access with your claude.ai account',ja:'claude.ai アカウントでデザインシステム連携を許可'}},
{cmd:'/slides',cat:'extend',args:1,d:{en:'Build a slide deck',ja:'スライドデッキを作成'}},
{cmd:'/claude-api',cat:'extend',args:['migrate','prompt-audit','managed-agents-onboard','cost-optimize','build-eval'],d:{en:'Claude API reference and migration workflows',ja:'Claude API リファレンスと移行ワークフロー'}},
{cmd:'/claude-in-chrome',cat:'extend',args:1,d:{en:'Drive your Chrome browser',ja:'Chrome ブラウザを操作'}},
{cmd:'/update-config',cat:'extend',args:1,d:{en:'Configure the harness via settings.json (hooks, permissions, env)',ja:'settings.json でハーネスを設定 (フック・権限・env)'}},
{cmd:'/fewer-permission-prompts',cat:'extend',d:{en:'Build an allowlist from your transcripts to cut prompts',ja:'履歴から許可リストを作り確認回数を削減'}},
// --- permissions & security ---
{cmd:'/permissions',cat:'perms',d:{en:'Manage allow, ask, and deny rules for tools',ja:'ツールの allow / ask / deny ルールを管理'}},
{cmd:'/sandbox',cat:'perms',d:{en:'Toggle sandbox mode',ja:'サンドボックスモードを切替'}},
{cmd:'/auto-mode-setup',cat:'perms',d:{en:'Draft autoMode.environment entries for this project',ja:'このプロジェクト用の autoMode.environment を下書き'}},
{cmd:'/privacy-settings',cat:'perms',d:{en:'View and update your privacy settings',ja:'プライバシー設定を確認・更新'}},
// --- platforms & remote ---
{cmd:'/remote-control',cat:'platform',d:{en:'Make this session controllable from claude.ai',ja:'claude.ai から操作できるようにする'}},
{cmd:'/desktop',cat:'platform',d:{en:'Continue this session in the Claude Code Desktop app',ja:'Claude Code Desktop でこのセッションを継続'}},
{cmd:'/mobile',cat:'platform',d:{en:'Show a QR code to download the Claude mobile app',ja:'Claude モバイルアプリの QR コードを表示'}},
{cmd:'/teleport',cat:'platform',d:{en:'Pull a cloud session into this terminal',ja:'クラウドセッションをこの端末に取り込む'}},
{cmd:'/ide',cat:'platform',d:{en:'Manage IDE integrations and show status',ja:'IDE 連携の管理と状態表示'}},
{cmd:'/chrome',cat:'platform',d:{en:'Configure Claude in Chrome settings',ja:'Claude in Chrome の設定'}},
{cmd:'/web-setup',cat:'platform',d:{en:'Connect your GitHub account for cloud sessions',ja:'クラウドセッション用に GitHub 連携'}},
{cmd:'/remote-env',cat:'platform',d:{en:'Choose the default cloud environment for cloud sessions',ja:'クラウドセッションの既定環境を選択'}},
{cmd:'/install-github-app',cat:'platform',d:{en:'Install the Claude GitHub App for a repository',ja:'リポジトリに Claude GitHub App を導入'}},
{cmd:'/install-slack-app',cat:'platform',d:{en:'Install the Claude Slack app',ja:'Claude Slack アプリを導入'}},
{cmd:'/setup-bedrock',cat:'platform',d:{en:'Configure Amazon Bedrock auth, region, and model pins',ja:'Amazon Bedrock の認証・リージョン・モデル設定'}},
{cmd:'/setup-vertex',cat:'platform',d:{en:'Configure Google Cloud auth, project, region, and model pins',ja:'Google Cloud の認証・プロジェクト・モデル設定'}},
{cmd:'/import',cat:'platform',args:['codex','gemini','cursor','--dry-run'],d:{en:'Import config from Codex, Gemini CLI, or Cursor',ja:'Codex / Gemini CLI / Cursor から設定を取り込む'}},
{cmd:'/share',cat:'platform',d:{en:'Share this session',ja:'このセッションを共有'}},
// --- configuration ---
{cmd:'/config',cat:'config',args:1,d:{en:'Open Settings: theme, model, output style, preferences',ja:'設定を開く: テーマ・モデル・出力スタイル等'}},
{cmd:'/status',cat:'config',app:1,d:{en:'Version, model, account, and connectivity',ja:'バージョン・モデル・アカウント・接続状態'}},
{cmd:'/statusline',cat:'config',d:{en:'Configure the status line',ja:'ステータスラインを設定'}},
{cmd:'/theme',cat:'config',d:{en:'Change the color theme',ja:'カラーテーマを変更'}},
{cmd:'/color',cat:'config',args:1,d:{en:'Set the prompt bar color for this session',ja:'このセッションのプロンプトバー色を設定'}},
{cmd:'/keybindings',cat:'config',d:{en:'Open your keyboard shortcuts file',ja:'キーボードショートカット設定を開く'}},
{cmd:'/terminal-setup',cat:'config',d:{en:'Install a Shift+Enter newline keybinding',ja:'Shift+Enter 改行のキーバインドを導入'}},
{cmd:'/tui',cat:'config',args:['default','fullscreen'],d:{en:'Set the terminal UI renderer and relaunch into it',ja:'ターミナル UI レンダラを設定して再起動'}},
{cmd:'/scroll-speed',cat:'config',d:{en:'Adjust mouse wheel scroll speed',ja:'マウスホイールのスクロール速度を調整'}},
{cmd:'/voice',cat:'config',args:['hold','tap','off'],d:{en:'Toggle voice dictation, or set its mode',ja:'音声入力の切替・モード設定'}},
// --- account & usage ---
{cmd:'/usage',cat:'account',d:{en:'Session cost, plan limits, and activity stats',ja:'セッションコスト・プラン上限・利用統計'}},
{cmd:'/cost',cat:'account',d:{en:'Alias for /usage',ja:'/usage のエイリアス'}},
{cmd:'/stats',cat:'account',d:{en:'Alias for /usage',ja:'/usage のエイリアス'}},
{cmd:'/login',cat:'account',d:{en:'Sign in to your Anthropic account',ja:'Anthropic アカウントにサインイン'}},
{cmd:'/logout',cat:'account',d:{en:'Sign out',ja:'サインアウト'}},
{cmd:'/upgrade',cat:'account',d:{en:'Open the upgrade page to switch plan tier',ja:'プラン変更ページを開く'}},
{cmd:'/usage-credits',cat:'account',d:{en:'Configure or request usage credits',ja:'利用クレジットの設定・申請'}},
{cmd:'/rate-limit-options',cat:'account',d:{en:'Ways to keep working when a usage limit blocks a request',ja:'利用上限に当たったときの続行手段'}},
{cmd:'/passes',cat:'account',d:{en:'Share a free week of Claude Code with friends',ja:'Claude Code の無料週間を友人に共有'}},
{cmd:'/insights',cat:'account',d:{en:'HTML report analyzing your recent sessions',ja:'最近のセッションを分析した HTML レポート'}},
{cmd:'/team-onboarding',cat:'account',d:{en:'Generate a team onboarding guide from your usage history',ja:'利用履歴からチーム導入ガイドを生成'}},
// --- info & help ---
{cmd:'/help',cat:'info',app:1,d:{en:'Show help and available commands',ja:'ヘルプと利用可能なコマンドを表示'}},
{cmd:'/release-notes',cat:'info',d:{en:'View the changelog in a version picker',ja:'バージョン選択で変更履歴を表示'}},
{cmd:'/powerup',cat:'info',d:{en:'Learn Claude Code features through interactive lessons',ja:'対話レッスンで Claude Code の機能を学ぶ'}},
{cmd:'/bug',cat:'info',args:1,d:{en:'Report a bug or share your conversation',ja:'バグ報告・会話の共有'}},
{cmd:'/feedback',cat:'info',args:1,d:{en:'Send product feedback about Claude Code',ja:'Claude Code への製品フィードバックを送信'}},
{cmd:'/stickers',cat:'info',d:{en:'Order Claude Code stickers',ja:'Claude Code ステッカーを注文'}},
{cmd:'/radio',cat:'info',d:{en:'Open Claude FM lo-fi radio in your browser',ja:'Claude FM (lo-fi ラジオ) をブラウザで開く'}},
{cmd:'/heapdump',cat:'info',d:{en:'Write a JS heap snapshot and memory breakdown',ja:'JS ヒープスナップショットとメモリ内訳を出力'}},
];
// The composer's inline autocomplete reuses the same catalog, so there is only
// one list to keep correct.
const SLASH_CMDS=CMD_CATALOG.map(c=>({
  cmd:c.cmd,
  d:c.d,
  args:Array.isArray(c.args)?c.args:null,
}));
let acIdx=-1,acItems=[];
function updateAc(){
  const inp=document.getElementById('msgIn'),popup=document.getElementById('acPopup'),val=inp.value;
  if(!val.startsWith('/')||val.length<1){popup.classList.remove('vis');acItems=[];return;}
  const parts=val.split(/\s+/),prefix=parts[0].toLowerCase();
  if(parts.length<=1){
    acItems=SLASH_CMDS.filter(c=>c.cmd.startsWith(prefix));
  }else{
    const cmd=SLASH_CMDS.find(c=>c.cmd===parts[0]);
    if(cmd&&cmd.args){const ap=(parts[1]||'').toLowerCase();acItems=cmd.args.filter(a=>a.startsWith(ap)).map(a=>({cmd:parts[0]+' '+a,d:{en:a,ja:a,zh:a,ko:a}}));}
    else{acItems=[];popup.classList.remove('vis');return;}
  }
  if(!acItems.length){popup.classList.remove('vis');return;}
  acIdx=-1;
  popup.innerHTML=acItems.map((it,i)=>'<div class="ac-item" data-i="'+i+'" onmousedown="selectAc('+i+')"><span class="ac-cmd">'+esc(it.cmd)+'</span><span class="ac-desc">'+(it.d[curLang]||it.d.en)+'</span></div>').join('');
  popup.classList.add('vis');
}
function selectAc(i){
  const it=acItems[i];if(!it)return;
  const inp=document.getElementById('msgIn');
  const base=it.cmd.split(' ')[0];
  const hasArgs=SLASH_CMDS.find(c=>c.cmd===base&&c.args);
  inp.value=it.cmd+((hasArgs&&!it.cmd.includes(' '))?' ':'');
  document.getElementById('acPopup').classList.remove('vis');
  acItems=[];inp.focus();aResize(inp);
  if(hasArgs&&!it.cmd.includes(' '))setTimeout(updateAc,0);
}
function navAc(dir){
  if(!acItems.length)return;
  acIdx=(acIdx+dir+acItems.length)%acItems.length;
  document.querySelectorAll('.ac-item').forEach((el,i)=>el.classList.toggle('sel',i===acIdx));
  const sel=document.querySelector('.ac-item.sel');if(sel)sel.scrollIntoView({block:'nearest'});
}
// ============================================================
// Lifecycle
// ============================================================
window.addEventListener('beforeunload',()=>{order.forEach(id=>{if(T[id]&&T[id].termActive){const el=document.getElementById('screen_'+id);if(el&&el.textContent)pywebview.api.save_screen_content(id,el.textContent);}});});
// Frontend exceptions are invisible inside the WebView, so forward them to
// Python's stdout where they're actually readable.
window.addEventListener('error',e=>{
  try{pywebview.api.report_js_error({message:String(e.message||e.type),source:String(e.filename||''),line:e.lineno||0,stack:e.error&&e.error.stack?String(e.error.stack):''});}catch(_){}
});
window.addEventListener('unhandledrejection',e=>{
  try{pywebview.api.report_js_error({message:'unhandled rejection: '+String((e.reason&&e.reason.message)||e.reason),stack:e.reason&&e.reason.stack?String(e.reason.stack):''});}catch(_){}
});
window.addEventListener('pywebviewready',init);
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------
def main():
    api = Api()
    # Inject the i18n catalogue as window.I18N so the page's mergeI18n() can
    # translate any [data-i18n*] attribute without an API round trip. Encoded
    # with ensure_ascii and with "</" escaped so nothing in a string can close
    # the inline script early.
    i18n_json = json.dumps(_STRINGS, ensure_ascii=True).replace("</", "<\\/")
    inject = "<script>window.I18N=" + i18n_json + ";</script>"
    html_with_i18n = HTML.replace("</head>", inject + "</head>", 1)
    window = webview.create_window(
        title="Claude Code GUI",
        html=html_with_i18n,
        js_api=api,
        width=1200,
        height=800,
        min_size=(800, 500),
        background_color="#0a0a1a",
        text_select=True,
    )
    api._window = window
    webview.start(debug="--debug" in sys.argv)
    api._persist()


if __name__ == "__main__":
    main()
