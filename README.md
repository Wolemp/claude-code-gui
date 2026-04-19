# Claude Code GUI

**ターミナル不要で Claude Code の全機能を使えるデスクトップアプリ。**
マルチタブで複数プロジェクトを並行開発。ブラウザ不要、ネイティブウィンドウ。

![Python](https://img.shields.io/badge/Python-3.10+-blue)
![License](https://img.shields.io/badge/License-MIT-green)

## 特徴

- **マルチタブ** — 複数の会話を並行して実行。各タブに独立したプロジェクト・セッション。
- **ブラウザ不要** — PyWebviewによるネイティブウィンドウ。Electron不要、軽量。
- **APIキー不要** — Claude Code CLI バックエンドなら Max Plan のログインで利用可能。
- **モデル選択** — Opus 4.6 / Sonnet 4.6 / Opus 4 / Sonnet 4 / Haiku 4.5 をタブごとに切り替え。
- **Effort制御** — Max / High / Auto をタブごとに設定。
- **ストリーミング応答** — リアルタイムで回答が表示。バックグラウンドタブにも通知ドット。
- **ファイルツリー** — プロジェクト構造をサイドバーで確認、ワンクリックで閲覧。
- **セッション継続** — `--resume` で会話の文脈を維持。アプリ再起動後も復元。
- **カスタムフラグ** — CLIの任意のフラグをGUIから指定可能（上級者向け）。
- **ローカル完結** — データは外部に送信されません（Claude APIへの通信を除く）。

## 動作モード

| モード | 認証 | 特徴 |
|--------|------|------|
| **CLI** (デフォルト) | Claude Code ログイン済みでOK | Max Plan ($100-200/月) の範囲で利用。APIキー不要 |
| **API** (フォールバック) | `sk-ant-` APIキー必要 | 従量課金。CLI未インストール時の代替 |

## インストール

```bash
git clone https://github.com/yourname/claude-code-gui.git
cd claude-code-gui
pip install -r requirements.txt
```

### 必要なもの

- Python 3.10 以上
- **CLIモード**: `npm i -g @anthropic-ai/claude-code` でCLIをインストール → `claude login` でログイン
- **APIモード** (オプション): Anthropic APIキー（[console.anthropic.com](https://console.anthropic.com/)）

## 使い方

```bash
python main.py
```

1. 起動するとネイティブウィンドウが開く
2. 左サイドバーの「プロジェクトを選択」でフォルダを開く
3. トップバーでモデル・Effortを選択
4. チャット欄にメッセージを入力して Enter で送信
5. `+` ボタンや Ctrl+T で新しいタブを追加して並行作業

### キーボードショートカット

| キー | 動作 |
|------|------|
| `Enter` | メッセージ送信 / 選択メニュー確定 |
| `Shift + Enter` | 改行 |
| `↑` `↓` | 選択メニューの項目を移動（権限確認・Claude の質問など） |
| `Tab` / `Shift + Tab` | メニュー操作（セッション許可など） |
| `Ctrl + T` | 新規タブ |
| `Ctrl + W` | タブを閉じる |
| `Ctrl + Tab` | 次のタブ |
| `Ctrl + Shift + Tab` | 前のタブ |
| `Ctrl + L` | 会話をクリア |
| `Esc` | 処理中断 / パネルを閉じる |
| ダブルクリック | タブ名を変更 |

### タブ設定（歯車アイコン）

各タブで個別に設定可能:

- **Max Turns** — エージェントの最大ターン数（0 = 無制限）
- **カスタムCLIフラグ** — `--allowedTools` など任意のフラグ
- **システムプロンプト** — APIモード用のカスタム指示
- **Session ID** — 現在のセッションID（読み取り専用）

## .exe にビルド（配布用）

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name ClaudeCodeGUI main.py
```

`dist/ClaudeCodeGUI.exe` が生成されます。Python不要で配布可能。

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
