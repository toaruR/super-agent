# 異ベンダー・エージェントチーム — 大枠設計 (v3)

- 版: v3（v1→v2→v3 の反復結果。採点は `SCORING.md`）
- 前提の実測: `EVIDENCE.md` / ゴールと評価基準: `GOAL_AND_RUBRIC.md`
- 対象: Claude Code / Codex / Antigravity(agy) / Hermes ほか

---

## 0. 設計の中心命題

実測 E-4 が本設計の出発点である。

> claude が書いた**論理的に正しい**コードを codex がレビューし、**fail** と判定した。
> 原因は成果物ではなく、**レビュア側の実行環境**（python が見つからない）だった。

ここから導かれる命題:

> **マルチエージェントの本質的な難所は「連携」ではなく「判定の信用」である。**
> エージェントに「動かして確かめて合否を言え」と頼む限り、我々が測っているのは
> 成果物の品質ではなく**そのエージェントの環境**である。

したがって本設計の背骨は次の一点に置く。

> ## 実行と判定を分離する。
> **決定的な検証はハーネスが唯一の環境で実行し、エージェントは「証拠を読む」だけにする。**

エージェントは賢い個人ではなく、**交換可能な作業員**として扱う。
信頼は人格ではなく、**証拠と手続き**に置く。

---

## 1. 全体像

```
        ┌──────────────────────────────────────────────┐
 人間    │ L5 承認ゲート  approvals/ (要判断だけが上がる) │
 ↕      ├──────────────────────────────────────────────┤
        │ L4 検証と信用                                  │
        │   決定的検証器(唯一環境) → 証拠 → 判定 → 分類器 │
        ├──────────────────────────────────────────────┤
        │ L3 統制  タスクDAG・役割割当・予算・リース       │
        ├──────────────────────────────────────────────┤
        │ L2 台帳(単一真実源)  append-only JSONL + 派生状態│
        ├──────────────────────────────────────────────┤
        │ L1 アダプタ  能力宣言(宣言データ)・呼出正規化    │
        ├──────────────────────────────────────────────┤
        │ L0 基盤  git worktree 隔離 / 唯一の検証環境      │
        └──────────────────────────────────────────────┘
              ↓ 呼び出しは常に「一発実行」
        claude -p / codex exec / agy -p   （常駐しない）
```

**情報の流れの鉄則**: エージェント同士は直接会話しない。
すべての伝播は **L2 台帳を経由**する。エージェントの入力は台帳から生成した簡報(briefing)、
出力は型付きJSON。散文の受け渡しは存在しない。

---

## 2. 基本方針（5つ）

| # | 方針 | 根拠 |
|---|---|---|
| P1 | **一発実行、常駐なし** | 本機に `inotifywait` 無し(E-1)。常駐は復旧を難しくする。プロセスは落ちて当然のものとして扱う |
| P2 | **実行と判定の分離** | E-4。合否の根拠は唯一環境の実行結果のみ |
| P3 | **証拠なき判定は無効** | E-4 / A-3。判定は必ず証拠IDを引用する |
| P4 | **状態は台帳にのみ在る** | プロセス内状態は落ちたら消える。台帳は append-only |
| P5 | **能力は宣言、分岐は禁止** | ベンダー差はデータで表現し、コードに `if vendor ==` を書かない |

---

## 3. L0 基盤 — 隔離と「唯一の検証環境」

### 3.1 作業隔離: タスク1件 = git worktree 1本

```
workspaces/<task_id>/     ← git worktree（ブランチ task/<task_id>）
```

並列実装が同一ファイルを壊し合う事故を、設計段階で不可能にする。
統合は L4 通過後に Integrator がまとめて行う。

### 3.2 ★唯一の検証環境 (Canonical Verification Environment; CVE)

**本設計で最も重要な構成要素。**

- テスト・ビルド・lint・実行は **CVE でのみ** 走る。
- CVE は1つだけ定義され、そのバージョンは台帳に記録される。
- **エージェントのサンドボックス内での実行結果は、合否の根拠として採用しない。**

```yaml
# config/verification_env.yaml
cve:
  id: "local-win-py311"
  shell: bash
  interpreters:
    python: "C:/Users/toaru/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe"
  probe:            # 起動時セルフチェック。ここが落ちたら全タスクを止める
    - "python -c 'import sys;print(sys.version)'"
    - "git --version"
```

E-4 の偽fail は、この一枚で構造的に消える。
codex の環境に python が無くても、CVE に在れば検証は通る。
逆に CVE のプローブが落ちていれば、**タスクを開始せずに人間へ上げる**（環境障害をタスク失敗として記録しない）。

---

## 4. L1 アダプタ — 能力宣言（コード分岐を書かない）

ベンダー差は **YAMLの宣言**として表現する。新ベンダー追加＝ブロック1つ追加。

```yaml
# config/vendors.yaml
claude:
  headless:  ["claude","-p","{prompt}"]
  structured: {flag: "--json-schema", form: inline}      # 実測: インライン文字列
  session:
    id_origin: caller                                     # ★呼び側がUUIDを決められる
    start: ["--session-id","{sid}"]
    resume: ["--resume","{sid}"]
  permission:
    readonly:  ["--tools",""]
    write:     ["--permission-mode","acceptEdits"]
  result_path: ".structured_output"
  cost_path:   ".total_cost_usd"                          # 実測: USD直値を返す唯一の存在
  reliable_exit_code: true

codex:
  headless:  ["codex","exec","{prompt}","--skip-git-repo-check"]
  structured: {flag: "--output-schema", form: file}       # ★ファイル渡しのみ
  session:
    id_origin: callee                                     # ★ツール側が採番
    id_capture: {stream_event: "thread.started", field: "thread_id"}
    resume: ["exec","resume","{sid}"]
  permission:
    readonly:  ["-s","read-only"]
    write:     ["-s","workspace-write"]
    # ★実測A-2: resume では -s が使えない。再開時はこちらを使う
    resume_readonly: ["-c",'sandbox_mode="read-only"']
    resume_write:    ["-c",'sandbox_mode="workspace-write"']
  result_path: "last(.item.type==agent_message).text"     # ★実測A-3: 必ず最後を採る
  cost_path:   null                                       # トークンのみ
  reliable_exit_code: true

agy:
  headless:  ["agy","-p","{prompt}"]
  structured: {flag: "--json-schema", form: file_or_string}
  session:
    id_origin: callee
    id_capture: {json_field: "conversation_id"}
    resume: ["--conversation","{sid}"]
  permission:
    readonly:  ["--sandbox"]
    write:     ["--dangerously-skip-permissions"]
  result_path: ".structured_output"
  cost_path:   null
```

### 4.1 宣言が吸収する実測差分（すべて EVIDENCE.md 由来）

| 実測 | 素朴な設計だとどうなるか | 本設計の吸収先 |
|---|---|---|
| A-1 セッションID採番主体が違う | 統制側で事前採番 → codex/agy で破綻 | `id_origin` + **ハンドル間接化**（§5.3） |
| A-2 `codex exec resume` が `-s` を拒否 | 再開時に権限がドリフト、または落ちる | `resume_*` を別宣言し、**毎回再宣言** |
| A-3 `agent_message` が複数出る | 空の先頭JSONを拾い誤判定 | `result_path: last(...)` |
| A-4 コスト単位が違う | 予算制御が claude でしか効かない | トークンへ正規化、価格表はハーネス側 |
| 構造化の渡し方が inline/file で違う | 文字列連結が破綻 | `structured.form` |

**不変条件**: `if vendor == "codex"` をハーネス本体に書いた時点で設計違反。分岐は必ず宣言に置く。

---

## 5. L2 台帳 — 単一真実源

### 5.1 形式: append-only の JSONL

```
ledger/events.jsonl        # 追記専用。過去は書き換えない
ledger/state/              # events から再構成される派生ビュー（消しても再生成可）
```

「上書き」を排することで、並行書き込みの競合と、状態の履歴喪失を同時に解決する。
**派生ビューはキャッシュに過ぎず、真実は常にイベント列**にある。

### 5.2 イベントの型（抜粋）

```jsonc
{"ts":"...","type":"task.created","task_id":"T-012","parent":"T-003","spec_id":"S-004"}
{"ts":"...","type":"task.leased","task_id":"T-012","role":"implementer",
 "agent":"claude","lease_until":"...","budget_tokens":120000}
{"ts":"...","type":"agent.invoked","task_id":"T-012","handle":"H-77",
 "vendor_session":"7a13795a-...","permission":"write"}
{"ts":"...","type":"artifact.produced","task_id":"T-012","paths":["fizz.py"],"commit":"a1b2c3d"}
{"ts":"...","type":"verification.run","task_id":"T-012","evidence_id":"E-991",
 "cve":"local-win-py311","exit_code":0}
{"ts":"...","type":"judgment","task_id":"T-012","verdict":"pass","cites":["E-991"]}
{"ts":"...","type":"escalation","task_id":"T-012","reason":"budget_exceeded"}
```

### 5.3 ★ハンドル間接化（A-1 への回答）

統制側は**論理ハンドル `H-77`** だけを扱い、ベンダーの実IDは対応表に隠す。

```
H-77 → {vendor: claude, session: "7a13795a-...", id_origin: caller}
H-78 → {vendor: codex,  session: "019fd69e-...", id_origin: callee}
```

`id_origin: callee` の場合、ハンドルは初回実行**後**に実IDで埋まる。
上層は「ハンドルに向かって喋る」だけでよく、採番主体の差を知らない。

### 5.4 記憶の四層と役割分担（「全部共有」を明確に否定する）

実測 E-2b の通り、各ベンダーは**自前のセッション記憶を持つ**。これは共有できない。
共有しようとすれば、全文脈を毎回転写することになり、E-5 のコストで破綻する。
したがって**共有するものと、しないものを分ける**。

| 層 | 実体 | 共有 | 寿命 | 用途 |
|---|---|---|---|---|
| **憲法** | `constitution.md` | 全員に毎回注入 | 恒久 | 禁止事項・出力契約・安全規則。短く保つ |
| **台帳** | `ledger/events.jsonl` | 全員が**簡報経由**で読む | 恒久 | 決定・判定・証拠。★これが唯一の真実源 |
| **作業文脈** | `workspaces/<task>/` | そのタスクの担当のみ | タスク中 | コードと中間生成物 |
| **ベンダー内記憶** | 各CLIのセッション | **共有しない（できない）** | セッション中 | そのエージェントの思考の連続性 |

**設計判断**: ベンダー内記憶は「あれば速い、無くても正しい」ものとしてのみ使う。
セッションが失われても、簡報を作り直せば別のエージェントが仕事を継げる。
**記憶は最適化であって、正しさの前提にしない。**

### 5.5 簡報（briefing）— 文脈予算の実装

台帳全体を渡さない。役割ごとに**必要な断面だけ**を組み立てて渡す。

| 役割 | 受け取る簡報 | 予算 |
|---|---|---|
| Architect | 要求 + 制約 + 既存の決定一覧 | 中 |
| Decomposer | 仕様 + 能力表 + 依存関係 | 小 |
| Implementer | 自タスクの仕様 + 受入基準 + 触ってよいパス | 中 |
| Reviewer | 差分 + **証拠ログ** + 受入基準（★ソース全体は渡さない） | 小 |
| Integrator | 判定済み一覧 + 衝突情報 | 小 |

E-5 の実測（16行のレビューに13.8万トークン）が示す通り、
**渡す文脈を絞ることは節約ではなく、成立条件**である。

### 5.6 ★簡報は「埋め込み」で渡す（S2実証で判明した必須要件）

簡報は**パスの列挙ではなく、中身の埋め込み**でなければならない。

`S2_VALIDATION.md` §4 の実測: レビュアに「実行するな」とだけ伝えた場合、
**「ファイルを読むこともできない」と解釈して判定不能に陥り、しかも `fail` を返した。**
——成果物と無関係な fail が、別経路で再発した。

```
=== ARTIFACT UNDER REVIEW (src: fizz.py) ===
<ソース本文をここに埋め込む>
=== EVIDENCE E-991 (cve: local-win-py311, cmd: python fizz.py) ===
<証拠本文をここに埋め込む>
```

> **原則: レビュアはファイルシステムに触れる必要が無い状態にする。**
> 「read-only にする」だけでは不十分で、「探しに行かせない」まで詰めて初めて判定が環境から独立する。
> 簡報は文脈節約策であると同時に、**判定独立性の実装**である。

---

## 6. L3 統制 — 役割・分解・割当

### 6.1 役割は「席」であって「人」ではない

役割はベンダーに固定しない。**席に誰を座らせるかは設定1行**（ゴールG1）。

| 席 | 責務 | 権限 | 既定の座り手 |
|---|---|---|---|
| Architect | 設計・アーキ設計・方式選定 | read-only | 推論の強いもの |
| Decomposer | タスク分解・DAG構築・割当案 | read-only | 安価なもの |
| Implementer × N | 実装 | write（自worktreeのみ） | 並列数を稼げるもの |
| Reviewer | **証拠を読んで**判定 | **read-only（強制）** | Implementer と**別ベンダー**（強制） |
| Integrator | 統合・衝突解消 | write（統合ブランチ） | 任意 |

**強制制約2つ**
- `Reviewer.permission == read-only`: レビュアに実行させない。実行はCVEの仕事（§7）。
- `Reviewer.vendor != Implementer.vendor`: 同一ベンダーの自己甘受を防ぐ。同一モデルは同一の盲点を持つ。

### 6.2 タスク分解の受入契約

分解の良し悪しは主観になりやすいので、**機械検査できる条件**を課す。
Decomposer の出力は下記を満たさなければ差し戻す（LLMに再考させる前に、まず構造検査）。

```jsonc
{"task_id":"T-012",
 "goal":"...",
 "acceptance":[                       // ★必須: 空なら即差し戻し
   {"check":"cmd","run":"python -m pytest tests/test_fizz.py","expect_exit":0}
 ],
 "touch_allow":["src/fizz.py","tests/test_fizz.py"],  // ★触ってよい範囲を宣言
 "depends_on":["T-011"],
 "est_tokens":40000}
```

| 検査 | 落ちたら |
|---|---|
| `acceptance` が空でない | 差し戻し（検証不能なタスクを作らせない） |
| `acceptance[].run` がCVEで**実行可能**な形式 | 差し戻し |
| DAGに循環が無い | 差し戻し |
| `touch_allow` が他の並行タスクと重複しない | 直列化するか分解し直す |

**設計意図**: 「検証方法を書けないタスクは、そもそもタスクとして未定義である」という規律を、
人間のレビューでなく構造検査で強制する。

### 6.3 リース方式の割当（クラッシュ耐性）

割当は「代入」ではなく**期限付きリース**にする。

```
task.leased(T-012, agent=claude, lease_until=T+15min, budget_tokens=120000)
```

- リース期限切れ = 自動的に再割当可能。エージェントが落ちても**放置されたタスクが生まれない**。
- 再割当時は**別ベンダー**を優先（同じ環境要因で再び詰まるのを避ける）。
- リース更新はハートビートでなく、**進捗イベントの追記**で行う（常駐不要 = P1）。

---

## 7. ★L4 検証と信用 — 本設計の心臓部

### 7.1 判定を3段に分ける

素朴な設計は「レビュアが合否を言う」の1段。これが E-4 を生んだ。本設計は3段に分ける。

```
 ①事実収集(機械)      ②解釈(エージェント)      ③裁定(機械)
 CVEで実行           証拠を読んで所見を述べる    規則で合否を確定
 ↓                   ↓                        ↓
 証拠E-991           findings[] + cites[]      verdict
 (誰の意見でもない)   (実行はしない)            (規則。意見ではない)
```

- **①事実収集**: `acceptance[].run` を **CVEで**実行し、exit code・stdout・stderr を証拠として台帳に固定する。
  ここにLLMは一切関与しない。**再現可能で、誰の環境にも依存しない。**
- **②解釈**: Reviewer は read-only で、**証拠と差分だけ**を読む。
  「動かして確かめろ」とは**決して指示しない**。所見には必ず証拠IDを引用させる。
- **③裁定**: 下の規則表で機械的に確定する。エージェントの `verdict` 文字列は**そのままでは採用しない**。

### 7.2 裁定規則（E-4 の偽fail がここで消える）

| ①CVEの証拠 | ②レビュアの所見 | ③確定判定 | 根拠 |
|---|---|---|---|
| 全て exit 0 | 指摘なし | **pass** | |
| 全て exit 0 | 指摘あり(証拠ID付き) | **pass_with_findings** → 指摘は新タスク化 | 動くものは止めない |
| 全て exit 0 | 指摘あり(**証拠IDなし**) | **指摘を破棄**しpass | P3: 証拠なき判定は無効 |
| 失敗あり | — | **fail** | 事実が優先 |
| **CVEプローブが失敗** | — | **environment_error**（タスクは無傷で保留、人間へ） | ★G2 |
| レビュア呼出が失敗 | — | **judgment_unavailable**（再割当。成果物はfailにしない） | ★G2 |

**E-4 を本設計に通すとどうなるか**（設計の妥当性の自己検証）:

| 段 | 実際に起きること |
|---|---|
| ① | CVE で `python fizz.py` を実行 → **exit 0**（実測で確認済み: 正常出力を得た） |
| ② | codex は read-only で証拠を読む。実行しないので「python が無い」は**そもそも発生しない** |
| ③ | 証拠が exit 0 → **pass** |

**結論: 偽fail は構造的に発生しない。** レビュアの環境は判定に影響できない。
仮に codex 自体が起動不能でも、結果は `judgment_unavailable` であって `fail` ではない。
**「成果物が悪い」と「判定できなかった」を混同しない**——これが G2 の実装である。

### 7.3 レビュアへの出力契約（A-3 対策込み）

```jsonc
// 必ず --json-schema / --output-schema で強制する
{"findings":[{"severity":"high|med|low","claim":"...","cites":["E-991"],"path":"src/x.py:42"}],
 "unverifiable":["..."],          // 証拠が足りず判断できなかった点（正直に言わせる）
 "opinion_verdict":"pass|fail"}   // ★参考値。裁定には使わない
```

- 出力の取り出しは `result_path` に従い、**最後の agent_message** を採る（実測A-3）。
- `cites` が空の finding は §7.2 により破棄される。**レビュアに「証拠を引け」と構造で強制する。**
- `opinion_verdict` という名前にしてあるのは、**それが意見に過ぎないことを設計上明示する**ため。

---

## 8. L5 人間の位置 — 承認者であって監視者ではない

### 8.1 人間に上がるものだけを上げる

```
approvals/pending/<id>.md     # ここに入ったものだけが人間の仕事
```

| 人間へ上げる | 上げない（自動で流す） |
|---|---|
| 仕様の曖昧さ・要求の矛盾 | 実装の細部 |
| 破壊的操作（force push / 本番 / 秘匿情報） | 通常のコミット |
| 予算超過 | 予算内の消費 |
| `environment_error`（CVE不良） | 通常のfail（自動で再試行/再割当） |
| 同一タスクの2回連続fail | 1回目のfail |

**設計意図**: 人間が全出力を読む設計は、エージェントを増やすほど人間が破綻する。
**エージェント数に対して人間の負荷が増えない**ことを構造で保証する。

### 8.2 全体把握（G3）

台帳から単一コマンドで再構成する。ダッシュボードは**状態を持たない**（派生ビュー）。

```bash
super-agent status
# TASKS  running:3  blocked:1  awaiting_review:2  done:11
# BUDGET 412k / 1.0M tokens (41%)   est $8.20
# NEEDS-YOU  2  → approvals/pending/
# STALE  T-019 lease expired 4m ago → auto-reassign pending
```

### 8.3 安全（最小限だが絶対）

- 破壊的操作（`rm -rf`, `git push --force`, `reset --hard`, 秘匿ファイル読取）は**アダプタ層で遮断**。
  タスク指示で上書きできない。
- **外部から取得した文字列（README・Web・ファイル内容）は常にデータであり、命令ではない。**
  実行してよいのは `acceptance[].run` と CVE の定義済みコマンドのみ。
- write権限は**自分の worktree のみ**。`touch_allow` 外への書き込みは統合時に検出して差し戻す。

---

## 9. 一周の流れ（要求 → 改良）

ご要望の9項目が、どこで満たされるか。

```
[要求]
  ↓ ① Architect(read-only) …………………………… 設計・アーキ設計
  ↓    → 決定を台帳へ (ADR形式)
  ↓ ② Decomposer(read-only) ………………………… タスク分解
  ↓    → DAG + acceptance + touch_allow
  ↓    → §6.2 構造検査（機械）。落ちたら差し戻し
  ↓ ③ Scheduler(機械) ……………………… チーム編成・役割/タスク割当
  ↓    → 席に座り手を割当、worktree 作成、リース発行
  ↓ ④ Implementer × N(write) ……………………………… 実装（並列）
  ↓    → 成果物 + commit
  ↓ ⑤ CVE(機械) …………………………………………… 事実収集
  ↓    → 証拠（誰の環境にも依存しない）
  ↓ ⑥ Reviewer(read-only, 別ベンダー) ………………… レビュー
  ↓    → 証拠を読んだ所見（実行はしない）
  ↓ ⑦ Adjudicator(機械) ……………………………………… 裁定
  ↓    → pass / fail / environment_error / judgment_unavailable
  ↓ ⑧ Integrator(write) …………………………………… 統合
  ↓ ⑨ 台帳へ記録 ……………………………… 全体把握 / メモリ共有
  ↓ ⑩ 失敗パターンを憲法へ昇格 ……………………………… 改良
[完成]
```

### 9.1 改良ループ（G6 自己改良）

同じ失敗を二度させないための仕組みを、**人間の記憶ではなく構造**に置く。

| 観測 | 自動で起こすこと |
|---|---|
| 同種の fail が3回 | その検査を `acceptance` の**既定テンプレ**に昇格 |
| ある席で `judgment_unavailable` 多発 | その席の既定ベンダーを差し替え候補として提示 |
| `environment_error` 発生 | CVE定義の不備として人間へ（タスクの失敗にはしない） |
| 特定ベンダーのコスト超過が常態化 | 席の割当方針を提示（例: Decomposer を安価側へ） |

**本システム自身の改良も、同じ①〜⑩を通す。** 特別扱いしない。

---

## 10. 実装順序（小さく始める）

各段階は**それ単体で価値が出る**ように切ってある。

| 段 | 作るもの | これで得られること | 検証 |
|---|---|---|---|
| **S1** | 台帳(JSONL) + アダプタ宣言 + `invoke` 1本 | 3ベンダーを同一IFで叩ける | 3者に同一スキーマを投げて3JSONを得る（**実測済み E-2a**） |
| **S2** | CVE + 証拠固定 + 簡報埋め込み | 判定が環境非依存になる | **E-4の偽failが pass になること → 実証済み（`S2_VALIDATION.md`）** |
| **S3** | 席 + リース + worktree | 並列実装が壊れない | 2実装を並列実行して衝突ゼロ |
| **S4** | 3段判定 + 裁定規則 | 信用できる合否 | 4分類が全部出ること → **実証済み（5ケース）** |
| **S5** | 予算 + 承認キュー + status | 人間の負荷が一定になる | 予算超過で安全停止、要判断のみ上がる |
| **S6** | 改良ループ | 同じ失敗を繰り返さない | 本システム自身の改良を1周通す |

**S2 が最優先。** ここが本設計の存在理由であり、S2 が動けば残りは配管である。

---

## 11. 本設計が明示的に否定する「よくある設計」

| よくある案 | なぜ否定するか |
|---|---|
| エージェント同士を会話させる | 高コスト・非決定的・検証不能。証拠の受け渡しで足りる |
| レビュアに「動かして確認して」と頼む | **E-4 で実測した通り、レビュアの環境を測ってしまう** |
| 全エージェントで巨大メモリを共有 | ベンダー内記憶は共有不能(E-2b)。転写はE-5のコストで破綻 |
| 常駐プロセス＋inotifyで起こす | 本機に inotifywait 無し(E-1)。落ちたときの復旧が難しい |
| 多数決で合意形成 | 同一の盲点を持つモデル同士の多数決は精度を上げない。事実(CVE)が優先 |
| オーケストレータを賢いLLMにする | 単一障害点かつ非決定的。統制は機械（規則）で足りる |

---

## 12. 未解決の論点（正直に残す）

| # | 論点 | 現状の扱い |
|---|---|---|
| U1 | CVEが複数必要な場合（例: Windows/Linux両検証） | 現設計はCVE単一。複数化は `cve: [...]` への拡張で対応予定 |
| U2 | 「設計」自体の良否は機械検証できない | 設計成果物は §7 の対象外。人間承認（§8.1）に残す |
| U3 | ベンダーのCLI仕様変更 | 宣言YAMLに集約済みだが、**起動時プローブで検出**する仕組みが必要 |
| U4 | 長時間タスクのリース期限の妥当値 | 実運用データが無い。初期値15分、実測で調整 |
| U5 | agy の権限粒度が粗い（`--sandbox` か全許可） | 現状はworktree隔離で補う。粒度が必要なら別席へ |


