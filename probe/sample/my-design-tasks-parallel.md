# タスク分解（decompose 出力）

要求: リポジトリ直下に probe_a.txt / probe_b.txt を並列作成し、内容をそれぞれ固定文字列とする（実装のみ、テスト不要）

タスク数: 2

## 1. PA

- 目標: リポジトリ直下に `probe_a.txt` を作成し、内容を `hello from probe A` とする。
- 依存: （なし）
- 触ってよい範囲: probe_a.txt
- 受入基準 (1):
  - `pytest` --version (expect_exit=0)

## 2. PB

- 目標: リポジトリ直下に `probe_b.txt` を作成し、内容を `hello from probe B` とする。
- 依存: （なし）
- 触ってよい範囲: probe_b.txt
- 受入基準 (1):
  - `pytest` --version (expect_exit=0)
