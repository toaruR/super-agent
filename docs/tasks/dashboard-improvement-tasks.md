# タスク分解: dashboard-improvement

## 1. T-DASHBOARD-01
- 目標: harness/roles/dashboard.py の build_model, render_markdown, render_html を改修し、論理タスクの進捗集約、進捗サマリーカード、ダークモードスタイルのステータスバッジ (CSS) を追加する。harness/tests/test_dashboard.py のテストを更新する。
- 依存: （なし）
- 触ってよい範囲: harness/roles/dashboard.py, harness/tests/test_dashboard.py
- 受入基準 (1):
  - `pytest` harness/tests/test_dashboard.py
