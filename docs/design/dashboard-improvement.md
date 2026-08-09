# 設計: super-agent dashboard コマンドの可視化・リッチ化改善

## 概要
`./super-agent dashboard` コマンドが生成する HTML/Markdown レポートを改善し、投機実行（サブチャネル PA__hermes_0 等）を親論理タスク（PA）へ集約するとともに、全体進捗カードやカラーバッジ付きのモダンな HTML デザインを提供する。

## 要件
1. **論理タスク集約とステータス評価 (build_model)**:
   - 投機試行チャネル（例: PA__hermes_0）を識別し、親論理タスク（PA）の配下またはモデル全体として集約する。
   - 過渡イベント（verification.run 等）で状態が止まらず、終端ステータス（integrated, passed, failed, leased）へ繰り上げる。

2. **リッチな HTML レンダリング (render_html)**:
   - ダークモード基調のモダンな CSS デザイン。
   - 全体進捗カード（総タスク数、Completed 率 %、ステータス分布）。
   - 各ステータスのカラーバッジ（Integrated: 緑, Passed: 緑, In Progress: 青, Failed: 赤, Scheduled: 灰）。

3. **Markdown レンダリング強化 (render_markdown)**:
   - 全体進捗サマリー表と論理タスクステータステーブルを出力。

## 影響範囲
- harness/roles/dashboard.py
- harness/tests/test_dashboard.py
