# タスク分解（decompose 出力）

要求: super-agent/src のルートに CHANGELOG.md を新規作成する

タスク数: 1

## 1. T1

- 目標: このリポジトリ（super-agent/src）のルートに `CHANGELOG.md` を新規作成する。コミット履歴から主要な変更を時系列でまとめた、シンプルな変更履歴ファイルとする。あわせてその存在を確認する `tests/test_changelog.py` を作成する。
- 依存: （なし）
- 触ってよい範囲: CHANGELOG.md, tests/test_changelog.py
- 受入基準 (1):
  - `pytest` tests/test_changelog.py (expect_exit=0)
