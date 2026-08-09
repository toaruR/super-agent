# タスク分解: status-improvement

## 1. T-STATUS-01
- 目標: harness/cli.py の cmd_status を改修し、全体の進捗率サマリー、論理タスク一覧、マイルストーンログを出力する。
- 依存: （なし）
- 触ってよい範囲: harness/cli.py, harness/tests/test_cli.py
- 受入基準 (1):
  - `pytest` harness/tests/test_cli.py
