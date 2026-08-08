# ダッシュボード要件（改良：状態遷移の優先順位 + render の分離）

## 背景（現在の不具合）
- `build_model(events)` は後勝ちで上書きするため、後ろの `judgment: judgment_unavailable`
  が `task.implemented` / `integrated` を上書きし、最終ステータスが `unknown` /
  `judgment_unavailable` だらけになる。
- `render_markdown` / `render_html` が `harness/cli.py` のフォールバック実装に散在し、
  `dashboard.py` には存在しない（設計要件違反）。

## 目的
台帳イベントを読み、タスクごとに「最終的な進行状態」を正しく集約し、
Markdown / HTML で可視化する。

## 要件（改良）

### A. build_model の状態遷移優先順位
- 各 task_id について、全イベントを走査し、**最も「進んだ」状態**を最終ステータスとする。
- 状態の重み付き順位（進んだ方が強い）：
  `integrated` > `passed` > `implemented` > `leased` > `scheduled` > `created` > `unknown`
- `judgment_unavailable` や `judgment:*` は「判定不能」であり、より進んだ実装状態
  （implemented / integrated 等）を上書きしてはならない。つまり判定不能は
  実装状態より弱い。
- 複数の `judgment:*` があっても、実装状態があれば実装状態を優先。
- イベントに `status` フィールドがある場合はその値を採用（ただし上記順位で弱いものは上書きしない）。

### B. render 関数を dashboard.py へ移動
- `harness/roles/dashboard.py` に `render_markdown(model)` と `render_html(model)` を実装。
  - `render_markdown(model)`: タスク一覧を Markdown テーブル（`| Task ID | Status |`）
    で返す。model は `dict[task_id -> status]` または `dict[task_id -> {status, ...}]`
    の両方を受け、後者なら `.get("status")` を使う。
  - `render_html(model)`: 同等の内容をブラウザで開ける単体 HTML 文字列で返す。
- `harness/cli.py` の `cmd_dashboard` は、`dashboard.py` から `build_model, render_markdown, render_html`
  を import する（フォールバック定義は削除してよい）。`--format md|html|both` と
  `--out` の既存動作は維持。

## 受け入れ基準
- `pytest harness/tests/test_dashboard.py` が全て通る（既存 test_build_model / test_build_model_empty
  に加え、test_render_markdown / test_render_html も追加して通す）。
- `super-agent dashboard --format both` で、実際の ledger から `integrated` / `implemented`
  等の正しい最終ステータスが出る（unknown だらけにならない）。
- `harness/roles/dashboard.py` に `build_model` / `render_markdown` / `render_html` の3つが揃う。
