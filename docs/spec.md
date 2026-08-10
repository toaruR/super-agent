# 仕様（spec）

super-agent ハーネスの**全仕様**をまとめる。使い方（コマンドの実行例）は `usage.md` を、システムの設計思想（なぜこうなっているか）は `design-notes/architecture-v3.md` を参照。

---

## 1. システム概要

super-agent は、複数ベンダー（Claude Code / Codex / Antigravity(agy) / Hermes 等）のエージェントを「交換可能な作業員」として扱い、要件→設計→分解→実装→レビュー→統合→自己改良のパイプラインを駆動するハーネス。

- **実行と判定の分離**: 決定的な検証はハーネスが唯一の環境（CVE: Controlled Verification Environment）で実行し、エージェントは「証拠を読む」だけ。
- **ベンダー抽象化**: ベンダー呼び出しは `config/vendors.yaml` の宣言のみで行う（新ベンダー追加は yaml 編集のみ、コード変更なし）。
- **台帳（ledger）**: 全イベントを append-only の `harness/ledger/events.jsonl` に記録。ダッシュボード・evolve がこれを読む。
- **検証ホワイトリスト（H2）**: 受入基準（acceptance）は `verifiers.yaml` に登録された verb のみ実行可能。シェル展開なし。

### 用語: DAG（タスク依存グラフ）

本システムの中心データ構造。**DAG = Directed Acyclic Graph（有向非巡回グラフ）**。

- **Directed（有向）**: タスク間に「依存」の向きがある。タスク A がタスク B に依存するなら、A → B（B は A が終わらないと始められない）。
- **Acyclic（非巡回）**: 循環依存がない。A→B→C→A のようなループは作れない（作るとデッドロック）。
- これを **トポロジカル順序（topo order）** で層（レイヤー）に分解し、同じレイヤー内のタスクは互いに依存がないため**並行実行**できる。

super-agent では、設計書（spec）から `decomposer` がタスクを分解して DAG を作り、`plan` / `drive` がその DAG に従って実行する。タスク定義ファイル（Markdown）の `依存:` フィールドがこの辺のエッジを記述する（詳細は §8）。

パッケージ構成:

```
harness/
  cli.py                 # 全サブコマンドのエントリ
  core/
    invoke.py            # ベンダー呼び出しアダプタ（コマンド組み立て・リトライ・結果抽出）
    ledger.py            # 台帳（append-only + Sequencer）
    verifiers.py         # verb -> argv 解決（ホワイトリスト）
    cve.py               # CVE ラッパ（検証環境での実行）
    adjudicate.py        # 判定（CVE 証拠から合否を分類）
    brief.py             # 簡報構築
  roles/
    architect.py         # design（ADR 記録）
    decomposer.py        # タスク分解（DAG）
    planner.py           # 再計画（adaptive）
    implementer.py       # 実装（worktree 隔離）
    review_flow.py       # レビュー（読み取り専用）
    integrator.py        # 統合
    improver.py          # evolve（自己改良）
    drive.py             # パイプライン一括駆動
    dashboard.py         # 台帳 -> モデル -> 描画
    scheduler.py         # タスク並行スケジューラ
  config/
    vendors.yaml         # ベンダー宣言 + ロール既定
    verifiers.yaml       # 検証 verb ホワイトリスト
    verification_env_sample.yaml  # CVE 設定サンプル
```

---

### 用語: task_id / event_id（グローバル一意識別子）

台帳イベントは、システム全体で**グローバルに一意**な識別子を持たなければならない。タスクファイルの格納場所がシステム的に決まっていない（任意の絶対パスをとりうる）ため、以下の2本立てで一意性と逆引きを両立する:

#### 1. `task_file` フィールド（絶対パスを別カラムで持つ）

各 event は、そのタスクの定義元ファイルの**絶対パス**を `task_file` フィールドに持つ。

```json
{ "task_file": "/home/u/proj/probe/samples/my-tasks.md", ... }
```

台帳から「元のタスクファイルはどこか」を直接逆引きできる（md5 等で難読化しない）。タスクファイルの格納場所がシステムで固定されていないため、絶対パスを event に含める。

#### 2. `event_id` = `md5(絶対パス)::<task_id_in_file>::<チャンネル>`

| 要素 | 意味 | 例 |
|---|---|---|
| `md5(絶対パス)` | タスクファイルの絶対パスを md5 化した固定長16進文字列。パス内の `/` `:` 等の区切り文字問題を回避し、ファイルを一意に識別 | `9f86d0819` |
| `task_id_in_file` | タスクファイル内の `## <id>` 見出しに書かれた人間可読な識別子。**ファイル内では一意だが、グローバルでは非一意になりうる**（別ファイルで同じ `PA` を書く可能性） | `PA` |
| チャンネル | 投機的実行の候補番号。単一チャンネル（デフォルト）でも常に付与し、`0` とする | `0` / `hermes_1` |

**例**:
- `9f86d0819::PA::0` — `my-tasks.md` の `PA` タスク、チャンネル0（単一実装）
- `9f86d0819::PA::hermes_1` — 同上の投機的チャンネル1

**区切り文字**: `::` を使用（md5 は hex のみで `:` を含まないため安全。task_id_in_file に `_` 等が入っても `::` で確実に分割できる）。

### 用語: 台帳の構造（1塊 = 1設計）

台帳は **JSONL（1行 = 1塊 = 1設計・1タスクファイル）** の形式をとる。各塊（chunk）は、その設計・タスクファイルに属する全イベントをまとめる:

```json
{"design_file": "docs/design/spec_v1.md", "task_file": "tasks/schedules/task_01.json", "events": [ ... ]}
{"design_file": "docs/design/spec_xxx.md", "task_file": "tasks/schedules/task_xxx.json", "events": [ ... ]}
```

各塊のスキーマ:

| フィールド | 意味 |
|---|---|
| `design_file` | このタスクの根拠となる設計ファイルのパス（トップレベル・1回のみ） |
| `task_file` | タスク定義ファイルのパス（トップレベル・1回のみ） |
| `events` | このタスクファイルに属するイベントの配列 |

**イベント内の `event_id`**: 塊内で一意な局所識別子（例 `PA__hermes_0:1`）。塊外との重複は `task_file` が異なれば別物として扱う（グローバル一意性は `task_file + event_id` で保証）。絶対パスや md5 を event_id に埋め込む必要はない。

**利点**:
- `design_file` / `task_file` は塊のトップレベルに1回だけ書かれ、各 event への重複を避ける（冗長性なし）
- 設計とタスクの紐付けが塊単位で一目瞭然
- Append-Only の原則を維持（1行1塊を追記。塊内の events は配列だが、塊全体として1回の append で書く）

> **設計理由**: 従来は全タスクのイベントが単一の `events.jsonl` に `task_id` ごとに散在し、タスクファイルとの紐付けが不明だった。1塊 = 1設計とすることで、「台帳 → 元ファイル」の逆引きが構造的に保証される。

> **未実装**: 現状の `harness/core/ledger.py` は1イベント = 1行の旧形式（`{event_id, task_id, seq, type, ...}`）を使っている。本仕様（1塊 = 1設計の JSONL）への移行は今後の実装課題。

## 2. 台帳（ledger）仕様

**ファイル**: `harness/ledger/events.jsonl`（1塊 = 1行 = 1 append 書き込み、改行終端）

**塊スキーマ**:

```json
{"design_file": "<path>", "task_file": "<path>", "events": [ {"event_id": "<chunk-local-id>", "type": "<type>", ...}, ... ]}
```

**原子性規則（ARCHITECTURE §5.1, H3）**:

- 1塊 = 1行 = 1 append 書き込み（OS が行サイズでアトミック保証）。
- ロード時、改行で終わらない末尾行は破棄（クラッシュ復旧）。
- 追加は `Sequencer` プロセスのみが行う。他は提案（propose）をキューに渡す。

**イベント type（events 配列内）**:

| type | 意味 |
|---|---|
| `task.created` | タスク定義 |
| `task.implemented` | 実装完了（implementer） |
| `verification.run` | 検証（CVE）実行 |
| `reviewer.invoked` | レビュア呼び出し（vendor 記録） |
| `judgment` | 裁定（verdict: pass/fail/reject/blocked 等） |
| `integrated` / `integrated.failed` | 統合結果 |
| `integration.touch_violation` | 許可外ファイル変更の差し戻し |
| `conflict` | 統合時のコンフリクト |
| `design.proposed` | evolve 提案 |

**実装**: `harness/core/ledger.py`（`Ledger` / `Sequencer`）。

---

## 3. ベンダー抽象化（vendors.yaml）仕様

**ファイル**: `harness/config/vendors.yaml`

### 3.1 トップレベル `roles:`（ロール既定の単一ソース）

```yaml
roles:
  design:    { vendor: claude, model: claude-sonnet-5,  effort: high }
  planner:   { vendor: claude, model: claude-sonnet-5,  effort: high }
  implement:                # チャンネルリスト（各エントリ = 1チャンネル = 独立 worktree）
    - { vendor: hermes, model: hy3:Free, effort: high }
    - { vendor: hermes, model: hy3:Free, effort: high }
  review:    { vendor: agy, model: gemini-3.6-flash, effort: high }
```

- `implement` は**リスト**。各エントリが1チャンネル（独立 worktree で並列）。単一辞書 `{vendor,model,effort}` も後方互換（1チャンネル化）。
- cli は `--vendor`/`--model`/`--effort` が未指定のとき、ここから解決する。
- 現在の既定: `implement` = hermes(hy3:Free)×5、`review` = agy(gemini-3.6-flash)。

### 3.2 ベンダー宣言スキーマ

各ベンダー（claude / codex / agy / hermes）は以下のキーを持つ:

| キー | 意味 |
|---|---|
| `model_flag` | モデル指定フラグ（例: `--model`, `-m`） |
| `effort_style` | `flag`（`--effort <lvl>`）/ `config`（`-c key=lvl`）/ `model_suffix`（モデル名に付与） |
| `effort_flag` | effort_style=flag 時のフラグ名（例: `--effort`, `--reasoning`） |
| `effort_key` | effort_style=config 時のキー名 |
| `headless` | 非対話実行コマンドテンプレート。`{prompt}` / `{worktree}` が置換される |
| `prompt_stdin` | true のときプロンプトを stdin 経由で渡す（codex 等） |
| `structured` | 構造化出力フラグ（`flag` + `form: inline|file`）。ないベンダーは prompt 内指示 |
| `result_path` | 応答 JSON から取り出すパス（claude は `result`、他は空=全体） |
| `session.id_origin` | `caller`（呼び側が UUID 採番）/ `callee`（ベンダーが出力） |
| `session.resume_flag` | セッション再開フラグ（例: `--resume`, `--resume-session-id`） |
| `session.resume_extra` | 再開時に追加する引数（codex は `--full-auto`） |
| `permission.readonly` | 読み取り専用ロール（design/review）に付与するフラグ |
| `brief_mode` | 簡報の渡し方（`path`） |

### 3.3 モデル名の正規化（normalize_model）

yaml に書かれたモデル名はコード側で実名に正規化される（yaml は書き換えない）:

- `hy3:Free` → `tencent/hy3:free`
- `hy3` → `tencent/hy3:free`
- リストにない名前はそのまま通す（サイレントな書き換えなし）

**実装**: `harness/core/invoke.py` の `MODEL_ALIASES` / `normalize_model()`。

---

## 4. 検証（verifiers.yaml + CVE）仕様

### 4.1 verb ホワイトリスト（H2）

**ファイル**: `harness/config/verifiers.yaml`

```yaml
verifiers:
  pytest:     [".cve-venv/Scripts/python.exe", "-m", "pytest", "-q"]
  unittest:   [".cve-venv/Scripts/python.exe", "-m", "unittest"]
  mypy:       [".cve-venv/Scripts/python.exe", "-m", "mypy"]
  ruff:       [".cve-venv/Scripts/python.exe", "-m", "ruff", "check"]
  node-test:  ["node", "--test"]
  go-test:    ["go", "test", "./..."]
  jest:       ["npx", "jest"]
  vitest:     ["npx", "vitest", "run"]
  tsc:        ["npx", "tsc", "--noEmit"]
  eslint:     ["npx", "eslint"]
  phpunit:    ["php", "vendor/bin/phpunit"]
  phpstan:    ["php", "vendor/bin/phpstan", "analyse"]
```

- 受入基準は `{"verb": <登録済み verb>, "args": [...], "expect_exit": <int>}` のみ許可。
- 未登録 verb は構造検査で拒否（実行前に弾かれる）。
- args はシェル展開せず逐次引数で渡す（`shell=False`）。`rm -rf /` のような悪意ある引数も文字列として扱われ、決して実行されない。

### 4.2 CVE（Controlled Verification Environment）

- 検証は CVE（ハーネス側が管理する唯一の環境）で実行される。
- Node系 verb（`node-test`/`jest`/`vitest`/`tsc`/`eslint`）は対象プロジェクトの `node_modules` が
  前提。無い場合、CVE はTTYなら `npm install` 実行を y/N で確認し、非対話実行（`drive` の自動ループ等）
  では警告のみ出して検証をそのまま失敗させる（`harness/core/cve.py` の `_ensure_node_deps`）。
- 環境設定: `verification_env.yaml`（存在しない場合は `verification_env_sample.yaml` にフォールバック、警告出力）。
- 実行は `probe/n3/cve.py` の `verify` を再利用（pass/fail の判定はしない。判定は adjudicator の責務）。
- 検証フロー: 許可リスト検証 → CVE で probe+acceptance 実行 → 証拠を台帳に記録 → レビュアが証拠を読んで裁定。

**実装**: `harness/core/verifiers.py`（`VerifierRegistry`）、`harness/core/cve.py`（`CVE`）。

### 4.3 rubric（Implementer 自己採点）と受入テスト保護

acceptance は exit_code の pass/fail しか見ないため、「テストの主張を通すためにテスト自体を
書き換える／緩める」誤魔化しを exit_code だけでは検出できない。これを補う2つの仕組み:

- **受入テストファイル保護**: `decomposer.structural_check` は、acceptance のテストランナー系
  verb（`pytest`/`unittest`/`node-test`/`jest`/`vitest`/`phpunit`）の args が指すテストファイルが `touch_allow` に含まれて
  いたらハードエラーで拒否する（`_check_test_protection`）。実装者は自身の受入基準となる
  テストファイルを一切変更できない。mypy/ruff のように args が「検査対象の実装ファイル」を
  指す verb は対象外（それらは touch_allow に入っているのが正しい）。
- **rubric（自己採点）**: タスクは acceptance とは別に `rubric`（`{criterion, weight}` の配列、
  重み合計100、planner が作成）と `rubric_threshold`（合格ライン）を持てる。
  `implementer.IMPLEMENT_PROMPT` はこれを提示し、Implementer に「実装 → 受入基準を自分で
  再実行 → rubric で自己採点 → 合格ラインに達するまで改良」を**同一ショット内**で繰り返させ、
  最後に `{"self_score": {"total", "threshold", "breakdown"}}` を出力させる。
  `implementer.implement()` は `invoke.extract_result()` でこの JSON を回収し
  `task.implemented` イベントの `self_score` フィールドに記録する。
  **self_score はハーネスの裁定（verdict）を代替しない**——実行と判定の分離（§4冒頭の中心命題）
  により、最終的な合否は引き続き CVE 証拠のみに基づく `judgment`（review_flow/adjudicate）が
  決める。rubric はテスト保護と組み合わせることで初めて意味を持つ（テストが保護されていない
  状態で自己採点だけを導入すると、採点自体も同じ誤魔化しの対象になり得る）。

**実装**: `harness/roles/decomposer.py`（`_check_test_protection`）、
`harness/roles/implementer.py`（`_fmt_rubric` / `_extract_self_score`）。

---

## 5. コマンド仕様

全コマンドは `harness/cli.py` のサブコマンド。共通: `--dry-run` は実行をスキップして計画のみ出力。

### 5.1 `architect` — 設計決定を ADR として記録（Stage 1）

```
super-agent architect <requirement> [--design_file FILE] [--vendor V] [--model M] [--effort E] [--dry-run]
```

### 5.2 `plan` — 分解 + スケジュール（Stage 3）

```
super-agent plan [<requirement>] [--design_file FILE] [--task_file FILE] [--vendor V] [--model M] [--effort E] [--lease N] [--root DIR] [--dry-run]
```

- `--task_file`: タスク DAG 定義ファイル（存在しない場合は `--design_file` から分解して書き出す）。
- `--lease`: リース時間（秒、既定 3600）。
- `--root`: worktree ルート（既定 `workspaces`）。

### 5.3 `implement` — タスク実装 + コミット（Stage 4）

```
super-agent implement --task <id> [--task_file FILE] [--worktree DIR] [--vendor V] [--model M] [--effort E] [--dry-run]
```

- 指定タスクを worktree で実装（書き込み可）。`touch_allow` 外の変更は検出される。
- 成功でコミットし、台帳に `task.implemented` を記録。
- 実装者には `--mode plan` 等の読み取り専用フラグは付与**しない**（編集をブロックするため）。

### 5.4 `review` / `review-task` — 検証パイプライン実行（Stage 0/5）

```bash
# 任意のディレクトリを直接検証
super-agent review <dir> [--accept EXPR] [--expect-exit N] [--reviewer V] [--model M] [--effort E] [--budget N] [--dry-run]

# 実装済みタスク（Stage 4 成果物）を tasks.md から解決してレビュー
super-agent review-task --task ID [--task_file FILE] [--worktree DIR] [--reviewer V] [--model M] [--effort E] [--budget N] [--dry-run]
```

- `review <dir>`: 位置引数 `dir`（必須）で任意のワークツリー／題材ディレクトリを検証。
- `review-task --task ID`: `--task`（必須）で指定した実装済みタスクを `--task_file` から acceptance + worktree を解決してレビュー。
- 読み取り専用ロール。ベンダーには `--mode plan`（agy）等の読み取り専用フラグを付与。
- 台帳に `verification.run` / `reviewer.invoked`（vendor 記録）/ `judgment`（verdict）を記録。
- 裁定は CVE の証拠のみで下す。

### 5.5 `integrate` — 実装済みタスクの統合（Stage 5）

```
super-agent integrate --task <id> [--task_file FILE] [--worktree DIR] [--target BRANCH] [--dry-run]
```

- `task/<id>` を `--target`（既定 `main`）へ `--no-ff` マージ。コンフリクトは `--abort` して検知。
- マージ後、CVE で acceptance を再実行（失敗は `integrated.failed`）。
- 成功で `integrated` 記録、`git worktree remove --force` で後片付け。
- **注意**: 実行（dry-run なし）すると worktree が削除される。

### 5.6 `drive` — DAG 全タスク一括駆動（Stage B）

```
super-agent drive [--requirement TEXT] [--design_file FILE] [--task_file FILE] [--target BRANCH]
                  [--vendor V] [--reviewer R] [--model M] [--effort E]
                  [--implement-vendors "agy:2,hermes:3"] [--parallel-tasks] [--speculative]
                  [--adaptive|--no-adaptive] [--max-task-workers N] [--dry-run]
```

- `plan`→`implement`→`review`→`integrate` を **DAG（§1「用語: DAG」参照）全タスク**に実行。
- `--target` 省略時は `--design_file` から導出した `design/<stem>-<crc32>` ブランチが統合先になる（`design_branch_name()`、`harness/roles/scheduler.py`）。存在しなければ自動作成される。`--design_file` 未設定時のみ `main` にフォールバック。固定の統合先（例: `master`）を使いたい場合は `--target` で明示指定する。
- `--speculative` または `--implement-vendors` で複数チャンネル指定時のみ投機的モード（複数チャンネルが同じタスクを競い、最初に review を通した勝者を統合、他は破棄）。
- `--parallel-tasks`（既定 ON）: 独立タスクを topo レイヤー単位で並行。
- `--adaptive` / `--no-adaptive`: 駆動中の再計画スイッチ（§6）。
- `--max-task-workers N`: 同時タスク数上限（既定 4）。
- 既定（非投機的）: 各タスクを `roles.implement` の最初の1チャンネルのみで実装。

### 5.7 `evolve` — 自己改良（Stage 6）

```
super-agent evolve [--dry-run]
```

- 台帳の失敗パターン（同じ `pattern` が 3回以上）から `acceptance` テンプレまたは憲法への昇格案を提案。
- `--dry-run`: 提案表示のみ。実行時は `design.proposed` event を記録し対象ファイルへ追記。

### 5.8 `dashboard` — 台帳可視化

```
super-agent dashboard [--format md|html|both] [--out DIR]
```

- 台帳イベントを `build_model()` で task_id → status に集約し、`render_markdown()` / `render_html()` で描画。
- `--out` 指定時はファイル書き出し、未指定時は標準出力。
- 詳細は §9。

### 5.9 その他

- `status`: 最近の台帳イベントを表示。
- `log <task>`: 指定タスクprefix の台帳イベントを表示。
- `show`: 詳細表示。

---

## 6. adaptive モード

**フラグ**: `--adaptive` / `--no-adaptive`（既定 ON）

**正体**: `drive` の「再計画スイッチ」。topo レイヤー間で planner ロールがタスク DAG を再計画（re-plan）するかどうか。

- `--adaptive`（ON）: topo レイヤー境目で、実行結果（event ログ）を踏まえて planner がタスク分解を再考。
- `--no-adaptive`（OFF）: 初期の静的 DAG に従う。

**誤解に注意**: 回数指定ではない。「再計画する / しない」のみ。タスク数やリトライ回数とは無関係。

**実装**: `harness/roles/drive.py`（adaptive 引数）、`harness/cli.py`（フラグ宣言、既定 True）。

---

## 7. ベンダー呼び出しの自動リトライ（content-policy ブロック対策）

**動機**: hermes（Tencent Hunyuan `hy3:free`）はたまにコンテンツポリシーでブロックし、

```
你好，我无法给到相关内容。
```

（「すみません、関連する内容をお出しできません」）を返して停止する。インタラクティブでは人間が「続けて」と入力すれば回復するが、ワンショット実行ではそのまま失敗していた。

**仕様**:

1. `invoke()` が stdout/stderr を調べ、content-policy ブロックを検出。
2. ブロック検出時、同一セッションを `--resume <session_id>` で再開してリトライ（人間の「続けて」と等価）。
3. リトライ上限 `max_retries`（既定 **3**）。上限到達でもブロックが続く場合は最後の応答に `content_blocked: true` を付与して返す。
4. ブロックしない場合は1回で終わる（正常系は遅くならない）。

**検出指標**（`_is_content_blocked()`）: `content_policy_blocked` / `无法给到相关内容` / `你好，我无法` / `unable to provide`。

**セッションID抽出**（`_extract_session_id()`）: hermes は出力末尾に `session_id: <id>` を出す。resume 非対応ベンダーは次試行で同じ prompt を新セッションで再発行。

**実装**: `harness/core/invoke.py`（`invoke(max_retries=3)` / `_is_content_blocked()` / `_extract_session_id()`）。
**テスト**: `harness/tests/test_invoke.py`（`test_is_content_blocked_markers` / `test_extract_session_id` / `test_invoke_retries_on_content_block`）。

---

## 8. ワークツリー分離・受入基準パース

- 各タスク/チャンネルは独立 worktree（`workspaces/<task>__<vendor>_<i>`）で実行。レビュー結果は本流へ書き戻さない（worktree 隔離で読み取り専用の担保を補完）。
- タスク定義（DAG、§1「用語: DAG」参照）は Markdown: `## <id>` 見出しの下に `目標` / `依存` / `触ってよい範囲` / `受入基準 (N)`（verb リスト）を記述。`依存:` フィールドが DAG のエッジ（他タスクへの依存）を指定する。
- 受入基準の verb は §4.1 のホワイトリストに限定。
- 構造検査（`decomposer.structural_check`）が未登録 verb を弾く。

---

## 9. ダッシュボード（dashboard）

`harness/roles/dashboard.py`:

- `build_model(events)`: 台帳イベントを task_id → status に集約。**後勝ちではなく状態遷移の優先順位**（integrated > implemented > leased > created 等）で最終ステータスを決定。
- `render_markdown(model)` / `render_html(model)`: 描画（dashboard.py に実装）。
- テスト: `harness/tests/test_dashboard.py`。
