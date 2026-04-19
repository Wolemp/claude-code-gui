# Claude Code GUI

**ターミナル不要で Claude Code の全機能を使えるデスクトップアプリ。**
マルチタブで複数プロジェクトを並行開発。ブラウザ不要、ネイティブウィンドウ。

![Python](https://img.shields.io/badge/Python-3.10+-blue)
![License](https://img.shields.io/badge/License-MIT-green)

## 特徴

- **マルチタブ** — 複数の会話を並行して実行。各タブに独立したプロジェクト・セッション。
- **ブラウザ不要** — PyWebviewによるネイティブウィンドウ。Electron不要、軽量。
- **APIキー不要** — Claude Code CLI バックエンドなら Max Plan のログインで利用可能。
- **モデル選択** — Opus 4.7 / 4.6 / Sonnet 4.6 / 4 / 3.7 / 3.5 / Haiku 4.5 / 3.5 / Opus 3 をタブごとに切り替え。GUIで変更すると実行中セッションにも即反映。
- **Effort制御** — Max / XHigh / High / Medium / Low の5段階。セッション開始時に自動適用、変更時も即反映。
- **権限モード切替** — Plan（読み取り専用）/ Accept Edits（ファイル操作のみ）をナビバーからワンクリックで切替。
- **動的パーミッションボタン** — CLIの実際の選択肢をPTY画面からパースして動的にボタン生成。フィードバック評価にも対応。
- **セッション管理** — クリア（表示のみ）/ セッション終了（PTY停止、履歴保持）/ 新規セッション（全リセット）の3段階。
- **自動セッション初期化** — 起動時に `/remote-control` と設定済み effort を自動送信。
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

### キーボードショートカット

| キー | 動作 |
|------|------|
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

- **Max Turns** — エージェントの最大ターン数（0 = 無制限）
- **カスタムCLIフラグ** — `--allowedTools` など任意のフラグ
- **システムプロンプト** — APIモード用のカスタム指示
- **権限モード** — Default / Accept Edits / Plan / カスタム
- **Session ID** — 現在のセッションID（読み取り専用）

## 設定ファイル

```
~/.claude-code-gui/
  config.json    # グローバル設定（モード、APIキー）
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
