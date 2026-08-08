# タスク分解（decompose 出力）

要求: 台帳イベントを読み、Markdown / HTML ダッシュボードを生成する機能を実装する

タスク数: 5

## 1. dashboard-model

- 目標: 台帳イベントを構造化モデルに変換する build_model(events) を実装する。task_id -> status の辞書を返す。
- 依存: （なし）
- 触ってよい範囲: harness/roles/dashboard.py, harness/tests/test_dashboard.py
- 受入基準 (1):
  - `pytest` harness/tests/test_dashboard.py::test_build_model (expect_exit=0)

## 2. dashboard-render-md

- 目標: モデルから Markdown テーブルを出力する render_markdown(model) を実装する。
- 依存: dashboard-model
- 触ってよい範囲: harness/roles/dashboard.py, harness/tests/test_dashboard.py
- 受入基準 (1):
  - `pytest` harness/tests/test_dashboard.py::test_render_markdown (expect_exit=0)

## 3. dashboard-render-html

- 目標: モデルから単体 HTML を出力する render_html(model) を実装する。
- 依存: dashboard-model, dashboard-render-md
- 触ってよい範囲: harness/roles/dashboard.py, harness/tests/test_dashboard.py
- 受入基準 (1):
  - `pytest` harness/tests/test_dashboard.py::test_render_html (expect_exit=0)

## 4. dashboard-write-entrypoint

- 目標: harness/cli.py に dashboard サブコマンドを追加し、--format で md/html/both を選択できるようにする。
- 依存: dashboard-model, dashboard-render-md, dashboard-render-html
- 触ってよい範囲: harness/cli.py, harness/tests/test_cli.py
- 受入基準 (1):
  - `pytest` harness/tests/test_cli.py::test_cli_dashboard_md_stdout (expect_exit=0)

## 5. dashboard-cli

- 目標: cli の引数パースとフォーマット分岐を整備する（dashboard コマンドの受け口）。
- 依存: dashboard-write-entrypoint
- 触ってよい範囲: harness/cli.py, harness/tests/test_cli.py
- 受入基準 (1):
  - `pytest` harness/tests/test_cli.py::test_cli_dashboard_both_writes_two_files (expect_exit=0)
