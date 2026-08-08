# タスク分解（dashboard 状態遷移優先順位化）

要求: dashboard の build_model を状態遷移優先順位方式に修正する（要件 A）。
render 関数の移動（B）は既に完了済み（commit 9c3d227）。

タスク数: 1

## 1. dashboard-status-priority

- 目標: `build_model(events)` を状態遷移優先順位方式に修正する。詳細は dashboard-priority-design.md の要件 A を参照。
- 依存: （なし）
- 触ってよい範囲: harness/roles/dashboard.py, harness/tests/test_dashboard.py
- 受入基準 (1):
  - `pytest` harness/tests/test_dashboard.py (expect_exit=0)
