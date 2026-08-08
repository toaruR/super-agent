# タスク分解（dashboard 改良）

要求: ダッシュボードの build_model を状態遷移優先順位化し、render 関数を dashboard.py へ移動する

タスク数: 3

## 1. dashboard-model-fix

- 目的: build_model を最終ステータス優先順位方式に修正する。詳細は design.md の要件 A を参照。
- 依存: （なし）
- 触ってよい範囲: harness/roles/dashboard.py, harness/tests/test_dashboard.py
- 受入基準 (1):
  - `pytest` harness/tests/test_dashboard.py (expect_exit=0)

## 2. dashboard-render-move

- 目的: render_markdown と render_html を dashboard.py に実装し、cli.py が import するよう修正する。詳細は design.md の要件 B を参照。
- 依存: dashboard-model-fix
- 触ってよい範囲: harness/roles/dashboard.py, harness/cli.py, harness/tests/test_dashboard.py, harness/tests/test_cli.py
- 受入基準 (1):
  - `pytest` harness/tests/test_dashboard.py (expect_exit=0)
- 受入基準 (2):
  - `pytest` harness/tests/test_cli.py (expect_exit=0)

## 3. dashboard-verify

- 目的: dashboard コマンドの出力を確認し、正しい最終ステータスが出ることを目視確認する。（受入基準なし、確認のみ。）
- 依存: dashboard-model-fix, dashboard-render-move
- 触ってよい範囲: （確認のみ）
- 受入基準 (1):
  - `pytest` harness/tests/test_dashboard.py (expect_exit=0)
