# Super Agent ハーネス — 使い方マニュアル

このドキュメントは、**完成した設計・実装を実際にどう動かすか**を説明します。
設計の意図（なぜこうなっているか）は `docs/ARCHITECTURE.md` を参照。

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
| Python | `.cve-venv`（uv 作成、pytest 入り）を使用 |
| ベンダーCLI | `claude` / `codex` / `agy` が PATH にあること |
| OS | Windows + git-bash（パスは `C:/...` / `D:/...` 表記） |

**実行に使う Python** は必ず `.cve-venv` のものを使ってください：

```bash
# このマシンでの例
export CVE=/d/vagrant/harnesses/super-agent/.cve-venv/Scripts/python.exe

# 動作確認
$CVE -c "import yaml, pytest; print('ok')"
```

> `.cve-venv` が無い場合：`uv venv .cve-venv && uv pip install pyyaml pytest`

作業ディレクトリは必ず `src/` の中で行ってください（`harness/` パッケージが解決できるため）：

```bash
cd D:/vagrant/harnesses/super-agent/src
```

---

## 2. コマンド一覧

現在 `super-agent` として使えるコマンドは **2つ**（Stage A）と、
**パイプライン呼び出し**（Stage C）です。

### 2.1 `super-agent run` — 要求を投入し台帳に記録

```bash
$CVE -m harness.cli run "<要求>" [--vendor claude|codex|agy] [--dry-run]
```

| オプション | 意味 |
|---|---|
| `--vendor` | 要求を処理させるベンダー（既定 `claude`） |
| `--dry-run` | **ベンダーを実際に起動せず**、組み立てるコマンドだけ確認 |

**何をするか**：要求を受け、`task.created` と `agent.invoked` の2イベントを
台帳（`harness/ledger/events.jsonl`）に書きます。まだ検証は走りません（並列/実行は未実装）。

**例**：
```bash
$CVE -m harness.cli run "build a fizzbuzz module" --vendor codex --dry-run
# → task T-XXXX recorded. ledger=...
```

### 2.2 `super-agent status` — 台帳の状態を表示

```bash
$CVE -m harness.cli status
```

台帳に記録された全イベントを（クラッシュセーフに）読み出して表示します。
```
events in ledger: 2
  T-418dd0b1:1 task.created
  T-418dd0b1:2 agent.invoked
```

---

## 3. 検証パイプラインを動かす（Stage C）

`run` はまだ検証を走らせません。**実際の「CVE実行→レビュー→裁定」**は
`harness/roles/review_flow.py` の `run_pipeline()` を呼び出します。

### 3.1 テスト題材（probe/n3/）

| 題材 | 内容 | 受理テスト |
|---|---|---|
| `caseGreen` | 1つの通るテスト | ✅ GREEN |
| `caseB` | 2ファイルの台帳（accounts/money）。実バグ（Money可変）あり | ✅ GREEN（バグはテストが拾えない） |
| `caseC` | util（retry/cache）。実バグ（attempts-1）あり | ❌ RED |
| `caseD` | 42ファイルの大きな差分用 | — |

### 3.2 CVE だけ走らせる（レビュアなし）

レビュアを呼ばず、CVE でテストを実行し、証拠（tree_hash 付き）を取る：

```bash
$CVE -c "
import sys; sys.path.insert(0,'.')
from harness.core.ledger import Sequencer
from harness.roles.review_flow import run_pipeline
seq = Sequencer('harness/ledger/events.jsonl'); seq.start()
j = run_pipeline('T-DEMO', 'probe/n3/caseGreen',
                 [{'verb':'pytest', 'args':['tests/'], 'expect_exit':0}],
                 reviewer_vendor='codex', seq=seq, dry_run=True)
seq.stop()
import json; print(json.dumps(j, ensure_ascii=False, indent=2))
"
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

> `dry_run=True` だと**レビュアを呼ばない**ので、レビュアの出力が無く
> `judgment_unavailable` になります。これは**正しい動作**です。
> CVE 自体は実行されており、`tree_hash` が束縛されています。
> 「受理テストが RED なら fail」を確かめたい場合は次の 3.3 の通り
> `dry_run=False` にしても、レビュアが構造化出力を返せば判定が出ます。

### 3.3 レビュアも本番呼び出し（claude / codex）

```bash
$CVE -c "
import sys; sys.path.insert(0,'.')
from harness.core.ledger import Sequencer
from harness.roles.review_flow import run_pipeline
seq = Sequencer('harness/ledger/events.jsonl'); seq.start()
j = run_pipeline('T-DEMO2', 'probe/n3/caseB',
                 [{'verb':'pytest', 'args':['tests/'], 'expect_exit':0}],
                 reviewer_vendor='claude', seq=seq, dry_run=False)
seq.stop()
import json; print(json.dumps(j, ensure_ascii=False, indent=2))
"
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

パイプラインが書いたイベントは全て台帳に残ります：

```bash
$CVE -c "
import sys; sys.path.insert(0,'.')
from harness.core.ledger import Ledger
for e in Ledger('harness/ledger/events.jsonl').load():
    print(e['event_id'], e['type'], e.get('tree_hash',''))
"
```

```
T-DEMO:1 task.created
T-DEMO:2 verification.run 3309c1ea35679a40   ← CVE の証拠（tree_hash 束縛）
T-DEMO:3 brief.built
T-DEMO:4 reviewer.invoked
T-DEMO:5 judgment 3309c1ea35679a40          ← 裁定も同じ tree_hash
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
$CVE -m pytest harness/tests/ -q
# ...........  11 passed
```

- `test_invoke.py`（6）：ベンダー呼び出しコマンドの組み立て（A-1〜A-6 実測値）
- `test_ledger.py`（3）：台帳の原子性（H3）
- `test_pipeline.py`（2）：パイプラインの CVE 実行＋tree_hash 束縛＋裁定記録

---

## 6. 今できないこと（未実装）

以下は**設計のみ**。マニュアルに書かれていても、まだ動きません：

- `super-agent run` での**実際の並列実装・リース・worktree**（Stage B）
- `pause` / `resume` / `abort` / `amend` / `show` コマンド（Stage D' 操作面）
- 予算上限での自動停止・承認キュー（Stage D）
- レビュアの OS レベル隔離（Stage F）

これらは `docs/IMPLEMENTATION_PLAN.md` の Stage B〜F を参照。

---

## 7. トラブルシューティング

| 現象 | 原因 / 対処 |
|---|---|
| `ModuleNotFoundError: yaml` | `.cve-venv` を使っているか確認。`uv pip install pyyaml` |
| `verdict: judgment_unavailable` | `dry_run=True` の場合は正常（レビュアを呼んでいない）。`False` でもベンダーが構造化出力を返せない環境の場合（偽 fail ではない） |
| CVE の `cve_ok: False` | `verification_env.yaml` の python パスが通っていない。パスを確認 |
| `ModuleNotFoundError: pytest`（CVE実行時） | `verifiers.yaml` の python が venv を指しているか確認 |
| 台帳が汚れる | `harness/ledger/` は `.gitignore` 対象。消して良い |
