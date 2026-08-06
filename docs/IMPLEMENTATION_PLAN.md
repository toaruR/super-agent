# 実装計画 — 大枠設計を動くハーネスにする

- 作成日: 2026-08-06
- 現在の状態: 設計 v4（89点/合格）／ 部品5つは `probe/n3/` に実装済み／ 統合ハーネスは未実施
- ゴール: `super-agent run <要求>` 一発で、**S1→S7 を通る最小ハーネス**を動かす
- 品質基準: 合格は「設計品質」ではなく「本番相当の動作」。各段階でユニット/統合テストを通す

---

## 0. 既存部品（再利用する。書き直さない）

| ファイル | 役割 | 公開インターフェース |
|---|---|---|
| `probe/n3/cve.py` | CVE（唯一の実行環境） | `verify(root, acceptance, probe) -> {cve_ok, tree_hash, evidence[]}` |
| `probe/n3/verifiers.py` | 許可リスト実行 | `run(acceptance, cwd) -> {ok, exit_code, argv, stdout, stderr}` |
| `probe/n3/brief.py` | 簡報生成 | `build(ev, changed, context, budget) -> (text, tokens_est, dropped[])` |
| `probe/n3/adjudicate2.py` | 機械裁定 | `adjudicate(evidence, review) -> {verdict, why, tree_hash, advisory[]}` |
| `probe/n3/ledger.py` | 台帳 | `Ledger(path)` / `Sequencer(path)` |

> これらは「実験の証跡」として `probe/n3/` に置かれている。
> **実装計画では、`src/harness/` に製品コードを新規作成**し、必要なら上記から関数を
> そのまま import する（コピーしない）。`probe/n3/` は回帰テストの題材として残す。

---

## 1. ディレクトリ構成（新規 `src/harness/`）

```
src/harness/
  __init__.py
  config/
    vendors.yaml          # §4 アダプタ宣言（claude/codex/agy）
    verification_env.yaml # §3.2 CVE 定義
    verifiers.yaml        # §6.2 H2 許可リスト
    prices.yaml           # §5.5 C6 価格表
  core/
    ledger.py             # Ledger/Sequencer を probe から昇格（そのまま import）
    invoke.py             # §4 ベンダー呼び出し（構造化出力・再開・権限フラグ）
    cve.py                # §3.2 CVE ラッパ（verify を呼ぶ）
    brief.py              # §5.6 簡報（build を呼ぶ）
    adjudicate.py         # §7.2 裁定（adjudicate を呼ぶ）
    verifiers.py          # §6.2 許可リスト（そのまま import）
  roles/
    decomposer.py         # §6.2 タスク分解 + 構造検査
    scheduler.py          # §6.3 リース + 割当 + 台帳追記（Sequencer 利用）
    integrator.py         # §9 統合
  cli.py                  # super-agent status / run
  tests/
    test_ledger.py        # probe/n3/ledger_test.py を昇格
    test_invoke.py        # 構造化出力・再開の回帰（E-2a/A-1/A-2）
    test_pipeline.py      # 最小パイプライン統合テスト（caseB で end-to-end）
```

> **原則**: 各モジュールは「プロンプト組み立て＋既存部品の呼び出し」に留める。
> ロジックの核心（verify/run/build/adjudicate/Ledger）は既に実装済みなので、書くのは
> **接着コードと台帳駆動の制御フロー**のみ。これが「小さく始める」の意味。

---

## 2. 段階（S1 → S7）

### Stage A — S1: 台帳 + アダプタ + invoke（基盤）

**目標**: 3ベンダーを同一IFで叩き、台帳にイベントを残せる。

1. `core/ledger.py`: `probe/n3/ledger.py` を `src/harness/core/ledger.py` に配置（そのまま、パス調整のみ）。`tests/test_ledger.py` も移動。
2. `core/invoke.py`: ベンダー宣言（vendors.yaml）を読み、以下を行う。
   - 構造化出力: claude=`--json-schema` インライン / codex=`--output-schema` ファイル（A-5）
   - 再開: claude=`--resume` / codex=`exec resume` + `resume_*` フラグ（A-2）
   - 権限: claude=`--allowedTools Read,Grep,Glob` / codex=`--sandbox read-only` / agy=`--mode plan --add-dir {worktree}`（A-6/§5.6）
   - 出力: `result_path` に従って抽出（A-3）
3. `core/cve.py`, `core/brief.py`, `core/adjudicate.py`, `core/verifiers.py`: 既存部品を import する薄いラッパ。
4. `roles/scheduler.py` の骨格: 台帳へ `task.created` / `agent.invoked` を書く。

**テスト**: `test_invoke.py` — claude/codex/agy に対し実際に構造化出力を取り、スキーマ通りにパースできること（E-2a）。ローカルで実ベンダーを叩くため、CI では `--dry-run`（コマンド組み立てだけ）に切り替え。

**完了条件**: `super-agent run "..."` が台帳にイベントを1件以上残す。

---

### Stage B — S3: 席 + リース + worktree（並列の土台）

**目標**: 同一要求を N 並列で実装でき、クラッシュしても放置されない。

1. `roles/decomposer.py`: 要求→DAG + `acceptance[].verb` リスト。構造検査（§6.2 表: 空/verb未登録/DAG循環/touch_allow重複）。
2. `roles/scheduler.py`:
   - 各タスクに git worktree を作成（`git worktree add`）。
   - リース発行（`task.leased` + `lease_until`）。heartbeat で更新。
   - リース期限切れ→別ベンダーへ再割当（§6.3）。
   - 全イベントを `Sequencer` 経由で台帳へ（H3）。
3. `core/invoke.py` に worktree パス差し込み（agy の `--add-dir`）。

**テスト**: 2タスクの並列リース→片方を強制終了→再割当が台帳に記録される（モックベンダー使用）。

**完了条件**: 並列実装が競合なく終わり、台帳から状態が再構成できる。

---

### Stage C — S2/S4: 検証パイプライン統合（既部品の接着）

**目標**: CVE → 簡報 → レビュー → 裁定 を台帳駆動でつなぐ。

1. `roles/review_flow.py`（新規）: 以下を順に実行し、各結果を台帳に残す。
   - `cve.verify(worktree, acceptance, probe)` → `verification.run`
   - `brief.build(evidence, changed_files, context, budget)` → 簡報テキスト
   - `invoke.review(vendor, brief, worktree)` → `reviewer` の `findings`
   - `adjudicate.adjudicate(evidence, review)` → `judgment`
2. tree_hash の束縛（H4）を `verification.run` イベントに含める。
3. 意見が割れても裁定が一致し、advisory が保持されることを `test_pipeline.py` で確認。

**テスト**: `caseB`（2ファイル相互依存）で end-to-end。期待: CVE pass → 簡報生成 → レビューで欠陥検出 → 裁定 `pass`（advisory に保持）。

**完了条件**: 1要求が「実装→検証→レビュー→裁定」を通り、台帳に全イベントが残る。

---

### Stage D — S5: 予算 + status（人間の負荷一定化）

**目標**: コスト上限と承認キュー。

1. `core/budget.py`（新規）: トークン正規化（A-4）+ `prices.yaml` 換算（C6）。超過で停止。
2. `cli.py`: `super-agent status` が台帳から `running/blocked/awaiting_review/budget` を1画面出力（§8.2）。
3. 承認キュー: `escalation` イベントを `approvals/pending/` に書き出し、人間が承認すると `judgment` が確定。

**テスト**: 予算超過で pipeline が安全側（停止・途中成果保持）に倒れる。

**完了条件**: `status` が人間に必要な情報のみを出し、承認ゲートが機能する。

---

### Stage E — S6: 改良ループ（G6）

**目標**: 同じ失敗を二度させない。

1. `roles/improver.py`（新規）: 台帳を読み、同種 fail 3回で `acceptance` テンプレへ昇格、あるいは憲法へ書き込む（§9.1）。
2. 改良タスク自身を `super-agent run` に再投入できること（自己適用 G6）。

**テスト**: 意図的な fail 連発でテンプレ昇格が起きる。

---

### Stage F — S7/U6: OS レベル隔離（残余リスクの封じ）

**目標**: レビュアの実行副作用（外部通信等）を OS 側で封じる。

1. レビュア呼び出しを `bwrap`（Linux）/ 同等の namespace 隔離で包む。Windows は制約あり→ `firejail` 相当か、CVE と同じく「実行されても平気」設計で補う。
2. H2 でインジェクションは塞がっているので、ここは「ベストエフォートの深層防御」。

**テスト**: レビュアからの外部通信がブロックされることを確認（可能なら）。

**完了条件**: 実行副作用が OS 側で抑制される（不可能なら設計上の理由を §12 に記す）。

---

## 3. 優先順位とスコープ

**最小で価値が出る順**: A（基盤）→ C（検証パイプライン） → B（並列） → D（status） → E（改良） → F（隔離）。

- **A + C が最優先**: これで「要求→検証→裁定」が一台で動く。設計の存在理由（判定の信用）が
  実際に動く形になる。
- B は「並列したい」ときには必須だが、まずは直列で動かす方が早期に価値が出る。
- D/E/F は運用成熟度。F は H2 で大部分がカバー済みなので最後に回す。

**この計画での「完成」の定義**: Stage A + C が終わり、`super-agent run "<要求>"` が
caseB 相当のタスクを end-to-end で検証・裁定できる状態。**それ以降は並列・運用・隔離の拡張。**

---

## 4. リスクと未解決（実装中に浮上しうる）

| # | リスク | 対応 |
|---|---|---|
| R1 | ベンダーCLIの出力形式が変わる | `invoke.py` の `result_path` で吸収。起動時プローブ（U3）で検出 |
| R2 | worktree の後片付け漏れでリポジトリが汚れる | リース解除時に `git worktree remove` を確実に呼ぶ |
| R3 | 長時間タスクのリース妥当値（U4） | 初期値15分、実測で調整。Stage B で計測 |
| R4 | Windows での OS 隔離（F）が困難 | H2 でインジェクション済み。隔離はベストエフォート、不可能なら §12 に記す |

---

## 5. 次の一手

**Stage A を開始する。** 具体的には:
1. `src/harness/` を作成し、`core/ledger.py` を `probe/n3/ledger.py` から移動（テスト共々）。
2. `vendors.yaml` / `verification_env.yaml` / `verifiers.yaml` を `config/` に書く。
3. `core/invoke.py` を書く（構造化出力・再開・権限フラグの実装）。
4. `test_invoke.py` で claude/codex の実構造化出力を確認。

この4つが終わったら Stage A 完了とし、コミットする。その後 Stage C へ。
