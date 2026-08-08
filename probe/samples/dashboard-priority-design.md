# ダッシュボード要件（改善：状態遷移の優先順位）

## 背景（現在の不具合）
`harness/roles/dashboard.py` の `build_model(events)` は、台帳イベントを
順に上書きする（後勝ち）。そのため、後ろの `judgment: judgment_unavailable`
イベントが `task.implemented` / `integrated` を上書きし、最終ステータスが
`unknown` / `judgment_unavailable` になる。本来は「最も進んだ状態」が
最終ステータスとなるべき。

## 要件 A: build_model の状態遷移優先順位化

`build_model(events)` を「最も進んだ状態を最終ステータスとする」優先順位方式に修正する。

### 順位（高いほど最終ステータスになる）
integ > implemented > failed > leased > created > unknown

- 各イベントの `type`（および `verdict` 等）から状態を決定し、既存の
  `STATUS_MAP` を利用する。
- 同じ task_id に対し、高い順位の状態が来たら上書き、低い順位なら維持する
  （後勝ちではなく順位比較）。
- 順位が同じ場合のみ後勝ち（時系列で最新を反映）。

### 受入基準
- `pytest` harness/tests/test_dashboard.py (expect_exit=0)
- 追加で、`integrated` イベントの後に `judgment: judgment_unavailable` が
  あっても、最終ステータスが `integrated` になることを確認するテストを
  `test_dashboard.py` に追加する（test_build_model_priority 等）。

### 触ってよい範囲
- harness/roles/dashboard.py
- harness/tests/test_dashboard.py
