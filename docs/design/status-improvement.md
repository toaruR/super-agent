# 設計: super-agent status コマンドの表示改善

## 概要
`./super-agent status` コマンドが現状、Ledger (events.jsonl) の末尾20件の生イベント文字列を表示するだけになっており、システム全体の進捗状況やタスクの成否が直感的に把握できない課題を解決する。

## 要件
1. **全体サマリー表示**:
   - Ledger 全イベントから算定した総タスク数、およびステータス別（Integrated, In-Progress, Failed, Scheduled など）の件数とパーセンテージ表示。
   - CLI 向けアスキープログレスバー ([████░░░░] 50%) の出力。

2. **論理タスク (Logical Task) テーブル**:
   - task_id（投機試行 PA__hermes_0 のような部分を親 PA に集約）ごとの最終ステータスを一覧表示。
   - 表示項目: Task ID, Status (テキスト/カラー表記), 選ばれた Winner / Channel (存在する場合)。

3. **直近ハイライトイベント**:
   - 単なる末尾ログではなく、重要なマイルストーンイベント（integrated, review.pass, implementer.error, conflict, judgment）の直近5件を表示。

4. **後方互換性と安全性の確保**:
   - イベントが空の場合や予期せぬスキーマの場合でもエラーでクラッシュせずフォールバック表示する。

## 影響範囲
- harness/cli.py (cmd_status 関数の実装)
- harness/tests/test_cli.py (CLI status 関数のユニットテスト)
