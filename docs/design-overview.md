# super-agent — 設計概要（旧 README から移動）

> この文書は、プロジェクトの**設計背景・中心命題・実測証拠・現状の詳細**をまとめたもの。
> プロジェクトの概要・導入・使い方のクイックスタートはルートの
> [`README.md`](../README.md) を参照。実際に動かす手順は [`usage.md](usage.md) を参照。

Claude Code / Codex / Antigravity など**ベンダーの異なるコーディングエージェント**を跨いでチームを組み、
設計〜実装〜レビュー〜改良を協同で回すシステムの大枠設計。

## 読む順番

> **全ドキュメントの目録（何がどこにあるか）は [`docs/catalog.md`](docs/catalog.md) を参照。**
> ドキュメント構造のゴールと評価方法は [`docs/goals/documentation.md`](docs/goals/documentation.md)。

| # | 文書 | 内容 |
|---|---|---|
| 1 | [`docs/goals/design.md`](docs/goals/design.md) | **ゴール**（何を良しとするか）と**評価方法**（100点ルーブリック） |
| 2 | [`docs/evidence/000-base-evidence.md`](docs/evidence/000-base-evidence.md) | 設計の前提となる**実測結果**（推測を排するため設計前に実施） |
| 3 | [`docs/spec.md`](docs/spec.md) | **大枠設計 本体**（v3） |
| 4 | [`docs/design-notes/scoring.md`](docs/design-notes/scoring.md) | v1(52点)→v2(79点)→v3(92点) の**採点と改稿の記録** |
| 5 | [`docs/evidence/0606-s2-validation.md`](docs/evidence/0606-s2-validation.md) | 設計の中心主張を**実機で検証**した結果（§5.6 の訂正を含む） |
| 6 | [`docs/evidence/0606-n3-large-diff.md`](docs/evidence/0606-n3-large-diff.md) | N=3→N=4 実測と、大きな差分への対処（決定関連性による劣化） |
| 7 | [`docs/evidence/0606-permission-control.md`](docs/evidence/0606-permission-control.md) | 起動オプションによる制御の実測。**read-only は実行を止めない**という発見 |
| 8 | [`docs/design-notes/review-response.md`](docs/design-notes/review-response.md) | 独立レビュー（`unsound`）への回答・自己採点92点の撤回・改訂案 |
| 9 | [`docs/plan.md`](docs/plan.md) | 設計を動くハーネスにする実装計画（Stage A→F） |
| 10 | [`docs/usage.md`](docs/usage.md) | **使い方マニュアル**（実際に動かす手順） |

## 一言でいうと

> **実行と判定を分離する。**
> 決定的な検証はハーネスが唯一の環境で行い、エージェントは「証拠を読む」だけにする。
> エージェントは賢い個人ではなく、**交換可能な作業員**として扱う。信頼は人格でなく**証拠と手続き**に置く。

## なぜそう設計したか

設計前に実機で測ったところ、**claude が書いた正しいコードを codex がレビューして `fail` を返した。**
原因は成果物ではなく**レビュア側の実行環境**（python が見つからない）だった。

> マルチエージェントの難所は「連携」に留まらず、**「判定の信用」が独立の難所**となる。
> 「動かして確かめて合否を言え」と頼む限り、測っているのは成果物ではなく**そのエージェントの環境**である。

この1件が設計全体の背骨を決めている。

## 検証済みの主張

| 主張 | 状態 |
|---|---|
| 3ベンダーが同一スキーマで構造化出力を返す | ✅ 実測（疎通レベル） |
| 3ベンダーが別プロセスからセッション再開できる | ✅ 実測（合言葉レベル） |
| 実行と判定の分離で**偽failが消える** | ✅ **N=4**（16行/2/3/42ファイル）。ただし「レビュア環境起因の偽fail」1経路のみ |
| 裁定4分類が機能 + 意見が割れても裁定一致 | ✅ **実CVE＋実LLMで確認**（advisory保持。合成入力から実測へ昇格） |
| レビュア役をベンダー差し替えしても動く | ✅ **claude/codex/agy の3者**でパス渡しレビューを確認 |
| 簡報は**パス渡し**が既定（埋め込みは代替） | ✅ 実測。パス渡しのほうが指摘の質が高い |
| **read-only 権限は実行を止めない** | ✅ 実測（claude/codex）。独立性は権限ではなく裁定器で担保 |
| **acceptance の質が正しさの天井** | ⚠ 実例あり（Case B。テスト通過でも欠陥残る） |

> **注意**: 上記の✔は「中心命題＋周辺の一部」が小規模題材（最大42ファイル）で動くことを示す。
> 本番規模（依存関係が複雑・複数ランタイム・長時間タスク）での成立は**未検証**。
> 「実証済み」と読まないこと。

## 現状

**実装フェーズ（進行中）。** 中心命題は N=4 で実証、周辺設計は部分的に実証、製品コードは**主要部が実装済み**。

`probe/n3/` に実機検証のコードと証跡がある（CVE `cve.py` / 簡報 `brief.py` / 裁定 `adjudicate2.py`）。
S2（実行と判定の分離）・S4（3段裁定）・S5予算（簡報圧縮）は**実コードで確認済み**。
S1（台帳）・S3（並列/リース/worktree）は**実装済み**（`harness/core/ledger.py`・`scheduler.py`・`drive.py`）。
S6（改良ループ）・S7（OSレベル隔離）は**未実施**。

本設計は別ベンダー（codex）による独立レビューを受けており、判定は **`unsound`**（過大主張26件・
構造的欠陥18件・high 8件）。その後の自らと独立レビュア双方による実測で、中心命題は N=4 で支持された。
詳細と訂正は [`docs/design-notes/review-response.md`](docs/design-notes/review-response.md)、
権限実測は [`docs/evidence/0606-permission-control.md`](docs/evidence/0606-permission-control.md)、
N=3＋大きな差分は [`docs/evidence/0606-n3-large-diff.md`](docs/evidence/0606-n3-large-diff.md)。

実装は `docs/spec.md` §10 の S1→S7 の順で進めており、
**現在は Stage 0〜5（台帳・検証・分解・スケジュール・実装・統合）＋ Stage B 並列駆動（マルチチャンネル実装＋タスク並列）が実装済み。**
実際に動かす手順は **[`docs/usage.md`](docs/usage.md)** を参照。
