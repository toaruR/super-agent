# Super Agent ハーネス — 使い方マニュアル

このドキュメントは、**完成した設計・実装を実際にどう動かすか**を説明します。
設計の意図（なぜこうなっているか）は `docs/spec.md` を参照。

---

## 0. これは何か（30秒で）

Super Agent は「異ベンダーのコーディングエージェント（Claude Code / Codex / Antigravity）
を、1つの検証可能な生産ラインの作業員として動かす」ハーネスです。

- あなたが**要求**を出す
- ハーネスが **CVE（検証環境）でテストを実行**し、証拠を取る
- 別ベンダーの**レビュア**が、証拠だけを読んで判定する
- 判定は**レビュアの実行環境に依存しない**（これがこのシステムの核心）

現在実装済み：Stage A（基盤・台帳・CLI）＋ Stage C（検証パイプライン）。
並列実行・操作面（pause/abort）・OS隔離は**未実装**（設計のみ）。

---

## 1. 前提環境

| 要件 | 確認済みの値（このマシン） |
|---|---|
| Python | `.cve-venv`（pytest 入りの専用仮想環境）を使用 |
| ベンダーCLI | `claude` / `codex` / `agy` が PATH にあること |
| OS | Windows（git-bash / PowerShell 両方可） |

**venv を有効化してから `python -m` で実行するのが基本です。コマンド内のパスはすべて
`src/` からの相対表記**です（例: `probe/n3/caseGreen`）。

### 1.1 VSCode ターミナル（推奨・最も簡単）

VSCode の Python 拡張が `.cve-venv` を自動検出し、ターミナル起動時に
`Activate.ps1` を自動実行してくれます（プロンプトに `(.cve-venv)` が付く）。
手動で Activate する必要はありません。

```powershell
cd D:/vagrant/harnesses/super-agent/src
python -m harness.cli status          # venv の python が使われる
```

### 1.2 素の PowerShell（VSCode 外）

自動 Activate が効かないので、venv の `python.exe` を**フルパスで直接**指定します
（Activate 経由より確実）。コマンド内の題材パス等は相対表記のままで可。

```powershell
cd D:/vagrant/harnesses/super-agent/src
D:/vagrant/harnesses/super-agent/.cve-venv/Scripts/python.exe -m harness.cli status
```

### 1.3 git-bash

```bash
cd D:/vagrant/harnesses/super-agent/src
./.cve-venv/Scripts/python.exe -m harness.cli status
```

### 1.4 動作確認（どの環境でも共通）

venv の python が使われているか：

```bash
python -c "import yaml, pytest; print('ok')"   # VSCode / Activate 後
# または
D:/vagrant/harnesses/super-agent/.cve-venv/Scripts/python.exe -c "import yaml, pytest; print('ok')"
```

> `.cve-venv` が無い／壊れている場合は作り直します（PyYAML と pytest が必要）：
> ```powershell
> cd D:/vagrant/harnesses/super-agent
> python -m venv .cve-venv
> .cve-venv/Scripts/python.exe -m pip install pyyaml pytest
> ```

作業ディレクトリは必ず `src/` の中で行ってください（`harness/` パッケージが解決できるため）。

---

## 2. コマンド一覧

`super-agent` として使えるコマンド（Stage A + Stage 0 足場）：

| コマンド | 役割 | § |
|---|---|---|
| `run "<要求>"` | 要求を台帳に記録しベンダーを呼ぶ（Stage A） | 2.1 |
| `review <dir>` | 検証パイプラインを走らせる（Stage 0＝⑤⑥⑦⑨） | 2.2 |
| `status` | 台帳の最近のイベントを表示 | 2.3 |
| `log <task>` | 指定タスクの全イベントを表示 | 2.4 |
| `show design\|plan` | 設計ゴール／実装計画を read-only 表示（L6） | 2.5 |
| `architect "<要求>"` | 設計決定を ADR として台帳に記録（Stage 1＝①） | 2.6 |
| `plan "<要求>"` | 分解(②)→編成・worktree・リース(③)。`--tasks` 既存なら分解をスキップ | 2.7 |
| `implement --task T1` | タスクを worktree 内で実装しコミット（Stage 4＝④） | 2.8 |

### 2.1 `super-agent run` — 要求を投入し台帳に記録

```bash
python -m harness.cli run "<要求>" [--vendor claude|codex|agy] [--dry-run]
```

| オプション | 意味 |
|---|---|
| `--vendor` | 要求を処理させるベンダー（既定 `claude`） |
| `--dry-run` | **ベンダーを実際に起動せず**、組み立てるコマンドだけ確認 |

**何をするか**：要求を受け、`task.created` と `agent.invoked` の2イベントを
台帳（`harness/ledger/events.jsonl`）に書きます。まだ検証は走りません（並列/実行は未実装）。

**例**：
```bash
python -m harness.cli run "build a fizzbuzz module" --vendor codex --dry-run
# → task T-XXXX recorded. ledger=...
```

### 2.2 `super-agent review <dir>` — 検証パイプライン（⑤⑥⑦⑨）

```bash
python -m harness.cli review <dir> [--accept "pytest tests/"] [--reviewer codex] [--dry-run]
```

| オプション | 意味 |
|---|---|
| `<dir>` | 検証するワークツリー／題材ディレクトリ（必須） |
| `--accept` | 受理テスト指定 `"verb arg1 arg2"`（既定 `pytest tests/`）。期待終了コードは `--expect-exit` |
| `--reviewer` | レビュアベンダー（既定 `codex`） |
| `--dry-run` | **CVE は実行するがレビュアは呼ばず**。裁定は `judgment_unavailable` になる |

**何をするか**：`run_pipeline` を呼び、CVE→簡報→レビュー→裁定を台帳駆動で実行。
JSON で裁定を標準出力に出します。

**例**：
```bash
python -m harness.cli review probe/n3/caseGreen --reviewer codex --dry-run
# → verdict/judgment_unavailable, tree_hash が束縛される
```

### 2.3 `super-agent status` — 台帳の状態を表示

```bash
python -m harness.cli status
```

台帳に記録された全イベントを（クラッシュセーフに）読み出して表示します。
```
events in ledger: 2
  T-418dd0b1:1 task.created
  T-418dd0b1:2 agent.invoked
```

### 2.4 `super-agent log <task>` — 指定タスクの全イベント

```bash
python -m harness.cli log T-XXXX
```

指定タスクID（接頭辞でも可）の全イベントを、付随データ付きで表示します。
```
events for T-XXXX: 5
  T-XXXX:1 verification.run {"tree_hash": "3309...", "cve_ok": true}
  T-XXXX:2 brief.built {"tokens_est": 1327}
  T-XXXX:3 reviewer.invoked {"vendor": "codex"}
  T-XXXX:4 reviewer.skipped {"reason": "dry_run"}
  T-XXXX:5 judgment {"verdict": "judgment_unavailable", "tree_hash": "3309..."}
```

### 2.5 `super-agent show design|plan` — 設計／計画の表示（L6 読み取り）

```bash
python -m harness.cli show design   # docs/goals/design.md を表示
python -m harness.cli show plan     # docs/plan.md を表示
```

読み取り専用。台帳イベントは発生しません（L6 の `show` 操作）。

### 2.6 `super-agent architect "<要求>"` — 設計決定を ADR として記録（①）

```bash
python -m harness.cli architect "<要求>" [--spec <file>] [--vendor claude] [--dry-run]
```

| オプション | 意味 |
|---|---|
| `--spec` | 設計ファイル。`<file>` が無ければ LLM で起案してそのパスに作成（後で編集可）。既存ならそのまま記録（推奨・確実） |
| `--vendor` | 起案させるベンダー（既定 `claude`）。`--spec` 無し、または `<file>` が無い時のみ使用 |
| `--dry-run` | プロンプトを組み立てるだけでベンダーは呼ばない |

**何をするか**：要求を受け、設計決定を **ADR（Architecture Decision Record）** として
台帳に `adr.written` イベントで記録します（§9 の①）。後から `log <task>` で
「何を・なぜ決めたか」を辿れます。

**例（人間の設計をそのまま記録 — 最も確実）**：
```bash
python -m harness.cli architect "Web API を作れ" --spec my-design.md
# → 既存ならその内容を記録。無ければ LLM が起案して my-design.md を作成し記録
```

**例（LLM に起案させる／dry-run で確認）**：
```bash
python -m harness.cli architect "Web API を作れ" --dry-run
# → {"source": "llm(dry)", "cmd": [...]}  # 実際に呼ぶコマンドを確認（ファイルは作らない）
```

> `--spec` なしで LLM に起案させる場合、ベンダーは **read-only**（実装しない）で
> 推論のみ行います。曖昧な点は `open_questions` に挙げさせ、人間が `amend`（未実装）で確定します。


### 2.7 `super-agent plan` — 分解(②)＋編成・worktree・リース(③)

`plan` は Stage 2（decomposer）と Stage 3（scheduler）を連続実行します。
`--tasks` ファイルの有無で「分解するか / 既存を使うか」が自動で決まります。

```bash
# ① 分解だけ（--tasks に書き出し。このとき worktree は作らない）
python -m harness.cli plan --spec my-design.md --tasks my-design-tasks.md --dry-run
# ② tasks.md を人間がレビュー/編集した後、分解をスキップして worktree だけ作る
python -m harness.cli plan --tasks my-design-tasks.md
# ③ 通常運用：設計から一気に分解→worktree作成→リース発行
python -m harness.cli plan --spec my-design.md --tasks my-design-tasks.md
```

| コマンド | vendor呼ぶ？ | worktree作る？ | 使い道 |
|---|---|---|---|
| `plan --spec X --tasks Y`（Y未作成） | ✔ | ✔ | 通常運用（分解→作業台） |
| `plan --spec X --tasks Y --dry-run` | × | ×（計画のみ） | 構成確認 |
| `plan --tasks Y`（Y既存） | × | ✔ | tasks.mdからworktreeだけ作る |

`--tasks` が**既存ファイル**ならそれを読み込んで（人手編集も反映）、vendor を呼ばずに
worktree/リースだけ作成します。`--tasks` が無い / 未作成なら vendor で分解してから進みます。

**構造検査（機械強制・H2 含む、再利用時も適用）**：
- `acceptance` が空でない
- `acceptance[].verb` が `verifiers.yaml` に登録済み（未登録 verb は差し戻し＝インジェクション排除）
- `acceptance[].args` はリスト
- DAG に循環が無い
- 並列タスク間で `touch_allow` が重複しない

各タスクに対して `git worktree add workspaces/<task_id> -b task/<task_id>` で作業ツリーを作成
（§3.1 隔離）、台帳に `worktree.created` と `task.leased`（lease_until, touch_allow 付き）を記録します。
冪等：再実行しても既存 worktree/ブランチを再利用・または prune して再作成します。


### 2.8 `super-agent implement` — タスク実装＋コミット（④）

`plan` が作った worktree 内で、指定タスクを Implementer ベンダーに実装させ、harness が
コミットします（§3.1 隔離＋§6.2 `touch_allow` 制約）。

```bash
# tasks.md から T1 を探し、workspaces/T1 で実装→コミット
python -m harness.cli implement --task T1 --tasks my-design-tasks.md
# ドライラン（プロンプト組み立てのみ）
python -m harness.cli implement --task T1 --tasks my-design-tasks.md --dry-run
```

| オプション | 意味 |
|---|---|
| `--task` | 実装するタスクID（必須） |
| `--tasks` | タスク定義を探す DAG（既定 `probe/sample/my-design-tasks.md`） |
| `--worktree` | worktree パス（既定 `workspaces/<task>`） |
| `--vendor` | Implementer ベンダー（既定 `claude`） |
| `--dry-run` | プロンプト組み立てのみ（vendor/git は呼ばない） |

**何をするか**：タスクの `goal`・`acceptance`・`touch_allow` をプロンプトに入れ、ベンダーを
worktree 内で実行。`touch_allow` 以外のファイルは触らせません。実装後、harness が
`git add <touch_allow>` → `commit` し、台帳に `artifact.produced`（paths, commit）と
`task.implemented`（commit, tree_hash）を記録します。`tree_hash` は §3.2 の証拠束縛です。

## 3. 検証パイプラインを動かす（Stage C）


`run` はまだ検証を走らせません。**実際の「CVE実行→レビュー→裁定」**は
`super-agent review <dir>`（§2.2）が `run_pipeline` を呼び出して行います。
ここでは題材と、レビュアも本番呼び出す場合の挙動を補足します。

### 3.1 テスト題材（probe/n3/）

| 題材 | 内容 | 受理テスト |
|---|---|---|
| `caseGreen` | 1つの通るテスト | ✅ GREEN |
| `caseB` | 2ファイルの台帳（accounts/money）。実バグ（Money可変）あり | ✅ GREEN（バグはテストが拾えない） |
| `caseC` | util（retry/cache）。実バグ（attempts-1）あり | ❌ RED |
| `caseD` | 42ファイルの大きな差分用 | — |

### 3.2 基本：CVE 実行＋裁定（レビュアなし）

`review --dry-run` で CVE を実行し、証拠（tree_hash 付き）を取って裁定まで回します
（レビュアは呼ばないので `judgment_unavailable` になります。これは正しい動作）：

```bash
python -m harness.cli review probe/n3/caseGreen --reviewer codex --dry-run
```

**出力例（caseGreen）**：
```json
{
  "verdict": "judgment_unavailable",
  "why": "reviewer produced no parseable output",
  "tree_hash": "3309c1ea35679a40",
  "advisory": []
}
```

> CVE 自体は実行されており、`tree_hash` が束縛されています。
> 「受理テストが RED なら fail」を確かめたい場合は caseC を指定してください。

### 3.3 レビュアも本番呼び出し（claude / codex）

`--dry-run` を外すと、レビュア（別ベンダー）が簡報を読んでレビューし、adjudicate が裁定します：

```bash
python -m harness.cli review probe/n3/caseB --reviewer claude --dry-run=False
# PowerShell の場合は --dry-run:$false と書く
```

**裁定の種類**：

| verdict | 意味 |
|---|---|
| `pass` | 受理テスト全部 GREEN、証拠裏付けの指摘なし |
| `pass_with_findings` | GREEN だが、証拠（E-n）を cite した指摘あり |
| `fail` | 受理テストが RED（evidence の exit_code != 0） |
| `environment_error` | CVE の起動自体が失敗（環境障害。タスク失敗と混同しない） |
| `judgment_unavailable` | レビュアが構造化出力を返せなかった（**偽 fail にはならない**） |

> **この環境での注意**：`codex` は構造化出力を返せず `judgment_unavailable`
> になることを確認済みです（設計どおり、偽 fail にはなりません）。
> `claude` なら返る可能性があります。

### 3.4 台帳で証拠を確認する

パイプラインが書いたイベントは全て台帳に残ります。`super-agent log <task>` で確認できます：

```bash
python -m harness.cli log T-XXXX
# T-XXXX:1 verification.run 3309c1ea35679a40   <- CVE の証拠（tree_hash 束縛）
# T-XXXX:2 brief.built
# T-XXXX:3 reviewer.invoked
# T-XXXX:4 reviewer.skipped
# T-XXXX:5 judgment 3309c1ea35679a40          <- 裁定も同じ tree_hash
```

`verification.run` と `judgment` の `tree_hash` が一致することが、**「どの成果物の
証拠か」が保証されている**ことの証拠です（H4）。

---

## 4. 設定ファイル（harness/config/）

| ファイル | 役割 | いつ触るか |
|---|---|---|
| `vendors.yaml` | ベンダーの呼び出し方（構造化出力・再開・権限） | 新ベンダー追加時 |
| `verification_env.yaml` | CVE（検証環境）の python パス・起動チェック | マシンが変わった時 |
| `verifiers.yaml` | 許可する検証コマンド（verb ホワイトリスト） | 新しい検証種別を足す時 |

> **`verification_env.yaml` の python パスは環境依存です。**
> このマシンでは `D:/vagrant/harnesses/super-agent/.cve-venv/...` を指しています。
> 別マシンでは書き換えてください（Windows git-bash は `C:/...` 表記を解釈します）。

---

## 5. テストを通す（動作の証明）

```bash
python -m pytest harness/tests/ -q
# ................  40 passed
```

- `test_invoke.py`（6）：ベンダー呼び出しコマンドの組み立て（A-1〜A-6 実測値）
- `test_ledger.py`（3）：台帳の原子性（H3）
- `test_pipeline.py`（2）：パイプラインの CVE 実行＋tree_hash 束縛＋裁定記録
- `test_implementer.py`（3）：実装→コミットの束縛＋台帳記録（vendor はモック）
- `test_scheduler.py`（7）：worktree 冪等性・リース記録・再利用

---

## 6. 今できないこと（未実装）

以下は**設計のみ**。マニュアルに書かれていても、まだ動きません：

- `super-agent run` での**並列実装の自動起動**（Stage B）：worktree/リース/scheduler は動くが、複数タスクを一斉に回す駆動は未実装
- `pause` / `resume` / `abort` / `amend` / `show` コマンド（Stage D' 操作面）
- 予算上限での自動停止・承認キュー（Stage D）
- レビュアの OS レベル隔離（Stage F）

これらは `docs/plan.md` の Stage B〜F を参照。

---

## 7. トラブルシューティング

| 現象 | 原因 / 対処 |
|---|---|
| `ModuleNotFoundError: yaml` | `.cve-venv` を使っているか確認。`uv pip install pyyaml` |
| `verdict: judgment_unavailable` | `dry_run=True` の場合は正常（レビュアを呼んでいない）。`False` でもベンダーが構造化出力を返せない環境の場合（偽 fail ではない） |
| CVE の `cve_ok: False` | `verification_env.yaml` の python パスが通っていない。パスを確認 |
| `ModuleNotFoundError: pytest`（CVE実行時） | `verifiers.yaml` の python が venv を指しているか確認 |
| 台帳が汚れる | `harness/ledger/` は `.gitignore` 対象。消して良い |
