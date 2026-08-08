# Change Log / 変更履歴

このプロジェクト（super-agent）における主な変更履歴です。

---

## [Unreleased]

### Added
- `CHANGELOG.md` を追加し、プロジェクトの変更履歴管理を開始
- `tests/test_changelog.py` を追加し、`CHANGELOG.md` の存在確認テストを作成

---

## [Stage B] - 2026-08

### Added
- **マルチチャンネル並列実行 (Stage B)**: `implementer` の複数チャンネル並列実行および敗者チャンネル worktree の自動クリーンアップ（既定 5 チャンネル化）
- **タスクレベル並列実行**: タスク DAG に基づくタスクレベルでの並列ドライブ対応
- **`drive` コマンド**: 全タスクを一括駆動（implement → review → integrate）する逐次/並列ドライブ機能
- **ベンダー追加**: Hermes を第4のベンダーとして追加 (`tencent/hy3:free`)

### Fixed & Improved
- `dry-run` 実行時に CVE 検証をスキップし計画のみ出力する仕様改善
- ベンダー設定を `vendors.yaml` の `roles:` に統一集約
- UTF-8 文字エンコーディング対応（Windows / PowerShell 実行時の cp932 クラッシュ対策）

### Documentation
- `README.md` を刷新し、詳細な設計仕様を `docs/design-overview.md` へ分離
- ドキュメント全体を最新の Stage B（並列実行・5チャンネル既定）仕様に更新
- `verification_env.yaml` のサンプル化と MIT ライセンスの明記

---

## [Stage A & Pipeline Core] - 2026-08

### Added
- **ハーネス基盤 (Stage A)**: 台帳昇格、アダプタ宣言、`invoke` コマンド、CLI 実装
- **Stage 0 - 足場**: `review`, `log`, `show` ツール群
- **Stage 1 - `architect`**: 設計決定を ADR として記録する機能
- **Stage 2 - `decomposer`**: ユーザー要求をタスク DAG へ自動分解する機能
- **Stage 3 - `scheduler`**: チーム編成、git worktree 生成、リース発行機能
- **Stage 4 - `implementer`**: isolated worktree でのタスク実装および自動コミット機能
- **Stage 5 - `reviewer` & `integrator`**: 自動コードレビューおよびマージ統合・クリーンアップ機能
- **`super-agent` CLI ラッパー**: PowerShell および git-bash に対応するランチャースクリプト (`super-agent`, `super-agent.bat`)
- **ドキュメント**: 使い方マニュアル (`USAGE.md`) の作成

---

## [Initial / Architecture Design] - 2026-08

### Added
- 異ベンダー・エージェントチームの基本設計と実機検証環境構築
- レビュープロセスを経た実装計画（Stage A〜F、harness/ 昇格方針）の立案
