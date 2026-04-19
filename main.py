"""
Claude Code GUI — Multi-tab desktop AI development assistant.
Full Claude Code CLI feature parity. No browser. No terminal. No API key needed.

Features:
  - Multi-tab parallel conversations (independent project/session per tab)
  - Model selection (Opus / Sonnet / Haiku) per tab
  - Effort level control (max / high / auto) per tab
  - i18n: English, Japanese, Chinese, Korean
  - Session continuity across restarts
  - Ctrl+L: display clear (keep session) / Ctrl+Shift+L: new session
  - Custom CLI flags passthrough
  - Background streaming with tab notifications

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

MODELS = [
    ("claude-opus-4-7", "Opus 4.7"),
    ("claude-opus-4-6", "Opus 4.6"),
    ("claude-sonnet-4-6", "Sonnet 4.6"),
    ("claude-opus-4-20250514", "Opus 4"),
    ("claude-sonnet-4-20250514", "Sonnet 4"),
    ("claude-haiku-4-5-20251001", "Haiku 4.5"),
    ("claude-3-7-sonnet-20250219", "Sonnet 3.7"),
    ("claude-3-5-sonnet-20241022", "Sonnet 3.5 v2"),
    ("claude-3-5-sonnet-20240620", "Sonnet 3.5"),
    ("claude-3-5-haiku-20241022", "Haiku 3.5"),
    ("claude-3-opus-20240229", "Opus 3"),
]
EFFORT_LEVELS = [("max", "Max"), ("xhigh", "XHigh"), ("high", "High"), ("medium", "Medium"), ("low", "Low")]
CLI_TOOLS = ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch", "WebSearch", "NotebookEdit"]


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
}


# ---------------------------------------------------------------------------
# Chat Tab
# ---------------------------------------------------------------------------
@dataclass
class ChatTab:
    id: str = ""
    name: str = "New Chat"
    project_path: str = ""
    session_id: str = ""
    model: str = "claude-opus-4-6"
    effort: str = "max"
    max_turns: int = 0
    custom_flags: str = ""
    system_prompt: str = ""
    permission_mode: str = "default"
    allowed_tools: list = field(default_factory=list)
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
            "messages": self.messages[-100:],
            "screen_content": self.screen_content[-60000:] if self.screen_content else "",
        }

    @classmethod
    def deserialize(cls, d: dict) -> "ChatTab":
        tab = cls()
        for k in ("id", "name", "project_path", "session_id", "model",
                   "effort", "max_turns", "custom_flags", "system_prompt",
                   "permission_mode", "allowed_tools", "messages",
                   "screen_content"):
            if k in d:
                setattr(tab, k, d[k])
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
        self.screen = pyte.HistoryScreen(120, 36, history=50000)
        self.screen.set_mode(pyte.modes.LNM)
        self.stream = pyte.Stream(self.screen)
        cmd = ["claude", "--model", self.tab.model, "--verbose"]
        if self.tab.session_id:
            cmd += ["--resume", self.tab.session_id]
        if self.tab.project_path:
            cmd += ["--add-dir", self.tab.project_path]
        if self.tab.max_turns > 0:
            cmd += ["--max-turns", str(self.tab.max_turns)]
        pm = self.tab.permission_mode
        if pm and pm != "default":
            cmd += ["--permission-mode", pm]
        if pm == "custom" and self.tab.allowed_tools:
            for tool in self.tab.allowed_tools:
                cmd += ["--allowedTools", tool]
        if self.tab.custom_flags:
            try:
                cmd += shlex.split(self.tab.custom_flags)
            except Exception:
                pass
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        if self.tab.effort and self.tab.effort != "auto":
            env["CLAUDE_CODE_EFFORT"] = self.tab.effort
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
# Backend API
# ---------------------------------------------------------------------------
class Api:
    def __init__(self):
        self._window = None
        self._config = _load_json(CONFIG_FILE, {})
        self._tabs: dict[str, ChatTab] = {}
        self._active: str = ""
        self._counter = 0
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
                    self._wait_and_remote_control(tab)
            else:
                self._wait_and_remote_control(tab)
        except FileNotFoundError:
            self._js(f"onStreamError('{tid}',{json.dumps(self._t('cli_not_found'))})")
        except Exception as e:
            self._js(f"onStreamError('{tid}',{json.dumps(str(e))})")

    def _wait_and_remote_control(self, tab: ChatTab):
        """Wait for CLI ready then auto-fire /remote-control."""
        try:
            if not tab.pty_session or not tab.pty_session.running:
                return
            # Wait for welcome screen to fully render
            for _ in range(50):  # 5s
                try:
                    screen_text = "\n".join(tab.pty_session.screen.display).lower()
                    if '>' in screen_text or '❯' in screen_text or 'claude' in screen_text:
                        time.sleep(1)
                        break
                except Exception:
                    pass
                time.sleep(0.1)
            if tab.pty_session and tab.pty_session.running:
                tab.pty_session.write("/remote-control\r")
                time.sleep(0.5)
                # Auto-send effort level
                if tab.effort and tab.effort != "high":
                    tab.pty_session.write(f"/effort {tab.effort}\r")
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
            "efforts": [{"id": e[0], "label": e[1]} for e in EFFORT_LEVELS],
            "cli_tools": CLI_TOOLS,
            "has_pty": HAS_PTY,
        }

    def _tab_info(self, t: ChatTab) -> dict:
        return {
            "id": t.id, "name": t.name, "project_path": t.project_path,
            "project_name": Path(t.project_path).name if t.project_path else "",
            "model": t.model, "effort": t.effort, "max_turns": t.max_turns,
            "custom_flags": t.custom_flags, "system_prompt": t.system_prompt,
            "permission_mode": t.permission_mode, "allowed_tools": t.allowed_tools,
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
        if t:
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
        if t:
            t.effort = effort
            self._persist()
            # Auto-send effort to running PTY session
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
        if t and mode in ("default", "acceptEdits", "plan", "custom"):
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
            levels = {"max", "xhigh", "high", "medium", "low"}
            if arg.lower() in levels:
                tab.effort = arg.lower()
                self._persist()
                self._js(f"onSystemMsg('{tid}','info','Effort: {tab.effort}')")
                self._js(f"document.getElementById('selEffort').value='{tab.effort}'")
            else:
                self._js(f"onSystemMsg('{tid}','info','Usage: /effort max|xhigh|high|medium|low')")
        elif command == "/model":
            model_map = {
                "opus": "claude-opus-4-6", "opus4.6": "claude-opus-4-6",
                "sonnet": "claude-sonnet-4-6", "sonnet4.6": "claude-sonnet-4-6",
                "opus4": "claude-opus-4-20250514",
                "sonnet4": "claude-sonnet-4-20250514",
                "haiku": "claude-haiku-4-5-20251001",
            }
            target = model_map.get(arg.lower().replace(" ", "").replace("-", ""))
            if target:
                tab.model = target
                self._persist()
                label = next((m[1] for m in MODELS if m[0] == target), target)
                self._js(f"onSystemMsg('{tid}','info','Model: {label}')")
                self._js(f"document.getElementById('selModel').value='{target}'")
            else:
                self._js(f"onSystemMsg('{tid}','info','Usage: /model opus|sonnet|haiku|opus4|sonnet4')")
        elif command == "/status":
            info_parts = [
                f"Session: {tab.session_id or 'none'}",
                f"Model: {tab.model}",
                f"Effort: {tab.effort}",
                f"Project: {tab.project_path or 'none'}",
                f"Permission: {tab.permission_mode}",
            ]
            self._js(f"onSystemMsg('{tid}','info',{json.dumps(chr(10).join(info_parts))})")
        elif command == "/help":
            lines = [
                "/clear — Clear display (keep session)",
                "/compact — Clear + preserve context",
                "/effort max|high|auto — Change effort",
                "/model opus|sonnet|haiku — Change model",
                "/status — Session info",
                "/help — This help",
                "",
                "Ctrl+L — Clear display",
                "Ctrl+Shift+L — New session",
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
                # Auto-fire /remote-control and effort before first message
                try:
                    tab.pty_session.write("/remote-control\r")
                    time.sleep(0.5)
                    if tab.effort and tab.effort != "high":
                        tab.pty_session.write(f"/effort {tab.effort}\r")
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
        if tab.project_path:
            cmd += ["--add-dir", tab.project_path]
        if tab.session_id:
            cmd += ["--resume", tab.session_id]
        if tab.max_turns > 0:
            cmd += ["--max-turns", str(tab.max_turns)]
        if tab.system_prompt:
            cmd += ["--system-prompt", tab.system_prompt]
        # Permission mode: use CLI's native --permission-mode flag
        mode_map = {"default": "default", "acceptEdits": "acceptEdits", "plan": "plan"}
        if tab.permission_mode in mode_map:
            cmd += ["--permission-mode", mode_map[tab.permission_mode]]
        elif tab.permission_mode == "custom" and tab.allowed_tools:
            cmd += ["--permission-mode", "default"]
            for tool in tab.allowed_tools:
                cmd += ["--allowedTools", tool]
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
        # Build interactive command (no -p, no stream-json)
        cmd_parts = ["claude", "--model", tab.model]
        if tab.session_id:
            cmd_parts += ["--resume", tab.session_id]
        if tab.project_path:
            cmd_parts += ["--add-dir", tab.project_path]
        cmd_str = " ".join(cmd_parts)
        # Open in new visible console window
        subprocess.Popen(
            f'start "Claude Code" cmd /k {cmd_str}',
            shell=True, cwd=tab.project_path or None,
        )
        self._js(f"onStreamEnd('{tab_id}')")

    def _send_cli(self, tab: ChatTab, message: str):
        tid = tab.id
        try:
            self._js(f"onStreamStart('{tid}')")
            cmd = self._build_cmd(tab, message)
            env = os.environ.copy()
            if tab.effort and tab.effort != "auto":
                env["CLAUDE_CODE_EFFORT"] = tab.effort
            tab.process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
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
                model=tab.model, max_tokens=8192, system=sys_p, messages=msgs,
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
  <div class="br">
    <button class="bg" onclick="closeGlobal()" data-i18n="cancel">Cancel</button>
    <button class="bp" onclick="saveGlobal()" data-i18n="save">Save</button>
  </div>
</div></div>

<!-- Tab Settings Modal -->
<div class="mo" id="tabModal"><div class="md">
  <h3 data-i18n="tabTitle">Tab Settings</h3>
  <label data-i18n="maxTurns">Max Turns</label>
  <input type="number" id="tsMaxTurns" min="0" value="0">
  <div class="nt" data-i18n="maxTurnsNote">0 = unlimited</div>
  <label data-i18n="customFlags">Custom CLI Flags</label>
  <input type="text" id="tsFlags" placeholder="--flag value">
  <div class="nt" data-i18n="customFlagsNote">Extra flags</div>
  <label data-i18n="sysPrompt">System Prompt</label>
  <textarea id="tsSysPrompt" data-i18n-ph="sysPromptPH" placeholder="Custom system prompt"></textarea>
  <hr class="sep">
  <label data-i18n="permMode">Permission Mode</label>
  <select id="tsPermMode" onchange="togglePermTools()">
    <option value="default" data-i18n="permDefault">Default (CLI decides)</option>
    <option value="acceptEdits" data-i18n="permAcceptEdits">Accept Edits (file ops allowed)</option>
    <option value="plan" data-i18n="permPlan">Plan (read-only)</option>
    <option value="custom" data-i18n="permCustom">Selected tools only</option>
  </select>
  <div class="nt" id="permNote" data-i18n="permNoteDefault">Interactive — tools require approval in GUI</div>
  <div id="permToolsBox" style="display:none;margin-top:8px">
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px 12px" id="permToolGrid"></div>
  </div>
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
      <span class="sp"></span>
      <select id="selModel" onchange="chModel(this.value)" title="Model"></select>
      <select id="selEffort" onchange="chEffort(this.value)" title="Effort"></select>
      <select id="selLang2" onchange="chLang(this.value)" title="Language" style="width:52px">
        <option value="en">EN</option><option value="ja">JA</option>
        <option value="zh">ZH</option><option value="ko">KO</option>
      </select>
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
function t(k){return (LANGS[curLang]&&LANGS[curLang][k])||LANGS.en[k]||k;}
function detectLang(){const n=(navigator.language||'').toLowerCase();if(n.startsWith('ja'))return'ja';if(n.startsWith('zh'))return'zh';if(n.startsWith('ko'))return'ko';return'en';}
function updateUI(){
  document.querySelectorAll('[data-i18n]').forEach(el=>{const v=t(el.dataset.i18n);if(v)el.textContent=v;});
  document.querySelectorAll('[data-i18n-ph]').forEach(el=>{const v=t(el.dataset.i18nPh);if(v)el.placeholder=v;});
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

// ============================================================
// Init
// ============================================================
async function init(){
  const s=await pywebview.api.get_initial_state();
  // Populate selectors
  const ms=document.getElementById('selModel');
  s.models.forEach(m=>{const o=document.createElement('option');o.value=m.id;o.textContent=m.label;ms.appendChild(o);});
  const es=document.getElementById('selEffort');
  s.efforts.forEach(e=>{const o=document.createElement('option');o.value=e.id;o.textContent=e.label;es.appendChild(o);});
  // Permission tool checkboxes
  const pg=document.getElementById('permToolGrid');
  (s.cli_tools||[]).forEach(tl=>{
    const lb=document.createElement('label');lb.style.cssText='display:flex;align-items:center;gap:5px;font-size:12px;cursor:pointer';
    const cb=document.createElement('input');cb.type='checkbox';cb.value=tl;cb.className='perm-cb';
    lb.appendChild(cb);lb.appendChild(document.createTextNode(tl));pg.appendChild(lb);
  });
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
}

function setStatus(cls,txt){document.getElementById('stDot').className='dot '+cls;document.getElementById('stText').textContent=txt;}

// ============================================================
// Tab DOM
// ============================================================
function createTabDOM(info){
  const id=info.id;
  T[id]={name:info.name,model:info.model||'claude-opus-4-6',effort:info.effort||'max',
    projectPath:info.project_path||'',projectName:info.project_name||'',
    maxTurns:info.max_turns||0,customFlags:info.custom_flags||'',
    sysPrompt:info.system_prompt||'',sessionId:info.session_id||'',
    permMode:info.permission_mode||'default',allowedTools:info.allowed_tools||[],
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
  const bp=document.getElementById('btnPlan');
  const ba=document.getElementById('btnAcceptEdits');
  if(bp){if(pm==='plan')bp.classList.add('active');else bp.classList.remove('active');}
  if(ba){if(pm==='acceptEdits')ba.classList.add('active');else ba.classList.remove('active');}
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
function openTabModal(){
  if(!act)return;const ti=T[act];
  document.getElementById('tsMaxTurns').value=ti.maxTurns||0;
  document.getElementById('tsFlags').value=ti.customFlags||'';
  document.getElementById('tsSysPrompt').value=ti.sysPrompt||'';
  document.getElementById('tsSessionId').textContent=ti.sessionId||'-';
  // Permission
  document.getElementById('tsPermMode').value=ti.permMode||'default';
  const at=ti.allowedTools||[];
  document.querySelectorAll('.perm-cb').forEach(cb=>{cb.checked=at.includes(cb.value);});
  togglePermTools();
  document.getElementById('tabModal').classList.add('open');
}
function closeTabModal(){document.getElementById('tabModal').classList.remove('open');}
function togglePermTools(){
  const mode=document.getElementById('tsPermMode').value;
  document.getElementById('permToolsBox').style.display=mode==='custom'?'block':'none';
  const noteKeys={default:'permNoteDefault',acceptEdits:'permNoteAcceptEdits',plan:'permNotePlan',custom:'permNoteCustom'};
  document.getElementById('permNote').textContent=t(noteKeys[mode]||'permNoteDefault');
}
async function saveTabModal(){
  if(!act)return;
  const mt=parseInt(document.getElementById('tsMaxTurns').value)||0;
  const fl=document.getElementById('tsFlags').value;
  const sp=document.getElementById('tsSysPrompt').value;
  const pm=document.getElementById('tsPermMode').value;
  const at=[];document.querySelectorAll('.perm-cb').forEach(cb=>{if(cb.checked)at.push(cb.value);});
  T[act].maxTurns=mt;T[act].customFlags=fl;T[act].sysPrompt=sp;
  T[act].permMode=pm;T[act].allowedTools=at;
  await pywebview.api.set_tab_max_turns(act,mt);
  await pywebview.api.set_tab_custom_flags(act,fl);
  await pywebview.api.set_tab_system_prompt(act,sp);
  await pywebview.api.set_tab_permission_mode(act,pm);
  await pywebview.api.set_tab_allowed_tools(act,at);
  closeTabModal();
}

// ============================================================
// Keyboard shortcuts
// ============================================================
document.addEventListener('keydown',(e)=>{
  if(e.ctrlKey&&e.key==='t'){e.preventDefault();addTab();}
  else if(e.ctrlKey&&e.key==='w'){e.preventDefault();closeCurrentTab();}
  else if(e.ctrlKey&&!e.shiftKey&&e.key==='Tab'){e.preventDefault();cycleTab(1);}
  else if(e.ctrlKey&&e.shiftKey&&e.key==='Tab'){e.preventDefault();cycleTab(-1);}
  else if(e.key==='Escape'){
    // Terminal mode: send Escape to PTY (interrupts Claude Code)
    if(act&&T[act]&&T[act].termActive){pywebview.api.pty_input(act,'\x1b');return;}
    // Stop streaming first
    if(act&&T[act]&&T[act].streaming){pywebview.api.stop_streaming(act);return;}
    closeVw();closeGlobal();closeTabModal();document.getElementById('permModal').classList.remove('open');
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
const SLASH_CMDS=[
{cmd:'/clear',d:{en:'Clear display',ja:'表示クリア',zh:'清除显示',ko:'표시 지우기'}},
{cmd:'/compact',d:{en:'Compact conversation',ja:'コンパクト化',zh:'压缩对话',ko:'대화 압축'}},
{cmd:'/effort',d:{en:'Set effort level',ja:'努力レベル設定',zh:'设置努力级别',ko:'노력 수준 설정'},args:['max','xhigh','high','medium','low']},
{cmd:'/model',d:{en:'Change model',ja:'モデル変更',zh:'切换模型',ko:'모델 변경'},args:['opus','sonnet','haiku','opus4','sonnet4']},
{cmd:'/status',d:{en:'Session info',ja:'セッション情報',zh:'会话信息',ko:'세션 정보'}},
{cmd:'/help',d:{en:'Show help',ja:'ヘルプ表示',zh:'显示帮助',ko:'도움말'}},
{cmd:'/cost',d:{en:'Show cost',ja:'コスト表示',zh:'显示费用',ko:'비용 표시'}},
{cmd:'/review',d:{en:'Review PR',ja:'PRレビュー',zh:'审查PR',ko:'PR 리뷰'}},
{cmd:'/init',d:{en:'Init CLAUDE.md',ja:'CLAUDE.md初期化',zh:'初始化CLAUDE.md',ko:'CLAUDE.md 초기화'}},
{cmd:'/bug',d:{en:'Report bug',ja:'バグ報告',zh:'报告bug',ko:'버그 보고'}},
{cmd:'/login',d:{en:'Log in',ja:'ログイン',zh:'登录',ko:'로그인'}},
{cmd:'/vim',d:{en:'Vim mode',ja:'Vimモード',zh:'Vim模式',ko:'Vim 모드'}},
{cmd:'/doctor',d:{en:'Health check',ja:'診断チェック',zh:'健康检查',ko:'진단 체크'}},
{cmd:'/permissions',d:{en:'Permission settings',ja:'権限設定',zh:'权限设置',ko:'권한 설정'}},
{cmd:'/memory',d:{en:'Memory management',ja:'メモリ管理',zh:'内存管理',ko:'메모리 관리'}},
];
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
window.addEventListener('pywebviewready',init);
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------
def main():
    api = Api()
    window = webview.create_window(
        title="Claude Code GUI",
        html=HTML,
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
