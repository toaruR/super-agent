# super-agent

**異ベンダーのコーディングエージェント（Claude Code / Codex / Antigravity / Hermes）を、
1つの検証可能な生産ライン上の作業員として動かすハーネス。**
要求 → 設計 → 分解 → 実装 → レビュー → 統合 を、台帳で証拠を残しながら自動駆動する。

> 設計意図・現状・実測証拠の詳細は [`docs/design-overview.md`](docs/design-overview.md) を参照。
> 実際に動かす手順は [`docs/usage.md`](docs/usage.md) を参照。

---

## Features

- **実行と判定の分離** — 決定的な検証はハーネスが唯一の環境（CVE）で行い、レビュアは「証拠を読む」だけ。レビュアの実行環境に依存しない裁定が得られる（偽 fail を排除）。
- **複数ベンダーの同一 IF** — `vendors.yaml` に宣言するだけで claude / codex / agy / hermes を交換可能な作業員として扱う。
- **マルチチャンネル実装（Stage B 並列）** — `roles.implement` をチャンネルリストで宣言すると、各エントリが独立 worktree で並列実装される（例: agy×2 + hermes×3 の同時実行）。model/effort はチャンネルごとに指定可。
- **タスクレベル並列** — 依存のないタスクを topological レイヤー単位で並行駆動（`--parallel-tasks`）。
- **台帳（証拠の束縛）** — 全イベントをクラッシュセーフな JSONL に記録。`tree_hash` で「どの成果物の証拠か」を保証。
- **worktree 隔離** — 各タスク/チャンネルは独立 git worktree で実行され、統合後に自動で片付く（敗者チャンネルも残らない）。
- **read-only レビュア** — レビュアは実装者と別ベンダーかつ読み取り専用。独立性は権限ではなく裁定器で担保。
- **自己改良（Stage 6: evolve）** — 台帳から失敗パターンを拾い、同種が3回以上继续したら `acceptance` テンプレまたは憲法への昇格を提案。`evolve --dry-run` で確認、実行で `design.proposed` を台帳に記録。

---

## Prerequisites

| 要件 | 確認済みの値（このマシン） |
|---|---|
| OS | Windows（git-bash / PowerShell 両方可）、Linux も可 |
| Python | 3.11 系（`.cve-venv` を使用） |
| ベンダー CLI | `claude` / `codex` / `agy` / `hermes` が PATH にあること |
| Git | worktree 操作に使用（2.26 以降推奨） |

> ベンダー CLI の認証・課金設定は各ベンダーの手順で事前に済ませておくこと。

---

## Installation

```bash
# 1. リポジトリを取得（src/ がハーネス本体の別 git リポジトリ）
cd /path/to/super-agent/src

# 2. 専用仮想環境を作成し依存を入れる
python -m venv .cve-venv
.cve-venv/Scripts/python.exe -m pip install pyyaml pytest

# 3. 動作確認（venv の python が使われるか）
.cve-venv/Scripts/python.exe -c "import yaml, pytest; print('ok')"
```

ラッパー `super-agent`（bash/git-bash/Linux）または `super-agent.bat`（cmd/PowerShell）を
`src/` から直接実行する。内部で `python -m harness.cli` を呼ぶ。

```bash
# git-bash / Linux
./super-agent status
# PowerShell / cmd
super-agent status
```

---

## Usage

```bash
# 設計から一気に分解→worktree→実装→レビュー→統合（既定は5チャンネル並列実装）
super-agent drive --tasks ./probe/sample/my-design-tasks.md

# 緊急オーバーライド: agy 2 + hermes 3 の5チャンネル
super-agent drive --tasks ./probe/sample/my-design-tasks.md --implement-vendors "agy:2,hermes:3"

# タスク並列 + チャンネル並列の両方
super-agent drive --tasks ./probe/sample/my-design-tasks-parallel.md \
    --implement-vendors "agy:1,hermes:1" --parallel-tasks

# 単体コマンド（ステージ個別に動かす）
super-agent review   probe/n3/caseGreen            # 検証パイプライン（CVE→簡報→裁定）
super-agent status                                # 台帳の最近のイベント
super-agent log T-XXXX                            # 指定タスクの全イベント
super-agent show design | plan                    # 設計／計画の read-only 表示
super-agent evolve --dry-run                      # 台帳から失敗パターンを拾い自己改良を提案
```

その他のコマンドと詳細な手順は [`docs/usage.md`](docs/usage.md) を参照。

```bash
# テストを通す（動作の証明）
.cve-venv/Scripts/python.exe -m pytest harness/tests/ -q
# 65 passed
```

---

## Configuration

ベンダーの呼び出し方は `harness/config/vendors.yaml` で宣言する（新ベンダー追加はこのファイルへの
宣言のみ）。主な設定項目：

| キー | 意味 |
|---|---|
| `roles.design` | 設計起案ベンダー（既定 `claude`） |
| `roles.implement` | **実装チャンネルのリスト**。各エントリ `{vendor, model, effort}` が1チャンネル＝独立 worktree で並列実装。既定は `agy×2 + hermes×3` |
| `roles.review` | レビュアベンダー（既定 `codex`） |
| `<vendor>.headless` | ヘッドレス呼び出しコマンド。`{worktree}` `{prompt}` が置換される |
| `verifiers.yaml` | 許可する検証コマンド（verb ホワイトリスト） |
| `verification_env.yaml` | CVE（検証環境）の python パス・起動チェック |

> `verification_env.yaml` の python パスは環境依存。`.cve-venv/Scripts/python.exe`
> （または各環境の venv）を指すよう、環境に合わせて設定すること。
> サンプルは `verification_env_sample.yaml` を参照。

環境変数（`.env`）は現在使用していない。ベンダー CLI の認証情報は各 CLI の仕組み
（ネイティブな認証キャッシュ等）に委ねる。

---

## License

**MIT License** — 詳細は [`LICENSE`](LICENSE) を参照。

```
Copyright (c) 2026 toaruR

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the conditions of the MIT License.
```
