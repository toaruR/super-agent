# 実装計画 — 大枠設計を動くハーネスにする

- 作成日: 2026-08-06 ／ 改訂: 2026-08-08（「動かしながら確認できる順」へ再構成＋master 一本化）
- 現在の状態: 設計 v4（89点/合格）
  - **済**: Stage A（台帳・アダプタ・invoke・CLI）／ Stage C（検証パイプライン ⑤⑥⑦⑨）
  - **済**: Stage 0（review/log/show）／ Stage 1（architect）／ Stage 2（decomposer）／
         Stage 3（scheduler: worktree+リース）／ Stage 4（implementer）／
         **Stage 5（integrator: 統合+worktree後片付け）**
  - **済**: Stage 6（evolve 改良: improver.py + `evolve` コマンド）／ Stage B（並列駆動）
  - **未実装**: Stage 7（D 予算・D' 操作面・F OS隔離）
- ゴール: `super-agent <サブコマンド>` で §9 の一周（要求→改良）を、**各段階が独立して動作確認できる**形で完成させる
- 品質基準: 各段階で「実際にコマンドを叩いて結果を見る」ことを完了条件にする

---

## 0. 基本方針 — 「動かしながら確認できる」とは

各ステージは **CLI サブコマンド1本で動かせる** ように切る。前段の出力が次段の入力になり、
ユーザーはいつでも `super-agent status` / `super-agent log <task>` で台帳を覗ける。

| §9 | サブコマンド（予定） | 入力 | 出力（台帳イベント） |
|---|---|---|---|
| ① Architect | `architect "<要求>"` | 要求文 | `adr.written` |
| ② Decomposer | `decompose <要求>` | 要求文 | `task.created` + `acceptance` |
| ③ Scheduler | `plan <要求>` | 要求（分解済み） | `task.leased` + worktree 作成 |
| ④ Implementer | `implement <task>` | task_id | `artifact.produced` + commit |
| ⑤⑥⑦⑨ | `review <dir>` | ワークツリー | `verification.run` / `judgment`（**済**） |
| ⑧ Integrator | `integrate <task>` | task_id | `integrated`（**済**） |
| ⑩ 改良 | `evolve` | 台帳 | `design.proposed`（**済**: improver.py + `evolve` コマンド） |

**進め方の鉄則**: 各ステージは「そのステージだけで `super-agent <cmd>` を叩き、
台帳にイベントが残り、ユーザーが結果を目で確認できる」ことを完了条件とする。
一気に全部は書かない。一段完了ごとにコミット。

---

## 1. ディレクトリ構成（完成形）

```
src/harness/
  config/        vendors.yaml / verification_env.yaml / verifiers.yaml / prices.yaml
  core/          ledger.py invoke.py cve.py brief.py adjudicate.py verifiers.py budget.py
  roles/         review_flow.py(済) decomposer.py scheduler.py implementer.py integrator.py improver.py architect.py
  cli.py         super-agent <subcommand>
  tests/         test_*.py
```

> 書くのは「プロンプト組み立て＋既存部品の呼び出し＋台帳駆動の制御フロー」のみ。
> ロジック核心（verify/run/build/adjudicate/Ledger）は既実装済み。

---

## 2. 段階（§9 の順＝動かしながら確認）

> 凡例: ✅済 / 🔜今回 / ⏳後続
> 各段に **動作確認コマンド** と **完了条件** を書く。

### Stage 0 — 動かしやすくする小さな足場（✅ 完了）

**目標**: 既に動く⑤⑥⑦⑨を1コマンドで試せるようにし、以降のステージも同じ形で差し込める。

1. `cli.py` に `review <dir>` サブコマンドを追加（既存 `review_flow.run_pipeline` を呼ぶ）。
   - `super-agent review probe/n3/caseGreen` で CVE→簡報→レビュー→裁定が走る。
2. `cli.py` に `log <task>` サブコマンドを追加（台帳から特定タスクのイベント列を表示）。
3. `cli.py` に `show design` / `show plan` を追加（L6 読み取り操作の先行実装）。

**動作確認**:
```
super-agent review probe/n3/caseGreen
super-agent log T-XXXX
super-agent status
```
**完了条件**: ドキュメントの通りに打てば⑤⑥⑦⑨が一人で確認できる。

---

### Stage 1 — ① Architect（要求→設計の記録）（✅ 完了）

**目標**: 要求に対する設計決定を ADR 形式で台帳に残す。まずは「人間が書いた設計を登録」
から始め、その後 LLM に起案させる。

1. `roles/architect.py`: 要求を受け、設計方針を LLM に起案させる（read-only）。
   最初は人間が `architect --spec <file>` で設計を渡す形でも可。
2. 台帳に `adr.written`（決定内容・根拠）を記録。

**動作確認**:
```
super-agent architect "Web API を作れ" --spec my-design.md
super-agent log <task>     # adr.written があることを確認
```
**完了条件**: 要求から設計決定が台帳に残る。LLM 起案は `--dry-run` でコマンド確認。

---

### Stage 2 — ② Decomposer（architectの設計→タスク分解）（✅）

**目標**: `architect` が作った設計ファイル（`--spec`）を受け取り、そこから DAG + acceptance[].verb を出し、構造検査する。

1. `roles/decomposer.py`: LLM に分解させ、`{"task_id","goal","acceptance":[{"verb","args"}],"depends_on"}` を返す。
2. §6.2 構造検査: acceptance 空 / verb 未登録 / DAG 循環 / touch_allow 重複 → 差し戻し。
3. 各タスクを台帳に `task.created` として記録。

**動作確認**:
```
super-agent decompose "Web API を作れ"
# → タスク一覧 + acceptance が表示される
super-agent status          # task.created が増えている
```
**完了条件**: 要求を入力すると、検査済みのタスクDAGが台帳に残る。

---

### Stage 3 — ③ Scheduler（編成・worktree・リース）（✅ 完了）

**目標**: タスクに役割を割り当て、worktree を作り、リースを発行する。まずは**直列1タスク**から。

1. `roles/scheduler.py`:
   - タスクごとに `git worktree add` で作業ツリーを作成。
   - 役割（Implementer 等）をベンダーに割り当て、`task.leased` + `lease_until` を記録。
   - 全イベントを `Sequencer` 経由で台帳へ（H3）。
2. リース期限切れ→再割当（§6.3）。並列は後で（Stage 3b）。

**動作確認**:
```
super-agent plan "Web API を作れ"      # decompose + 編成を通す
super-agent log <task>                 # task.leased + worktree パスを確認
git worktree list                      # worktree ができている
```
**完了条件**: 要求1件で worktree が1つでき、リースが台帳に残る。

---

### Stage 4 — ④ Implementer（実装・commit）（✅ 完了）

**目標**: ベンダーに worktree で実装させ、commit させる。

1. `roles/implementer.py`: `invoke` でベンダーに実装を依頼（write 権限・自 worktree のみ）。
   `touch_allow` 外への書き込みは拒否。
2. 完了で `artifact.produced`（paths + commit）を台帳に記録。

**動作確認**:
```
super-agent implement <task_id>
super-agent log <task_id>     # artifact.produced + commit hash
git -C <worktree> log --oneline
```
**完了条件**: `implement` で実際にコードが書かれ、commit される。

---

### Stage 5 — ⑧ Integrator（統合）（✅）

**目標**: 実装済みタスクの worktree を統合ブランチへマージ/検証。

1. `roles/integrator.py`: 
   - `touch_allow` 外の変更を検出して差し戻し（`integration.touch_violation`）。
   - `task/<id>` を `--target`（既定 main）へ `--no-ff` マージ。`conflict` は `--abort` して検知。
   - マージ後 acceptance（CVE）を再実行し GREEN を確認。失敗は `integrated.failed`。
   - 成功で `integrated` 記録＋`git worktree remove --force` 後片付け。
2. `cli.py` に `integrate --task <id> --tasks <dag> [--target main] [--dry-run]` を追加。

**動作確認**:
```
super-agent integrate --task T1 --tasks ./probe/sample/my-design-tasks.md --dry-run  # ok:true、worktree は維持
super-agent integrate --task T1 --tasks ./probe/sample/my-design-tasks.md            # 統合→integrated, worktree 消える
super-agent log <task_id>     # integrated
git worktree list            # worktree が消えている
```
**完了条件**: implement 済みタスクを統合でき、worktree が綺麗に消える（単体テスト4件: success/conflict/violation/verify-fail）。

---

### Stage B — 並列駆動（実装 + タスク並列）（✅）

**目標**: `drive` で DAG 全タスクを一括駆動する際、① 各タスクは**単一チャンネル**で実装（投機的 fan-out なし）、② 独立タスクは**自動でタスクレベル並列**に実行する。

**投機的実装（マルチチャンネル競争）は非デフォルトのモード**: `--speculative` フラグ（または `--implement-vendors` で複数チャンネルを明示）を付けたときのみ有効。その際は `roles.implement` の全チャンネルで同じタスクを並列実装し、最初に review を通した勝者を統合、他は破棄。

1. `harness/core/invoke.py`: `resolve_role_channels(role)` を追加。`roles.implement` はチャンネルリスト（各エントリ = 1チャンネル、model/effort 自由指定）。単一辞書も後方互換。`parse_channel_override("agy:2,hermes:3")` で CLI 指定を解析。
2. `harness/roles/drive.py`:
   - **デフォルト（非投機的）**: 各タスクを `channels = resolve_role_channels("implement")[:1]` で**単一チャンネル**実装。`speculative=False` のときは常に最初の1チャンネルのみ。
   - **タスク並列（デフォルトで有効）**: 依存のないタスクを topo レイヤー単位で並行駆動（`scheduler.topo_layers`）。integrate（git checkout/merge）は共有リポジトリのため直列に実行。
   - **投機的モード（opt-in）**: `--speculative` または `implement_channels` が複数のとき、`channels` を全fan-out。各チャンネルが独立 worktree `workspaces/<tid>__<vendor>_<i>` / branch `task/<tid>__<vendor>_<i>` で並列実装（ThreadPoolExecutor）。review を通した最初のチャンネルを winner として統合、他は破棄。
   - **cleanup**: 統合後に各タスクの全チャンネル worktree を `teardown_worktree` で破棄（敗者チャンネルも残らない）。
3. `harness/roles/scheduler.py`: `topo_layers()`（タスクを依存レイヤーに分割。レイヤー0=依存なし）、`teardown_worktree()`（worktree+branch を idempotent に削除）を追加。
4. `harness/cli.py`: `--implement-vendors "agy:2,hermes:3"`（緊急オーバーライド）、`--parallel-tasks`（デフォルトで有効）、`--speculative`（投機的モードの opt-in）、`--max-task-workers N` を追加。

**動作確認**:
```bash
# デフォルト: 各タスクを単一チャンネルで実装、独立タスクは自動並行
super-agent drive --tasks ./probe/sample/my-design-tasks.md
# 投機的モード: 各タスクを roles.implement の全チャンネルで競わせ、勝者を統合
super-agent drive --tasks ./probe/sample/my-design-tasks.md --speculative
# 明示的チャンネル数指定でも投機的になる（agy 1 + hermes 1 = 2チャンネル競争）
super-agent drive --tasks ./probe/sample/my-design-tasks.md --implement-vendors "agy:1,hermes:1"
# implement チャンネルのモデル/effort を CLI で上書き（全チャンネルに適用）
super-agent drive --tasks ./probe/sample/my-design-tasks.md --model tencent/hy3:free --effort high
# worktree 確認: 実行中は各タスク/チャンネルの worktree が並び、終了後は綺麗に消える
git worktree list
```
**完了条件**: デフォルトの単一チャンネル実装＋タスクレベル並列が実ベンダーで完走し、敗者チャンネルの worktree が残らない。投機的モードは `--speculative` 時のみ複数チャンネルを起動（65 passed）。

> **実測で判明した運用事実（2026-08-08）**:
> - `vendors.yaml` の `roles.implement` は現在 **hermes(hy3:Free) ×5** に設定（agy/codex はコメントアウト）。
> - `hy3:Free` は yaml にそのまま書くが、**コード側 `normalize_model()` が `tencent/hy3:free` に自動正規化**する（OpenRouter カタログ名との乖離を吸収）。yaml を `tencent/hy3:free` に書き換える必要はない。
> - `roles.review` は現在 **agy（gemini-3.6-flash）**。drive の review 記録は実際の reviewer vendor（agy）を使うよう修正済み（`fix(drive): review フェーズの記録に実際の reviewer を表示する`）。
> - drive 経由の hermes 5チャンネル並列 implement を実測: implement 4/5 成功（hy3:free 無料枠の不安定で 1 チャンネル失敗）、review(agy) を通した勝者を統合。

---



---

### Stage 6 — ⑩ 改良（evolve: 自己改良）（✅ 完了）

**目標**: 台帳から失敗パターンを拾い、憲法/テンプレへ昇格（G6 自己改良）。

1. `roles/improver.py`: 台帳を読み、同種 fail 3回で `acceptance` テンプレへ昇格、または憲法へ書き込む（§9.1）。実装: `cve_ok==False` / `verdict in (fail,reject,blocked)` / 非ゼロ returncode を失敗と判定、`pattern` でグループ化、しきい値3回で提案生成。
2. `super-agent evolve` で起動（`--dry-run` で提案のみ表示、実行で `design.proposed` を台帳に記録＋対象ファイルへ追記）。

**動作確認**:
```
super-agent evolve --dry-run     # 提案される design.proposed を確認
super-agent evolve              # 承認すると design.proposed が記録
```
**完了条件**: 失敗パターンからの提案が台帳に残る（harness/tests/test_improver.py: 5 passed）。

---

### Stage 7 — D（予算・承認キュー）・D'（操作面）・F（OS隔離）（⏳ 未実装・運用成熟度）

- **D**: `budget.py` でトークン正規化＋価格換算。超過で停止。`status` に予算表示。
- **D'**: `pause`/`resume`/`abort`/`amend` を cli に追加（L6）。
- **F**: レビュアの OS レベル隔離（H2 でインジェクション済み、深層防御）。

これらは「動く一周」ができてから。

---

## 3. 推奨順序（動かしながら確認）

```
Stage 0（足場: review/log/show）     → 既存⑤⑥⑦⑨を1コマンドで試せる
  → Stage 1（architect）             → 要求から設計が残る
  → Stage 2（decomposer）            → 設計からタスクDAGが残る
  → Stage 3（scheduler: 直列）       → タスクから worktree+リースが残る
  → Stage 4（implementer）           → worktree で実装+commit される
  → Stage 5（integrator）            → 統合されて worktree が消える
  → [ここで §9 の①〜⑧ が一人で動く]
  → Stage 6（evolve）                → 失敗から改良が残る（⑩）
  → Stage 7（D/D'/F）                → 運用成熟度
```

各ステージは**前段の出力（台帳のイベント）を入力にする**ので、順に積み上がる。
飛ばさず一段ずつ `super-agent <cmd>` で目で確認して進める。

**「完成」の定義**: `super-agent plan "..."` 一発で ①要求→②分解→③編成→④実装→
⑤⑥⑦検証裁定→⑧統合 が回り、⑩で失敗から自己改良される。すべて台帳に証拠が残る。

---

## 4. リスクと未解決

| # | リスク | 対応 |
|---|---|---|
| R1 | ベンダーCLIの出力形式が変わる | `invoke.py` の `result_path` で吸収。起動時プローブ（U3）で検出 |
| R2 | worktree の後片付け漏れでリポジトリが汚れる | リース/統合解除時に `git worktree remove` を確実に呼ぶ |
| R3 | 長時間タスクのリース妥当値（U4） | 初期値15分、実測で調整。Stage 3 で計測 |
| R4 | Windows での OS 隔離（F）が困難 | H2 でインジェクション済み。隔離はベストエフォート、不可能なら §12 に記す |
| R5 | LLM が acceptance を不正に組めないか | §6.2 構造検査（verb ホワイトリスト）で落とす。H2 と同じ防御 |

---

## 5. 次の一手

**2026-08-08 時点の状態**: 全機能（Stage 0〜6 + Stage B）が `master` 一本に統合済み。
`feat/dashboard` / `feat/planner` は `master` へマージして削除。`main` は事故生成物として削除。
作業ツリーは clean、ローカル `master` と `origin/master` は一致（`cc9addb`）。

**残課題（運用成熟・Stage 7）**:
1. `budget.py`（D）の実装 — トークン正規化＋価格換算、超過停止、`status` への予算表示。
2. `pause`/`resume`/`abort`/`amend`（D'）の cli への追加（L6）。
3. レビュアの OS レベル隔離（F）の本実装（Windows ではベストエフォート）。

**次に手を入れるなら**: Stage 7 の D（予算）から。現状は「動く一周」ができているので、
運用でどこが辛いか（予算見えない／途中停止できない）を実測して優先順位を決める。
