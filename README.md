# Claude Code GUI

**ターミナル不要で Claude Code の全機能を使えるデスクトップアプリ。**
マルチタブで複数プロジェクトを並行開発。ブラウザ不要、ネイティブウィンドウ。

![Python](https://img.shields.io/badge/Python-3.10+-blue)
![License](https://img.shields.io/badge/License-MIT-green)

## 特徴

- **マルチタブ** — 複数の会話を並行して実行。各タブに独立したプロジェクト・セッション。
- **ブラウザ不要** — PyWebviewによるネイティブウィンドウ。Electron不要、軽量。
- **APIキー不要** — Claude Code CLI バックエンドなら Max Plan のログインで利用可能。
- **CLI自動アップデート** — GUI起動時に `claude update` をバックグラウンド実行し、常に最新の Claude Code CLI で動作。設定でオフ可。
- **全モデル対応** — バージョン固定（Opus 5.5 / Sonnet 5.5 / Haiku 5.5 / Fable 5.1 / Fable 5 / Opus 5 / Sonnet 5 / Opus 4.8〜4.0 / Sonnet 4.6〜4.0 / Haiku 4.5）と、CLIが解決するエイリアス（`default` / `best` / `opus` / `sonnet` / `haiku` / `fable` / `opusplan` / `opus[1m]` / `sonnet[1m]`）をタブごとに切替。GUIで変更すると実行中セッションにも即反映。新規タブは Opus 5.5。APIモードではエイリアスを各ファミリーの最新の固定バージョンに自動変換。
- **Effort制御** — Low / Medium / High / XHigh / Max / Auto。`--effort` フラグで起動時に適用、変更時も即反映。
- **セッション切替** — Ultracode（動的ワークフロー編成）/ Fast モード / 出力スタイル / サンドボックスを実行中セッションへ即時適用。
- **権限モード6種** — Manual / Accept Edits / Plan / Auto / Don't Ask / Bypass Permissions。Plan・Accept Edits・Auto はナビバーからワンクリック切替。ツール単位の許可リストにも対応。
- **起動オプションパネル** — `--append-system-prompt` `--tools` `--disallowedTools` `--fallback-model` `--autocompact` `--agent` `--mcp-config` `--worktree` `--restricted` `--safe-mode` など主要な `claude` 起動フラグをGUIから設定。**実際に渡される argv をプレビュー表示**。
- **コマンドパレット** — `Ctrl+K` で Claude Code の全コマンド（117件）をカテゴリ別に検索・実行。Enter で入力欄に挿入、Shift+Enter で即送信。
- **動的パーミッションボタン** — CLIの実際の選択肢をPTY画面からパースして動的にボタン生成。フィードバック評価にも対応。
- **セッション管理** — クリア（表示のみ）/ セッション終了（PTY停止、履歴保持）/ 新規セッション（全リセット）の3段階。
- **Remote Control** — `--remote-control` で起動し、claude.ai から同じセッションを操作可能。
- **ストリーミング応答** — リアルタイムで回答が表示。バックグラウンドタブにも通知ドット。
- **ファイルツリー** — プロジェクト構造をサイドバーで確認、ワンクリックで閲覧。
- **セッション継続** — `--resume` で会話の文脈を維持。アプリ再起動後も復元。
- **レスポンシブUI** — ウィンドウサイズに応じてナビバーが自動調整。小画面でもボタンが崩れない。
- **i18n** — English / 日本語 / 中文 / 한국어 対応。
- **ローカル完結** — データは外部に送信されません（Claude APIへの通信を除く）。

## 動作モード

| モード | 認証 | 特徴 |
|--------|------|------|
| **CLI** (デフォルト) | Claude Code ログイン済みでOK | Max Plan ($100-200/月) の範囲で利用。APIキー不要 |
| **API** (フォールバック) | `sk-ant-` APIキー必要 | 従量課金。CLI未インストール時の代替 |

## 起動方法

### ワンクリック起動（Python不要）

```
launch.bat をダブルクリック
```

Pythonがなくてもポータブル版を自動ダウンロード→依存関係インストール→起動まで全自動。

### Python環境がある場合

```bash
git clone https://github.com/Wolemp/claude-code-gui.git
cd claude-code-gui
pip install -r requirements.txt
python main.py
```

### .exe ビルド（配布用）

```bash
build.bat をダブルクリック
```

`dist/ClaudeCodeGUI.exe` が生成されます。Python不要で配布可能。

### 必要なもの

- **CLIモード**: `npm i -g @anthropic-ai/claude-code` でCLIをインストール → `claude login` でログイン
- **APIモード** (オプション): Anthropic APIキー（[console.anthropic.com](https://console.anthropic.com/)）

## 使い方

1. 起動するとネイティブウィンドウが開く
2. 左サイドバーの「プロジェクトを選択」でフォルダを開く
3. トップバーでモデル・Effortを選択
4. チャット欄にメッセージを入力して Enter で送信
5. `+` ボタンや Ctrl+T で新しいタブを追加して並行作業

### ナビバー

入力欄の下にあるナビバーで主要操作にアクセス:

| ボタン | ショートカット | 動作 |
|--------|---------------|------|
| ⎚ クリア | `Ctrl+L` | 表示のみクリア（セッション継続） |
| ⏹ セッション終了 | `Ctrl+Shift+L` | PTY停止、履歴保持 |
| ↻ 新規セッション | `Ctrl+Shift+N` | 全リセット |
| Plan | - | 読み取り専用モードに切替（トグル） |
| Accept Edits | - | ファイル操作のみ許可モードに切替（トグル） |
| Auto | - | 全自動（安全分類器付き）モードに切替（トグル） |
| ⌨ コマンドパレット | `Ctrl+K` | Claude Code の全コマンドを検索して実行 |

### キーボードショートカット

| キー | 動作 |
|------|------|
| `Ctrl + K` | コマンドパレット |
| `Enter` | メッセージ送信 / 選択メニュー確定 |
| `Shift + Enter` | 改行 |
| `↑` `↓` | 選択メニューの項目を移動 |
| `Ctrl + T` | 新規タブ |
| `Ctrl + W` | タブを閉じる |
| `Ctrl + Tab` | 次のタブ |
| `Ctrl + Shift + Tab` | 前のタブ |
| `Esc` | 処理中断 / パネルを閉じる |
| ダブルクリック | タブ名を変更 |

### タブ設定（歯車アイコン）

各タブで個別に設定可能:

- **カスタムCLIフラグ** — 任意のフラグを素通し
- **システムプロンプト** — `--system-prompt` に渡すカスタム指示
- **権限モード** — Manual / Accept Edits / Plan / Auto / Don't Ask / Bypass Permissions / 選択ツールのみ
- **Remote Control** — claude.ai からの操作を許可
- **セッション切替** — Ultracode / Fast モード / 出力スタイル / サンドボックス（実行中セッションへ即時適用）
- **起動オプション** — グループ別に全起動フラグ。`起動コマンドをプレビュー` で実際の argv を確認可能（次回セッション開始時に適用）
- **Session ID** — 現在のセッションID（読み取り専用）

> `Max Turns` は、Claude Code が `--max-turns` を公式フラグ一覧（`claude --help`）から外したため削除しました。

## 設定ファイル

```
~/.claude-code-gui/
  config.json    # グローバル設定（モード、APIキー、言語、CLI自動アップデートのON/OFF）
  tabs.json      # タブ状態（各タブのプロジェクト、セッション、会話履歴）
```

## セキュリティ

- APIキーはローカルファイルにのみ保存
- CLIモードではAPIキー自体が不要（Claude Code のOAuth認証を使用）
- ネットワーク通信は Claude API / Claude Code CLI 経由のみ
- プロジェクトファイルは `--add-dir` でCLIにコンテキストとして渡される

## 技術スタック

- **[PyWebview](https://pywebview.flowrl.com/)** — 軽量ネイティブWebViewラッパー
- **[Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)** — プライマリバックエンド
- **[Anthropic SDK](https://github.com/anthropics/anthropic-sdk-python)** — APIフォールバック
- **HTML/CSS/JS** — UI（main.pyに埋め込み、外部ファイル不要）

## ライセンス

MIT License
