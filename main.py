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
import contextlib
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
import unicodedata # mitigation: Unicode category check + NFKC normalize for harness prefix sanitize
import uuid
from collections import OrderedDict
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
ROLES_FILE = CONFIG_DIR / "roles.json"
ROLE_REQUEST_SEEN_FILE = CONFIG_DIR / "role_request_seen.json"

MODELS = [
    ("claude-opus-5", "Opus 5"),
    ("claude-fable-5", "Fable 5"),
    ("claude-sonnet-5", "Sonnet 5"),
    ("claude-opus-4-8", "Opus 4.8"),
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
# Catalog default = first entry. Opus 5 leads: flagship-tier, priced the same as
# Opus 4.8 ($5/$25) but more capable, so it's a cost-neutral default. Fable 5 is
# the most capable model but premium-priced ($10/$50), so it's offered as a choice
# rather than the default. Referenced by the ChatTab default, the role-proposal
# wizard, and the JS fallback so a single edit to MODELS[0] propagates everywhere.
DEFAULT_MODEL = MODELS[0][0]
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
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: a crash/kill mid-write must not truncate the live file
    # (a corrupt roles.json/config.json/peers.json silently loads as {} and
    # wipes that state). Write to a sibling tmp then os.replace (atomic on
    # Windows + POSIX).
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# i18n -- centralised strings for user-visible text (Python + JS).
# Python side: `_t(key)` reads the language from config and indexes here.
# JS side:     `window.I18N` is injected from this dict at window load, and
#              `applyI18n(lang)` walks [data-i18n*] attributes to retranslate
#              every piece of visible UI on language change.
# Note: _ERRORS is kept as an alias below so legacy call sites still work.
# ---------------------------------------------------------------------------
_STRINGS_EXTRA = {
    # -- app shell --------------------------------------------------------
    "appTitle": {"en": "Claude Code", "ja": "Claude Code", "zh": "Claude Code", "ko": "Claude Code"},
    "newTab": {"en": "New Tab", "ja": "新しいタブ", "zh": "新建标签", "ko": "새 탭"},
    "newTabShort": {"en": "+ Tab", "ja": "+ タブ", "zh": "+ 标签", "ko": "+ 탭"},
    "settings": {"en": "Settings", "ja": "設定", "zh": "设置", "ko": "설정"},
    "tabSettings": {"en": "Tab Settings", "ja": "タブ設定", "zh": "标签设置", "ko": "탭 설정"},
    "coordination": {"en": "Coordination", "ja": "コーディネーション", "zh": "协作", "ko": "협업"},
    "roles": {"en": "Roles", "ja": "ロール", "zh": "角色", "ko": "역할"},
    "peers": {"en": "Peers", "ja": "ピア", "zh": "对等端", "ko": "피어"},
    "proposals": {"en": "Proposals", "ja": "提案", "zh": "提案", "ko": "제안"},
    "requests": {"en": "Requests", "ja": "リクエスト", "zh": "请求", "ko": "요청"},
    "newSession": {"en": "New Session", "ja": "新規セッション", "zh": "新会话", "ko": "새 세션"},
    "clearDisplay": {"en": "Clear Display", "ja": "表示クリア", "zh": "清除显示", "ko": "화면 지우기"},
    "save": {"en": "Save", "ja": "保存", "zh": "保存", "ko": "저장"},
    "cancel": {"en": "Cancel", "ja": "キャンセル", "zh": "取消", "ko": "취소"},
    "close": {"en": "Close", "ja": "閉じる", "zh": "关闭", "ko": "닫기"},
    "closeHint": {"en": "ESC or ×", "ja": "ESC または ×", "zh": "ESC 或 ×", "ko": "ESC 또는 ×"},
    "open": {"en": "Open", "ja": "開く", "zh": "打开", "ko": "열기"},
    "add": {"en": "Add", "ja": "追加", "zh": "添加", "ko": "추가"},
    "remove": {"en": "Remove", "ja": "削除", "zh": "删除", "ko": "삭제"},
    "edit": {"en": "Edit", "ja": "編集", "zh": "编辑", "ko": "편집"},
    "rename": {"en": "Rename", "ja": "名前変更", "zh": "重命名", "ko": "이름 바꾸기"},
    "refresh": {"en": "Refresh", "ja": "再読み込み", "zh": "刷新", "ko": "새로고침"},
    "send": {"en": "Send", "ja": "送信", "zh": "发送", "ko": "전송"},
    "stop": {"en": "Stop", "ja": "停止", "zh": "停止", "ko": "중지"},
    "start": {"en": "Start", "ja": "開始", "zh": "开始", "ko": "시작"},
    "yes": {"en": "Yes", "ja": "はい", "zh": "是", "ko": "예"},
    "no": {"en": "No", "ja": "いいえ", "zh": "否", "ko": "아니오"},
    "none": {"en": "None", "ja": "なし", "zh": "无", "ko": "없음"},
    "loading": {"en": "Loading...", "ja": "読み込み中...", "zh": "加载中...", "ko": "로드 중..."},
    # -- welcome / empty state --------------------------------------------
    "welcomeTitle": {"en": "Claude Code GUI", "ja": "Claude Code GUI", "zh": "Claude Code GUI", "ko": "Claude Code GUI"},
    "welcomeSubtitle": {
        "en": "Multi-tab desktop AI development assistant",
        "ja": "マルチタブ式 AI 開発アシスタント",
        "zh": "多标签桌面 AI 开发助手",
        "ko": "멀티탭 데스크탕 AI 개발 어시스턴트",
    },
    "welcomeStart": {"en": "Open a tab to start", "ja": "タブを開いて開始", "zh": "打开标签以开始", "ko": "탭을 열어 시작하세요"},
    "welcomeNewTab": {"en": "Open New Tab", "ja": "新規タブを開く", "zh": "新建标签", "ko": "새 탭 열기"},
    "wcT1": {"en": "Multi-tab parallel sessions", "ja": "複数タブで並行セッション", "zh": "多标签并行会话", "ko": "멀티탭 병렬 세션"},
    "wcT2": {"en": "No API key needed -- uses your Claude Code CLI login", "ja": "APIキー不要 -- Claude Code CLI のログインを利用", "zh": "无需 API 密钥 -- 复用 Claude Code CLI 登录", "ko": "API 키 불필요 -- Claude Code CLI 로그인 사용"},
    "wcT3": {"en": "Persistent sessions across restarts", "ja": "再起動してもセッション保持", "zh": "重启后会话持久", "ko": "재시작 후에도 세션 유지"},
    "wcT4": {"en": "Multi-role autonomous coordination", "ja": "マルチロール自律コーディネーション", "zh": "多角色自主协作", "ko": "멀티롤 자율 협업"},
    # -- global settings modal --------------------------------------------
    "globalSettings": {"en": "Global Settings", "ja": "全体設定", "zh": "全局设置", "ko": "전체 설정"},
    "language": {"en": "Language", "ja": "言語", "zh": "语言", "ko": "언어"},
    "theme": {"en": "Theme", "ja": "テーマ", "zh": "主题", "ko": "테마"},
    "fontSize": {"en": "Font Size", "ja": "フォントサイズ", "zh": "字体大小", "ko": "글꼴 크기"},
    "apiKey": {"en": "API Key (optional)", "ja": "APIキー (オプション)", "zh": "API 密钥 (可选)", "ko": "API 키 (선택)"},
    "apiKeyHint": {"en": "Leave blank to use Claude Code CLI login", "ja": "空欄なら Claude Code CLI ログインを使用", "zh": "留空则使用 Claude Code CLI 登录", "ko": "비워두면 Claude Code CLI 로그인 사용"},
    "cliPath": {"en": "CLI Path", "ja": "CLIパス", "zh": "CLI 路径", "ko": "CLI 경로"},
    "cliPathAuto": {"en": "Auto-detect", "ja": "自動検出", "zh": "自动检测", "ko": "자동 감지"},
    "autoAccept": {"en": "Auto-accept safe permissions (Yolo)", "ja": "安全な許可を自動承認 (Yolo)", "zh": "自动接受安全权限 (Yolo)", "ko": "안전한 권한 자동 승인 (Yolo)"},
    # -- tab settings modal -----------------------------------------------
    "projectDir": {"en": "Project Directory", "ja": "プロジェクトディレクトリ", "zh": "项目目录", "ko": "프로젝트 디렉토리"},
    "chooseDir": {"en": "Choose...", "ja": "選択...", "zh": "选择...", "ko": "선택..."},
    "model": {"en": "Model", "ja": "モデル", "zh": "模型", "ko": "모델"},
    "effort": {"en": "Effort", "ja": "思考レベル", "zh": "思考等级", "ko": "사고 수준"},
    "maxTurns": {"en": "Max Turns (0 = unlimited)", "ja": "最大ターン (0=無制限)", "zh": "最大轮次 (0=无限)", "ko": "최대 턴 (0=무제한)"},
    "customFlags": {"en": "Custom CLI Flags", "ja": "カスタムCLIフラグ", "zh": "自定义 CLI 标志", "ko": "사용자 정의 CLI 플래그"},
    "systemPrompt": {"en": "System Prompt (optional)", "ja": "システムプロンプト (オプション)", "zh": "系统提示词 (可选)", "ko": "시스템 프롬프트 (선택)"},
    "permissionMode": {"en": "Permission Mode", "ja": "許可モード", "zh": "权限模式", "ko": "권한 모드"},
    "pmDefault": {"en": "Default (ask)", "ja": "デフォルト (確認)", "zh": "默认 (询问)", "ko": "기본 (확인)"},
    "pmAcceptEdits": {"en": "Accept edits", "ja": "編集を自動許可", "zh": "自动接受编辑", "ko": "편집 자동 승인"},
    "pmBypass": {"en": "Bypass all", "ja": "すべてバイパス", "zh": "全部绕过", "ko": "모두 우회"},
    "pmPlan": {"en": "Plan mode", "ja": "プランモード", "zh": "计划模式", "ko": "계획 모드"},
    "pmCustom": {"en": "Custom (choose tools)", "ja": "カスタム (ツール選択)", "zh": "自定义 (选择工具)", "ko": "사용자 정의 (도구 선택)"},
    "allowedTools": {"en": "Allowed Tools", "ja": "許可ツール", "zh": "允许的工具", "ko": "허용된 도구"},
    "sessionId": {"en": "Session ID", "ja": "セッションID", "zh": "会话 ID", "ko": "세션 ID"},
    "tabName": {"en": "Tab Name", "ja": "タブ名", "zh": "标签名称", "ko": "탭 이름"},
    "tabRole": {"en": "Role (for coordination)", "ja": "ロール (コーディネーション用)", "zh": "角色 (用于协作)", "ko": "역할 (협업용)"},
    # -- permission modal -------------------------------------------------
    "permTitle": {"en": "Permission Required", "ja": "許可が必要です", "zh": "需要权限", "ko": "권한 필요"},
    "permBannerLabel": {"en": "Permission:", "ja": "許可:", "zh": "权限:", "ko": "권한:"},
    "permAllow": {"en": "Allow", "ja": "許可", "zh": "允许", "ko": "허용"},
    "permDeny": {"en": "Deny", "ja": "拒否", "zh": "拒绝", "ko": "거부"},
    "permDetails": {"en": "Details", "ja": "詳細", "zh": "详情", "ko": "세부사항"},
    "permHintWait": {"en": "Waiting for Claude...", "ja": "Claudeの応答を待機中...", "zh": "等待 Claude 响应...", "ko": "Claude 응답 대기 중..."},
    # -- roles editor modal -----------------------------------------------
    "rolesEditor": {"en": "Roles Editor", "ja": "ロールエディタ", "zh": "角色编辑器", "ko": "역할 편집기"},
    "rolesHint": {"en": "Define coordinator + worker roles per project. A coordinator dispatches to workers.", "ja": "プロジェクトごとにコーディネータ+ワーカーロールを定義", "zh": "为每个项目定义协调者+工作者角色", "ko": "프로젝트별로 코디네이터 + 워커 역할 정의"},
    "colId": {"en": "ID", "ja": "ID", "zh": "ID", "ko": "ID"},
    "colName": {"en": "Name", "ja": "名前", "zh": "名称", "ko": "이름"},
    "colIcon": {"en": "Icon", "ja": "アイコン", "zh": "图标", "ko": "아이콘"},
    "colColor": {"en": "Color", "ja": "色", "zh": "颜色", "ko": "색상"},
    "colKind": {"en": "Kind", "ja": "種別", "zh": "类型", "ko": "종류"},
    "kindCoordinator": {"en": "Coordinator", "ja": "コーディネータ", "zh": "协调者", "ko": "코디네이터"},
    "kindWorker": {"en": "Worker", "ja": "ワーカー", "zh": "工作者", "ko": "워커"},
    "kindCco": {"en": "CCO", "ja": "CCO", "zh": "CCO", "ko": "CCO"},
    "addRole": {"en": "+ Add Role", "ja": "+ ロール追加", "zh": "+ 添加角色", "ko": "+ 역할 추가"},
    "rolesSaved": {"en": "Roles saved", "ja": "ロールを保存しました", "zh": "角色已保存", "ko": "역할이 저장되었습니다"},
    # -- coordination panel -----------------------------------------------
    "coordPanelTitle": {"en": "Coordination", "ja": "コーディネーション", "zh": "协作", "ko": "협업"},
    "coordStart": {"en": "Start", "ja": "開始", "zh": "开始", "ko": "시작"},
    "coordStop": {"en": "Stop", "ja": "停止", "zh": "停止", "ko": "중지"},
    "coordRunning": {"en": "Running", "ja": "実行中", "zh": "运行中", "ko": "실행 중"},
    "coordStopped": {"en": "Stopped", "ja": "停止中", "zh": "已停止", "ko": "중지됨"},
    "coordCycles": {"en": "Cycles", "ja": "サイクル", "zh": "周期", "ko": "사이클"},
    "coordManager": {"en": "Manager", "ja": "マネージャ", "zh": "管理者", "ko": "관리자"},
    "coordWorkers": {"en": "Workers", "ja": "ワーカー", "zh": "工作者", "ko": "워커"},
    "coordLog": {"en": "Activity Log", "ja": "アクティビティログ", "zh": "活动日志", "ko": "활동 로그"},
    "coordAssignHint": {"en": "Assign roles to tabs (coordinator + at least one worker) to enable auto-dispatch.", "ja": "タブにロール(コーディネータ+ワーカー最低1人)を割り当てて自動分派を有効化", "zh": "为标签分配角色(协调者+至少一个工作者)以启用自动分发", "ko": "탭에 역할을 할당하세요(코디네이터 + 워커 1인 이상)"},
    "coordNoManager": {"en": "No coordinator tab assigned", "ja": "コーディネータタブが未割り当て", "zh": "未分配协调者标签", "ko": "코디네이터 탭이 할당되지 않았습니다"},
    "coordNoWorkers": {"en": "No worker tabs assigned", "ja": "ワーカータブが未割り当て", "zh": "未分配工作者标签", "ko": "워커 탭이 할당되지 않았습니다"},
    # -- peers modal ------------------------------------------------------
    "peersTitle": {"en": "Cross-project Peers", "ja": "クロスプロジェクトピア", "zh": "跨项目对等端", "ko": "프로젝트 간 피어"},
    "peersHint": {"en": "Aliases for other project roots. Use @alias.role: to dispatch across projects.", "ja": "他プロジェクトのエイリアス。@alias.role: でプロジェクト間送信", "zh": "其他项目根的别名。使用 @alias.role: 跨项目分发", "ko": "다른 프로젝트 루트 별칭. @alias.role: 로 프로젝트 간 디스패치"},
    "peerAlias": {"en": "Alias", "ja": "エイリアス", "zh": "别名", "ko": "별칭"},
    "peerPath": {"en": "Project Root", "ja": "プロジェクトルート", "zh": "项目根", "ko": "프로젝트 루트"},
    "peerAdd": {"en": "+ Add Peer", "ja": "+ ピア追加", "zh": "+ 添加对等端", "ko": "+ 피어 추가"},
    "peerRemove": {"en": "Remove", "ja": "削除", "zh": "删除", "ko": "삭제"},
    "peerTabsTitle": {"en": "In-GUI Peer Tabs", "ja": "GUI内ピアタブ", "zh": "GUI 内对等端标签", "ko": "GUI 내 피어 탭"},
    "peerTabsHint": {"en": "Select other tabs this tab may dispatch to via @peerName:.", "ja": "@peerName: でこのタブから送信できるタブを選択", "zh": "选择本标签可通过 @peerName: 分发的其他标签", "ko": "이 탭에서 @peerName: 으로 디스패치 가능한 다른 탭 선택"},
    "peerTabsNone": {"en": "None", "ja": "なし", "zh": "无", "ko": "없음"},
    # -- proposal / request modals ----------------------------------------
    "propTitle": {"en": "Proposal", "ja": "提案", "zh": "提案", "ko": "제안"},
    "reqTitle": {"en": "Request", "ja": "リクエスト", "zh": "请求", "ko": "요청"},
    "propEmpty": {"en": "No proposals", "ja": "提案はありません", "zh": "没有提案", "ko": "제안 없음"},
    "reqEmpty": {"en": "No requests", "ja": "リクエストはありません", "zh": "没有请求", "ko": "요청 없음"},
    "approve": {"en": "Approve", "ja": "承認", "zh": "批准", "ko": "승인"},
    "reject": {"en": "Reject", "ja": "却下", "zh": "拒绝", "ko": "거절"},
    # -- yolo / badges ----------------------------------------------------
    "yoloBadge": {"en": "YOLO", "ja": "YOLO", "zh": "YOLO", "ko": "YOLO"},
    "yoloOn": {"en": "Yolo ON", "ja": "Yolo ON", "zh": "Yolo 开", "ko": "Yolo 켜짐"},
    "yoloOff": {"en": "Yolo OFF", "ja": "Yolo OFF", "zh": "Yolo 关", "ko": "Yolo 꺼짐"},
    "yoloAccepted": {"en": "Auto-accepted", "ja": "自動承認", "zh": "已自动接受", "ko": "자동 승인됨"},
    "yoloSkipDangerKeyword": {"en": "Yolo skipped: danger keyword \"{kw}\" in prompt", "ja": "Yolo 停止: プロンプト内に危険キーワード「{kw}」", "zh": "Yolo 已跳过：提示中出现危险关键词「{kw}」", "ko": "Yolo 건너뜀: 프롬프트에 위험 키워드 \"{kw}\""},
    "yoloSkipNoApprove": {"en": "Yolo skipped: first option doesn't look like approval (\"{label}\")", "ja": "Yolo 停止: 最初の選択肢が承認ではない (「{label}」)", "zh": "Yolo 已跳过：首项不像是批准（「{label}」）", "ko": "Yolo 건너뜀: 첫 항목이 승인 형태가 아님 (\"{label}\")"},
    "yoloSkipNoChoices": {"en": "Yolo skipped: no choices detected", "ja": "Yolo 停止: 選択肢未検出", "zh": "Yolo 已跳过：未检测到选项", "ko": "Yolo 건너뜀: 선택지 미검출"},
    # -- dispatched bubbles (peer / coord cross-tab messages) -------------
    "dispSrcPeerTab": {"en": "Peer tab dispatch", "ja": "ピアタブから転送", "zh": "对等标签转发", "ko": "피어 탭 전달"},
    "dispSrcPeerInbox": {"en": "Cross-project inbox", "ja": "クロスプロジェクト受信", "zh": "跨项目收件", "ko": "프로젝트간 수신함"},
    "dispSrcCoordInject": {"en": "User instruction → Manager", "ja": "ユーザー指示 → マネージャ", "zh": "用户指令 → 经理", "ko": "사용자 지시 → 매니저"},
    "dispSrcCoordReport": {"en": "Worker commit → Manager", "ja": "ワーカー commit → マネージャ", "zh": "工作者提交 → 经理", "ko": "작업자 커밋 → 매니저"},
    "dispSrcCoordDispatch": {"en": "Manager → Worker", "ja": "マネージャ → ワーカー", "zh": "经理 → 工作者", "ko": "매니저 → 워커"},
    "dispSrcFileDispatch": {"en": "Queue → Worker", "ja": "キュー → ワーカー", "zh": "队列 → 工作者", "ko": "큐 → 워커"},
    "dispSrcFileReport": {"en": "Report → Manager", "ja": "レポート → マネージャ", "zh": "报告 → 经理", "ko": "보고 → 매니저"},
    "dispExpand": {"en": "Expand", "ja": "展開", "zh": "展开", "ko": "펼치기"},
    "dispCollapse": {"en": "Collapse", "ja": "折りたたむ", "zh": "折叠", "ko": "접기"},
    "dispTruncated": {"en": "(truncated)", "ja": "(省略)", "zh": "(已截断)", "ko": "(잘림)"},
    # -- session continuity -----------------------------------------------
    "sessStartFailed": {"en": "Session failed to start. Open Settings to retry.", "ja": "セッション起動失敗。設定から再試行してください。", "zh": "会话启动失败，请从设置重试。", "ko": "세션 시작 실패. 설정에서 재시도하세요."},
    "sessResumeFailed": {"en": "Resume failed — previous session expired. Starting fresh.", "ja": "セッション復帰失敗 — 前回のセッション期限切れ。新規起動します。", "zh": "恢复失败 — 上一会话已过期，正在新建。", "ko": "재개 실패 — 이전 세션이 만료되었습니다. 새로 시작합니다."},
    "sessRestartBtn": {"en": "New Session", "ja": "新規セッション", "zh": "新会话", "ko": "새 세션"},
    # -- blockers / deploy status -----------------------------------------
    "blockerTitle": {"en": "Blocker", "ja": "ブロッカー", "zh": "阻塞项", "ko": "블록커"},
    "blockerOpen": {"en": "Open blockers.md", "ja": "blockers.mdを開く", "zh": "打开 blockers.md", "ko": "blockers.md 열기"},
    "deployStatus": {"en": "Deploy Status", "ja": "デプロイ状態", "zh": "部署状态", "ko": "배포 상태"},
    # -- alerts / prompts -------------------------------------------------
    "alertOpenTab": {"en": "Open a project tab first.", "ja": "まずプロジェクトタブを開いてください。", "zh": "请先打开项目标签。", "ko": "먼저 프로젝트 탭을 열어주세요."},
    "alertNoProject": {"en": "No project path set for this tab.", "ja": "このタブにはプロジェクトパスが設定されていません。", "zh": "未为此标签设置项目路径。", "ko": "이 탭에 프로젝트 경로가 설정되지 않았습니다."},
    "confirmRemoveTab": {"en": "Close this tab?", "ja": "このタブを閉じますか?", "zh": "关闭此标签?", "ko": "이 탭을 닫을까요?"},
    "confirmRemoveRole": {"en": "Remove this role?", "ja": "このロールを削除しますか?", "zh": "删除此角色?", "ko": "이 역할을 삭제할까요?"},
    "confirmRemovePeer": {"en": "Remove this peer?", "ja": "このピアを削除しますか?", "zh": "删除此对等端?", "ko": "이 피어를 삭제할까요?"},
    "promptNewName": {"en": "New name:", "ja": "新しい名前:", "zh": "新名称:", "ko": "새 이름:"},
    "promptAlias": {"en": "Alias (letters/digits/_):", "ja": "エイリアス(英数字/_):", "zh": "别名(字母/数字/_):", "ko": "별칭(영숫자/_):"},
    "promptPath": {"en": "Project root path:", "ja": "プロジェクトルートパス:", "zh": "项目根路径:", "ko": "프로젝트 루트 경로:"},
    "errInvalidAlias": {"en": "Invalid alias", "ja": "エイリアスが無効", "zh": "别名无效", "ko": "잘못된 별칭"},
    "errInvalidPath": {"en": "Invalid path", "ja": "パスが無効", "zh": "路径无效", "ko": "잘못된 경로"},
    "errRoleExists": {"en": "Role ID already exists", "ja": "ロールIDが既に存在", "zh": "角色ID已存在", "ko": "역할 ID가 이미 존재"},
    "errNeedCoordinator": {"en": "At least one coordinator role required", "ja": "最低1つのコーディネータが必要", "zh": "至少需要一个协调者角色", "ko": "코디네이터 역할이 최소 1개 필요"},
    "errNeedWorker": {"en": "At least one worker role required", "ja": "最低1つのワーカーが必要", "zh": "至少需要一个工作者角色", "ko": "워커 역할이 최소 1개 필요"},
    # -- misc -------------------------------------------------------------
    "detailsTools": {"en": "Tools: ", "ja": "ツール: ", "zh": "工具: ", "ko": "도구: "},
    "screenCleared": {"en": "(display cleared -- session kept)", "ja": "(表示クリア -- セッションは維持)", "zh": "(显示已清除 -- 会话保留)", "ko": "(화면 지움 -- 세션 유지)"},
    "sessionReset": {"en": "(session reset -- new conversation)", "ja": "(セッションリセット -- 新規会話)", "zh": "(会话已重置 -- 新对话)", "ko": "(세션 재설정 -- 새 대화)"},
    "copyOk": {"en": "Copied", "ja": "コピーしました", "zh": "已复制", "ko": "복사됨"},
    "saved": {"en": "Saved", "ja": "保存しました", "zh": "已保存", "ko": "저장됨"},
    # -- additional UI pieces ---------------------------------------------
    "noRole": {"en": "(no role)", "ja": "(ロール未設定)", "zh": "(未设置角色)", "ko": "(역할 없음)"},
    "editRoles": {"en": "Edit roles...", "ja": "ロールを編集...", "zh": "编辑角色...", "ko": "역할 편집..."},
    "setRoleTitle": {"en": "Set role", "ja": "ロールを設定", "zh": "设置角色", "ko": "역할 설정"},
    "genRolesBtn": {"en": "Generate roles", "ja": "ロール自動生成", "zh": "生成角色", "ko": "역할 생성"},
    "genRolesTitle": {"en": "Analyse directory & propose role set", "ja": "ディレクトリを解析しロールセットを提案", "zh": "分析目录并建议角色集", "ko": "디렉토리 분석 후 역할 세트 제안"},
    "genRolesGen": {"en": "Generating...", "ja": "生成中...", "zh": "生成中...", "ko": "생성 중..."},
    "peersBtn": {"en": "Peers", "ja": "ピア", "zh": "对等端", "ko": "피어"},
    "peersBtnTitle": {"en": "Register cross-project peers", "ja": "クロスプロジェクトピアを登録", "zh": "注册跨项目对等端", "ko": "프로젝트 간 피어 등록"},
    "rolesBtn": {"en": "Roles", "ja": "ロール", "zh": "角色", "ko": "역할"},
    "rolesBtnTitle": {"en": "Edit role set", "ja": "ロールセットを編集", "zh": "编辑角色集", "ko": "역할 세트 편집"},
    "autoCoord": {"en": "Auto Coordination", "ja": "自動コーディネーション", "zh": "自动协作", "ko": "자동 협업"},
    "coordOff": {"en": "OFF", "ja": "オフ", "zh": "关", "ko": "꺼짐"},
    "coordOn": {"en": "RUNNING", "ja": "実行中", "zh": "运行中", "ko": "실행 중"},
    "coordRefreshTitle": {"en": "Refresh", "ja": "更新", "zh": "刷新", "ko": "새로고침"},
    "coordInject": {"en": "Inject", "ja": "注入", "zh": "注入", "ko": "주입"},
    "coordInjectTitle": {"en": "Send instruction to Manager", "ja": "マネージャに指示を送信", "zh": "向管理者发送指示", "ko": "관리자에게 지시 전송"},
    "coordInjectPrompt": {"en": "Instruction for Manager to classify and dispatch:", "ja": "マネージャが分類・分派する指示を入力:", "zh": "输入管理者分类并分派的指示:", "ko": "관리자가 분류·분배할 지시 입력:"},
    "coordStartFirst": {"en": "Start coordination first", "ja": "先にコーディネーションを開始してください", "zh": "请先启动协作", "ko": "먼저 협업을 시작하세요"},
    "coordFailStart": {"en": "Failed to start coordination", "ja": "コーディネーション開始に失敗", "zh": "启动协作失败", "ko": "협업 시작 실패"},
    "peerChannel": {"en": "Peer", "ja": "ピア", "zh": "Peer", "ko": "Peer"},
    "peerChannelTitle": {"en": "Open peer_channel.md — free-form cross-role board", "ja": "peer_channel.md を開く — 横断フリーボード", "zh": "打开 peer_channel.md — 跨角色自由板", "ko": "peer_channel.md 열기 — 역할 간 자유 게시판"},
    "noProjectForPeer": {"en": "Select a project first.", "ja": "先にプロジェクトを選択してください", "zh": "请先选择一个项目", "ko": "먼저 프로젝트를 선택하세요"},
    "peerChannelMissing": {"en": "peer_channel.md not found at", "ja": "peer_channel.md が見つかりません:", "zh": "未找到 peer_channel.md:", "ko": "peer_channel.md 을 찾을 수 없습니다:"},
    "deployLabel": {"en": "Deploy", "ja": "デプロイ", "zh": "部署", "ko": "배포"},
    "mrrLabel": {"en": "MRR", "ja": "MRR", "zh": "MRR", "ko": "MRR"},
    "sTierLabel": {"en": "S tier", "ja": "Sティア", "zh": "S 级", "ko": "S 티어"},
    "qualityLabel": {"en": "Quality", "ja": "品質", "zh": "质量", "ko": "품질"},
    "blockerNeed": {"en": "blocker(s) -- user decision needed", "ja": "件のブロッカー -- ユーザー判断が必要", "zh": "个阻塞项 -- 需要用户决定", "ko": "개 블로커 -- 사용자 결정 필요"},
    "blockerOpenBtn": {"en": "open", "ja": "開く", "zh": "打开", "ko": "열기"},
    "alertNoOtherTabs": {"en": "No other tabs yet. Open more tabs first.", "ja": "他のタブがありません。まず新しいタブを開いてください。", "zh": "还没有其他标签。请先打开更多标签。", "ko": "다른 탭이 없습니다. 먼저 탭을 더 여세요."},
    "alertNoProjectOther": {"en": "This tab has no project. Select a project folder first.", "ja": "このタブにはプロジェクトがありません。先にプロジェクトフォルダを選択してください。", "zh": "此标签未关联项目。请先选择项目文件夹。", "ko": "이 탭에 프로젝트가 없습니다. 먼저 프로젝트 폴더를 선택하세요."},
    "alertSelectProject": {"en": "Select a project folder first.", "ja": "先にプロジェクトフォルダを選択してください。", "zh": "请先选择项目文件夹。", "ko": "먼저 프로젝트 폴더를 선택하세요."},
    "alertFailedTrigger": {"en": "Failed to trigger", "ja": "起動に失敗", "zh": "触发失败", "ko": "트리거 실패"},
    "alertFailedLoadPeers": {"en": "Failed to load peers", "ja": "ピアの読み込みに失敗", "zh": "加载对等端失败", "ko": "피어 로드 실패"},
    "alertSaveFail": {"en": "Save failed", "ja": "保存に失敗", "zh": "保存失败", "ko": "저장 실패"},
    "alertApplyFail": {"en": "Apply failed", "ja": "適用に失敗", "zh": "应用失败", "ko": "적용 실패"},
    "alertFailSaveRoles": {"en": "Failed to save roles", "ja": "ロールの保存に失敗", "zh": "保存角色失败", "ko": "역할 저장 실패"},
    "alertAtLeastOne": {"en": "At least one role required", "ja": "最低1つのロールが必要", "zh": "至少需要一个角色", "ko": "역할이 최소 1개 필요"},
    "alertNeedCoord": {"en": "At least one coordinator required", "ja": "最低1つのコーディネータが必要", "zh": "至少需要一个协调者", "ko": "코디네이터가 최소 1명 필요"},
    "alertNeedWorker": {"en": "At least one worker required", "ja": "最低1つのワーカーが必要", "zh": "至少需要一个工作者", "ko": "워커가 최소 1명 필요"},
    "errorPrefix": {"en": "Error: ", "ja": "エラー: ", "zh": "错误: ", "ko": "오류: "},
    "confirmResetRoles": {"en": "Reset roles to the default four (Manager / Developer / Marketing / Security)?", "ja": "ロールを既定の4つ (Manager/Developer/Marketing/Security) にリセットしますか?", "zh": "重置角色为默认四个 (Manager/Developer/Marketing/Security) ?", "ko": "역할을 기본 4개 (Manager/Developer/Marketing/Security) 로 재설정할까요?"},
    "peerTabLabel": {"en": "Peer tabs", "ja": "ピアタブ", "zh": "对等端标签", "ko": "피어 탭"},
    "peerTabsHintShort": {"en": "for @@name: dispatch", "ja": "@@name: で送信", "zh": "用于 @@name: 分发", "ko": "@@name: 디스패치용"},
    "browseTitle": {"en": "Browse", "ja": "参照", "zh": "浏览", "ko": "찾아보기"},
    "removeTitle": {"en": "Remove", "ja": "削除", "zh": "删除", "ko": "삭제"},
    "roleMenuIDLabel": {"en": "ID", "ja": "ID", "zh": "ID", "ko": "ID"},
    "colColorKind": {"en": "Color / Kind", "ja": "色 / 種別", "zh": "颜色 / 类型", "ko": "색상 / 종류"},
    "roleProposedBy": {"en": "Role proposal", "ja": "ロール提案", "zh": "角色提案", "ko": "역할 제안"},
    "roleProposalHint": {"en": "Claude analysed the project and proposed the following role set. Review and apply to replace the current roles, or dismiss.", "ja": "Claudeがプロジェクトを解析し以下のロールセットを提案しました。確認し適用するか却下してください。", "zh": "Claude 分析了项目并提议以下角色集。审核后应用替换当前角色，或取消。", "ko": "Claude가 프로젝트를 분석하여 다음 역할 세트를 제안했습니다. 검토 후 적용하거나 취소하세요."},
    "apply": {"en": "Apply", "ja": "適用", "zh": "应用", "ko": "적용"},
    "dismiss": {"en": "Dismiss", "ja": "却下", "zh": "取消", "ko": "취소"},
    "roleReqTitle": {"en": "New role requested by Manager", "ja": "マネージャから新規ロール要求", "zh": "管理者请求新角色", "ko": "관리자가 새 역할 요청"},
    "roleReqHint": {"en": "The Manager wants to spawn a new team. Approving will create the directory, register the role, and open a dedicated tab.", "ja": "マネージャが新チームを立ち上げたいと要求しています。承認するとディレクトリを作成し、ロールを登録し専用タブを開きます。", "zh": "管理者想要创建新团队。批准将创建目录、注册角色并打开专用标签。", "ko": "관리자가 새 팀을 만들고자 합니다. 승인하면 디렉터리 생성, 역할 등록, 전용 탭 열기가 수행됩니다."},
    "approveCreate": {"en": "Approve & Create", "ja": "承認して作成", "zh": "批准并创建", "ko": "승인 및 생성"},
    "crossPeerTitle": {"en": "Cross-project peers", "ja": "クロスプロジェクトピア", "zh": "跨项目对等端", "ko": "프로젝트 간 피어"},
    "crossPeerHint": {"en": "Register other projects you want to send @alias.role: messages to.", "ja": "@alias.role: でメッセージを送りたい他プロジェクトを登録", "zh": "注册要发送 @alias.role: 消息的其他项目", "ko": "@alias.role: 메시지를 보낼 다른 프로젝트 등록"},
    "myAlias": {"en": "My alias", "ja": "自身のエイリアス", "zh": "本项目别名", "ko": "내 별칭"},
    "aliasPlaceholder": {"en": "(auto from directory name)", "ja": "(ディレクトリ名から自動)", "zh": "(按目录名自动)", "ko": "(디렉토리 이름에서 자동)"},
    "projectPath": {"en": "Project path", "ja": "プロジェクトパス", "zh": "项目路径", "ko": "프로젝트 경로"},
    "inGuiPeerTabs": {"en": "In-GUI peer tabs", "ja": "GUI内ピアタブ", "zh": "GUI 内对等端标签", "ko": "GUI 내 피어 탭"},
    "inGuiPeerTabsHint": {"en": "Select tabs that this tab can address with @@<tab_name>:.", "ja": "このタブから @@<tab_name>: で呼べるタブを選択", "zh": "选择本标签可通过 @@<tab_name>: 访问的标签", "ko": "이 탭이 @@<tab_name>: 으로 호출 가능한 탭 선택"},
    "blockersTitle": {"en": "Blockers", "ja": "ブロッカー", "zh": "阻塞项", "ko": "블로커"},
    "sessionEndedDash": {"en": "--- Session ended ---", "ja": "--- セッションを終了しました ---", "zh": "--- 会话已结束 ---", "ko": "--- 세션이 종료되었습니다 ---"},
    "sessionResumedDash": {"en": "--- Resumed session ---", "ja": "--- セッションを再開しました ---", "zh": "--- 已恢复会话 ---", "ko": "--- 세션이 복원되었습니다 ---"},
    "sessionExpiredDash": {"en": "--- Session expired, starting new ---", "ja": "--- セッション期限切れ、新規開始 ---", "zh": "--- 会话过期，正在开始新会话 ---", "ko": "--- 세션 만료, 새로 시작 ---"},
    "aliasPH": {"en": "alias", "ja": "エイリアス", "zh": "别名", "ko": "별칭"},
    "displayNamePH": {"en": "Display name", "ja": "表示名", "zh": "显示名称", "ko": "표시 이름"},
    "roleIdPH": {"en": "role_id", "ja": "role_id", "zh": "role_id", "ko": "role_id"},
    "sendBtnTitle": {"en": "Send (Enter)", "ja": "送信 (Enter)", "zh": "发送 (Enter)", "ko": "전송 (Enter)"},
    "tabSettingsTitle": {"en": "Tab settings", "ja": "タブ設定", "zh": "标签设置", "ko": "탭 설정"},
    "newTabTitle": {"en": "New tab (Ctrl+T)", "ja": "新規タブ (Ctrl+T)", "zh": "新建标签 (Ctrl+T)", "ko": "새 탭 (Ctrl+T)"},
    "modelTitle": {"en": "Model", "ja": "モデル", "zh": "模型", "ko": "모델"},
    "effortTitle": {"en": "Effort", "ja": "思考レベル", "zh": "思考等级", "ko": "사고 수준"},
    "langTitle": {"en": "Language", "ja": "言語", "zh": "语言", "ko": "언어"},
    "planModeTitle": {"en": "Plan (read-only)", "ja": "プラン (読み取り専用)", "zh": "计划 (只读)", "ko": "계획 (읽기 전용)"},
    "acceptEditsTitle": {"en": "Accept Edits (file ops only)", "ja": "編集自動承認 (ファイル操作のみ)", "zh": "接受编辑 (仅文件操作)", "ko": "편집 승인 (파일 작업만)"},
    "yoloTitle": {"en": "Auto-accept safe permissions", "ja": "安全な許可を自動承認", "zh": "自动接受安全权限", "ko": "안전한 권한 자동 승인"},
    "rolesHintHtml": {
        "en": "Roles are <b>per-project</b>. Coordinators dispatch to workers via <code>@role:</code>; CCO sits above coordinators for multi-team integration. IDs must be <code>a-z0-9_</code>. At least one coordinator and one worker are required.",
        "ja": "ロールは<b>プロジェクトごと</b>。コーディネータは<code>@role:</code>でワーカーへ分派、CCOはマルチチーム統合でコーディネータの上に位置します。IDは<code>a-z0-9_</code>のみ。最低1人のコーディネータと1人のワーカーが必要です。",
        "zh": "角色是<b>按项目</b>的。协调者通过<code>@role:</code>向工作者分发；CCO 位于协调者之上以支持多团队集成。ID 必须为<code>a-z0-9_</code>。至少需要一个协调者和一个工作者。",
        "ko": "역할은 <b>프로젝트별</b>. 코디네이터는 <code>@role:</code>로 워커에게 분배, CCO는 멀티팀 통합을 위해 코디네이터 위에 있습니다. ID는 <code>a-z0-9_</code>만. 최소 1명의 코디네이터와 1명의 워커가 필요합니다.",
    },
    "resetDefaults": {"en": "Reset to defaults", "ja": "既定に戻す", "zh": "重置为默认", "ko": "기본값 재설정"},
    "roleProposalHintHtml": {
        "en": "Claude analysed the project and proposed the following role set. Review and apply to replace the current roles, or dismiss.",
        "ja": "Claudeがプロジェクトを解析し以下のロールセットを提案しました。確認して適用するか、却下してください。",
        "zh": "Claude 分析了项目并提议以下角色集。审核后应用替换当前角色，或取消。",
        "ko": "Claude가 프로젝트를 분석하여 다음 역할 세트를 제안했습니다. 검토 후 적용하거나 취소하세요.",
    },
    "roleReqHintHtml": {
        "en": "The Manager wants to spawn a new team. Approving will create the directory, register the role, and open a dedicated tab.",
        "ja": "マネージャが新チームを立ち上げたいと要求しています。承認するとディレクトリを作成し、ロールを登録し専用タブを開きます。",
        "zh": "管理者想要创建新团队。批准将创建目录、注册角色并打开专用标签。",
        "ko": "관리자가 새 팀을 만들고자 합니다. 승인하면 디렉토리 생성, 역할 등록, 전용 탭 열기가 수행됩니다.",
    },
    "crossPeerHintHtml": {
        "en": "Register other projects you want to send <code>@alias.role:</code> messages to. Messages are appended to <code>&lt;target&gt;/.claude-harness/inbox/from_&lt;self&gt;.md</code> and forwarded to the role's tab if it is running.",
        "ja": "<code>@alias.role:</code>でメッセージを送りたい他プロジェクトを登録します。メッセージは<code>&lt;target&gt;/.claude-harness/inbox/from_&lt;self&gt;.md</code>に追記され、対応ロールのタブが稼働中なら転送されます。",
        "zh": "注册要发送 <code>@alias.role:</code> 消息的其他项目。消息将追加到 <code>&lt;target&gt;/.claude-harness/inbox/from_&lt;self&gt;.md</code> ，若对应角色标签运行中则转发。",
        "ko": "<code>@alias.role:</code> 메시지를 보낼 다른 프로젝트 등록. 메시지는 <code>&lt;target&gt;/.claude-harness/inbox/from_&lt;self&gt;.md</code>에 추가되고 대상 역할 탭이 실행 중이면 전달됩니다.",
    },
    "fieldProject": {"en": "Project", "ja": "プロジェクト", "zh": "项目", "ko": "프로젝트"},
    "fieldRoleId": {"en": "Role id", "ja": "ロールID", "zh": "角色 ID", "ko": "역할 ID"},
    "fieldName": {"en": "Name", "ja": "名前", "zh": "名称", "ko": "이름"},
    "fieldIcon": {"en": "Icon", "ja": "アイコン", "zh": "图标", "ko": "아이콘"},
    "fieldColor": {"en": "Color", "ja": "色", "zh": "颜色", "ko": "색상"},
    "fieldKind": {"en": "Kind", "ja": "種別", "zh": "类型", "ko": "종류"},
    "fieldSubDir": {"en": "Sub-dir", "ja": "サブディレクトリ", "zh": "子目录", "ko": "하위 디렉토리"},
    "fieldBrief": {"en": "Brief", "ja": "概要", "zh": "简介", "ko": "개요"},
    "inGuiPeerTabsHintHtml": {
        "en": "Select tabs that <b>this</b> tab can address with <code>@@&lt;tab_name&gt;:</code>. The body is written directly into the peer tab's PTY.",
        "ja": "<b>このタブ</b>が<code>@@&lt;tab_name&gt;:</code>で呼べるタブを選択。本文はピアタブのPTYへ直接書き込まれます。",
        "zh": "选择<b>本</b>标签可通过<code>@@&lt;tab_name&gt;:</code>访问的标签。消息体直接写入对等端标签的 PTY。",
        "ko": "<b>이 탭</b>이 <code>@@&lt;tab_name&gt;:</code>으로 호출할 수 있는 탭을 선택. 본문은 피어 탭의 PTY에 직접 기록됩니다.",
    },
    # -- New Project Wizard (2026-04-24) ---------------------------------
    "newProject": {"en": "+ New Project", "ja": "+ 新規プロジェクト", "zh": "+ 新项目", "ko": "+ 새 프로젝트"},
    "back": {"en": "Back", "ja": "戻る", "zh": "返回", "ko": "뒤로"},
    "wzTitle": {"en": "New Project Wizard", "ja": "新規プロジェクトウィザード", "zh": "新项目向导", "ko": "새 프로젝트 마법사"},
    "wzHint1": {"en": "Enter a brief overview and we'll propose the optimal role team.", "ja": "概要を入力すると最適なロール構成を提案します。", "zh": "输入简要概述，我们将提议最佳角色团队。", "ko": "간단한 개요를 입력하면 최적의 역할 팀을 제안합니다."},
    "wzType": {"en": "Type", "ja": "種別", "zh": "类型", "ko": "유형"},
    "wzOverview": {"en": "Overview", "ja": "概要", "zh": "概述", "ko": "개요"},
    "wzOverviewPH": {"en": "Brief description (1-3 lines)...", "ja": "簡潔な説明 (1〜3行)…", "zh": "简短描述 (1-3 行)…", "ko": "간단한 설명 (1-3줄)…"},
    "wzPath": {"en": "Path", "ja": "パス", "zh": "路径", "ko": "경로"},
    "wzPathPH": {"en": "/absolute/path/to/new/project", "ja": "/絶対パス/新規プロジェクト", "zh": "/绝对路径/新项目", "ko": "/절대경로/새프로젝트"},
    "wzPathNote": {"en": "Directory will be created if it doesn't exist.", "ja": "ディレクトリが存在しない場合は作成します。", "zh": "若目录不存在将自动创建。", "ko": "디렉토리가 없으면 자동으로 생성됩니다."},
    "wzChooseFolder": {"en": "Browse...", "ja": "参照…", "zh": "浏览…", "ko": "찾아보기…"},
    "wzTypeSaaS": {"en": "SaaS / Product", "ja": "SaaS / プロダクト", "zh": "SaaS / 产品", "ko": "SaaS / 제품"},
    "wzTypeBot": {"en": "Bot / Automation", "ja": "Bot / 自動化", "zh": "机器人 / 自动化", "ko": "봇 / 자동화"},
    "wzTypeContent": {"en": "Content / Media", "ja": "コンテンツ / メディア", "zh": "内容 / 媒体", "ko": "콘텐츠 / 미디어"},
    "wzTypeResearch": {"en": "Research / Analysis", "ja": "リサーチ / 分析", "zh": "研究 / 分析", "ko": "리서치 / 분석"},
    "wzTypeOther": {"en": "Other", "ja": "その他", "zh": "其他", "ko": "기타"},
    "wzPropose": {"en": "Propose roles", "ja": "ロールを提案", "zh": "提议角色", "ko": "역할 제안"},
    "wzProposing": {"en": "Proposing...", "ja": "提案中…", "zh": "提议中…", "ko": "제안 중…"},
    "wzCreate": {"en": "Create", "ja": "作成", "zh": "创建", "ko": "생성"},
    "wzCreating": {"en": "Creating...", "ja": "作成中…", "zh": "创建中…", "ko": "생성 중…"},
    "wzReviewHint": {"en": "Review the proposed roles. Edit if needed, then create the project.", "ja": "提案されたロールを確認・編集して「作成」してください。", "zh": "查看提议的角色，按需编辑后创建项目。", "ko": "제안된 역할을 확인/편집 후 프로젝트를 생성하세요."},
    "wzSrcAPI": {"en": "Proposed by AI", "ja": "AI による提案", "zh": "AI 提议", "ko": "AI 제안"},
    "wzSrcFallback": {"en": "Preset defaults", "ja": "プリセット", "zh": "预设默认", "ko": "프리셋 기본값"},
    "wzAutoStartCoord": {"en": "Start Auto Coordination", "ja": "Auto Coordination を開始", "zh": "启动自动协调", "ko": "Auto Coordination 시작"},
    "wzAlertPath": {"en": "Please enter a project path.", "ja": "プロジェクトパスを入力してください。", "zh": "请输入项目路径。", "ko": "프로젝트 경로를 입력하세요."},
    "wzErrorAPI": {"en": "Failed to propose roles", "ja": "ロール提案に失敗しました", "zh": "提议角色失败", "ko": "역할 제안 실패"},
    "wzErrorCreate": {"en": "Failed to create project", "ja": "プロジェクト作成に失敗しました", "zh": "创建项目失败", "ko": "프로젝트 생성 실패"},
}

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

# Merge the extended UI strings into _ERRORS so `_t(key)` resolves keys from
# both the original error block and the newly-added UI-text block.
_ERRORS.update(_STRINGS_EXTRA)
# Public alias that reads better when treated as a full i18n catalogue.
_STRINGS = _ERRORS


# ---------------------------------------------------------------------------
# Role Registry — user-editable coordination roles (replaces hardcoded 4)
# ---------------------------------------------------------------------------
DEFAULT_ROLES = [
    {"id": "manager",   "name": "Manager",   "icon": "\U0001F39B️", "color": "#a78bfa", "kind": "coordinator"},
    {"id": "developer", "name": "Developer", "icon": "\U0001F4BB",       "color": "#34d399", "kind": "worker"},
    {"id": "marketing", "name": "Marketing", "icon": "\U0001F4E2",       "color": "#fb923c", "kind": "worker"},
    {"id": "security",  "name": "Security",  "icon": "\U0001F6E1️", "color": "#f87171", "kind": "worker"},
]


# Metadata hints for auto-sync from <project>/.claude/roles/*.md.
# Keys = role id (filename stem), values = (name, icon, color, kind).
# Unknown roles fall back to a generic palette rotation (see _sync_from_dir).
KNOWN_ROLE_HINTS = {
    "manager":   ("Manager",   "\U0001F39B️", "#a78bfa", "coordinator"),
    "developer": ("Developer", "\U0001F4BB",        "#34d399", "worker"),
    "marketing": ("Marketing", "\U0001F4E2",        "#fb923c", "worker"),
    "security":  ("Security",  "\U0001F6E1️",  "#f87171", "worker"),
    "cco":       ("CCO",       "\U0001F9E0",        "#fed50b", "cco"),
    "pa":        ("PA",        "\U0001F9ED",        "#60a5fa", "cco"),
    "designer":  ("Designer",  "\U0001F3A8",        "#ec4899", "worker"),
    "qa":        ("QA",        "\U0001F9EA",        "#22d3ee", "worker"),
    "analyst":   ("Analyst",   "\U0001F4CA",        "#14b8a6", "worker"),
    "product":   ("Product",   "\U0001F3AF",        "#8b5cf6", "worker"),
    "legal":     ("Legal",     "⚖️",       "#d97706", "worker"),
    "content":   ("Content",   "\U0001F3AC",        "#f43f5e", "worker"),
    "growth":    ("Growth",    "\U0001F4C8",        "#10b981", "worker"),
    "docs":      ("Docs",      "\U0001F4DA",        "#6366f1", "worker"),
    "sre":       ("SRE",       "\U0001F6E0️",  "#ef4444", "worker"),
    "revops":    ("RevOps",    "\U0001F4B0",        "#eab308", "worker"),
    "reviewer":  ("Reviewer",  "\U0001F440",        "#fde047", "worker"),  # 2026-04-24: user-perspective reviewer
    "surfer":    ("Surfer",    "\U0001F3C4",        "#0ea5e9", "worker"),  # 2026-04-25: web research + fact-check (paired with analyst)
}

# Fallback palette for roles that don't have a hint entry (indexed by hash).
_FALLBACK_PALETTE = [
    "#a78bfa", "#34d399", "#fb923c", "#f87171", "#22d3ee",
    "#ec4899", "#8b5cf6", "#14b8a6", "#f59e0b", "#6366f1",
]

# 2026-04-27 per-role default effort allocation. judgment-criticality × work
# frequency 軸で 5-tier 配分。session token 消費 ~50-60% 減 想定 (vs. 全 max)。
# user 直接 override は dropdown / `/effort` で上書き可、本 default は新 role
# 割当時 + "apply role defaults" migration で適用される。
ROLE_DEFAULT_EFFORTS: dict[str, str] = {
    # max — strategic dispatcher, decision quality compounds across all roles
    "manager":   "max",
    # xhigh — judgment-critical specialists (audit / numbers / legal / sec / strategy)
    "cco":       "xhigh",
    "analyst":   "xhigh",
    "legal":     "xhigh",
    "security":  "xhigh",
    "product":   "xhigh",
    # high — production judgment with bounded scope
    "developer": "high",
    "revops":    "high",
    "reviewer":  "high",
    "qa":        "high",
    # medium — production-heavy / visual / comms
    "pa":        "medium",
    "marketing": "medium",
    "content":   "medium",
    "docs":      "medium",
    "designer":  "medium",
    "growth":    "medium",
    # low — mechanical / tool-heavy ops
    "sre":       "low",
    "surfer":    "low",
}
_VALID_EFFORTS = {e[0] for e in EFFORT_LEVELS}  # {"max", "xhigh", "high", "medium", "low"}

# Wizard presets by project_type. Each preset MUST satisfy RoleRegistry.set_all():
# exactly one coordinator + at least one worker. Used as fallback when the
# Anthropic API is unavailable or returns an invalid proposal.
WIZARD_TYPE_DEFAULTS: dict[str, list[dict]] = {
    "saas": [
        {"id": "manager",   "name": "Manager",   "icon": "\U0001F39B", "color": "#a78bfa", "kind": "coordinator"},
        {"id": "developer", "name": "Developer", "icon": "\U0001F4BB", "color": "#34d399", "kind": "worker"},
        {"id": "security",  "name": "Security",  "icon": "\U0001F6E1", "color": "#f87171", "kind": "worker"},
        {"id": "marketing", "name": "Marketing", "icon": "\U0001F4E2", "color": "#fb923c", "kind": "worker"},
    ],
    "bot": [
        {"id": "manager",   "name": "Manager",   "icon": "\U0001F39B", "color": "#a78bfa", "kind": "coordinator"},
        {"id": "developer", "name": "Developer", "icon": "\U0001F4BB", "color": "#34d399", "kind": "worker"},
        {"id": "security",  "name": "Security",  "icon": "\U0001F6E1", "color": "#f87171", "kind": "worker"},
    ],
    "content": [
        {"id": "editor",      "name": "Editor",      "icon": "\U0001F4DD", "color": "#a78bfa", "kind": "coordinator"},
        {"id": "researcher",  "name": "Researcher",  "icon": "\U0001F50D", "color": "#34d399", "kind": "worker"},
        {"id": "distributor", "name": "Distributor", "icon": "\U0001F4E4", "color": "#fb923c", "kind": "worker"},
    ],
    "research": [
        {"id": "analyst",  "name": "Analyst",  "icon": "\U0001F4CA", "color": "#a78bfa", "kind": "coordinator"},
        {"id": "reviewer", "name": "Reviewer", "icon": "\U0001F440", "color": "#34d399", "kind": "worker"},
    ],
    "other": [
        {"id": "manager",   "name": "Manager",   "icon": "\U0001F39B", "color": "#a78bfa", "kind": "coordinator"},
        {"id": "developer", "name": "Developer", "icon": "\U0001F4BB", "color": "#34d399", "kind": "worker"},
    ],
}


# ---------------------------------------------------------------------------
# Routing — GUI display-based role derivation (2026-04-24, User directive)
# ---------------------------------------------------------------------------
# User 2026-04-24: 「タブの role 判別は gui 上の表示で行えばいい」.
# Display (tab.name) is the single source of truth; stored tab.role is a cache
# that may drift (new role added mid-session, tab renamed, etc.). When the two
# diverge, dispatches silently drop to `skip_file` events and tasks retry
# forever without surface — this is the "claude code gui 上で適切なとこに流
# れてない感じ" report.
#
# _role_from_tab_display(tab_name, registry=None) derives the role id from
# the visible tab name. Callers use it as the primary lookup and fall back to
# tab.role on None. See research/infra/gui_routing_audit_2026-04-24.md for
# full root-cause analysis and fix plan.
# ---------------------------------------------------------------------------

ROUTING_DISPLAY_FIRST = True    # True: derive role from tab.name first, tab.role as fallback
ROUTING_AUDIT_ENABLED = False   # True: enable [ROUTING-AUDIT] ACK + re-fire (Phase 3, dormant until user flip)
AUTO_RENAME_ON_ROLE_SET = True # 26-04-26: True = set_tab_role auto-renames tab to "<icon> <RoleName>"; False = legacy (manual rename only)


def _role_display_name(role_def: "dict | None") -> str:
    """Compose a display name for a tab from a RoleRegistry role dict.

    Format: "<icon> <Name>" (icon present) or "<Name>" (icon empty/absent).
    Returns empty string when role_def is None/missing required fields, so
    callers can short-circuit and skip rename. Strips trailing whitespace
    around concatenation; never returns leading-space results.
    """
    if not role_def:
        return ""
    name = str(role_def.get("name", "")).strip()
    if not name:
        return ""
    icon = str(role_def.get("icon", "")).strip()
    return (icon + " " + name).strip() if icon else name


def _role_from_tab_display(tab_name: str, registry=None) -> "str | None":
    """Derive role id from a tab's GUI display name.

    Match chain (first win):
      1. Exact match against KNOWN_ROLE_HINTS key (lowercase alnum form)
      2. Match against KNOWN_ROLE_HINTS[rid][0] display name
      3. Registry id exact match (covers custom roles from .claude/roles/*.md)
      4. Substring match (bidirectional) against KNOWN_ROLE_HINTS keys
      5. Fuzzy match via difflib.get_close_matches (cutoff=0.75)

    Returns matched role id (lowercase alnum) or None. Callers fall back to
    tab.role on None to preserve backward-compat when ROUTING_DISPLAY_FIRST
    flag is True but the display-derived match fails.
    """
    if not tab_name:
        return None
    m = re.search(r'[A-Za-z]', tab_name)
    if not m:
        return None
    stripped = tab_name[m.start():].strip()
    normalized = re.sub(r'[^a-z0-9]', '', stripped.lower())
    if not normalized:
        return None
    if normalized in KNOWN_ROLE_HINTS:
        return normalized
    for rid, hint in KNOWN_ROLE_HINTS.items():
        if hint[0].lower().replace(' ', '') == normalized:
            return rid
    if registry is not None:
        try:
            for rid in registry.ids():
                if rid == normalized:
                    return rid
        except Exception:
            pass
    # Alpha-prefix match: strip trailing digits/variant markers before substring
    # comparison so "Dev-01", "Sec-3", "Reviewer-02" still match. Extract the
    # leading alpha run and repeat the substring/prefix checks against it.
    alpha_prefix = ""
    m2 = re.match(r'([a-z]+)', normalized)
    if m2:
        alpha_prefix = m2.group(1)
    candidates = [normalized]
    if alpha_prefix and alpha_prefix != normalized:
        candidates.append(alpha_prefix)
    for cand in candidates:
        for rid in KNOWN_ROLE_HINTS:
            if rid in cand or cand in rid:
                return rid
    try:
        import difflib
        pool = list(KNOWN_ROLE_HINTS.keys())
        if registry is not None:
            try:
                pool.extend(r for r in registry.ids() if r not in pool)
            except Exception:
                pass
        for cand in candidates:
            matches = difflib.get_close_matches(cand, pool, n=1, cutoff=0.75)
            if matches:
                return matches[0]
    except Exception:
        pass
    return None


def _role_effective(tab, registry=None) -> str:
    """Resolve the effective role of a tab using the display-first fallback chain.

    Respects ROUTING_DISPLAY_FIRST flag:
      - True (default)  : try _role_from_tab_display(tab.name); fall back to tab.role
      - False           : return tab.role directly (legacy behavior for rollback)

    Returns empty string if no role can be derived (tab has no role assigned
    and display name doesn't match any known role).
    """
    if ROUTING_DISPLAY_FIRST:
        derived = _role_from_tab_display(getattr(tab, "name", "") or "", registry)
        if derived:
            return derived
    return getattr(tab, "role", "") or ""


class RoleRegistry:
    """User-editable registry of coordination roles, scoped to a project.

    Persisted to `<project>/.claude-harness/roles.json` when `project_path` is
    given, else to the global `~/.claude-code-gui/roles.json` (fallback for
    tabs without a project). Preserves the single-coordinator dispatch model
    (kind="coordinator" dispatches to kind="worker") and the CCO tier
    (kind="cco" sits above coordinators, dispatches across them).

    Dispatch regex and queue-section regex are generated dynamically from the
    current worker list so AutoCoordinator never sees a hardcoded role name.
    """

    VALID_KINDS = ("worker", "coordinator", "cco")

    def __init__(self, project_path: str = ""):
        self.project_path = project_path or ""
        self._roles: list[dict] = []
        self.load()

    @property
    def save_path(self) -> Path:
        if self.project_path:
            return Path(self.project_path) / ".claude-harness" / "roles.json"
        return ROLES_FILE

    def load(self):
        data = _load_json(self.save_path, None)
        if isinstance(data, dict) and isinstance(data.get("roles"), list) and data["roles"]:
            cleaned = []
            seen = set()
            for raw in data["roles"]:
                if not isinstance(raw, dict):
                    continue
                r = self._clean(raw)
                if not r["id"] or r["id"] in seen:
                    continue
                seen.add(r["id"])
                cleaned.append(r)
            if any(r["kind"] == "coordinator" for r in cleaned) and any(r["kind"] == "worker" for r in cleaned):
                self._roles = cleaned
                self._sync_from_dir()
                return
        # Missing / corrupt / incomplete — seed defaults
        self._roles = [self._clean(r) for r in DEFAULT_ROLES]
        self._sync_from_dir()
        self.save()

    def _sync_from_dir(self) -> None:
        """Auto-merge any role files found in <project>/.claude/roles/*.md.

        Source of truth = the filesystem. If the working directory defines
        additional roles (e.g., cco.md, designer.md, content.md) that are
        not yet in the registry, add them with sensible defaults so they
        become selectable in the GUI role menu without requiring manual
        registry edits. Existing entries are preserved (user-customized
        icon/color/kind survive).

        Metadata precedence:
          1. Existing registry entry (user's customization wins)
          2. KNOWN_ROLE_HINTS table (same palette as seeded roles.json)
          3. Auto-generated (name from "# Role: X" heading if present,
             icon from rid[0].upper(), color from _FALLBACK_PALETTE)

        Safe to call repeatedly; idempotent.
        """
        if not self.project_path:
            return
        roles_dir = Path(self.project_path) / ".claude" / "roles"
        if not roles_dir.is_dir():
            return
        existing_ids = {r["id"] for r in self._roles}
        added = False
        # 2026-04-27 Layer 2: discover roles from BOTH the legacy flat layout
        # (`.claude/roles/<role>.md`) AND the new 4-file layout
        # (`.claude/roles/<role>/instructions.md`). Legacy is preserved; new
        # layout becomes additive. Subdirs without instructions.md are skipped.
        candidate_paths: list[tuple[str, Path]] = []
        try:
            for entry in sorted(roles_dir.iterdir()):
                if entry.is_file() and entry.suffix == ".md":
                    candidate_paths.append((entry.stem, entry))
                elif entry.is_dir() and entry.name not in {"archive", "__pycache__"}:
                    inst = entry / "instructions.md"
                    if inst.exists():
                        candidate_paths.append((entry.name, inst))
        except Exception:
            return
        for rid_raw, mf in candidate_paths:
            rid_norm = rid_raw.strip().lower()
            rid = "".join(ch for ch in rid_norm if ch.isalnum() or ch == "_")
            if not rid or rid in existing_ids:
                continue
            hint = KNOWN_ROLE_HINTS.get(rid)
            if hint:
                name, icon, color, kind = hint
            else:
                name = self._extract_name_from_md(mf) or rid.title()
                icon = "\U0001F539"
                color = _FALLBACK_PALETTE[abs(hash(rid)) % len(_FALLBACK_PALETTE)]
                kind = "worker"
            self._roles.append(self._clean({
                "id": rid, "name": name, "icon": icon,
                "color": color, "kind": kind,
            }))
            existing_ids.add(rid)
            added = True
        if added:
            try:
                self.save()
            except Exception:
                pass  # Non-fatal: registry is still correct in-memory.

    @staticmethod
    def _extract_name_from_md(path: Path) -> str:
        """Pull the role's human name from a `# Role: X` first-line heading.

        Matches both `# Role: Manager` and `# Role: CCO (Chief ...)` — the
        part after `Role:` up to the first `(` or end-of-line, trimmed.
        Returns empty string if no match (caller supplies fallback).
        """
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh):
                    if i > 5:  # Only scan first few lines.
                        break
                    m = re.match(r"^\s*#\s*Role\s*:\s*([^\(\n]+)", line, re.IGNORECASE)
                    if m:
                        return m.group(1).strip()
        except Exception:
            pass
        return ""

    def save(self):
        _save_json(self.save_path, {"roles": self._roles})

    def _clean(self, r: dict) -> dict:
        rid_raw = str(r.get("id", "")).strip().lower()
        rid = "".join(ch for ch in rid_raw if ch.isalnum() or ch == "_")
        kind_raw = str(r.get("kind", "")).strip().lower()
        kind = kind_raw if kind_raw in self.VALID_KINDS else "worker"
        color = str(r.get("color", "#a78bfa")).strip() or "#a78bfa"
        if not re.match(r"^#[0-9a-fA-F]{3,8}$", color):
            color = "#a78bfa"
        icon = str(r.get("icon", "\U0001F539"))
        if len(icon) > 6:
            icon = icon[:6]
        # 2026-04-27 default_effort field: validated against EFFORT_LEVELS ids.
        # Falls back to ROLE_DEFAULT_EFFORTS table when absent (auto-fill).
        eff_raw = str(r.get("default_effort", "")).strip().lower()
        if eff_raw not in _VALID_EFFORTS:
            eff_raw = ROLE_DEFAULT_EFFORTS.get(rid, "high")
        return {
            "id": rid,
            "name": str(r.get("name", rid)).strip() or (rid.title() if rid else "Role"),
            "icon": icon or "\U0001F539",
            "color": color,
            "kind": kind,
            "default_effort": eff_raw,
        }

    # -- queries --------------------------------------------------------------

    def all(self) -> list[dict]:
        return [dict(r) for r in self._roles]

    def ids(self) -> list[str]:
        return [r["id"] for r in self._roles]

    def workers(self) -> list[str]:
        return [r["id"] for r in self._roles if r["kind"] == "worker"]

    def coordinators(self) -> list[str]:
        return [r["id"] for r in self._roles if r["kind"] == "coordinator"]

    def ccos(self) -> list[str]:
        return [r["id"] for r in self._roles if r["kind"] == "cco"]

    def by_id(self, rid: str) -> dict | None:
        return next((dict(r) for r in self._roles if r["id"] == rid), None)

    def is_valid(self, rid: str) -> bool:
        if not rid:
            return True  # empty = no role assigned
        return any(r["id"] == rid for r in self._roles)

    # -- mutation -------------------------------------------------------------

    def set_all(self, roles: list) -> tuple[bool, str]:
        if not isinstance(roles, list):
            return False, "roles must be a list"
        cleaned: list[dict] = []
        seen: set[str] = set()
        for raw in roles:
            if not isinstance(raw, dict):
                continue
            r = self._clean(raw)
            if not r["id"] or r["id"] in seen:
                continue
            seen.add(r["id"])
            cleaned.append(r)
        if not cleaned:
            return False, "At least one role is required"
        if not any(r["kind"] == "coordinator" for r in cleaned):
            return False, "At least one coordinator role required"
        if not any(r["kind"] == "worker" for r in cleaned):
            return False, "At least one worker role required"
        self._roles = cleaned
        self.save()
        return True, ""

    # -- regex helpers --------------------------------------------------------

    def dispatch_regex(self) -> re.Pattern:
        """@role: dispatch pattern. Dynamic based on worker list.

        26-05-05 sibling fix to PeerBridge TAB_DISPATCH_RE: the greedy
        `(.+?)` + DOTALL/MULTILINE body capture vacuumed up later screen
        content, so a `/role manager` (or any slash command) typed on a
        line after an existing `@<role>:` directive ended up embedded in
        the dispatch body. The receiver tab's Claude then ran the slash
        command as if the operator had typed it there. Mitigation is
        the same single-line termination as PeerBridge — bound the body
        to one line so the dispatch stops at the first newline. Multi-
        line dispatch was never a documented contract; dedup keys on
        body[:300] still work, and operational fix priority outweighs
        preserving an undocumented multi-line capture habit.
        """
        workers = self.workers()
        if not workers:
            return re.compile(r"(?!x)x")  # never-match
        alt = "|".join(re.escape(w) for w in workers)
        return re.compile(
            rf"@({alt})\s*:\s*([^\n]+)",
            re.IGNORECASE,
        )

    def queue_section_regex(self) -> re.Pattern:
        """`## [ts] to:<role>[,<role2>,...] priority:<P>` section header regex.

        2026-04-23: comma-separated multi-role support. Examples:
        - `## [ts] to:developer priority:S` — single role
        - `## [ts] to:developer,security priority:S` — multi-role, task
          replicated per role in _parse_queue_tasks (dedup by task_id:role)
        """
        workers = self.workers()
        if not workers:
            return re.compile(r"(?!x)x")
        alt = "|".join(re.escape(w) for w in workers)
        return re.compile(
            rf"^##\s+\[[^\]]+\]\s+to:((?:{alt})(?:\s*,\s*(?:{alt}))*)\s+priority:([SABC])",
            re.IGNORECASE,
        )


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
    # Multi-role autonomous system
    yolo_mode: bool = False  # auto-accept safe permission prompts
    role: str = ""  # role id from this tab's project's RoleRegistry
    # In-GUI peer tab ids — for @peerName: cross-tab dispatch (PeerBridge layer A)
    peer_tabs: list = field(default_factory=list)
    _last_yolo_accept: float = field(default=0.0, repr=False)
    # Observability: per-reason skip-event dedup timestamps (reason+payload → last emit)
    _last_yolo_skip: dict = field(default_factory=dict, repr=False)

    def serialize(self) -> dict:
        return {
            "id": self.id, "name": self.name, "project_path": self.project_path,
            "session_id": self.session_id, "model": self.model, "effort": self.effort,
            "max_turns": self.max_turns, "custom_flags": self.custom_flags,
            "system_prompt": self.system_prompt,
            "permission_mode": self.permission_mode,
            "allowed_tools": self.allowed_tools,
            "yolo_mode": self.yolo_mode,
            "role": self.role,
            "peer_tabs": list(self.peer_tabs or []),
            "messages": self.messages[-30:],
            "screen_content": self.screen_content[-20000:] if self.screen_content else "",
        }

    @classmethod
    def deserialize(cls, d: dict) -> "ChatTab":
        tab = cls()
        for k in ("id", "name", "project_path", "session_id", "model",
                   "effort", "max_turns", "custom_flags", "system_prompt",
                   "permission_mode", "allowed_tools", "yolo_mode", "role",
                   "peer_tabs", "messages", "screen_content"):
            if k in d:
                setattr(tab, k, d[k])
        return tab


# ---------------------------------------------------------------------------
# Harness prefix strict validator (pure helper)
# ---------------------------------------------------------------------------
# CLAUDE.md "Authentication 例外2" declares a whitelist of prefixes that the
# receiving Claude auto-runs without "by suz". If a cross-tab message's
# first line doesn't match one of these exactly, the receiver hits the auth
# check and stalls waiting for user approval — the user-visible "止まる"
# symptom. Strict validation at send-time catches malformed prefixes
# (e.g. empty tab.name producing `@@]`, whitespace-eaten names, prefix
# drift) before they hit the PTY.
#
# Patterns are ANCHORED to start-of-line. PEER-DISPATCH is the strictest:
# requires `@@` followed by at least one alnum/_/- character so `@@]`
# (empty-name bug) is rejected.
_HARNESS_PREFIX_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"^\[USER-INSTRUCTION\]"),
    re.compile(r"^\[MANAGER-DISPATCH\b"),
    re.compile(r"^\[WORKER-REPORT from [A-Za-z][A-Za-z0-9_\-]*"),
    re.compile(r"^\[PEER-DISPATCH from @@[A-Za-z0-9_\-]+\]"),
    re.compile(r"^\[PEER-INBOX from [A-Za-z0-9_\-]+"),
    re.compile(r"^\[MD-WATCH from [A-Za-z0-9_\-/.]+"),
    re.compile(r"^\[CC: "),  # Manager-observed peer CC traffic.
    re.compile(r"^\[GRAPH-CTX\b"), # mitigation: bidirectional asymmetry closure with receiver _HARNESS_PREFIX_RE.
)


def _validate_harness_prefix(first_line: str) -> tuple[bool, str]:
    """Check first-line prefix against the CLAUDE.md harness whitelist.

    Returns (ok, reason). `reason` is a telemetry key — one of:
      ok                     — recognised prefix, safe to send
      empty_prefix           — caller passed empty/None prefix
      peer_empty_name        — `[PEER-DISPATCH from @@]` (tab.name was empty)
      peer_invalid_name      — `@@<name>` has chars outside [A-Za-z0-9_-]
      worker_missing_role    — `[WORKER-REPORT from ]` (role blank)
      unknown_prefix         — no whitelist pattern matched at all
    """
    if not first_line:
        return (False, "empty_prefix")
    # Pattern-specific diagnostics: if it *looks* like a harness prefix but
    # is malformed, give the caller a precise reason (easier to debug than
    # the generic "unknown_prefix").
    if first_line.startswith("[PEER-DISPATCH from @@]"):
        return (False, "peer_empty_name")
    if first_line.startswith("[PEER-DISPATCH from @@"):
        m = re.match(r"^\[PEER-DISPATCH from @@([^\]]*)\]", first_line)
        if m and not re.fullmatch(r"[A-Za-z0-9_\-]+", m.group(1)):
            return (False, "peer_invalid_name")
    if first_line.startswith("[WORKER-REPORT from ]"):
        return (False, "worker_missing_role")
    for pat in _HARNESS_PREFIX_PATTERNS:
        if pat.match(first_line):
            return (True, "ok")
    return (False, "unknown_prefix")


def _safe_tab_name(tab) -> str:
    """Sanitized display name suitable for `@@<name>` peer-dispatch prefix.

    Strips whitespace and non-[A-Za-z0-9_-] chars. Falls back to
    `tab-<short_id>` if the result is empty so the prefix regex never
    degenerates to `@@]` (the primary cause of the "@@prefix ついてない"
    stall symptom).
    """
    raw = getattr(tab, "name", "") or ""
    sanitized = re.sub(r"[^A-Za-z0-9_\-]", "", raw)
    if sanitized:
        return sanitized
    tid = getattr(tab, "id", "") or ""
    return f"tab-{tid[:8]}" if tid else "unknown"


# ---------------------------------------------------------------------------
# MD-WATCH routing (2026-04-24: user-surfaced self-echo fix)
# ---------------------------------------------------------------------------
# Before this fix: _poll_file_generic and _run_md_watch_fallback always sent
# [MD-WATCH from coordination/<file>] to the Manager tab only. Manager was
# also the primary writer of most coord files, so every Manager write caused
# a self-echo (Manager seeing its own writes propagated back). Worker tabs
# never received MD-WATCH and had no signal about coord file changes unless
# they manually polled.
#
# This fix introduces file-owner-aware routing:
#   1. Each coord file has a known owner (the role that primarily writes it).
#   2. MD-WATCH is fanned out to ALL role tabs in the project EXCEPT the
#      owner (self-echo prevention).
#   3. Shared files (no single owner) go to all role tabs.
#
# Feature flags allow rollback to legacy behavior without code revert.
# ---------------------------------------------------------------------------

MD_WATCH_WORKER_FANOUT = True    # True: send to all role tabs; False: manager only (legacy)
MD_WATCH_OWNER_EXCLUSION = True  # True: skip owner tab (self-echo prevention)
# 2026-04-27 content-aware routing — refine targets by parsing delta_text for
# `(to:roles)` / @mentions / T### tokens. Reduces mgr/cco bottleneck on
# files like blockers.md / decisions_log.md (skip-reduction, user feedback).
MD_WATCH_CONTENT_ROUTING = True
MAILBOX_ENABLED = True # 2026-04-26: True = autocoord peer hints write to peer_<role>.md (per-role mailbox); False = legacy single peer_channel.md broadcast
# Legacy single-file queue dispatch — retired. All dispatch delivery flows
# through per-role peer_<role>.md mailboxes (MD-WATCH fanout).
# False (default) = _poll_file_dispatch early-return; True = legacy parser
# (emergency rollback only).
LEGACY_QUEUE_DISPATCH_ENABLED = False
MD_WATCH_DEBUG_FOOTER = False # 2026-04-26 True = include "[MD-WATCH-FOOTER ...]" debug line + scripted "Review and adjust your queue" prompt; False (default) = strip both (model-side default behavior is sufficient)

# 2026-04-26 (Phase 1 token economy): prompt cache + MD-WATCH coalescing.
PROMPT_CACHE_ENABLED = True       # True: attach cache_control markers on direct anthropic API calls ; False: legacy uncached path
PROMPT_CACHE_SYSTEM_TTL = "1h"    # 1h TTL on stable system prompt (write 2× cost, but 1 write per session)
PROMPT_CACHE_TOOLS_TTL = "5m"     # 5m TTL on tools (default — short, but tools rarely change mid-session)
PROMPT_CACHE_MIN_TOKENS = 4096    # Opus 4.7 min token per breakpoint (cookbook-confirmed)
MDWATCH_COALESCE_ENABLED = True    # True: 30s debounce + 90s ceiling on same-file MD-WATCH delta; False: emit immediately (legacy)
MDWATCH_COALESCE_DEBOUNCE_S = 30.0
MDWATCH_COALESCE_CEILING_S = 90.0

# 2026-04-26: MD-WATCH pointer mode (push→pull).
# True: emit `[MD-PULL <path> from_hash=<old> to_hash=<new> +N/-M lines L<a>-<b>]`
# instead of full delta body. Roles use Read tool with offset/limit to fetch
# the specific lines. Hash mismatch triggers tail-N fallback. Expected ~75%+
# token cut per dispatch. Composes with coalescing (content-layer vs.
# time-layer cuts).
MDWATCH_POINTER_MODE = False       # default off until soak-tested; set True to activate
MDWATCH_POINTER_TAIL_FALLBACK_LINES = 50

# 2026-04-27 GRAPH ENFORCEMENT — auto-inject [GRAPH-CTX] into selected dispatches.
# Forces roles to engage with graph memory whether they query manually or not.
# Limits: max 3 related nodes per dispatch, only files where context helps.
GRAPH_ENFORCE_ENABLED = True
GRAPH_ENFORCE_TOP_N = 3
# File-name → (does this dispatch type benefit from graph context?)
GRAPH_ENFORCE_FILES = {
    "blockers.md",          # escalations → mgr+cco need full context
}
# Peer mailbox files (peer_<role>.md) are also enriched (handled by prefix match).
GRAPH_ENFORCE_PEER_PREFIX = "peer_"

# 2026-04-27 Layer 0a: per-file pointer mode for
# audit/reference shared coord docs that benefit from hash-notify-style
# minimal signal but still need real-time presence (vs. complete silence).
# These files were no-broadcast as of 26-04-26 partial-close phase1
# item #1; pointer mode restores ~20-token signal so audit-chain owners see
# "something happened, fetch L<a>-<b> if relevant" without paying full delta.
MDWATCH_POINTER_MODE_FILES = {
    "decisions_log.md",       # audit log — manager+cco need awareness
    "tier_list.md",           # quality grades — manager+designer+product
    "project_portfolio.md",   # RICE allocation — manager+product+revops
    # 2026-04-27 protocol docs: broadcast-but-rare changes. Pointer mode
    # cuts a 2280-token full delta to ~360 (19 tabs × 20-token pointer).
    "PROTOCOL_V2.md",                  # core protocol SoT
    "PROTOCOL_V2_reference.md",        # lazy-load reference
    "boundary_arbitration_protocol.md",  # boundary spec (rare)
    "role_interaction_protocols.md",     # role interaction spec (rare)
}


# 2026-04-27: per-message full-body override for pointer-mode files.
# Senders prefix any line of the appended body with [FULL-DELIVERY] to bypass
# pointer mode for that specific dispatch. Use case: high-priority/urgent
# dispatches where the recipient must act immediately (vs deferred Read fetch).
# Token cost: same as pre- full body — reserve for time-sensitive use.
FULL_DELIVERY_MARKER = "[FULL-DELIVERY]"


def _md_watch_force_full_body(delta_text: str) -> bool:
    """Return True if delta_text contains FULL_DELIVERY_MARKER → bypass pointer."""
    return bool(delta_text) and FULL_DELIVERY_MARKER in delta_text


def _md_watch_pointer_mode_for(file_name: str) -> bool:
    """Return True if this file uses [MD-PULL] pointer mode (hash-notify).

    Sources of truth:
      1. Global toggle MDWATCH_POINTER_MODE (on = all eligible files pointer-mode).
      2. Per-file allowlist MDWATCH_POINTER_MODE_FILES (Layer 0a default).

    Note (2026-04-29 mgr direct fix): peer_<role>.md mailboxes were previously
    auto-included here (Tier 2 #5) and emitted `[MD-PULL ...]` on update. Per
    user request "起動時と同様に PEER-INBOX 形式で update も送る" the mailbox
    branch was migrated to the dedicated inbox_mode in `_md_watch_loop`
    (`elif _md_watch_mailbox_recipient(name):`), which emits the same
    `[PEER-INBOX from coordination/peer_<role>.md] ... Read tail-10 ...`
    format used by the spawn-time `_send_peer_inbox_hint`. Result: spawn and
    update notifications now share one canonical format (handler 統一)、role
    side は `Read tail-10` の単一動作で済む。This helper no longer covers
    peer_*.md — the routing is exclusive (inbox_mode catches them first via
    elif chain ordering — see `_md_watch_loop`).

    Per-message override: see _md_watch_force_full_body — caller must check it
    separately and bypass this branch when marker present in delta_text.
    """
    if MDWATCH_POINTER_MODE:
        return True
    name = file_name or ""
    if name in MDWATCH_POINTER_MODE_FILES:
        return True
    return False

# 2026-04-26 tool result auto-truncation thresholds.
# Used by _truncate_tool_result helper (callable utility for any future
# prompt-assembly layer). Bash output most often spikes; Grep is multi-file;
# generic catches everything else. Read tool already has its own truncation
# (2000 line cap) and is skipped here.
TOOL_TRUNC_BASH_LINES = 200
TOOL_TRUNC_GREP_LINES = 300
TOOL_TRUNC_GENERIC_LINES = 500
TOOL_TRUNC_KEEP_HEAD = 100      # lines kept from head
TOOL_TRUNC_KEEP_TAIL = 100      # lines kept from tail

# 2026-04-26 system reminder dedup interval.
REMINDER_MIN_INTERVAL_TURNS = 5

# 2026-04-26 skills list conditional injection — top-N default.
SKILLS_DEFAULT_TOP_N = 5


def _md_watch_file_owner(file_name: str) -> str | None:
    """Return the role that owns this coord file (primary writer).

    Owner tabs are excluded from MD-WATCH fanout to prevent self-echo.
    Returns None for shared files (blockers / peer_channel / decisions_log
    / tier_list / project_portfolio / etc.) — routing for those is decided
    by _md_watch_interest_targets (scoped or broadcast or no-broadcast).

    Examples:
      developer_report.md     → "developer"
      cco_brief.md            → "cco"
      peer_channel.md         → None (shared; no-broadcast policy in interest map)
      blockers.md             → None (shared; manager+cco interest)
      decisions_log.md        → None (shared; no-broadcast — session-start tail read)
    """
    if not file_name:
        return None
    # manager_queue.md: legacy queue file — no longer routed
    if file_name.endswith("_report.md"):
        return file_name[: -len("_report.md")]
    if file_name.endswith("_brief.md"):
        return file_name[: -len("_brief.md")]
    return None  # Shared files have no single owner.


def _md_watch_interest_targets(file_name: str) -> "set[str] | None":
    """Return the set of role IDs interested in changes to this shared file.

    Returns None for files NOT in the interest map → caller falls back to
    the standard broadcast (all role tabs minus owner) so existing behavior
    is preserved for unmapped files.

    Scopes shared coord files (decisions_log / blockers / briefs /
    tier_list / project_portfolio) so unrelated worker tabs no longer
    receive every change. Mailbox files (peer_<role>.md) are handled
    separately by _md_watch_mailbox_recipient.

    peer_channel.md is no-broadcast (task-end read only). Returns empty
    set so even if the primary _GENERIC_WATCH_EXCLUDE filter is bypassed,
    routing still yields zero targets (defense-in-depth).

    decisions_log/tier_list/project_portfolio are pointer-mode push
    (MDWATCH_POINTER_MODE_FILES allowlist — the message is a 1-2 line
    [MD-PULL] signal, ~20 tokens). boundary_rulings /
    peer_consultation_checklist stay fully no-broadcast (pure static
    reference, zero event-signal value).

    Rules:
      peer_channel.md             → set()  (no broadcast — task-end read)
      decisions_log.md            → mgr+cco       [pointer-mode]
      tier_list.md                → mgr+dsn+prd   [pointer-mode]
      project_portfolio.md        → mgr+prd       [pointer-mode]
      boundary_rulings.md         → set()  (no broadcast — 判例集 lookup)
      peer_consultation_checklist.md → set()  (no broadcast — static peer-trigger map)
      blockers.md                 → manager + cco (escalation — real-time matters)
      <role>_brief.md             → manager (briefs are mgr's exec input)
      everything else             → None (broadcast)
    """
    if not file_name:
        return None
    # Pointer-mode allowlist (decisions_log/tier_list/project_portfolio):
    # scoped + [MD-PULL] msg. boundary_rulings / peer_consultation_checklist /
    # peer_channel stay fully silent (pure static reference).
    if file_name in {
        "peer_channel.md",
        "boundary_rulings.md",
        "peer_consultation_checklist.md",
    }:
        return set()
    if file_name == "decisions_log.md":
        return {"manager", "cco"}
    if file_name == "tier_list.md":
        return {"manager", "designer", "product"}
    if file_name == "project_portfolio.md":
        return {"manager", "product", "revops"}
    if file_name == "blockers.md":
        return {"manager", "cco"}
    if file_name.endswith("_brief.md"):
        return {"manager"}
    # role_report no-broadcast: the WORKER-REPORT prefix
    # (_poll_file_reports) already pushes the digest to the manager; a raw
    # MD-WATCH push on top is redundant. Only the raw delta fanout is cut.
    if file_name.endswith("_report.md"):
        return set()
    return None


def _md_watch_mailbox_recipient(file_name: str) -> "str | None":
    """Return the role that EXCLUSIVELY receives a per-role mailbox file.

    Files
    matching `peer_<role>.md` are role-private mailboxes — only that role's
    tab(s) receive the MD-WATCH delta. `peer_channel.md` is NOT a mailbox
    (returns None) — it's also no-broadcast per 2026-04-26 user directive,
    enforced by _GENERIC_WATCH_EXCLUDE + empty interest set (see
    _md_watch_interest_targets). Roles read it tail-20 at task end.

    Examples:
      peer_developer.md → "developer" (only developer tab receives)
      peer_manager.md   → "manager"
      peer_channel.md   → None (not a mailbox; no-broadcast policy elsewhere)
      developer_report.md → None (not a mailbox file)
      foo.md           → None
    """
    if not file_name:
        return None
    if not file_name.startswith("peer_") or not file_name.endswith(".md"):
        return None
    role_id = file_name[len("peer_"):-len(".md")]
    if not role_id or role_id == "channel":
        return None
    if not re.match(r"^[a-z][a-z0-9_-]*$", role_id):
        return None  # invalid id shape — treat as non-mailbox
    # 2026-04-28 critical fix: peer_dev.md / peer_lgl.md / peer_mgr.md /
    # peer_design.md 等の略称 file → tab role は full name (developer/legal/
    # manager/designer)。略称→フル展開しないと recipient match 0 で silent
    # drop = peer 通信完全死亡の主因。_ROLE_ABBREV_TO_FULL で正規化。
    return _ROLE_ABBREV_TO_FULL.get(role_id, role_id)


def _md_watch_targets(api, project_path: str, file_name: str,
                      delta_text: str = "") -> list:
    """Collect target tabs for MD-WATCH delivery in a project.

    Rules:
      - Mailbox file (`peer_<role>.md`, ): deliver ONLY to that role's
        tab(s) — overrides standard owner/fanout below.
      - Otherwise (standard fanout):
        - Include all running role tabs (tab.role non-empty, pty_session
          alive) whose project matches project_path.
        - Exclude the file owner tab (self-echo prevention), if
          MD_WATCH_OWNER_EXCLUSION is True.
        - If MD_WATCH_WORKER_FANOUT is False, only include the Manager tab
          (legacy behavior — kept for quick rollback).

    2026-04-27 content-aware routing (skip-reduction, mgr/cco bottleneck fix):
      When delta_text is provided AND MD_WATCH_CONTENT_ROUTING is True, the
      interest set is REFINED by content analysis (`(to:roles)` / @<role>
      mentions / graph-spread). This narrows audience for files like
      blockers.md and decisions_log.md so role tabs no longer receive every
      mgr+cco-default push regardless of body content.
    """
    # 2026-04-24 routing strict: derive role from tab display (User directive)
    registry = None
    try:
        if hasattr(api, "_roles_for"):
            registry = api._roles_for(project_path)
    except Exception:
        registry = None

    # Normalize project keys for comparison (consistent with every other
    # isolation site — PeerBridge/HarnessWatcher use _norm_project). Raw string
    # equality under-delivered when a sibling tab's path differed only in
    # drive-letter case or / vs \ separators.
    def _np(p):
        try:
            return api._norm_project(p) if hasattr(api, "_norm_project") else (p or "")
        except Exception:
            return p or ""
    proj_key = _np(project_path)

    # 2026-04-26 mailbox routing: peer_<role>.md → recipient role only.
    mailbox_role = _md_watch_mailbox_recipient(file_name)
    if mailbox_role:
        targets = []
        # 2026-04-28 mgr direct diag: track filter reasons. Stash on the api
        # object as a one-shot so the caller (_md_watch_process_coord_dir)
        # can include diag in the watchdog log when targets is empty.
        diag = {"role_mismatch": 0, "not_running": 0, "pty_dead": 0, "wrong_proj": 0}
        for tab in getattr(api, "_tabs", {}).values():
            if _np(getattr(tab, "project_path", "")) != proj_key:
                diag["wrong_proj"] += 1
                continue
            role = _role_effective(tab, registry)
            if role != mailbox_role:
                diag["role_mismatch"] += 1
                continue
            if not getattr(tab, "pty_session", None) or not tab.pty_session.running:
                diag["not_running"] += 1
                continue
            # high-sev defensive (26-04-28): also check pty.isalive — race
            # window between producer-thread exit and `running=False` write
            # (now closed in _producer post-loop). Belt-and-suspenders so a
            # dead PTY phantom can't slip into mailbox single-target list.
            try:
                pty = getattr(tab.pty_session, "pty", None)
                if pty and not pty.isalive():
                    diag["pty_dead"] += 1
                    continue
            except Exception:
                pass
            targets.append(tab)
        try:
            setattr(api, "_md_watch_last_mailbox_diag", (file_name, mailbox_role, diag))
        except Exception:
            pass
        return targets
    # 2026-04-26 follow-up: scoped interest filter for shared files
    # (decisions_log / blockers / briefs / tier_list / project_portfolio).
    # When set, restrict targets to roles in the interest set instead of
    # broadcasting to every role tab. None = standard broadcast preserved.
    interest = _md_watch_interest_targets(file_name)
    # 2026-04-27 content-aware refinement (mgr/cco bottleneck fix):
    # Routine entries with explicit `to:roles` / @mentions / task IDs route
    # to ACTUAL stakeholders, not the default mgr+cco audit chain. Skip
    # reduction is the primary goal (user feedback "skipが多すぎる").
    if MD_WATCH_CONTENT_ROUTING and delta_text:
        refined = _md_watch_resolve_targets_by_content(
            file_name, delta_text, interest, project_path,
        )
        # Only override if content yielded a non-default result that's also
        # non-empty (empty = full silence which we don't want here).
        if refined is not None and refined != interest and refined:
            interest = refined
    owner = _md_watch_file_owner(file_name) if MD_WATCH_OWNER_EXCLUSION else None
    targets = []
    for tab in getattr(api, "_tabs", {}).values():
        if getattr(tab, "project_path", "") != project_path:
            continue
        role = _role_effective(tab, registry)
        if not role:
            continue  # Skip unassigned tabs.
        if not getattr(tab, "pty_session", None) or not tab.pty_session.running:
            continue
        if interest is not None and role not in interest:
            continue  # Scoped file — role not in interest set.
        if MD_WATCH_OWNER_EXCLUSION and owner and role == owner:
            continue  # Owner wrote it — don't echo back.
        if not MD_WATCH_WORKER_FANOUT and role != "manager":
            continue  # Legacy mode: manager only.
        targets.append(tab)
    return targets


def _md_watch_self_exclusion_test() -> dict:
    """Self-test shim for `--md-watch-test` CLI flag.

    Delegates to tests/test_md_watch_routing.py (130 lines moved out 2026-04-26
    P5 to keep the production harness file lean). Returns the merged result
    of run_self_test() + test_bounded_seen_set_lru() so the CLI flag still
    surfaces every check in one report.
    """
    try:
        from tests.test_md_watch_routing import (
            run_self_test,
            test_bounded_seen_set_lru,
            test_phase2_context_cuts,
            test_role_report_no_broadcast,
            test_md_watch_no_review_prompt,
            test_coalescing_window,
            test_pointer_mode,
            test_state_persist,
            test_cache_helpers,
        )
        from tests.test_skill_list_inject import run_self_test as _skill_test
        from tests.test_tool_truncation import run_self_test as _trunc_test
        from tests.test_reminder_dedup import run_self_test as _rem_test
    except Exception as e:
        return {
            "passed": 0,
            "failed": 1,
            "details": [f"FAIL  test-import: {type(e).__name__}: {e}"],
        }
    results = [
        run_self_test(),
        test_bounded_seen_set_lru(),
        test_phase2_context_cuts(),
        test_role_report_no_broadcast(),
        test_md_watch_no_review_prompt(),
        test_coalescing_window(),
        test_pointer_mode(),
        test_state_persist(),
        test_cache_helpers(),
        _skill_test(),
        _trunc_test(),
        _rem_test(),
    ]
    return {
        "passed": sum(r["passed"] for r in results),
        "failed": sum(r["failed"] for r in results),
        "details": [line for r in results for line in r["details"]],
    }


# ---------------------------------------------------------------------------
# Yolo accept-gate (pure helper)
# ---------------------------------------------------------------------------
# Danger patterns examined ONLY within the current permission prompt box
# (NOT scrollback). Scope narrowing is handled by the caller; this helper
# only receives the already-extracted prompt text.
#
# Yolo auto-accepts ROUTINE work — cd / ls / dir / Get-ChildItem / cat /
# Get-Content / grep / Select-String / git status|log|diff|add|commit /
# python / node / npm install|run|ci / pytest / mkdir … — and stops ONLY for
# genuinely important, hard-to-reverse decisions, per CLAUDE.md carve-outs:
# destructive / deploy / publish / secrets / kill-by-name.
#
# Patterns are substring-matched against the LOWERCASED prompt box, so each
# must be specific enough not to fire on benign commands.
#  - REMOVED 2026-06-07 (user: "stop only for real decisions, not
#    powershell/cd"): bare "production" + "--prod" (hit file paths and
#    `npm i --production`) and bare "--force" (hit `npm cache clean --force`).
#    The dangerous git/deploy forms are kept in their specific shapes
#    (`push --force`, `vercel --prod`).
#  - REMOVED 2026-04-23 (gui-yolo-reliability-fix): "token"/".env"/
#    "credential"/"secret"/"api key"/bare "force"/bare "publish" — too common
#    in normal dev discussion. Yolo-ON means reading a .env auto-accepts.
_YOLO_DANGER_PATTERNS: tuple[str, ...] = (
    # -- destructive filesystem (POSIX + Windows / PowerShell) --
    "rm -rf", "rm -fr", "rm -r -f",
    "remove-item -recurse", "rmdir /s", "rd /s", "del /s",
    # -- destructive git (history / branch / remote rewrite) --
    "git reset --hard", "git clean -fd", "git clean -df", "git branch -d",
    "filter-branch", "push --force", "push -f", "--force-with-lease", "force-push",
    # -- destructive database --
    "drop table", "drop database", "truncate table", "delete from",
    # -- deploy / publish (irreversible / outward-facing) --
    "vercel deploy", "vercel --prod", "npm publish", "twine upload",
    "gh release create", "firebase deploy", "netlify deploy",
    # -- packages / secrets / auth / kill-by-name (CLAUDE.md prohibitions) --
    "pip uninstall", "gh auth logout", "gh secret delete", "gh secret remove",
    "stop-process -name", "taskkill /im",
)
_YOLO_APPROVE_MARKERS: tuple[str, ...] = (
    "yes", "allow", "proceed", "confirm", "accept", "はい", "許可", "承認",
)


def _eval_yolo_prompt(prompt_text: str, choices: list) -> tuple[bool, str | None, dict]:
    """Pure logic for Yolo auto-accept gate. Returns (ok, reason, payload).

    Args:
        prompt_text: lowercase text of ONLY the current permission prompt
                     box (must NOT include scrollback — caller is responsible
                     for scope narrowing)
        choices: list of [num, label] pairs as parsed from the prompt

    Returns:
        (True, None, {})                              — safe to auto-accept
        (False, "no_choices", {})                     — <2 choices
        (False, "no_approve_marker", {"label": ...}) — first choice isn't an approval
        (False, "danger_keyword", {"kw": ...})       — first matched danger kw in prompt
    """
    if len(choices) < 2:
        return (False, "no_choices", {})
    first_label = (choices[0][1] if choices else "").lower()
    if not any(m in first_label for m in _YOLO_APPROVE_MARKERS):
        return (False, "no_approve_marker", {"label": first_label[:60]})
    for kw in _YOLO_DANGER_PATTERNS:
        if kw in prompt_text:
            return (False, "danger_keyword", {"kw": kw})
    return (True, None, {})


# ---------------------------------------------------------------------------
# Quiescence Gate — AutoCoordinator must not write while Claude is generating.
# Pure helper for testability; PtySession.is_quiescent() is a thin wrapper.
# ---------------------------------------------------------------------------
# Spinner glyphs the Claude CLI renders while a message is streaming.
# watchdog (26-04-28): bounds consecutive deferrals for MD-WATCH dispatch
# so single-target mailbox files (peer_<role>.md) don't get permanently stuck
# when the recipient tab has a stale busy/permission marker. Module-level
# state — keyed by `key_prefix:path` (same as file_mtime / file_hash).
# Threshold = 8 polls × 2s sleep ≈ 16s before force-advance.
_MD_WATCH_DEFER_COUNT: dict[str, int] = {}
_MD_WATCH_DEFER_LIMIT: int = 8


_QUIESCE_SPINNER_CHARS: tuple[str, ...] = (
    "⠇", "⠑", "⠙", "⠸", "⢰", "⢠", "⢤", "⠤", "⠮",
    # braille spinners (subset of U+2800..U+28FF)
    "⠋", "⠙", "⠚", "⠸", "⠴", "⠦", "⠇",
    "|", "/", "-", "\\",  # classic spinner, rarer but defensive
)
# Text signals indicating Claude is mid-generation.
_QUIESCE_BUSY_MARKERS: tuple[str, ...] = (
    "esc to interrupt", "press esc", "(ctrl+c to stop)", "generating",
    "thinking…", "thinking...",  # various thinking indicators
)
# Text signals for a permission prompt (do NOT auto-write while one is open).
_QUIESCE_PERMISSION_MARKERS: tuple[str, ...] = (
    "do you want", "would you like", "1. yes", "do you trust",
    "press enter to continue", "approve this",
)
# Text signals that suggest a ready CLI prompt (quiescent).
_QUIESCE_PROMPT_MARKERS: tuple[str, ...] = (
    "❯", "│", "╭", "╰",  # ❯ │ ╭ ╰
)


def _eval_quiescence(
    screen_text: str,
    last_change_ts: float,
    now: float,
    quiesce_sec: float = 2.0,
    strict: bool = True,
) -> tuple[bool, str]:
    """Pure logic for quiescence gate. Returns (is_quiescent, reason).

    Quiescent = safe for AutoCoordinator to write a new instruction.

    **strict mode (default, for screen-based flows):**
    1. Permission prompt visible → NOT quiescent (Yolo layer handles it)
    2. Busy marker (spinner, "esc to interrupt") → NOT quiescent
    3. Screen changed within `quiesce_sec` → NOT quiescent (still rendering)
    4. Empty screen → NOT quiescent (PTY still booting)
    5. No prompt glyph + never stabilised → NOT quiescent
    6. Otherwise → quiescent

    **non-strict mode (for file-based relay, 2026-04-23 fix):**
    Only block on permission_prompt / busy_generating / spinner+recent. Checks
    3/4/5 are skipped because file-relay target PTY may have chat-bubble DOM
    rendering affecting recent_activity, or tail scrolled away from prompt
    glyph — writing is still safe (PTY queues the input, write_submit has
    safety_tap for CONPTY race).

    Args:
        screen_text: full visible screen
        last_change_ts: time.time() of most recent screen mutation
        now: time.time() at call site
        quiesce_sec: minimum idle time (default 2.0s)
        strict: if False, skip recent_activity / no_prompt_visible gates
    """
    full_text = screen_text or ""
    full_lower = full_text.lower()
    # root-cause fix (26-04-28): restrict marker matching to the tail
    # window — markers (spinner, busy strings, permission prompts) only ever
    # appear in the CLI status area.
    tail_lines = full_text.splitlines()[-15:]
    tail_text = "\n".join(tail_lines)
    tail_lower = tail_text.lower()
    # 1. Permission prompt — checked BEFORE the screen-stable short-circuit.
    #    A menu waiting for input does NOT animate, so a static open prompt
    #    would otherwise pass screen_stable and let coord inject an instruction
    #    on top of it (digits in the dispatched text could even select an
    #    option + the trailing \r confirm it). Yolo handles approval; coord
    #    must never write onto an open prompt.
    for marker in _QUIESCE_PERMISSION_MARKERS:
        if marker in tail_lower:
            return (False, "permission_prompt")
    # root-cause fix (26-04-28): screen-stable short-circuit. If the
    # screen has not mutated for >= 2*quiesce_sec, Claude cannot be mid-stream
    # — the spinner would be animating each frame. Returning quiescent here
    # bypasses scrollback false-positives where "esc to interrupt"/"generating"
    # linger in the buffer from prior responses. Permission prompts are
    # excluded above, so this can't approve writing onto an open menu.
    if last_change_ts > 0 and (now - last_change_ts) >= quiesce_sec * 2:
        return (True, "screen_stable")
    # 2. Busy markers
    for marker in _QUIESCE_BUSY_MARKERS:
        if marker in tail_lower:
            return (False, "busy_generating")
    for sp in _QUIESCE_SPINNER_CHARS:
        if sp in tail_text:
            # Avoid false-positive on "|" appearing in box-drawing lines —
            # require another busy signal OR recent change. Bail out if
            # the screen is static and no other busy evidence.
            if now - last_change_ts < quiesce_sec:
                return (False, "busy_generating")
    if strict:
        # 3. Recent screen activity (strict only)
        if last_change_ts > 0 and (now - last_change_ts) < quiesce_sec:
            return (False, "recent_activity")
        # 4. Empty screen / session never rendered (strict only)
        if not full_lower.strip():
            return (False, "no_prompt_visible")
        # 5. No prompt glyph + never stabilised (strict only)
        has_prompt_glyph = any(g in full_text for g in _QUIESCE_PROMPT_MARKERS)
        if last_change_ts == 0 and not has_prompt_glyph:
            return (False, "no_prompt_visible")
    return (True, "")


def _validate_cmd_fields(tab) -> bool:
    """ SoT: validate exec inputs to prevent shell injection.
    Single source for open_terminal + PtySession.start + Api._build_cmd.
    Returns True if all fields safe, False = caller must graceful-abort."""
    if not re.fullmatch(r'[a-zA-Z0-9._-]+', tab.model or ''):
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


# ---------------------------------------------------------------------------
# ANSI colour → HTML rendering for the terminal view
# ---------------------------------------------------------------------------
# pyte parses the CLI's ANSI escapes into a screen buffer of styled `Char`s
# (fg/bg/bold/italics/underscore/strikethrough/reverse). The reader thread used
# to drop every attribute and ship only `.data`, which is why the terminal was
# monochrome. These helpers turn a pyte line back into per-run <span> HTML so
# the GUI mirrors the CLI's colours. Note: this is display-only — `screen_content`
# stays plain text because the AutoCoordinator regex-parses it (dispatch tags,
# session-id extraction, quiescence). The colour build is best-effort and must
# never break the plain path (callers wrap it and fall back to plain on error).

# ANSI colour name → hex, tuned for the terminal's dark bg (#0d1117). Same map
# for fg and bg (an ANSI colour is the same regardless of role). Palette is the
# widely-used GitHub-dark set so Claude Code's TUI (diff green/red, prompts,
# spinners, syntax) reads the way it does in a real terminal.
_ANSI_HTML_COLOR = {
    "black": "#484f58", "red": "#ff7b72", "green": "#3fb950", "brown": "#d29922",
    "blue": "#58a6ff", "magenta": "#bc8cff", "cyan": "#39c5cf", "white": "#b1bac4",
    "brightblack": "#6e7681", "brightred": "#ffa198", "brightgreen": "#56d364",
    "brightbrown": "#e3b341", "brightblue": "#79c0ff", "brightmagenta": "#d2a8ff",
    "brightcyan": "#56d4dd", "brightwhite": "#f0f6fc",
    # pyte BG_AIXTERM has a typo ("bfightmagenta") — map it so bright magenta bg
    # doesn't silently render as the default colour.
    "bfightmagenta": "#d2a8ff",
}
# Match the .term-screen CSS so `reverse` video swaps against the real defaults.
_TERM_DEFAULT_FG = "#c9d1d9"
_TERM_DEFAULT_BG = "#0d1117"


def _ansi_css_color(name):
    """pyte colour token → CSS colour string, or None for the terminal default.

    pyte stores named ANSI colours ("red", "brightblue", "default") and 256/true
    colour as a bare 6-hex-digit string ("ff8800")."""
    if not name or name == "default":
        return None
    hit = _ANSI_HTML_COLOR.get(name)
    if hit:
        return hit
    if len(name) == 6:
        try:
            int(name, 16)
            return "#" + name
        except ValueError:
            return None
    return None


def _html_escape(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _char_css(ch):
    """CSS style string for one pyte Char (empty when it needs no styling)."""
    if ch is None:
        return ""
    fg = _ansi_css_color(ch.fg)
    bg = _ansi_css_color(ch.bg)
    if ch.reverse:
        fg, bg = (bg or _TERM_DEFAULT_BG), (fg or _TERM_DEFAULT_FG)
    parts = []
    if fg:
        parts.append("color:" + fg)
    if bg:
        parts.append("background:" + bg)
    if ch.bold:
        parts.append("font-weight:700")
    if ch.italics:
        parts.append("font-style:italic")
    deco = []
    if ch.underscore:
        deco.append("underline")
    if ch.strikethrough:
        deco.append("line-through")
    if deco:
        parts.append("text-decoration:" + " ".join(deco))
    return ";".join(parts)


def _ansi_line_html(get_char, ncols):
    """Render one terminal line to run-length-encoded <span> HTML.

    `get_char(x)` returns a pyte Char or None (missing cell = blank default).
    Trailing blank cells are trimmed to match the plain path's per-line rstrip;
    a trailing cell with a background fill (reverse or explicit bg) is kept."""
    cells = [get_char(x) for x in range(ncols)]
    last = -1
    for x in range(ncols):
        ch = cells[x]
        if ch is None:
            continue
        d = ch.data
        if (d and d != " ") or ch.reverse or _ansi_css_color(ch.bg):
            last = x
    if last < 0:
        return ""
    spans = []
    cur_style = None
    buf = []
    for x in range(last + 1):
        ch = cells[x]
        if ch is not None and not ch.data:
            # Wide-char continuation cell: pyte stores "" in the cell after a
            # CJK/fullwidth char (which already renders double-width). Emit
            # nothing — mapping it to " " would shift every column after each
            # wide char one cell right vs the plain-text path (which drops "").
            continue
        style = _char_css(ch)
        data = ch.data if ch is not None else " "
        if buf and style != cur_style:
            spans.append(_wrap_span(cur_style, buf))
            buf = []
        if not buf:
            cur_style = style
        buf.append(data)
    if buf:
        spans.append(_wrap_span(cur_style, buf))
    return "".join(spans)


def _wrap_span(style, buf):
    txt = _html_escape("".join(buf))
    return ('<span style="' + style + '">' + txt + "</span>") if style else txt


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
        # True once kill() is called for an INTENTIONAL teardown (end/new
        # session, tab close, restart). The reader uses it to suppress the
        # onPtyDied() crash signal so the GUI doesn't false-restart a session
        # the user (or a restart path) deliberately stopped. A real crash
        # leaves this False (producer clears `running` on EOF), so onPtyDied
        # still fires and the JS crash-recovery runs.
        self._intentional = False
        self._start_time = 0
        self._data_queue = queue.Queue()
        self._last_persist = 0
        #: write lock — 4 write entry points (write/
        # write_submit/write_dispatch/_yolo_accept_send) all touch self.pty.write
        # without coordination. Yolo "1\r\r" 0.18-0.45s sleep window can
        # interleave with dispatch byte stream → corrupted submit. Lock
        # serializes the entire keystroke sequence per writer.
        self._write_lock = threading.Lock()
        # Quiescence gate (2026-04-23): AutoCoordinator must not dispatch new
        # tasks while Claude is still generating a response. We track when the
        # rendered screen last changed; is_quiescent() reads this + spinner /
        # "esc to interrupt" / prompt signals to decide if it's safe to write.
        self._last_screen_change_ts = 0.0

    def start(self):
        # History reduced 50000→3000 (2026-04-22 perf fix). The read loop
        # rebuilds and IPC-ships the entire history on every screen change;
        # at 50000 × 120 cols that was multi-MB per emit and caused ~1-min
        # input→display lag during long sessions.
        #: validate exec inputs (shared SoT). Fail = graceful no-op spawn.
        if not _validate_cmd_fields(self.tab):
            return
        self.screen = pyte.HistoryScreen(120, 36, history=1000)
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
        # Cap each claude CLI (Node.js) old-gen heap. Default is ~4GB which
        # multiplies across many tabs. 512MB fits typical streaming/tool
        # turns; if a heavy task hits the cap the CLI crashes, the user
        # raises this value, restart that tab.
        existing = env.get("NODE_OPTIONS", "")
        if "--max-old-space-size" not in existing:
            env["NODE_OPTIONS"] = (existing + " --max-old-space-size=512").strip()
        if self.tab.effort and self.tab.effort != "auto":
            env["CLAUDE_CODE_EFFORT"] = self.tab.effort
        # Autonomous system: expose role + tab id so Stop hook can decide whether
        # to continue the loop for this session specifically (not all sessions).
        if self.tab.role:
            env["CLAUDE_CODE_ROLE"] = self.tab.role
        if self.tab.id:
            env["CLAUDE_CODE_TAB_ID"] = self.tab.id
        # Launch Claude Code inside the selected project directory so slash
        # commands resolve against that project's .claude/commands, hooks
        # fire for that project, and relative paths match user expectation.
        # Fallback: inherit GUI's cwd if no project selected yet.
        spawn_cwd = None
        if self.tab.project_path:
            pp = Path(self.tab.project_path)
            if pp.is_dir():
                spawn_cwd = str(pp)
        self.pty = PtyProcess.spawn(cmd, dimensions=(36, 120), env=env, cwd=spawn_cwd)
        self.running = True
        self._start_time = time.time()
        threading.Thread(target=self._producer, daemon=True).start()
        threading.Thread(target=self._reader, daemon=True).start()
        # Notify UI: background tabs may not have called showTerminal, so the
        # "died within 10s" fallback in onPtyDied needs a baseline start time.
        try:
            self.api._js(f"onPtyStarted('{self.tab.id}')")
        except Exception:
            pass

    def kill(self):
        self._intentional = True
        self.running = False
        if self.pty:
            try:
                self.pty.terminate()
            except Exception:
                pass

    def write(self, data):
        """Send raw input to PTY (keystrokes, text, control chars).

: lock-serialized — see _write_lock docstring.
        """
        if self.pty and self.pty.isalive():
            with self._write_lock:
                if self.pty and self.pty.isalive():
                    self.pty.write(data)

    def write_submit(self, text: str, safety_tap: bool = True,
                     prepause: float = 0.3, tap_gap: float = 0.18) -> bool:
        """Write text, then submit via a separately-written Enter.

        Why: when we send `text + "\\r"` in one ConPTY chunk, Claude Code's
        input reader may buffer the whole chunk as pasted content; the
        trailing \\r is appended to the input buffer as a newline instead of
        registering as a submit keystroke. The user then sees their text
        loaded into the prompt but not sent, and has to press Enter manually.

        Fix: write the text first, wait long enough for the paste window
        to close, then write \\r as a standalone keystroke. A second \\r
        `safety_tap` follows a short gap later — if the first \\r was still
        absorbed by the paste buffer, this one submits; if the first \\r
        already submitted, the second falls on an empty input (no-op).
        Set `safety_tap=False` for slash commands and single-choice
        permission responses where a stray second Enter could auto-confirm
        an unrelated prompt that renders in the gap.

        Returns True if every write landed without the PTY dying mid-send.
        File-dispatch callers use this to skip marking a task as "seen"
        when delivery failed, so the next poll will retry it.
        """
        if not (self.pty and self.pty.isalive()):
            return False
        #: hold lock for entire submit sequence so a
        # concurrent writer (yolo accept / raw write / md-watch dispatch)
        # cannot interleave bytes between text and \r.
        try:
            with self._write_lock:
                if text:
                    self.pty.write(text)
                    time.sleep(prepause)
                if not (self.pty and self.pty.isalive()):
                    return False
                self.pty.write("\r")
                if safety_tap:
                    time.sleep(tap_gap)
                    if not (self.pty and self.pty.isalive()):
                        return False
                    self.pty.write("\r")
                return True
        except Exception:
            return False

    def write_dispatch(self, text: str) -> tuple[bool, str]:
        """Strict cross-tab dispatch: validate CLAUDE.md harness prefix on
        the first line before sending, refuse to submit if malformed.

        Why: receiving Claude auto-runs messages whose first line begins
        with one of the whitelisted harness prefixes (see CLAUDE.md
        "Authentication 例外2"). A malformed / missing prefix causes the
        receiver to apply the "by suz" authentication check and stall
        the coordination loop ("止まる" symptom). This wrapper acts as a
        send-time guard: if the prefix doesn't validate, nothing is
        written and the caller gets (False, reason) so it can decide
        whether to retry, fallback, or surface the bug.

        Returns (ok, reason):
          (True,  "ok")              — validated + written
          (True,  "written_pty")     — validated, written, but safety_tap/
                                       second \\r may have been dropped by
                                       a dying PTY (rare; treat as ok)
          (False, "empty_prefix" / "peer_empty_name" /
                  "peer_invalid_name" / "worker_missing_role" /
                  "unknown_prefix")  — malformed prefix, NOT sent
          (False, "pty_dead")        — PTY isn't alive
          (False, "write_failed")    — write raised, partial or no data
        """
        if not text:
            return (False, "empty_prefix")
        first_line = text.splitlines()[0] if text else ""
        ok, reason = _validate_harness_prefix(first_line)
        if not ok:
            return (False, reason)
        if not (self.pty and self.pty.isalive()):
            return (False, "pty_dead")
        # -05-05 user directive (operator often AFK):
        # write_submit(safety_tap=False) preserves strict-mode
        # protection during the initial Enter — no stray \r that would
        # confirm a permission prompt rendered between text and Enter #1.
        # However the lone \r is sometimes absorbed by the ConPTY paste
        # buffer, leaving the dispatch text staged in the input field
        # until a human presses Enter manually. When the operator is away
        # this stalls the entire coord loop. Mitigation: after the initial
        # submit settles (~0.5s), re-check the screen via _eval_quiescence
        # — if no permission_prompt is visible, fire a guarded 2nd \r to
        # defeat paste-buffer absorption. If Enter #1 already submitted,
        # the 2nd \r lands on empty input (no-op). If a permission prompt
        # is up, we skip the 2nd \r so we don't yolo-confirm something
        # the operator never saw — preserves the original invariant
        # for the only case it actually mattered.
        if not self.write_submit(text, safety_tap=False):
            return (False, "write_failed")
        try:
            time.sleep(0.5)
            _, qreason = self.is_quiescent(quiesce_sec=0.3, strict=False)
            if qreason != "permission_prompt":
                with self._write_lock:
                    if self.pty and self.pty.isalive():
                        self.pty.write("\r")
        except Exception:
            pass
        return (True, "ok")

    def is_quiescent(self, quiesce_sec: float = 2.0, strict: bool = True) -> tuple[bool, str]:
        """Return (ok, reason) gating AutoCoordinator writes.

        ok=True means it's safe to inject a new instruction via write_submit.
        reason is a telemetry key (e.g. "busy_generating", "permission_prompt")
        usable as a skip event payload.

        strict=True (default): full gate. Use for screen-based flows where
          user may be actively interacting with the terminal.
        strict=False: only block on busy/permission (see _eval_quiescence
          docstring for rationale). Use for file-based relay where bubble
          rendering can keep screen "recently changed" permanently.
        """
        if not self.pty or not self.pty.isalive():
            return (False, "pty_dead")
        screen_text = self.tab.screen_content or ""
        return _eval_quiescence(
            screen_text,
            self._last_screen_change_ts,
            time.time(),
            quiesce_sec,
            strict,
        )

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
        # high-sev fix (26-04-28): clear running flag on producer exit so
        # _md_watch_targets / _poll_tab_peers exclude this dead PTY. Without
        # this, pty.isalive()==False but self.running==True creates phantom
        # targets — write_dispatch returns pty_dead for every dispatch, mailbox
        # files (single-target) get permanent silent fail (caches never advance,
        # 18h+ stale state observed in production).
        self.running = False
        self._data_queue.put(None)

    def _screen_html_json(self, nrows, history_html):
        """JSON-encoded colour HTML for the current pyte screen (or "null").

        Called only at emit points (throttled to MIN_EMIT_INTERVAL) so the
        120×36 cell walk runs at ≤10fps, not on every screen change — during
        bursty streams changes outpace emits and frames in between are never
        shown. At the trailing flush the queue has been idle since the pending
        frame was built, so the buffer still matches pending_text and the
        colour build is consistent. Display-only and best-effort: any failure
        returns "null" and the GUI falls back to plain text."""
        try:
            ncols = self.screen.columns
            buf = self.screen.buffer
            srows = [
                _ansi_line_html(lambda x, _l=buf[y]: _l[x], ncols)
                for y in range(nrows)
            ]
            screen_html = "\n".join(srows)
            html = (history_html + "\n" + screen_html) if history_html else screen_html
            return json.dumps(html)
        except Exception:
            return "null"

    def _reader(self):
        """Consumes PTY data, feeds pyte, forwards rendered screen to GUI.

        Perf notes (2026-04-22):
        * History string is rebuilt only when `hist_len` changes (screen-only
          changes skip the O(history) walk).
        * Emit to JS is throttled to `MIN_EMIT_INTERVAL`; a trailing flush
          runs on queue-idle so the final frame after a burst always lands.
        """
        MIN_EMIT_INTERVAL = 0.1  # 100ms — smooth, cuts IPC by ~10x during streams
        tid = self.tab.id
        prev_hash = None
        history_str = ""
        history_html = ""
        last_hist_len = -1
        last_emit = 0.0
        pending_text: str | None = None
        pending_rows = 0
        while self.running:
            try:
                data = self._data_queue.get(timeout=0.1)
            except queue.Empty:
                # Trailing flush: if a pending frame is ready, emit it now.
                if pending_text is not None and (time.time() - last_emit) >= MIN_EMIT_INTERVAL:
                    try:
                        # Queue idle since the pending frame → buffer unchanged →
                        # colour build here matches pending_text exactly.
                        _h = self._screen_html_json(pending_rows, history_html)
                        self.api._js(f"onScreenUpdate('{tid}',{json.dumps(pending_text)},{_h})")
                    except Exception:
                        pass
                    self.tab.screen_content = pending_text
                    last_emit = time.time()
                    pending_text = None
                    if last_emit - self._last_persist > 30:
                        self._last_persist = last_emit
                        try:
                            self.api._persist()
                        except Exception:
                            pass
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
                # Quiescence gate: record when the screen last changed so
                # AutoCoordinator can avoid writing while Claude generates.
                self._last_screen_change_ts = time.time()
                # Rebuild history string only when its length changed; otherwise
                # reuse the cached one. This is the big win — at 3000 lines the
                # rebuild was ~10ms per change during bursty streaming.
                if hist_len != last_hist_len:
                    last_hist_len = hist_len
                    hl: list[str] = []
                    hlh: list[str] = []
                    if hist_len > 0:
                        for hline in self.screen.history.top:
                            try:
                                row = "".join(hline[col].data if hline[col].data else "" for col in sorted(hline.keys())).rstrip()
                            except (IndexError, AttributeError):
                                row = ""
                            hl.append(row)
                            # Colour build for scrollback — best-effort, mirrors
                            # the plain row on failure. dict.get() returns None for
                            # absent columns; _ansi_line_html reads that as a blank.
                            try:
                                hncols = (max(hline.keys()) + 1) if hline else 0
                                hlh.append(_ansi_line_html(lambda x, _l=hline: _l.get(x), hncols))
                            except Exception:
                                hlh.append(_html_escape(row))
                    history_str = "\n".join(hl)
                    history_html = "\n".join(hlh)
                while screen_lines and not screen_lines[-1]:
                    screen_lines.pop()
                screen_str = "\n".join(screen_lines)
                text = (history_str + "\n" + screen_str) if history_str else screen_str
                # Emit to JS — throttled with trailing flush (see queue.Empty branch
                # above). Colour HTML (display-only — screen_content stays plain for
                # the AutoCoordinator's regex parsing) is built in _screen_html_json
                # at emit time only, so the cell walk never runs for skipped frames.
                now = time.time()
                if (now - last_emit) >= MIN_EMIT_INTERVAL:
                    _h = self._screen_html_json(len(screen_lines), history_html)
                    self.api._js(f"onScreenUpdate('{tid}',{json.dumps(text)},{_h})")
                    self.tab.screen_content = text[-30000:]
                    last_emit = now
                    pending_text = None
                    pending_rows = 0
                    if now - self._last_persist > 30:
                        self._last_persist = now
                        try:
                            self.api._persist()
                        except Exception:
                            pass
                else:
                    pending_text = text
                    pending_rows = len(screen_lines)
                    self.tab.screen_content = text[-30000:]  # keep in-memory fresh
                # Extract session ID from screen (for --resume)
                # Always check within first 30s to detect session changes after restart
                if time.time() - self._start_time < 30:
                    for ln in screen_lines:
                        m = re.search(
                            r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}', ln)
                        if m:
                            new_sid = m.group(0)
                            if new_sid != self.tab.session_id:
                                old_sid = self.tab.session_id
                                self.tab.session_id = new_sid
                                try:
                                    self.api._persist()
                                except Exception:
                                    pass
                                self.api._js(f"onSessionIdUpdate('{tid}',{json.dumps(new_sid)},{json.dumps(bool(old_sid))})")
                            break
                # Detect interactive menu → parse numbered choices from screen.
                # Only scan the BOTTOM of the screen (last 20 rows) and require
                # choices to be on consecutive lines — CLI prompts are always
                # contiguous at the bottom, while generic numbered bulletpoints
                # in Claude's response can appear anywhere and are broken up by
                # body text. This avoids false-positiving markdown lists.
                tail = screen_lines[-20:] if len(screen_lines) > 20 else screen_lines
                raw_choices: list[tuple[int, int, str]] = []  # (row_idx, num, label)
                for idx, ln in enumerate(tail):
                    m = re.match(r'^[^0-9]*(\d+)\.\s+(.+)$', ln.strip())
                    if m:
                        raw_choices.append((idx, int(m.group(1)), m.group(2).strip()))
                # Keep only the longest run of consecutive (row gap <= 1) choices
                choices: list = []
                if raw_choices:
                    best: list[tuple[int, int, str]] = []
                    run: list[tuple[int, int, str]] = [raw_choices[0]]
                    for c in raw_choices[1:]:
                        if c[0] - run[-1][0] <= 1:
                            run.append(c)
                        else:
                            if len(run) > len(best):
                                best = run
                            run = [c]
                    if len(run) > len(best):
                        best = run
                    choices = [[n, lab] for _, n, lab in best]
                screen_text = "\n".join(screen_lines).lower()
                # Require a strong CLI-prompt signal so generic numbered
                # lists in Claude's response don't false-positive as a
                # permission prompt. The AND condition is intentional — see
                # feedback_perm_buttons.md for the design rationale. If the
                # CLI adds new prompt glyphs, append them here (not remove
                # existing ones).
                #
                # Expanded 2026-04-23 (gui-ux-bugfix-batch item 3): added
                # `▶/→/▸/◆` alternate selector glyphs and two English question
                # stems that appear in newer Claude Code permission prompts,
                # plus a "bottom-3-rows + ends-with-question-mark" fallback
                # to catch prompts where the screen lacks box-drawing but is
                # clearly interactive.
                strong_signals = (
                    "❯", "│", "╭", "╰", "(esc", "press enter",
                    "▶", "→", "▸", "◆",
                    "do you want", "would you like",
                )
                has_strong = any(s in screen_text for s in strong_signals)
                # Fallback: if 2+ consecutive choices sit in the last 3 rows
                # of the screen AND a question mark appears in the prompt
                # area (last 5 rows), treat as interactive even without a
                # box-drawing glyph. Safe because consecutive numbered lines
                # in Claude's prose output are rare at the absolute bottom
                # (there's usually trailing prose after a numbered list).
                if not has_strong and choices and best:
                    tail_off_for_check = len(screen_lines) - len(tail)
                    last_choice_row = tail_off_for_check + best[-1][0]
                    near_bottom = last_choice_row >= len(screen_lines) - 3
                    last_five = "\n".join(screen_lines[-5:])
                    if near_bottom and "?" in last_five:
                        has_strong = True
                is_interactive = len(choices) >= 2 and has_strong
                if is_interactive:
                    self.api._js(f"onPermState('{tid}',true,{json.dumps(choices)})")
                    # --- Narrow danger-check scope to the prompt box only -----------
                    # Map the chosen `best` run (in tail coords) back to screen_lines
                    # index space, then extend up until a `╭` (top of box) / 10-row
                    # cap and down until a `╰` / 3-row cap. This prevents stale
                    # scrollback keywords (e.g. "token" from past conversation)
                    # from permanently blocking Yolo auto-accept.
                    prompt_text = screen_text  # fallback
                    if choices and raw_choices:
                        tail_off = len(screen_lines) - len(tail)
                        first_tail_idx = best[0][0]
                        last_tail_idx = best[-1][0]
                        first_row = tail_off + first_tail_idx
                        last_row = tail_off + last_tail_idx
                        # extend upward
                        up_limit = max(0, first_row - 10)
                        top = first_row
                        for r in range(first_row - 1, up_limit - 1, -1):
                            top = r
                            if "╭" in screen_lines[r]:
                                break
                        # extend downward
                        down_limit = min(len(screen_lines) - 1, last_row + 3)
                        bot = last_row
                        for r in range(last_row + 1, down_limit + 1):
                            bot = r
                            if "╰" in screen_lines[r]:
                                break
                        prompt_text = "\n".join(screen_lines[top:bot + 1]).lower()
                    # Yolo Mode: auto-accept first (allow) choice for safe operations
                    if self.tab.yolo_mode:
                        ok, reason, payload = self._yolo_should_accept(prompt_text, choices)
                        if ok:
                            now = time.time()
                            # debounce 0.8s → 1.5s → 2.2s: _yolo_accept_send's
                            # worst case is ~1.6s (0.45+0.18+0.22+0.6 + retry
                            # writes), so 1.5s let a SECOND accept thread spawn
                            # mid-retry and double-tap a following prompt. 2.2s
                            # covers the full single-accept budget with margin.
                            if now - self.tab._last_yolo_accept > 2.2:
                                self.tab._last_yolo_accept = now
                                # Reliability rework (2026-04-23, user feedback):
                                # the old inline `"1\r"` fired from the reader
                                # thread with only 0.25s of pre-wait often got
                                # absorbed before the CLI's menu was ready to
                                # accept keystrokes — the approval event fired
                                # but no submit registered, forcing manual
                                # click. Delegated to a worker thread so reader
                                # emits don't stall, and uses split `1` + Enter
                                # + safety re-tap with verification retry.
                                threading.Thread(
                                    target=self._yolo_accept_send,
                                    args=(tid, choices[0][1][:80]),
                                    daemon=True,
                                ).start()
                        else:
                            # Observability: surface skip reason to UI.
                            # Dedup: same reason+payload emits at most once per 5s.
                            dedup_key = f"{reason}:{json.dumps(payload, sort_keys=True)}"
                            last = self.tab._last_yolo_skip or {}
                            now = time.time()
                            if now - last.get(dedup_key, 0) > 5.0:
                                last[dedup_key] = now
                                self.tab._last_yolo_skip = last
                                try:
                                    self.api._js(
                                        f"onYoloSkip('{tid}',"
                                        f"{json.dumps(reason)},"
                                        f"{json.dumps(payload)})"
                                    )
                                except Exception:
                                    pass
                else:
                    self.api._js(f"onPermState('{tid}',false,[])")
        # PTY died — persist screen content
        try:
            self.api._persist()
        except Exception:
            pass
        # Only signal a crash (→ JS auto-restart) for an UNEXPECTED death.
        # Intentional teardowns (kill via end/new session, close, restart) set
        # _intentional, and their own JS callbacks own the UI transition.
        if not self._intentional:
            self.api._js(f"onPtyDied('{tid}')")

    def _yolo_should_accept(self, prompt_text: str, choices: list) -> tuple[bool, str | None, dict]:
        """Gate Yolo auto-accept. Thin wrapper around `_eval_yolo_prompt`.

        Scope change (2026-04-23, gui-yolo-reliability-fix): `prompt_text`
        must be the CURRENT permission prompt box only, NOT scrollback.
        Caller is responsible for the scope narrowing; danger patterns
        no longer include generic tokens like "token"/".env"/"credential".

        Returns (ok, reason, payload). See `_eval_yolo_prompt` for shape.
        """
        return _eval_yolo_prompt(prompt_text, choices)

    def _yolo_accept_send(self, tid: str, label: str) -> None:
        """Send the Yolo auto-accept keystrokes with timing tuned to avoid
        paste-buffer absorption, and retry once if the menu is still visible.

        Runs in its own daemon thread so the PTY reader is never blocked on
        the ~1s cumulative sleep budget below. Fires `onYoloAccept` only
        after the menu actually clears (best-effort check) so the UI flash
        reflects success, not just intent.
        """
        try:
            # Let prompt box fully render before first keystroke.
            # 0.25s was too tight — raised to 0.45s based on observed lag
            # between menu detection and Claude accepting input.
            time.sleep(0.45)
            if not (self.pty and self.pty.isalive()):
                return
            #: hold lock for the full "1\r[\r]" sequence
            # so dispatch byte stream cannot inject between digit and submit.
            with self._write_lock:
                if not (self.pty and self.pty.isalive()):
                    return
                # Set selection explicitly (defensive: ❯ starts on choice 1 by
                # default, but if user nudged it before Yolo fired, "1" resets).
                self.pty.write("1")
                time.sleep(0.18)
                if not (self.pty and self.pty.isalive()):
                    return
                self.pty.write("\r")
                # Safety Enter: if the first \r was still absorbed as paste,
                # this one submits. Short gap keeps the window small for a
                # *new* prompt appearing in between.
                time.sleep(0.22)
                if not (self.pty and self.pty.isalive()):
                    return
                self.pty.write("\r")

            # Verify-and-retry: if the menu is STILL showing ~0.6s later,
            # the CLI rejected our keystrokes (e.g. it was mid-render). One
            # more "1\r" tap is much cheaper than waiting for the user.
            time.sleep(0.6)
            still_menu = False
            try:
                lines = [ln.rstrip() for ln in self.screen.display][-20:]
                txt = "\n".join(lines)
                # Same heuristic as the reader: menu needs ❯ + a numbered list.
                if "❯" in txt and re.search(r"\b1\.\s", txt):
                    still_menu = True
            except Exception:
                still_menu = False
            if still_menu and self.pty and self.pty.isalive():
                try:
                    #: lock the retry sequence as well.
                    with self._write_lock:
                        if self.pty and self.pty.isalive():
                            self.pty.write("1")
                            time.sleep(0.15)
                        if self.pty and self.pty.isalive():
                            self.pty.write("\r")
                except Exception:
                    pass

            try:
                self.api._js(f"onYoloAccept('{tid}',{json.dumps(label)})")
            except Exception:
                pass
        except Exception:
            pass


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
# Coordination Watcher — polls coordination/ files across project roots,
# surfaces blocker and deploy-status changes to the frontend.
# ---------------------------------------------------------------------------
class CoordinationWatcher:
    """Polls coordination/ directories in known project paths, notifies the GUI
    when blockers.md changes (new user-decision items) or deploy_status.json flips."""

    def __init__(self, api_ref):
        self.api = api_ref
        self.running = False
        self._thread = None
        self._hashes: dict[str, str] = {}

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.running = False

    def _project_roots(self) -> list[Path]:
        roots: list[Path] = []
        for tab in self.api._tabs.values():
            if tab.project_path:
                p = Path(tab.project_path)
                if p.is_dir() and p not in roots:
                    roots.append(p)
        return roots

    def _loop(self) -> None:
        while self.running:
            try:
                for root in self._project_roots():
                    coord = root / "coordination"
                    if not coord.is_dir():
                        continue
                    self._check_file(coord / "blockers.md", "blockers", root)
                    self._check_file(coord / "deploy_status.json", "status", root)
            except Exception:
                pass
            time.sleep(3)

    def _check_file(self, path: Path, kind: str, root: Path) -> None:
        if not path.exists():
            return
        try:
            content = path.read_text("utf-8", errors="replace")
        except Exception:
            return
        import hashlib
        h = hashlib.md5(content.encode("utf-8", errors="replace")).hexdigest()
        key = f"{root}|{kind}"
        prev = self._hashes.get(key)
        self._hashes[key] = h
        if prev is None or prev == h:
            return
        # File changed — notify frontend
        if kind == "blockers":
            # count "## [" entries minus the template one
            entries = [ln for ln in content.splitlines() if ln.startswith("## [") and "blocker_title" not in ln]
            count = max(0, len(entries))
            self.api._js(
                f"onBlockersChanged({count},{json.dumps(str(root))},{json.dumps(content[:2000])})"
            )
        elif kind == "status":
            self.api._js(f"onStatusChanged({json.dumps(str(root))},{json.dumps(content[:4000])})")


# ---------------------------------------------------------------------------
# Persistent Sync Daemon — always-on role sync + MD watch fallback
# ---------------------------------------------------------------------------
class PersistentSyncDaemon:
    """Always-on background daemon for role sync + MD watching.

    Starts on GUI boot, stops only on shutdown. Guarantees that:

    1. **Role sync (永続的)**: every CADENCE_SEC, re-scans each cached
       `RoleRegistry` against its project's `.claude/roles/*.md` directory.
       New role files added at runtime (without GUI restart) are picked up
       and merged into the registry → become selectable in the GUI role
       menu automatically. Idempotent — existing roles are preserved.

    2. **MD watch fallback (ずーっと永続的に)**: when AutoCoordinator is
       STOPPED, this daemon takes over `_poll_file_generic` duties —
       discovers any tab with a coordinator-kind role, scans its
       project's `coordination/*.md` files, and sends delta notifications
       to that tab's PTY with strict `write_dispatch` prefix validation.
       When AutoCoord IS running, this daemon defers to it (AutoCoord has
       tighter semantics around dispatch ordering and manager tracking).
       The hand-off ensures MD watching never pauses — whether or not
       AutoCoord is actively coordinating, file changes still reach the
       Manager PTY.

    Both duties use retry-preserved delivery (caches updated only on
    successful `write_dispatch`) so a busy/dead Manager PTY doesn't drop
    notifications — they retry on the next tick.
    """

    CADENCE_SEC = 5  # role sync + md watch cycle; ~1-2ms work per idle tick

    def __init__(self, api_ref):
        self.api = api_ref
        self.running = False
        self._thread: threading.Thread | None = None
        # MD watch caches (separate from AutoCoordinator's — they don't
        # share state to avoid contention at the AutoCoord start/stop
        # transition). Keys are prefixed "pd:" so a single key namespace
        # distinction makes log entries searchable.
        self._file_mtime: dict[str, float] = {}
        self._file_hash: dict[str, str] = {}
        self._file_prev: dict[str, str] = {}
        self._first_seen: set[str] = set()
        self._log: list[dict] = []

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.running = False

    def _evt(self, kind: str, msg: str) -> None:
        try:
            self._log.append({"ts": time.time(), "kind": kind, "msg": msg})
            if len(self._log) > 400:
                self._log = self._log[-200:]
        except Exception:
            pass

    def _loop(self) -> None:
        # Bulletproof: nested try so a single tick exception can't kill the
        # thread (same pattern as AutoCoordinator._loop).
        while self.running:
            try:
                self._run_role_sync()
                self._run_md_watch_fallback()
            except Exception as e:
                try:
                    self._evt("error", str(e))
                except Exception:
                    pass
            try:
                time.sleep(self.CADENCE_SEC)
            except Exception:
                pass

    # -- role sync ------------------------------------------------------------

    def _run_role_sync(self) -> None:
        """Re-sync every cached RoleRegistry from disk, and trigger sync
        for any tab's project that has no cached registry yet.

        Effect: newly-created `.claude/roles/<name>.md` files become visible
        in the role dropdown within CADENCE_SEC seconds of file creation —
        no GUI restart required (user's "syncも確実にやろう" requirement).
        """
        registries = getattr(self.api, "_role_registries", None)
        if isinstance(registries, dict):
            for key, reg in list(registries.items()):
                try:
                    before = len(reg._roles) if hasattr(reg, "_roles") else 0
                    reg._sync_from_dir()
                    after = len(reg._roles) if hasattr(reg, "_roles") else 0
                    if after > before:
                        self._evt("role_sync", f"{key or 'global'}: +{after - before} role(s)")
                except Exception:
                    pass
        # Also surface any newly-opened project that hasn't triggered
        # a registry load yet — harmless if already cached (idempotent).
        tabs = getattr(self.api, "_tabs", None)
        if isinstance(tabs, dict):
            for tab in list(tabs.values()):
                pp = getattr(tab, "project_path", "") or ""
                if pp:
                    try:
                        self.api._roles_for(pp)
                    except Exception:
                        pass

    # -- md watch fallback ----------------------------------------------------

    def _find_coordinator_tabs(self) -> list[tuple]:
        """Discover all (tab, coord_dir) pairs for projects with a running
        tab. **2026-04-28 fix**: previously required coordinator-kind tab —
        meaning if mgr/cco tab wasn't open, peer mailbox push died entirely.
        Now returns ANY running tab as project anchor; the actual dispatch
        targeting is done by `_md_watch_targets` per file (mailbox delivers
        direct to recipient role regardless of anchor). Coordinator preferred
        when multiple tabs exist (better log attribution), but not required.

        One pair per project_path (first match wins to avoid double-send).
        """
        out: list[tuple] = []
        seen_projects: set[str] = set()
        # First pass: prefer coordinator-kind tabs (better log attribution +
        # historical compat with AutoCoord which uses manager_tab as anchor)
        tabs = getattr(self.api, "_tabs", None)
        if not isinstance(tabs, dict):
            return out
        for tab in tabs.values():
            pp = getattr(tab, "project_path", "") or ""
            if not pp or pp in seen_projects:
                continue
            pty = getattr(tab, "pty_session", None)
            if not pty or not getattr(pty, "running", False):
                continue
            try:
                reg = self.api._roles_for(pp)
            except Exception:
                continue
            if _role_effective(tab, reg) not in reg.coordinators():
                continue
            coord = Path(pp) / "coordination"
            if coord.is_dir():
                out.append((tab, coord))
                seen_projects.add(pp)
        # Second pass: any running tab whose project hasn't been anchored yet
        # (peer revival 26-04-28 — guarantees fallback runs even without mgr).
        for tab in tabs.values():
            pp = getattr(tab, "project_path", "") or ""
            if not pp or pp in seen_projects:
                continue
            pty = getattr(tab, "pty_session", None)
            if not pty or not getattr(pty, "running", False):
                continue
            coord = Path(pp) / "coordination"
            if coord.is_dir():
                out.append((tab, coord))
                seen_projects.add(pp)
        return out

    def _run_md_watch_fallback(self) -> None:
        """Fallback MD watcher — runs only when AutoCoordinator is stopped.

        When AutoCoord is running, its `_poll_file_generic` handles the
        same watching. Deferring avoids double-dispatch during normal
        coordination. When AutoCoord stops (or never started), this daemon
        picks up seamlessly.

        2026-04-26 P3: previously this was ~120 lines of code duplicated from
        AutoCoordinator._poll_file_generic. Drift between the two paths
        leaked into history twice (mirrored fixes). Both now delegate to
        the shared `_md_watch_process_coord_dir` helper.
        """
        ac = getattr(self.api, "_auto_coord", None)
        if ac is not None and getattr(ac, "running", False):
            #: zombie-state detection. AutoCoord may have running=True
            # but _loop dead (e.g. thread crashed). If heartbeat stale > 60s,
            # fall through and run fallback so MD-WATCH delivery is preserved.
            last_tick = float(getattr(ac, "_last_tick_ts", 0.0) or 0.0)
            if last_tick == 0.0 or (time.time() - last_tick) <= 60.0:
                return  # AutoCoordinator is handling it — defer.
            self._evt(
                "autocoord_zombie",
                f"AutoCoord running=True but last_tick {time.time() - last_tick:.0f}s ago — taking over MD-WATCH",
            )
        coord_targets = self._find_coordinator_tabs()
        if not coord_targets:
            return
        excluded = getattr(AutoCoordinator, "_GENERIC_WATCH_EXCLUDE",
                           {"manager_queue.md", "README.md", "peer_channel.md"})
        for manager_tab, coord in coord_targets:
            _md_watch_process_coord_dir(
                api=self.api,
                manager_tab=manager_tab,
                coord=coord,
                excluded=excluded,
                file_mtime=self._file_mtime,
                file_hash=self._file_hash,
                file_prev=self._file_prev,
                first_seen=self._first_seen,
                key_prefix="pd",
                log_evt=self._evt,
                js_emit=None,  # Persistent daemon doesn't push JS events.
                label="persistent-daemon",
            )


# ---------------------------------------------------------------------------
# Bounded LRU seen-set — used by AutoCoordinator dedup state.
# ---------------------------------------------------------------------------
# 2026-04-26 fix: previous code did `set(list(self._seen)[-1000:])` to bound
# growth, but Python sets DON'T preserve insertion order — so the trim kept
# a roughly random 1000 items, evicting ~50% of the most recent dedup marks.
# Effect: tasks/reports could be re-dispatched after the seen-set hit 2000.
# OrderedDict preserves insertion order (3.7+), so popitem(last=False) is
# true FIFO eviction. add() also re-inserts at the tail so a re-add bumps
# recency (LRU touch — keeps actively-used keys from being evicted).
class _BoundedSeenSet:
    """LRU-trimmed dedup seen-set. Drop-in for `set` for add/discard/in/len.

    Bulk-trims to `low` watermark when size exceeds `cap`, so eviction cost
    is amortized across additions instead of evicting one item per add.
    """
    __slots__ = ("_d", "_cap", "_low")

    def __init__(self, cap: int = 2000, low: int | None = None) -> None:
        self._d: "OrderedDict[str, None]" = OrderedDict()
        self._cap = cap
        self._low = low if low is not None else cap // 2

    def __contains__(self, key: str) -> bool:
        return key in self._d

    def __len__(self) -> int:
        return len(self._d)

    def __iter__(self):
        return iter(self._d)

    def add(self, key: str) -> None:
        if key in self._d:
            self._d.move_to_end(key)
        else:
            self._d[key] = None
        if len(self._d) > self._cap:
            while len(self._d) > self._low:
                self._d.popitem(last=False)

    def update(self, keys) -> None:
        for k in keys:
            self.add(k)

    def discard(self, key: str) -> None:
        self._d.pop(key, None)

    def clear(self) -> None:
        self._d.clear()


# ---------------------------------------------------------------------------
# — Skills list conditional injection
# ---------------------------------------------------------------------------
# Pure utility callable by any prompt-assembly layer (current harness drives
# external Claude Code CLI subprocesses, so this is engine-ready: callers can
# import filter_skill_list() to compress a 35+ skill catalog down to a top-5
# default for normal turns, expanding to the full list when the user types
# /<command>. Footer "(N more skills available — use /list to view)" hints
# the model that more is fetchable.
# ---------------------------------------------------------------------------
def filter_skill_list(
    skills: "list[dict]",
    *,
    user_message: str = "",
    recently_invoked: "list[str] | None" = None,
    role_defaults: "list[str] | None" = None,
    top_n: int = SKILLS_DEFAULT_TOP_N,
) -> "tuple[list[dict], int]":
    """Return (filtered_list, suppressed_count) for a given turn.

    Rules:
      1. If `user_message` starts with "/<word>" or contains "/<word>" matching
         a skill name prefix, return ALL skills with prefix-matching names
         (full list mode). Counted as 0 suppressed for this branch.
      2. Otherwise return at most `top_n` skills, ranked by:
         - recently_invoked appears first (preserve order),
         - then role_defaults entries not already included,
         - then any remaining skills in original order.
         Suppressed count = len(skills) - len(returned).

    Each skill dict must have at least a "name" key. Other fields (description,
    body) are passed through unchanged.

    Examples:
      >>> filter_skill_list([{"name":"a"},{"name":"b"},{"name":"c"}], top_n=2)[0]
      [{'name': 'a'}, {'name': 'b'}]
    """
    if not skills:
        return ([], 0)
    recently_invoked = recently_invoked or []
    role_defaults = role_defaults or []

    # /<command> detection — find any /<word> token in the user message.
    msg = (user_message or "").strip()
    cmd_match = None
    if msg:
        # First token only; standard slash-command convention.
        # Also support mid-message /<word> (e.g. "Use /audit for that").
        for tok in msg.split():
            if tok.startswith("/") and len(tok) > 1:
                candidate = tok[1:].rstrip(":,.;!?").lower()
                if candidate:
                    cmd_match = candidate
                    break

    if cmd_match:
        # Full list mode: keep skills whose name starts with the typed prefix
        # OR contains the prefix. False-negative rate 0 by design.
        prefix_match = [s for s in skills if cmd_match in (s.get("name") or "").lower()]
        if prefix_match:
            return (prefix_match, 0)
        # No prefix match — fall through to top-N (defensive — typed slash but
        # no skill matched, so we don't drop the user's other context).

    # Top-N mode: rank by recently_invoked + role_defaults + original order.
    by_name = {(s.get("name") or ""): s for s in skills}
    ordered: list[dict] = []
    seen: set[str] = set()
    for name in recently_invoked:
        if name in by_name and name not in seen:
            ordered.append(by_name[name])
            seen.add(name)
    for name in role_defaults:
        if name in by_name and name not in seen:
            ordered.append(by_name[name])
            seen.add(name)
    for s in skills:
        nm = s.get("name") or ""
        if nm not in seen:
            ordered.append(s)
            seen.add(nm)
    capped = ordered[:max(0, int(top_n))]
    return (capped, max(0, len(skills) - len(capped)))


def format_skill_list_footer(suppressed_count: int) -> str:
    """Return the conventional footer line. Empty string if nothing suppressed."""
    if suppressed_count <= 0:
        return ""
    return f"(N more skills available — use /list to view) [N={suppressed_count}]"


# ---------------------------------------------------------------------------
# — Tool result auto-truncation
# ---------------------------------------------------------------------------
# Pure utility for trimming verbose tool output. The harness doesn't directly
# format Claude's tool results (those flow through the CLI subprocess), but
# this is callable from any layer that DOES touch tool output (peer dispatch
# rendering, log capture, etc.). Drop-in shape: result_text → truncated text.
# ---------------------------------------------------------------------------
def _truncate_lines(
    text: str,
    threshold: int,
    *,
    keep_head: int = TOOL_TRUNC_KEEP_HEAD,
    keep_tail: int = TOOL_TRUNC_KEEP_TAIL,
) -> str:
    """Generic line-based truncation — first head + marker + last tail.

    Returns text unchanged if line count ≤ threshold. Otherwise:
      <first head lines>\n[CLAUDE_TRUNC: kept=H+T dropped=D]\n<last tail lines>
    """
    if not text:
        return text
    lines = text.splitlines()
    if len(lines) <= int(threshold):
        return text
    if keep_head < 0 or keep_tail < 0:
        raise ValueError("keep_head / keep_tail must be ≥ 0")
    head = lines[: int(keep_head)]
    tail = lines[-int(keep_tail):] if int(keep_tail) > 0 else []
    dropped = len(lines) - len(head) - len(tail)
    if dropped <= 0:
        return text  # head + tail already cover everything
    marker = f"[CLAUDE_TRUNC: kept={len(head) + len(tail)} dropped={dropped}]"
    return "\n".join(head + [marker] + tail)


def truncate_tool_result(tool_name: str, result_text: str) -> str:
    """Truncate a tool result based on tool kind.

    Bash → 200 lines / Grep → 300 lines / generic → 500 lines.
    Read tool is skipped (already has its own 2000-line cap).

    Tool name is matched case-insensitively. Any first-letter-capital variant
    (Bash / bash / BASH) maps to the bash threshold.
    """
    if not result_text:
        return result_text
    name = (tool_name or "").strip().lower()
    if name == "read":
        return result_text  # Read tool has its own cap, don't double-truncate
    if name == "bash":
        return _truncate_lines(result_text, TOOL_TRUNC_BASH_LINES)
    if name == "grep":
        return _truncate_lines(result_text, TOOL_TRUNC_GREP_LINES)
    return _truncate_lines(result_text, TOOL_TRUNC_GENERIC_LINES)


# ---------------------------------------------------------------------------
# — System reminder dedup
# ---------------------------------------------------------------------------
# Tracks last-injected turn per reminder kind. Repeated injections within
# MIN_INTERVAL_TURNS are suppressed. Critical-flag reminders bypass dedup.
# ---------------------------------------------------------------------------
class ReminderDedup:
    """Per-kind reminder dedup state.

    Use `should_inject(kind, turn, critical=False)` per candidate reminder.
    Returns True iff the reminder should be emitted on this turn. Critical
    reminders always emit (and update the timestamp, so a non-critical
    follow-up won't re-emit immediately).

    `kind` is a stable string identifier (e.g. "task-tool-hint",
    "claudemd-context", "security-warning"). Free-form, caller's choice.
    """
    __slots__ = ("_last", "_min_interval")

    def __init__(self, min_interval_turns: int = REMINDER_MIN_INTERVAL_TURNS) -> None:
        self._last: dict[str, int] = {}
        self._min_interval = max(1, int(min_interval_turns))

    def should_inject(self, kind: str, turn: int, *, critical: bool = False) -> bool:
        if not kind:
            return True  # never dedup unkeyed reminders (defensive)
        if critical:
            self._last[kind] = int(turn)
            return True
        last = self._last.get(kind)
        if last is None or (int(turn) - last) >= self._min_interval:
            self._last[kind] = int(turn)
            return True
        return False

    def reset(self, kind: str | None = None) -> None:
        if kind is None:
            self._last.clear()
        else:
            self._last.pop(kind, None)


# ---------------------------------------------------------------------------
# — MD-WATCH coalescing window (debounce + ceiling)
# ---------------------------------------------------------------------------
# Same-file MD-WATCH events ≤30s apart are coalesced into a single dispatch.
# A 90s ceiling ensures bursty writes still flush within a bounded window
# even if they keep arriving inside the debounce. Adjacent to the existing
# _BoundedSeenSet dedup LRU but operates on the time axis (LRU = content
# axis). Composes with pointer mode (content-layer cut).
# ---------------------------------------------------------------------------
class _MdWatchCoalescer:
    """Time-axis MD-WATCH coalescer.

    State per file:
      - first_event_at: time the first not-yet-flushed event landed
      - last_event_at:  most recent event time
      - pending: a callable to dispatch the *latest* delta (callers swap on
                 each new event so the freshest payload wins)

    `should_emit_now(name, now)` returns True iff:
      - debounce expired (now - last_event_at ≥ debounce_s) OR
      - ceiling reached (now - first_event_at ≥ ceiling_s)
    On True, the entry is cleared (caller is expected to flush its delta).
    """
    __slots__ = ("_state", "_debounce_s", "_ceiling_s", "_enabled")

    def __init__(
        self,
        *,
        debounce_s: float = MDWATCH_COALESCE_DEBOUNCE_S,
        ceiling_s: float = MDWATCH_COALESCE_CEILING_S,
        enabled: bool = MDWATCH_COALESCE_ENABLED,
    ) -> None:
        self._state: dict[str, tuple[float, float]] = {}
        self._debounce_s = float(debounce_s)
        self._ceiling_s = float(ceiling_s)
        self._enabled = bool(enabled)

    def is_enabled(self) -> bool:
        return self._enabled

    def note_event(self, name: str, now: float) -> None:
        """Record a new event for `name` at time `now`."""
        if not self._enabled:
            return
        prev = self._state.get(name)
        first_at = prev[0] if prev else now
        self._state[name] = (first_at, now)

    def should_emit_now(self, name: str, now: float) -> bool:
        """Return True if the entry's accumulated wait is over.

        On True, removes the entry from state (caller flushes downstream).
        On False (or coalescer disabled / no entry), returns True for the
        disabled path (so the caller's "emit now" decision is unaffected).
        """
        if not self._enabled:
            return True
        st = self._state.get(name)
        if st is None:
            return True  # no buffered state — caller's call should pass through
        first_at, last_at = st
        debounce_ready = (now - last_at) >= self._debounce_s
        ceiling_ready = (now - first_at) >= self._ceiling_s
        if debounce_ready or ceiling_ready:
            self._state.pop(name, None)
            return True
        return False

    def pending(self) -> "list[str]":
        """Return names currently buffered (for diagnostics / forced flush)."""
        return list(self._state.keys())

    def force_flush(self, name: str) -> None:
        self._state.pop(name, None)


# ---------------------------------------------------------------------------
# — MD-WATCH pointer mode (push→pull)
# ---------------------------------------------------------------------------
# When enabled, MD-WATCH dispatches contain a pointer line instead of the
# full delta body. Receiving roles use Read tool with offset/limit to fetch
# the changed lines on-demand. If the file's hash drifts before the role
# reads, fall back to tail-N (the last 50 lines) — that's almost always
# the freshest dispatched content for append-mostly coord files.
# ---------------------------------------------------------------------------
def _md_watch_format_pointer(
    name: str,
    *,
    from_hash: str,
    to_hash: str,
    lines_added: int,
    lines_removed: int,
    line_start: int,
    line_end: int,
) -> str:
    """Build the pointer line per spec.

    Format: `[MD-PULL <path> from_hash=<old> to_hash=<new> +N/-M lines L<a>-<b>]`
    Roles read coord/<name> with offset=line_start, limit=(line_end-line_start+1).
    """
    return (
        f"[MD-PULL coordination/{name} "
        f"from_hash={from_hash} to_hash={to_hash} "
        f"+{int(lines_added)}/-{int(lines_removed)} "
        f"lines L{int(line_start)}-{int(line_end)}]"
    )


# vector A: serialize read-modify-write in
# `_md_watch_state_save`. atomic write (tmp + os.replace) prevented
# torn write but the surrounding read → modify → write sequence remained
# non-atomic → concurrent PersistentSyncDaemon + autocoord _loop save calls
# could lost-update each other → per-file baseline silent reset →
# `_md_watch_compute_appended_range` full-file fallback → MD-WATCH replay
# spam → trust boundary mitigation bypass via replay vector.
# Layer 1 = threading.Lock for intra-process (race window 1).
# Layer 2 = OS advisory file lock for cross-process (race window 2 =
# double GUI / external script / user 手動編集). Separate `.lock` file
# because state.json is replaced atomically via os.replace, which
# invalidates any fd held on the original inode.
_md_watch_state_lock = threading.Lock()

try:
    import msvcrt as _md_watch_msvcrt
    _MD_WATCH_FLOCK_KIND = "windows"
except ImportError:
    _md_watch_msvcrt = None
    try:
        import fcntl as _md_watch_fcntl
        _MD_WATCH_FLOCK_KIND = "posix"
    except ImportError:
        _md_watch_fcntl = None
        _MD_WATCH_FLOCK_KIND = None  # unsupported platform → degrade to threading.Lock only


def _md_watch_state_acquire_flock(lock_fp) -> None:
    """Best-effort exclusive cross-process advisory lock on lock_fp."""
    if _MD_WATCH_FLOCK_KIND == "windows" and _md_watch_msvcrt is not None:
        try:
            _md_watch_msvcrt.locking(lock_fp.fileno(), _md_watch_msvcrt.LK_LOCK, 1)
        except OSError:
            pass
    elif _MD_WATCH_FLOCK_KIND == "posix" and _md_watch_fcntl is not None:
        try:
            _md_watch_fcntl.flock(lock_fp.fileno(), _md_watch_fcntl.LOCK_EX)
        except OSError:
            pass


def _md_watch_state_release_flock(lock_fp) -> None:
    """Release cross-process advisory lock on lock_fp."""
    if _MD_WATCH_FLOCK_KIND == "windows" and _md_watch_msvcrt is not None:
        try:
            _md_watch_msvcrt.locking(lock_fp.fileno(), _md_watch_msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    elif _MD_WATCH_FLOCK_KIND == "posix" and _md_watch_fcntl is not None:
        try:
            _md_watch_fcntl.flock(lock_fp.fileno(), _md_watch_fcntl.LOCK_UN)
        except OSError:
            pass


@contextlib.contextmanager
def _locked_coord_write(target_file: Path,
                        threading_lock: "threading.Lock | None" = None):
    """Cycle 14 (26-05-05) — unified 2-layer write lock for coord/* files.

    Yields `target_file` after acquiring (1) an optional caller-supplied
    threading.Lock for intra-process serialization, then (2) an OS
    advisory flock on a `.lock` sibling for cross-process serialization.
    Both layers are released on context exit. flock failure degrades
    silently to Layer 1 only (POSIX/Windows platform compat already
    handled by the underlying `_md_watch_state_acquire_flock` helper —
    naming-carryover from origin, generic on any fp).

    Unifies the 3 ad-hoc lock sites that grew during mitigation:
      - `_md_watch_state_save` (state.json) — keeps its dedicated
        impl due to atomic load+modify+write complexity, not migrated.
      - `_peer_hint_scan` (peer_*.md append) — migrated below to
        use this helper.
      - `_inbox_write_lock` (cross-project inbox) — migrated to
        use this helper, gaining the cross-process flock layer that the
        original mitigation lacked (defense-in-depth extension).
    """
    lock_path = target_file.parent / f"{target_file.name}.lock"
    lock_fp = None
    tlock_acquired = False
    try:
        if threading_lock is not None:
            threading_lock.acquire()
            tlock_acquired = True
        try:
            lock_fp = open(lock_path, "ab")
            _md_watch_state_acquire_flock(lock_fp)
        except OSError:
            lock_fp = None
        yield target_file
    finally:
        if lock_fp is not None:
            try:
                _md_watch_state_release_flock(lock_fp)
            except Exception:
                pass
            try:
                lock_fp.close()
            except Exception:
                pass
        if tlock_acquired:
            try:
                threading_lock.release()
            except Exception:
                pass


def _md_watch_state_path(coord: Path) -> Path:
    """Sidecar JSON persisting per-file mtime/hash/prev across GUI restarts.

    Without this, post-restart first-seen baseline silently absorbs any
    appends made while GUI was down → delta=0 → 配送消失 .
    """
    return coord / ".md_watch_state.json"


def _md_watch_state_load(coord: Path, file_mtime: dict, file_hash: dict,
                         file_prev: dict, first_seen: set,
                         key_prefix: str) -> bool:
    """Load persisted state into in-memory caches. Returns True if loaded.

    Marks files as first_seen so the next tick computes delta against the
    persisted prev_content (not silent baseline). Idempotent: skips if a
    `__loaded__` sentinel already in first_seen.
    """
    sentinel = f"{key_prefix}:__loaded__" if key_prefix else "__loaded__"
    if sentinel in first_seen:
        return False
    first_seen.add(sentinel)
    p = _md_watch_state_path(coord)
    if not p.exists():
        return False
    try:
        data = json.loads(p.read_text("utf-8"))
    except Exception:
        return False
    for name, rec in data.items():
        try:
            key = f"{key_prefix}:{coord / name}" if key_prefix else str(coord / name)
            file_mtime[key] = float(rec.get("mtime", 0))
            file_hash[key] = str(rec.get("hash", ""))
            file_prev[key] = str(rec.get("prev", ""))
            first_seen.add(key)
        except Exception:
            continue
    return True


def _md_watch_state_save(coord: Path, name: str, mtime: float,
                         chash: str, prev: str) -> None:
    """Persist a single file's mtime/hash/prev to sidecar JSON.

    Best-effort: silent on any I/O error (state is recoverable from disk).
: atomic write via tmp + os.replace — concurrent persistent
    daemon + autocoord were racing on direct write_text → JSON corrupt →
    state lost → baseline re-capture loop (false silent dispatches).
     vector A:
      Layer 1 (threading.Lock): intra-process serialization of the
        read → modify → write sequence; atomic write alone left
        intra-process lost-update window open.
      Layer 2 (OS advisory flock on `.lock` sibling): cross-process
        serialization (double GUI / external script / user manual edit).
        Best-effort — silently degrades to Layer 1 if unsupported.
    """
    p = _md_watch_state_path(coord)
    lock_path = p.with_suffix(p.suffix + ".lock")
    with _md_watch_state_lock:
        lock_fp = None
        try:
            lock_fp = open(lock_path, "ab")
            _md_watch_state_acquire_flock(lock_fp)
        except OSError:
            lock_fp = None
        try:
            try:
                data = json.loads(p.read_text("utf-8")) if p.exists() else {}
            except Exception:
                data = {}
            data[name] = {"mtime": mtime, "hash": chash, "prev": prev}
            try:
                tmp = p.with_suffix(p.suffix + ".tmp")
                tmp.write_text(json.dumps(data), encoding="utf-8")
                os.replace(str(tmp), str(p))
            except Exception:
                pass
        finally:
            if lock_fp is not None:
                try:
                    _md_watch_state_release_flock(lock_fp)
                except Exception:
                    pass
                try:
                    lock_fp.close()
                except Exception:
                    pass


def _md_watch_compute_appended_range(prev_text: str, curr_text: str) -> "tuple[int, int]":
    """For an append-only delta, return (line_start, line_end) 1-indexed.

    line_start = first newly-appended line number, line_end = last.
    For non-append (edit/shrink), return (1, len(curr_text.splitlines())) so
    the role re-reads the whole file (rare; falls back to tail-N elsewhere).
     vector A: prev_text without trailing newline is a *byte* prefix
    of curr_text but not a *line* prefix — extending the last line and
    appending new ones makes startswith True while splitlines counts the
    extended last line as one line, silently omitting the byte-extension
    (= attacker injection vector) from the delivered range. Require
    prev_text to end with `\n` so any extension to the last line forces
    full-file fallback at line 3094.
    """
    prev_lines = prev_text.splitlines() if prev_text else []
    curr_lines = curr_text.splitlines() if curr_text else []
    if not curr_lines:
        return (1, 0)
    # Append-only: prev is a *line-aligned* prefix of curr.
    if prev_text and prev_text.endswith("\n") and curr_text.startswith(prev_text):
        line_start = len(prev_lines) + 1
        line_end = len(curr_lines)
        if line_start > line_end:
            line_start = line_end
        return (line_start, line_end)
    # Default: full-file range (caller should consider tail-N fallback).
    return (1, len(curr_lines))


# ---------------------------------------------------------------------------
# — Anthropic prompt caching helpers (direct-API path only)
# ---------------------------------------------------------------------------
# The 19-role coordination harness drives `claude-code` CLI subprocesses, so
# these helpers don't touch the autonomous coordination path (cache_control
# is an Anthropic SDK concept; the CLI manages its own caching internally).
# These helpers ARE used by `_send_api` (chat-tab streaming) and the project
# wizard's role-proposer call, where main.py speaks directly to the
# anthropic SDK.
#
# Strategy:
#   - System prompt: 1h TTL (write 2× cost amortizes across multi-turn chat).
#   - Tools (last entry): 5m TTL (default, tools rarely change mid-session).
#   - Static turn context (history before the current user turn): 5m TTL.
#   - Up to 4 breakpoints; we use 3 (system + tools + history) to leave one
#     for caller-specific extension.
#
# Token-count-vs-min check: `PROMPT_CACHE_MIN_TOKENS = 4096` for Opus 4.7.
# A breakpoint with fewer than this many input tokens won't actually cache
# (Anthropic silently no-ops). We expose a coarse char/4 estimator.
# ---------------------------------------------------------------------------
def _approx_token_count(text: str) -> int:
    """Coarse token estimator (≈char/4). Sufficient for breakpoint min check."""
    return max(0, len(text or "") // 4)


def build_cached_system_block(system_text: str, ttl: str = PROMPT_CACHE_SYSTEM_TTL) -> "list[dict] | str":
    """Wrap system prompt in a cache_control block when caching is enabled.

    Returns a list with one block (Anthropic structured-system shape) when
    caching is enabled and the prompt is large enough to cache; otherwise
    returns the raw string for legacy compatibility.
    """
    if not PROMPT_CACHE_ENABLED or not system_text:
        return system_text
    if _approx_token_count(system_text) < PROMPT_CACHE_MIN_TOKENS:
        # Below cache breakpoint min — emit uncached to avoid silent no-op.
        return system_text
    return [{
        "type": "text",
        "text": system_text,
        "cache_control": {"type": "ephemeral", "ttl": ttl},
    }]


def attach_tools_cache_marker(tools: "list[dict]", ttl: str = PROMPT_CACHE_TOOLS_TTL) -> "list[dict]":
    """Attach cache_control marker to the LAST tool entry (Anthropic spec).

    Idempotent: if the last tool already has cache_control, returns unchanged.
    Returns a new list (never mutates the input).
    """
    if not PROMPT_CACHE_ENABLED or not tools:
        return list(tools or [])
    out = [dict(t) for t in tools]
    if "cache_control" not in out[-1]:
        out[-1]["cache_control"] = {"type": "ephemeral", "ttl": ttl}
    return out


def attach_history_cache_marker(messages: "list[dict]") -> "list[dict]":
    """Mark the boundary between cached static history and the current turn.

    The LAST message before the current user turn gets a cache_control block.
    Caller passes the full list including the current user message; the
    second-to-last entry is marked. Returns a new list (never mutates).
    """
    if not PROMPT_CACHE_ENABLED or not messages or len(messages) < 2:
        return list(messages or [])
    out = [dict(m) for m in messages]
    boundary = out[-2]
    content = boundary.get("content", "")
    if isinstance(content, str):
        # Convert the str content to the structured-content shape so we can
        # attach cache_control to the trailing block.
        out[-2] = {
            **boundary,
            "content": [{
                "type": "text",
                "text": content,
                "cache_control": {"type": "ephemeral"},
            }],
        }
    elif isinstance(content, list) and content and isinstance(content[-1], dict):
        # Already structured — attach to the last block if not present.
        new_content = [dict(b) for b in content]
        if "cache_control" not in new_content[-1]:
            new_content[-1]["cache_control"] = {"type": "ephemeral"}
        out[-2] = {**boundary, "content": new_content}
    return out


# ---------------------------------------------------------------------------
# MD-WATCH delivery — shared between AutoCoordinator._poll_file_generic and
# PersistentSyncDaemon._run_md_watch_fallback. Before extraction these were
# ~120 lines of near-identical code; behavior drifted twice when fixes were
# applied to one path but not the other (see comment trail "mirrors
# AutoCoordinator ._poll_file_generic" / "user-surfaced regression fix").
# Behavior is bit-for-bit equivalent to the previous duplicated paths;
# only the per-caller knobs (caches, key prefix, logger, label, JS emit
# hook) are parameterized.
# ---------------------------------------------------------------------------
# Module-level shared coalescer instance.: 30s debounce + 90s
# ceiling, gated on MDWATCH_COALESCE_ENABLED. Both AutoCoordinator and the
# persistent fallback share this so a burst that crosses a daemon swap is
# still coalesced. Restart of the GUI resets state (acceptable — the next
# tick's mtime-changed branch will see fresh content).
_MD_WATCH_COALESCER = _MdWatchCoalescer()


# Role abbreviation → canonical role id. Supports `(to:dev)` / `@mgr` shorthands
# in coord file content while harness tabs use full role names. Unknown tokens
# pass through unchanged so future custom roles still work.
_ROLE_ABBREV_TO_FULL = {
    "ans": "analyst", "cnt": "content",
    "dev": "developer", "dsn": "designer", "lgl": "legal",
    "mgr": "manager", "mkt": "marketing", "prd": "product",
    "rev": "reviewer", "rvo": "revops", "sec": "security",
    "surf": "surfer", "grw": "growth", "qa": "qa",
    "sre": "sre", "cco": "cco", "pa": "pa", "docs": "docs",
}


def _normalize_role_tokens(tokens) -> "set[str]":
    """Expand abbreviations (dev→developer) so they match harness role IDs."""
    out = set()
    for tok in tokens:
        t = (tok or "").strip().lower()
        if not t:
            continue
        out.add(_ROLE_ABBREV_TO_FULL.get(t, t))
    return out


def _md_watch_resolve_targets_by_content(
    file_name: str,
    delta_text: str,
    fallback_role_ids: "set[str] | None",
    project_path: str,
) -> "set[str] | None":
    """Refine MD-WATCH targets via content analysis (skip-reduction).

    Priority:
    1. **Explicit `(to:role1,role2)` directive** in delta_text → use only those.
       Format: anywhere in the recent delta, e.g. "B007|H|site-publish (to:mkt,lgl)".
    2. **`@<role>` mentions** (1+) → the mentioned roles only (no broadcast).
    3. **Graph-spread** from extracted T##### / @<role> tokens → top-3 roles by
       activation score (synapse-like routing).
    4. **fallback_role_ids** — original interest-map default (mgr/cco etc.).

    Returns None to mean "use fallback" (broadcast / no-change). Returns a set
    of role ids to scope the dispatch. The caller is responsible for filtering
    tabs by those role ids.

    Goal: reduce mgr/cco over-broadcast — every blocker / decision push
    historically went to mgr+cco regardless of content. Now content-concrete
    entries (with `to:` or `@role` markers) skip mgr/cco unless explicitly
    listed, while truly-routine entries still fall back to the audit chain.
    """
    if not delta_text:
        return fallback_role_ids
    # 1. Explicit (to:roles) directive — both parenthesized form `(to:mkt,lgl)`
    # and queue-header form `## [ts] to:role1,role2 priority:P` are accepted.
    # The parenthesized form is tried first (legacy / inline use); the
    # queue-header form is the multi-role dispatch convention per the coordination protocol
    # §Queue. 26-04-27 fix: prior regex required parens so `## [ts] to:mgr,dev
    # priority:A` headers fell through to mgr+cco default and broadcasts to 18
    # other roles silently dropped.
    m = re.search(r"\(\s*to\s*:\s*([a-z][a-z0-9_,\s\-]+)\s*\)", delta_text, re.IGNORECASE)
    if not m:
        # Queue-header form: anchored by `priority:` terminator (or EOL/space)
        # to avoid matching prose like "escalate to: user".
        m = re.search(
            r"(?:^|[\s\[])to\s*:\s*([a-z][a-z0-9_,\s\-]+?)\s+priority\s*:",
            delta_text,
            re.IGNORECASE | re.MULTILINE,
        )
    if m:
        raw = [r for r in m.group(1).split(",") if r.strip()]
        roles = _normalize_role_tokens(raw)
        if roles:
            return roles
    # 2. @<role> mentions — collect first 5
    mentions_raw = []
    for mm in re.finditer(r"@([a-z][a-z0-9_-]{1,16})\b", delta_text, re.IGNORECASE):
        ref = mm.group(1).lower()
        if ref == "all" or ref == "channel":
            continue  # @all / @channel are broadcast indicators, skip
        mentions_raw.append(ref)
        if len(mentions_raw) >= 5:
            break
    if mentions_raw:
        return _normalize_role_tokens(mentions_raw)
    # 3. Graph-spread (only if graph DB exists in project)
    if project_path:
        db_path = Path(project_path) / "coordination" / "graph" / "decisions.sqlite"
        if db_path.exists():
            seeds = []
            for tm in re.finditer(r"\bT\d{5}\b", delta_text):
                seeds.append(f"task_{tm.group(0)}")
            if seeds:
                try:
                    import importlib.util
                    cli_path = Path(project_path) / "tools" / "graph" / "cli.py"
                    if cli_path.exists():
                        spec = importlib.util.spec_from_file_location("_md_watch_route_loader", cli_path)
                        mod = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(mod)
                        from tools.graph.schema import init_db
                        conn = init_db(str(db_path))
                        try:
                            rows = mod.spread_activation(
                                conn, list(set(seeds)),
                                max_hops=2, decay_per_hop=0.5, top_n=8,
                            )
                            graph_roles_raw = [r["role"] for r in rows
                                               if r.get("type") == "role" and r.get("role")]
                            if graph_roles_raw:
                                return _normalize_role_tokens(graph_roles_raw[:3])
                        finally:
                            conn.close()
                except Exception:
                    pass  # Best-effort — fall through to defaults
    # 4. Fallback to interest-map default
    return fallback_role_ids


def _graph_enrich_msg(msg: str, file_name: str, body_text: str, project_path: str) -> str:
    """Append a [GRAPH-CTX] block to a dispatch message when relevant.

    Strategy: extract task ids / role mentions from body_text, run a graph
    search/spread, and append top-N related nodes as a small block. Roles
    receiving the dispatch see relevant historical context for free without
    needing to manually query the graph CLI.

    Bounded: top-3 nodes × ~50 chars title = ~150 token max block.
    Best-effort: any failure → return msg unchanged (graph is non-critical).
    """
    if not GRAPH_ENFORCE_ENABLED or not project_path:
        return msg
    is_target = (
        file_name in GRAPH_ENFORCE_FILES
        or (file_name and file_name.startswith(GRAPH_ENFORCE_PEER_PREFIX) and file_name.endswith(".md"))
    )
    if not is_target:
        return msg
    db = Path(project_path) / "coordination" / "graph" / "decisions.sqlite"
    if not db.exists():
        return msg
    # Pull seeds from body_text: task IDs (T#####) + role mentions (@<role>)
    seeds: list[str] = []
    for m in re.finditer(r"\bT\d{5}\b", body_text):
        seeds.append(f"task_{m.group(0)}")
    for m in re.finditer(r"@([a-z][a-z0-9_-]*)\b", body_text):
        seeds.append(f"role_{m.group(1).lower()}")
    if not seeds:
        return msg
    # Dedup, cap at 5 seeds
    seen = set()
    capped: list[str] = []
    for s in seeds:
        if s not in seen:
            seen.add(s)
            capped.append(s)
        if len(capped) >= 5:
            break
    try:
        import importlib.util
        cli_path = Path(project_path) / "tools" / "graph" / "cli.py"
        if not cli_path.exists():
            return msg
        spec = importlib.util.spec_from_file_location("_graph_enrich_loader", cli_path)
        if not spec or not spec.loader:
            return msg
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        from tools.graph.schema import init_db
        conn = init_db(str(db))
        try:
            rows = mod.spread_activation(
                conn, capped,
                max_hops=2, decay_per_hop=0.5,
                top_n=GRAPH_ENFORCE_TOP_N,
            )
        finally:
            conn.close()
    except Exception:
        return msg
    if not rows:
        return msg
    block = "\n[GRAPH-CTX]\n"
    for r in rows:
        title = (r.get("title") or "").replace("\n", " ").strip()[:120]
        block += f"  {r.get('type','?')[:4]} {r.get('role','-'):>4} :: {title}\n"
    block += "(Use `python tools/graph/cli.py related --node <id>` to expand.)\n"
    return msg + block


def _md_watch_process_coord_dir(
    api,
    manager_tab,
    coord: Path,
    *,
    excluded: "set[str]",
    file_mtime: dict,
    file_hash: dict,
    file_prev: dict,
    first_seen: set,
    key_prefix: str,
    log_evt,
    js_emit=None,
    label: str = "",
    running_check=None,
) -> None:
    """Single-pass file delta watcher with per-target dispatch.

    Iterates `coord/*.md`, skips `excluded` and `*_report.md`, applies
    mtime+hash fast-path, baselines first-seen files silently, and emits
    [MD-WATCH] dispatches for changed files. Caches are advanced only on
    successful delivery (永続送信 retry guarantee).

    Knobs:
      excluded     — files to skip (e.g. _GENERIC_WATCH_EXCLUDE).
      file_mtime/hash/prev/first_seen — per-caller caches; key_prefix is
                     prepended so callers can share storage if needed.
      log_evt(kind,msg) — caller-specific event logger.
      js_emit(delivered_names, name) — JS-side event hook (None = no emit).
      label        — appended to event/footer for log readability
                     (e.g. "persistent-daemon").
      running_check — optional callable; if returns False the loop breaks
                      (used by AutoCoord to stop mid-glob).
    """
    import hashlib
    #: load persisted state at first invocation so post-restart
    # appends compute delta vs persisted prev (not silent baseline).
    _md_watch_state_load(coord, file_mtime, file_hash, file_prev,
                          first_seen, key_prefix)
    try:
        candidates = sorted(coord.glob("*.md"))
    except Exception:
        return
    for path in candidates:
        if running_check is not None and not running_check():
            break
        name = path.name
        if name in excluded or name.endswith("_report.md"):
            continue
        key = f"{key_prefix}:{path}" if key_prefix else str(path)
        try:
            mtime = path.stat().st_mtime
        except Exception:
            continue
        if file_mtime.get(key) == mtime:
            continue  # Fast-path: mtime unchanged.
        try:
            content = path.read_text("utf-8", errors="replace")
        except Exception:
            continue
        # (26-05-05) read-stability guard: defend against
        # torn-read race when a writer (Worker subprocess Claude PTY
        # Edit/Write tool) is mid-write. Without this guard, mid-write
        # content lands in file_prev cache; the next tick re-reads full
        # content → startswith check fails → fallback path (1, len(curr))
        # replays the entire file to the receiver. Mitigation: brief
        # sleep + re-read + hash compare; mismatch = caches don't advance,
        # next tick retries with stable content. POSIX/Windows passive
        # detection (no writer-side coordination required). Latency cost
        # is 50ms only on files that pass the mtime fast-path above —
        # typically 0-2 files per tick.
        try:
            time.sleep(0.05)
            content_reread = path.read_text("utf-8", errors="replace")
        except Exception:
            continue
        if content != content_reread:
            log_evt("md_torn_read", f"{name} torn-read detected, retry next tick")
            continue
        chash = hashlib.md5(content.encode("utf-8", errors="replace")).hexdigest()[:12]
        if file_hash.get(key) == chash:
            # Content identical despite mtime change (atomic save etc.).
            file_mtime[key] = mtime
            continue
        # Baseline first-seen: snapshot, no dispatch.
        if key not in first_seen:
            first_seen.add(key)
            file_mtime[key] = mtime
            file_hash[key] = chash
            file_prev[key] = content
            #: persist baseline so restart can resume from here.
            try:
                _md_watch_state_save(coord, name, mtime, chash, content)
            except Exception:
                pass
            if label:
                log_evt("md_baseline", f"{name} baseline captured ({label}, hash={chash})")
            else:
                log_evt(
                    "md_baseline",
                    f"{name} baseline (hash={chash}, size={len(content)}, "
                    f"lines={len(content.splitlines())})",
                )
            continue
        #: coalesce same-file events within debounce window.
        # Note this event; if the debounce/ceiling hasn't elapsed yet, skip
        # this dispatch — caches stay un-advanced so the next tick re-evaluates.
        # On the tick when should_emit_now() returns True, the FRESHEST content
        # (current `content`/`chash`) is what gets dispatched (we always use
        # the latest read), so coalescing is "silence-then-emit-latest".
        if _MD_WATCH_COALESCER.is_enabled():
            import time as _t
            now = _t.time()
            # Key by full `key` (path/prefix-qualified), not bare filename — in a
            # multi-project hub every project's coordination/ has same-named
            # files (blockers.md, decisions_log.md …); bare-name keying would
            # cross-coalesce their debounce windows.
            _MD_WATCH_COALESCER.note_event(key, now)
            if not _MD_WATCH_COALESCER.should_emit_now(key, now):
                # Defer: caches NOT advanced, retry next tick.
                log_evt("md_coalesce_defer", f"{name} buffered (debounce)")
                continue
        # Real change: build delta + message.
        prev_content = file_prev.get(key, "")
        delta_text, delta_kind, lines_added, lines_removed = \
            AutoCoordinator._compute_md_delta(prev_content, content, name)
        prev_hash = file_hash.get(key, "")
        prefix_hdr = f"[MD-WATCH from coordination/{name}]"
        footer_label = f" ({label})" if label else ""
        body_label = f", {label}" if label else ""
        footer_hdr = (
            f"[MD-WATCH-FOOTER from coordination/{name}] "
            f"kind={delta_kind} +{lines_added}/-{lines_removed} hash={chash}{footer_label}"
        )
        # 2026-04-29 mgr direct fix (user request "起動時と同様にPTYで PEER-INBOX
        # 形式で update も送る"): peer_<role>.md 更新検知時は spawn-time
        # _send_peer_inbox_hint と同じ軽量 [PEER-INBOX from coordination/peer_<role>.md]
        # 形式で配信 (full delta_text body は不送、role が tail-10 を Read で pull)。
        # rationale: (a) spawn と update で format 一致 = role 側 handler 統一、
        # (b) body 抜きで token 節約 + recipient screen に過去 entry の重複露出回避、
        # (c) role-private mailbox なので body 不要 = privacy guard 追加保険、
        # (d) MD-PULL pointer_mode と同 layer の "push notification → pull body"
        # paradigm。inbox_mode を pointer_mode より先 (elif chain head) に置くこ
        # とで、MDWATCH_POINTER_MODE=True 時の global toggle override から peer_*.md
        # を確実遮断 (defensive guard、_md_watch_pointer_mode_for 自体は
        # 2026-04-29 で peer 経路除去済だが elif 先行で多重防御)。
        if _md_watch_mailbox_recipient(name):
            content_lines = sum(1 for ln in content.splitlines()
                                if ln.strip() and not ln.strip().startswith("---"))
            msg = (
                f"[PEER-INBOX from coordination/{name}]\n"
                f"Mailbox updated: {content_lines} non-empty line(s) "
                f"(+{lines_added}/-{lines_removed} since last poll, hash={chash}).\n"
                f"Read tail-10 to pick up the latest directed messages.\n"
            )
        #: pointer mode (push→pull) — emit a [MD-PULL] line instead
        # of full body. Roles use Read tool offset/limit to fetch the lines.
        # Hash mismatch → tail-N fallback hint embedded in the pointer.
        # 2026-04-27 Layer 0a: per-file allowlist MDWATCH_POINTER_MODE_FILES
        # extends pointer mode to audit/reference docs (decisions_log etc.)
        # while keeping the global toggle off until full soak.
        elif _md_watch_pointer_mode_for(name) and not _md_watch_force_full_body(delta_text):
            line_start, line_end = _md_watch_compute_appended_range(prev_content, content)
            pointer_line = _md_watch_format_pointer(
                name,
                from_hash=prev_hash or "first",
                to_hash=chash,
                lines_added=lines_added,
                lines_removed=lines_removed,
                line_start=line_start,
                line_end=line_end,
            )
            msg = (
                f"{pointer_line}\n"
                f"Read coordination/{name} with offset={line_start} limit={max(1, line_end - line_start + 1)}. "
                f"On hash mismatch, tail-{MDWATCH_POINTER_TAIL_FALLBACK_LINES} fallback (Read with offset=-{MDWATCH_POINTER_TAIL_FALLBACK_LINES}).\n"
            )
        # 2026-04-26 footer suppression (default).
        # The "Review and adjust your queue" scripted prompt + per-delivery
        # FOOTER debug line add ~60-100 token / msg with no decision value
        # (model already decides when to act). Set MD_WATCH_DEBUG_FOOTER=True
        # to restore both lines for debugging delta routing.
        elif MD_WATCH_DEBUG_FOOTER:
            msg = (
                f"[MD-WATCH from coordination/{name}]\n"
                f"Change: {delta_kind} (+{lines_added} -{lines_removed} lines, "
                f"size {len(prev_content)}→{len(content)}, hash {chash}{body_label}).\n"
                f"---\n"
                f"{delta_text}\n"
                f"---\n"
                f"Review and adjust your queue / next action if relevant.\n"
                f"{footer_hdr}\n"
            )
        else:
            msg = (
                f"[MD-WATCH from coordination/{name}]\n"
                f"Change: {delta_kind} (+{lines_added} -{lines_removed} lines, "
                f"size {len(prev_content)}→{len(content)}, hash {chash}{body_label}).\n"
                f"---\n"
                f"{delta_text}\n"
                f"---\n"
            )
        # Routing: file-owner exclusion + scoped/broadcast fanout.
        proj_path = getattr(manager_tab, "project_path", "") or str(coord.parent)
        # 2026-04-27 GRAPH ENFORCEMENT: inject [GRAPH-CTX] block into dispatches
        # for blockers / peer_<role> so recipients see relevant past decisions
        # without manually querying. Bounded to top-3 nodes.
        #: defensive try/except — graph subsystem failure must not
        # silently drop the dispatch.
        try:
            msg = _graph_enrich_msg(msg, name, content, proj_path)
        except Exception as _ge:
            log_evt("md_watch_graph_enrich_fail", f"{name}: {_ge}")
        # 2026-04-27 content-aware routing: pass delta_text to _md_watch_targets
        # so blockers/decisions_log entries with (to:roles) / @<role> / T###
        # tokens get scoped to actual stakeholders instead of mgr+cco default.
        #: targets resolve は dispatch の前提、例外時は manager fallback
        # しないと silent drop 発生 (peer 死んでる症状の根本原因 #1)。
        try:
            targets = _md_watch_targets(api, proj_path, name, delta_text=delta_text)
        except Exception as _te:
            # 2026-04-28 mailbox privacy guard (mgr direct fix): on resolve
            # exception, mailbox files MUST NOT fall back to manager_tab —
            # role-private leak. Empty list lets the next branch's mailbox
            # guard handle the watchdog/skip path uniformly.
            if _md_watch_mailbox_recipient(name):
                log_evt("md_watch_targets_fail",
                        f"{name}: {_te} (mailbox file: skip, no mgr fallback)")
                targets = []
            else:
                log_evt("md_watch_targets_fail", f"{name}: {_te} (fallback to manager_tab)")
                targets = [manager_tab]
        if not targets:
            # No-broadcast policy guard (peer_channel.md): empty interest set
            # means EXCLUDE was bypassed somehow — DO NOT fall back to
            # manager_tab (would re-introduce the leak).
            if _md_watch_interest_targets(name) == set():
                continue
            # 2026-04-28 mailbox privacy guard (mgr direct fix, user-surfaced
            # "graph-ctxのみが関係ないタブに送られる" symptom): peer_<role>.md
            # is role-private. When the recipient tab isn't running (busy/
            # permission/dead), DO NOT fall back to manager_tab — that
            # silently leaks role-private mailbox content to mgr. Use the
            # watchdog defer-counter so the cache eventually advances and
            # the next mailbox edit isn't queued behind a stuck delivery.
            if _md_watch_mailbox_recipient(name):
                # Pull diag from _md_watch_targets one-shot (set on api).
                diag_str = ""
                try:
                    last_diag = getattr(api, "_md_watch_last_mailbox_diag", None)
                    if last_diag and last_diag[0] == name:
                        diag_str = f" diag={last_diag[2]}"
                except Exception:
                    pass
                cnt = _MD_WATCH_DEFER_COUNT.get(key, 0) + 1
                _MD_WATCH_DEFER_COUNT[key] = cnt
                if cnt < _MD_WATCH_DEFER_LIMIT:
                    log_evt(
                        "md_watch_mailbox_skip",
                        f"{name}: recipient tab unavailable "
                        f"(defer {cnt}/{_MD_WATCH_DEFER_LIMIT}){diag_str}",
                    )
                    continue
                log_evt(
                    "md_watch_mailbox_drop",
                    f"{name}: recipient unavailable {cnt} polls, "
                    f"cache force-advanced (delta dropped, hash={chash}){diag_str}",
                )
                _MD_WATCH_DEFER_COUNT[key] = 0
                file_mtime[key] = mtime
                file_hash[key] = chash
                file_prev[key] = content
                try:
                    _md_watch_state_save(coord, name, mtime, chash, content)
                except Exception:
                    pass
                continue
            targets = [manager_tab]  # Fresh project (no role tabs yet).
        delivered_to = []
        skipped_prompt = []
        skipped_busy = []
        for tgt in targets:
            # Per-target permission_prompt skip — writing during a yes/no
            # prompt could auto-confirm. busy_generating is OK to write to.
            #: busy_generating は permission_prompt と同様 defer
            # する (caches 未 advance で next tick retry)。 過去設計は busy 中も
            # write 強行 → claude 応答中の interleave で「完全な形式で来ない」
            # 症状の主因。 retry guarantee は pty queue 永続化 ではなく caches
            # 次 tick 再評価で担保する。
            try:
                ok_q, qreason = tgt.pty_session.is_quiescent(strict=False)
            except Exception:
                # quiesce-fail-defer: when quiesce check raises, prefer
                # safety-side defer (False) rather than proceed (True) — the
                # write path can interleave with a permission prompt the
                # caller didn't see. Caches stay un-advanced; next tick
                # retries with a fresh evaluation.
                ok_q, qreason = (False, "quiesce_check_failed")
                try:
                    log_evt("md_watch_quiesce_check_fail",
                            f"tgt={getattr(tgt, 'id', '?')}")
                except Exception:
                    pass
            if not ok_q and qreason == "permission_prompt":
                skipped_prompt.append((tgt, qreason))
                continue
            if not ok_q and qreason == "busy_generating":
                skipped_busy.append((tgt, qreason))
                continue
            try:
                ok_w, wreason = tgt.pty_session.write_dispatch(msg)
            except Exception as e:
                ok_w, wreason = (False, f"exception:{e}")
            if not ok_w:
                if wreason in ("pty_dead", "write_failed"):
                    log_evt("md_watch_fail", f"PTY error: {name}→@@{_safe_tab_name(tgt)} ({wreason})")
                else:
                    log_evt("dispatch_rejected", f"md_watch [{name}]→@@{_safe_tab_name(tgt)}: {wreason}")
                continue
            delivered_to.append(tgt)
        if skipped_prompt:
            sp_str = ",".join(f"@@{_safe_tab_name(t)}({r})" for t, r in skipped_prompt)
            persistent_str = "persistent " if label else ""
            log_evt("md_watch_skip_prompt", f"{persistent_str}{name} skipped (permission_prompt): {sp_str}")
        if skipped_busy:
            sb_str = ",".join(f"@@{_safe_tab_name(t)}({r})" for t, r in skipped_busy)
            persistent_str = "persistent " if label else ""
            log_evt("md_watch_skip_busy", f"{persistent_str}{name} deferred (busy_generating): {sb_str}")
        if not delivered_to:
            # watchdog (26-04-28 mgr direct fix): bounded defer to prevent
            # permanent silent block. Single-target mailbox files (peer_<role>.md)
            # have ZERO redundancy — when the lone recipient tab sits on a stale
            # busy_generating / permission_prompt marker in tail-15, every poll
            # tick re-defers and state never advances → 18h+ stale state observed
            # in production (peer_security/peer_manager/decisions_log all stuck
            # at 04-27 14:54). Pa_brief/cco_brief succeeded because broadcast
            # fanout had ≥1 quiescent recipient. Fix: after _MD_WATCH_DEFER_LIMIT
            # consecutive defers for the same key, force-advance cache (this delta
            # is dropped) so subsequent edits aren't queued behind the stuck one.
            # permission_prompt safety: a long-stuck prompt = user-side action
            # required, not a delivery problem we can solve in the daemon.
            cnt = _MD_WATCH_DEFER_COUNT.get(key, 0) + 1
            _MD_WATCH_DEFER_COUNT[key] = cnt
            if cnt < _MD_WATCH_DEFER_LIMIT:
                continue  # normal retry
            log_evt("md_watch_force_advance",
                    f"{name}: {cnt} consecutive defers, cache force-advanced "
                    f"(delta dropped, hash={chash})")
            _MD_WATCH_DEFER_COUNT[key] = 0
            # fall through to advance caches + state
        else:
            _MD_WATCH_DEFER_COUNT[key] = 0  # success → reset counter
        file_mtime[key] = mtime
        file_hash[key] = chash
        file_prev[key] = content
        #: persist for restart resilience.
        try:
            _md_watch_state_save(coord, name, mtime, chash, content)
        except Exception:
            pass
        delivered_names = ",".join(_safe_tab_name(t) for t in delivered_to)
        if label:
            log_evt(
                "md_watch",
                f"{name} → [{delivered_names}] ({delta_kind}, +{lines_added}/-{lines_removed})",
            )
        else:
            log_evt(
                "md_watch",
                f"coordination/{name} → [{delivered_names}] "
                f"({delta_kind}, +{lines_added}/-{lines_removed}, hash={chash})",
            )
        if js_emit is not None:
            try:
                js_emit(delivered_names, name)
            except Exception:
                pass
        for tgt in delivered_to:
            try:
                api._render_dispatched(tgt, prefix_hdr, msg, "md_watch")
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Auto Coordinator — multi-role autonomous dispatch loop
# ---------------------------------------------------------------------------
class AutoCoordinator:
    """Multi-role autonomous coordination engine.

    Two parallel dispatch channels, both monitored every loop iteration:

    **Screen-based** (original, fast path for ad-hoc instructions):
    1. User → Manager tab  (inject())
    2. Manager replies with "@developer: ...", "@marketing: ...", "@security: ..."
    3. `_poll_dispatch` parses manager's visible screen and writes instructions
       into each target worker's PTY.
    4. Worker executes and creates a git commit.
    5. `_poll_commits` detects the new HEAD and writes a report back to
       Manager's PTY ("[WORKER-REPORT from ...]").

    **File-based** (persistent, works without git):
    A. **Legacy (retired)** — Manager wrote tasks to a single queue file.
       Now: Manager writes to `coordination/peer_<role>.md` directly.
       LEGACY_QUEUE_DISPATCH_ENABLED=False disables _poll_file_dispatch.
    B. `_poll_file_dispatch` legacy reader (no-op when feature flag off).
    C. Worker writes results to `coordination/<role>_report.md` using the
       `## [ts] task_id` entry format.
    D. `_poll_file_reports` detects new/changed entries and writes a summary
       into Manager's PTY.
    E. **NEW dispatch path**: Manager appends `## [ts] from:mgr` entries to
       `coordination/peer_<role>.md`; MD-WATCH fanout (mailbox routing in
       `_md_watch_targets`) delivers to that role's tab via `_poll_file_generic`.

    Both channels share `_cycles` / `_log` for observability; dedupe state is
    separate so ad-hoc @role instructions and queue-file tasks don't collide.
    """

    # @role: and `## [ts] to:<role>` regexes are rebuilt from RoleRegistry on
    # each start() so newly-added custom roles are immediately picked up.
    # Task/report line formats are independent of role names.
    QUEUE_TASK_RE = re.compile(r'^-\s+\[\s\]\s+([A-Za-z0-9_\-]+):\s*(.+)$')
    QUEUE_DONE_RE = re.compile(r'^-\s+\[[xX]\]')
    REPORT_ENTRY_RE = re.compile(r'^##\s+\[([^\]]+)\]\s+([A-Za-z0-9_\-]+)\s*$')

    def __init__(self, api_ref):
        self.api = api_ref
        self.running = False
        self._thread: threading.Thread | None = None
        self._manager_id = ""
        self._manager_project = ""
        # 2026-04-23: multi-tab per role (e.g., 2 developer tabs for parallel work).
        # _workers: role → list of tab_ids in insertion order.
        # _worker_rr: role → round-robin counter (incremented each pick).
        self._workers: dict[str, list[str]] = {}
        self._worker_rr: dict[str, int] = {}
        self._last_commits: dict[str, str] = {}
        # Bounded LRU seen-sets (cap=2000, trim to 1000 when over). See
        # _BoundedSeenSet — the previous raw-set + slice trim was broken
        # (non-deterministic eviction) so dedup marks could be lost and the
        # same task re-dispatched.
        self._dispatched = _BoundedSeenSet(cap=2000)
        # Worker role snapshot at start() — used for file-based report polling.
        self._worker_roles: list[str] = []
        # Dynamic regexes (rebuilt at start() from current RoleRegistry).
        self._dispatch_re: re.Pattern = re.compile(r'(?!x)x')
        self._queue_section_re: re.Pattern = re.compile(r'(?!x)x')
        # File-based coordination state
        self._queue_seen = _BoundedSeenSet(cap=2000)   # task_ids already file-dispatched
        self._report_seen = _BoundedSeenSet(cap=2000)  # report entry hash-keys already relayed
        self._skip_logged: set[str] = set()     # throttle "no tab for role" events
        # 2026-04-23 reliability upgrade: in-memory file state cache.
        # Enables immediate diff-detection without re-parsing unchanged files
        # every poll, and allows us to poll more aggressively (2s) without
        # burning CPU on unchanged content.
        self._file_mtime: dict[str, float] = {}  # str(path) -> last observed mtime
        self._file_hash: dict[str, str] = {}     # str(path) -> last observed content md5[:12]
        # Generic coordination/*.md watcher caches (separate from queue/report
        # caches above so the two channels don't collide on path overlap).
        # Watches all coordination/*.md EXCEPT *_report.md
        # (*_report has its own semantics).
        # 永続送信: write_submit retry on busy Manager preserves delivery
        # guarantee; mtime+hash cache survives for the life of the AutoCoord
        # run, so mtime revert or identical-content rewrites don't re-fire.
        self._generic_file_mtime: dict[str, float] = {}
        self._generic_file_hash: dict[str, str] = {}
        self._generic_first_seen: set[str] = set()  # suppress baseline noise
        # 2026-04-24 strict change extraction: store previous content so the
        # watcher can emit a precise +N/-N line delta instead of a blind tail
        # preview. Enables the Manager to see exactly what changed.
        self._generic_file_prev: dict[str, str] = {}
        self._loop_tick = 0  # iteration counter; git polls throttled to every 4th
        self._cycles = 0
        self._log: list[dict] = []
        #: heartbeat — used by PersistentDaemon to detect zombie
        # state (running=True but _loop crashed/stuck). 0 = never ticked.
        self._last_tick_ts: float = 0.0

    # -- public ---------------------------------------------------------------

    def start(self) -> dict:
        if self.running:
            # Already running. Re-running start() would spawn a second loop
            # thread (the old one is never joined) → every dispatch sent twice
            # and concurrent mutation of the non-locked dedup sets. The
            # role-edit / proposal-apply flow calls stop() first, so this only
            # guards accidental double-starts (double-click, re-entrancy).
            return {"ok": True, "manager": self._manager_id,
                    "workers": dict(self._workers), "already_running": True}
        self._manager_id = ""
        self._workers = {}
        self._manager_project = ""
        # First pass: find the first coordinator tab (becomes manager). Its
        # project's role registry defines the role set for this run. Workers
        # are restricted to tabs in the same project so cross-project traffic
        # is forced through the PeerBridge (explicit alias.role: routing).
        manager_tab: ChatTab | None = None
        for tid, tab in self.api._tabs.items():
            reg = self.api._roles_for(tab.project_path)
            # Display-first (same contract as worker detection below): a
            # coordinator tab whose cached tab.role drifted (renamed / added
            # mid-session) is still matched by its visible "🎛️ Manager" name.
            if _role_effective(tab, reg) in reg.coordinators():
                self._manager_id = tid
                manager_tab = tab
                self._manager_project = tab.project_path
                break
        if not manager_tab:
            return {"ok": False, "error": "No coordinator tab. Assign a coordinator role to a tab."}
        roles_reg = self.api._roles_for(manager_tab.project_path)
        coord_ids = set(roles_reg.coordinators())
        worker_ids = set(roles_reg.workers())
        self._dispatch_re = roles_reg.dispatch_regex()
        self._queue_section_re = roles_reg.queue_section_regex()
        self._worker_roles = list(roles_reg.workers())
        mgr_key = self.api._norm_project(manager_tab.project_path)
        for tid, tab in self.api._tabs.items():
            if tid == self._manager_id:
                continue
            # Same-project workers only.
            # 2026-04-23: append to list instead of first-wins. Multiple tabs
            # with the same role are all registered; _pick_worker_tab() picks
            # the best one (alive → quiescent preferred → round-robin).
            # 2026-04-24 routing strict: derive role from tab display (User
            # directive). _role_effective falls back to tab.role if display
            # derivation fails, preserving backward compat.
            if self.api._norm_project(tab.project_path) != mgr_key:
                continue
            eff_role = _role_effective(tab, roles_reg)
            if eff_role in worker_ids:
                self._workers.setdefault(eff_role, []).append(tid)

        if not self._workers:
            worker_list = ", ".join(roles_reg.workers()) or "worker"
            return {"ok": False, "error": f"No worker tabs in manager's project. Assign one of: {worker_list}."}

        for role, tids in self._workers.items():
            for tid in tids:
                tab = self.api._tabs.get(tid)
                if tab and tab.project_path:
                    self._last_commits[tid] = self._git_head(tab.project_path)

        self._dispatched.clear()
        self._queue_seen.clear()
        self._report_seen.clear()
        self._skip_logged.clear()
        self._file_mtime.clear()
        self._file_hash.clear()
        self._generic_file_mtime.clear()
        self._generic_file_hash.clear()
        self._generic_first_seen.clear()
        self._generic_file_prev.clear()
        self._worker_rr.clear()
        self._loop_tick = 0
        self.running = True
        # Refresh the heartbeat NOW (before the synchronous _rebuild_coord_index
        # below, which can take seconds). Otherwise PersistentSyncDaemon reads a
        # stale pre-stop tick during startup, declares us a zombie, and runs
        # MD-watch concurrently → the same delta dispatched twice.
        self._last_tick_ts = time.time()
        workers_summary = {role: len(tids) for role, tids in self._workers.items()}
        self._evt("start", f"Manager={self._manager_id}, Workers={workers_summary}")
        # 2026-04-27 Layer 0b: rebuild coordination/INDEX.md once on startup so
        # role tabs see fresh tier/audience/mtime catalog at session-start.
        # Failure is non-fatal — INDEX is a convenience, not coord-CRITICAL.
        try:
            self._rebuild_coord_index(manager_tab.project_path)
        except Exception as e:
            self._evt("index_rebuild_fail", f"non-fatal: {e}")
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self.api._js("onCoordStateChange(true)")
        return {"ok": True, "manager": self._manager_id, "workers": dict(self._workers)}

    def _graph_periodic_ingest(self) -> None:
        """Layer 3: refresh graph DB from decisions_log + peer_*.md every ~60s.

        Only re-ingests files whose mtime changed since last sync. Failure is
        non-fatal — graph is a convenience, not coord-CRITICAL.
        """
        manager = self.api._tabs.get(self._manager_id)
        if not manager or not manager.project_path:
            return
        coord = Path(manager.project_path) / "coordination"
        if not coord.is_dir():
            return
        graph_db = coord / "graph" / "decisions.sqlite"
        # Cache mtime per file across ticks — instance attribute lazy-init.
        if not hasattr(self, "_graph_ingest_mtimes"):
            self._graph_ingest_mtimes: dict[str, float] = {}
        targets: list[tuple[Path, str]] = []  # (path, kind)
        decisions = coord / "decisions_log.md"
        if decisions.exists():
            targets.append((decisions, "decisions"))
        for pp in coord.glob("peer_*.md"):
            targets.append((pp, "peer"))
        # Skip files unchanged since last ingest
        changed = []
        for path, kind in targets:
            try:
                mt = path.stat().st_mtime
            except Exception:
                continue
            if self._graph_ingest_mtimes.get(str(path)) == mt:
                continue
            changed.append((path, kind, mt))
        if not changed:
            return
        # Lazy import to keep harness boot light
        try:
            import importlib.util
            ingest_path = Path(manager.project_path) / "tools" / "graph" / "ingest.py"
            if not ingest_path.exists():
                return
            spec = importlib.util.spec_from_file_location("_graph_ingest_periodic", ingest_path)
            if not spec or not spec.loader:
                return
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception as e:
            self._evt("graph_loader_fail", f"{e}")
            return
        total_inserted = 0
        for path, kind, mt in changed:
            try:
                stats = mod.ingest_file(str(graph_db), str(path), kind=kind)
                total_inserted += stats.get("inserted", 0)
                self._graph_ingest_mtimes[str(path)] = mt
            except Exception as e:
                self._evt("graph_ingest_fail", f"{path.name}: {e}")
        if total_inserted:
            self._evt("graph_ingested", f"+{total_inserted} nodes from {len(changed)} file(s)")

    def _rebuild_coord_index(self, project_path: str) -> None:
        """Layer 0b: regenerate coordination/INDEX.md catalog at startup.

        Pure delegation to tools/scripts/build_coord_index.py — keeps the
        builder logic out of main.py and lets roles run it manually too.
        """
        if not project_path:
            return
        coord = Path(project_path) / "coordination"
        if not coord.is_dir():
            return
        # Inline import to avoid circular concern at module load time.
        import importlib.util
        builder_path = Path(project_path) / "tools" / "scripts" / "build_coord_index.py"
        if not builder_path.exists():
            return
        spec = importlib.util.spec_from_file_location("_coord_index_builder", builder_path)
        if not spec or not spec.loader:
            return
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        content = mod.build_index(coord)
        target = coord / "INDEX.md"
        target.write_text(content, encoding="utf-8")
        self._evt("index_rebuilt", f"{target.name} ({len(content)} bytes, {sum(1 for _ in target.open(encoding='utf-8'))} lines)")

    def stop(self) -> dict:
        self.running = False
        # Join the loop thread so a subsequent start() (e.g. role-edit flow)
        # can't run concurrently with the old loop (double dispatch + races on
        # the unlocked dedup sets). The loop exits within one sleep cycle (~2s).
        th = getattr(self, "_thread", None)
        if th and th.is_alive():
            try:
                th.join(timeout=5)
            except Exception:
                pass
        self._evt("stop", "Stopped")
        self.api._js("onCoordStateChange(false)")
        return {"ok": True}

    def status(self) -> dict:
        return {
            "running": self.running,
            "manager": self._manager_id,
            "workers": dict(self._workers),
            "cycles": self._cycles,
            "log": self._log[-50:],
        }

    def inject(self, instruction: str) -> dict:
        manager = self.api._tabs.get(self._manager_id)
        if not manager:
            return {"ok": False, "error": "No manager tab"}
        if not manager.pty_session or not manager.pty_session.running:
            return {"ok": False, "error": "Manager session not running"}
        roles_csv = ", ".join(self._workers.keys())
        prompt = (
            f"[USER-INSTRUCTION]\n{instruction}\n\n"
            f"Break this down and dispatch to active workers using @role: format.\n"
            f"Active workers: {roles_csv}\n"
            f"Include explicit file paths and acceptance criteria.\n"
        )
        ok_w, reason = manager.pty_session.write_dispatch(prompt)
        if not ok_w:
            self._evt("dispatch_rejected", f"user→manager [USER-INSTRUCTION]: {reason}")
            return {"ok": False, "error": f"dispatch rejected: {reason}"}
        self._evt("inject", f"user → manager: {instruction[:100]}")
        self.api._js(f"onCoordEvent('inject','user',{json.dumps(instruction[:80])})")
        self.api._render_dispatched(manager, "[USER-INSTRUCTION]", prompt, "coord_inject")
        return {"ok": True}

    # -- loop -----------------------------------------------------------------

    def _loop(self):
        # Bulletproofed 2026-04-23: the `_evt()` call used to live OUTSIDE
        # a nested try, so an exception from logging (e.g. a transient I/O
        # error appending to self._log) would escape the except, skip the
        # time.sleep, and kill the daemon thread — silently stopping MD
        # coordination until the GUI restarted. Nested try ensures the
        # loop itself is immortal while self.running is True.
        #
        # 2026-04-23 reliability upgrade: loop cadence reduced from 8s to
        # 2s so that file-based relay (the primary relay channel) has
        # sub-2-second latency between manager editing queue.md and worker
        # receiving the dispatch. Expensive operations (git subprocess,
        # state dumps) are throttled to every 4th / 40th iteration so the
        # overall cost is unchanged vs the original 8s design.
        while self.running:
            try:
                tick = self._loop_tick
                #: heartbeat updated at top of every tick so zombie
                # detection (PersistentDaemon._run_md_watch_fallback) sees a
                # fresh ts even on no-work ticks.
                self._last_tick_ts = time.time()
                # Refresh the worker map so roles/tabs added mid-session become
                # reachable. Without this, self._workers is a frozen snapshot
                # from start() and new roles dispatch-loop on skip forever.
                # Throttled every 10 ticks (~20s) — cheap but not free.
                if tick % 10 == 0:
                    try:
                        self._resync_workers()
                    except Exception:
                        pass
                # State dump every 40 ticks ≈ 80s (same cadence as before 8s loop).
                if tick % 40 == 0:
                    try:
                        coord_path = self._coord_root()
                        self._evt("state_dump", json.dumps({
                            "running": self.running,
                            "manager": self._manager_id,
                            "workers": dict(self._workers),
                            "coord_root": str(coord_path) if coord_path else None,
                            "queue_seen_size": len(self._queue_seen),
                            "report_seen_size": len(self._report_seen),
                            "cycles": self._cycles,
                            "loop_tick": tick,
                        }))
                    except Exception:
                        pass
                # Git commit polling: every 4 ticks ≈ 8s (same as legacy cadence).
                # Subprocess cost is high so we don't run it at 2s.
                if tick % 4 == 0:
                    self._poll_commits()
                # Screen-based @role: dispatch (cheap regex on screen text, 2s OK).
                self._poll_dispatch()
                # File-based relay (the primary channel) — runs every tick (2s)
                # with mtime+hash fast-path so unchanged files cost ~2 stat() calls.
                self._poll_file_dispatch()
                self._poll_file_reports()
                # Generic coordination/*.md watcher (blockers / decisions_log /
                # peer_channel / cco_brief / portfolio / etc.) — sends delta
                # notifications to Manager with the same retry-preserved
                # semantics as the structured channels. Excludes queue +
                # *_report.md (already handled above).
                self._poll_file_generic()
                # 2026-04-27 Layer 3: periodic graph ingest of decisions_log +
                # peer_*.md so the synapse-style memory stays current without
                # a manual `python tools/graph/cli.py ingest-md` step. Runs
                # every 30 ticks ≈ 60s; mtime check inside _graph_periodic_ingest
                # makes the no-change case nearly free (single stat per file).
                if tick % 30 == 0:
                    try:
                        self._graph_periodic_ingest()
                    except Exception as ge:
                        self._evt("graph_ingest_fail", f"non-fatal: {ge}")
                self._loop_tick += 1
            except Exception as e:
                try:
                    self._evt("error", str(e))
                except Exception:
                    pass
            try:
                time.sleep(2)
            except Exception:
                pass

    def _poll_commits(self):
        # 2026-04-23: iterate all tabs per role (multi-tab aware).
        for role, tids in list(self._workers.items()):
            if not self.running:
                break
            for tid in tids:
                tab = self.api._tabs.get(tid)
                if not tab or not tab.project_path:
                    continue
                head = self._git_head(tab.project_path)
                prev = self._last_commits.get(tid, "")
                if head and head != prev and prev:
                    # Advance the cursor ONLY after the report is actually
                    # delivered. Previously it advanced first, so a report that
                    # deferred (manager busy) or was rejected was lost forever —
                    # next poll saw head==prev and never re-fired.
                    if self._report(role, tab, head):
                        self._last_commits[tid] = head

    def _pick_worker_tab(self, role: str) -> str | None:
        """Pick the best tab to dispatch to `role` from the multi-tab pool.

        Strategy (2026-04-23, Python-side enforcement, 漏れゼロ):
        1. Filter to alive PTY (session.running=True)
        2. Prefer quiescent (not busy_generating / permission_prompt) via
           is_quiescent(strict=False) — same gate the caller will apply
        3. Round-robin within the preferred pool (counter per role)
        4. If all busy, fall back to round-robin across all alive; caller
           still gates via is_quiescent and skips if needed (retry next tick)
        5. If no alive tab, return None → caller skips dispatch, no dedup
           mark, retry guaranteed on next poll

        This ensures:
        - Multi-tab same role distributes load (not first-wins)
        - Same task_id never goes to 2 tabs (caller dedup by {task_id}:{role})
        - Busy tab is avoided when a sibling is quiescent
        - No task is silently dropped (alive=None → retry, alive>0 → deliver)
        """
        tabs = self._workers.get(role, [])
        if not tabs:
            return None
        # 1. Filter alive
        alive: list[str] = []
        for tid in tabs:
            t = self.api._tabs.get(tid)
            if t and t.pty_session and t.pty_session.running:
                alive.append(tid)
        if not alive:
            return None
        # 2. Split by quiescence
        quiescent: list[str] = []
        for tid in alive:
            t = self.api._tabs.get(tid)  # .get: a tab can close between the two loops
            if not t or not t.pty_session:
                continue
            ok_q, _reason = t.pty_session.is_quiescent(strict=False)
            if ok_q:
                quiescent.append(tid)
        # 3. Pool preference: quiescent first, fall back to any alive
        pool = quiescent if quiescent else alive
        # 4. Round-robin
        counter = self._worker_rr.get(role, 0)
        chosen = pool[counter % len(pool)]
        self._worker_rr[role] = counter + 1
        return chosen

    def _resync_workers(self) -> None:
        """Rebuild self._workers from current tabs state.

        2026-04-24 routing strict: self._workers was a frozen snapshot from
        start(), so any role added mid-session (e.g., `.claude/roles/reviewer.md`
        created after AutoCoord started) was unreachable — dispatches to that
        role silently looped on skip_file forever. This method re-derives the
        worker tab map using _role_effective (display-first + tab.role fallback)
        so new roles, renamed tabs, and role reassignments propagate to dispatch.

        Idempotent; logs workers_resync event only when the map changed.
        """
        mgr = self.api._tabs.get(self._manager_id)
        if not mgr:
            return
        try:
            reg = self.api._roles_for(mgr.project_path)
        except Exception:
            return
        worker_ids = set(reg.workers())
        mgr_key = self.api._norm_project(mgr.project_path)
        fresh: dict[str, list[str]] = {}
        for tid, tab in self.api._tabs.items():
            if tid == self._manager_id:
                continue
            try:
                if self.api._norm_project(tab.project_path) != mgr_key:
                    continue
            except Exception:
                continue
            eff = _role_effective(tab, reg)
            if eff and eff in worker_ids:
                fresh.setdefault(eff, []).append(tid)
        # Diff telemetry only on change
        prev_keys = set(self._workers)
        new_keys = set(fresh)
        added = new_keys - prev_keys
        removed = prev_keys - new_keys
        if added or removed or any(set(fresh.get(r, [])) != set(self._workers.get(r, [])) for r in prev_keys | new_keys):
            summary = {r: len(t) for r, t in fresh.items()}
            self._evt(
                "workers_resync",
                f"added={sorted(added) or []} removed={sorted(removed) or []} workers={summary}",
            )
        self._workers = fresh
        # Also refresh queue_section regex + _worker_roles snapshot so newly
        # added roles in RoleRegistry show up in parse + report channels.
        try:
            self._queue_section_re = reg.queue_section_regex()
            self._worker_roles = list(reg.workers())
        except Exception:
            pass

    def _report(self, role: str, tab, commit: str) -> bool:
        """Relay a worker commit to the manager. Returns True only when the
        report was actually written (caller advances its commit cursor on True;
        False = deferred/undeliverable → retry next poll)."""
        try:
            msg_r = subprocess.run(
                ["git", "log", "-1", "--format=%s", commit],
                cwd=tab.project_path, capture_output=True, text=True, timeout=5,
            )
            stat_r = subprocess.run(
                ["git", "diff", "--stat", "--stat-width=60", f"{commit}~1..{commit}"],
                cwd=tab.project_path, capture_output=True, text=True, timeout=5,
            )
            msg = msg_r.stdout.strip()
            stat = stat_r.stdout.strip()[:500]
        except Exception:
            msg, stat = commit[:8], ""

        worker_tags = " ".join(f"@{w}:" for w in self._worker_roles) or "@<worker>:"
        report = (
            f"[WORKER-REPORT from {role.upper()}]\n"
            f"Commit: {commit[:8]} — {msg}\n"
            f"{stat}\n---\n"
            f"Review and dispatch next instructions via {worker_tags} format.\n"
        )
        manager = self.api._tabs.get(self._manager_id)
        if manager and manager.pty_session and manager.pty_session.running:
            # Quiescence gate: retry next poll if Claude is still generating
            ok_q, reason = manager.pty_session.is_quiescent()
            if not ok_q:
                self._evt("skip_busy", f"report deferred ({role}→manager {commit[:8]}): {reason}")
                return False  # caller keeps the old cursor → retries next poll
            ok_w, wreason = manager.pty_session.write_dispatch(report)
            if not ok_w:
                self._evt("dispatch_rejected", f"{role}→manager [WORKER-REPORT]: {wreason}")
                return False
            self._evt("report", f"{role} → manager: {commit[:8]} {msg[:60]}")
            self.api._js(f"onCoordEvent('report','{role}',{json.dumps(msg[:60])})")
            self.api._render_dispatched(
                manager, f"[WORKER-REPORT from {role.upper()}]", report, "coord_report"
            )
            self._cycles += 1
            return True
        # No live manager tab to receive the report — retry when one appears.
        return False

    def _poll_dispatch(self):
        manager = self.api._tabs.get(self._manager_id)
        if not manager or not manager.screen_content:
            return
        # Only parse a SETTLED manager screen. Mid-stream, the regex can match a
        # half-written "@role: ..." line and dispatch a truncated instruction,
        # then dispatch the completed line on a later tick (duplicate + corrupt
        # first delivery). Quiescence also means no open prompt to write past.
        if manager.pty_session:
            ok_q, _qr = manager.pty_session.is_quiescent(strict=False)
            if not ok_q:
                return
        for role_m, instruction in self._dispatch_re.findall(manager.screen_content):
            role = role_m.lower().strip()
            inst = re.sub(r'\s+', ' ', instruction).strip()
            if len(inst) < 5:
                continue
            # Trust boundary (CLAUDE.md): the manager screen is L5 external.
            # Defang any harness prefix the manager output embedded in the body
            # before wrapping it in an authenticated [MANAGER-DISPATCH] header —
            # parity with the MD-WATCH delta path, which already sanitizes.
            # Stops prefix-spoofing from riding an upstream injection into the
            # worker's exec chain ( class).
            inst = AutoCoordinator._sanitize_harness_prefixes(inst)
            key = f"{role}:{inst[:300]}"
            if key in self._dispatched:
                continue
            self._dispatched.add(key)
            # 2026-04-23: multi-tab aware pick (quiescent preferred, round-robin).
            tid = self._pick_worker_tab(role)
            if not tid:
                self._evt("skip", f"No alive tab for '{role}'")
                self._dispatched.discard(key)  # retry next poll when tab comes up
                continue
            target = self.api._tabs.get(tid)
            if not target or not target.pty_session or not target.pty_session.running:
                # Defense-in-depth: _pick filtered alive but tab died between
                # pick and here (race). Skip and retry.
                self._evt("skip", f"{role} tab became inactive after pick")
                self._dispatched.discard(key)
                continue
            # Quiescence gate: retry next _loop iteration if worker is busy.
            # Do NOT add to _dispatched yet — it's already marked above, so
            # remove it so the next pass will re-attempt delivery.
            ok_q, reason = target.pty_session.is_quiescent()
            if not ok_q:
                self._evt("skip_busy", f"dispatch deferred (manager→{role}): {reason}")
                self._dispatched.discard(key)
                continue
            # Prefix with [MANAGER-DISPATCH ...] so the receiving Claude can
            # recognise this as an authenticated harness message (see
            # CLAUDE.md Authentication 例外2). Without a prefix, the receiver
            # would ask "by suz?" and the coordination loop would stall.
            prefix_hdr = f"[MANAGER-DISPATCH to {role}]"
            prefixed = f"{prefix_hdr}\n{inst}"
            # 2026-04-27 GRAPH ENFORCEMENT: enrich with [GRAPH-CTX] so the
            # worker sees relevant past decisions for the dispatched task.
            try:
                proj = getattr(target, "project_path", "") or ""
                # use peer mailbox key for graph enrich
                prefixed = _graph_enrich_msg(prefixed, f"peer_{role}.md", inst, proj)
            except Exception:
                pass
            ok_w, wreason = target.pty_session.write_dispatch(prefixed)
            if not ok_w:
                self._evt("dispatch_rejected", f"manager→{role} [MANAGER-DISPATCH]: {wreason}")
                self._dispatched.discard(key)  # Preserve retry on next poll.
                continue
            self._evt("dispatch", f"manager → {role}: {inst[:80]}")
            self.api._js(f"onCoordEvent('dispatch','{role}',{json.dumps(inst[:80])})")
            self.api._render_dispatched(target, prefix_hdr, inst, "coord_dispatch")

    def _poll_file_dispatch(self):
        """Legacy single-file queue dispatch — retired (no-op stub).

        All dispatch flows through peer_<role>.md mailboxes (MD-WATCH
        fanout). Retained as a no-op stub for caller compatibility.
        """
        if not LEGACY_QUEUE_DISPATCH_ENABLED:
            if "legacy_queue_disabled" not in self._skip_logged:
                self._skip_logged.add("legacy_queue_disabled")
                self._evt(
                    "legacy_queue_dispatch_disabled",
                    "legacy queue dispatch retired — delivery via peer_<role>.md mailboxes",
                )
            return
        # Legacy body removed. See git history.
        return

    def _read_queue_closed_tasks(self, coord) -> "set[str]":
        """Legacy queue closed-task suppression — retired.

        Returns empty set (no closed-task suppression). Worker-report
        dedup now relies on body-hash + role mailbox flow only.
        """
        return set()

    def _poll_file_reports(self):
        """Relay new entries from coordination/{role}_report.md to Manager PTY.

        For each of developer/marketing/security, scans the report file and
        writes any newly-seen `## [timestamp] task_id` entry into Manager's
        PTY. Entries are keyed by (role, task_id, content-hash) so edits to an
        existing entry also trigger re-notification.

        2026-04-23 reliability upgrade: mtime+hash fast-path (same as
        _poll_file_dispatch). Per-role file cache so one changed report
        doesn't force re-parse of all roles' reports.

        Token-saving: WORKER-REPORTs for tasks already marked closed in the
        legacy queue file are suppressed (logged as `worker_report_dedup`).
        Stops redundant fanout when a stale report mtime-bumps but the task
        is already closed.
        """
        coord = self._coord_root()
        if not coord:
            return
        manager = self.api._tabs.get(self._manager_id)
        if not manager or not manager.pty_session or not manager.pty_session.running:
            return
        # Build closed-T-id set once per poll (cheap re-read).
        closed_task_ids = self._read_queue_closed_tasks(coord)
        import hashlib
        for role in self._worker_roles:
            report_file = coord / f"{role}_report.md"
            if not report_file.exists():
                continue
            # Fast-path: skip unchanged report files (per-role cache key).
            key = str(report_file)
            try:
                mtime = report_file.stat().st_mtime
            except Exception:
                continue
            if self._file_mtime.get(key) == mtime:
                continue
            try:
                content = report_file.read_text("utf-8", errors="replace")
            except Exception:
                continue
            chash = hashlib.md5(content.encode("utf-8", errors="replace")).hexdigest()[:12]
            if self._file_hash.get(key) == chash:
                self._file_mtime[key] = mtime
                continue
            self._file_mtime[key] = mtime
            self._file_hash[key] = chash
            self._evt("report_change", f"{role}_report.md changed (mtime={mtime:.1f}, hash={chash})")
            entries = self._parse_report_entries(content)
            for e in entries:
                body_hash = hashlib.md5(e["body"].encode("utf-8", errors="replace")).hexdigest()[:12]
                key = f"{role}|{e['task_id']}|{body_hash}"
                if key in self._report_seen:
                    continue
                # 2026-04-27 token-saving Tier 1 #1: skip if T-id already
                # closed in queue. Mark `_report_seen` so future identical
                # bodies short-circuit on the cache check above.
                if e['task_id'] in closed_task_ids:
                    self._report_seen.add(key)
                    self._evt(
                        "worker_report_dedup",
                        f"{role} {e['task_id']} suppressed (already [x] in queue)",
                    )
                    continue
                summary = e["body"][:1200]
                prefix_hdr = f"[WORKER-REPORT from {role.upper()}] {e['task_id']}"
                msg = (
                    f"[WORKER-REPORT from {role.upper()}] {e['task_id']}\n"
                    f"Timestamp: {e['ts']}\n"
                    f"---\n"
                    f"{summary}\n"
                    f"---\n"
                    f"Source: coordination/{role}_report.md (full detail). "
                    f"Coordinate via coordination/peer_<role>.md (per-role mailbox) + decisions_log.md; escalate via blockers.md if needed.\n"
                )
                # Quiescence gate: retry next _loop iteration if manager is busy.
                # Don't add to _report_seen — retry guaranteed.
                # strict=False: file-relay 用途、bubble render が screen "recently changed" 永続化対策 (is_quiescent docstring L1811-1813 参照)
                ok_q, reason = manager.pty_session.is_quiescent(strict=False)
                if not ok_q:
                    scr_tail = ((manager.screen_content or "")[-200:]).replace("\n", "↵").replace("\r", "")
                    self._evt("skip_busy", f"file_report deferred ({role} → manager {e['task_id']}): {reason} | tail: {scr_tail}")
                    continue
                # Mark-after-send: file_report 配信完了後 add_to_seen — manager
                # PTY mid-write 死亡時 next poll で retry 復旧、worker→manager
                # 配信失敗 silent drop 防止 (2026-04-24 strict write_dispatch 補完)
                ok_w, wreason = manager.pty_session.write_dispatch(msg)
                if not ok_w:
                    if wreason in ("pty_dead", "write_failed"):
                        self._evt("file_report_fail", f"PTY error: {role} → manager {e['task_id']} ({wreason})")
                    else:
                        self._evt("dispatch_rejected", f"{role}→manager [WORKER-REPORT] {e['task_id']}: {wreason}")
                    continue
                self._report_seen.add(key)
                self._evt("file_report", f"{role} → manager: {e['task_id']}")
                self.api._js(f"onCoordEvent('report','{role}',{json.dumps(e['task_id'][:80])})")
                self.api._render_dispatched(manager, prefix_hdr, msg, "file_report")
                self._cycles += 1
                # Peer-first hint: scan the report body for cross-role triggers
                # and suggest a peer review in coordination/peer_channel.md.
                # Non-intrusive: writes a suggestion entry, peers read it when
                # they look. Doesn't auto-dispatch.
                self._peer_hint_scan(role, e['task_id'], e['body'], coord)

    # Files ALWAYS excluded from generic MD watch (handled by dedicated channels
    # or purely UI-side, or would cause feedback loops if echoed back).
    # peer_channel.md is the @all broadcast file. Real-time MD-WATCH push for
    # every entry is context noise (every role's tab gets every @all post
    # mid-task). Writes remain free; roles read it tail-20 at task close.
    # Per-role peer_<role>.md mailboxes handle directed messages with
    # real-time push. Audit-trail / static-reference docs are likewise
    # no-broadcast: their read pattern is session-start tail or on-demand
    # lookup — real-time push is context pollution + token waste.
    _GENERIC_WATCH_EXCLUDE = {
        "manager_queue.md",          # legacy queue file (archived — never broadcast)
        "README.md",                 # not coord
        "peer_channel.md",           # no-broadcast (task-end tail read, 26-04-26)
        "boundary_rulings.md",       # no-broadcast (判例集 lookup, 26-04-26)
        "peer_consultation_checklist.md",  # no-broadcast (static peer-trigger map, 26-04-26)
        "INDEX.md",                  # no-broadcast (auto-gen catalog, push value 0; 27-04)
        # decisions_log/tier_list/project_portfolio are NOT excluded — they
        # route through the pointer-mode path (MDWATCH_POINTER_MODE_FILES).
    }

    def _poll_file_generic(self) -> None:
        """Relay changes in miscellaneous coordination/*.md files to Manager.

        Covers files that aren't the structured queue/report channels but
        still carry coordination-relevant context (blockers.md,
        decisions_log.md, peer_channel.md, cco_brief.md,
        project_portfolio.md, tier_list.md, and any future coord *.md).

        Design:
        - Discovers files via glob each tick so newly-created coord md files
          are picked up without restart.
        - First-seen content is captured silently (prevents flood on startup).
        - Subsequent changes trigger a [MD-WATCH] notification to Manager
          PTY with a tail preview of the current file.
        - mtime+hash fast-path skips unchanged files cheaply.
        - Retry-preserved: caches are NOT updated until write_submit succeeds
          so a busy/dead Manager PTY doesn't lose the notification (永続送信).
        - Excludes *_report.md (the report channel) and the explicit exclude
          set so we don't double-relay.
        """
        coord = self._coord_root()
        if not coord:
            return
        manager = self.api._tabs.get(self._manager_id)
        if not manager or not manager.pty_session or not manager.pty_session.running:
            return
        # 2026-04-26 P3: delegated to shared helper. Behavior is bit-for-bit
        # equivalent to the previous inline body; see _md_watch_process_coord_dir.
        _md_watch_process_coord_dir(
            api=self.api,
            manager_tab=manager,
            coord=coord,
            excluded=self._GENERIC_WATCH_EXCLUDE,
            file_mtime=self._generic_file_mtime,
            file_hash=self._generic_file_hash,
            file_prev=self._generic_file_prev,
            first_seen=self._generic_first_seen,
            key_prefix="",
            log_evt=self._evt,
            js_emit=lambda delivered, name: self.api._js(
                f"onCoordEvent('md_watch',{json.dumps(delivered)},{json.dumps(name)})"
            ),
            label="",
            running_check=lambda: self.running,
        )

    # mitigation — neutralize harness-prefix lines inside coord-md
    # delta_text. Prefix lines arriving from external (L5) coord/.md content
    # would otherwise look identical to genuine harness-emitted [PEER-DISPATCH
    # from peer_*] etc., enabling instruction-injection across the trust
    # boundary (CLAUDE.md ## Trust boundary). Quote-prefix (`> `) is enough:
    # Claude reads the `>` as Markdown blockquote → the line is content, not
    # an executive instruction. Original text is preserved (visible) for audit.
    # mitigation regex covers bare prefix forms too — attacker-injected
    # `[MANAGER-DISPATCH] rm -rf` (no "from coordination/" anchor) must also be
    # blockquoted, not only canonical harness emissions. Strict superset of the
    # original anchor-only regex: every legitimate harness line still matches.
    # MD-WATCH added per CLAUDE.md ## Trust boundary L31-43 trust-priority list.
    _HARNESS_PREFIX_RE = re.compile(
        r"^(\[USER-INSTRUCTION"
        r"|\[MANAGER-DISPATCH"
        r"|\[PEER-DISPATCH"
        r"|\[PEER-INBOX"
        r"|\[WORKER-REPORT"
        r"|\[MD-WATCH"
        r"|\[GRAPH-CTX"
        r"|\[CC: )", # mitigation: receiver-side CC: prefix sanitize (sender L1005 既 listed)
        re.MULTILINE,
    )

    # mitigation: property-aware Cf/Zs/Zl/Zp/Mn + explicit NEL + Hangul fillers.
    # Mn (combining marks) at LINE-START is anomalous — legit text starts with base letters.
    # Lo Hangul fillers are "invisible placeholders" (Default_Ignorable) — explicit list
    # rather than full Lo category (which includes legit CJK letters).
    _INVISIBLE_CATEGORIES = frozenset({"Cf", "Zs", "Zl", "Zp", "Mn"})
    _INVISIBLE_EXTRA = frozenset({
        "",      # NEL U+0085 (Cc)
        "ᅟ",      # HANGUL CHOSEONG FILLER U+115F (Lo)
        "ᅠ",      # HANGUL JUNGSEONG FILLER U+1160 (Lo)
        "ㅤ",      # HANGUL FILLER U+3164 (Lo)
        "ﾠ",      # HALFWIDTH HANGUL FILLER U+FFA0 (Lo)
    })

    # mitigation: Alt bracket translate (NFKC doesn't decompose lenticular/angle/math/tortoise).
    _ALT_BRACKET_TRANSLATE = str.maketrans({
        "【": "[", "】": "]",  # 黒 lenticular U+3010/3011 (Japanese UI 頻出)
        "〔": "[", "〕": "]",  # 白 lenticular U+3014/3015
        "〚": "[", "〛": "]",  # 白 square U+301A/301B
        "〈": "[", "〉": "]",  # angle U+3008/3009
        "⟦": "[", "⟧": "]",   # math white square U+27E6/27E7
        "❲": "[", "❳": "]",   # light tortoise U+2772/2773
    })

    @staticmethod
    def _strip_invisible_leading(text: str) -> str:
        """Per-line strip leading invisible Unicode chars ( mitigation)."""
        if not text:
            return text
        cats = AutoCoordinator._INVISIBLE_CATEGORIES
        extra = AutoCoordinator._INVISIBLE_EXTRA
        out_lines = []
        for line in text.split("\n"):
            i = 0
            while i < len(line) and (line[i] in extra or unicodedata.category(line[i]) in cats):
                i += 1
            out_lines.append(line[i:])
        return "\n".join(out_lines)

    # Legacy regex retained for backwards-compat reference (unused, dead code).
    _INVISIBLE_LEADING_RE = re.compile(
        r"^[ ​‌‍‪‫‬‭‮⁦⁧⁨⁩﻿]+",
        re.MULTILINE,
    )

    @staticmethod
    def _sanitize_harness_prefixes(text: str) -> str:
        """Quote-prefix any harness-prefix line so injected prefixes inside
        coord-md content cannot impersonate harness dispatch headers.

        Applied to delta_text *before* truncation so the byte budget is
        spent on content, not on attacker padding.
        """
        if not text:
            return text
        # mitigation: NFKC FIRST so U+2329/232A math angle decompose to U+3008/3009 first.
        text = unicodedata.normalize("NFKC", text)
        # mitigation: alt bracket family translate AFTER NFKC (catches NFKC-decomposed variants too).
        text = text.translate(AutoCoordinator._ALT_BRACKET_TRANSLATE)
        # mitigation: strip invisible Unicode chars (property-aware Mn added).
        text = AutoCoordinator._strip_invisible_leading(text)
        return AutoCoordinator._HARNESS_PREFIX_RE.sub(r"> \1", text)

    @staticmethod
    def _compute_md_delta(prev: str, curr: str, name: str) -> tuple[str, str, int, int]:
        """Compute a precise, Manager-readable diff summary.

        Returns (delta_text, kind, lines_added, lines_removed).

        kind ∈ {"append", "edit", "shrink", "first"}:
          - "first"  : no prev snapshot (shouldn't happen; baseline guard
                       runs first, but defensive fallback)
          - "append" : curr starts with prev → show only the appended suffix
                       (most common case for coord logs like decisions_log)
          - "edit"   : neither append nor shrink — emit a unified-diff-style
                       summary of changes (bounded to ~80 context lines)
                       since mid-file edits happen when user edits blockers
                       status / queue hygiene
          - "shrink" : curr is a strict prefix of prev → file was truncated;
                       report lines removed + current tail
        """
        if not prev:
            return (
                AutoCoordinator._sanitize_harness_prefixes(curr[-1500:]),
                "first", len(curr.splitlines()), 0,
            )
        if curr == prev:
            return ("(no line-level change)", "noop", 0, 0)
        prev_lines = prev.splitlines()
        curr_lines = curr.splitlines()
        #: require prev to end with "\n" before treating curr as a pure
        # line-aligned append. Without it, a write that EXTENDS the last line
        # (prev had no trailing newline) and appends is a byte-prefix but not a
        # line-prefix — the modified last line would be silently dropped from
        # the delta. Fall through to the edit (difflib) branch in that case.
        if prev.endswith("\n") and curr.startswith(prev):
            appended = curr_lines[len(prev_lines):]
            if not appended and curr != prev:
                # Trailing-newline-only change; treat as noop for display.
                return ("(only trailing whitespace changed)", "noop", 0, 0)
            body = "\n".join(appended)
            # mitigation — sanitize harness prefixes BEFORE truncation so the
            # byte budget covers content, not attacker padding.
            body = AutoCoordinator._sanitize_harness_prefixes(body)
            # 2026-04-24 user-surfaced: PTY screen rendering + context capture
            # can drop the head of long messages (scrollback / char-limit).
            # Tighter bound (2000→1200) keeps messages more likely to fit a
            # single screen render pass and reduces head-truncation risk.
            #: bump 1200 → 3000 chars. Coalescer can pile
            # multiple appends in one delta, and 1200 was over-aggressive
            # — single P2 dispatch (~500-800 chars) + coalesced peer line
            # quickly overflowed → mid-msg truncation = "完全な形式で来ない"
            # symptom. 3000 covers ~3 P2 entries, recipient still gets full
            # context for typical use. Past that, Read tool fetches full file.
            _DELTA_TRUNC_LIMIT = 3000
            if len(body) > _DELTA_TRUNC_LIMIT:
                body = (
                    body[:_DELTA_TRUNC_LIMIT]
                    + f"\n... (appended body truncated at {_DELTA_TRUNC_LIMIT} chars; "
                    + f"Read coordination/{name} with offset/limit for full body)"
                )
            return (body, "append", len(appended), 0)
        if prev.startswith(curr):
            removed = len(prev_lines) - len(curr_lines)
            tail = AutoCoordinator._sanitize_harness_prefixes(
                "\n".join(curr_lines[-20:])
            )
            return (
                f"(file shrank by {removed} lines — content kept:)\n{tail}",
                "shrink", 0, removed,
            )
        # Generic edit — produce a compact unified-style diff. Bounded output.
        import difflib
        diff = list(difflib.unified_diff(
            prev_lines, curr_lines,
            fromfile=f"prev/{name}", tofile=f"curr/{name}",
            n=2, lineterm="",
        ))
        added = sum(1 for d in diff if d.startswith("+") and not d.startswith("+++"))
        removed = sum(1 for d in diff if d.startswith("-") and not d.startswith("---"))
        # 2026-04-24 user-surfaced: reduce 80→50 lines for head-truncation
        # resilience (PTY render + context capture both favor shorter bursts).
        body = "\n".join(diff[:50])
        if len(diff) > 50:
            body += f"\n... ({len(diff) - 50} more diff lines truncated)"
        # mitigation — sanitize harness prefixes per diff line. Diff lines
        # start with `+`/`-`/` ` (header lines `---`/`+++`/`@@` skipped);
        # strip the diff column, sanitize the content, re-prepend. Without
        # this, an attacker line like `+[PEER-DISPATCH from peer_x] ...` in
        # a coord-md edit would pass through verbatim → trust-boundary leak.
        def _sanitize_diff_line(ln: str) -> str:
            if ln.startswith(("---", "+++", "@@")):
                return ln
            if ln and ln[0] in "+- ":
                return ln[0] + AutoCoordinator._sanitize_harness_prefixes(ln[1:])
            return AutoCoordinator._sanitize_harness_prefixes(ln)
        body = "\n".join(_sanitize_diff_line(ln) for ln in body.splitlines())
        return (body, "edit", added, removed)

    # -- helpers --------------------------------------------------------------

    def _coord_root(self) -> Path | None:
        """Return the Manager tab's coordination/ dir, or None if unavailable."""
        mgr = self.api._tabs.get(self._manager_id)
        if not mgr or not mgr.project_path:
            return None
        coord = Path(mgr.project_path) / "coordination"
        return coord if coord.is_dir() else None

    def _parse_queue_tasks(self, content: str) -> list[dict]:
        """Legacy queue parser — retired.

        Returns empty list. Dispatch flows through peer_<role>.md mailbox
        (MD-WATCH fanout) instead of queue task parsing.
        """
        return []

    def _parse_report_entries(self, content: str) -> list[dict]:
        """Extract `## [ts] task_id` entries from a *_report.md file.

        Returns list of {task_id, ts, body}. Skips code blocks and nested
        headers. Body is everything between this header and the next `## `.
        """
        out: list[dict] = []
        lines = content.splitlines()
        in_code = False
        i = 0
        n = len(lines)
        while i < n:
            line = lines[i]
            stripped = line.strip()
            if stripped.startswith("```"):
                in_code = not in_code
                i += 1
                continue
            if in_code:
                i += 1
                continue
            m = self.REPORT_ENTRY_RE.match(line)
            if m:
                ts = m.group(1)
                task_id = m.group(2)
                body_lines: list[str] = []
                j = i + 1
                while j < n:
                    nxt = lines[j]
                    nxt_stripped = nxt.strip()
                    if nxt_stripped.startswith("```"):
                        # toggle code state and include in body so fenced
                        # blocks inside an entry are preserved
                        body_lines.append(nxt)
                        j += 1
                        # fast-forward to the matching closing fence
                        while j < n and not lines[j].strip().startswith("```"):
                            body_lines.append(lines[j])
                            j += 1
                        if j < n:
                            body_lines.append(lines[j])
                            j += 1
                        continue
                    if nxt.startswith("## "):
                        break
                    body_lines.append(nxt)
                    j += 1
                out.append({
                    "task_id": task_id,
                    "ts": ts,
                    "body": "\n".join(body_lines).strip(),
                })
                i = j
                continue
            i += 1
        return out

    def _git_head(self, path: str) -> str:
        try:
            r = subprocess.run(
                ["git", "log", "-1", "--format=%H"],
                cwd=path, capture_output=True, text=True, timeout=5,
            )
            return r.stdout.strip()
        except Exception:
            return ""

    def _evt(self, kind: str, msg: str):
        self._log.append({"ts": time.time(), "type": kind, "msg": msg})
        if len(self._log) > 500:
            self._log = self._log[-250:]

    # Keywords (lowercase) that suggest a peer review by a specific role.
    # Conservative set — too broad would spam peer_channel, too narrow misses
    # cross-role signal. Tweak as the team develops shared vocabulary.
    #
    # Legal-scope keywords (景表/薬機/金商/弁護士/特商/税理士/著作権/宅建/個情)
    # flow through SCOPE_KEYWORDS (source-agnostic) → role:legal so
    # marketing-sourced legal wording no longer misroutes to security.
    # See _scope_match_targets below.
    _PEER_HINT_RULES = (
        # (source_role, target_role, keywords, reason_key)
        ("developer", "security", (
            "xss", "sanitize", "escapehtml", "escape_html", "injection",
            "csrf", "csp", "innerhtml", "eval(", "new function",
            "auth", "permission", "credential", "api key", "token",
            "localstorage save", "localstorage 保存", "password",
        ), "security-sensitive change — consider peer review"),
        ("developer", "marketing", (
            "ui 刷新", "ui改善", "コピー書換", "copy rewrite", "ux改善",
            "スクショ", "screenshot", "スクリーンショット", "マイクロインタラクション",
            "ランディング", "landing", "cta ",
        ), "user-facing change — Marketing may want to adjust copy/visuals"),
        ("marketing", "security", (
            "ephemeral", "消えるデータ", "機密",
        ), "data-handling concern — Security may want a privacy review"),
        ("marketing", "developer", (
            "撮影中に発見", "スクショ撮れない", "表示崩れ", "uiが壊",
            "使い勝手が悪", "説明できない機能",
        ), "UX blocker found in marketing work — Dev may want to fix"),
        ("security", "developer", (
            "resolved", "regression", "回帰", "修正確認", "残存", "remaining",
        ), "security regression check — Developer may want to act"),
        ("security", "marketing", (
            "note 記事", "出品文", "公開前", "x スレッド", "irresponsible",
            "disclosure",
        ), "security-adjacent public content — Marketing may want to adjust"),
    )

    # Scope-based keyword → role taxonomy. Source-agnostic. Any role's
    # report containing these keywords routes a peer-review hint to the
    # scope-owning role. Multiple scopes can match in one body → all
    # matching peers get cc'd. Self-CC is suppressed by _scope_match_targets.
    # Lowercased for case-insensitive `in` test on body.lower().
    SCOPE_KEYWORDS: "dict[str, tuple[str, ...]]" = {
        "legal": (
            "景表", "薬機", "金商", "弁護士", "特商", "税理士",
            "著作権", "宅建", "個情",
            "個人情報", "プライバシー", "privacy policy",
            "要配慮", "医療情報", "法律", "法的",
        ),
        "security": (
            "cwe-79", "cwe-94", "cwe-918", "ssrf",
            "secret-leak", "credential leak",
        ),
        "designer": (
            "a11y", "accessibility", "aria", "wcag",
        ),
        "qa": (
            "regression", "playwright", "browser-behavior",
        ),
        "sre": (
            "runbook", "slo", "sli", "incident", "postmortem",
        ),
        "revops": (
            "monetization", "pricing", "cohort", "elasticity", "mrr",
        ),
    }

    @staticmethod
    def _scope_match_targets(low_body: str, src_role: str) -> "list[tuple[str, str]]":
        """Return (target_role, matched_keyword) per scope match in low_body.

        Source-agnostic: any role's report can trigger any scope. Self-CC is
        suppressed (target_role != src_role). At most one match per scope
        (first keyword wins) to avoid duplicate hints. Order is dict-iteration
        order which is insertion-stable (legal > security > designer > qa >
        sre > revops). acceptance §3.3 fallthrough: multiple scopes
        match → all cc'd; single scope match → single cc; no match → no hint.
        """
        out: "list[tuple[str, str]]" = []
        for scope, kws in AutoCoordinator.SCOPE_KEYWORDS.items():
            if scope == src_role:
                continue  # don't self-CC
            for kw in kws:
                if kw in low_body:
                    out.append((scope, kw))
                    break  # first hit per scope
        return out

    def _peer_hint_scan(self, role: str, task_id: str, body: str, coord: Path) -> None:
        """Scan a Worker report body and append peer-review suggestions to
        `coordination/peer_channel.md` when cross-role keywords match. This is
        the 'peer-first routing' nudge — Manager still gets the CC through
        the normal report relay, but peers get a proactive heads-up."""
        if not body or not coord:
            return
        low = body.lower()
        hints: list[tuple[str, str]] = []
        for src, target, kws, reason in self._PEER_HINT_RULES:
            if src != role:
                continue
            matched = [k for k in kws if k in low]
            if matched:
                hints.append((target, f"{reason} (matched: {matched[0]})"))
        # 2026-04-26: source-agnostic scope keyword → role routing.
        # Fires regardless of source role (legal/sec/dsn/qa/sre/rvo scope
        # detection across all reports). De-dup against (target, kw) already
        # surfaced via _PEER_HINT_RULES so we don't spam the same target with
        # the same matched keyword twice.
        already = {(t, m) for t, reason in hints for m in [reason.split("matched: ", 1)[-1].rstrip(")")]}
        for target, kw in self._scope_match_targets(low, role):
            if (target, kw) in already:
                continue
            hints.append((target, f"scope-keyword detected (matched: {kw})"))
        if not hints:
            return
        try:
            ts = time.strftime("%Y-%m-%d %H:%M")
            # 2026-04-26 mailbox routing: group hints by target so each
            # peer_<role>.md gets only its own hints (vs legacy single
            # peer_channel.md broadcast). MD-WATCH per-role mailbox routing on
            # the read side delivers to the named role's tab only.
            by_target: dict[str, list[str]] = {}
            for target, reason in hints:
                line = (
                    f"\n## [{ts}] autocoord @{target}\n"
                    f"Suggested peer review from {role} on `{task_id}`: {reason}.\n"
                    f"Source: `coordination/{role}_report.md` → search `{task_id}`.\n"
                )
                by_target.setdefault(target, []).append(line)
            for target, t_lines in by_target.items():
                target_file = (
                    (coord / f"peer_{target}.md") if MAILBOX_ENABLED
                    else (coord / "peer_channel.md")
                )
                # target-side dedup: skip lines whose (task_id, matched: kw)
                # already appears in target file. Defends against session-restart /
                # body-hash drift / _BoundedSeenSet trim re-emits.
                try:
                    existing = target_file.read_text("utf-8") if target_file.exists() else ""
                except Exception:
                    existing = ""
                filtered = []
                for line in t_lines:
                    m_task = re.search(r"on `([^`]+)`", line)
                    m_kw = re.search(r"\(matched: ([^)]+)\)", line)
                    if m_task and m_kw:
                        needle = f"on `{m_task.group(1)}`"
                        kw_needle = f"(matched: {m_kw.group(1)})"
                        if needle in existing and kw_needle in existing:
                            self._evt(
                                "peer_hint_dedup",
                                f"{role} → {target}: {m_task.group(1)} (kw={m_kw.group(1)})",
                            )
                            continue
                    filtered.append(line)
                if not filtered:
                    continue
                # vector B + cycle 14 (26-05-05) unification:
                # cross-actor race protection. Worker subprocess (Claude PTY
                # Edit/Write tool) can write the same target_file concurrently
                # with this autocoord append → silent overwrite loss without
                # OS-level advisory lock. Empirical dogfood evidence
                # (2026-05-04): a role push to peer_manager.md observed "File
                # has been modified since read" retried 4 times. Migrated to
                # `_locked_coord_write`
                # ctx mgr (cycle 14 rec:C) — single shared abstraction across
                # sites. No threading_lock needed (harness
                # single-thread serialize for _peer_hint_scan callers; flock
                # alone covers the cross-process race).
                try:
                    with _locked_coord_write(target_file):
                        with open(target_file, "a", encoding="utf-8") as f:
                            f.write("".join(filtered))
                except Exception:
                    continue
            for target, _ in hints:
                self._evt("peer_hint", f"{role} → {target}: {task_id}")
                try:
                    self.api._js(
                        f"onCoordEvent('peer_hint',{json.dumps(role)},{json.dumps(target + ':' + task_id[:60])})"
                    )
                except Exception:
                    pass
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Peer Bridge — tab-to-tab (A) + cross-project file inbox (B)
# ---------------------------------------------------------------------------
class PeerBridge:
    """Tab-to-tab (in-GUI) + cross-project (file-based) peer message routing.

    Two dispatch syntaxes, scanned from each tab's visible screen on every
    loop iteration:

    * `@@<peer_tab_name>:<body>` — (A) tab-to-tab. The sender tab must list
      the target tab's id in its own `peer_tabs`. Case-insensitive match on
      the target tab's display `name`. Body is injected into the target's
      PTY as `[PEER-DISPATCH from @@<sender_name>]`.

    * `@<peer_alias>.<role>:<body>` — (B) cross-project. The sender's project
      must have `.claude-harness/peers.json` mapping the alias to a target
      project path. The body is appended to
      `<target>/.claude-harness/inbox/from_<self_alias>.md` as a
      `## [ts] to:<role>` entry. The target project's inbox watcher picks it
      up and forwards it to the addressed role tab's PTY (or the manager
      tab, if the role tab is not running).

    Both channels are dedup'd by (source, target, body-prefix) so re-scans of
    the same screen do not re-dispatch.
    """
    # 26-05-05 user-reported bug fix: greedy multi-line `(.+?)` body capture
    # vacuumed up later input on the same screen — typing `/role manager` (or
    # similar) in tab A while an older `@@<name>:` line was still visible in
    # the scrollback caused the regex to extend the dispatch body up to the
    # new content, dedup-key changes (body differs), and the PEER-DISPATCH
    # re-fires to tab B with the slash command embedded in the body. Receiver
    # Claude then runs `/role manager` as if the operator had typed it there.
    # Mitigation: bound the body to a single line so an `@@<name>:` directive
    # stops at the first newline. Multi-line dispatch was never a documented
    # contract and the dedup machinery already keys on body[:300], so a
    # single-line cap is consistent with how the channel is actually used.
    TAB_DISPATCH_RE = re.compile(
        r"@@([A-Za-z0-9_\-]+)\s*:\s*([^\n]+)",
    )
    PROJ_DISPATCH_RE = re.compile(
        r"@([A-Za-z0-9_\-]+)\.([A-Za-z0-9_]+)\s*:\s*([^\n]+)",
    )
    # 2026-04-23 multi-role: allow comma-separated role list in cross-project inbox.
    INBOX_ENTRY_RE = re.compile(r"^##\s+\[([^\]]+)\]\s+to:([A-Za-z0-9_,\s]+?)\s*$")

    def __init__(self, api_ref):
        self.api = api_ref
        self.running = False
        self._thread: threading.Thread | None = None
        # Bounded LRU dedup (cap=2000, trim to 1000 when over). See
        # _BoundedSeenSet — fixes broken set-slice trim that randomly evicted
        # ~50% of recent dedup marks.
        self._dispatched_tab = _BoundedSeenSet(cap=2000)
        self._dispatched_proj = _BoundedSeenSet(cap=2000)
        #: serialize cross-project inbox append (read+concat+write
        # pattern wiped concurrent writes; append-mode + lock fixes wipe +
        # race + transient read failure simultaneously).
        self._inbox_write_lock = threading.Lock()
        # Inbox dedup: (inbox_file, entry-hash) → True
        # LRU-bounded (insertion-ordered eviction). A plain set + slice-trim
        # would evict a hash-random ~half on overflow and replay stale
        # [PEER-INBOX] messages (the antipattern _BoundedSeenSet was built for).
        self._inbox_seen = _BoundedSeenSet(cap=4000)
        self._inbox_file_known: set[str] = set()
        self._log: list[dict] = []

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.running = False

    def _loop(self) -> None:
        # Bulletproofed 2026-04-23: see AutoCoordinator._loop for rationale.
        # PeerBridge handles @@ tab dispatch, @alias.role: cross-project, and
        # inbox forwarding — if this loop dies, inter-session coordination
        # silently stops even though the coordinator loop keeps running.
        while self.running:
            try:
                self._poll_tab_peers()
                self._poll_cross_project()
                self._poll_inboxes()
            except Exception as e:
                try:
                    self._evt("error", str(e))
                except Exception:
                    pass
            try:
                time.sleep(4)
            except Exception:
                pass

    # -- A: tab-to-tab --------------------------------------------------------

    def _poll_tab_peers(self) -> None:
        for tid, tab in list(self.api._tabs.items()):
            if not tab.peer_tabs or not tab.screen_content:
                continue
            allowed_ids = set(tab.peer_tabs)
            for m in self.TAB_DISPATCH_RE.finditer(tab.screen_content):
                target_name = m.group(1).strip()
                body = re.sub(r"\s+", " ", m.group(2)).strip()
                if len(body) < 5:
                    continue
                key = f"{tid}:{target_name.lower()}:{body[:300]}"
                if key in self._dispatched_tab:
                    continue
                # fix (26-04-28): defer .add until after quiescence
                # gate + write_dispatch succeed. Marking before the gate poisons
                # the dedup set on every defer → permanent silent drop on
                # subsequent polls even after the target tab quiesces.
                target_tab = None
                tn_lower = target_name.lower()
                for ptid in allowed_ids:
                    pt = self.api._tabs.get(ptid)
                    if not pt:
                        continue
                    # Allow match against tab name (whitespace-collapsed, lower)
                    ptn = re.sub(r"\s+", "", pt.name or "").lower()
                    if ptn == re.sub(r"\s+", "", tn_lower):
                        target_tab = pt
                        break
                if not target_tab:
                    self._evt("skip_tab", f"No peer '{target_name}' for tab {tid}")
                    continue
                if not target_tab.pty_session or not target_tab.pty_session.running:
                    self._evt("skip_tab", f"peer '{target_name}' not running")
                    continue
                # 2026-04-24 strictification: sanitize tab.name to avoid the
                # `@@]` empty-name bug (the most common cause of "@@prefix
                # がついてない" stall symptom). _safe_tab_name strips non-alnum
                # and falls back to `tab-<short_id>` if the result is empty.
                safe_name = _safe_tab_name(tab)
                prefix = f"[PEER-DISPATCH from @@{safe_name}]"
                #: quiescence gate before dispatch — refuses to write
                # while target tab shows a permission prompt or is busy
                # generating, preventing direct strikes that auto-confirm
                # an unrelated permission menu.
                try:
                    ok_q, qreason = target_tab.pty_session.is_quiescent(strict=False)
                except Exception:
                    ok_q, qreason = (False, "quiesce_check_failed")
                if not ok_q:
                    self._evt("dispatch_deferred", f"@@{safe_name}→@@{target_tab.name} [PEER-DISPATCH]: {qreason}")
                    continue
                try:
                    ok_w, wreason = target_tab.pty_session.write_dispatch(prefix + "\n" + body)
                    if not ok_w:
                        self._evt("dispatch_rejected", f"@@{safe_name}→@@{target_tab.name} [PEER-DISPATCH]: {wreason}")
                        continue
                    # fix: mark dedup AFTER successful delivery, not before gate.
                    self._dispatched_tab.add(key)
                    self._evt("tab_dispatch", f"@@{safe_name} → @@{target_tab.name}: {body[:80]}")
                    self.api._js(
                        f"onPeerDispatch({json.dumps(tid)},{json.dumps(target_tab.id)},{json.dumps(body[:80])},'tab')"
                    )
                    # Render the full dispatched body on the receiver's chat
                    # pane so the operator can see what triggered Claude's
                    # next response. Full body is passed (not body[:80] which
                    # is the log-truncated form used for yolo-flash logging).
                    self.api._render_dispatched(target_tab, prefix, body, "peer_tab")
                except Exception:
                    pass

    # -- B: cross-project outbound --------------------------------------------

    def _poll_cross_project(self) -> None:
        for tid, tab in list(self.api._tabs.items()):
            if not tab.project_path or not tab.screen_content:
                continue
            peers = self._read_peers(tab.project_path)
            if not peers:
                continue
            self_alias = self._self_alias(tab.project_path)
            for m in self.PROJ_DISPATCH_RE.finditer(tab.screen_content):
                alias = m.group(1).lower()
                role = m.group(2).lower()
                body = re.sub(r"\s+", " ", m.group(3)).strip()
                if len(body) < 5:
                    continue
                peer_path = peers.get(alias)
                if not peer_path:
                    continue
                #: drop tid from dedup key — when N tabs in the same
                # project echo the same `@alias.role: body` (cross-tab screen
                # mirroring), per-tid keys amplified to N inbox writes; the
                # (alias.role, body) tuple collapses that to 1.
                key = f"{alias}.{role}:{body[:300]}"
                if key in self._dispatched_proj:
                    continue
                try:
                    target_dir = Path(peer_path) / ".claude-harness" / "inbox"
                    target_dir.mkdir(parents=True, exist_ok=True)
                    inbox_file = target_dir / f"from_{self_alias}.md"
                    ts = time.strftime("%Y-%m-%d %H:%M:%S")
                    entry = f"\n## [{ts}] to:{role}\n{body}\n"
                    # + cycle 14 (26-05-05) unification: migrated
                    # to _locked_coord_write ctx mgr. Gains cross-process
                    # flock layer (original was threading.Lock-only,
                    # cross-process gap until this cycle). threading_lock
                    # passed to preserve intra-process serialization scope
                    # already validated by mitigation.
                    with _locked_coord_write(inbox_file,
                                             threading_lock=self._inbox_write_lock):
                        with open(inbox_file, "a", encoding="utf-8") as f:
                            f.write(entry)
                    #: mark-after-success (was pre-try) — mkdir/open/write
                    # exception on first attempt previously caused silent drop;
                    # retry now recovers via next _loop iteration.
                    self._dispatched_proj.add(key)
                    self._evt("proj_dispatch", f"{tab.name} → @{alias}.{role}: {body[:80]}")
                    self.api._js(
                        f"onPeerDispatch({json.dumps(tid)},{json.dumps(peer_path)},{json.dumps(body[:80])},'project')"
                    )
                except Exception as e:
                    self._evt("error", f"inbox write failed: {e}")

    # -- B: cross-project inbound (inbox → role tab PTY) ----------------------

    def _poll_inboxes(self) -> None:
        import hashlib
        for tid, tab in list(self.api._tabs.items()):
            if not tab.project_path:
                continue
            inbox = Path(tab.project_path) / ".claude-harness" / "inbox"
            if not inbox.is_dir():
                continue
            for inbox_file in inbox.glob("from_*.md"):
                fkey = str(inbox_file)
                try:
                    content = inbox_file.read_text("utf-8", errors="replace")
                except Exception:
                    continue
                # First time we see a file, mark all existing entries as seen
                # so we don't replay history on startup.
                first_seen = fkey not in self._inbox_file_known
                self._inbox_file_known.add(fkey)
                entries = self._parse_inbox(content)
                src_alias = inbox_file.stem.replace("from_", "", 1)
                for e in entries:
                    ekey_raw = f"{fkey}|{e['ts']}|{e['role']}|{e['body'][:200]}"
                    ekey = hashlib.md5(ekey_raw.encode("utf-8")).hexdigest()
                    if ekey in self._inbox_seen:
                        continue
                    if first_seen:
                        # Pre-existing entries on first observation: mark seen so
                        # we don't replay history on GUI restart, but never attempt
                        # to forward them.
                        self._inbox_seen.add(ekey)  # _BoundedSeenSet self-evicts
                        continue
                    # Mark-after-forward (2026-04-23 MD-coord fix): if the
                    # receiving tab isn't ready yet, do NOT poison the dedup
                    # set — the next poll should retry once the PTY is up.
                    if not self._forward_to_role_tab(
                        tab.project_path, e["role"], src_alias, e["ts"], e["body"]
                    ):
                        continue
                    self._inbox_seen.add(ekey)  # _BoundedSeenSet self-evicts
            # Once per pass, the tab itself is done.

    def _forward_to_role_tab(self, project_path: str, role: str,
                              src_alias: str, ts: str, body: str) -> bool:
        """Forward an inbox entry to the target role's PTY.

        Returns True if the message was actually delivered (PTY accepted both
        the body and the submit Enter), False if the target is unavailable or
        the write failed. Callers use the bool to gate dedup tracking so a
        missed delivery can be retried on the next poll.
        """
        reg = self.api._roles_for(project_path)
        proj_key = self.api._norm_project(project_path)
        target_tab: ChatTab | None = None
        # 2026-04-24 routing strict: display-first role match for cross-project
        # inbox (User directive). tab.role drift (stale cache) was silently
        # routing inbox forwards to wrong tabs; display-based match corrects.
        for tab in self.api._tabs.values():
            if self.api._norm_project(tab.project_path) != proj_key:
                continue
            if _role_effective(tab, reg) == role:
                target_tab = tab
                break
        if not target_tab:
            for tab in self.api._tabs.values():
                if self.api._norm_project(tab.project_path) != proj_key:
                    continue
                eff = _role_effective(tab, reg)
                if eff in reg.coordinators() or eff in reg.ccos():
                    target_tab = tab
                    break
        if not target_tab or not target_tab.pty_session or not target_tab.pty_session.running:
            return False
        # 2026-04-24 strictification: sanitize src_alias to avoid malformed
        # prefix (blank alias → "[PEER-INBOX from @ts]" would be rejected).
        safe_alias = re.sub(r"[^A-Za-z0-9_\-]", "", src_alias or "")
        if not safe_alias:
            safe_alias = "unknown"
        prefix = f"[PEER-INBOX from {safe_alias}@{ts}] → {role}"
        #: quiescence gate before forward — strict=True for
        # cross-project inbox forwarding (sender process can't observe
        # target screen, so a strict gate is the only protection against
        # writing into a permission prompt or active generation).
        try:
            ok_q, qreason = target_tab.pty_session.is_quiescent(strict=True)
        except Exception:
            ok_q, qreason = (False, "quiesce_check_failed")
        if not ok_q:
            self._evt("inbox_deferred", f"inbox {safe_alias}.{role} → {target_tab.name}: {qreason}")
            return False
        try:
            ok_w, wreason = target_tab.pty_session.write_dispatch(prefix + "\n" + body)
            if not ok_w:
                self._evt("dispatch_rejected", f"inbox {safe_alias}.{role}: {wreason}")
                return False
            self._evt("inbox_forward", f"{safe_alias}.{role} → {target_tab.name}")
            self.api._js(
                f"onPeerInbox({json.dumps(safe_alias)},{json.dumps(role)},{json.dumps(body[:80])})"
            )
            self.api._render_dispatched(target_tab, prefix, body, "peer_inbox")
            return True
        except Exception:
            return False

    # -- helpers --------------------------------------------------------------

    def _parse_inbox(self, content: str) -> list[dict]:
        out: list[dict] = []
        lines = content.splitlines()
        i = 0
        n = len(lines)
        while i < n:
            m = self.INBOX_ENTRY_RE.match(lines[i])
            if m:
                ts = m.group(1)
                # 2026-04-23 multi-role: split comma-separated role list.
                roles_raw = m.group(2).lower()
                roles = [r.strip() for r in roles_raw.split(",") if r.strip()]
                body_lines: list[str] = []
                j = i + 1
                while j < n and not lines[j].startswith("## "):
                    body_lines.append(lines[j])
                    j += 1
                body_str = "\n".join(body_lines).strip()
                for role in roles:
                    out.append({
                        "ts": ts, "role": role,
                        "body": body_str,
                    })
                i = j
            else:
                i += 1
        return out

    def _read_peers(self, project_path: str) -> dict[str, str]:
        """Return {alias: abs_path} from <project>/.claude-harness/peers.json."""
        peers_file = Path(project_path) / ".claude-harness" / "peers.json"
        data = _load_json(peers_file, None)
        out: dict[str, str] = {}
        if isinstance(data, dict) and isinstance(data.get("peers"), list):
            for p in data["peers"]:
                if not isinstance(p, dict):
                    continue
                alias = str(p.get("alias", "")).strip().lower()
                alias = re.sub(r"[^a-z0-9_\-]", "", alias)
                path = str(p.get("path", "")).strip()
                if alias and path:
                    out[alias] = path
        return out

    def _self_alias(self, project_path: str) -> str:
        ident_file = Path(project_path) / ".claude-harness" / "identity.json"
        data = _load_json(ident_file, None)
        if isinstance(data, dict) and data.get("alias"):
            alias = str(data["alias"]).strip().lower()
            alias = re.sub(r"[^a-z0-9_\-]", "", alias)
            if alias:
                return alias
        base = Path(project_path).name.lower()
        return re.sub(r"[^a-z0-9_\-]", "", base) or "unknown"

    # -- public state / helpers used by Api API methods -----------------------

    def status(self) -> dict:
        return {
            "running": self.running,
            "log": self._log[-50:],
        }

    def _evt(self, kind: str, msg: str) -> None:
        self._log.append({"ts": time.time(), "type": kind, "msg": msg})
        if len(self._log) > 500:
            self._log = self._log[-250:]


# ---------------------------------------------------------------------------
# Harness Watcher — role proposal files (E) and Manager role requests (F)
# ---------------------------------------------------------------------------
class HarnessWatcher:
    """Polls `<project>/.claude-harness/` and `<project>/coordination/` for
    artifacts produced by two workflows:

    E) **Role generation** — user clicks the sidebar "Generate roles" button.
       The GUI injects a prompt asking Claude to analyse the project and
       write a proposed role set to `.claude-harness/roles_proposal.json`.
       When this watcher sees the file change, it fires `onRoleProposal` so
       the GUI can show a confirmation modal.

    F) **Runtime role creation by Manager** — the Manager (only the
       coordinator role) writes to `coordination/role_requests.md` with
       `## [ts] create` blocks. When a new block appears, the watcher fires
       `onRoleRequest` with the parsed fields; the user approves via the
       GUI, which then calls `apply_role_request()` to actually create the
       `<project>/teams/<id>/` directory, register the role, and spawn a
       dedicated tab.
    """
    REQUEST_HEADER_RE = re.compile(r"^##\s+\[([^\]]+)\]\s+create\s*$")

    def __init__(self, api_ref):
        self.api = api_ref
        self.running = False
        self._thread: threading.Thread | None = None
        self._proposal_hashes: dict[str, str] = {}
        # Persist across restarts so role requests aren't silently suppressed
        # on first-seen of the file after a relaunch (pending approvals used
        # to disappear until Manager wrote a NEW entry).
        # Bounded LRU (cap=2000, trim to 1000). Loaded from disk so requests
        # survive restart. See _BoundedSeenSet for trim correctness.
        self._request_seen = _BoundedSeenSet(cap=2000)
        self._request_seen.update(_load_json(ROLE_REQUEST_SEEN_FILE, {"seen": []}).get("seen", []))
        self._request_files_known: set[str] = set()

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.running = False

    def _loop(self) -> None:
        while self.running:
            try:
                self._poll_proposals()
                self._poll_role_requests()
            except Exception:
                pass
            time.sleep(3)

    def _poll_proposals(self) -> None:
        import hashlib
        seen_projects: set[str] = set()
        for tab in list(self.api._tabs.values()):
            if not tab.project_path:
                continue
            key_p = self.api._norm_project(tab.project_path)
            if key_p in seen_projects:
                continue
            seen_projects.add(key_p)
            proposal = Path(tab.project_path) / ".claude-harness" / "roles_proposal.json"
            if not proposal.exists():
                continue
            try:
                content = proposal.read_text("utf-8", errors="replace")
            except Exception:
                continue
            h = hashlib.md5(content.encode("utf-8", errors="replace")).hexdigest()
            prev = self._proposal_hashes.get(str(proposal))
            self._proposal_hashes[str(proposal)] = h
            if prev == h:
                continue
            # Parsed form: {"roles": [...]} or raw list. Tolerant.
            try:
                parsed = json.loads(content)
            except Exception:
                parsed = None
            roles_list: list = []
            if isinstance(parsed, dict) and isinstance(parsed.get("roles"), list):
                roles_list = parsed["roles"]
            elif isinstance(parsed, list):
                roles_list = parsed
            if not roles_list:
                continue
            self.api._js(
                f"onRoleProposal({json.dumps(tab.project_path)},{json.dumps(roles_list)})"
            )

    def _poll_role_requests(self) -> None:
        import hashlib
        seen_dirty = False
        for tab in list(self.api._tabs.values()):
            if not tab.project_path:
                continue
            req_file = Path(tab.project_path) / "coordination" / "role_requests.md"
            if not req_file.exists():
                continue
            fkey = str(req_file)
            self._request_files_known.add(fkey)
            try:
                content = req_file.read_text("utf-8", errors="replace")
            except Exception:
                continue
            requests = self._parse_requests(content)
            for r in requests:
                rkey_raw = f"{fkey}|{r['ts']}|{r.get('id','')}"
                rkey = hashlib.md5(rkey_raw.encode("utf-8")).hexdigest()
                if rkey in self._request_seen:
                    continue
                # Persisted seen set; no first-seen suppression — if a request
                # is pending across a restart it should still surface.
                self._request_seen.add(rkey)
                seen_dirty = True
                r["project_path"] = tab.project_path
                r["request_key"] = rkey
                self.api._js(f"onRoleRequest({json.dumps(r)})")
        if seen_dirty:
            try:
                _save_json(ROLE_REQUEST_SEEN_FILE, {"seen": list(self._request_seen)})
            except Exception:
                pass

    def _parse_requests(self, content: str) -> list[dict]:
        out: list[dict] = []
        lines = content.splitlines()
        i = 0
        n = len(lines)
        cur: dict | None = None
        while i < n:
            m = self.REQUEST_HEADER_RE.match(lines[i])
            if m:
                if cur:
                    out.append(cur)
                cur = {"ts": m.group(1).strip(), "id": "", "name": "", "icon": "",
                       "color": "", "kind": "worker", "dir": "", "brief": ""}
                i += 1
                continue
            if cur is not None:
                stripped = lines[i].strip()
                if stripped.startswith("## "):
                    out.append(cur)
                    cur = None
                    continue
                # Accept `- key: value`  or `key: value`
                fm = re.match(r"^-?\s*(id|name|icon|color|kind|dir|brief)\s*:\s*(.+)$", stripped, re.IGNORECASE)
                if fm:
                    cur[fm.group(1).lower()] = fm.group(2).strip()
            i += 1
        if cur:
            out.append(cur)
        return [r for r in out if r.get("id")]


# ---------------------------------------------------------------------------
# Backend API
# ---------------------------------------------------------------------------
class Api:
    def __init__(self):
        self._window = None
        self._config = _load_json(CONFIG_FILE, {})
        # Per-project role registries, keyed by normalized absolute path (or ""
        # for tabs without a project — falls back to global ROLES_FILE).
        self._role_registries: dict[str, RoleRegistry] = {}
        self._tabs: dict[str, ChatTab] = {}
        self._active: str = ""
        self._counter = 0
        # Persist serialization: lock + global debounce.
        # Multiple PTY reader threads call _persist() concurrently; without
        # a lock each thread builds its own multi-MB JSON string in parallel,
        # exhausting memory (MemoryError) and killing the reader thread —
        # which freezes the tab's GUI screen since onScreenUpdate stops.
        self._persist_lock = threading.Lock()
        self._persist_last_ts = 0.0
        self._persist_min_interval = 2.0  # global floor across all callers
        print("[boot]   _restore_tabs...", flush=True)
        self._restore_tabs()
        print(f"[boot]   restored {len(self._tabs)} tabs", flush=True)
        # DIAGNOSTIC: --no-daemons skips watcher thread spawning to test
        # whether they're blocking pywebview's main-thread init.
        if "--no-daemons" in sys.argv:
            print("[boot]   --no-daemons: skipping all watcher threads", flush=True)
            self._coord_watcher = None
            self._auto_coord = None
            self._peer_bridge = None
            self._harness_watcher = None
            self._persistent_sync = None
        else:
            print("[boot]   CoordinationWatcher.start...", flush=True)
            self._coord_watcher = CoordinationWatcher(self)
            self._coord_watcher.start()
            print("[boot]   AutoCoordinator.__init__...", flush=True)
            self._auto_coord = AutoCoordinator(self)
            print("[boot]   PeerBridge.start...", flush=True)
            # Tab-to-tab and cross-directory peer bridge (A + B layers)
            self._peer_bridge = PeerBridge(self)
            self._peer_bridge.start()
            print("[boot]   HarnessWatcher.start...", flush=True)
            # Harness watcher: role_requests.md + roles_proposal.json (E + F)
            self._harness_watcher = HarnessWatcher(self)
            self._harness_watcher.start()
            print("[boot]   PersistentSyncDaemon.start...", flush=True)
            # 2026-04-24: Persistent sync daemon — always-on role sync (picks up
            # new .claude/roles/*.md without restart) + MD watch fallback
            # (continues md scan + auto-send even when AutoCoord is stopped,
            # so development can progress through user-silent periods).
            self._persistent_sync = PersistentSyncDaemon(self)
            self._persistent_sync.start()
            print("[boot]   all daemons started", flush=True)

    # -- Role registry lookup (per-project) ------------------------------------

    @staticmethod
    def _norm_project(path: str) -> str:
        if not path:
            return ""
        try:
            return os.path.normcase(os.path.abspath(path))
        except Exception:
            return path

    def _roles_for(self, project_path: str = "") -> RoleRegistry:
        """Return (cached) RoleRegistry for the given project path.

        Tabs without a project share a global registry keyed by "".
        """
        key = self._norm_project(project_path)
        reg = self._role_registries.get(key)
        if reg is None:
            reg = RoleRegistry(project_path)
            self._role_registries[key] = reg
        return reg

    def _roles(self) -> RoleRegistry:
        """Active tab's role registry (convenience for places that used the
        old `self._roles` attribute). Callers that know a project path should
        prefer `_roles_for(path)` directly."""
        t = self._tabs.get(self._active)
        return self._roles_for(t.project_path if t else "")

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

    def _persist(self, force: bool = False):
        # Serialize concurrent callers to avoid duplicating the multi-MB JSON
        # string per thread. Global debounce skips redundant writes when
        # reader threads burst together. force=True bypasses debounce for
        # user-driven actions (tab create/delete/config) that must persist now.
        if not self._persist_lock.acquire(timeout=5.0):
            return
        try:
            now = time.time()
            if not force and (now - self._persist_last_ts) < self._persist_min_interval:
                return
            try:
                payload = {
                    "tabs": [t.serialize() for t in self._tabs.values()],
                    "active": self._active, "counter": self._counter,
                }
                # No indent: persistence file, not human-edited. Indent doubles
                # encoded size and intermediate buffers during json.dumps.
                text = json.dumps(payload, ensure_ascii=False)
                TABS_FILE.parent.mkdir(parents=True, exist_ok=True)
                tmp = TABS_FILE.with_suffix(".json.tmp")
                tmp.write_text(text, "utf-8")
                os.replace(tmp, TABS_FILE)
                self._persist_last_ts = now
            except MemoryError:
                # Don't propagate — reader threads must keep running so the
                # PTY screen stays live. Skip this persist cycle.
                pass
            except Exception:
                pass
        finally:
            self._persist_lock.release()

    # ---- Graph memory bridge (Layer 3, 2026-04-27) -----------------------------
    # Synapse-style selective traversal of coordination/graph/decisions.sqlite.
    # JS-callable via pywebview.api.graph_*. Uses tools/graph/cli.py helpers.
    # Each call opens its own connection (short-lived, WAL-safe under concurrent
    # role tab access). Output is JSON-friendly (dicts of primitives).

    def _graph_db_for_active_tab(self) -> "Path | None":
        """Resolve coordination/graph/decisions.sqlite for the active tab's project."""
        tab = self._tabs.get(self._active)
        if not tab or not tab.project_path:
            return None
        p = Path(tab.project_path) / "coordination" / "graph" / "decisions.sqlite"
        return p if p.exists() else None

    def _graph_call(self, fn_name: str, **kwargs) -> dict:
        """Internal: load tools.graph.cli + invoke a query function with a fresh conn."""
        db = self._graph_db_for_active_tab()
        if db is None:
            return {"ok": False, "error": "graph DB not found for active tab project"}
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "_graph_cli_bridge", db.parent.parent.parent / "tools" / "graph" / "cli.py"
            )
            if not spec or not spec.loader:
                return {"ok": False, "error": "cannot load tools/graph/cli.py"}
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception as e:
            return {"ok": False, "error": f"loader failed: {e}"}
        try:
            from tools.graph.schema import init_db
            conn = init_db(str(db))
            try:
                fn = getattr(mod, fn_name)
                rows = fn(conn, **kwargs)
                return {"ok": True, "rows": rows}
            finally:
                conn.close()
        except Exception as e:
            return {"ok": False, "error": f"{fn_name} failed: {e}"}

    def graph_search(self, query: str, limit: int = 10) -> dict:
        """JS bridge: full-text search the graph for nodes matching `query`."""
        db = self._graph_db_for_active_tab()
        if db is None:
            return {"ok": False, "error": "graph DB not found"}
        try:
            from tools.graph.schema import init_db, fts_available
            conn = init_db(str(db))
            try:
                if fts_available(conn):
                    cur = conn.execute(
                        "SELECT n.id, n.type, n.title, n.role, n.weight, n.quality "
                        "FROM nodes_fts f JOIN nodes n ON n.rowid=f.rowid "
                        "WHERE nodes_fts MATCH ? ORDER BY rank LIMIT ?",
                        (query, max(1, int(limit))),
                    )
                else:
                    pat = f"%{query}%"
                    cur = conn.execute(
                        "SELECT id, type, title, role, weight, quality FROM nodes "
                        "WHERE title LIKE ? OR body LIKE ? ORDER BY weight DESC LIMIT ?",
                        (pat, pat, max(1, int(limit))),
                    )
                rows = [dict(r) for r in cur.fetchall()]
                return {"ok": True, "rows": rows}
            finally:
                conn.close()
        except Exception as e:
            return {"ok": False, "error": f"search failed: {e}"}

    def graph_spread(self, seeds: "list[str] | str", depth: int = 2,
                     decay: float = 0.5, limit: int = 15) -> dict:
        """JS bridge: synapse-style spreading activation from seed node ids.

        `seeds` may be a list or a comma-separated string.
        """
        if isinstance(seeds, str):
            seed_list = [s.strip() for s in seeds.split(",") if s.strip()]
        else:
            seed_list = [str(s).strip() for s in seeds if s and str(s).strip()]
        if not seed_list:
            return {"ok": False, "error": "no seeds provided"}
        db = self._graph_db_for_active_tab()
        if db is None:
            return {"ok": False, "error": "graph DB not found"}
        try:
            import importlib.util
            cli_path = db.parent.parent.parent / "tools" / "graph" / "cli.py"
            spec = importlib.util.spec_from_file_location("_graph_cli_bridge", cli_path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            from tools.graph.schema import init_db
            conn = init_db(str(db))
            try:
                rows = mod.spread_activation(
                    conn, seed_list,
                    max_hops=int(depth),
                    decay_per_hop=float(decay),
                    top_n=int(limit),
                )
                return {"ok": True, "rows": rows, "seeds": seed_list}
            finally:
                conn.close()
        except Exception as e:
            return {"ok": False, "error": f"spread failed: {e}"}

    def graph_related(self, node: str, depth: int = 2, limit: int = 20) -> dict:
        """JS bridge: multi-hop traversal from a single node."""
        db = self._graph_db_for_active_tab()
        if db is None:
            return {"ok": False, "error": "graph DB not found"}
        try:
            from tools.graph.schema import init_db
            conn = init_db(str(db))
            try:
                cur = conn.execute(
                    """
                    WITH RECURSIVE g(node_id, depth) AS (
                      SELECT ?, 0
                      UNION
                      SELECT
                        CASE WHEN e.src = g.node_id THEN e.dst ELSE e.src END,
                        g.depth + 1
                      FROM edges e JOIN g ON (e.src = g.node_id OR e.dst = g.node_id)
                      WHERE g.depth < ?
                    )
                    SELECT DISTINCT n.id, n.type, n.title, n.role, n.weight, MIN(g.depth) AS depth
                    FROM g JOIN nodes n ON n.id = g.node_id
                    GROUP BY n.id ORDER BY depth ASC, n.weight DESC LIMIT ?
                    """,
                    (str(node), int(depth), int(limit)),
                )
                rows = [dict(r) for r in cur.fetchall()]
                return {"ok": True, "rows": rows}
            finally:
                conn.close()
        except Exception as e:
            return {"ok": False, "error": f"related failed: {e}"}

    def graph_stats(self) -> dict:
        """JS bridge: return node/edge counts + breakdowns."""
        db = self._graph_db_for_active_tab()
        if db is None:
            return {"ok": False, "error": "graph DB not found"}
        try:
            from tools.graph.schema import init_db
            conn = init_db(str(db))
            try:
                n_nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
                n_edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
                by_type = {r[0]: r[1] for r in conn.execute(
                    "SELECT type, COUNT(*) FROM nodes GROUP BY type ORDER BY 2 DESC"
                ).fetchall()}
                by_rel = {r[0]: r[1] for r in conn.execute(
                    "SELECT rel, COUNT(*) FROM edges GROUP BY rel ORDER BY 2 DESC"
                ).fetchall()}
                return {"ok": True, "nodes": n_nodes, "edges": n_edges,
                        "by_type": by_type, "by_rel": by_rel}
            finally:
                conn.close()
        except Exception as e:
            return {"ok": False, "error": f"stats failed: {e}"}

    def apply_role_default_efforts(self, force: bool = False) -> dict:
        """JS bridge: migrate all tabs to their role's default_effort.

        Without `force`, tabs whose effort matches the legacy global default
        ("max") OR is invalid get migrated; tabs that look user-customized
        (any other value) are preserved. `force=True` overrides user values
        too — useful for full re-allocation after editing role defaults.

        Returns counts dict so the GUI can render a toast.
        """
        migrated: list[dict] = []
        skipped: list[dict] = []
        for tid, tab in self._tabs.items():
            if not tab.role:
                skipped.append({"id": tid, "reason": "no role"})
                continue
            try:
                reg = self._roles_for(tab.project_path)
                rdef = reg.by_id(tab.role) or {}
                role_eff = rdef.get("default_effort", "")
            except Exception:
                role_eff = ""
            if not role_eff or role_eff not in _VALID_EFFORTS:
                skipped.append({"id": tid, "reason": "no role default"})
                continue
            if tab.effort == role_eff:
                skipped.append({"id": tid, "reason": "already matches"})
                continue
            if not force and tab.effort and tab.effort in _VALID_EFFORTS and tab.effort != "max":
                # User-customized — preserve unless force=True
                skipped.append({"id": tid, "reason": "user-customized",
                                "current": tab.effort})
                continue
            old = tab.effort
            tab.effort = role_eff
            migrated.append({"id": tid, "role": tab.role, "from": old, "to": role_eff})
            # Live PTY: push /effort now if running
            if tab.pty_session and getattr(tab.pty_session, "running", False):
                try:
                    tab.pty_session.write(f"/effort {role_eff}\r")
                except Exception:
                    pass
        try:
            self._persist()
        except Exception:
            pass
        return {"ok": True, "migrated": migrated, "skipped": skipped,
                "n_migrated": len(migrated), "n_skipped": len(skipped)}

    def graph_open_viewer(self, regenerate: bool = True, limit: int = 300) -> dict:
        """JS bridge: regenerate (optional) + open the HTML graph viewer in OS browser.

        Writes coordination/graph/view.html with cytoscape.js-based interactive
        viewer (node colors per type, fcose force-directed layout, search +
        filter). Opens via webbrowser.open so the user sees an actual graph,
        not just rows.
        """
        tab = self._tabs.get(self._active)
        if not tab or not tab.project_path:
            return {"ok": False, "error": "no active project"}
        proj = Path(tab.project_path)
        out_path = proj / "coordination" / "graph" / "view.html"
        if regenerate:
            try:
                import importlib.util
                cli_path = proj / "tools" / "graph" / "cli.py"
                if not cli_path.exists():
                    return {"ok": False, "error": "tools/graph/cli.py missing"}
                spec = importlib.util.spec_from_file_location("_graph_cli_viewer", cli_path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                from tools.graph.schema import init_db
                db = proj / "coordination" / "graph" / "decisions.sqlite"
                if not db.exists():
                    return {"ok": False, "error": f"graph DB missing: {db}"}
                conn = init_db(str(db))
                try:
                    class _Args:
                        out = str(out_path)
                        limit = int(limit)
                        json = False
                    rc = mod.cmd_export_html(conn, _Args)
                finally:
                    conn.close()
                if rc != 0:
                    return {"ok": False, "error": f"export-html rc={rc}"}
            except Exception as e:
                return {"ok": False, "error": f"export failed: {e}"}
        # Open in OS browser (works on Windows/macOS/Linux)
        try:
            import webbrowser
            uri = out_path.absolute().as_uri()
            webbrowser.open(uri)
            return {"ok": True, "path": str(out_path), "uri": uri}
        except Exception as e:
            return {"ok": True, "path": str(out_path), "error_open": str(e)}

    def graph_seed_for_role(self, role: str, top_n: int = 5) -> dict:
        """JS bridge: return seed node ids for a role (recent decisions + role node).

        Used by the GUI to populate the "active context" panel — the role's
        recent decisions become spreading-activation seeds, surfacing their
        2-hop neighborhood as automatically-relevant context.
        """
        if not role:
            return {"ok": False, "error": "role required"}
        db = self._graph_db_for_active_tab()
        if db is None:
            return {"ok": False, "error": "graph DB not found"}
        try:
            from tools.graph.schema import init_db
            conn = init_db(str(db))
            try:
                cur = conn.execute(
                    "SELECT id FROM nodes "
                    "WHERE type='decision' AND role=? "
                    "ORDER BY created_at DESC, weight DESC LIMIT ?",
                    (role, max(1, int(top_n))),
                )
                seeds = [r[0] for r in cur.fetchall()]
                seeds.append(f"role_{role}")
                return {"ok": True, "seeds": seeds, "role": role}
            finally:
                conn.close()
        except Exception as e:
            return {"ok": False, "error": f"seed failed: {e}"}

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
        """Worker thread: start PTY, detect dead sessions, retry as new.

        The 3s death-check was previously too short — slow starts (resume of
        large sessions, CI machines, cold cache) got killed as "invalid
        session_id" even when they would have recovered. 2026-04-23
        gui-session-continuity-fix extends the window to 10s with early exit
        when either (a) welcome pattern detected on screen, or (b) process
        dies (fast failure path unchanged). Failure → UI toast + clear
        session_id so next start is a clean --new.
        """
        tid = tab.id
        try:
            if tab.pty_session:
                tab.pty_session.kill()
            tab.pty_session = PtySession(self, tab)
            tab.pty_session.start()
            self._js(f"showTerminal({json.dumps(tid)})")
            # Poll for up to 10s with early exit on welcome or death.
            alive = True
            welcome = False
            for _ in range(100):  # 10s at 0.1s intervals
                ps = tab.pty_session
                if not ps or not ps.pty or not ps.pty.isalive():
                    alive = False
                    break
                try:
                    screen_text = "\n".join(ps.screen.display).lower()
                    if ('>' in screen_text or '❯' in screen_text
                            or 'claude' in screen_text or 'welcome' in screen_text):
                        welcome = True
                        break
                except Exception:
                    pass
                time.sleep(0.1)
            if not alive:
                # Session died — if it had a session_id, --resume likely
                # failed. Clear and retry as new.
                if tab.session_id:
                    self._js(
                        f"onSessionRestoreFailed({json.dumps(tid)},"
                        f"{json.dumps(tab.session_id)})"
                    )
                    tab.session_id = ""
                    self._persist()
                    tab.pty_session = PtySession(self, tab)
                    tab.pty_session.start()
                    self._js(f"showTerminal({json.dumps(tid)})")
                    self._wait_and_remote_control(tab)
                else:
                    self._js(
                        f"onStreamError({json.dumps(tid)},"
                        f"{json.dumps(self._t('sessStartFailed'))})"
                    )
            else:
                if tab.session_id and welcome:
                    self._js(f"onSessionResumed({json.dumps(tid)})")
                self._wait_and_remote_control(tab)
        except FileNotFoundError:
            self._js(f"onStreamError('{tid}',{json.dumps(self._t('cli_not_found'))})")
        except Exception as e:
            self._js(f"onStreamError('{tid}',{json.dumps(str(e))})")

    def _wait_and_remote_control(self, tab: ChatTab):
        """Wait for CLI ready then auto-fire /remote-control + /effort + /role.

        Order (2026-04-24 user preference):
          1. /remote-control — activate coordination protocol baseline
          2. /effort <level> — set reasoning depth (if non-default)
          3. /role <tab.role> — load role identity last, so role.md read
                                happens in the correct mode/effort context

        2026-04-27 bug-fixes (user report: /role 未送信 / "role doesn't exist"):
          - poll timeout 5s → 15s (cold-start / large project boot)
          - indicator: drop `claude` keyword (false-positive on welcome banner),
            require actual prompt char `>`/`❯` AT END of a recent line
          - timeout-fallback: even if no indicator appears, sleep 3s instead of 0
            so commands don't race a not-yet-ready prompt
          - inter-command spacing 0.5s → 1.0s (registry load before /role)
          - /role retry with verification: if "does not exist" detected on
            screen within 2s, retry once with normalized role id
        """
        try:
            if not tab.pty_session or not tab.pty_session.running:
                return
            # Phase 1: wait for prompt indicator (15s budget, 0.1s polling)
            ready = False
            for _ in range(150):  # 15s
                try:
                    lines = tab.pty_session.screen.display
                    # Look at the last 5 non-empty lines for an actual prompt char
                    tail = [ln.rstrip() for ln in lines[-5:] if ln.strip()]
                    if any(ln.endswith(">") or ln.endswith("❯") or ln.endswith("> ") or ln.endswith("❯ ") or ">" in ln[-3:] for ln in tail):
                        time.sleep(1.0)  # extra settle
                        ready = True
                        break
                except Exception:
                    pass
                time.sleep(0.1)
            if not ready:
                # Conservative fallback: prompt never confirmed. Don't race —
                # wait an extra 3s so any in-flight CLI boot finishes.
                time.sleep(3.0)
            if not (tab.pty_session and tab.pty_session.running):
                return

            # Phase 2: /remote-control
            try:
                tab.pty_session.write("/remote-control\r")
            except Exception:
                pass
            time.sleep(1.0)

            # Phase 3: /effort — 2026-04-27 per-role default-aware send.
            # Resolution:
            #   1. If tab.role is set + role has default_effort, that's the
            #      "intended" effort for this role.
            #   2. Migration: if tab.effort is still the legacy global default
            #      "max" but role default is not "max", adopt role default
            #      (one-time silent migration for tabs stuck on old default).
            #   3. Otherwise use tab.effort as-is (user override survives).
            effective_effort = tab.effort or ""
            if tab.role:
                try:
                    reg = self._roles_for(tab.project_path)
                    rdef = reg.by_id(tab.role) or {}
                    role_eff = rdef.get("default_effort", "")
                    if role_eff in _VALID_EFFORTS:
                        # Migrate stale "max" default to role default
                        if tab.effort == "max" and role_eff != "max":
                            tab.effort = role_eff
                            try:
                                self._persist()
                            except Exception:
                                pass
                            effective_effort = role_eff
                        # Empty / invalid tab.effort → role default
                        elif tab.effort not in _VALID_EFFORTS:
                            tab.effort = role_eff
                            try:
                                self._persist()
                            except Exception:
                                pass
                            effective_effort = role_eff
                except Exception:
                    pass
            if effective_effort and effective_effort in _VALID_EFFORTS:
                try:
                    tab.pty_session.write(f"/effort {effective_effort}\r")
                except Exception:
                    pass
                time.sleep(1.0)

            # Phase 4: /role with verification + retry
            if tab.role:
                self._send_role_with_retry(tab)
                # 2026-04-27 mailbox catch-up: surface any pre-existing
                # peer_<role>.md content that was queued before this tab
                # spawned (Gap 1 in mailbox delivery — see protocol §
                # Session start step 7). Sends a [PEER-INBOX] hint so the
                # role tail-reads on its own.
                try:
                    self._send_peer_inbox_hint(tab)
                except Exception:
                    pass
        except Exception:
            pass

    def _send_peer_inbox_hint(self, tab: ChatTab) -> None:
        """If `coordination/peer_<role>.md` is non-empty, dispatch a one-shot
        [PEER-INBOX] hint pointing the role at the tail. Avoids the silent
        loss of mailbox entries written before the tab existed.
        """
        if not tab.role or not tab.project_path:
            return
        if not tab.pty_session or not tab.pty_session.running:
            return
        peer_path = Path(tab.project_path) / "coordination" / f"peer_{tab.role}.md"
        try:
            if not peer_path.exists():
                return
            size = peer_path.stat().st_size
            if size <= 0:
                return
            # Count non-blank, non-separator lines as proxy for entry density
            lines = peer_path.read_text("utf-8", errors="replace").splitlines()
            content_lines = sum(1 for ln in lines
                                if ln.strip() and not ln.strip().startswith("---"))
            if content_lines < 2:
                return  # essentially empty / fresh template — no notify
        except Exception:
            return
        msg = (
            f"[PEER-INBOX from coordination/peer_{tab.role}.md]\n"
            f"Mailbox contains {content_lines} non-empty line(s) on tab spawn. "
            f"Read tail-10 to catch any pre-spawn directed messages.\n"
        )
        try:
            tab.pty_session.write_dispatch(msg)
        except Exception:
            try:
                tab.pty_session.write(msg + "\n")
            except Exception:
                pass

    def _send_role_with_retry(self, tab: ChatTab) -> None:
        """Send `/role <name>` with one retry on "does not exist" error.

        Two failure modes addressed:
        1. Timing — Claude CLI's role registry not yet loaded when /role fires.
           Mitigation: sleep 1.5s after send, then check screen for error
           pattern; if found, sleep 2s more and retry once.
        2. Naming mismatch — tab.role might be stored as a normalized id
           (e.g. "manager") but the CLI expects the file stem. Both forms
           usually match for the stock roles, but as a defensive measure,
           the retry uses both the original tab.role and a sanitized variant.
        """
        if not tab.role or not tab.pty_session:
            return
        primary = tab.role.strip()
        # First attempt
        try:
            tab.pty_session.write(f"/role {primary}\r")
        except Exception:
            return
        time.sleep(1.5)
        # Check for "does not exist" / "not found" error pattern in the last 10 lines
        if not self._role_command_failed(tab):
            return
        # Retry: pause for registry load, then re-send
        time.sleep(2.0)
        if not (tab.pty_session and tab.pty_session.running):
            return
        # Try sanitized form (lowercase, alphanumerics + underscore only)
        retry_id = "".join(ch for ch in primary.lower() if ch.isalnum() or ch == "_")
        try:
            tab.pty_session.write(f"/role {retry_id or primary}\r")
        except Exception:
            pass

    @staticmethod
    def _role_command_failed(tab: ChatTab) -> bool:
        """Return True if the last screen lines contain a /role error indicator."""
        try:
            lines = tab.pty_session.screen.display[-10:]
            blob = "\n".join(lines).lower()
            for needle in ("does not exist", "not found", "unknown role",
                           "no such role", "invalid role"):
                if needle in blob:
                    return True
        except Exception:
            pass
        return False

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
            # Initial roles = active tab's project's roles (frontend refreshes
            # on tab switch via get_roles). Fallback to global defaults.
            "roles": self._roles().all(),
            "has_pty": HAS_PTY,
            # Persisted Auto Coordination state. JS uses this to re-activate
            # the coord loop on startup (staggered after tab PTYs spawn).
            "auto_coord_running": bool(self._config.get("auto_coord_running", False)),
            "coord_collapsed": bool(self._config.get("coord_collapsed", False)),
        }

    def _tab_info(self, t: ChatTab) -> dict:
        return {
            "id": t.id, "name": t.name, "project_path": t.project_path,
            "project_name": Path(t.project_path).name if t.project_path else "",
            "model": t.model, "effort": t.effort, "max_turns": t.max_turns,
            "custom_flags": t.custom_flags, "system_prompt": t.system_prompt,
            "permission_mode": t.permission_mode, "allowed_tools": t.allowed_tools,
            "yolo_mode": t.yolo_mode, "role": t.role,
            "peer_tabs": list(t.peer_tabs or []),
            "messages": t.messages[-30:], "session_id": t.session_id,
            "screen_content": t.screen_content[-20000:] if t.screen_content else "",
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
        # Cascade: drop closed tab_id from other tabs' peer_tabs so
        # PeerBridge doesn't silently skip phantom targets.
        for t in self._tabs.values():
            if t.peer_tabs:
                t.peer_tabs = [p for p in t.peer_tabs if p != tab_id]
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
            # Directory changed → the existing CLI is still running in the OLD
            # project dir. Kill it so the next send spawns fresh in the new dir
            # (session_id is cleared below, so no stale --resume either).
            if path != tab.project_path and tab.pty_session:
                try:
                    tab.pty_session.kill()
                except Exception:
                    pass
                tab.pty_session = None
            tab.project_path = path
            tab.session_id = ""
            tab.name = Path(path).name
            # Project changed → tab's role may not exist in the new project's
            # registry. Clear it silently so the UI drops the stale badge;
            # user re-assigns if needed. Peer list is per-tab, preserved.
            new_reg = self._roles_for(path)
            if tab.role and not new_reg.is_valid(tab.role):
                tab.role = ""
            self._persist()
            return {
                "path": path,
                "name": tab.name,
                "role": tab.role,
                "roles": new_reg.all(),
            }
        return None

    def pick_folder(self, initial: str = "") -> dict:
        """Generic folder picker — used by the cross-project peers editor."""
        try:
            result = self._window.create_file_dialog(
                webview.FOLDER_DIALOG, directory=initial or ""
            )
            if result and len(result) > 0:
                path = result[0] if isinstance(result, (list, tuple)) else str(result)
                return {"ok": True, "path": path}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": False}

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

    def set_tab_yolo(self, tab_id: str, enabled: bool) -> dict:
        """Yolo Mode: auto-accept safe permission prompts. Danger patterns still require manual confirm."""
        t = self._tabs.get(tab_id)
        if not t:
            return {"ok": False}
        t.yolo_mode = bool(enabled)
        self._persist()
        return {"ok": True, "yolo_mode": t.yolo_mode}

    def set_tab_role(self, tab_id: str, role: str) -> dict:
        """Assign a coordination role to the tab.

        Role IDs come from the tab's project's RoleRegistry (user-editable,
        project-scoped). Sends `/role <name>` to the running session so
        project-level role prompts can load.

        2026-04-26: when `AUTO_RENAME_ON_ROLE_SET=True` and a non-empty
        valid role is assigned, the tab is auto-renamed to "<icon> <Name>"
        derived from the registry. Empty role (clear) leaves the name intact.
        Set the flag to False for legacy "manual rename only" behavior.
        """
        t = self._tabs.get(tab_id)
        if not t:
            return {"ok": False}
        role = role or ""
        reg = self._roles_for(t.project_path)
        if not reg.is_valid(role):
            return {"ok": False, "error": f"Unknown role '{role}'. Known: {reg.ids()}"}
        t.role = role
        renamed_to = ""
        if AUTO_RENAME_ON_ROLE_SET and role:
            display = _role_display_name(reg.by_id(role))
            if display and display != t.name:
                t.name = display
                renamed_to = display
        # 2026-04-27 per-role default_effort: when assigning a role, also adopt
        # its default effort level (token-budget-aware allocation, see
        # ROLE_DEFAULT_EFFORTS). User can still override per-tab via dropdown
        # or `/effort <level>`. Skips if role has no default (custom roles).
        effort_changed = ""
        if role:
            role_def = reg.by_id(role) or {}
            new_effort = role_def.get("default_effort", "")
            if new_effort and new_effort in _VALID_EFFORTS and new_effort != t.effort:
                t.effort = new_effort
                effort_changed = new_effort
        self._persist()
        if role and t.pty_session and t.pty_session.running:
            # 2026-04-27 user report: occasional "/role does not exist" race.
            # Use the same retry-on-error helper as auto-restart path.
            try:
                self._send_role_with_retry(t)
            except Exception:
                # Defensive fallback to single direct write
                try:
                    t.pty_session.write(f"/role {role}\r")
                except Exception:
                    pass
            # If effort changed via role default, push it too (non-fatal on fail)
            if effort_changed:
                try:
                    t.pty_session.write(f"/effort {effort_changed}\r")
                except Exception:
                    pass
        if renamed_to:
            try:
                self._js(f"onTabRenamed({json.dumps(t.id)},{json.dumps(renamed_to)})")
            except Exception:
                pass
        return {"ok": True, "role": t.role, "name": t.name, "renamed": bool(renamed_to)}

    # -- Role Registry API -----------------------------------------------------

    def get_roles(self, tab_id: str = "") -> list[dict]:
        """Return role definitions for the given tab's project (or active tab).

        Passing an empty/unknown tab_id falls back to the active tab, then to
        the global default registry.
        """
        t = self._tabs.get(tab_id or self._active)
        return self._roles_for(t.project_path if t else "").all()

    def get_roles_for_path(self, project_path: str = "") -> list[dict]:
        """Return role definitions for an arbitrary project path (no tab needed).

        Used by the sidebar "generate roles" workflow before the proposal is
        applied, and for peer-project previews.
        """
        return self._roles_for(project_path).all()

    def set_roles(self, tab_id: str, roles: list) -> dict:
        """Replace the role list for the tab's project. Validates and persists.

        After a successful save, any running coordinator whose manager lives
        in the same project is restarted so dispatch regex / worker lists
        refresh immediately. Tabs in that project with a role no longer in
        the set are cleared.
        """
        t = self._tabs.get(tab_id or self._active)
        if not t:
            return {"ok": False, "error": "No active tab."}
        reg = self._roles_for(t.project_path)
        ok, err = reg.set_all(roles)
        if not ok:
            return {"ok": False, "error": err}
        # Clear any tab.role in the same project that no longer exists.
        valid = set(reg.ids())
        proj_key = self._norm_project(t.project_path)
        for tab in self._tabs.values():
            if self._norm_project(tab.project_path) != proj_key:
                continue
            if tab.role and tab.role not in valid:
                tab.role = ""
        self._persist()
        # Restart coordinator if its manager is in this project.
        was_running = self._auto_coord.running
        same_project = (
            was_running
            and self._norm_project(self._auto_coord._manager_project) == proj_key
        )
        if same_project:
            self._auto_coord.stop()
            self._auto_coord.start()
        return {"ok": True, "roles": reg.all(), "project": t.project_path}

    # -- Peer / Harness API (A + B) -------------------------------------------

    def set_tab_peers(self, tab_id: str, peer_ids: list) -> dict:
        """Register in-GUI peer tabs for tab-to-tab @@name: dispatch."""
        t = self._tabs.get(tab_id)
        if not t:
            return {"ok": False, "error": "No tab"}
        cleaned: list[str] = []
        for pid in peer_ids or []:
            if not isinstance(pid, str) or pid == tab_id:
                continue
            if pid in self._tabs and pid not in cleaned:
                cleaned.append(pid)
        t.peer_tabs = cleaned
        self._persist()
        return {"ok": True, "peer_tabs": cleaned}

    def get_project_peers(self, project_path: str = "") -> dict:
        """Return the cross-project peer registry for a project."""
        t = self._tabs.get(self._active)
        path = project_path or (t.project_path if t else "")
        if not path:
            return {"ok": False, "error": "No project"}
        peers_file = Path(path) / ".claude-harness" / "peers.json"
        ident_file = Path(path) / ".claude-harness" / "identity.json"
        data = _load_json(peers_file, {"peers": []})
        peers = data.get("peers", []) if isinstance(data, dict) else []
        ident = _load_json(ident_file, {})
        alias = ""
        if isinstance(ident, dict) and ident.get("alias"):
            alias = str(ident["alias"])
        return {"ok": True, "project": path, "peers": peers, "self_alias": alias}

    def set_project_peers(self, project_path: str, peers: list, self_alias: str = "") -> dict:
        """Replace the cross-project peer registry. Validates entries."""
        if not project_path:
            return {"ok": False, "error": "No project path"}
        cleaned: list[dict] = []
        seen_alias: set[str] = set()
        for p in peers or []:
            if not isinstance(p, dict):
                continue
            alias = re.sub(r"[^a-z0-9_\-]", "", str(p.get("alias", "")).strip().lower())
            path = str(p.get("path", "")).strip()
            if not alias or not path or alias in seen_alias:
                continue
            seen_alias.add(alias)
            cleaned.append({"alias": alias, "path": path})
        peers_file = Path(project_path) / ".claude-harness" / "peers.json"
        _save_json(peers_file, {"peers": cleaned})
        # Identity alias (self)
        if self_alias is not None:
            alias = re.sub(r"[^a-z0-9_\-]", "", str(self_alias).strip().lower())
            ident_file = Path(project_path) / ".claude-harness" / "identity.json"
            if alias:
                _save_json(ident_file, {"alias": alias})
            elif ident_file.exists():
                try:
                    ident_file.unlink()
                except Exception:
                    pass
        return {"ok": True, "peers": cleaned, "self_alias": self_alias}

    # -- Role generation (E) & runtime role creation (F) ----------------------

    def trigger_role_generation(self, tab_id: str = "") -> dict:
        """Inject a prompt asking Claude to analyse the project and write a
        role proposal JSON to `.claude-harness/roles_proposal.json`.

        The HarnessWatcher forwards the file change to the GUI, which opens a
        confirmation modal. Returns immediately; the actual apply happens via
        `apply_role_proposal()`.
        """
        t = self._tabs.get(tab_id or self._active)
        if not t:
            return {"ok": False, "error": "No tab"}
        if not t.project_path:
            return {"ok": False, "error": "Tab has no project"}
        if not t.pty_session or not t.pty_session.running:
            return {"ok": False, "error": "Tab PTY not running"}
        # Seed the directory so the file path is valid
        harness_dir = Path(t.project_path) / ".claude-harness"
        harness_dir.mkdir(parents=True, exist_ok=True)
        proposal = harness_dir / "roles_proposal.json"
        existing_roles = self._roles_for(t.project_path).all()
        existing_ids = ", ".join(r["id"] for r in existing_roles) or "(none)"
        prompt = (
            "[HARNESS: GENERATE ROLES]\n"
            f"Analyse this project (directory: {t.project_path}) and propose the "
            "set of coordination roles needed to run a multi-agent team here.\n"
            "\n"
            "REQUIREMENTS:\n"
            "- MUST include exactly one `coordinator` kind (usually `manager`).\n"
            "- MUST include `developer`, `marketing`, `security` as workers (rename fine).\n"
            "- MAY add project-specific `worker` roles (designer, qa, ops, researcher, writer, etc.)\n"
            "- MAY add at most one `cco` kind if multi-team integration is expected.\n"
            "- Use short lowercase role ids ([a-z0-9_], max 24 chars).\n"
            "- Pick distinct hex colours and single-emoji icons.\n"
            "\n"
            f"Existing roles (may replace/extend): {existing_ids}\n"
            "\n"
            f"WRITE the proposal as JSON to: `{proposal}`\n"
            "Format: `{\"roles\": [{\"id\": \"...\", \"name\": \"...\", \"icon\": \"...\", \"color\": \"#...\", \"kind\": \"worker|coordinator|cco\"}, ...]}`\n"
            "After writing the file, reply with a one-line summary of what you added and why.\n"
        )
        try:
            t.pty_session.write_submit(prompt)
        except Exception as e:
            return {"ok": False, "error": f"PTY write failed: {e}"}
        return {"ok": True, "proposal_path": str(proposal)}

    def apply_role_proposal(self, project_path: str = "", tab_id: str = "") -> dict:
        """Read `.claude-harness/roles_proposal.json` for the given project
        (or the active tab's project) and apply it to the project's role
        registry. Restarts the coordinator if running.
        """
        if not project_path:
            t = self._tabs.get(tab_id or self._active)
            if not t or not t.project_path:
                return {"ok": False, "error": "No project"}
            project_path = t.project_path
        proposal = Path(project_path) / ".claude-harness" / "roles_proposal.json"
        if not proposal.exists():
            return {"ok": False, "error": "No proposal file"}
        try:
            data = json.loads(proposal.read_text("utf-8", errors="replace"))
        except Exception as e:
            return {"ok": False, "error": f"Invalid JSON: {e}"}
        roles_list = data.get("roles") if isinstance(data, dict) else data
        if not isinstance(roles_list, list) or not roles_list:
            return {"ok": False, "error": "Proposal has no 'roles' list"}
        reg = self._roles_for(project_path)
        ok, err = reg.set_all(roles_list)
        if not ok:
            return {"ok": False, "error": err}
        # Drop stale tab.role values in this project
        valid = set(reg.ids())
        proj_key = self._norm_project(project_path)
        for tab in self._tabs.values():
            if self._norm_project(tab.project_path) != proj_key:
                continue
            if tab.role and tab.role not in valid:
                tab.role = ""
        self._persist()
        # Restart coordinator if relevant
        if self._auto_coord.running and self._norm_project(self._auto_coord._manager_project) == proj_key:
            self._auto_coord.stop()
            self._auto_coord.start()
        # Optionally delete the proposal so repeat-clicks don't re-trigger
        try:
            proposal.unlink()
        except Exception:
            pass
        return {"ok": True, "roles": reg.all(), "project": project_path}

    def dismiss_role_proposal(self, project_path: str) -> dict:
        """Delete the proposal file without applying it."""
        if not project_path:
            return {"ok": False, "error": "No project"}
        proposal = Path(project_path) / ".claude-harness" / "roles_proposal.json"
        if proposal.exists():
            try:
                proposal.unlink()
            except Exception as e:
                return {"ok": False, "error": str(e)}
        return {"ok": True}

    def apply_role_request(self, request: dict) -> dict:
        """Create the directory, register the role, and (optionally) spawn a
        tab for a Manager-issued role creation request.

        Only processes requests whose issuing Manager is the current
        project's coordinator — i.e. the request must have originated from a
        tab whose role is in coordinators(). Callers pass the parsed request
        dict from `onRoleRequest`.
        """
        project_path = str(request.get("project_path", ""))
        if not project_path:
            return {"ok": False, "error": "No project_path"}
        rid = re.sub(r"[^a-z0-9_]", "", str(request.get("id", "")).lower())
        if not rid:
            return {"ok": False, "error": "No role id"}
        reg = self._roles_for(project_path)
        existing = reg.all()
        new_role = {
            "id": rid,
            "name": str(request.get("name") or rid.title()),
            "icon": str(request.get("icon") or "\U0001F539"),
            "color": str(request.get("color") or "#a78bfa"),
            "kind": str(request.get("kind") or "worker").lower(),
        }
        # Replace if id exists, else append
        merged = [r for r in existing if r["id"] != rid] + [new_role]
        ok, err = reg.set_all(merged)
        if not ok:
            return {"ok": False, "error": err}
        # Create sub-directory
        sub = str(request.get("dir") or f"teams/{rid}").strip()
        sub_path = Path(project_path) / sub
        try:
            sub_path.mkdir(parents=True, exist_ok=True)
            # Drop a README so it's not empty
            readme = sub_path / "README.md"
            if not readme.exists():
                brief = str(request.get("brief") or f"Workspace for {new_role['name']}")
                readme.write_text(
                    f"# {new_role['name']}\n\n{brief}\n\n"
                    f"Role id: `{rid}`  •  Kind: `{new_role['kind']}`\n",
                    "utf-8",
                )
        except Exception as e:
            return {"ok": False, "error": f"dir create failed: {e}"}
        # Spawn a tab pointing at the sub-directory
        tab = self._make_tab(str(sub_path))
        tab.role = rid
        tab.name = new_role["name"]
        self._persist()
        # Notify frontend
        self._js(f"onRoleRequestApplied({json.dumps(request.get('request_key',''))},{json.dumps(self._tab_info(tab))})")
        return {"ok": True, "tab": self._tab_info(tab), "roles": reg.all()}

    def dismiss_role_request(self, request_key: str) -> dict:
        """Dismiss (no-op) a role request. For now this just drops from the
        dedup set on the backend isn't needed — the frontend simply ignores
        it — but we return ok for symmetry with apply."""
        return {"ok": True}

    # -- New Project Wizard (2026-04-24) --------------------------------------

    def wizard_propose_roles(self, overview: str = "", project_type: str = "other") -> dict:
        """Propose a role set for a new project via Anthropic API (strict JSON tool use).

        Falls back to `WIZARD_TYPE_DEFAULTS[project_type]` when the API key is
        missing, the call errors, or the proposal fails shape validation
        (exactly one coordinator + at least one worker).

        Returns {ok, roles, source: "api"|"fallback", reason?, rationale?}.
        """
        ptype = (project_type or "other").strip().lower()
        if ptype not in WIZARD_TYPE_DEFAULTS:
            ptype = "other"
        overview = (overview or "").strip()[:800]
        fallback = [dict(r) for r in WIZARD_TYPE_DEFAULTS[ptype]]

        key = self._config.get("api_key", "")
        if not key:
            return {"ok": True, "roles": fallback, "source": "fallback", "reason": "api_key_missing"}

        try:
            import anthropic
            client = anthropic.Anthropic(api_key=key)
            sys_p = (
                "You are a multi-agent coordination architect. Propose a role set "
                "(2-5 roles) for a new project. Requirements:\n"
                "- EXACTLY ONE role must have kind=\"coordinator\" (e.g. manager/editor/analyst).\n"
                "- AT LEAST ONE role must have kind=\"worker\".\n"
                "- role id: lowercase a-z0-9_, unique, max 24 chars.\n"
                "- icon: ONE emoji character (1-4 chars).\n"
                "- color: #RRGGBB hex.\n"
                "- name: concise display name (max 24 chars).\n"
                "- kind: \"worker\" | \"coordinator\" | \"cco\" (use cco only for multi-team hubs).\n"
                "Return ONLY via the propose_roles tool; do not emit natural-language output."
            )
            user_msg = (
                f"Project type: {ptype}\n"
                f"Overview: {overview or '(not provided)'}\n\n"
                "Propose the optimal role team. Keep it small (2-4 roles) unless the overview clearly requires more."
            )
            tool_schema = {
                "name": "propose_roles",
                "description": "Register the proposed role set for a new multi-agent project.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "roles": {
                            "type": "array",
                            "minItems": 2, "maxItems": 5,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id":    {"type": "string", "pattern": "^[a-z0-9_]{1,24}$"},
                                    "name":  {"type": "string", "minLength": 1, "maxLength": 24},
                                    "icon":  {"type": "string", "minLength": 1, "maxLength": 4},
                                    "color": {"type": "string", "pattern": "^#[0-9a-fA-F]{6}$"},
                                    "kind":  {"type": "string", "enum": ["worker", "coordinator", "cco"]},
                                },
                                "required": ["id", "name", "icon", "color", "kind"],
                            },
                        },
                        "rationale": {"type": "string", "maxLength": 400},
                    },
                    "required": ["roles"],
                },
            }
            #: prompt cache. The system prompt + tool schema
            # are stable across wizard invocations within a session, so they
            # can cache. attach_tools_cache_marker is idempotent — safe to
            # call even when caching is disabled (returns the list unchanged).
            cached_system = build_cached_system_block(sys_p, ttl=PROMPT_CACHE_SYSTEM_TTL)
            cached_tools = attach_tools_cache_marker([tool_schema], ttl=PROMPT_CACHE_TOOLS_TTL)
            resp = client.messages.create(
                model=DEFAULT_MODEL,
                max_tokens=1024,
                system=cached_system,
                messages=[{"role": "user", "content": user_msg}],
                tools=cached_tools,
                tool_choice={"type": "tool", "name": "propose_roles"},
            )
            roles = None
            rationale = ""
            for block in resp.content:
                if getattr(block, "type", "") == "tool_use" and getattr(block, "name", "") == "propose_roles":
                    data = block.input or {}
                    roles = data.get("roles")
                    rationale = str(data.get("rationale", ""))[:400]
                    break
            if not isinstance(roles, list) or not roles:
                return {"ok": True, "roles": fallback, "source": "fallback", "reason": "api_no_tool_use"}
            coords = [r for r in roles if isinstance(r, dict) and r.get("kind") == "coordinator"]
            workers = [r for r in roles if isinstance(r, dict) and r.get("kind") == "worker"]
            if len(coords) != 1 or not workers:
                return {"ok": True, "roles": fallback, "source": "fallback", "reason": "api_invalid_shape"}
            # Dedup by id, clip count to 5
            seen: set[str] = set()
            clean: list[dict] = []
            for r in roles:
                rid = str(r.get("id", "")).strip().lower()
                if not rid or rid in seen:
                    continue
                seen.add(rid)
                clean.append({
                    "id": rid,
                    "name": str(r.get("name") or rid.title())[:24],
                    "icon": str(r.get("icon") or "\U0001F539")[:4],
                    "color": str(r.get("color") or "#a78bfa"),
                    "kind": r.get("kind", "worker"),
                })
                if len(clean) >= 5:
                    break
            return {"ok": True, "roles": clean, "source": "api", "rationale": rationale}
        except Exception as e:
            return {"ok": True, "roles": fallback, "source": "fallback", "reason": f"api_error: {e}"}

    def wizard_create_project(
        self,
        project_path: str,
        overview: str = "",
        project_type: str = "other",
        roles: list | None = None,
        start_auto_coord: bool = True,
    ) -> dict:
        """Scaffold a new project and spawn one tab per role.

        Steps: create dir, write `.claude-harness/roles.json`, scaffold
        `coordination/` (README/peer_<role>/blockers/decisions_log/per-worker
        reports), stub `CLAUDE.md`, spawn N tabs, activate the coordinator tab,
        optionally start Auto Coordination.

        Returns {ok, project, tabs: [...], coordinator_tab_id, auto_coord_started}.
        """
        project_path = (project_path or "").strip()
        if not project_path:
            return {"ok": False, "error": "Project path required"}
        if not isinstance(roles, list) or not roles:
            return {"ok": False, "error": "Roles required"}

        # Validate + persist roles via RoleRegistry (enforces coord+worker rule).
        reg = self._roles_for(project_path)
        ok, err = reg.set_all(roles)
        if not ok:
            return {"ok": False, "error": f"Invalid roles: {err}"}
        cleaned_roles = reg.all()

        # 1. Create project directory.
        try:
            proj_dir = Path(project_path)
            proj_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return {"ok": False, "error": f"Cannot create project dir: {e}"}

        # 2. Coordination scaffolding (don't clobber existing files).
        coord_dir = proj_dir / "coordination"
        try:
            coord_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return {"ok": False, "error": f"Cannot create coordination dir: {e}"}
        now = time.strftime("%Y-%m-%d %H:%M")
        proj_name = proj_dir.name or "project"
        ptype_disp = (project_type or "other").strip().lower()

        def _wif(p: Path, body: str):
            if not p.exists():
                try:
                    p.write_text(body, encoding="utf-8")
                except Exception:
                    pass

        _wif(coord_dir / "README.md", (
            f"# {proj_name} — Coordination\n\n"
            f"**Type**: {ptype_disp}\n"
            f"**Created**: {now}\n\n"
            f"## Overview\n\n{overview or '(no overview provided)'}\n\n"
            "## Files\n\n"
            "- `peer_<role>.md` — Per-role mailbox (dispatch + comms via MD-WATCH fanout)\n"
            "- `blockers.md` — User decisions / blockers\n"
            "- `decisions_log.md` — Cycle decisions\n"
            "- `{role}_report.md` — Per-worker status reports\n"
            "\n"
            "All dispatches flow through `peer_<role>.md` mailboxes. Coordinator\n"
            "appends\n"
            "`## [ts] from:mgr\\n@<role> dispatch: ...\\n---\\n` entries.\n"
        ))
        # New projects run dispatch on peer_<role>.md mailboxes +
        # decisions_log only (no single-queue file is scaffolded).
        _wif(coord_dir / "blockers.md", (
            f"# Blockers — {proj_name}\n\n"
            "Pending user decisions or external dependencies.\n\n"
            "Format: `## [timestamp] title` + detail + resolution.\n"
        ))
        _wif(coord_dir / "decisions_log.md", (
            f"# Decisions Log — {proj_name}\n\n"
            f"Created: {now}\n"
        ))
        for r in cleaned_roles:
            if r.get("kind") == "worker":
                _wif(coord_dir / f"{r['id']}_report.md", (
                    f"# {r['name']} Report — {proj_name}\n\n"
                    "Append status entries as:\n\n"
                    "```\n"
                    "## [YYYY-MM-DD HH:MM] task-id\n"
                    "- status: completed | in_progress | blocked\n"
                    "- summary: ...\n"
                    "- changes: ...\n"
                    "- verified: ...\n"
                    "```\n"
                ))

        # 3. CLAUDE.md stub (skip if already present).
        claude_md = proj_dir / "CLAUDE.md"
        if not claude_md.exists():
            role_lines = "\n".join(
                f"- **{r['name']}** (`{r['id']}`, {r['kind']}) {r.get('icon','')}"
                for r in cleaned_roles
            )
            try:
                claude_md.write_text((
                    f"# {proj_name}\n\n"
                    f"**Type**: {ptype_disp}\n"
                    f"**Created**: {now}\n\n"
                    f"## Overview\n\n{overview or '(fill in)'}\n\n"
                    f"## Roles\n\n{role_lines}\n\n"
                    "## Coordination\n\n"
                    "- `coordination/peer_<role>.md` — per-role dispatch mailbox (26-04-27 directive)\n"
                    "- `coordination/blockers.md` — pending user decisions\n"
                    "- `coordination/{role}_report.md` — per-worker status\n"
                ), encoding="utf-8")
            except Exception:
                pass

        # 4. Spawn one tab per role (coordinator first so it becomes active).
        ordered = sorted(
            cleaned_roles,
            key=lambda r: (0 if r.get("kind") == "coordinator" else 1 if r.get("kind") == "cco" else 2),
        )
        spawned: list[dict] = []
        for r in ordered:
            tab = self._make_tab(project_path)
            icon = str(r.get("icon", "")).strip()
            tab.name = (icon + " " + r["name"]).strip() if icon else r["name"]
            tab.role = r["id"]
            spawned.append(self._tab_info(tab))
        if spawned:
            self._active = spawned[0]["id"]
        self._persist()

        # 5. Notify frontend so it can DOM-create the new tabs and switch.
        try:
            self._js(
                f"onWizardTabsSpawned({json.dumps(spawned)},"
                f"{json.dumps(self._active)},"
                f"{json.dumps(project_path)})"
            )
        except Exception:
            pass

        # 6. Optionally start Auto Coordination (persists running flag).
        auto_started = False
        if start_auto_coord:
            try:
                r = self._auto_coord.start()
                if isinstance(r, dict) and r.get("ok"):
                    self._config["auto_coord_running"] = True
                    _save_json(CONFIG_FILE, self._config)
                    auto_started = True
            except Exception:
                pass

        coord_tab_id = spawned[0]["id"] if spawned else ""
        return {
            "ok": True,
            "project": project_path,
            "tabs": spawned,
            "coordinator_tab_id": coord_tab_id,
            "auto_coord_started": auto_started,
        }

    # -- Auto Coordination API ------------------------------------------------

    def start_auto_coord(self) -> dict:
        """Start autonomous multi-role coordination loop."""
        r = self._auto_coord.start()
        # Persist running state so a restart can resume coordination.
        if isinstance(r, dict) and r.get("ok"):
            self._config["auto_coord_running"] = True
            _save_json(CONFIG_FILE, self._config)
        return r

    def stop_auto_coord(self) -> dict:
        """Stop autonomous coordination."""
        r = self._auto_coord.stop()
        self._config["auto_coord_running"] = False
        _save_json(CONFIG_FILE, self._config)
        return r

    def get_auto_coord_status(self) -> dict:
        """Return coordination status + event log."""
        return self._auto_coord.status()

    def coord_inject(self, instruction: str) -> dict:
        """Send high-level instruction to Manager for classification & dispatch."""
        return self._auto_coord.inject(instruction)

    def get_coordination_state(self, tab_id: str = "") -> dict:
        """Return coordination/ snapshot for the tab's project. Used for dashboard display."""
        t = self._tabs.get(tab_id or self._active)
        if not t or not t.project_path:
            return {"ok": False, "reason": "no-project"}
        coord = Path(t.project_path) / "coordination"
        if not coord.is_dir():
            return {"ok": False, "reason": "no-coordination"}

        def _safe_read(p: Path, limit: int = 8000) -> str:
            try:
                if p.exists():
                    return p.read_text("utf-8", errors="replace")[:limit]
            except Exception:
                pass
            return ""

        status_raw = _safe_read(coord / "deploy_status.json", 4000)
        try:
            status = json.loads(status_raw) if status_raw else {}
        except Exception:
            status = {}

        # Legacy queue removed — queue_raw retained as empty string for
        # downstream pending/done counters (always 0).
        queue_raw = ""
        blockers_raw = _safe_read(coord / "blockers.md", 6000)
        reports: dict[str, str] = {}
        for role in self._roles_for(t.project_path).workers():
            tail = _safe_read(coord / f"{role}_report.md", 6000)
            if tail:
                reports[role] = "\n".join(tail.splitlines()[-20:])

        pending = sum(1 for ln in queue_raw.splitlines() if ln.strip().startswith("- [ ]"))
        done = sum(1 for ln in queue_raw.splitlines() if ln.strip().startswith("- [x]"))
        blocker_entries = [
            ln for ln in blockers_raw.splitlines()
            if ln.startswith("## [") and "blocker_title" not in ln
        ]

        return {
            "ok": True,
            "project": t.project_path,
            "deploy_status": status,
            "queue": {"pending": pending, "done": done},
            "blockers": {"count": len(blocker_entries), "preview": blockers_raw[:800]},
            "reports": reports,  # {role_id: tail_text}
            # Backward-compat aliases for existing UI consumers.
            "developer_report_tail": reports.get("developer", ""),
            "security_report_tail": reports.get("security", ""),
        }

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

    def set_coord_collapsed(self, on: bool) -> dict:
        self._config["coord_collapsed"] = bool(on)
        _save_json(CONFIG_FILE, self._config)
        return {"ok": True}

    # -- Chat — clear display vs new session -----------------------------------

    def clear_display(self, tab_id: str):
        """Clear UI messages only. Session (context) is preserved."""
        t = self._tabs.get(tab_id)
        if t:
            t.messages = []
            self._persist()
            self._js(f"onDisplayCleared({json.dumps(tab_id)})")

    def end_session(self, tab_id: str):
        """Stop PTY only. Keep history and session_id for resume."""
        t = self._tabs.get(tab_id)
        if t:
            if t.pty_session:
                t.pty_session.kill()
                t.pty_session = None
            self._persist()
            self._js(f"onSessionEnded({json.dumps(tab_id)})")

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
            self._js(f"onSessionReset({json.dumps(tab_id)})")

    def save_screen_content(self, tab_id: str, content: str):
        """Save screen content from JS on window close."""
        tab = self._tabs.get(tab_id)
        if tab and content:
            tab.screen_content = content[-20000:]

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
            self._js(f"onSystemMsg({json.dumps(tid)},'info','Display cleared. Session context preserved via --resume.')")
        elif command == "/effort":
            levels = {"max", "xhigh", "high", "medium", "low"}
            if arg.lower() in levels:
                tab.effort = arg.lower()
                self._persist()
                self._js(f"onSystemMsg({json.dumps(tid)},'info',{json.dumps(f'Effort: {tab.effort}')})")
                self._js(f"document.getElementById('selEffort').value={json.dumps(tab.effort)}")
            else:
                self._js(f"onSystemMsg({json.dumps(tid)},'info','Usage: /effort max|xhigh|high|medium|low')")
        elif command == "/model":
            def _norm_model(s: str) -> str:
                return s.lower().replace(" ", "").replace("-", "").replace(".", "")
            # Friendly aliases; bare family names resolve to the newest of each.
            aliases = {
                "opus": "claude-opus-5", "opus5": "claude-opus-5",
                "fable": "claude-fable-5", "fable5": "claude-fable-5",
                "sonnet": "claude-sonnet-5", "sonnet5": "claude-sonnet-5",
                "opus4.8": "claude-opus-4-8",
                "opus4.7": "claude-opus-4-7", "opus4.6": "claude-opus-4-6",
                "sonnet4.6": "claude-sonnet-4-6",
                "opus4": "claude-opus-4-20250514", "sonnet4": "claude-sonnet-4-20250514",
                "sonnet3.7": "claude-3-7-sonnet-20250219",
                "haiku": "claude-haiku-4-5-20251001", "haiku4.5": "claude-haiku-4-5-20251001",
                "opus3": "claude-3-opus-20240229",
            }
            lookup = {_norm_model(k): v for k, v in aliases.items()}
            # Also accept exact model ids and display labels (e.g. "Opus 4.8").
            for _mid, _label in MODELS:
                lookup[_norm_model(_mid)] = _mid
                lookup[_norm_model(_label)] = _mid
            target = lookup.get(_norm_model(arg))
            if target:
                tab.model = target
                self._persist()
                label = next((m[1] for m in MODELS if m[0] == target), target)
                self._js(f"onSystemMsg({json.dumps(tid)},'info',{json.dumps(f'Model: {label}')})")
                self._js(f"document.getElementById('selModel').value={json.dumps(target)}")
            else:
                self._js(f"onSystemMsg({json.dumps(tid)},'info','Usage: /model opus|fable|sonnet|haiku|opus5|fable5|sonnet5|opus4.8|opus4.7')")
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
                "/effort max|xhigh|high|medium|low — Change effort",
                "/model opus|sonnet|haiku|opus4.8|opus4.7|opus4.6 — Change model",
                "/status — Session info",
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
        # PTY mode: send choice directly to terminal. The CLI prompt is a
        # numbered select menu — "n" is not a valid key; deny = Escape (cancel).
        if tab.pty_session:
            tab.pty_session.write((choice + "\r") if approved else "\x1b")
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
                self._js(f"showTerminal({json.dumps(tid)})")
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
                    # Reload the tab's role identity on restart (parity with the
                    # auto-start path) — a role-assigned worker would otherwise
                    # come back up without its /role loaded. No-op for role-less
                    # tabs (returns immediately).
                    if getattr(tab, "role", ""):
                        self._send_role_with_retry(tab)
                except Exception:
                    pass
            tab.pty_session.write_submit(message)
        except FileNotFoundError:
            self._js(f"onStreamError('{tid}',{json.dumps(self._t('cli_not_found'))})")
        except Exception as e:
            self._js(f"onStreamError('{tid}',{json.dumps(str(e))})")

    def _build_cmd(self, tab: ChatTab, message: str) -> list:
        #: validate exec inputs (shared SoT). Fail = empty cmd → caller skips spawn.
        if not _validate_cmd_fields(tab):
            return []
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
        #: shared validator (drop shell=True, list args + CREATE_NEW_CONSOLE)
        if not _validate_cmd_fields(tab):
            self._js(f"onStreamEnd({json.dumps(tab_id)})")
            return
        cwd = os.path.abspath(tab.project_path) if tab.project_path else None
        # Build interactive command (no -p, no stream-json)
        cmd_parts = ["claude", "--model", tab.model]
        if tab.session_id:
            cmd_parts += ["--resume", tab.session_id]
        if cwd:
            cmd_parts += ["--add-dir", cwd]
        # Open in new visible console window — list args + shell=False, no f-string interpolation
        subprocess.Popen(
            cmd_parts,
            cwd=cwd,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        self._js(f"onStreamEnd({json.dumps(tab_id)})")

    def _send_cli(self, tab: ChatTab, message: str):
        tid = tab.id
        try:
            self._js(f"onStreamStart({json.dumps(tid)})")
            cmd = self._build_cmd(tab, message)
            if not cmd: #: validation failed in _build_cmd → graceful abort
                self._js(f"onStreamEnd({json.dumps(tid)})")
                return
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
            self._js(f"onStreamEnd({json.dumps(tid)})")
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
            # Filter dispatch-kind entries (peer/coord bubbles) — they have
            # role="dispatch" which is not a valid Anthropic API role and
            # would 400 the request. See _render_dispatched for context.
            msgs = [
                {"role": m["role"], "content": m["content"]}
                for m in tab.messages[-10:]
                if m.get("role") in ("user", "assistant")
            ]
            # The Messages API requires the first message to be 'user'. After
            # filtering out dispatch bubbles, the [-10:] window can begin with
            # an 'assistant' turn → 400. Drop any leading non-user entries.
            while msgs and msgs[0]["role"] != "user":
                msgs.pop(0)
            msgs.append({"role": "user", "content": message})
            self._js(f"onStreamStart({json.dumps(tid)})")
            full = ""
            #: prompt cache. The system prompt + project tree
            # is large and stable across the chat session — wrap with a 1h
            # TTL block. The conversation history (msgs[:-1]) gets a 5m TTL
            # boundary marker on its trailing message. Helpers no-op when
            # PROMPT_CACHE_ENABLED=False so this is a single-flag rollback.
            cached_system = build_cached_system_block(sys_p, ttl=PROMPT_CACHE_SYSTEM_TTL)
            cached_msgs = attach_history_cache_marker(msgs)
            with client.messages.stream(
                model=tab.model, max_tokens=8192,
                system=cached_system, messages=cached_msgs,
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
            self._js(f"onStreamEnd({json.dumps(tid)})")
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
            self._js(f"onStreamEnd({json.dumps(tab_id)})")

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

    def _render_dispatched(self, tab, prefix: str, body: str, source: str) -> None:
        """Render a cross-tab dispatch (peer / AutoCoordinator) onto the
        receiving tab's chat pane.

        This is called on every PTY write that originates from another tab so
        the receiver's chat log shows the incoming instruction that triggered
        Claude's next response. Without this, review of past conversations
        leaves an orphan assistant bubble with no visible cause.

        The bubble is also appended to `tab.messages` (role: "dispatch") so
        tab restore replays it. `prefix` is the PTY-write header line
        (e.g. "[MANAGER-DISPATCH to <role>]") and
        `source` is one of:
          peer_tab / peer_inbox / coord_inject / coord_report / coord_dispatch
          / file_dispatch / file_report
        (used for icon + colour + i18n label selection in JS).
        """
        if not tab:
            return
        try:
            tab.messages.append({
                "role": "dispatch",
                "content": body or "",
                "prefix": prefix or "",
                "source": source or "coord_dispatch",
                "ts": time.time(),
            })
            # Cap to avoid unbounded growth of tab.messages; serialize also
            # truncates on save, but the in-memory list is unbounded otherwise.
            if len(tab.messages) > 150:
                tab.messages = tab.messages[-50:]
        except Exception:
            pass
        try:
            self._js(
                "renderDispatchedBubble("
                f"{json.dumps(tab.id)},"
                f"{json.dumps(prefix or '')},"
                f"{json.dumps(body or '')},"
                f"{json.dumps(source or 'coord_dispatch')},true)"
            )
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
.new-proj-bar{padding:8px 12px 4px;border-bottom:1px solid var(--bd)}
.new-proj-btn{width:100%;padding:9px 10px;background:linear-gradient(135deg,var(--ac),var(--acl));border:none;border-radius:8px;color:#fff;cursor:pointer;text-align:center;font-size:12px;font-weight:600;transition:all .2s;box-shadow:0 1px 3px rgba(0,0,0,0.18)}
.new-proj-btn:hover{filter:brightness(1.1);transform:translateY(-1px);box-shadow:0 3px 8px rgba(0,0,0,0.25)}
.new-proj-btn:active{transform:translateY(0);filter:brightness(0.95)}
.proj-bar{padding:8px 12px;border-bottom:1px solid var(--bd)}
.harness-bar{display:flex;gap:4px;padding:6px 12px;border-bottom:1px solid var(--bd);background:var(--sf2)}
.hb-btn{flex:1;padding:5px 6px;background:var(--bg);border:1px solid var(--bd);border-radius:4px;color:var(--txd);font-size:10px;cursor:pointer;transition:all .15s;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.hb-btn:hover{color:var(--tx);border-color:var(--ac)}
.hb-btn:disabled{opacity:.4;cursor:not-allowed}
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
.tabs{display:flex;flex-wrap:wrap;align-items:center;background:var(--bg);border-bottom:1px solid var(--bd);min-height:30px;padding:0 4px}
.tabs::-webkit-scrollbar{height:0}
.tab{display:flex;align-items:center;gap:4px;padding:5px 10px;font-size:11px;color:var(--txd);cursor:pointer;border-bottom:2px solid transparent;white-space:nowrap;flex:0 0 auto;min-width:110px;transition:all .15s}
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
.ia{padding:10px 16px;border-top:1px solid var(--bd);background:var(--sf);position:relative;flex-shrink:0}
.ir{display:flex;gap:6px}
.ir textarea{flex:1;background:var(--bg);border:1px solid var(--bd);border-radius:8px;padding:9px 12px;color:var(--tx);font-family:var(--fn);font-size:13px;resize:none;min-height:40px;max-height:180px;outline:none;transition:border .2s;line-height:1.5}
.ir textarea:focus{border-color:var(--ac)}.ir textarea::placeholder{color:var(--txd)}
.snd{background:var(--ac);border:none;border-radius:8px;color:#fff;width:40px;cursor:pointer;font-size:16px;transition:all .15s;display:flex;align-items:center;justify-content:center}
.snd:hover{filter:brightness(1.1)}.snd:disabled{opacity:.3;cursor:not-allowed}.snd.stop{background:var(--err)}
.ia-nav{display:flex;gap:4px;padding:4px 0 0;flex-wrap:wrap;align-items:center;flex-shrink:0}
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
.vw-head{padding:8px 14px;border-bottom:1px solid var(--bd);display:flex;align-items:center;justify-content:space-between;gap:10px}
.vw-head .fn{font-size:12px;font-weight:600;color:var(--acl);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
.vw-head .right{display:flex;align-items:center;gap:10px;flex:none}
.vw-head .hint{font-size:10px;color:var(--txd);opacity:.75;letter-spacing:.02em}
.vw-head button{background:var(--bg);border:1px solid var(--bd);border-radius:4px;color:var(--tx);cursor:pointer;font-size:13px;padding:3px 10px;line-height:1.4;transition:background .15s,color .15s,border-color .15s}
.vw-head button:hover{background:var(--ac);color:#fff;border-color:var(--ac)}
.vw-head button:focus{outline:none;border-color:var(--ac);box-shadow:0 0 0 2px var(--ac)}
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
/* ===== Autonomous system ===== */
.blocker-bar{display:none;align-items:center;gap:10px;padding:8px 14px;background:#2a0a0a;border-bottom:1px solid #7a1f1f;color:#fecaca;font-size:12px;animation:blink-red 1.2s infinite;z-index:80;flex-shrink:0}
.blocker-bar b{color:#fff}
.blocker-bar .blocker-root{color:#fca5a5;font-family:var(--mono);font-size:10px;margin-left:auto}
.blocker-bar .blocker-open{background:#7a1f1f;border:1px solid #b91c1c;color:#fff;padding:3px 10px;border-radius:5px;font-size:11px;cursor:pointer}
.blocker-bar .blocker-open:hover{background:#b91c1c}
@keyframes blink-red{0%,100%{background:#2a0a0a}50%{background:#3d0f0f}}
.coord-panel{padding:8px 12px;border-top:1px solid var(--bd);background:rgba(99,102,241,.04);font-size:10px;color:var(--txd);line-height:1.7}
.coord-panel b{color:var(--acl);font-weight:600}
.coord-panel-head{display:flex;align-items:center;justify-content:space-between;gap:4px;font-size:9px;color:var(--acl);font-weight:700;margin-bottom:4px;text-transform:uppercase;letter-spacing:.6px}
.coord-panel-head button{background:transparent;border:1px solid var(--bd);border-radius:3px;color:var(--txd);cursor:pointer;font-size:9px;padding:1px 4px}
.coord-panel-head button:hover{border-color:var(--ac);color:var(--tx)}
.coord-panel.collapsed > *:not(.coord-panel-head){display:none !important}
.coord-panel.collapsed{padding-bottom:4px}
.cp-fold{display:inline-block;transition:transform .15s;font-size:10px;color:var(--txd);margin-right:2px}
.coord-panel.collapsed .cp-fold{transform:rotate(-90deg)}
/* Auto-coordination */
.coord-roles{margin:4px 0;font-size:9px;line-height:1.6}
.coord-roles .cr-row{display:flex;align-items:center;gap:4px}
.coord-roles .cr-dot{width:6px;height:6px;border-radius:50%;flex-shrink:0}
.cr-dot.manager{background:#a78bfa}.cr-dot.developer{background:#34d399}.cr-dot.marketing{background:#fb923c}.cr-dot.security{background:#f87171}
.coord-ctrl{display:flex;gap:4px;margin:6px 0}
.coord-ctrl button{flex:1;padding:4px 6px;font-size:9px;border-radius:4px;cursor:pointer;border:1px solid var(--bd);background:transparent;color:var(--txd);transition:all .15s}
.coord-ctrl button:hover{border-color:var(--ac);color:var(--tx)}
.coord-ctrl .cc-start{border-color:#34d399;color:#34d399}.coord-ctrl .cc-start:hover{background:rgba(52,211,153,.12)}
.coord-ctrl .cc-stop{border-color:#ef4444;color:#ef4444}.coord-ctrl .cc-stop:hover{background:rgba(239,68,68,.12)}
.coord-ctrl .cc-inject{border-color:#fbbf24;color:#fbbf24}.coord-ctrl .cc-inject:hover{background:rgba(251,191,36,.12)}
.coord-log{max-height:130px;overflow-y:auto;margin-top:4px;font-family:var(--mono);font-size:8px;line-height:1.5;scrollbar-width:thin}
.coord-log div{padding:1px 0;border-bottom:1px solid rgba(99,102,241,.06)}
.coord-log .ev-report{color:#34d399}.coord-log .ev-dispatch{color:#fbbf24}.coord-log .ev-inject{color:#818cf8}
.coord-log .ev-start,.coord-log .ev-stop{color:#94a3b8;font-weight:600}.coord-log .ev-error,.coord-log .ev-skip{color:#f87171}
.coord-running{animation:coord-pulse 2s infinite}
@keyframes coord-pulse{0%,100%{opacity:1}50%{opacity:.5}}
/* Yolo button in each tab */
.yolo-btn{display:inline-block;font-size:9px;padding:1px 4px;border-radius:3px;cursor:pointer;margin-left:4px;border:1px solid transparent;transition:all .15s;user-select:none;font-family:var(--mono);line-height:1.4}
.yolo-btn.off{color:#555;border-color:transparent}
.yolo-btn.off:hover{color:#999;border-color:#30304a}
.yolo-btn.on{color:#fbbf24;border-color:#fbbf24;background:rgba(251,191,36,.12);animation:yolo-pulse 2.2s infinite}
@keyframes yolo-pulse{0%,100%{box-shadow:0 0 0 0 rgba(251,191,36,.35)}50%{box-shadow:0 0 0 3px rgba(251,191,36,0)}}
.yolo-flash{background:rgba(251,191,36,.3)!important;transition:background .4s}
.msg-yolo-skip{color:#9a7a2e;font-size:11px;font-style:italic;padding:2px 8px;opacity:.75}
/* Dispatched bubble — for peer/coord cross-tab messages rendered on receiver side */
.msg.dispatch{max-width:92%;margin-left:auto;margin-right:auto}
.msg.dispatch .bub.disp-bub{background:rgba(99,102,241,.08);border:1px dashed rgba(99,102,241,.45);border-radius:10px;padding:8px 12px;color:var(--tx)}
.msg.dispatch.src-peer_tab .bub.disp-bub{border-color:rgba(251,146,60,.55);background:rgba(251,146,60,.06)}
.msg.dispatch.src-peer_inbox .bub.disp-bub{border-color:rgba(52,211,153,.55);background:rgba(52,211,153,.06)}
.msg.dispatch.src-coord_inject .bub.disp-bub{border-color:rgba(129,140,248,.6);background:rgba(129,140,248,.08)}
.msg.dispatch.src-coord_report .bub.disp-bub,
.msg.dispatch.src-file_report .bub.disp-bub{border-color:rgba(52,211,153,.55);background:rgba(52,211,153,.06)}
.msg.dispatch.src-coord_dispatch .bub.disp-bub,
.msg.dispatch.src-file_dispatch .bub.disp-bub{border-color:rgba(251,191,36,.6);background:rgba(251,191,36,.06)}
.disp-head{display:flex;align-items:center;gap:6px;font-size:10px;text-transform:uppercase;letter-spacing:.5px;color:var(--acl);font-weight:600;margin-bottom:6px;padding-bottom:4px;border-bottom:1px solid rgba(99,102,241,.18)}
.disp-icon{font-size:14px;line-height:1;flex-shrink:0}
.disp-label{flex-shrink:0}
.disp-prefix{color:var(--txd);font-weight:400;font-size:9px;text-transform:none;letter-spacing:0;opacity:.85;margin-left:auto;font-family:var(--mono);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:55%}
.disp-body{font-size:12px;line-height:1.55;color:var(--tx);font-family:var(--mono);white-space:pre-wrap;word-break:break-word;max-height:320px;overflow-y:auto}
.disp-body.full{max-height:none}
.disp-expand{background:transparent;border:1px solid var(--bd);border-radius:4px;color:var(--acl);font-size:10px;padding:2px 8px;cursor:pointer;margin-top:6px;transition:all .15s}
.disp-expand:hover{border-color:var(--ac);background:rgba(99,102,241,.1)}
.disp-note{color:var(--txd);font-size:10px;margin-top:4px;opacity:.7;font-style:italic}
/* Role badge */
.role-badge{display:inline-block;font-size:9px;padding:1px 5px;border-radius:3px;margin-left:3px;background:transparent;border:1px solid transparent;color:#555;cursor:pointer;user-select:none;line-height:1.4}
.role-badge:hover{border-color:var(--bd);color:var(--txd)}
.role-badge.r-none{color:#555}
.role-badge.r-manager{border-color:#a78bfa;color:#c4b5fd;background:rgba(139,92,246,.12)}
.role-badge.r-developer{border-color:#34d399;color:#6ee7b7;background:rgba(16,185,129,.12)}
.role-badge.r-marketing{border-color:#fb923c;color:#fdba74;background:rgba(251,146,60,.12)}
.role-badge.r-security{border-color:#f87171;color:#fca5a5;background:rgba(239,68,68,.12)}
.role-badge.r-cco{box-shadow:0 0 0 1px rgba(253,224,71,.35) inset;font-weight:600}
.role-menu{position:fixed;background:var(--sf);border:1px solid var(--bd);border-radius:6px;padding:4px;z-index:100;min-width:150px;box-shadow:0 4px 16px rgba(0,0,0,.5)}
.role-menu div{padding:6px 10px;font-size:11px;cursor:pointer;border-radius:3px;display:flex;gap:6px;align-items:center}
.role-menu div:hover{background:rgba(99,102,241,.18)}
.roles-list{max-height:48vh;overflow-y:auto;border:1px solid var(--bd);border-radius:6px;padding:4px;background:var(--bg)}
.rle-row{display:grid;grid-template-columns:28px 1fr 1.6fr 72px 110px 28px;gap:6px;align-items:center;padding:6px 6px;border-bottom:1px solid var(--bd)}
.rle-row:last-child{border-bottom:none}
.rle-row input[type=text],.rle-row input[type=color],.rle-row select{font-size:11px;padding:4px 6px;background:var(--sf);border:1px solid var(--bd);border-radius:4px;color:var(--tx);width:100%;box-sizing:border-box;height:26px}
.rle-row input[type=color]{padding:0;cursor:pointer}
.rle-row .ri-icon{text-align:center;font-size:16px;padding:0;background:transparent;border:1px solid transparent}
.rle-row .ri-icon:focus{outline:1px solid var(--ac);border-color:var(--ac)}
.rle-row .ri-del{background:transparent;border:1px solid transparent;color:var(--txd);font-size:14px;cursor:pointer;border-radius:4px;padding:2px 4px}
.rle-row .ri-del:hover{color:#f87171;border-color:#f87171}
.rle-row .ri-kind option[value=coordinator]{color:#a78bfa}
.rle-head{display:grid;grid-template-columns:28px 1fr 1.6fr 72px 110px 28px;gap:6px;font-size:9px;color:var(--txd);text-transform:uppercase;letter-spacing:.5px;padding:4px 6px;border-bottom:1px solid var(--bd)}
</style>
</head>
<body>

<!-- Blocker detail modal -->
<div class="mo" id="blockerModal"><div class="md" style="width:600px">
  <h3 style="color:#f87171" data-i18n="blockersTitle">Blockers</h3>
  <div id="blockerPath" style="font-size:10px;color:var(--txd);font-family:var(--mono);margin:4px 0 8px;word-break:break-all;opacity:.75"></div>
  <pre id="blockerContent" style="white-space:pre-wrap;word-break:break-word;font-size:12px;line-height:1.6;color:var(--tx);max-height:60vh;overflow-y:auto;background:var(--bg);border:1px solid var(--bd);border-radius:6px;padding:12px"></pre>
  <div class="br"><button class="bp" onclick="document.getElementById('blockerModal').classList.remove('open')" data-i18n="close">Close</button></div>
</div></div>

<!-- Global Settings Modal -->
<div class="mo" id="globalModal"><div class="md">
  <h3 data-i18n="globalSettings">Global Settings</h3>
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
  <h3 data-i18n="tabSettings">Tab Settings</h3>
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
  <label><span data-i18n="peerTabLabel">Peer tabs</span> <span style="color:var(--txd);font-size:10px">(<span data-i18n="peerTabsHintShort">for @@name: dispatch</span>)</span></label>
  <div style="display:flex;align-items:center;gap:8px;margin-top:4px">
    <div id="tsPeerTabsSummary" style="flex:1;font-size:11px;color:var(--txd);padding:6px 8px;background:var(--bg);border:1px solid var(--bd);border-radius:4px" data-i18n="peerTabsNone">None</div>
    <button class="bg" onclick="openPeerTabs(_tsCurId)" style="font-size:11px" data-i18n="edit">Edit</button>
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
  <div class="vw-head"><span class="fn" id="vwName"></span><div class="right"><span class="hint" data-i18n="closeHint">ESC or &#x2715;</span><button onclick="closeVw()" data-i18n-title="close" title="Close">&#x2715;</button></div></div>
  <div class="vw-body"><pre id="vwContent"></pre></div>
</div>

<!-- Permission Dialog -->
<div class="mo" id="permModal"><div class="md">
  <h3 data-i18n="permTitle">Permission Required</h3>
  <div style="margin:12px 0">
    <div style="display:flex;align-items:center;gap:8px">
      <span style="font-size:18px">&#x1F527;</span>
      <span style="font-weight:600;font-size:14px" id="permToolName" data-i18n="permToolLabel">Tool</span>
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

<!-- Role Editor Modal -->
<div class="mo" id="rolesModal"><div class="md" style="width:680px;max-width:92vw">
  <h3><span data-i18n="roles">Roles</span> <span id="rolesScope" style="font-size:11px;color:var(--txd);font-weight:normal"></span></h3>
  <div class="nt" style="margin-bottom:8px" data-i18n-html="rolesHintHtml">Roles are <b>per-project</b>. Coordinators dispatch to workers via <code>@role:</code>; CCO sits above coordinators for multi-team integration. IDs must be <code>a-z0-9_</code>. At least one coordinator and one worker are required.</div>
  <div id="rolesList" class="roles-list"></div>
  <div style="margin-top:10px;display:flex;gap:8px">
    <button class="bg" onclick="addRoleRow()" data-i18n="addRole">+ Add role</button>
    <button class="bg" onclick="resetRoles()" data-i18n="resetDefaults">Reset to defaults</button>
  </div>
  <div class="br" style="margin-top:12px">
    <button class="bg" onclick="closeRolesEditor()" data-i18n="cancel">Cancel</button>
    <button class="bp" onclick="saveRolesEditor()" data-i18n="save">Save</button>
  </div>
</div></div>

<!-- Role Proposal Modal (E) — shown when Claude writes roles_proposal.json -->
<div class="mo" id="roleProposalModal"><div class="md" style="width:680px;max-width:92vw">
  <h3>&#x1F9E0; <span data-i18n="roleProposedBy">Role proposal</span> <span id="ropScope" style="font-size:11px;color:var(--txd);font-weight:normal"></span></h3>
  <div class="nt" style="margin-bottom:8px" data-i18n="roleProposalHint">Claude analysed the project and proposed the following role set. Review and apply to replace the current roles, or dismiss.</div>
  <div id="ropList" class="roles-list" style="pointer-events:none"></div>
  <div class="br" style="margin-top:12px">
    <button class="bg" onclick="dismissRoleProposal()" data-i18n="dismiss">Dismiss</button>
    <button class="bp" onclick="applyRoleProposal()" data-i18n="apply">Apply</button>
  </div>
</div></div>

<!-- Role Request Modal (F) — shown when Manager writes role_requests.md -->
<div class="mo" id="roleRequestModal"><div class="md" style="width:560px;max-width:92vw">
  <h3>&#x1F195; <span data-i18n="roleReqTitle">New role requested by Manager</span></h3>
  <div class="nt" style="margin-bottom:8px" data-i18n="roleReqHint">The Manager wants to spawn a new team. Approving will create the directory, register the role, and open a dedicated tab.</div>
  <div id="rreqFields" style="display:grid;grid-template-columns:120px 1fr;gap:6px 10px;font-size:12px;margin-bottom:10px"></div>
  <div class="br" style="margin-top:12px">
    <button class="bg" onclick="dismissRoleRequest()" data-i18n="dismiss">Dismiss</button>
    <button class="bp" onclick="applyRoleRequest()" data-i18n="approveCreate">Approve &amp; Create</button>
  </div>
</div></div>

<!-- Cross-project Peers Modal (B) -->
<div class="mo" id="projectPeersModal"><div class="md" style="width:640px;max-width:92vw">
  <h3>&#x1F517; <span data-i18n="crossPeerTitle">Cross-project peers</span> <span id="ppScope" style="font-size:11px;color:var(--txd);font-weight:normal"></span></h3>
  <div class="nt" style="margin-bottom:8px" data-i18n-html="crossPeerHintHtml">Register other projects you want to send <code>@alias.role:</code> messages to. Messages are appended to <code>&lt;target&gt;/.claude-harness/inbox/from_&lt;self&gt;.md</code> and forwarded to the role's tab if it is running.</div>
  <div style="display:grid;grid-template-columns:120px 1fr;gap:6px 10px;font-size:12px;margin-bottom:10px">
    <label style="align-self:center" data-i18n="myAlias">My alias</label>
    <input type="text" id="pp_selfAlias" data-i18n-ph="aliasPlaceholder" placeholder="(auto from directory name)" style="padding:4px 6px;background:var(--bg);border:1px solid var(--bd);border-radius:4px;color:var(--tx)" />
  </div>
  <div id="ppList" class="roles-list"></div>
  <div style="margin-top:10px;display:flex;gap:8px">
    <button class="bg" onclick="ppAddRow()" data-i18n="peerAdd">+ Add peer</button>
  </div>
  <div class="br" style="margin-top:12px">
    <button class="bg" onclick="closeProjectPeers()" data-i18n="cancel">Cancel</button>
    <button class="bp" onclick="saveProjectPeers()" data-i18n="save">Save</button>
  </div>
</div></div>

<!-- In-GUI Peer Tabs Modal (A) -->
<div class="mo" id="peerTabsModal"><div class="md" style="width:520px;max-width:92vw">
  <h3>&#x1F5B3; <span data-i18n="inGuiPeerTabs">In-GUI peer tabs</span> <span id="ptScope" style="font-size:11px;color:var(--txd);font-weight:normal"></span></h3>
  <div class="nt" style="margin-bottom:8px" data-i18n-html="inGuiPeerTabsHintHtml">Select tabs that <b>this</b> tab can address with <code>@@&lt;tab_name&gt;:</code>. The body is written directly into the peer tab's PTY.</div>
  <div id="ptList" style="display:flex;flex-direction:column;gap:4px;max-height:300px;overflow-y:auto"></div>
  <div class="br" style="margin-top:12px">
    <button class="bg" onclick="closePeerTabs()" data-i18n="cancel">Cancel</button>
    <button class="bp" onclick="savePeerTabs()" data-i18n="save">Save</button>
  </div>
</div></div>

<!-- New Project Wizard Modal (2026-04-24) -->
<div class="mo" id="wizardModal"><div class="md" style="width:720px;max-width:94vw">
  <h3>&#x1F680; <span data-i18n="wzTitle">New Project</span> <span id="wzStage" style="font-size:11px;color:var(--txd);font-weight:normal"></span></h3>

  <!-- Stage 1: Input -->
  <div id="wzStage1">
    <div class="nt" style="margin-bottom:10px" data-i18n="wzHint1">Enter a brief overview. We'll propose the optimal role team.</div>
    <div style="display:grid;grid-template-columns:130px 1fr;gap:8px 10px;font-size:12px;margin-bottom:10px">
      <label style="align-self:center" data-i18n="wzType">Type</label>
      <select id="wzTypeSel" style="padding:5px 6px;background:var(--bg);border:1px solid var(--bd);border-radius:4px;color:var(--tx)">
        <option value="saas" data-i18n="wzTypeSaaS">SaaS / Product</option>
        <option value="bot" data-i18n="wzTypeBot">Bot / Automation</option>
        <option value="content" data-i18n="wzTypeContent">Content / Media</option>
        <option value="research" data-i18n="wzTypeResearch">Research / Analysis</option>
        <option value="other" data-i18n="wzTypeOther">Other</option>
      </select>
      <label style="align-self:start;padding-top:4px" data-i18n="wzOverview">Overview</label>
      <textarea id="wzOverview" data-i18n-ph="wzOverviewPH" placeholder="Brief description (1-3 lines)..." rows="3" style="padding:5px 6px;background:var(--bg);border:1px solid var(--bd);border-radius:4px;color:var(--tx);font-family:inherit;resize:vertical"></textarea>
      <label style="align-self:center" data-i18n="wzPath">Path</label>
      <div style="display:flex;gap:4px">
        <input type="text" id="wzPathInp" data-i18n-ph="wzPathPH" placeholder="/absolute/path/to/new/project" style="flex:1;padding:5px 6px;background:var(--bg);border:1px solid var(--bd);border-radius:4px;color:var(--tx)" />
        <button class="bg" onclick="wzPickFolder()" data-i18n="wzChooseFolder">Browse...</button>
      </div>
    </div>
    <div class="nt" style="margin-top:4px;font-size:10px;color:var(--txd)" data-i18n="wzPathNote">Directory will be created if it doesn't exist.</div>
    <div class="br" style="margin-top:12px">
      <button class="bg" onclick="closeWizard()" data-i18n="cancel">Cancel</button>
      <button class="bp" id="wzProposeBtn" onclick="wzPropose()" data-i18n="wzPropose">Propose roles</button>
    </div>
  </div>

  <!-- Stage 2: Review -->
  <div id="wzStage2" style="display:none">
    <div class="nt" style="margin-bottom:8px"><span id="wzReviewSrc" style="font-weight:600"></span> <span data-i18n="wzReviewHint">Review the proposed roles. Edit if needed, then create the project.</span></div>
    <div id="wzReviewList" class="roles-list"></div>
    <div style="margin-top:10px;display:flex;gap:8px;align-items:center">
      <button class="bg" onclick="wzAddRoleRow()" data-i18n="addRole">+ Add role</button>
      <label style="margin-left:auto;display:flex;align-items:center;gap:6px;font-size:11px;color:var(--txd);cursor:pointer">
        <input type="checkbox" id="wzStartCoord" checked /> <span data-i18n="wzAutoStartCoord">Start Auto Coordination</span>
      </label>
    </div>
    <div class="br" style="margin-top:12px">
      <button class="bg" onclick="wzBackToStage1()" data-i18n="back">Back</button>
      <button class="bp" id="wzCreateBtn" onclick="wzCreate()" data-i18n="wzCreate">Create</button>
    </div>
  </div>
</div></div>

<!-- App -->
<div class="app">
  <div class="sb">
    <div class="sb-head"><h1>Claude Code GUI</h1><div class="sub" data-i18n="appSub">Multi-tab AI Development</div></div>
    <div class="new-proj-bar">
      <button class="new-proj-btn" id="newProjBtn" onclick="openWizard()" data-i18n-title="wzTitle" title="New Project Wizard">&#x1F680; <span data-i18n="newProject">+ New Project</span></button>
    </div>
    <div class="proj-bar">
      <button class="proj-btn" id="projBtn" onclick="selectProject()">
        <div id="projLabel" data-i18n="selProj">Select project...</div>
        <div class="pp" id="projPath"></div>
      </button>
    </div>
    <div class="harness-bar" id="harnessBar">
      <button class="hb-btn" id="genRolesBtn" onclick="genRoles()" data-i18n-title="genRolesTitle" title="Analyse directory & propose role set">&#x1F9E0; <span data-i18n="genRolesBtn">Generate roles</span></button>
      <button class="hb-btn" id="prjPeersBtn" onclick="openProjectPeers()" data-i18n-title="peersBtnTitle" title="Register cross-project peers">&#x1F517; <span data-i18n="peersBtn">Peers</span></button>
      <button class="hb-btn" id="editRolesBtn" onclick="openRolesEditor()" data-i18n-title="rolesBtnTitle" title="Edit role set">&#x2699; <span data-i18n="rolesBtn">Roles</span></button>
    </div>
    <div class="ftree" id="fileTree"><div style="padding:14px;color:var(--txd);font-size:11px;text-align:center" data-i18n="treeHint">Select a project to see file tree</div></div>
    <div class="coord-panel" id="coordStatus">
      <div class="coord-panel-head" onclick="toggleCoordPanel(event)" style="cursor:pointer" title="Click to fold/unfold">
        <span><span class="cp-fold">&#x25BE;</span>&#x1F500; <span data-i18n="autoCoord">Auto Coordination</span></span>
        <span id="coordIndicator" style="font-size:8px;color:var(--txd)" data-i18n="coordOff">OFF</span>
        <button onclick="event.stopPropagation();refreshCoordination()" data-i18n-title="coordRefreshTitle" title="Refresh">&#x21BB;</button>
      </div>
      <div id="coordRoles" class="coord-roles"></div>
      <div class="coord-ctrl">
        <button class="cc-start" id="coordStartBtn" onclick="coordStart()">&#x25B6; <span data-i18n="start">Start</span></button>
        <button class="cc-stop" id="coordStopBtn" onclick="coordStop()" style="display:none">&#x25A0; <span data-i18n="stop">Stop</span></button>
        <button class="cc-inject" onclick="coordInjectPrompt()" data-i18n-title="coordInjectTitle" title="Send instruction to Manager">&#x2709; <span data-i18n="coordInject">Inject</span></button>
        <button class="cc-peer" onclick="openPeerChannel()" data-i18n-title="peerChannelTitle" title="Peer Channel">&#x1F4EE; <span data-i18n="peerChannel">Peer</span></button>
        <button class="cc-graph" onclick="openGraphPanel()" title="Graph memory (synapse-like activation)">&#x1F9E0; Graph</button>
        <button class="cc-graphvw" onclick="openGraphViewer()" title="Open visual graph viewer (cytoscape.js, browser)">&#x1F310; Viewer</button>
        <button class="cc-effort" onclick="applyRoleEfforts()" title="Migrate all tabs to their role default_effort (token-budget allocation)">&#x26A1; Effort</button>
      </div>
      <div id="coordBody" style="font-size:10px;color:var(--txd)"></div>
      <div id="coordLog" class="coord-log"></div>
      <div id="graphHeaderLine" style="font-size:9px;color:var(--txd);padding:2px 0;display:flex;align-items:center;gap:6px">
        <span>&#x1F9E0;</span><span id="graphHeaderStats">graph: --</span>
      </div>
      <div id="graphPanel" style="display:none;margin-top:8px;border-top:1px solid var(--bd);padding-top:6px;font-size:10px;line-height:1.5">
        <div class="coord-panel-head">
          <span>&#x1F9E0; Graph context</span>
          <span id="graphStatsLine" style="font-size:8px;color:var(--txd)">--</span>
          <button onclick="graphRefreshContext()" title="Refresh from active role">&#x21BB;</button>
          <button onclick="closeGraphPanel()" title="Close">&times;</button>
        </div>
        <div style="display:flex;gap:4px;margin-bottom:4px">
          <input id="graphSearchInput" placeholder="search..." style="flex:1;background:var(--bg);border:1px solid var(--bd);border-radius:3px;color:var(--tx);padding:2px 4px;font-size:10px" />
          <button onclick="graphSearchRun()" style="font-size:9px">Search</button>
        </div>
        <div id="graphResults" style="max-height:240px;overflow-y:auto"></div>
      </div>
    </div>
    <div class="sb-foot">
      <button onclick="refreshTree()" data-i18n="refresh">Refresh</button>
      <button onclick="doClearDisplay()" data-i18n="clearDisp">Clear</button>
      <button onclick="openGlobal()" data-i18n="settings">Settings</button>
    </div>
  </div>
  <div class="mn">
    <div id="blockerBar" class="blocker-bar"></div>
    <div class="topbar">
      <div class="dot" id="stDot"></div>
      <span id="stText" style="color:var(--txd)" data-i18n="starting">Starting...</span>
      <span class="pn" id="topProj"></span>
      <span class="sp"></span>
      <select id="selModel" onchange="chModel(this.value)" data-i18n-title="modelTitle" title="Model"></select>
      <select id="selEffort" onchange="chEffort(this.value)" data-i18n-title="effortTitle" title="Effort"></select>
      <select id="selLang2" onchange="chLang(this.value)" data-i18n-title="langTitle" title="Language" style="width:52px">
        <option value="en">EN</option><option value="ja">JA</option>
        <option value="zh">ZH</option><option value="ko">KO</option>
      </select>
      <button class="tb-btn" onclick="openTabModal()" data-i18n-title="tabSettingsTitle" title="Tab settings">&#9881;</button>
    </div>
    <div class="tabs" id="tabBar"><div class="tab-add" onclick="addTab()" data-i18n-title="newTabTitle" title="Ctrl+T">+</div></div>
    <div class="cc" id="chatContainer"></div>
    <div class="ia">
      <div class="ac-popup" id="acPopup"></div>
      <div class="ir">
        <textarea id="msgIn" data-i18n-ph="ph" placeholder="Ask Claude anything..." rows="1" onkeydown="hKey(event)" oninput="aResize(this);updateAc()"></textarea>
        <button class="snd" id="sndBtn" onclick="sndClick()" data-i18n-title="sendBtnTitle" title="Send">&#9654;</button>
      </div>
      <div class="ia-nav">
        <button class="nav-btn" onclick="doClearDisplay()" title="Ctrl+L"><span class="nav-ico">&#x239A;</span> <span data-i18n="btnClear">Clear</span> <kbd>Ctrl+L</kbd></button>
        <button class="nav-btn nav-warn" onclick="doEndSession()" title="Ctrl+Shift+L"><span class="nav-ico">&#x23F9;</span> <span data-i18n="btnEndSess">End Session</span> <kbd>Ctrl+Shift+L</kbd></button>
        <button class="nav-btn nav-danger" onclick="doNewSession()" title="Ctrl+Shift+N"><span class="nav-ico">&#x21BB;</span> <span data-i18n="btnNewSess">New Session</span> <kbd>Ctrl+Shift+N</kbd></button>
        <span class="nav-sep"></span>
        <button class="nav-btn nav-toggle" id="btnPlan" onclick="togglePermMode('plan')" data-i18n-title="planModeTitle" title="Plan (read-only)"><span class="nav-ico">&#x1F4D6;</span> <span data-i18n="permPlan">Plan</span></button>
        <button class="nav-btn nav-toggle" id="btnAcceptEdits" onclick="togglePermMode('acceptEdits')" data-i18n-title="acceptEditsTitle" title="Accept Edits (file ops only)"><span class="nav-ico">&#x270F;</span> <span data-i18n="permAcceptEdits">Accept Edits</span></button>
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
sessResumed:'Session resumed',
sessInvalid:'Previous session expired, starting new session',
sessRestartFail:'CLI failed to start repeatedly — stopped. Send a message to retry.',
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
sessResumed:'セッションを再開しました',
sessInvalid:'前回のセッションが期限切れのため、新規セッションを開始します',
sessRestartFail:'CLIの起動に繰り返し失敗したため停止しました。メッセージを送信すると再試行します。',
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
sessResumed:'会话已恢复',
sessInvalid:'上次会话已过期，正在开始新会话',
sessRestartFail:'CLI 多次启动失败，已停止。发送消息可重试。',
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
sessResumed:'세션이 복원되었습니다',
sessInvalid:'이전 세션이 만료되어 새 세션을 시작합니다',
sessRestartFail:'CLI 시작에 반복적으로 실패하여 중지했습니다. 메시지를 보내면 다시 시도합니다.',
permMode:'권한 모드',permDefault:'기본 (모든 도구)',permAcceptEdits:'Accept Edits (파일 작업만)',permPlan:'Plan (읽기 전용)',permCustom:'선택한 도구만',
permNoteDefault:'인터랙티브 — 도구 사용 시 GUI에서 승인 (PTY 모드)',permNoteAcceptEdits:'Read, Write, Edit, Glob, Grep 자동 승인',permNotePlan:'읽기 전용 — 쓰기 및 Bash 불가',permNoteCustom:'체크한 도구만 허용됩니다',
permTitle:'권한 요청',permAllow:'허용',permDeny:'거부',permToolLabel:'도구',
}
};

let curLang='en';
// Merge Python-injected I18N catalogue into LANGS so legacy keys and the
// new comprehensive Python keys live in one table. Python injects window.I18N
// as { key: { en, ja, zh, ko } }; LANGS is { lang: { key: str } }. We flip.
(function mergeI18n(){
  try{
    if(!window.I18N)return;
    const langs=['en','ja','zh','ko'];
    Object.keys(window.I18N).forEach(k=>{
      langs.forEach(L=>{
        if(!LANGS[L])LANGS[L]={};
        // Only set if not already present so locally-defined LANGS win
        // (preserves the original JS-only keys where they exist).
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
  // Text content: data-i18n="key" replaces element.textContent.
  document.querySelectorAll('[data-i18n]').forEach(el=>{const v=t(el.dataset.i18n);if(v)el.textContent=v;});
  // Placeholder: data-i18n-ph="key" sets input/textarea placeholder.
  document.querySelectorAll('[data-i18n-ph]').forEach(el=>{const v=t(el.dataset.i18nPh);if(v)el.placeholder=v;});
  // Title attr: data-i18n-title="key" sets the tooltip/title attribute.
  document.querySelectorAll('[data-i18n-title]').forEach(el=>{const v=t(el.dataset.i18nTitle);if(v)el.title=v;});
  // aria-label: data-i18n-aria="key" sets the aria-label for screen readers.
  document.querySelectorAll('[data-i18n-aria]').forEach(el=>{const v=t(el.dataset.i18nAria);if(v)el.setAttribute('aria-label',v);});
  // HTML content: data-i18n-html="key" replaces innerHTML (use sparingly,
  // only for strings whose translated form contains markup like <br>).
  document.querySelectorAll('[data-i18n-html]').forEach(el=>{const v=t(el.dataset.i18nHtml);if(v)el.innerHTML=v;});
}
// Public alias for the re-apply entry point. Callable from Python side as
// `api._js('applyI18n("ja")')` after a set_language() roundtrip.
function applyI18n(v){ if(v){ curLang=v; } updateUI(); }
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
  // Re-render dynamic role badges, yolo badges, and coord-panel labels
  // (these contain text generated from t() at creation time).
  try{
    for(const id of Object.keys(T)){
      const ti=T[id]||{};
      renderYoloBadge(id,!!ti.yolo);
      if(ti.role)renderRoleBadge(id,ti.role); else renderRoleBadge(id,'');
    }
    refreshCoordRoles();
    refreshCoordStatus();
  }catch(e){console.warn('chLang rerender',e);}
  await pywebview.api.set_language(v);
}

// ============================================================
// State
// ============================================================
const T={};let order=[];let act=null;let gHasPty=false;let gMode='cli';let _ROLES=[];

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
  _ROLES=Array.isArray(s.roles)?s.roles:[];
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
  // Auto-start PTY sessions for ALL tabs on restart — staggered 1s apart
  // to avoid simultaneous PTY spawn race (Windows conpty is sensitive).
  // Active tab starts first (0s) so it's visible immediately, others follow.
  // 2026-04-23 gui-session-continuity-fix: was only auto-starting active tab,
  // leaving background worker tabs dormant until user clicked them.
  if(s.cli_available&&s.mode==='cli'&&s.has_pty){
    const tabIds=order.slice();
    // Ensure active tab is first in staggered queue
    if(act){
      const i=tabIds.indexOf(act);
      if(i>0){tabIds.splice(i,1);tabIds.unshift(act);}
    }
    tabIds.forEach((tid,i)=>{
      setTimeout(()=>{
        try{pywebview.api.auto_start_session(tid);}catch(e){console.warn('[auto-start]',tid,e);}
      }, i*1000);
    });
  }
  // If Auto Coordination was running before restart, re-activate it after
  // giving PTYs time to spawn and /remote-control to land (10s is generous
  // but matches the existing welcome-detection window).
  if(s.auto_coord_running){
    setTimeout(async()=>{
      try{
        const r=await pywebview.api.start_auto_coord();
        if(r&&r.ok){onCoordStateChange(true);}
      }catch(e){}
    },10000);
  }
  if(s.coord_collapsed){
    const cp=document.getElementById('coordStatus');
    if(cp)cp.classList.add('collapsed');
  }
  // PTY resize on window resize — the handler exists server-side but wasn't
  // wired to the window event, so cols/rows stayed pinned at 120x36.
  let _rszT=null;
  window.addEventListener('resize',()=>{
    if(_rszT)clearTimeout(_rszT);
    _rszT=setTimeout(()=>{
      if(!act)return;
      // Measure the terminal screen element (fallback to reasonable
      // defaults if not in terminal view).
      const el=document.getElementById('screen_'+act);
      if(!el){return;}
      // pyte cols × monospace char width ≈ el.clientWidth; rows × line-height ≈ el.clientHeight.
      const st=window.getComputedStyle(el);
      const fs=parseFloat(st.fontSize)||13;
      const lh=parseFloat(st.lineHeight)||(fs*1.4);
      // Approx char width = 0.55 × fontSize for monospace fonts.
      const cw=Math.max(fs*0.55,6);
      const cols=Math.max(40,Math.min(240,Math.floor((el.clientWidth-16)/cw)));
      const rows=Math.max(10,Math.min(80,Math.floor((el.clientHeight-4)/lh)));
      try{pywebview.api.pty_resize(act,rows,cols);}catch(e){}
    },250);
  });
}

function setStatus(cls,txt){document.getElementById('stDot').className='dot '+cls;document.getElementById('stText').textContent=txt;}

// ============================================================
// Tab DOM
// ============================================================
function createTabDOM(info){
  const id=info.id;
  T[id]={name:info.name,model:info.model||'claude-opus-5',effort:info.effort||'max',
    projectPath:info.project_path||'',projectName:info.project_name||'',
    maxTurns:info.max_turns||0,customFlags:info.custom_flags||'',
    sysPrompt:info.system_prompt||'',sessionId:info.session_id||'',
    permMode:info.permission_mode||'default',allowedTools:info.allowed_tools||[],
    yolo:!!info.yolo_mode,role:info.role||'',
    peerTabs:Array.isArray(info.peer_tabs)?info.peer_tabs.slice():[],
    streaming:false,bubble:null,content:'',bubbleText:'',termActive:false,permPrompt:false};
  if(!order.includes(id))order.push(id);
  const bar=document.getElementById('tabBar');
  const addBtn=bar.querySelector('.tab-add');
  const tab=document.createElement('div');
  tab.className='tab';tab.dataset.tab=id;
  const yoloCls=T[id].yolo?'on':'off';
  const yoloTxt=T[id].yolo?'\u26A1'+t('yoloOn'):'\u26A1'+t('yoloOff');
  const curRole=T[id].role||'';
  const roleCls='r-'+(curRole||'none');
  const rIco=roleIcon(curRole);
  const roleTxt=curRole?((rIco?esc(rIco)+' ':'')+esc(roleName(curRole))):esc(t('noRole'));
  tab.innerHTML='<span class="nd"></span>'
    +'<span class="tn" ondblclick="startRename(\''+id+'\')">'+esc(info.name)+'</span>'
    +'<span class="role-badge '+roleCls+'" id="role_'+id+'" onclick="event.stopPropagation();openRoleMenu(event,\''+id+'\')" title="'+esc(t('setRoleTitle'))+'">'+roleTxt+'</span>'
    +'<span class="yolo-btn '+yoloCls+'" id="yolo_'+id+'" onclick="event.stopPropagation();toggleYolo(\''+id+'\')" title="'+esc(t('yoloTitle'))+'">'+yoloTxt+'</span>'
    +'<span class="tc" onclick="event.stopPropagation();closeTab(\''+id+'\')">&#x2715;</span>';
  tab.onclick=()=>switchTab(id);
  bar.insertBefore(tab,addBtn);
  // Apply dynamic color/icon from RoleRegistry (inline styles override class-based CSS)
  if(curRole)renderRoleBadge(id,curRole);
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
    info.messages.forEach(m=>{
      if(m.role==='dispatch'){
        renderDispatchedBubble(id,m.prefix||'',m.content||'',m.source||'coord_dispatch',false);
      }else{
        addMsg(id,m.role,m.content,false);
      }
    });
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
  refreshCoordination();
  refreshRolesForActive();
  // Layer 3 GUI bridge: auto-refresh graph context for the new active role
  // (synapse-like: switching tabs surfaces the role's relevant past decisions).
  // Only runs if the panel is currently visible — silent otherwise.
  try{
    const gp=document.getElementById('graphPanel');
    if(gp&&gp.style.display!=='none'){graphRefreshContext();}
    // Always refresh the lightweight stats line in the coord header.
    refreshGraphStatsLine();
  }catch(e){}
  document.getElementById('msgIn').focus();
}

// Refresh _ROLES from the active tab's project's registry. Called on tab
// switch and after role edits, so UI badges / role menus always show the
// currently-focused project's role set.
async function refreshRolesForActive(){
  if(!act)return;
  try{
    const roles=await pywebview.api.get_roles(act);
    if(Array.isArray(roles)){
      _ROLES=roles;
      rerenderAllRoleBadges();
    }
  }catch(e){}
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
    document.getElementById('topProj').textContent=r.name;updateSidebar();
    // Sync role state: backend may have cleared tab.role if the old role
    // doesn't exist in the new project's registry. Refresh JS cache + badge.
    if(typeof r.role!=='undefined'){T[act].role=r.role||'';}
    if(r.roles){_ROLES=r.roles;}
    if(typeof rerenderAllRoleBadges==='function')rerenderAllRoleBadges();
    else if(typeof renderRoleBadge==='function')renderRoleBadge(act,T[act].role||'');
  }
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
  // Terminal mode: forward control keys to PTY.
  // stopPropagation prevents the document-level keydown listener from double-
  // firing (e.g. Ctrl+L would otherwise both send FF to PTY AND clear GUI).
  if(act&&T[act]&&T[act].termActive){
    const empty=!document.getElementById('msgIn').value;
    const snd=(b)=>{e.preventDefault();e.stopPropagation();pywebview.api.pty_input(act,b);};
    if(e.key==='Escape'){snd('\x1b');return;}
    if(e.key==='Tab'&&!e.shiftKey){snd('\t');return;}
    if(e.key==='Tab'&&e.shiftKey){snd('\x1b[Z');return;}
    if(e.ctrlKey&&e.key==='c'){const sel=window.getSelection();const ta=document.getElementById('msgIn');if((sel&&sel.toString())||(ta&&ta.selectionStart!==ta.selectionEnd)){return;}snd('\x03');return;}
    if(e.ctrlKey&&e.key==='d'){snd('\x04');return;}
    if(e.ctrlKey&&e.key==='z'){snd('\x1a');return;}
    if(e.ctrlKey&&e.key==='r'){snd('\x12');return;}
    if(e.ctrlKey&&e.key==='l'){snd('\x0c');return;}
    // Only forward when input is empty (otherwise normal text editing)
    if(empty){
      if(e.key==='ArrowUp'){snd('\x1b[A');return;}
      if(e.key==='ArrowDown'){snd('\x1b[B');return;}
      if(e.key==='ArrowLeft'){snd('\x1b[D');return;}
      if(e.key==='ArrowRight'){snd('\x1b[C');return;}
      if(e.key==='Backspace'){snd('\x7f');return;}
      if(e.key==='Delete'){snd('\x1b[3~');return;}
      if(e.key==='Home'){snd('\x1b[H');return;}
      if(e.key==='End'){snd('\x1b[F');return;}
    }
  }
  if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();e.stopPropagation();doSend();return;}
  // Ctrl+L / Ctrl+Shift+L / Ctrl+Shift+N are handled by the document-level
  // listener (single source of truth). msgIn only needs Enter handling here
  // to avoid duplicate action firing.
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
    // Capture the tab id now: `act` can change if the user switches tabs
    // during the 500ms wait, which would send into the wrong tab.
    const tid=act;
    pywebview.api.stop_streaming(tid);
    setTimeout(()=>{
      const wc=document.getElementById('wc_'+tid);if(wc)wc.style.display='none';
      addMsg(tid,'user',text,true);scrollBot(tid);
      pywebview.api.send_message(tid,text);
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

// Icons & i18n labels for each dispatch source. Kept in one place so restore
// replay and live dispatch use identical visuals.
const _DISP_ICONS={
  peer_tab:'⇆',        // ⇆
  peer_inbox:'\u{1F4EE}',   // 📮
  coord_inject:'▼',     // ▼
  coord_report:'↑',     // ↑
  file_report:'↑',      // ↑
  coord_dispatch:'↓',   // ↓
  file_dispatch:'↓',    // ↓
};
const _DISP_LABEL_KEYS={
  peer_tab:'dispSrcPeerTab',
  peer_inbox:'dispSrcPeerInbox',
  coord_inject:'dispSrcCoordInject',
  coord_report:'dispSrcCoordReport',
  file_report:'dispSrcFileReport',
  coord_dispatch:'dispSrcCoordDispatch',
  file_dispatch:'dispSrcFileDispatch',
};
function _dispIcon(source){return _DISP_ICONS[source]||'▼';}
function _dispLabel(source){const k=_DISP_LABEL_KEYS[source];return k?t(k):source;}

// renderDispatchedBubble — render a cross-tab dispatch (peer / AutoCoordinator)
// onto the receiving tab's chat pane. Called live by Python side right after
// the PTY write, and replayed from tab.messages on tab restore.
function renderDispatchedBubble(tabId,prefix,body,source,anim){
  const pane=document.getElementById('chat_'+tabId);if(!pane)return;
  const wc=document.getElementById('wc_'+tabId);if(wc&&wc.style.display!=='none')wc.style.display='none';
  const div=document.createElement('div');div.className='msg dispatch src-'+(source||'coord_dispatch');
  if(anim===false)div.style.animation='none';
  const bub=document.createElement('div');bub.className='bub disp-bub';
  // Header: icon + label + raw prefix (truncated via CSS ellipsis)
  const hdr=document.createElement('div');hdr.className='disp-head';
  const ico=document.createElement('span');ico.className='disp-icon';ico.textContent=_dispIcon(source);hdr.appendChild(ico);
  const lbl=document.createElement('span');lbl.className='disp-label';
  lbl.dataset.i18n=_DISP_LABEL_KEYS[source]||'';lbl.textContent=_dispLabel(source);hdr.appendChild(lbl);
  if(prefix){const pf=document.createElement('span');pf.className='disp-prefix';pf.title=prefix;pf.textContent=prefix;hdr.appendChild(pf);}
  bub.appendChild(hdr);
  // Body with truncation
  const bodyEl=document.createElement('div');bodyEl.className='disp-body';
  const full=String(body||'');
  const LIMIT=500;
  if(full.length>LIMIT){
    bodyEl.textContent=full.slice(0,LIMIT);
    const note=document.createElement('div');note.className='disp-note';
    note.textContent='('+t('dispTruncated')+' '+full.length+' chars)';
    bub.appendChild(bodyEl);
    bub.appendChild(note);
    const btn=document.createElement('button');btn.className='disp-expand';
    btn.dataset.i18n='dispExpand';btn.textContent=t('dispExpand');
    let expanded=false;
    btn.addEventListener('click',()=>{
      expanded=!expanded;
      if(expanded){bodyEl.textContent=full;bodyEl.classList.add('full');btn.dataset.i18n='dispCollapse';btn.textContent=t('dispCollapse');note.style.display='none';}
      else{bodyEl.textContent=full.slice(0,LIMIT);bodyEl.classList.remove('full');btn.dataset.i18n='dispExpand';btn.textContent=t('dispExpand');note.style.display='';}
    });
    bub.appendChild(btn);
  }else{
    bodyEl.textContent=full;
    bub.appendChild(bodyEl);
  }
  div.appendChild(bub);
  const ts=document.createElement('div');ts.className='ts';
  ts.textContent=new Date().toLocaleTimeString('ja-JP',{hour:'2-digit',minute:'2-digit'});
  div.appendChild(ts);
  pane.appendChild(div);
  if(tabId===act)scrollBot(tabId);
  return bub;
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

function onSessionResumed(tid){
  const ti=T[tid];if(!ti)return;
  const el=document.getElementById('screen_'+tid);
  if(el){
    const marker='\n--- '+t('sessResumed')+' ('+(ti.sessionId||'').slice(0,8)+') ---\n';
    if(!el.textContent.includes(t('sessResumed')))el.textContent+=marker;
  }
}
function onSessionIdUpdate(tid,newSid,hadOld){
  const ti=T[tid];if(!ti)return;
  ti.sessionId=newSid;
  if(hadOld){
    const el=document.getElementById('screen_'+tid);
    if(el)el.textContent+='\n--- '+t('sessInvalid')+' ---\n';
  }
}
// Auto-restart failure: --resume'd a stale session_id and PTY died within
// the probe window. Show a toast + screen marker so the user knows why
// their session won't "look the same" as before.
function onSessionRestoreFailed(tid,staleSid){
  const el=document.getElementById('screen_'+tid);
  if(el)el.textContent+='\n--- '+t('sessResumeFailed')+' ('+(staleSid||'').slice(0,8)+') ---\n';
  try{showToast(t('sessResumeFailed'));}catch(e){}
}

// ============================================================
// Terminal mode (PTY) callbacks
// ============================================================
function showTerminal(tid){
  const ti=T[tid];if(!ti)return;
  ti.termActive=true;
  if(_ptyStartTimes[tid]===undefined)_ptyStartTimes[tid]=Date.now();
  // Visibility is owned solely by switchTab. For a BACKGROUND tab just record
  // termActive (switchTab reveals its terminal when the user selects it) —
  // never force a non-active tab's terminal pane visible over the active one.
  if(tid!==act)return;
  const wc=document.getElementById('wc_'+tid);if(wc)wc.style.display='none';
  const chat=document.getElementById('chat_'+tid);if(chat)chat.classList.remove('vis');
  const term=document.getElementById('term_'+tid);if(term)term.classList.add('vis');
  const sc=document.getElementById('screen_'+tid);if(sc)sc.scrollTop=sc.scrollHeight;
  updateHint();
}
function onScreenUpdate(tid,text,html){
  const el=document.getElementById('screen_'+tid);
  if(!el)return;
  const wasNearBottom=el.scrollHeight-el.scrollTop-el.clientHeight<50;
  const prevScroll=el.scrollTop;
  // html carries per-run <span> colour built from the pyte buffer (Python side
  // escapes the text and controls every style value). Fall back to plain text
  // when the colour build was skipped/failed (html null) — older/safe path.
  if(html!=null)el.innerHTML=html;
  else el.textContent=text;
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
      let h='<span class="perm-lbl">'+esc(t('permBannerLabel'))+'</span>';
      choices.forEach(function(c,i){
        const num=c[0],label=c[1];
        const cls=/deny|no$/i.test(label)?'d':(/session|always/i.test(label)?'s':'a');
        h+='<button class="perm-btn '+cls+'" onclick="permSend('+i+')">'+num+'. '+esc(label)+'</button>';
      });
      p.innerHTML=h;p.classList.add('vis');
    }else if(isPerm){
      p.innerHTML='<span class="perm-lbl">'+esc(t('permBannerLabel'))+'</span><button class="perm-btn a" onclick="permSend(0)">'+esc(t('permAllow'))+'</button><button class="perm-btn d" onclick="permSend(1)">'+esc(t('permDeny'))+'</button>';
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

// ============================================================
// Autonomous system: Yolo Mode, Role selection, Coordination status
// ============================================================
// Role registry helpers (data sourced from RoleRegistry via get_initial_state)
function roleDef(id){if(!id)return null;return _ROLES.find(r=>r.id===id)||null;}
function roleIcon(id){const r=roleDef(id);return r&&r.icon?r.icon:'';}
function roleColor(id){const r=roleDef(id);return r&&r.color?r.color:'#a78bfa';}
function roleName(id){const r=roleDef(id);return r?r.name:(id||'');}
function roleKind(id){const r=roleDef(id);return r?r.kind:'';}
function _roleRgba(hex,a){
  if(!hex||hex[0]!=='#')return 'rgba(167,139,250,'+a+')';
  let h=hex.slice(1);
  if(h.length===3)h=h.split('').map(c=>c+c).join('');
  if(h.length<6)return 'rgba(167,139,250,'+a+')';
  const r=parseInt(h.slice(0,2),16),g=parseInt(h.slice(2,4),16),b=parseInt(h.slice(4,6),16);
  if(isNaN(r)||isNaN(g)||isNaN(b))return 'rgba(167,139,250,'+a+')';
  return 'rgba('+r+','+g+','+b+','+a+')';
}
function rerenderAllRoleBadges(){
  for(const id of Object.keys(T)){renderRoleBadge(id,T[id].role||'');}
  refreshCoordRoles();
}
function onYoloAccept(tid,label){
  const te=document.querySelector('[data-tab="'+tid+'"]');
  if(te){te.classList.add('yolo-flash');setTimeout(()=>te.classList.remove('yolo-flash'),600);}
  console.log('[yolo-auto-accept]',tid,label);
}
function onYoloSkip(tid,reason,payload){
  // Observability: append a dim warning bubble explaining WHY Yolo skipped.
  // Python-side already dedups same reason+payload within 5s, so no throttle here.
  let key;
  if(reason==='danger_keyword') key='yoloSkipDangerKeyword';
  else if(reason==='no_approve_marker') key='yoloSkipNoApprove';
  else if(reason==='no_choices') key='yoloSkipNoChoices';
  else key='yoloSkipDangerKeyword'; // fallback
  let msg=t(key);
  if(payload && payload.kw) msg=msg.replace('{kw}',payload.kw);
  if(payload && payload.label) msg=msg.replace('{label}',payload.label);
  const pane=document.getElementById('chat_'+tid);
  if(!pane){console.log('[yolo-skip]',tid,reason,payload);return;}
  const div=document.createElement('div');
  div.className='msg msg-yolo-skip';
  div.textContent='⚡ '+msg;
  pane.appendChild(div);
  pane.scrollTop=pane.scrollHeight;
  console.log('[yolo-skip]',tid,reason,payload);
}
function toggleYolo(tid){
  tid=tid||act;if(!tid)return;
  const ti=T[tid]||{};const on=!ti.yolo;ti.yolo=on;
  pywebview.api.set_tab_yolo(tid,on).then(r=>{
    renderYoloBadge(tid,on);
  });
}
function renderYoloBadge(tid,on){
  const el=document.getElementById('yolo_'+tid);
  if(!el)return;
  el.textContent=on?'\u26A1'+t('yoloOn'):'\u26A1'+t('yoloOff');
  el.className='yolo-btn '+(on?'on':'off');
}
function setRole(tid,role){
  tid=tid||act;if(!tid)return;
  pywebview.api.set_tab_role(tid,role).then(r=>{
    if(r&&r.ok){
      if(T[tid])T[tid].role=r.role;
      renderRoleBadge(tid,r.role);
      // 2026-04-26: auto-rename tab to "<icon> <RoleName>" when backend
      // returns a new name (AUTO_RENAME_ON_ROLE_SET feature). Sync local state
      // and update the tab's display label without a full re-render.
      if(r.renamed&&r.name&&T[tid]){
        T[tid].name=r.name;
        const tab=document.querySelector('[data-tab="'+tid+'"]');
        if(tab){const sp=tab.querySelector('.tn');if(sp)sp.textContent=r.name;}
      }
      refreshCoordRoles();
    }
  });
}
//: backend hook to push tab renames triggered outside set_tab_role
// flow (e.g. Wizard role apply, role request approval). Keeps T[id].name +
// DOM .tn span in sync without forcing a full state refetch.
function onTabRenamed(tid,name){
  if(!tid||!name)return;
  if(T[tid])T[tid].name=name;
  const tab=document.querySelector('[data-tab="'+tid+'"]');
  if(tab){const sp=tab.querySelector('.tn');if(sp)sp.textContent=name;}
}
function renderRoleBadge(tid,role){
  const el=document.getElementById('role_'+tid);
  if(!el)return;
  if(!role){
    el.textContent=t('noRole');
    el.className='role-badge r-none';
    el.removeAttribute('style');
    return;
  }
  const ico=roleIcon(role);
  const kind=roleKind(role);
  // CCO kind = special top-of-org-chart indicator. Prepend 👑 to the badge
  // text and add r-cco class for CSS hooks (future crown glow / etc).
  const crown=(kind==='cco')?'\u{1F451} ':'';
  el.textContent=crown+(ico?ico+' ':'')+roleName(role);
  el.className='role-badge r-'+role+(kind==='cco'?' r-cco':'');
  if(kind==='cco')el.title=t('setRoleTitle')+' (CCO)';
  const col=roleColor(role);
  el.style.borderColor=col;
  el.style.color=col;
  el.style.background=_roleRgba(col,0.12);
}
function openRoleMenu(ev,tid){
  const prev=document.getElementById('roleMenu');if(prev)prev.remove();
  const m=document.createElement('div');m.id='roleMenu';m.className='role-menu';
  const items=[..._ROLES.map(r=>[r.id,(r.kind==='cco'?'\u{1F451} ':'')+(r.icon?r.icon+' ':'')+r.name,r.color,r.kind]),['','\u2014 None','','none']];
  items.forEach(r=>{
    const it=document.createElement('div');it.textContent=r[1];
    if(r[2]){it.style.borderLeft='3px solid '+r[2];it.style.paddingLeft='8px';}
    if(r[3]==='coordinator')it.style.fontWeight='600';
    if(r[3]==='cco'){it.style.fontWeight='600';it.style.color='#fde047';}
    it.onclick=e=>{e.stopPropagation();setRole(tid,r[0]);m.remove();};
    m.appendChild(it);
  });
  const edit=document.createElement('div');
  edit.style.cssText='border-top:1px solid var(--bd);margin-top:4px;padding-top:6px;color:var(--ac);font-size:10px';
  edit.textContent='\u2699 '+t('editRoles');
  edit.onclick=e=>{e.stopPropagation();m.remove();openRolesEditor();};
  m.appendChild(edit);
  document.body.appendChild(m);
  const rect=ev.currentTarget.getBoundingClientRect();
  m.style.left=rect.left+'px';m.style.top=(rect.bottom+4)+'px';
  setTimeout(()=>{
    const close=e=>{if(!m.contains(e.target)){m.remove();document.removeEventListener('click',close);}};
    document.addEventListener('click',close);
  },0);
}

// --- Role Editor modal (edit/add/remove roles) ---
function openRolesEditor(){
  const list=document.getElementById('rolesList');
  if(!list)return;
  list.innerHTML='<div class="rle-head"><div></div><div>'+esc(t('colId'))+'</div><div>'+esc(t('colName'))+'</div><div>'+esc(t('colIcon'))+'</div><div>'+esc(t('colColorKind'))+'</div><div></div></div>';
  _ROLES.forEach(r=>list.appendChild(_roleEditorRow(r)));
  document.getElementById('rolesModal').classList.add('open');
}
function closeRolesEditor(){document.getElementById('rolesModal').classList.remove('open');}
function _roleEditorRow(r){
  const row=document.createElement('div');row.className='rle-row';
  const dot=document.createElement('div');
  dot.style.cssText='width:14px;height:14px;border-radius:50%;justify-self:center;background:'+(r.color||'#a78bfa');
  row.appendChild(dot);
  const idI=document.createElement('input');idI.type='text';idI.className='ri-id';idI.value=r.id||'';idI.placeholder=t('roleIdPH');idI.setAttribute('maxlength','24');
  idI.addEventListener('input',()=>{idI.value=idI.value.toLowerCase().replace(/[^a-z0-9_]/g,'');});
  row.appendChild(idI);
  const nmI=document.createElement('input');nmI.type='text';nmI.className='ri-name';nmI.value=r.name||'';nmI.placeholder=t('displayNamePH');nmI.setAttribute('maxlength','32');
  row.appendChild(nmI);
  const icI=document.createElement('input');icI.type='text';icI.className='ri-icon';icI.value=r.icon||'\u{1F539}';icI.setAttribute('maxlength','4');
  row.appendChild(icI);
  const right=document.createElement('div');right.style.cssText='display:flex;gap:4px;align-items:center';
  const cI=document.createElement('input');cI.type='color';cI.className='ri-color';cI.value=(r.color&&/^#[0-9a-fA-F]{6}$/.test(r.color))?r.color:'#a78bfa';cI.style.width='32px';cI.style.minWidth='32px';
  cI.addEventListener('input',()=>{dot.style.background=cI.value;});
  right.appendChild(cI);
  const kS=document.createElement('select');kS.className='ri-kind';
  kS.innerHTML='<option value="worker">worker</option><option value="coordinator">coordinator</option><option value="cco">cco</option>';
  kS.value=(r.kind==='coordinator'||r.kind==='cco')?r.kind:'worker';
  right.appendChild(kS);
  row.appendChild(right);
  const del=document.createElement('button');del.className='ri-del';del.textContent='✕';del.title=t('removeTitle');
  del.onclick=e=>{e.preventDefault();row.remove();};
  row.appendChild(del);
  return row;
}
function addRoleRow(){
  const list=document.getElementById('rolesList');if(!list)return;
  list.appendChild(_roleEditorRow({id:'',name:'',icon:'\u{1F539}',color:'#a78bfa',kind:'worker'}));
}
function resetRoles(){
  if(!confirm(t('confirmResetRoles')))return;
  const defaults=[
    {id:'manager',name:'Manager',icon:'\u{1F39B}️',color:'#a78bfa',kind:'coordinator'},
    {id:'developer',name:'Developer',icon:'\u{1F4BB}',color:'#34d399',kind:'worker'},
    {id:'marketing',name:'Marketing',icon:'\u{1F4E2}',color:'#fb923c',kind:'worker'},
    {id:'security',name:'Security',icon:'\u{1F6E1}️',color:'#f87171',kind:'worker'},
  ];
  const list=document.getElementById('rolesList');
  list.innerHTML='<div class="rle-head"><div></div><div>'+esc(t('colId'))+'</div><div>'+esc(t('colName'))+'</div><div>'+esc(t('colIcon'))+'</div><div>'+esc(t('colColorKind'))+'</div><div></div></div>';
  defaults.forEach(r=>list.appendChild(_roleEditorRow(r)));
}
async function saveRolesEditor(){
  const list=document.getElementById('rolesList');
  const rows=Array.from(list.querySelectorAll('.rle-row'));
  const collected=rows.map(row=>({
    id:(row.querySelector('.ri-id')||{}).value||'',
    name:(row.querySelector('.ri-name')||{}).value||'',
    icon:(row.querySelector('.ri-icon')||{}).value||'',
    color:(row.querySelector('.ri-color')||{}).value||'',
    kind:(row.querySelector('.ri-kind')||{}).value||'worker',
  })).filter(r=>r.id);
  if(!collected.length){alert(t('alertAtLeastOne'));return;}
  if(!collected.some(r=>r.kind==='coordinator')){alert(t('alertNeedCoord'));return;}
  if(!collected.some(r=>r.kind==='worker')){alert(t('alertNeedWorker'));return;}
  try{
    // Roles are now per-project. Pass the active tab id so the backend
    // knows which project's registry to update.
    const res=await pywebview.api.set_roles(act||'',collected);
    if(!res||!res.ok){alert((res&&res.error)||t('alertFailSaveRoles'));return;}
    _ROLES=res.roles||[];
    const valid=new Set(_ROLES.map(r=>r.id));
    // Only clear roles for tabs in the same project (roles are per-project).
    const actTab=act&&T[act]?T[act]:null;
    const actProj=actTab&&actTab.projectPath?actTab.projectPath:'';
    for(const id of Object.keys(T)){
      if((T[id].projectPath||'')===actProj&&T[id].role&&!valid.has(T[id].role)){T[id].role='';}
    }
    rerenderAllRoleBadges();
    closeRolesEditor();
  }catch(e){alert(t('errorPrefix')+e);}
}

// ============================================================================
// Harness: peer management, role generation, runtime role creation
// ============================================================================
let _pendingProposal=null;  // {project, roles:[...]}
let _pendingRequest=null;   // {request_key, project_path, id, name, ...}
let _peersEditProject='';   // project path currently shown in peers modal
let _peerTabsEdit='';       // tab id currently shown in peer-tabs modal
let _tsCurId='';            // tab id currently shown in tab-settings modal

// --- E: Role generation ---
async function genRoles(){
  if(!act){alert(t('alertOpenTab'));return;}
  const ti=T[act]||{};
  if(!ti.projectPath){alert(t('alertNoProjectOther'));return;}
  const btn=document.getElementById('genRolesBtn');
  const prev=btn.textContent;btn.disabled=true;btn.textContent='⏳ '+t('genRolesGen');
  try{
    const r=await pywebview.api.trigger_role_generation(act);
    if(!r||!r.ok){alert((r&&r.error)||t('alertFailedTrigger'));return;}
    // Proposal file will appear via onRoleProposal when Claude finishes.
  }catch(e){alert(t('errorPrefix')+e);}
  finally{setTimeout(()=>{btn.disabled=false;btn.textContent=prev;},1500);}
}

function onRoleProposal(project,roles){
  _pendingProposal={project:project,roles:roles};
  const scope=document.getElementById('ropScope');
  if(scope)scope.textContent='— '+(project.split(/[\/\\]/).pop()||project);
  const list=document.getElementById('ropList');
  list.innerHTML='<div class="rle-head"><div></div><div>'+esc(t('colId'))+'</div><div>'+esc(t('colName'))+'</div><div>'+esc(t('colIcon'))+'</div><div>'+esc(t('colKind'))+'</div><div></div></div>';
  (roles||[]).forEach(r=>{
    const row=document.createElement('div');row.className='rle-row';
    const dot=document.createElement('div');dot.style.cssText='width:14px;height:14px;border-radius:50%;justify-self:center;background:'+(r.color||'#a78bfa');
    row.appendChild(dot);
    const mk=(v,cls)=>{const d=document.createElement('div');d.className=cls;d.textContent=v||'';d.style.cssText='padding:4px 6px;color:var(--tx);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap';return d;};
    row.appendChild(mk(r.id,'ri-id-r'));
    row.appendChild(mk(r.name,'ri-name-r'));
    row.appendChild(mk(r.icon,'ri-icon-r'));
    row.appendChild(mk(r.kind,'ri-kind-r'));
    row.appendChild(document.createElement('div'));
    list.appendChild(row);
  });
  document.getElementById('roleProposalModal').classList.add('open');
}

async function applyRoleProposal(){
  if(!_pendingProposal)return;
  const proj=_pendingProposal.project;
  try{
    const r=await pywebview.api.apply_role_proposal(proj,'');
    if(!r||!r.ok){alert((r&&r.error)||t('alertApplyFail'));return;}
    // Clear stale per-tab role cache for every tab in this project (backend
    // already cleared `tab.role` server-side; JS cache must mirror it so
    // subsequent renders don't show phantom old roles).
    const newRoleIds=new Set(((r.roles||[]).map(x=>x.id||x)));
    for(const id in T){
      const ti=T[id];if(!ti)continue;
      if(ti.projectPath===proj && ti.role && !newRoleIds.has(ti.role)){
        ti.role='';
      }
    }
    if(act&&T[act]&&T[act].projectPath===proj){
      _ROLES=r.roles||[];
    }
    if(typeof rerenderAllRoleBadges==='function')rerenderAllRoleBadges();
  }catch(e){alert(t('errorPrefix')+e);}
  _pendingProposal=null;
  document.getElementById('roleProposalModal').classList.remove('open');
}

async function dismissRoleProposal(){
  if(!_pendingProposal){document.getElementById('roleProposalModal').classList.remove('open');return;}
  try{await pywebview.api.dismiss_role_proposal(_pendingProposal.project);}catch(e){}
  _pendingProposal=null;
  document.getElementById('roleProposalModal').classList.remove('open');
}

// --- G: New Project Wizard (2026-04-24) ---
function openWizard(){
  document.getElementById('wzStage1').style.display='';
  document.getElementById('wzStage2').style.display='none';
  const stg=document.getElementById('wzStage');if(stg)stg.textContent='— 1/2';
  const ov=document.getElementById('wzOverview');if(ov)ov.value='';
  const pt=document.getElementById('wzPathInp');if(pt)pt.value='';
  const sc=document.getElementById('wzStartCoord');if(sc)sc.checked=true;
  const ts=document.getElementById('wzTypeSel');if(ts&&!ts.value)ts.value='saas';
  document.getElementById('wizardModal').classList.add('open');
  setTimeout(()=>{const o=document.getElementById('wzOverview');if(o)o.focus();},50);
}
function closeWizard(){document.getElementById('wizardModal').classList.remove('open');}
function wzBackToStage1(){
  document.getElementById('wzStage1').style.display='';
  document.getElementById('wzStage2').style.display='none';
  const stg=document.getElementById('wzStage');if(stg)stg.textContent='— 1/2';
}
async function wzPickFolder(){
  try{
    const cur=document.getElementById('wzPathInp').value||'';
    const r=await pywebview.api.pick_folder(cur);
    if(r&&r.ok&&r.path)document.getElementById('wzPathInp').value=r.path;
  }catch(e){}
}
async function wzPropose(){
  const overview=document.getElementById('wzOverview').value.trim();
  const ptype=document.getElementById('wzTypeSel').value||'other';
  const path=document.getElementById('wzPathInp').value.trim();
  if(!path){alert(t('wzAlertPath'));return;}
  const btn=document.getElementById('wzProposeBtn');
  const prev=btn.textContent;btn.disabled=true;btn.textContent='⏳ '+t('wzProposing');
  try{
    const r=await pywebview.api.wizard_propose_roles(overview,ptype);
    if(!r||!r.ok){alert((r&&r.error)||t('wzErrorAPI'));return;}
    const srcLbl=r.source==='api'?('\u{1F916} '+t('wzSrcAPI')):('⚙ '+t('wzSrcFallback'));
    const reasonTxt=r.reason?(' ('+r.reason+')'):'';
    document.getElementById('wzReviewSrc').textContent=srcLbl+reasonTxt;
    wzRenderRoleList(r.roles||[]);
    document.getElementById('wzStage1').style.display='none';
    document.getElementById('wzStage2').style.display='';
    const stg=document.getElementById('wzStage');if(stg)stg.textContent='— 2/2';
  }catch(e){alert(t('errorPrefix')+e);}
  finally{btn.disabled=false;btn.textContent=prev;}
}
function wzRenderRoleList(roles){
  const list=document.getElementById('wzReviewList');
  list.innerHTML='<div class="rle-head"><div></div><div>'+esc(t('colId'))+'</div><div>'+esc(t('colName'))+'</div><div>'+esc(t('colIcon'))+'</div><div>'+esc(t('colColorKind'))+'</div><div></div></div>';
  (roles||[]).forEach(r=>list.appendChild(_roleEditorRow(r)));
}
function wzAddRoleRow(){
  const list=document.getElementById('wzReviewList');if(!list)return;
  list.appendChild(_roleEditorRow({id:'',name:'',icon:'\u{1F539}',color:'#a78bfa',kind:'worker'}));
}
async function wzCreate(){
  const list=document.getElementById('wzReviewList');
  const rows=Array.from(list.querySelectorAll('.rle-row'));
  const collected=rows.map(row=>({
    id:(row.querySelector('.ri-id')||{}).value||'',
    name:(row.querySelector('.ri-name')||{}).value||'',
    icon:(row.querySelector('.ri-icon')||{}).value||'',
    color:(row.querySelector('.ri-color')||{}).value||'',
    kind:(row.querySelector('.ri-kind')||{}).value||'worker',
  })).filter(r=>r.id);
  if(!collected.length){alert(t('alertAtLeastOne'));return;}
  if(!collected.some(r=>r.kind==='coordinator')){alert(t('alertNeedCoord'));return;}
  if(!collected.some(r=>r.kind==='worker')){alert(t('alertNeedWorker'));return;}
  const overview=document.getElementById('wzOverview').value.trim();
  const ptype=document.getElementById('wzTypeSel').value||'other';
  const path=document.getElementById('wzPathInp').value.trim();
  const startCoord=document.getElementById('wzStartCoord').checked;
  if(!path){alert(t('wzAlertPath'));return;}
  const btn=document.getElementById('wzCreateBtn');
  const prev=btn.textContent;btn.disabled=true;btn.textContent='⏳ '+t('wzCreating');
  try{
    const r=await pywebview.api.wizard_create_project(path,overview,ptype,collected,startCoord);
    if(!r||!r.ok){alert((r&&r.error)||t('wzErrorCreate'));return;}
    closeWizard();
    // onWizardTabsSpawned is invoked by backend via self._js() and handles DOM + switch.
  }catch(e){alert(t('errorPrefix')+e);}
  finally{btn.disabled=false;btn.textContent=prev;}
}
function onWizardTabsSpawned(tabs,activeId,projectPath){
  try{
    if(Array.isArray(tabs)){
      for(const info of tabs){
        if(info&&info.id&&typeof createTabDOM==='function')createTabDOM(info);
      }
    }
    if(activeId&&typeof switchTab==='function')switchTab(activeId);
    // Refresh the role list / coord panel for the newly active tab.
    if(typeof refreshCoordination==='function'){try{refreshCoordination();}catch(e){}}
  }catch(e){console.warn('[wizard-spawned]',e);}
}

// --- F: Runtime role creation by Manager ---
function onRoleRequest(req){
  _pendingRequest=req;
  const box=document.getElementById('rreqFields');
  const mk=(k,v)=>'<div style="color:var(--txd)">'+esc(k)+'</div><div>'+esc(String(v||''))+'</div>';
  box.innerHTML=
    mk(t('fieldProject'),(req.project_path||'').split(/[\/\\]/).pop())
    +mk(t('fieldRoleId'),req.id)
    +mk(t('fieldName'),req.name||req.id)
    +mk(t('fieldIcon'),req.icon||'')
    +mk(t('fieldColor'),req.color||'')
    +mk(t('fieldKind'),req.kind||'worker')
    +mk(t('fieldSubDir'),req.dir||('teams/'+req.id))
    +mk(t('fieldBrief'),req.brief||'');
  document.getElementById('roleRequestModal').classList.add('open');
}

async function applyRoleRequest(){
  if(!_pendingRequest)return;
  const req=_pendingRequest;
  try{
    const r=await pywebview.api.apply_role_request(req);
    if(!r||!r.ok){alert((r&&r.error)||t('alertApplyFail'));return;}
    if(r.tab){createTabDOM(r.tab);}
  }catch(e){alert(t('errorPrefix')+e);}
  _pendingRequest=null;
  document.getElementById('roleRequestModal').classList.remove('open');
}

async function dismissRoleRequest(){
  if(_pendingRequest){
    try{await pywebview.api.dismiss_role_request(_pendingRequest.request_key||'');}catch(e){}
  }
  _pendingRequest=null;
  document.getElementById('roleRequestModal').classList.remove('open');
}

function onRoleRequestApplied(key,tabInfo){
  // Backend-spawned tab — DOM already created by applyRoleRequest(). This
  // hook exists for out-of-band applies (e.g. future auto-approve).
}

// --- B: Cross-project peers ---
async function openProjectPeers(){
  if(!act){alert(t('alertOpenTab'));return;}
  const ti=T[act]||{};
  if(!ti.projectPath){alert(t('alertSelectProject'));return;}
  _peersEditProject=ti.projectPath;
  const scope=document.getElementById('ppScope');
  if(scope)scope.textContent='— '+(ti.projectName||ti.projectPath);
  try{
    const r=await pywebview.api.get_project_peers(ti.projectPath);
    if(!r||!r.ok){alert((r&&r.error)||t('alertFailedLoadPeers'));return;}
    document.getElementById('pp_selfAlias').value=r.self_alias||'';
    const list=document.getElementById('ppList');
    list.innerHTML='<div class="rle-head"><div></div><div>'+esc(t('peerAlias'))+'</div><div style="grid-column:span 3">'+esc(t('projectPath'))+'</div><div></div></div>';
    (r.peers||[]).forEach(p=>list.appendChild(_ppRow(p)));
  }catch(e){alert(t('errorPrefix')+e);return;}
  document.getElementById('projectPeersModal').classList.add('open');
}

function _ppRow(p){
  const row=document.createElement('div');row.className='rle-row';
  row.style.gridTemplateColumns='28px 120px 1fr 28px';
  const dot=document.createElement('div');dot.style.cssText='width:10px;height:10px;border-radius:50%;justify-self:center;background:#1f6feb';
  row.appendChild(dot);
  const aI=document.createElement('input');aI.type='text';aI.className='pp-alias';aI.value=p.alias||'';aI.placeholder='alias';aI.setAttribute('maxlength','24');
  aI.addEventListener('input',()=>{aI.value=aI.value.toLowerCase().replace(/[^a-z0-9_\-]/g,'');});
  row.appendChild(aI);
  const pWrap=document.createElement('div');pWrap.style.cssText='display:flex;gap:4px;grid-column:span 1';
  const pI=document.createElement('input');pI.type='text';pI.className='pp-path';pI.value=p.path||'';pI.placeholder='C:\\\\path\\\\to\\\\other-project';pI.style.cssText='flex:1;padding:4px 6px;background:var(--bg);border:1px solid var(--bd);border-radius:4px;color:var(--tx);font-size:11px';
  pWrap.appendChild(pI);
  const bB=document.createElement('button');bB.className='bg';bB.textContent='\u{1F4C1}';bB.title=t('browseTitle');bB.style.cssText='padding:2px 6px;font-size:11px';
  bB.onclick=async(e)=>{
    e.preventDefault();
    try{const r=await pywebview.api.pick_folder();if(r&&r.path)pI.value=r.path;}catch(err){}
  };
  pWrap.appendChild(bB);
  row.appendChild(pWrap);
  const del=document.createElement('button');del.className='ri-del';del.textContent='✕';del.title=t('removeTitle');
  del.onclick=e=>{e.preventDefault();row.remove();};
  row.appendChild(del);
  return row;
}

function ppAddRow(){
  document.getElementById('ppList').appendChild(_ppRow({alias:'',path:''}));
}

async function saveProjectPeers(){
  if(!_peersEditProject)return;
  const rows=Array.from(document.querySelectorAll('#ppList .rle-row')).filter(r=>r.querySelector('.pp-alias'));
  const peers=rows.map(row=>({
    alias:(row.querySelector('.pp-alias')||{}).value||'',
    path:(row.querySelector('.pp-path')||{}).value||'',
  })).filter(p=>p.alias&&p.path);
  const selfAlias=document.getElementById('pp_selfAlias').value||'';
  try{
    const r=await pywebview.api.set_project_peers(_peersEditProject,peers,selfAlias);
    if(!r||!r.ok){alert((r&&r.error)||t('alertSaveFail'));return;}
  }catch(e){alert(t('errorPrefix')+e);}
  closeProjectPeers();
}

function closeProjectPeers(){
  document.getElementById('projectPeersModal').classList.remove('open');
  _peersEditProject='';
}

// --- A: In-GUI peer tabs ---
function openPeerTabs(tid){
  tid=tid||act;if(!tid||!T[tid])return;
  _peerTabsEdit=tid;
  const scope=document.getElementById('ptScope');
  if(scope)scope.textContent='— '+(T[tid].name||tid);
  const cur=new Set(T[tid].peerTabs||[]);
  const list=document.getElementById('ptList');list.innerHTML='';
  Object.keys(T).forEach(id=>{
    if(id===tid)return;
    const row=document.createElement('label');
    row.style.cssText='display:flex;align-items:center;gap:8px;padding:6px 8px;background:var(--bg);border:1px solid var(--bd);border-radius:4px;cursor:pointer';
    const cb=document.createElement('input');cb.type='checkbox';cb.dataset.tid=id;
    if(cur.has(id))cb.checked=true;
    row.appendChild(cb);
    const info=T[id];
    const roleTxt=info.role?' ['+info.role+']':'';
    const projTxt=info.projectName?' '+info.projectName:'';
    const span=document.createElement('span');span.style.fontSize='12px';
    span.textContent=(info.name||id)+roleTxt+projTxt;
    row.appendChild(span);
    list.appendChild(row);
  });
  if(!list.children.length)list.innerHTML='<div style="color:var(--txd);font-size:11px;padding:6px">'+esc(t('alertNoOtherTabs'))+'</div>';
  document.getElementById('peerTabsModal').classList.add('open');
}

async function savePeerTabs(){
  if(!_peerTabsEdit){closePeerTabs();return;}
  const cbs=Array.from(document.querySelectorAll('#ptList input[type=checkbox]:checked'));
  const ids=cbs.map(c=>c.dataset.tid).filter(Boolean);
  try{
    const r=await pywebview.api.set_tab_peers(_peerTabsEdit,ids);
    if(!r||!r.ok){alert((r&&r.error)||t('alertSaveFail'));return;}
    if(T[_peerTabsEdit])T[_peerTabsEdit].peerTabs=r.peer_tabs||[];
  }catch(e){alert(t('errorPrefix')+e);}
  closePeerTabs();
}

function closePeerTabs(){
  document.getElementById('peerTabsModal').classList.remove('open');
  _peerTabsEdit='';
}

// --- Peer dispatch events ---
function onPeerDispatch(srcId,targetId,body,kind){
  try{console.log('[peer-dispatch]',kind,srcId,'->',targetId,body);}catch(e){}
  if(kind==='tab'){
    const te=document.querySelector('[data-tab="'+targetId+'"]');
    if(te){te.classList.add('yolo-flash');setTimeout(()=>te.classList.remove('yolo-flash'),500);}
  }
}

function onPeerInbox(srcAlias,role,body){
  try{console.log('[peer-inbox]',srcAlias,role,body);}catch(e){}
}

// --- Blocker alert (user action needed) ---
function onBlockersChanged(count,root,preview){
  _blockerRoot=root;
  const bar=document.getElementById('blockerBar');
  if(!bar)return;
  if(count>0){
    bar.style.display='flex';
    bar.innerHTML='\u{1F6A8} <b>'+count+' '+esc(t('blockerNeed'))+'</b> '
      +'<span class="blocker-root">'+esc(root.split(/[\/\\]/).pop())+'</span>'
      +'<button class="blocker-open" onclick="openBlockers()">'+esc(t('blockerOpenBtn'))+'</button>';
    try{
      // simple beep via WebAudio — no binary needed
      const ac=new (window.AudioContext||window.webkitAudioContext)();
      const o=ac.createOscillator();const g=ac.createGain();
      o.connect(g);g.connect(ac.destination);
      o.frequency.value=880;g.gain.value=0.12;
      o.start();setTimeout(()=>{o.frequency.value=660;},150);
      setTimeout(()=>{o.stop();ac.close();},350);
    }catch(e){}
  }else{
    bar.style.display='none';
  }
}
let _blockerRoot='';
async function openBlockers(){
  // Fallback: if _blockerRoot wasn't set (race between startup polling
  // and file-watcher), pull the active tab's project path as a last
  // resort. Without this, the button silently no-ops.
  if(!_blockerRoot){
    if(act&&T[act]&&T[act].projectPath){_blockerRoot=T[act].projectPath;}
    else{
      try{console.warn('[blocker] _blockerRoot empty, active tab has no projectPath');}catch(e){}
      showToast(t('alertNoProject')||'No project path');
      return;
    }
  }
  const sep=_blockerRoot.includes('\\')?'\\':'/';
  const path=_blockerRoot+sep+'coordination'+sep+'blockers.md';
  try{
    const r=await pywebview.api.read_file(path);
    const el=document.getElementById('blockerContent');
    if(r&&r.ok){el.textContent=r.content;}
    else{el.textContent=t('errorPrefix')+(r&&r.error||path);}
  }catch(e){document.getElementById('blockerContent').textContent=t('errorPrefix')+e;}
  // Header: show which project's blockers.md is being displayed so the
  // operator isn't confused when multiple project tabs are open.
  const hdrPath=document.getElementById('blockerPath');
  if(hdrPath)hdrPath.textContent=path;
  document.getElementById('blockerModal').classList.add('open');
}
function onStatusChanged(root,content){
  const panel=document.getElementById('coordStatus');
  const body=document.getElementById('coordBody');
  if(!panel||!body)return;
  panel.style.display='block';
  try{
    const s=JSON.parse(content);
    const ts=s.tool_stats||{};
    const rev=s.revenue||{};
    body.innerHTML='<b>'+esc(t('deployLabel'))+'</b>: '+(s.production_url?esc(s.production_url):'\u2014')
      +'<br><b>'+esc(t('mrrLabel'))+'</b>:\u00A5'+(rev.mrr_jpy||0).toLocaleString()
      +'<br><b>'+esc(t('sTierLabel'))+'</b>: '+(ts.tier_s||0)+'/'+(ts.total||0)
      +'<br><b>'+esc(t('qualityLabel'))+'</b>: '+(ts.avg_quality||0)+'%';
  }catch(e){body.textContent=content.slice(0,200);}
}
async function refreshCoordination(){
  if(!act)return;
  try{
    const r=await pywebview.api.get_coordination_state(act);
    if(r&&r.ok){
      if(r.deploy_status)onStatusChanged(r.project,JSON.stringify(r.deploy_status));
      // Wire _blockerRoot from the polling path as well. Without this,
      // the blocker bar's "open" button would silently no-op until the
      // file watcher fired onBlockersChanged at least once (which only
      // happens when blockers.md changes on disk after startup).
      if(r.project)_blockerRoot=r.project;
      const bar=document.getElementById('blockerBar');
      if(bar){
        const n=(r.blockers&&r.blockers.count)||0;
        if(n>0){
          bar.style.display='flex';
          const projName=(r.project||'').split(/[\/\\]/).pop();
          bar.innerHTML='\u{1F6A8} <b>'+n+' '+esc(t('blockerNeed'))+'</b> '
            +'<span class="blocker-root">'+esc(projName)+'</span>'
            +'<button class="blocker-open" onclick="openBlockers()">'+esc(t('blockerOpenBtn'))+'</button>';
        }
        else{bar.style.display='none';}
      }
    }
  }catch(e){}
  refreshCoordRoles();
  refreshCoordStatus();
}
// --- Auto Coordination controls ---
let _coordRunning=false;
function refreshCoordRoles(){
  const el=document.getElementById('coordRoles');if(!el)return;
  const roles={};
  for(const[id,ti]of Object.entries(T)){
    if(ti.role)roles[ti.role]=ti.name||id;
  }
  if(!Object.keys(roles).length){el.innerHTML='<span style="color:var(--txd)">'+esc(t('coordAssignHint'))+'</span>';return;}
  el.innerHTML=Object.entries(roles).map(([r,n])=>{
    const col=roleColor(r);const ic=roleIcon(r);
    return '<div class="cr-row"><span class="cr-dot" style="background:'+escAttr(col)+'"></span>'+(ic?esc(ic)+' ':'')+'<b>'+esc(r)+'</b>: '+esc(n)+'</div>';
  }).join('');
}
async function refreshCoordStatus(){
  try{
    const s=await pywebview.api.get_auto_coord_status();
    if(!s)return;
    _coordRunning=s.running;
    onCoordStateChange(s.running);
    const body=document.getElementById('coordBody');
    if(body&&s.running){body.textContent=t('coordCycles')+': '+s.cycles;}
    else if(body){body.textContent='';}
    if(s.log&&s.log.length){
      const el=document.getElementById('coordLog');if(!el)return;
      el.innerHTML=s.log.slice(-20).reverse().map(e=>{
        const d=new Date(e.ts*1000);const hm=d.toLocaleTimeString('ja-JP',{hour:'2-digit',minute:'2-digit',second:'2-digit'});
        return'<div class="ev-'+e.type+'">'+hm+' ['+e.type+'] '+esc(e.msg)+'</div>';
      }).join('');
    }
  }catch(e){}
}
function toggleCoordPanel(ev){
  const p=document.getElementById('coordStatus');if(!p)return;
  const c=p.classList.toggle('collapsed');
  try{pywebview.api.set_coord_collapsed(c);}catch(e){}
}
function onCoordStateChange(running){
  _coordRunning=running;
  const ind=document.getElementById('coordIndicator');
  const startBtn=document.getElementById('coordStartBtn');
  const stopBtn=document.getElementById('coordStopBtn');
  if(ind){ind.textContent=running?t('coordOn'):t('coordOff');ind.style.color=running?'#34d399':'var(--txd)';if(running)ind.classList.add('coord-running');else ind.classList.remove('coord-running');}
  if(startBtn)startBtn.style.display=running?'none':'block';
  if(stopBtn)stopBtn.style.display=running?'block':'none';
}
function onCoordEvent(type,role,msg){
  const el=document.getElementById('coordLog');if(!el)return;
  const d=new Date();const hm=d.toLocaleTimeString('ja-JP',{hour:'2-digit',minute:'2-digit',second:'2-digit'});
  const div=document.createElement('div');div.className='ev-'+type;
  div.textContent=hm+' ['+type+'] '+role+': '+msg;
  el.insertBefore(div,el.firstChild);
  while(el.children.length>30)el.removeChild(el.lastChild);
  // Update cycles
  if(type==='dispatch'||type==='report'){
    const body=document.getElementById('coordBody');
    if(body&&_coordRunning){
      const n=parseInt(body.textContent.replace(/\D/g,''))||0;
      body.textContent=t('coordCycles')+': '+(n+1);
    }
  }
}
async function coordStart(){
  try{
    const r=await pywebview.api.start_auto_coord();
    if(r&&r.ok){
      onCoordStateChange(true);
      refreshCoordRoles();
    }else{
      alert((r&&r.error)||t('coordFailStart'));
    }
  }catch(e){alert(t('errorPrefix')+e);}
}
async function coordStop(){
  try{await pywebview.api.stop_auto_coord();onCoordStateChange(false);}catch(e){}
}
function coordInjectPrompt(){
  if(!_coordRunning){alert(t('coordStartFirst'));return;}
  const inst=prompt(t('coordInjectPrompt'));
  if(!inst)return;
  pywebview.api.coord_inject(inst);
}
async function openPeerChannel(){
  // Open the active tab's project/coordination/peer_channel.md in the file
  // viewer. Peer channel is the free-form cross-role board (task-gate-less).
  if(!act||!T[act]||!T[act].projectPath){alert(t('noProjectForPeer'));return;}
  const p=T[act].projectPath+'/coordination/peer_channel.md';
  try{
    const r=await pywebview.api.read_file(p);
    if(r&&r.ok){
      document.getElementById('vwName').textContent=r.name+' — '+t('peerChannel');
      document.getElementById('vwContent').textContent=r.content;
      document.getElementById('fileViewer').classList.add('open');
    }else{
      alert(t('peerChannelMissing')+'\n\n'+p);
    }
  }catch(e){alert(t('errorPrefix')+e);}
}

// ============================================================
// Graph memory panel (Layer 3, 2026-04-27) — synapse-like activation
// ============================================================
function openGraphPanel(){
  const p=document.getElementById('graphPanel');if(!p)return;
  p.style.display='block';
  graphRefreshContext();
}
function closeGraphPanel(){
  const p=document.getElementById('graphPanel');if(p)p.style.display='none';
}
function _graphRowHtml(row){
  const score=row.score!==undefined?(' <span style="color:var(--ac);font-weight:600">'+(row.score).toFixed(2)+'</span>'):'';
  const role=row.role?' ['+row.role+']':'';
  const type=row.type?'<span style="color:var(--txd);font-size:9px">['+row.type+']</span> ':'';
  const id=row.id||'';
  const title=(row.title||'').replace(/[<>]/g,m=>m==='<'?'&lt;':'&gt;');
  return '<div class="g-row" data-id="'+id+'" style="padding:3px 4px;border-bottom:1px solid var(--bd);cursor:pointer" '
    +'onclick="graphInjectNode(\''+id.replace(/\'/g,"\\'")+'\')" '
    +'title="Click to inject this node into active tab">'
    +type+role+score+'<br><span style="color:var(--tx);font-size:10px">'+title+'</span></div>';
}
async function graphRefreshContext(){
  const ti=T[act];if(!ti){return;}
  const role=ti.role||'';
  const out=document.getElementById('graphResults');
  const stats=document.getElementById('graphStatsLine');
  if(!out)return;
  out.innerHTML='<div style="color:var(--txd);padding:6px">loading...</div>';
  try{
    const s=await pywebview.api.graph_stats();
    if(s&&s.ok){
      stats.textContent='nodes='+s.nodes+' edges='+s.edges;
    }else{
      stats.textContent='no graph DB';
      out.innerHTML='<div style="color:var(--txd);padding:6px">'
        +'Graph DB not found in current project.<br>Run: python tools/graph/ingest.py coordination/decisions_log.md</div>';
      return;
    }
    if(!role){
      out.innerHTML='<div style="color:var(--txd);padding:6px">No role on active tab — use search above.</div>';
      return;
    }
    // 1) Get seeds for this role
    const seedRes=await pywebview.api.graph_seed_for_role(role,5);
    if(!seedRes||!seedRes.ok||!seedRes.seeds||!seedRes.seeds.length){
      out.innerHTML='<div style="color:var(--txd);padding:6px">No seeds for role: '+role+'</div>';
      return;
    }
    // 2) Spread activation from those seeds
    const sp=await pywebview.api.graph_spread(seedRes.seeds,2,0.5,12);
    if(!sp||!sp.ok){
      out.innerHTML='<div style="color:#f87171;padding:6px">spread failed: '+(sp&&sp.error||'?')+'</div>';
      return;
    }
    if(!sp.rows||!sp.rows.length){
      out.innerHTML='<div style="color:var(--txd);padding:6px">No activated nodes (graph too sparse).</div>';
      return;
    }
    let h='<div style="color:var(--txd);font-size:9px;padding:3px 4px">activated for role <b>'+role+'</b> (seeds='+seedRes.seeds.length+'):</div>';
    sp.rows.forEach(r=>{h+=_graphRowHtml(r);});
    out.innerHTML=h;
  }catch(e){
    out.innerHTML='<div style="color:#f87171;padding:6px">error: '+e+'</div>';
  }
}
async function graphSearchRun(){
  const inp=document.getElementById('graphSearchInput');
  const q=inp&&inp.value?inp.value.trim():'';
  if(!q)return;
  const out=document.getElementById('graphResults');
  out.innerHTML='<div style="color:var(--txd);padding:6px">searching...</div>';
  try{
    const r=await pywebview.api.graph_search(q,15);
    if(!r||!r.ok){
      out.innerHTML='<div style="color:#f87171;padding:6px">search failed: '+(r&&r.error||'?')+'</div>';return;
    }
    if(!r.rows||!r.rows.length){
      out.innerHTML='<div style="color:var(--txd);padding:6px">no matches for: '+q+'</div>';return;
    }
    let h='<div style="color:var(--txd);font-size:9px;padding:3px 4px">search "'+q+'" — '+r.rows.length+' result(s):</div>';
    r.rows.forEach(row=>{h+=_graphRowHtml(row);});
    out.innerHTML=h;
  }catch(e){
    out.innerHTML='<div style="color:#f87171;padding:6px">error: '+e+'</div>';
  }
}
async function applyRoleEfforts(){
  // Migrate all tabs to their role's default_effort. Confirm before forcing.
  const force=confirm('Apply role default efforts to all tabs?\n\n'
    +'OK = preserve user-customized (only migrate "max" defaults).\n'
    +'Cancel = abort.');
  if(!force)return;
  try{
    const r=await pywebview.api.apply_role_default_efforts(false);
    if(!r||!r.ok){alert('apply failed: '+(r&&r.error||'?'));return;}
    let msg='Migrated '+r.n_migrated+' tab(s), skipped '+r.n_skipped+'.\n\n';
    if(r.migrated&&r.migrated.length){
      msg+='Migrated:\n';
      r.migrated.slice(0,10).forEach(m=>{
        msg+='  '+m.role+': '+m.from+' → '+m.to+'\n';
      });
      if(r.migrated.length>10)msg+='  ... and '+(r.migrated.length-10)+' more\n';
    }
    alert(msg);
  }catch(e){alert('error: '+e);}
}

async function openGraphViewer(){
  // Regenerate view.html + open in OS browser. The viewer renders the graph
  // visually (nodes + edges, force-directed) using cytoscape.js — a real
  // graph view, not just rows.
  try{
    const r=await pywebview.api.graph_open_viewer(true,300);
    if(!r||!r.ok){
      alert('viewer open failed: '+(r&&r.error||'?'));
      return;
    }
    if(r.error_open){
      alert('viewer generated at:\\n'+r.path+'\\n(open it manually — auto-open failed: '+r.error_open+')');
    }
  }catch(e){alert('viewer error: '+e);}
}

async function graphInjectNode(nodeId){
  // Pull related context for the clicked node and inject summary into the
  // active tab as a [GRAPH-CTX] message. Roles see "here are the relevant
  // surrounding nodes" without re-reading raw .md files.
  if(!act||!T[act]){return;}
  try{
    const r=await pywebview.api.graph_related(nodeId,2,8);
    if(!r||!r.ok){alert('related lookup failed');return;}
    if(!r.rows||!r.rows.length){alert('no related nodes');return;}
    let msg='[GRAPH-CTX from '+nodeId+'] depth=2 top='+r.rows.length+'\n';
    r.rows.forEach(row=>{
      msg+='  '+(row.depth||0)+'  ['+(row.type||'?')+'] '+(row.role||'')+' :: '+(row.title||'').slice(0,140)+'\n';
    });
    msg+='Use Read for full body via tools/graph/cli.py search/related.';
    await pywebview.api.pty_input(act,msg+'\n');
  }catch(e){alert('inject error: '+e);}
}
// Keypress: Enter in graph search input triggers search
document.addEventListener('DOMContentLoaded',()=>{
  const inp=document.getElementById('graphSearchInput');
  if(inp){inp.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();graphSearchRun();}});}
});

// Always-visible graph stats in the coord panel header. Auto-refreshed:
// - on tab switch (switchTab → refreshGraphStatsLine)
// - on a 15s interval (idle update; cheap single-row SQL)
async function refreshGraphStatsLine(){
  const el=document.getElementById('graphHeaderStats');
  if(!el)return;
  try{
    const s=await pywebview.api.graph_stats();
    if(s&&s.ok){
      const types=s.by_type||{};
      const dec=types.decision||0,peer=types.peer_message||0;
      el.textContent='graph: '+s.nodes+'n / '+s.edges+'e (dec '+dec+' / peer '+peer+')';
      el.style.color='var(--txd)';
    }else{
      el.textContent='graph: --';
      el.style.color='var(--txd)';
    }
  }catch(e){
    el.textContent='graph: err';
    el.style.color='#f87171';
  }
}
// Kick off periodic refresh once DOM ready (ignore until first tab settles)
setTimeout(()=>{refreshGraphStatsLine();setInterval(refreshGraphStatsLine,15000);},2000);
let _ptyStartTimes={},_ptyCrashCount={};
function onPtyStarted(tid){_ptyStartTimes[tid]=Date.now();}
function onPtyDied(tid){
  const ti=T[tid];if(!ti)return;
  ti.streaming=false;ti.permPrompt=false;
  const p=document.getElementById('tperm_'+tid);if(p)p.classList.remove('vis');
  if(tid===act)setSndBtn(false);
  const el=document.getElementById('screen_'+tid);
  const startedAt=_ptyStartTimes[tid]||0;
  const elapsed=Date.now()-startedAt;
  // If PTY died within 10s, it likely crashed or had an invalid session.
  // Auto-restart as new — but cap consecutive fast deaths so a CLI that can't
  // launch (bad flags / conpty failure) doesn't spin forever; back off too.
  if(elapsed<10000&&startedAt>0){
    _ptyCrashCount[tid]=(_ptyCrashCount[tid]||0)+1;
    ti.termActive=false;
    if(_ptyCrashCount[tid]>3){
      if(el)el.textContent+='\n\n--- '+t('sessRestartFail')+' ---\n';
      showToast(t('sessRestartFail'),'error');
      return;
    }
    if(el)el.textContent+='\n\n--- '+t('sessInvalid')+' ---\n';
    ti.sessionId='';
    pywebview.api.new_session(tid);
    setTimeout(()=>{pywebview.api.auto_start_session(tid);},Math.min(1500*_ptyCrashCount[tid],6000));
  }else{
    // Normal/long-lived exit — session ended (next message starts a new PTY,
    // with --resume if session_id exists). Reset the crash counter: the PTY
    // proved it can run past 10s, so future fast-death runs start fresh.
    _ptyCrashCount[tid]=0;
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
      // label is parsed off the PTY screen (untrusted). JSON.stringify makes a
      // valid JS string literal; escAttr() then HTML-escapes the quotes so it
      // survives the double-quoted onclick attribute. (esc() doesn't touch " —
      // it would have broken the attribute and re-opened the injection.)
      btnsHtml+='<button class="'+cls+'" onclick="respondPermChoice(\''+tid+'\','+num+','+escAttr(JSON.stringify(label))+','+(!isDeny)+')">'+num+'. '+esc(label)+'</button>';
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
async function openGlobal(){
  // Hydrate inputs from current backend state so "save without editing" doesn't
  // silently revert persisted values (e.g. mode=api → cli).
  try{
    const s=await pywebview.api.get_initial_state();
    const mode=(s&&s.mode)||'cli';
    const ms=document.getElementById('modeSelect');
    const as=document.getElementById('apiSec');
    if(ms)ms.value=mode;
    if(as)as.style.display=mode==='api'?'block':'none';
    // API key is never returned from backend (security); input stays empty and
    // only sent to backend if user types a new one.
    const ak=document.getElementById('apiKeyInput');
    if(ak)ak.value='';
    // Language is already synced via chLang; make sure dropdown matches.
    const ls=document.getElementById('langSelect');
    if(ls&&s&&s.language)ls.value=s.language;
  }catch(e){}
  document.getElementById('globalModal').classList.add('open');
}
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
  _tsCurId=act;
  document.getElementById('tsMaxTurns').value=ti.maxTurns||0;
  document.getElementById('tsFlags').value=ti.customFlags||'';
  document.getElementById('tsSysPrompt').value=ti.sysPrompt||'';
  document.getElementById('tsSessionId').textContent=ti.sessionId||'-';
  // Permission
  document.getElementById('tsPermMode').value=ti.permMode||'default';
  const at=ti.allowedTools||[];
  document.querySelectorAll('.perm-cb').forEach(cb=>{cb.checked=at.includes(cb.value);});
  togglePermTools();
  // Peer tabs summary
  const pts=ti.peerTabs||[];
  const sum=document.getElementById('tsPeerTabsSummary');
  if(sum){
    if(!pts.length){sum.textContent='None';}
    else{sum.textContent=pts.map(pid=>{const pt=T[pid];return pt?('@@'+pt.name):pid;}).join(', ');}
  }
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
  // Save to the tab the modal was opened for (_tsCurId), NOT the live `act` —
  // Ctrl+Tab can change `act` while the modal is open.
  const tid=_tsCurId;
  if(!tid||!T[tid])return;
  const mt=parseInt(document.getElementById('tsMaxTurns').value)||0;
  const fl=document.getElementById('tsFlags').value;
  const sp=document.getElementById('tsSysPrompt').value;
  const pm=document.getElementById('tsPermMode').value;
  const at=[];document.querySelectorAll('.perm-cb').forEach(cb=>{if(cb.checked)at.push(cb.value);});
  T[tid].maxTurns=mt;T[tid].customFlags=fl;T[tid].sysPrompt=sp;
  T[tid].permMode=pm;T[tid].allowedTools=at;
  await pywebview.api.set_tab_max_turns(tid,mt);
  await pywebview.api.set_tab_custom_flags(tid,fl);
  await pywebview.api.set_tab_system_prompt(tid,sp);
  await pywebview.api.set_tab_permission_mode(tid,pm);
  await pywebview.api.set_tab_allowed_tools(tid,at);
  closeTabModal();
}

// ============================================================
// Keyboard shortcuts
// ============================================================
document.addEventListener('keydown',(e)=>{
  if(e.ctrlKey&&e.key==='t'){e.preventDefault();addTab();}
  else if(e.ctrlKey&&e.key==='w'){e.preventDefault();closeCurrentTab();}
  else if(e.ctrlKey&&!e.shiftKey&&e.key==='l'){e.preventDefault();doClearDisplay();}
  else if(e.ctrlKey&&e.shiftKey&&(e.key==='L'||e.key==='l')){e.preventDefault();doEndSession();}
  else if(e.ctrlKey&&e.shiftKey&&(e.key==='N'||e.key==='n')){e.preventDefault();doNewSession();}
  else if(e.ctrlKey&&!e.shiftKey&&e.key==='Tab'){e.preventDefault();cycleTab(1);}
  else if(e.ctrlKey&&e.shiftKey&&e.key==='Tab'){e.preventDefault();cycleTab(-1);}
  else if(e.key==='Escape'){
    // Modal-first: if any modal is open, close it (even in terminal mode).
    // Otherwise terminal ESC is trapped forever while a modal is showing.
    const anyModalOpen=!!document.querySelector('.mo.open')
      || document.getElementById('fileViewer')?.classList.contains('open');
    if(anyModalOpen){
      closeVw();closeGlobal();closeTabModal();closeRolesEditor();closeProjectPeers();closePeerTabs();dismissRoleProposal();dismissRoleRequest();document.getElementById('permModal').classList.remove('open');document.getElementById('blockerModal').classList.remove('open');
      return;
    }
    // Terminal mode: send Escape to PTY (interrupts Claude Code)
    if(act&&T[act]&&T[act].termActive){pywebview.api.pty_input(act,'\x1b');return;}
    // Stop streaming
    if(act&&T[act]&&T[act].streaming){pywebview.api.stop_streaming(act);return;}
  }
  // Backspace fallback: when focus drifted off msgIn (e.g. clicked the PTY
  // <pre> to copy), forward Backspace to PTY so the queued-send buffer in
  // Claude Code CLI can be edited. Skip if focus is on any editable element
  // (their own Backspace handling wins). hKey on msgIn already forwards when
  // msgIn is empty and stopPropagation()s, so this only fires for non-msgIn
  // focus paths.
  else if(e.key==='Backspace'){
    if(!(act&&T[act]&&T[act].termActive))return;
    const ae=document.activeElement;
    const editable=ae&&(ae.tagName==='INPUT'||ae.tagName==='TEXTAREA'||ae.isContentEditable);
    if(editable)return;
    const inp=document.getElementById('msgIn');
    if(inp&&inp.value)return;
    e.preventDefault();
    pywebview.api.pty_input(act,'\x7f');
  }
});

// ============================================================
// Helpers
// ============================================================
function scrollBot(tid){const p=document.getElementById('chat_'+tid);if(p)p.scrollTop=p.scrollHeight;}
function updateHint(){}
function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML;}
// esc() (textContent->innerHTML) escapes & < > but NOT quotes. For values that
// land inside a double-quoted HTML attribute (e.g. an onclick="..." JS string),
// use escAttr() so " and ' can't break out of / inject into the attribute.
function escAttr(s){return String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/'/g,'&#39;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
// Simple top-right toast. Reuses a single #_toast container, auto-dismisses
// after 4s. Stack via multiple entries if called rapidly (keeps visible so
// the user can read all three-or-so recent errors).
function showToast(msg,kind){
  if(!msg)return;
  let wrap=document.getElementById('_toastWrap');
  if(!wrap){
    wrap=document.createElement('div');
    wrap.id='_toastWrap';
    wrap.style.cssText='position:fixed;top:16px;right:16px;z-index:999;display:flex;flex-direction:column;gap:6px;pointer-events:none;max-width:360px';
    document.body.appendChild(wrap);
  }
  const el=document.createElement('div');
  const bg=kind==='error'?'#7a1f1f':(kind==='success'?'#14532d':'#1f2937');
  const bd=kind==='error'?'#b91c1c':(kind==='success'?'#15803d':'#374151');
  el.style.cssText='background:'+bg+';border:1px solid '+bd+';color:#fff;padding:8px 14px;border-radius:6px;font-size:12px;box-shadow:0 4px 16px rgba(0,0,0,.4);pointer-events:auto;animation:fi .2s';
  el.textContent=msg;
  wrap.appendChild(el);
  setTimeout(()=>{el.style.opacity='0';el.style.transition='opacity .3s';setTimeout(()=>el.remove(),300);},4000);
}
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
  // NOTE: code blocks are restored LAST (after all block/inline transforms +
  // the \n->\<br> step) so header/list/bold/link regexes can't corrupt code,
  // and code can't be routed back through the link regex.
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
  // Lists — ordered items carry a marker class so each run picks <ol> vs <ul>.
  h=h.replace(/^- (.+)$/gm,'<li>$1</li>');
  h=h.replace(/^\d+\. (.+)$/gm,'<li class="lo">$1</li>');
  h=h.replace(/((?:\n?<li(?: class="lo")?>.*?<\/li>)+)/g,function(run){
    const ordered=/^\n?<li class="lo">/.test(run);
    const inner=run.replace(/ class="lo"/g,'');
    return ordered?('<ol>'+inner+'</ol>'):('<ul>'+inner+'</ul>');
  });
  // HR
  h=h.replace(/^---+$/gm,'<hr>');
  // Links —: scheme allow-list (http/https/mailto/tel/#) + rel=noopener (reverse-tabnabbing)
  h=h.replace(/\[([^\]]+)\]\(([^)]+)\)/g,(m,text,url)=>{
    if(!/^(https?:\/\/|mailto:|tel:|#)/i.test(url)) return text;
    // url comes from esc()'d text so &<> are entities, but " is not — escape it
    // so it can't break out of the double-quoted href and inject an attribute.
    return `<a href="${url.replace(/"/g,'&quot;')}" style="color:var(--acl)" target="_blank" rel="noopener noreferrer">${text}</a>`;
  });
  // Newlines
  h=h.replace(/\n/g,'<br>');
  // Restore code blocks LAST (function replacer so $&, $`, $$ etc. inside code
  // are inserted literally, not interpreted as replacement patterns).
  for(let i=0;i<blocks.length;i++){h=h.replace('%%CB'+i+'%%',()=>blocks[i]);}
  // Clean br around block elements
  h=h.replace(/<br>\s*(<\/?(?:h[1-6]|ul|ol|li|pre|hr))/g,'$1');
  h=h.replace(/(<\/(?:h[1-6]|ul|ol|li|pre|hr)>)\s*<br>/g,'$1');
  return h;
}

// ============================================================
// Slash command autocomplete
// ============================================================
const SLASH_CMDS=[
// --- handled in-app (see Api.handle_slash) ---
{cmd:'/clear',d:{en:'Clear display',ja:'表示クリア',zh:'清除显示',ko:'표시 지우기'}},
{cmd:'/compact',d:{en:'Compact conversation',ja:'コンパクト化',zh:'压缩对话',ko:'대화 압축'}},
{cmd:'/effort',d:{en:'Set effort level',ja:'努力レベル設定',zh:'设置努力级别',ko:'노력 수준 설정'},args:['max','xhigh','high','medium','low']},
{cmd:'/model',d:{en:'Change model',ja:'モデル変更',zh:'切换模型',ko:'모델 변경'},args:['opus','sonnet','haiku','opus4.8','opus4.7','opus4.6','opus4','sonnet4']},
{cmd:'/status',d:{en:'Session info',ja:'セッション情報',zh:'会话信息',ko:'세션 정보'}},
{cmd:'/help',d:{en:'Show help',ja:'ヘルプ表示',zh:'显示帮助',ko:'도움말'}},
// --- forwarded to the Claude Code CLI ---
{cmd:'/resume',d:{en:'Resume a past session',ja:'過去のセッション再開',zh:'恢复历史会话',ko:'이전 세션 재개'}},
{cmd:'/context',d:{en:'Show context usage',ja:'コンテキスト使用量',zh:'显示上下文用量',ko:'컨텍스트 사용량'}},
{cmd:'/cost',d:{en:'Show token cost',ja:'コスト表示',zh:'显示费用',ko:'비용 표시'}},
{cmd:'/usage',d:{en:'Plan usage & limits',ja:'プラン使用量・上限',zh:'用量与限制',ko:'사용량·한도'}},
{cmd:'/config',d:{en:'Settings',ja:'設定',zh:'设置',ko:'설정'}},
{cmd:'/permissions',d:{en:'Permission settings',ja:'権限設定',zh:'权限设置',ko:'권한 설정'}},
{cmd:'/hooks',d:{en:'Manage hooks',ja:'フック管理',zh:'管理钩子',ko:'후크 관리'}},
{cmd:'/mcp',d:{en:'MCP servers',ja:'MCPサーバー',zh:'MCP服务器',ko:'MCP 서버'}},
{cmd:'/agents',d:{en:'Manage subagents',ja:'サブエージェント管理',zh:'管理子代理',ko:'서브에이전트 관리'}},
{cmd:'/memory',d:{en:'Edit memory (CLAUDE.md)',ja:'メモリ編集',zh:'编辑记忆',ko:'메모리 편집'}},
{cmd:'/add-dir',d:{en:'Add a working directory',ja:'作業ディレクトリ追加',zh:'添加工作目录',ko:'작업 디렉터리 추가'}},
{cmd:'/init',d:{en:'Init CLAUDE.md',ja:'CLAUDE.md初期化',zh:'初始化CLAUDE.md',ko:'CLAUDE.md 초기화'}},
{cmd:'/review',d:{en:'Review a pull request',ja:'PRレビュー',zh:'审查PR',ko:'PR 리뷰'}},
{cmd:'/code-review',d:{en:'Review the current diff',ja:'差分レビュー',zh:'审查当前改动',ko:'변경 리뷰'}},
{cmd:'/security-review',d:{en:'Security review of changes',ja:'セキュリティレビュー',zh:'安全审查',ko:'보안 리뷰'}},
{cmd:'/simplify',d:{en:'Simplify changed code',ja:'コード簡素化',zh:'简化改动代码',ko:'코드 단순화'}},
{cmd:'/pr-comments',d:{en:'Get PR comments',ja:'PRコメント取得',zh:'获取PR评论',ko:'PR 댓글 가져오기'}},
{cmd:'/verify',d:{en:'Verify a change works',ja:'変更動作検証',zh:'验证改动',ko:'변경 검증'}},
{cmd:'/run',d:{en:'Run the app',ja:'アプリ起動',zh:'运行应用',ko:'앱 실행'}},
{cmd:'/deep-research',d:{en:'Deep research report',ja:'詳細リサーチ',zh:'深度研究',ko:'심층 리서치'}},
{cmd:'/frontend-design',d:{en:'Build a UI',ja:'UI作成',zh:'构建界面',ko:'UI 생성'}},
{cmd:'/loop',d:{en:'Run a task on a loop',ja:'繰り返し実行',zh:'循环执行任务',ko:'반복 실행'}},
{cmd:'/schedule',d:{en:'Schedule a recurring agent',ja:'定期エージェント',zh:'计划定期代理',ko:'예약 에이전트'}},
{cmd:'/remember',d:{en:'Save a memory',ja:'メモリ保存',zh:'保存记忆',ko:'메모리 저장'}},
{cmd:'/remote-control',d:{en:'Enable remote control',ja:'リモート制御有効化',zh:'启用远程控制',ko:'원격 제어 활성화'}},
{cmd:'/export',d:{en:'Export conversation',ja:'会話エクスポート',zh:'导出对话',ko:'대화 내보내기'}},
{cmd:'/ide',d:{en:'Connect to IDE',ja:'IDE接続',zh:'连接IDE',ko:'IDE 연결'}},
{cmd:'/terminal-setup',d:{en:'Configure terminal',ja:'ターミナル設定',zh:'配置终端',ko:'터미널 설정'}},
{cmd:'/todos',d:{en:'List current todos',ja:'TODO一覧',zh:'查看待办',ko:'할 일 목록'}},
{cmd:'/rewind',d:{en:'Restore a checkpoint',ja:'チェックポイント復元',zh:'恢复检查点',ko:'체크포인트 복원'}},
{cmd:'/vim',d:{en:'Toggle vim mode',ja:'Vimモード',zh:'Vim模式',ko:'Vim 모드'}},
{cmd:'/doctor',d:{en:'Health check',ja:'診断チェック',zh:'健康检查',ko:'진단 체크'}},
{cmd:'/bug',d:{en:'Report a bug',ja:'バグ報告',zh:'报告bug',ko:'버그 보고'}},
{cmd:'/release-notes',d:{en:'Show release notes',ja:'リリースノート',zh:'发布说明',ko:'릴리스 노트'}},
{cmd:'/upgrade',d:{en:'Upgrade your plan',ja:'プランアップグレード',zh:'升级套餐',ko:'플랜 업그레이드'}},
{cmd:'/login',d:{en:'Log in',ja:'ログイン',zh:'登录',ko:'로그인'}},
{cmd:'/logout',d:{en:'Log out',ja:'ログアウト',zh:'登出',ko:'로그아웃'}},
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
  popup.innerHTML=acItems.map((it,i)=>'<div class="ac-item" data-i="'+i+'" onmousedown="selectAc('+i+')"><span class="ac-cmd">'+esc(it.cmd)+'</span><span class="ac-desc">'+esc(it.d[curLang]||it.d.en)+'</span></div>').join('');
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
    print("[boot] main() start", flush=True)
    print("[boot] creating Api()...", flush=True)
    api = Api()
    print(f"[boot] Api() ready, tabs={len(api._tabs)}", flush=True)
    # Inject the full i18n catalogue as window.I18N so the JS layer's
    # mergeI18n()/updateUI()/applyI18n() can translate any [data-i18n*]
    # attribute without a separate API roundtrip. Encoded via json.dumps so
    # non-ASCII chars stay as \uXXXX escapes and HTML special chars
    # (</script>, etc.) cannot break out of the injected inline script.
    i18n_json = json.dumps(_STRINGS, ensure_ascii=True)
    inject = (
        "<script>window.I18N=" + i18n_json
        + ";window.I18N_DEFAULT_LANG="
        + json.dumps(api._config.get("language", "") or "")
        + ";</script>"
    )
    # Place the inject block right before </head> so it runs before any
    # body-level <script> (including the main i18n merge).
    html_with_i18n = HTML.replace("</head>", inject + "</head>", 1)
    print("[boot] creating window...", flush=True)
    # DIAGNOSTIC: check if --no-api flag is set to skip js_api binding
    # (helps isolate whether pywebview is hanging on Api class introspection)
    use_api = "--no-api" not in sys.argv
    print(f"[boot]   js_api enabled: {use_api}", flush=True)
    window = webview.create_window(
        title="Claude Code GUI",
        html=html_with_i18n if use_api else "<h1>NO-API DIAGNOSTIC</h1>",
        js_api=api if use_api else None,
        width=1200,
        height=800,
        x=100,           # force on-screen position (avoid off-screen restore)
        y=100,
        min_size=(800, 500),
        background_color="#0a0a1a",
        text_select=True,
        on_top=False,    # 26-04-29 user directive: 最前面表示無効化 (user 操作邪魔解消)
    )
    api._window = window
    print("[boot] webview.start() — GUI window should appear now", flush=True)
    webview.start(debug=True)  # force debug to surface pywebview internal logs
    print("[boot] webview exited", flush=True)
    # force=True: the final save MUST bypass the debounce, else a reader-thread
    # persist <2s earlier makes this a no-op and on-close screen capture +
    # last-moment tab/session changes are lost (screen-restore-on-relaunch).
    api._persist(force=True)


def _yolo_self_test() -> None:
    """Logic-only test for `_eval_yolo_prompt`. Invoke via:
        python -c "import sys; sys.path.insert(0, 'claude-code-gui'); from main import _yolo_self_test; _yolo_self_test()"

    The helper is a pure function so no PtySession / webview init is needed.
    """
    # Case 1: safe bash — accept
    prompt1 = ("╭─────╮\n│ bash: ls -la │\n│ ❯ 1. yes │\n"
               "│   2. no │\n╰─────╯").lower()
    ok, r, p = _eval_yolo_prompt(prompt1, [[1, "Yes"], [2, "No"]])
    assert ok, f"case1 expected accept, got {r} {p}"

    # Case 2: rm -rf — refuse with danger_keyword
    prompt2 = "│ rm -rf /tmp/x │\n│ ❯ 1. yes │\n│   2. no │".lower()
    ok, r, p = _eval_yolo_prompt(prompt2, [[1, "Yes"], [2, "No"]])
    assert not ok and r == "danger_keyword" and "rm -rf" in p.get("kw", ""), f"case2 {ok} {r} {p}"

    # Case 3: cat .env — accept (.env removed from danger list, intentional)
    prompt3 = "│ cat .env │\n│ ❯ 1. yes │\n│   2. no │".lower()
    ok, r, p = _eval_yolo_prompt(prompt3, [[1, "Yes"], [2, "No"]])
    assert ok, f"case3 expected accept (.env removed), got {r} {p}"

    # Case 4: no_approve_marker — first choice is "No, stop"
    prompt4 = "│ ❯ 1. no, stop │\n│   2. yes │".lower()
    ok, r, p = _eval_yolo_prompt(prompt4, [[1, "No, stop"], [2, "Yes"]])
    assert not ok and r == "no_approve_marker", f"case4 {ok} {r} {p}"

    # Case 5: git push --force — refuse
    prompt5 = "│ git push --force origin main │\n│ ❯ 1. yes │".lower()
    ok, r, p = _eval_yolo_prompt(prompt5, [[1, "Yes"], [2, "No"]])
    assert not ok and r == "danger_keyword", f"case5 {ok} {r} {p}"

    # Case 6: npm publish — refuse
    prompt6 = "│ npm publish │\n│ ❯ 1. yes │".lower()
    ok, r, p = _eval_yolo_prompt(prompt6, [[1, "Yes"], [2, "No"]])
    assert not ok and r == "danger_keyword", f"case6 {ok} {r} {p}"

    # Case 7: 'accept' marker restored — should work
    prompt7 = "│ ls │\n│ ❯ 1. accept │".lower()
    ok, r, p = _eval_yolo_prompt(prompt7, [[1, "Accept"], [2, "Reject"]])
    assert ok, f"case7 accept marker should work, got {r} {p}"

    print("[yolo-self-test] 7/7 passed")


def _quiesce_self_test() -> None:
    """Logic-only test for `_eval_quiescence`. Invoke via:
        python -c "import sys; sys.path.insert(0, 'claude-code-gui'); from main import _quiesce_self_test; _quiesce_self_test()"

    The helper is pure so no PtySession / webview init is needed.
    """
    # Case 1: empty screen + no activity → no_prompt_visible
    ok, r = _eval_quiescence("", 0.0, 100.0, 2.0)
    assert not ok and r == "no_prompt_visible", f"case1 {ok} {r}"

    # Case 2: prompt glyph visible, 3 seconds since last change → quiescent
    screen2 = "│ ❯                                           │\n╰───────────────────╯"
    ok, r = _eval_quiescence(screen2, 97.0, 100.0, 2.0)
    assert ok and r == "", f"case2 {ok} {r}"

    # Case 3: spinner char + recent activity → busy_generating
    screen3 = "Generating response ⠋ processing...\n│ ❯ │"
    ok, r = _eval_quiescence(screen3, 99.5, 100.0, 2.0)
    assert not ok and r == "busy_generating", f"case3 {ok} {r}"

    # Case 4: "esc to interrupt" with RECENT activity → busy (active stream).
    screen4 = "│ Claude is working on response (esc to interrupt) │\n│ ❯ │"
    ok, r = _eval_quiescence(screen4, 99.0, 100.0, 2.0)
    assert not ok and r == "busy_generating", f"case4 {ok} {r}"

    # Case 4b: the SAME busy marker but the screen is static for
    # >2*quiesce → it's a stale scrollback marker (a live stream would animate
    # the spinner), so screen_stable wins. This is the intended behavior.
    ok, r = _eval_quiescence(screen4, 90.0, 100.0, 2.0)
    assert ok and r == "screen_stable", f"case4b {ok} {r}"

    # Case 5: permission prompt "Do you want" → permission_prompt (highest priority)
    screen5 = "╭──────╮\n│ Do you want to proceed? │\n│ 1. Yes  2. No │\n╰──╯"
    ok, r = _eval_quiescence(screen5, 97.0, 100.0, 2.0)
    assert not ok and r == "permission_prompt", f"case5 {ok} {r}"

    # Case 5b (safety): a STATIC permission prompt (no animation, last change
    # 10s ago) must STILL block — the permission check runs BEFORE the
    # screen_stable short-circuit, else coord would write onto an open menu.
    ok, r = _eval_quiescence(screen5, 90.0, 100.0, 2.0)
    assert not ok and r == "permission_prompt", f"case5b {ok} {r}"

    print("[quiesce-self-test] 7/7 passed")


def _routing_self_test() -> None:
    """Logic-only test for `_role_from_tab_display` + `_role_effective`.

    Invoke via:
        python -c "import sys; sys.path.insert(0, 'claude-code-gui'); from main import _routing_self_test; _routing_self_test()"

    Covers 7 deterministic cases + 1 deferred (ROUTING-AUDIT, Phase 3 dormant).
    """
    passed = 0
    # Case 1: emoji-prefixed tab name "👑 CCO" → "cco"
    got = _role_from_tab_display("\U0001F451 CCO")
    assert got == "cco", f"case1 expected 'cco', got {got!r}"
    passed += 1
    # Case 2: compound emoji "👨‍💻 Developer" → "developer"
    got = _role_from_tab_display("\U0001F468‍\U0001F4BB Developer")
    assert got == "developer", f"case2 expected 'developer', got {got!r}"
    passed += 1
    # Case 3: no icon "Reviewer" → "reviewer"
    got = _role_from_tab_display("Reviewer")
    assert got == "reviewer", f"case3 expected 'reviewer', got {got!r}"
    passed += 1
    # Case 4: variant "Dev-01" → "developer" via substring match
    got = _role_from_tab_display("Dev-01")
    assert got == "developer", f"case4 expected 'developer' (substring), got {got!r}"
    passed += 1
    # Case 5: empty string → None
    got = _role_from_tab_display("")
    assert got is None, f"case5 expected None, got {got!r}"
    passed += 1
    # Case 6: non-matching "Xyzzy Plugh" → None
    got = _role_from_tab_display("Xyzzy Plugh")
    assert got is None, f"case6 expected None, got {got!r}"
    passed += 1
    # Case 7: _role_effective fallback when display derivation fails
    #   (display returns None, falls back to tab.role)
    class _MockTab:
        def __init__(self, name, role):
            self.name = name
            self.role = role
    t_stale = _MockTab(name="", role="analyst")
    assert _role_effective(t_stale) == "analyst", "case7a: empty name → fallback to tab.role"
    t_drift = _MockTab(name="\U0001F440 Reviewer", role="")
    assert _role_effective(t_drift) == "reviewer", "case7b: display derives when tab.role empty"
    t_prefer = _MockTab(name="\U0001F9E0 CCO", role="manager")
    assert _role_effective(t_prefer) == "cco", "case7c: display wins over stale tab.role"
    passed += 1
    # Case 8: [ROUTING-AUDIT] ACK timeout → re-fire (Phase 3 dormant)
    # Deferred: ROUTING_AUDIT_ENABLED is False by default; code path exists but
    # is not activated. Will be enabled post user review in a separate dispatch.
    assert ROUTING_AUDIT_ENABLED is False, "case8: ROUTING_AUDIT_ENABLED must default False"
    print(f"[routing-self-test] {passed}/7 passed (case 8 deferred pending ROUTING_AUDIT_ENABLED)")


def _taxonomy_self_test() -> None:
    """AutoCoord scope-based keyword → role taxonomy 8-case test.

    Verifies §2 (scope discrimination) + §3.3 (fallthrough). Body strings
    are inputs to AutoCoordinator._scope_match_targets; expected target list
    is the set of (target_role, kw) pairs the routing should fire.

    Invoke via: python claude-code-gui/main.py --taxonomy-test
    """
    passed = 0
    AC = AutoCoordinator
    # Case 1: 景表 in mkt body → legal (the original recurring misroute)
    out = AC._scope_match_targets("景表 法 違反", "marketing")
    assert ("legal", "景表") in out, f"case1 expected legal/景表, got {out!r}"
    assert all(t != "security" for t, _ in out), f"case1 must NOT route to sec, got {out!r}"
    passed += 1
    # Case 2: 金商 in rev body → legal
    out = AC._scope_match_targets("金商 法 28-3 disclaimer", "revops")
    assert ("legal", "金商") in out, f"case2 expected legal/金商, got {out!r}"
    passed += 1
    # Case 3: ssrf in dev body → security (preserved valid sec route)
    out = AC._scope_match_targets("ssrf hardening api-tester regex", "developer")
    assert ("security", "ssrf") in out, f"case3 expected security/ssrf, got {out!r}"
    passed += 1
    # Case 4: secret-leak in dev body → security (preserved)
    out = AC._scope_match_targets("secret-leak detected in env-gen", "developer")
    assert ("security", "secret-leak") in out, f"case4 expected security/secret-leak, got {out!r}"
    passed += 1
    # Case 5: aria in dsn body → no self-route (designer scope but src=designer)
    out = AC._scope_match_targets("aria role region polish wave", "designer")
    assert all(t != "designer" for t, _ in out), f"case5 must suppress self-CC, got {out!r}"
    passed += 1
    # Case 6: playwright in qa body → no self-route
    out = AC._scope_match_targets("playwright headless smoke 5/5 PASS", "qa")
    assert all(t != "qa" for t, _ in out), f"case6 must suppress self-CC, got {out!r}"
    passed += 1
    # Case 7: SLO in sre body → no self-route; multi-scope conflict → all cc'd
    out = AC._scope_match_targets("slo refresh + 景表 内 spot-check", "sre")
    assert all(t != "sre" for t, _ in out), f"case7a must suppress self-CC, got {out!r}"
    assert ("legal", "景表") in out, f"case7b multi-scope: legal must still match, got {out!r}"
    passed += 1
    # Case 8: MRR in rvo body → no self; mrr is legal-clean (no false legal hit)
    out = AC._scope_match_targets("mrr p50 ¥80K cohort", "revops")
    assert all(t != "revops" for t, _ in out), f"case8a must suppress self-CC, got {out!r}"
    # mrr matches scope=revops only — but src=revops so no hint emitted (acceptable)
    targets = {t for t, _ in out}
    assert "legal" not in targets, f"case8b: 'mrr' must not false-fire on legal scope, got {out!r}"
    passed += 1
    # Case 9 ( v0.2): 個人情報 in mkt body → legal (gap close)
    out = AC._scope_match_targets("個人情報 取扱規程 update", "marketing")
    assert ("legal", "個人情報") in out, f"case9 expected legal/個人情報, got {out!r}"
    assert all(t != "security" for t, _ in out), f"case9 must NOT route to sec, got {out!r}"
    passed += 1
    # Case 10 ( v0.2): プライバシー in dev body → legal
    out = AC._scope_match_targets("プライバシーポリシー link 確認", "developer")
    assert ("legal", "プライバシー") in out, f"case10 expected legal/プライバシー, got {out!r}"
    passed += 1
    # Case 11 ( v0.2): privacy policy English in mkt body → legal
    out = AC._scope_match_targets("privacy policy compliance audit", "marketing")
    assert ("legal", "privacy policy") in out, f"case11 expected legal/privacy policy, got {out!r}"
    passed += 1
    # Case 12 ( v0.2): 要配慮 / 医療情報 in dev body → legal scope routes (first-hit semantics OK)
    out = AC._scope_match_targets("要配慮個人情報 + 医療情報 storage", "developer")
    targets12 = {t for t, _ in out}
    assert "legal" in targets12, f"case12 expected legal scope route, got {out!r}"
    passed += 1
    # Case 13 ( v0.2): 法的 in qa body → legal
    out = AC._scope_match_targets("法的 boundary cross-check needed", "qa")
    assert ("legal", "法的") in out, f"case13 expected legal/法的, got {out!r}"
    passed += 1
    # Case 14 ( v0.2 negative): pure technical body → no legal hint
    out = AC._scope_match_targets("python ast parse + numpy ufunc benchmark", "developer")
    assert all(t != "legal" for t, _ in out), f"case14 must NOT fire legal on pure-tech body, got {out!r}"
    passed += 1
    print(f"[taxonomy-self-test] {passed}/14 passed")


def _rename_self_test() -> None:
    """Logic-only test for `_role_display_name` + AUTO_RENAME_ON_ROLE_SET feature.

    Covers acceptance: `/role <name>` (set_tab_role) auto-renames the
    tab to "<icon> <RoleName>" derived from the registry, preserving the
    legacy path when AUTO_RENAME_ON_ROLE_SET is False.

    Invoke via: python claude-code-gui/main.py --rename-test
    """
    passed = 0
    # Case 1: typical role with icon
    got = _role_display_name({"id": "developer", "name": "Developer", "icon": "\U0001F4BB"})
    assert got == "\U0001F4BB Developer", f"case1 expected emoji+Developer, got {got!r}"
    passed += 1
    # Case 2: role without icon
    got = _role_display_name({"id": "qa", "name": "QA", "icon": ""})
    assert got == "QA", f"case2 expected 'QA', got {got!r}"
    passed += 1
    # Case 3: missing name → empty (caller skips rename)
    got = _role_display_name({"id": "developer", "icon": "\U0001F4BB"})
    assert got == "", f"case3 expected '', got {got!r}"
    passed += 1
    # Case 4: None role_def → empty
    got = _role_display_name(None)
    assert got == "", f"case4 expected '', got {got!r}"
    passed += 1
    # Case 5: whitespace-only icon stripped, name kept
    got = _role_display_name({"id": "manager", "name": "Manager", "icon": "  "})
    assert got == "Manager", f"case5 expected 'Manager' (icon stripped), got {got!r}"
    passed += 1
    # Case 6: feature flag default state
    assert AUTO_RENAME_ON_ROLE_SET is True, "case6: AUTO_RENAME_ON_ROLE_SET must default True"
    passed += 1
    # Case 7: round-trip — display derived back to id
    got_name = _role_display_name({"id": "reviewer", "name": "Reviewer", "icon": "\U0001F440"})
    got_id = _role_from_tab_display(got_name)
    assert got_id == "reviewer", f"case7 round-trip expected 'reviewer', got {got_id!r}"
    passed += 1
    print(f"[rename-self-test] {passed}/7 passed")


if __name__ == "__main__":
    if "--yolo-test" in sys.argv:
        _yolo_self_test()
    elif "--quiesce-test" in sys.argv:
        _quiesce_self_test()
    elif "--md-watch-test" in sys.argv:
        result = _md_watch_self_exclusion_test()
        for line in result["details"]:
            print(line)
        total = result["passed"] + result["failed"]
        print(f"[md-watch-test] {result['passed']}/{total} passed")
        sys.exit(0 if result["failed"] == 0 else 1)
    elif "--routing-test" in sys.argv:
        _routing_self_test()
    elif "--rename-test" in sys.argv:
        _rename_self_test()
    elif "--taxonomy-test" in sys.argv:
        _taxonomy_self_test()
    else:
        main()
