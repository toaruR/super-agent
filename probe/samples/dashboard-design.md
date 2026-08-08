# ダッシュボード要件（md / html 出力）

## 目的
スーパーエージェントの実行台帳（ledger/events.jsonl）を読み、進捗を
Markdown と HTML の両方で可視化するダッシュボードを生成する。

## 要件
- 台帳の全イベント（task.leased / implement.ok / review.pass / integrate.ok 等）
  を読み、タスクごとに状態を集約する。
- Markdown 出力：タスク一覧をテーブル形式で出力する。
- HTML 出力：Markdown と同等の内容を、ブラウザで開ける単体 HTML として出力する。
- エントリポイント：`dashboard` コマンド（harness.cli 経由）で呼び出せること。

## 受け入れ基準
- `harness/roles/dashboard.py` に `build_model(events)` が存在し、台帳イベントを
  構造化モデルに変換すること。
- `render_markdown(model)` が Markdown 文字列を返すこと。
- `render_html(model)` が単体で開ける HTML 文字列を返すこと。
- `harness/cli.py` に `dashboard` サブコマンドが追加され、`--format md|html|both`
  を選択できること。
